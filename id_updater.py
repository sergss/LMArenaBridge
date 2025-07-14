# id_updater.py
#
# 这是一个极简的、一次性的HTTP服务器，用于接收来自油猴脚本的会话信息，
# 并将其更新到 config.jsonc 文件中。

import http.server
import socketserver
import json
import re
import threading

# --- 配置 ---
HOST = "127.0.0.1"
PORT = 5103  # 使用一个专用的、不同于主API服务器的端口
CONFIG_PATH = 'config.jsonc'

def save_config(session_id, message_id):
    """将新的ID更新到 config.jsonc 文件，尽可能保留注释和格式。"""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            content = f.read()

        # 使用正则表达式安全地替换值
        def replacer(key, value, text):
            pattern = re.compile(rf'("{key}"\s*:\s*")[^"]*(")')
            return pattern.sub(rf'\g<1>{value}\g<2>', text)

        content = replacer("session_id", session_id, content)
        content = replacer("message_id", message_id, content)

        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"✅ 成功将新ID写入 '{CONFIG_PATH}'。")
        return True
    except Exception as e:
        print(f"❌ 写入 '{CONFIG_PATH}' 时发生错误: {e}")
        return False

class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def _send_cors_headers(self):
        """发送 CORS 头部，允许所有来源的请求。"""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        """处理 CORS 预检请求。"""
        self.send_response(204)  # 204 No Content
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        if self.path == '/update':
            try:
                content_length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(content_length)
                data = json.loads(post_data)

                session_id = data.get('sessionId')
                message_id = data.get('messageId')

                if session_id and message_id:
                    print("\n" + "=" * 50)
                    print("🎉 成功从浏览器捕获到ID！")
                    print(f"  - Session ID: {session_id}")
                    print(f"  - Message ID: {message_id}")
                    print("=" * 50)

                    save_config(session_id, message_id)

                    self.send_response(200)
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(b'{"status": "success"}')

                    # 成功后关闭服务器
                    print("\n任务完成，服务器将在1秒后自动关闭。")
                    threading.Thread(target=self.server.shutdown).start()

                else:
                    self.send_response(400)
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(b'{"error": "Missing sessionId or messageId"}')
            except Exception as e:
                self.send_response(500)
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(f'{{"error": "Internal server error: {e}"}}'.encode('utf-8'))
        else:
            self.send_response(404)
            self._send_cors_headers()
            self.end_headers()

    # 禁用日志，保持控制台清洁
    def log_message(self, format, *args):
        return

def run_server():
    with socketserver.TCPServer((HOST, PORT), RequestHandler) as httpd:
        print("="*50)
        print("  🚀 会话ID更新监听器已启动")
        print(f"  - 监听地址: http://{HOST}:{PORT}")
        print("  - 请在浏览器中操作LMArena页面以触发ID捕获。")
        print("  - 捕获成功后，此脚本将自动关闭。")
        print("="*50)
        httpd.serve_forever()

if __name__ == "__main__":
    run_server()
    print("服务器已关闭。")