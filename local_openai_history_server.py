# local_openai_history_server.py
# v12.4 - Server-Side Port Balancing

import logging
import os
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from werkzeug.serving import run_simple
from queue import Queue, Empty
import uuid
import threading
import time
import json
import re
import random
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
PENDING_JOBS = Queue()
# { "tab_id": {"status": "idle"|"busy", "job": {}, "last_seen": timestamp, "task_id": "...", "sse_queue": Queue(), "port": 5103} }
TAB_SESSIONS = {}
SESSION_LOCK = threading.Lock()
RESULTS = {}
PORT_CONNECTIONS = {} # {5103: 2, 5104: 5}
# 防人机检测挂机池
HANGING_TAB_ID = None
NEXT_HANGING_JOB_TIME = 0

# --- 常量定义 ---
TASK_TIMEOUT_SECONDS = 300  # 任务超时时间（5分钟）

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
            
            payload_string = full_payload.split('\\n')[0]
            
            json_start_index = payload_string.find(':')
            if json_start_index == -1:
                continue
            
            json_string_with_escapes = payload_string[json_start_index + 1:]
            json_string = json_string_with_escapes.replace('\\"', '"')
            
            try:
                data = json.loads(json_string)
                
                def find_initial_state(obj):
                    if isinstance(obj, dict):
                        for key, value in obj.items():
                            if key == 'initialState' and isinstance(value, list):
                                if value and isinstance(value[0], dict) and 'publicName' in value[0]:
                                    return value
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

    logger.info("\n结论: 检测到模型变更，正在更新 'models.json'...")
    updated_model_map = {model['publicName']: model.get('id') for model in new_models_list if 'publicName' in model and 'id' in model}
    try:
        with open(models_path, 'w', encoding='utf-8') as f:
            json.dump(updated_model_map, f, indent=4, ensure_ascii=False)
        logger.info(f"'{models_path}' 已成功更新，包含 {len(updated_model_map)} 个模型。")
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
                update_script_path = os.path.join("modules", "update_script.py")
                subprocess.Popen([sys.executable, update_script_path])
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
            config_data = json.loads(json_content)
            # 从配置中移除 worker_ports，不需要发送给客户端
            config_data.pop('worker_ports', None)
            return jsonify(config_data)
    except Exception as e:
        logger.error(f"读取或解析 config.jsonc 失败: {e}")
        return jsonify({"error": "Config file issue"}), 500

@app.route('/get_worker_port', methods=['GET'])
def get_worker_port():
    """为新的标签页分配一个负载最低的 Worker 端口。"""
    with SESSION_LOCK:
        worker_ports = CONFIG.get("worker_ports", [])
        if not worker_ports:
            return jsonify({"status": "error", "message": "No worker ports configured."}), 500

        # 找到连接数最少的端口
        best_port = -1
        min_connections = float('inf')

        for port in worker_ports:
            connections = PORT_CONNECTIONS.get(port, 0)
            if connections < min_connections:
                min_connections = connections
                best_port = port
        
        # 检查选出的最佳端口是否已满（例如每个端口限制6个连接）
        if min_connections < 6:
            logger.info(f"为新标签页分配了端口 {best_port} (当前连接数: {min_connections})")
            return jsonify({"status": "success", "port": best_port})
        else:
            logger.error(f"所有 Worker 端口 {worker_ports} 的连接数都已达到或超过6个。无法分配新端口。")
            return jsonify({"status": "error", "message": "All worker ports are at maximum capacity."}), 503

@app.route('/')
def index():
    return "LMArena 自动化工具 v12.2 (中文本地化) 正在运行。"

@app.route('/log_from_client', methods=['POST'])
def log_from_client():
    log_data = request.json
    if log_data and 'message' in log_data:
        logger.info(f"[油猴脚本] {log_data.get('level', 'INFO')}: {log_data['message']}")
    return jsonify({"status": "logged"})

