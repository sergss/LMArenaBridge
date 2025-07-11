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
import requests
from packaging.version import parse as parse_version
import zipfile
import io
import sys
import subprocess

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

# --- 模型更新检查逻辑 ---
def extract_models_from_html(html_content):
    """
    从 HTML 内容中提取模型数据，采用更健壮的解析方法。
    """
    script_contents = re.findall(r'<script>(.*?)</script>', html_content, re.DOTALL)
    
    for script_content in script_contents:
        if 'self.__next_f.push' in script_content and 'initialState' in script_content and 'publicName' in script_content:
            match = re.search(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', script_content, re.DOTALL)
            if not match:
                continue
            
            full_payload = match.group(1)
            
            # 载荷可能包含由 \n 分隔的多个部分。我们只关心第一个主要的数据块。
            # 我们按字面上的 '\\n' 分割
            payload_string = full_payload.split('\\n')[0]
            
            json_start_index = payload_string.find(':')
            if json_start_index == -1:
                continue
            
            json_string_with_escapes = payload_string[json_start_index + 1:]
            # 移除转义的引号
            json_string = json_string_with_escapes.replace('\\"', '"')
            
            try:
                data = json.loads(json_string)
                
                # 递归查找 initialState 键
                def find_initial_state(obj):
                    if isinstance(obj, dict):
                        for key, value in obj.items():
                            if key == 'initialState' and isinstance(value, list):
                                # 确保列表不为空且包含模型字典
                                if value and isinstance(value[0], dict) and 'publicName' in value[0]:
                                    return value
                            # 即使找到一个，也要继续搜索，以防有嵌套
                            result = find_initial_state(value)
                            if result is not None:
                                return result
                    elif isinstance(obj, list):
                        for item in obj:
                            result = find_initial_state(item)
                            if result is not None:
                                return result
                    return None

                models = find_initial_state(data)
                if models:
                    logger.info(f"成功从脚本块中提取到 {len(models)} 个模型。")
                    return models
            except json.JSONDecodeError as e:
                logger.error(f"解析提取的JSON字符串时出错: {e}")
                continue

    logger.error("错误：在HTML响应中找不到包含有效模型数据的脚本块。")
    return None

def compare_and_update_models(new_models_list, models_path):
    """
    比较新旧模型列表，打印差异，并用新列表更新本地 models.json 文件。
    """
    try:
        with open(models_path, 'r', encoding='utf-8') as f:
            old_models = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        old_models = {}

    new_models_dict = {model['publicName']: model for model in new_models_list if 'publicName' in model}
    old_models_set = set(old_models.keys())
    new_models_set = set(new_models_dict.keys())

    added_models = new_models_set - old_models_set
    removed_models = old_models_set - new_models_set
    
    logger.info("--- 模型更新检查 ---")
    has_changes = False

    if added_models:
        has_changes = True
        logger.info("\n[+] 新增模型:")
        for name in added_models:
            model = new_models_dict[name]
            logger.info(f"  - 名称: {name}, ID: {model.get('id')}, 组织: {model.get('organization', 'N/A')}")

    if removed_models:
        has_changes = True
        logger.info("\n[-] 删除模型:")
        for name in removed_models:
            logger.info(f"  - 名称: {name}, ID: {old_models.get(name)}")

    logger.info("\n[*] 共同模型检查:")
    changed_models = 0
    for name in new_models_set.intersection(old_models_set):
        new_id = new_models_dict[name].get('id')
        old_id = old_models.get(name)
        if new_id != old_id:
            has_changes = True
            changed_models += 1
            logger.info(f"  - ID 变更: '{name}' 旧ID: {old_id} -> 新ID: {new_id}")
    
    if changed_models == 0:
        logger.info("  - 共同模型的ID无变化。")

    if not has_changes:
        logger.info("\n结论: 模型列表无任何变化，无需更新文件。")
        logger.info("--- 检查完毕 ---")
        return

    # 更新文件
    logger.info("\n结论: 检测到模型变更，正在更新 'models.json'...")
    updated_model_map = {model['publicName']: model.get('id') for model in new_models_list if 'publicName' in model and 'id' in model}
    try:
        with open(models_path, 'w', encoding='utf-8') as f:
            json.dump(updated_model_map, f, indent=4, ensure_ascii=False)
        logger.info(f"'{models_path}' 已成功更新，包含 {len(updated_model_map)} 个模型。")
        # 更新后重新加载到内存中
        load_model_map()
    except IOError as e:
        logger.error(f"写入 '{models_path}' 文件时出错: {e}")
    
    logger.info("--- 检查与更新完毕 ---")


# --- 更新检查 ---
GITHUB_REPO = "Lianues/LMArenaBridge"

def download_and_extract_update(version):
    """下载并解压最新版本到临时文件夹。"""
    update_dir = "update_temp"
    if not os.path.exists(update_dir):
        os.makedirs(update_dir)

    try:
        zip_url = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"
        logger.info(f"正在从 {zip_url} 下载新版本...")
        response = requests.get(zip_url, timeout=60)
        response.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            # 解压所有文件到临时目录
            z.extractall(update_dir)
        
        logger.info(f"新版本已成功下载并解压到 '{update_dir}' 文件夹。")
        return True
    except requests.RequestException as e:
        logger.error(f"下载更新失败: {e}")
    except zipfile.BadZipFile:
        logger.error("下载的文件不是一个有效的zip压缩包。")
    except Exception as e:
        logger.error(f"解压更新时发生未知错误: {e}")
    
    return False

def check_for_updates():
    """从 GitHub 检查新版本。"""
    if not CONFIG.get("enable_auto_update", True):
        logger.info("自动更新已禁用，跳过检查。")
        return

    current_version = CONFIG.get("version", "0.0.0")
    logger.info(f"当前版本: {current_version}。正在从 GitHub 检查更新...")

    try:
        config_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/config.jsonc"
        response = requests.get(config_url, timeout=10)
        response.raise_for_status()

        # 移除注释以解析 JSON
        jsonc_content = response.text
        json_content = re.sub(r'//.*', '', jsonc_content)
        json_content = re.sub(r'/\*.*?\*/', '', json_content, flags=re.DOTALL)
        remote_config = json.loads(json_content)
        
        remote_version_str = remote_config.get("version")
        if not remote_version_str:
            logger.warning("远程配置文件中未找到版本号，跳过更新检查。")
            return

        if parse_version(remote_version_str) > parse_version(current_version):
            logger.info("="*60)
            logger.info(f"🎉 发现新版本! 🎉")
            logger.info(f"  - 当前版本: {current_version}")
            logger.info(f"  - 最新版本: {remote_version_str}")
            if download_and_extract_update(remote_version_str):
                logger.info("准备应用更新。服务器将在5秒后关闭并启动更新脚本。")
                time.sleep(5)
                # 启动更新脚本并分离
                update_script_path = os.path.join("modules", "update_script.py")
                subprocess.Popen([sys.executable, update_script_path])
                # 干净地退出当前程序
                sys.exit(0)
            else:
                logger.error(f"自动更新失败。请访问 https://github.com/{GITHUB_REPO}/releases/latest 手动下载。")
            logger.info("="*60)
        else:
            logger.info("您的程序已是最新版本。")

    except requests.RequestException as e:
        logger.error(f"检查更新失败: {e}")
    except json.JSONDecodeError:
        logger.error("解析远程配置文件失败。")
    except Exception as e:
        logger.error(f"检查更新时发生未知错误: {e}")


# --- API 端点 ---
@app.route('/update_models', methods=['POST'])
def update_models():
    html_content = request.data.decode('utf-8')
    if not html_content:
        return jsonify({"status": "error", "message": "No HTML content received."}), 400
    
    logger.info("收到来自油猴脚本的页面内容，开始检查并更新模型...")
    new_models_list = extract_models_from_html(html_content)
    
    if new_models_list:
        compare_and_update_models(new_models_list, 'models.json')
        return jsonify({"status": "success", "message": "Model comparison and update complete."})
    else:
        return jsonify({"status": "error", "message": "Could not extract model data from HTML."}), 400

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

def format_openai_finish_chunk(model: str, request_id: str, reason: str = 'stop'):
    return f"data: {json.dumps({'id': request_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}]})}\n\ndata: [DONE]\n\n"

def format_openai_non_stream_response(content: str, model: str, request_id: str, reason: str = 'stop'):
    return {'id': request_id, 'object': 'chat.completion', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': reason}], 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}

def _normalize_message_content(message: dict) -> dict:
    content = message.get("content")
    if isinstance(content, list):
        message["content"] = "\n\n".join([p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"])
    return message

def _openai_response_generator(task_id: str):
    text_pattern = re.compile(r'a0:"((?:\\.|[^"\\])*)"')
    error_pattern = re.compile(r'(\{\s*"error".*?\})', re.DOTALL)
    finish_pattern = re.compile(r'"finishReason"\s*:\s*"(stop|content-filter)"')
    buffer = ""
    RESULTS[task_id]['finish_reason'] = None

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
            
            finish_match = finish_pattern.search(raw_chunk)
            if finish_match:
                reason = finish_match.group(1)
                logger.info(f"检测到任务 {task_id[:8]} 的 LMArena 流结束信号，原因: {reason}。")
                RESULTS[task_id]['finish_reason'] = reason
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
                return

            finish_reason = RESULTS[task_id].get('finish_reason')
            if finish_reason == 'content-filter':
                yield format_openai_chunk("\n\n响应被终止，可能是上下文超限或者模型内部审查的原因", model, request_id)
            
            # 确保即使 finish_reason 为 None，也传递 'stop'
            yield format_openai_finish_chunk(model, request_id, reason=finish_reason or 'stop')
        return Response(stream_response(), mimetype='text/event-stream')
    else:
        full_response_content = "".join(list(_openai_response_generator(task_id)))
        if RESULTS[task_id].get('error'):
            return jsonify({"error": {"message": f"[LMArena 自动化工具错误]: {RESULTS[task_id]['error']}", "type": "automator_error"}}), 500
        
        finish_reason = RESULTS[task_id].get('finish_reason', 'stop')
        if finish_reason == 'content-filter':
            full_response_content += "\n\n响应被终止，可能是上下文超限或者模型内部审查的原因"
            
        return jsonify(format_openai_non_stream_response(full_response_content, model, request_id, reason=finish_reason))

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
    
    # 检查更新
    check_for_updates()

    logger.info("="*60)
    logger.info("  🚀 LMArena 自动化工具 - v12.2 (中文本地化)")
    logger.info(f"  - 监听地址: http://127.0.0.1:5102")
    
    # 使用一个字典来映射配置键和它们的中文名称
    config_keys_in_chinese = {
        "enable_auto_update": "自动更新",
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