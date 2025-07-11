# local_openai_history_server.py
# v12.2 - Chinese Localization

import logging
import os
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from queue import Queue, Empty
import uuid
import threading
import time
import json
import re
from datetime import datetime

# --- 全局配置 ---
CONFIG = {}
logger = logging.getLogger(__name__)

# --- Flask 应用设置 ---
app = Flask(__name__)
CORS(app)
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.disabled = True

# --- 数据存储 ---
MESSAGES_JOBS = Queue()
PROMPT_JOBS = Queue()
RESULTS = {}

# --- 模型映射 ---
MODEL_NAME_TO_ID_MAP = {}
DEFAULT_MODEL_ID = "f44e280a-7914-43ca-a25d-ecfcc5d48d09"

def load_model_map():
    global MODEL_NAME_TO_ID_MAP
    try:
        with open('models.json', 'r', encoding='utf-8') as f:
            MODEL_NAME_TO_ID_MAP = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        MODEL_NAME_TO_ID_MAP = {}

# --- API 端点 ---
@app.route('/get_config', methods=['GET'])
def get_config():
    try:
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            jsonc_content = f.read()
            json_content = re.sub(r'//.*', '', jsonc_content)
            json_content = re.sub(r'/\*.*?\*/', '', json_content, flags=re.DOTALL)
            return jsonify(json.loads(json_content))
    except Exception as e:
        logger.error(f"读取或解析 config.jsonc 失败: {e}")
        return jsonify({"error": "Config file issue"}), 500

@app.route('/')
def index():
    return "LMArena 自动化工具 v12.2 (中文本地化) 正在运行。"

@app.route('/log_from_client', methods=['POST'])
def log_from_client():
    log_data = request.json
    if log_data and 'message' in log_data:
        logger.info(f"[油猴脚本] {log_data.get('level', 'INFO')}: {log_data['message']}")
    return jsonify({"status": "logged"})

# --- 核心逻辑 (无变化) ---
def convert_openai_to_lmarena_templates(openai_data: dict) -> dict:
    model_name = openai_data.get("model", "claude-3-5-sonnet-20241022")
    target_model_id = MODEL_NAME_TO_ID_MAP.get(model_name, DEFAULT_MODEL_ID)
    message_templates = []
    for oai_msg in openai_data["messages"]:
        message_templates.append({"role": oai_msg["role"], "content": oai_msg.get("content", "")})
    if CONFIG.get("bypass_enabled"):
        message_templates.append({"role": "user", "content": " "})
    message_templates.append({"role": "assistant", "content": ""})
    return {"message_templates": message_templates, "target_model_id": target_model_id}

@app.route('/get_messages_job', methods=['GET'])
def get_messages_job():
    try: return jsonify({"status": "success", "job": MESSAGES_JOBS.get_nowait()})
    except Empty: return jsonify({"status": "empty"})

@app.route('/get_prompt_job', methods=['GET'])
def get_prompt_job():
    try: return jsonify({"status": "success", "job": PROMPT_JOBS.get_nowait()})
    except Empty: return jsonify({"status": "empty"})

@app.route('/stream_chunk', methods=['POST'])
def stream_chunk():
    data = request.json
    task_id = data.get('task_id')
    if task_id in RESULTS:
        RESULTS[task_id]['stream_queue'].put(data.get('chunk'))
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 404

@app.route('/report_result', methods=['POST'])
def report_result():
    data = request.json
    task_id = data.get('task_id')
    if task_id in RESULTS:
        RESULTS[task_id]['status'] = data.get('status', 'completed')
        logger.info(f"任务 {task_id[:8]} 已被客户端报告为完成。")
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 404

def format_openai_chunk(content: str, model: str, request_id: str):
    return f"data: {json.dumps({'id': request_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': None}]})}\n\n"

def format_openai_finish_chunk(model: str, request_id: str):
    return f"data: {json.dumps({'id': request_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\ndata: [DONE]\n\n"

def format_openai_non_stream_response(content: str, model: str, request_id: str):
    return {'id': request_id, 'object': 'chat.completion', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}