# --- 核心逻辑 ---
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
    tab_id = request.args.get('tab_id')
    if not tab_id:
        return jsonify({"status": "error", "message": "tab_id is required"}), 400
    
    with SESSION_LOCK:
        session = TAB_SESSIONS.get(tab_id)
        if session and session.get('status') == 'busy' and session.get('job'):
            job_data = session['job'].get('messages_job')
            if job_data:
                # Check if logging for hanging tasks is enabled
                is_hanging = session.get('job', {}).get('is_hanging_job', False)
                if not is_hanging or CONFIG.get("log_hanging_pool_activity", True):
                    logger.info(f"提供 messages_job 给标签页 {tab_id[:8]} (任务 {session['task_id'][:8]})")
                session['job']['messages_job'] = None
                return jsonify({"status": "success", "job": job_data})
            
    return jsonify({"status": "empty"})

@app.route('/events', methods=['GET'])
def events():
    tab_id = request.args.get('tab_id')
    is_hanging = request.args.get('is_hanging') == 'true'
    
    # 获取当前连接的端口。Werkzeug 会将它放入 environ。
    port_str = request.environ.get('SERVER_PORT')
    if not port_str:
        logger.error("无法确定SSE连接的服务器端口。")
        return Response("Could not determine server port", status=500)
    port = int(port_str)

    if not tab_id:
        return Response("tab_id is required", status=400)

    def stream():
        q = Queue()
        with SESSION_LOCK:
            if tab_id not in TAB_SESSIONS:
                logger.info(f"新的SSE连接在端口 {port} 上建立: {tab_id[:8]} (报告挂机状态: {is_hanging})")
                PORT_CONNECTIONS[port] = PORT_CONNECTIONS.get(port, 0) + 1
                TAB_SESSIONS[tab_id] = {
                    "status": "idle", "job": None, "task_id": None,
                    "last_seen": time.time(), "sse_queue": q,
                    "is_hanging_client": is_hanging, "port": port,
                    "refresh_requested": False
                }
            else:
                old_port = TAB_SESSIONS[tab_id].get('port')
                logger.info(f"标签页 {tab_id[:8]} 在端口 {port} 上重新建立了SSE连接。")
                if old_port and old_port != port:
                    logger.warning(f"标签页 {tab_id[:8]} 从旧端口 {old_port} 移动到了新端口 {port}。")
                    # 减少旧端口连接数，增加新端口连接数
                    PORT_CONNECTIONS[old_port] = max(0, PORT_CONNECTIONS.get(old_port, 1) - 1)
                    PORT_CONNECTIONS[port] = PORT_CONNECTIONS.get(port, 0) + 1
                
                TAB_SESSIONS[tab_id].update({
                    'sse_queue': q, 'last_seen': time.time(),
                    'is_hanging_client': is_hanging, 'port': port,
                    'refresh_requested': False
                })

            # 立即同步挂机状态，确保客户端状态与服务器一致
            is_currently_hanging = (tab_id == HANGING_TAB_ID)
            q.put(f"event: set_hanging_status\ndata: {json.dumps({'is_hanging': is_currently_hanging})}\n\n")
            logger.info(f"标签页 {tab_id[:8]} SSE连接时同步挂机状态: {is_currently_hanging}")

            if TAB_SESSIONS[tab_id]['status'] == 'idle':
                try:
                    job_package = PENDING_JOBS.get_nowait()
                    task_id = job_package['task_id']
                    TAB_SESSIONS[tab_id]['job'] = job_package
                    TAB_SESSIONS[tab_id]['status'] = 'busy'
                    TAB_SESSIONS[tab_id]['task_id'] = task_id
                    
                    prompt_job_data = job_package.get('prompt_job')
                    if prompt_job_data:
                        prompt_job_data['type'] = 'prompt'
                        logger.info(f"通过新建立的SSE连接，将待处理任务 {task_id[:8]} 推送给标签页 {tab_id[:8]}")
                        q.put(f"event: new_job\ndata: {json.dumps(prompt_job_data)}\n\n")

                except Empty:
                    pass

        try:
            while True:
                message = q.get()
                yield message
        except GeneratorExit:
            # port 变量在 stream 函数的闭包中是可用的
            logger.info(f"SSE连接已由客户端关闭: {tab_id[:8]} (端口: {port})")
            with SESSION_LOCK:
                if tab_id in TAB_SESSIONS:
                    TAB_SESSIONS[tab_id]['sse_queue'] = None
                    # 注意：在这里不减少连接计数。连接计数将在 cleanup_and_dispatch_thread 中处理，
                    # 因为那里是唯一确定性地清理僵尸会话的地方。

    return Response(stream(), mimetype='text/event-stream')