def _normalize_message_content(message: dict) -> dict:
    content = message.get("content")
    if isinstance(content, list):
        message["content"] = "\n\n".join([p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"])
    return message

def _openai_response_generator(task_id: str):
    text_pattern = re.compile(r'a0:"((?:\\.|[^"\\])*)"')
    error_pattern = re.compile(r'(\{\s*"error".*?\})', re.DOTALL)
    finish_pattern = re.compile(r'"finishReason"\s*:\s*"stop"')
    buffer = ""
    while True:
        try:
            raw_chunk = RESULTS[task_id]['stream_queue'].get(timeout=60)
            buffer += raw_chunk
            error_match = error_pattern.search(buffer)
            if error_match:
                try:
                    error_json = json.loads(error_match.group(1))
                    error_message = error_json.get("error", "来自 LMArena 的未知错误")
                    logger.error(f"任务 {task_id[:8]} 的流式响应中检测到错误: {error_message}")
                    RESULTS[task_id]['error'] = str(error_message)
                    return
                except json.JSONDecodeError: pass
            while True:
                match = text_pattern.search(buffer)
                if not match: break
                try:
                    text_content = json.loads(f'"{match.group(1)}"')
                    if text_content: yield text_content
                except json.JSONDecodeError: pass
                buffer = buffer[match.end():]
            if finish_pattern.search(raw_chunk):
                logger.info(f"检测到任务 {task_id[:8]} 的 LMArena 流结束信号。")
                return
        except Empty:
            logger.warning(f"任务 {task_id[:8]} 的生成器超时。")
            RESULTS[task_id]['error'] = '流式响应在60秒后超时。'
            return

def _load_config():
    global CONFIG
    try:
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            CONFIG = json.loads(re.sub(r'/\*.*?\*/', '', re.sub(r'//.*', '', f.read()), flags=re.DOTALL))
    except Exception as e:
        logging.error(f"无法加载 config.jsonc: {e}。将使用默认设置。")
        CONFIG = {"enable_comprehensive_logging": False}

@app.route('/v1/models', methods=['GET'])
def list_models():
    return jsonify({"object": "list", "data": [{"id": name, "object": "model", "owned_by": "local-server"} for name in MODEL_NAME_TO_ID_MAP.keys()]})

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    request_data = request.json
    if CONFIG.get("log_server_requests"):
        logger.info(f"--- 收到 OpenAI 请求 ---\n{json.dumps(request_data, indent=2, ensure_ascii=False)}")
    if not request_data or "messages" not in request_data: return jsonify({"error": "请求必须包含 'messages'"}), 400
    request_data["messages"] = [_normalize_message_content(msg) for msg in request_data.get("messages", [])]
    if not request_data["messages"]: return jsonify({"error": "'messages' 列表不能为空"}), 400
    if CONFIG.get("tavern_mode_enabled"):
        system_prompts = [msg['content'] for msg in request_data["messages"] if msg['role'] == 'system']
        other_messages = [msg for msg in request_data["messages"] if msg['role'] != 'system']
        merged_system_prompt = "\n\n".join(system_prompts)
        final_messages = []
        if merged_system_prompt: final_messages.append({"role": "system", "content": merged_system_prompt})
        final_messages.extend(other_messages)
        request_data["messages"] = final_messages
    messages_job = convert_openai_to_lmarena_templates(request_data)
    MESSAGES_JOBS.put(messages_job)
    task_id = str(uuid.uuid4())
    prompt_job = {"task_id": task_id, "prompt": f"[这条消息仅起占位，请以外部应用中显示的内容为准：/{task_id}]"}
    PROMPT_JOBS.put(prompt_job)
    RESULTS[task_id] = {"status": "pending", "stream_queue": Queue(), "error": None}
    model = request_data.get("model", "default")
    use_stream = request_data.get("stream", False)
    request_id = f"chatcmpl-{uuid.uuid4()}"
    if use_stream:
        def stream_response():
            for chunk in _openai_response_generator(task_id):
                yield format_openai_chunk(chunk, model, request_id)
            if RESULTS[task_id].get('error'):
                yield format_openai_chunk(f"[LMArena 自动化工具错误]: {RESULTS[task_id]['error']}", model, request_id)
            yield format_openai_finish_chunk(model, request_id)
        return Response(stream_response(), mimetype='text/event-stream')
    else:
        full_response_content = "".join(list(_openai_response_generator(task_id)))
        if RESULTS[task_id].get('error'):
            return jsonify({"error": {"message": f"[LMArena 自动化工具错误]: {RESULTS[task_id]['error']}", "type": "automator_error"}}), 500
        return jsonify(format_openai_non_stream_response(full_response_content, model, request_id))

if __name__ == '__main__':
    _load_config()
    if CONFIG.get("enable_comprehensive_logging"):
        log_dir = "Debug"
        os.makedirs(log_dir, exist_ok=True)
        log_filename = os.path.join(log_dir, f"debug_log_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log")
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s', handlers=[logging.FileHandler(log_filename, encoding='utf-8'), logging.StreamHandler()])
        logger.info(f"聚合日志已启用。日志文件保存至: {os.path.abspath(log_filename)}")
    else:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s', handlers=[logging.StreamHandler()])
    
    load_model_map()
    logger.info("="*60)
    logger.info("  🚀 LMArena 自动化工具 - v12.2 (中文本地化)")
    logger.info(f"  - 监听地址: http://127.0.0.1:5102")
    
    # 使用一个字典来映射配置键和它们的中文名称
    config_keys_in_chinese = {
        "bypass_enabled": "Bypass 模式",
        "tavern_mode_enabled": "酒馆模式",
        "log_server_requests": "服务器请求日志",
        "log_tampermonkey_debug": "油猴脚本调试日志",
        "enable_comprehensive_logging": "聚合日志"
    }
    
    logger.info("\n  当前配置:")
    for key, name in config_keys_in_chinese.items():
        status = '✅ 已启用' if CONFIG.get(key) else '❌ 已禁用'
        logger.info(f"  - {name}: {status}")
        
    logger.info("\n  请在浏览器中打开一个 LMArena 的 Direct Chat 页面以激活油猴脚本。")
    logger.info("="*60)
    
    app.run(host='0.0.0.0', port=5102, threaded=True)