@app.route('/stream_chunk', methods=['POST'])
def stream_chunk():
    data = request.json
    task_id = data.get('task_id')
    tab_id = data.get('tab_id')
    if task_id in RESULTS:
        RESULTS[task_id]['stream_queue'].put(data.get('chunk'))
        return jsonify({"status": "success"})
    logger.warning(f"从标签页 {tab_id[:8] if tab_id else 'N/A'} 收到了未知任务 {task_id[:8] if task_id else 'N/A'} 的数据块。")
    return jsonify({"status": "error", "message": "Task ID not found"}), 404

@app.route('/report_result', methods=['POST'])
def report_result():
    data = request.json
    task_id = data.get('task_id')
    tab_id = data.get('tab_id')
    
    if not tab_id:
        return jsonify({"status": "error", "message": "tab_id is required"}), 400

    if task_id in RESULTS:
        RESULTS[task_id]['status'] = data.get('status', 'completed')
        
        is_hanging = task_id.startswith("hanging-")
        log_activity = CONFIG.get("log_hanging_pool_activity", True)

        if not is_hanging or log_activity:
            logger.info(f"任务 {task_id[:8]} (来自标签页 {tab_id[:8]}) 已被客户端报告为完成。")
        
        with SESSION_LOCK:
            session = TAB_SESSIONS.get(tab_id)
            if session and session.get('task_id') == task_id:
                if not is_hanging or log_activity:
                    logger.info(f"标签页 {tab_id[:8]} 已完成任务，状态重置为空闲。")
                session['status'] = 'idle'
                session['job'] = None
                session['task_id'] = None
            else:
                logger.warning(f"报告完成时，标签页 {tab_id[:8]} 的会话状态异常或任务ID不匹配。")

        return jsonify({"status": "success"})
        
    logger.warning(f"从标签页 {tab_id[:8]} 收到了未知任务 {task_id[:8] if task_id else 'N/A'} 的完成报告。")
    return jsonify({"status": "error", "message": "Task ID not found"}), 404

def format_openai_chunk(content: str, model: str, request_id: str):
    return f"data: {json.dumps({'id': request_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': None}]})}\n\n"

def format_openai_finish_chunk(model: str, request_id: str, reason: str = 'stop'):
    return f"data: {json.dumps({'id': request_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}]})}\n\ndata: [DONE]\n\n"

def format_openai_non_stream_response(content: str, model: str, request_id: str, reason: str = 'stop'):
    return {'id': request_id, 'object': 'chat.completion', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': reason}], 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}

def _normalize_message_content(message: dict) -> dict:
    content = message.get("content")
    
    # 1. 处理列表形式的内容 (例如多模态输入)
    if isinstance(content, list):
        # 提取文本部分并连接
        text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        message["content"] = "\n\n".join(text_parts)
        content = message["content"] # 规范化后更新 content 变量

    # 2. 检查 user 角色的空内容并替换为空格
    if message.get("role") == "user" and content == "":
        message["content"] = " "
        
    return message

def _openai_response_generator(task_id: str):
    text_pattern = re.compile(r'a0:"((?:\\.|[^"\\])*)"')
    error_pattern = re.compile(r'(\{\s*"error".*?\})', re.DOTALL)
    finish_pattern = re.compile(r'"finishReason"\s*:\s*"(stop|content-filter)"')
    # Cloudflare 检测特征
    cloudflare_patterns = [
        r'<title>Just a moment...</title>',
        r'Enable JavaScript and cookies to continue'
    ]
    
    buffer = ""
    RESULTS[task_id]['finish_reason'] = None
    timeout = CONFIG.get("stream_response_timeout_seconds", 120)

    while True:
        try:
            raw_chunk = RESULTS[task_id]['stream_queue'].get(timeout=timeout)
            buffer += raw_chunk

            # 1. 检测 Cloudflare 人机验证
            for pattern in cloudflare_patterns:
                if re.search(pattern, buffer, re.IGNORECASE):
                    error_message = "检测到 Cloudflare 人机验证页面。请在浏览器中刷新 LMArena 页面并手动完成验证，然后重试请求。"
                    logger.error(f"任务 {task_id[:8]} 检测到 Cloudflare 验证: {error_message}")
                    RESULTS[task_id]['error'] = error_message
                    return

            # 2. 检测 LMArena 返回的错误
            error_match = error_pattern.search(buffer)
            if error_match:
                try:
                    error_json = json.loads(error_match.group(1))
                    error_message = error_json.get("error", "来自 LMArena 的未知错误")
                    logger.error(f"任务 {task_id[:8]} 的流式响应中检测到错误: {error_message}")
                    RESULTS[task_id]['error'] = str(error_message)
                    return
                except json.JSONDecodeError: pass

            # 3. 提取文本内容
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
            RESULTS[task_id]['error'] = f'流式响应在{timeout}秒后超时。'
            return

def _load_config():
    global CONFIG
    try:
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            CONFIG = json.loads(re.sub(r'/\*.*?\*/', '', re.sub(r'//.*', '', f.read()), flags=re.DOTALL))
        logger.info("成功从 'config.jsonc' 加载配置。")
        timeout_val = CONFIG.get("stream_response_timeout_seconds")
        if timeout_val:
            logger.info(f"配置的响应超时时间: {timeout_val} 秒。")
        else:
            logger.warning("'stream_response_timeout_seconds' 未在配置中找到，将使用代码中的默认值。")
    except Exception as e:
        logging.error(f"无法加载或解析 'config.jsonc': {e}。将使用默认设置。")
        CONFIG = {}

@app.route('/v1/models', methods=['GET'])
def list_models():
    return jsonify({"object": "list", "data": [{"id": name, "object": "model", "owned_by": "local-server"} for name in MODEL_NAME_TO_ID_MAP.keys()]})

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    # API Key 验证
    api_key = CONFIG.get("api_key")
    if api_key:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            logger.warning("请求缺少有效的 Authorization Bearer 头部")
            return jsonify({"error": {"message": "未提供 API Key。请在 Authorization 头部中以 'Bearer YOUR_KEY' 格式提供。", "type": "invalid_request_error", "code": "invalid_api_key"}}), 401
        
        provided_key = auth_header.split(' ')[1]
        if provided_key != api_key:
            logger.warning("提供的 API Key 不正确")
            return jsonify({"error": {"message": "提供的 API Key 不正确。", "type": "invalid_request_error", "code": "invalid_api_key"}}), 401

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
    task_id = str(uuid.uuid4())
    
    messages_job['task_id'] = task_id
    
    prompt_job = {"task_id": task_id, "prompt": f"[这条消息仅起占位，请以外部应用中显示的内容为准：/{task_id}]"}

    job_package = {
        "task_id": task_id,
        "messages_job": messages_job,
        "prompt_job": prompt_job
    }

    RESULTS[task_id] = {"status": "pending", "stream_queue": Queue(), "error": None}

    with SESSION_LOCK:
        # The background dispatcher now handles all logic, so we just queue the job.
        PENDING_JOBS.put(job_package)
        logger.info(f"新任务 {task_id[:8]} 已收到并放入待处理队列。调度器将在后台处理。")
    model = request_data.get("model", "default")
    use_stream = request_data.get("stream", False)
    request_id = f"chatcmpl-{uuid.uuid4()}"
    if use_stream:
        def stream_response():
            for chunk in _openai_response_generator(task_id):
                yield format_openai_chunk(chunk, model, request_id)

            if RESULTS[task_id].get('error'):
                error_info = {
                    "error": {
                        "message": f"[LMArena 自动化工具错误]: {RESULTS[task_id]['error']}",
                        "type": "automator_error"
                    }
                }
                yield f"data: {json.dumps(error_info)}\n\n"
                yield "data: [DONE]\n\n"
                return

            finish_reason = RESULTS[task_id].get('finish_reason')
            if finish_reason == 'content-filter':
                yield format_openai_chunk("\n\n响应被终止，可能是上下文超限或者模型内部审查的原因", model, request_id)
            
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

def create_hanging_job_package():
    """
    创建一个完全模拟 OpenAI 请求的挂机任务包。
    """
    # 模拟一个外部应用发来的请求体
    request_data = {
        "model": "claude-3-5-sonnet-20241022", # 或者任何一个有效的默认模型
        "messages": [{"role": "user", "content": "你好"}]
    }

    # 使用与 /v1/chat/completions 端点完全相同的逻辑来创建任务
    messages_job = convert_openai_to_lmarena_templates(request_data)
    task_id = f"hanging-{uuid.uuid4()}"
    messages_job['task_id'] = task_id
    
    prompt_job = {
        "task_id": task_id,
        "prompt": f"[防人机检测挂机任务]"}

    job_package = {
        "task_id": task_id,
        "messages_job": messages_job,
        "prompt_job": prompt_job,
        "is_hanging_job": True  # 标记为挂机任务
    }

    # 注册任务以跟踪其结果
    RESULTS[task_id] = {"status": "pending", "stream_queue": Queue(), "error": None}
    
    return job_package

def cleanup_and_dispatch_thread():
    """
    一个后台线程，负责清理僵尸连接、调度待处理任务以及管理防人机检测挂机池。
    """
    global HANGING_TAB_ID, NEXT_HANGING_JOB_TIME

    while True:
        try:
            # Increased responsiveness for higher concurrency
            # Reduced from 2s to 0.5s to allow faster dispatching when multiple workers are available
            time.sleep(0.5)

            # 读取配置，确保是最新的
            enable_hanging = CONFIG.get("enable_anti_bot_hanging", False)
            hanging_interval = CONFIG.get("hanging_interval_seconds", 120)

            with SESSION_LOCK:
                # --- 0. 状态诊断 ---
                num_pending = PENDING_JOBS.qsize()
                num_sessions = len(TAB_SESSIONS)
                idle_sessions = [sid[:8] for sid, s in TAB_SESSIONS.items() if s.get('status') == 'idle']
                # 仅在有任务或有挂机活动时记录心跳，以减少噪音
                if num_pending > 0 or (enable_hanging and num_sessions > 0):
                    logger.info(f"调度器心跳: {num_pending}个待处理, {num_sessions}个会话 (空闲: {idle_sessions if idle_sessions else '无'}), 挂机池: {HANGING_TAB_ID[:8] if HANGING_TAB_ID else '无'}")

                # --- 1. Active Ping & Cleanup Phase ---
                zombie_tabs = []
                active_sessions = list(TAB_SESSIONS.items())

                current_time_for_timeout = time.time()

                for tab_id, session in active_sessions:
                    # 标记僵尸会话的条件:
                    # 1. SSE队列不存在 (客户端已正常断开)
                    # 2. Ping失败 (客户端异常断开)
                    # 3. 任务超时 (客户端可能无响应)
                    is_zombie = False

                    # 条件 1 & 2: 连接检查
                    if not session.get('sse_queue'):
                        is_zombie = True
                    else:
                        try:
                            session['sse_queue'].put_nowait(": ping\n\n")
                        except Exception:
                            is_zombie = True
                    
                    # 条件 3: 任务超时检查
                    if not is_zombie and session.get('status') == 'busy' and 'task_start_time' in session:
                        if current_time_for_timeout - session['task_start_time'] > TASK_TIMEOUT_SECONDS:
                            if not session.get('refresh_requested', False):
                                # 首次超时，尝试发送刷新请求
                                logger.warning(f"调度器：任务超时但连接活跃。向标签页 {tab_id[:8]} (任务 {session['task_id'][:8]}) 发送刷新请求。")
                                try:
                                    session['sse_queue'].put_nowait(f"event: refresh\ndata: {{}}\n\n")
                                    session['refresh_requested'] = True
                                    # 重置开始时间，给予刷新后重新处理的时间
                                    session['task_start_time'] = current_time_for_timeout
                                except Exception:
                                    # 如果发送失败，则立即标记为僵尸
                                    logger.error(f"调度器：向超时标签页 {tab_id[:8]} 发送刷新请求失败。标记为僵尸。")
                                    is_zombie = True
                            else:
                                # 已经请求过刷新但仍然超时，标记为僵尸
                                logger.warning(f"调度器：标签页 {tab_id[:8]} 在请求刷新后仍然超时 (任务 {session['task_id'][:8]})。标记为僵尸会话。")
                                is_zombie = True

                    if is_zombie:
                        zombie_tabs.append(tab_id)

                if zombie_tabs:
                    logger.warning(f"调度器：检测到 {len(zombie_tabs)} 个僵尸会话: {[tid[:8] for tid in zombie_tabs]}，正在清理。")
                    for tab_id in zombie_tabs:
                        session = TAB_SESSIONS.pop(tab_id, None)
                        if session:
                            port = session.get('port')
                            if port:
                                PORT_CONNECTIONS[port] = max(0, PORT_CONNECTIONS.get(port, 1) - 1)
                                logger.info(f"清理僵尸会话 {tab_id[:8]}，端口 {port} 连接数减至 {PORT_CONNECTIONS[port]}")
                            
                            if tab_id == HANGING_TAB_ID:
                                logger.info(f"调度器：挂机标签页 {tab_id[:8]} 是僵尸，正在重置。")
                                HANGING_TAB_ID = None
                            
                            if session.get('status') == 'busy' and session.get('job'):
                                if not session['job'].get("is_hanging_job"):
                                    requeued_job = session['job']
                                    PENDING_JOBS.put(requeued_job)
                                    logger.warning(f"调度器：来自僵尸会话 {tab_id[:8]} 的任务 {requeued_job['task_id'][:8]} 已被重新排队。")
                                else:
                                    logger.info(f"调度器：丢弃来自僵尸会话 {tab_id[:8]} 的挂机任务 {session['task_id'][:8]}。")

                # --- 2. Anti-Bot Hanging Management Phase ---
                previous_hanging_id = HANGING_TAB_ID

                if enable_hanging and len(TAB_SESSIONS) >= 2:
                    if HANGING_TAB_ID is None or HANGING_TAB_ID not in TAB_SESSIONS:
                        # 优先选择那些报告自己是挂机状态的标签页
                        preferred_tabs = [tid for tid, s in TAB_SESSIONS.items() if s.get('is_hanging_client')]

                        if preferred_tabs:
                            HANGING_TAB_ID = random.choice(preferred_tabs)
                            logger.info(f"调度器：已从 {len(preferred_tabs)} 个前挂机标签页中，重新选择 {HANGING_TAB_ID[:8]} 作为挂机池。")
                        else:
                            # 如果没有，则从所有可用标签页中随机选择
                            available_tabs = list(TAB_SESSIONS.keys())
                            if available_tabs:
                                HANGING_TAB_ID = random.choice(available_tabs)
                                logger.info(f"调度器：没有找到前挂机标签页，已随机选择新标签页 {HANGING_TAB_ID[:8]} 作为挂机池。")

                        if HANGING_TAB_ID:
                            NEXT_HANGING_JOB_TIME = time.time()
                else:
                    if HANGING_TAB_ID is not None:
                         logger.info(f"调度器：因条件不满足（启用: {enable_hanging}, 标签页数: {len(TAB_SESSIONS)}），取消挂机模式。")
                    HANGING_TAB_ID = None

                # --- 状态变更通知 ---
                if previous_hanging_id != HANGING_TAB_ID:
                    # 通知旧的挂机标签页取消状态
                    if previous_hanging_id and previous_hanging_id in TAB_SESSIONS:
                        try:
                            TAB_SESSIONS[previous_hanging_id]['sse_queue'].put(f"event: set_hanging_status\ndata: {json.dumps({'is_hanging': False})}\n\n")
                            logger.info(f"通知标签页 {previous_hanging_id[:8]} 已取消挂机状态。")
                        except Exception: pass
                    # 通知新的挂机标签页设置状态
                    if HANGING_TAB_ID and HANGING_TAB_ID in TAB_SESSIONS:
                        try:
                            TAB_SESSIONS[HANGING_TAB_ID]['sse_queue'].put(f"event: set_hanging_status\ndata: {json.dumps({'is_hanging': True})}\n\n")
                            logger.info(f"通知标签页 {HANGING_TAB_ID[:8]} 已设为挂机状态。")
                        except Exception: pass


                # --- 3. Hanging Job Creation Phase ---
                if enable_hanging and HANGING_TAB_ID:
                    current_time = time.time()
                    has_pending_hanging_job = any(job.get('is_hanging_job') for job in list(PENDING_JOBS.queue))

                    if current_time >= NEXT_HANGING_JOB_TIME and not has_pending_hanging_job:
                        if CONFIG.get("log_hanging_pool_activity", True):
                            logger.info(f"调度器：创建新的挂机任务并放入队列。")
                        hanging_job_package = create_hanging_job_package()
                        PENDING_JOBS.put(hanging_job_package)
                        NEXT_HANGING_JOB_TIME = current_time + hanging_interval

                # --- 4. Dispatch Phase (Optimized for High Concurrency) ---
                # 持续调度，直到队列为空或没有可用的 Worker (满足 FIFO 原则)
                while not PENDING_JOBS.empty():
                    # 1. 识别所有空闲 Worker
                    idle_sessions = {tid: s for tid, s in TAB_SESSIONS.items() if s.get('status') == 'idle'}
                    
                    if not idle_sessions:
                        # 没有空闲 Worker，停止本次调度循环
                        break

                    # 2. 查看队列中的下一个任务 (Peek)
                    try:
                        job_package = PENDING_JOBS.queue[0]
                    except IndexError:
                        break # 队列变空

                    is_hanging_job = job_package.get("is_hanging_job", False)
                    target_session_id = None

                    # 3. 寻找合适的 Worker
                    if is_hanging_job:
                        # 挂机任务必须分配给挂机池标签页
                        if HANGING_TAB_ID and HANGING_TAB_ID in idle_sessions:
                            target_session_id = HANGING_TAB_ID
                        # 如果挂机池不可用，挂机任务（作为队首）将阻塞队列，等待下一次循环
                        
                    else:
                        # 普通任务
                        # 优先选择非挂机池的空闲 Worker
                        idle_non_hanging = [tid for tid in idle_sessions.keys() if tid != HANGING_TAB_ID]
                        
                        if idle_non_hanging:
                            # 选择第一个可用的非挂机 Worker
                            target_session_id = idle_non_hanging[0]
                        elif HANGING_TAB_ID and HANGING_TAB_ID in idle_sessions:
                            # 如果没有普通 Worker，则使用挂机池 Worker
                            target_session_id = HANGING_TAB_ID

                    # 4. 分配任务
                    if target_session_id:
                        try:
                            session = TAB_SESSIONS.get(target_session_id)
                            # 再次确认 Worker 状态（虽然在锁内，但作为防御性编程）
                            if session and session['status'] == 'idle':
                                # 正式从队列中取出任务
                                job_to_dispatch = PENDING_JOBS.get()
                                
                                # 验证 (可选，但在并发环境中很重要)
                                if job_to_dispatch['task_id'] != job_package['task_id']:
                                     logger.error("严重错误：调度器取出的任务与预期的不一致！")
                                     PENDING_JOBS.put(job_to_dispatch) # 放回去
                                     break

                                dispatch_job(target_session_id, session, job_to_dispatch)
                                
                                # 如果普通任务使用了挂机池，推迟下一次挂机任务
                                if not is_hanging_job and target_session_id == HANGING_TAB_ID:
                                    # 确保 hanging_interval 已定义
                                    hanging_interval = CONFIG.get("hanging_interval_seconds", 120)
                                    NEXT_HANGING_JOB_TIME = time.time() + hanging_interval
                                    logger.info(f"挂机标签页被用于执行普通任务，下一次挂机任务推迟。")
                            else:
                                # Worker 状态意外改变
                                break
                        except Empty:
                            break # 队列突然空了
                    else:
                        # 队首任务无法调度（例如挂机任务但挂机池忙碌），停止本次调度循环以保持 FIFO
                        break

        except Exception:
            logger.error("调度器后台线程发生致命错误！将会在10秒后重试。", exc_info=True)
            # To prevent a fast spinning loop of death if the error is persistent
            time.sleep(10)

def dispatch_job(tab_id, session, job_package):
    """辅助函数，用于将任务发送到指定的标签页会话。"""
    global HANGING_TAB_ID
    session['status'] = 'busy'
    session['job'] = job_package
    session['task_id'] = job_package['task_id']
    session['last_seen'] = time.time()
    session['task_start_time'] = time.time() # 记录任务开始时间用于超时检测

    prompt_job_data = job_package.get('prompt_job')
    if prompt_job_data:
        prompt_job_data['type'] = 'prompt'
        try:
            if session['sse_queue']:
                # 确保在分配任务时，挂机状态（和标题）是最新的
                is_currently_hanging = (tab_id == HANGING_TAB_ID)
                session['sse_queue'].put(f"event: set_hanging_status\ndata: {json.dumps({'is_hanging': is_currently_hanging})}\n\n")

                session['sse_queue'].put(f"event: new_job\ndata: {json.dumps(prompt_job_data)}\n\n")
                
                # Check if logging for hanging tasks is enabled
                is_hanging = job_package.get("is_hanging_job", False)
                if not is_hanging or CONFIG.get("log_hanging_pool_activity", True):
                    logger.info(f"调度器：将任务 {job_package['task_id'][:8]} 分配给了标签页 {tab_id[:8]}")
            else:
                raise Exception("SSE Queue is None")
        except Exception as e:
            logger.error(f"调度器：在分配任务给 {tab_id[:8]} 时连接失效: {e}")
            # 如果是普通任务，重新排队
            if not job_package.get("is_hanging_job"):
                PENDING_JOBS.put(job_package)
            TAB_SESSIONS.pop(tab_id, None)
            if tab_id == HANGING_TAB_ID:
                HANGING_TAB_ID = None

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
    
    check_for_updates()

    # 启动后台调度线程
    dispatcher_thread = threading.Thread(target=cleanup_and_dispatch_thread, daemon=True)
    dispatcher_thread.start()
    logger.info("后台任务调度器已启动。")

    logger.info("="*60)
    logger.info("  🚀 LMArena 自动化工具 - v12.3 (多端口并发)")
    # logger.info(f"  - 监听地址: http://127.0.0.1:5102")
    
    config_keys_in_chinese = {
        "enable_auto_update": "自动更新",
        "bypass_enabled": "Bypass 模式",
        "tavern_mode_enabled": "酒馆模式",
        "log_server_requests": "服务器请求日志",
        "log_tampermonkey_debug": "油猴脚本调试日志",
        "enable_comprehensive_logging": "聚合日志",
        "enable_anti_bot_hanging": "防人机检测挂机",
        "log_hanging_pool_activity": "挂机池活动日志",
        "api_key": "API Key 保护"
    }
    
    logger.info("\n  当前配置:")
    for key, name in config_keys_in_chinese.items():
        status = '✅ 已启用' if CONFIG.get(key) else '❌ 已禁用'
        logger.info(f"  - {name}: {status}")
        
    logger.info("\n  请在浏览器中打开一个 LMArena 的 Direct Chat 页面以激活油猴脚本。")
    logger.info("="*60)

    # --- 多端口启动逻辑 (v12.4) ---
    api_port = CONFIG.get("api_port", 5102)
    worker_ports = CONFIG.get("worker_ports", [])
    
    # 初始化所有 worker 端口的连接计数
    for p in worker_ports:
        PORT_CONNECTIONS[p] = 0

    all_ports = sorted(list(set([api_port] + worker_ports)))
    
    logger.info(f"🌐 准备在以下 {len(all_ports)} 个端口上启动服务器: {all_ports}")
    logger.info(f"  - API 入口端口: {api_port}")
    logger.info(f"  - 浏览器 Worker 端口: {worker_ports}")

    threads = []
    host = '0.0.0.0'

    for port in all_ports:
        try:
            port_num = int(port)
            # Werkzeug 的 run_simple 在一个线程中运行 Flask 应用。
            # 我们为每个端口创建一个独立的线程来运行一个服务器实例。
            # 所有线程共享同一个 Flask app 对象和全局变量，实现了状态共享。
            t = threading.Thread(target=run_simple, args=(host, port_num, app), kwargs={'use_reloader': False, 'use_debugger': False, 'threaded': True})
            t.daemon = True
            threads.append(t)
            t.start()
            logger.info(f"  ✅ 服务器已在 http://{host}:{port_num} 启动")
        except Exception as e:
            logger.error(f"  ❌ 无法在端口 {port} 启动服务器: {e}")

    if not threads:
        logger.error("未能启动任何服务器实例。程序将退出。")
        sys.exit(1)

    # 主线程等待所有服务器线程 (虽然它们是守护线程，但这样可以保持主程序运行)
    try:
        while True:
            # 保持主线程活跃，以便接收 Ctrl+C
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭服务器...")
        # 注意：由于 Werkzeug 服务器运行在守护线程中，当主线程退出时它们会自动停止。