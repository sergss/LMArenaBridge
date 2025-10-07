# id_updater.py
#
# Это обновленный одноразовый HTTP-сервер, предназначенный для получения информации о сессии
# от скрипта Tampermonkey в зависимости от выбранного пользователем режима
# (DirectChat или Battle) и обновления этой информации в файле config.jsonc.

import http.server
import socketserver
import json
import re
import threading
import os
import requests

# --- Конфигурация ---
HOST = "127.0.0.1"
PORT = 5103
CONFIG_PATH = 'config.jsonc'

def read_config():
    """Читает и парсит файл config.jsonc, удаляя комментарии для корректного разбора."""
    if not os.path.exists(CONFIG_PATH):
        print(f"❌ Ошибка: файл конфигурации '{CONFIG_PATH}' не существует.")
        return None
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Более надёжное удаление комментариев, построчная обработка для предотвращения удаления '//' в URL
        no_comments_lines = []
        in_block_comment = False
        for line in lines:
            stripped_line = line.strip()
            if in_block_comment:
                if '*/' in stripped_line:
                    in_block_comment = False
                    line = stripped_line.split('*/', 1)[1]
                else:
                    continue
            
            if '/*' in line and not in_block_comment:
                before_comment, _, after_comment = line.partition('/*')
                if '*/' in after_comment:
                    _, _, after_block = after_comment.partition('*/')
                    line = before_comment + after_block
                else:
                    line = before_comment
                    in_block_comment = True

            if line.strip().startswith('//'):
                continue
            
            no_comments_lines.append(line)

        json_content = "".join(no_comments_lines)
        return json.loads(json_content)
    except Exception as e:
        print(f"❌ Ошибка при чтении или разборе '{CONFIG_PATH}': {e}")
        return None

def save_config_value(key, value):
    """
    Безопасно обновляет одно значение в config.jsonc, сохраняя исходный формат и комментарии.
    Работает только для строковых или числовых значений.
    """
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            content = f.read()

        # Использует регулярное выражение для безопасной замены значения
        # Находит "key": "любое значение" и заменяет "любое значение"
        pattern = re.compile(rf'("{key}"\s*:\s*")[^"]*(")')
        new_content, count = pattern.subn(rf'\g<1>{value}\g<2>', content, 1)

        if count == 0:
            print(f"🤔 Предупреждение: не удалось найти ключ '{key}' в '{CONFIG_PATH}'.")
            return False

        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return True
    except Exception as e:
        print(f"❌ Ошибка при обновлении '{CONFIG_PATH}': {e}")
        return False

def save_session_ids(session_id, message_id):
    """Обновляет идентификаторы сессии в файле config.jsonc."""
    print(f"\n📝 Пытаемся записать идентификаторы в '{CONFIG_PATH}'...")
    res1 = save_config_value("session_id", session_id)
    res2 = save_config_value("message_id", message_id)
    if res1 and res2:
        print(f"✅ Идентификаторы успешно обновлены.")
        print(f"   - session_id: {session_id}")
        print(f"   - message_id: {message_id}")
    else:
        print(f"❌ Не удалось обновить идентификаторы. Проверьте сообщения об ошибках выше.")

class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(204)
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
                    print("🎉 Идентификаторы успешно получены из браузера!")
                    print(f"  - Session ID: {session_id}")
                    print(f"  - Message ID: {message_id}")
                    print("=" * 50)

                    save_session_ids(session_id, message_id)

                    self.send_response(200)
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(b'{"status": "success"}')

                    print("\nЗадача завершена, сервер автоматически закроется через 1 секунду.")
                    threading.Thread(target=self.server.shutdown).start()

                else:
                    self.send_response(400, "Bad Request")
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(b'{"error": "Missing sessionId or messageId"}')
            except Exception as e:
                self.send_response(500, "Internal Server Error")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(f'{{"error": "Internal server error: {e}"}}'.encode('utf-8'))
        else:
            self.send_response(404, "Not Found")
            self._send_cors_headers()
            self.end_headers()

    def log_message(self, format, *args):
        return

def run_server():
    with socketserver.TCPServer((HOST, PORT), RequestHandler) as httpd:
        print("\n" + "="*50)
        print("  🚀 Слушатель обновления идентификаторов сессии запущен")
        print(f"  - Адрес прослушивания: http://{HOST}:{PORT}")
        print("  - Пожалуйста, взаимодействуйте со страницей LMArena в браузере, чтобы инициировать захват идентификаторов.")
        print("  - После успешного захвата скрипт автоматически завершит работу.")
        print("="*50)
        httpd.serve_forever()

def notify_api_server():
    """Уведомляет главный API-сервер о начале процесса захвата идентификаторов."""
    api_server_url = "http://127.0.0.1:5102/internal/start_id_capture"
    try:
        response = requests.post(api_server_url, timeout=3)
        if response.status_code == 200:
            print("✅ Главный сервер успешно уведомлён о запуске режима захвата идентификаторов.")
            return True
        else:
            print(f"⚠️ Не удалось уведомить главный сервер, код состояния: {response.status_code}.")
            print(f"   - Сообщение об ошибке: {response.text}")
            return False
    except requests.ConnectionError:
        print("❌ Не удалось подключиться к главному API-серверу. Убедитесь, что api_server.py запущен.")
        return False
    except Exception as e:
        print(f"❌ Произошла неизвестная ошибка при уведомлении главного сервера: {e}")
        return False

if __name__ == "__main__":
    config = read_config()
    if not config:
        exit(1)

    # --- Получение выбора пользователя ---
    last_mode = config.get("id_updater_last_mode", "direct_chat")
    mode_map = {"a": "direct_chat", "b": "battle"}
    
    prompt = f"Выберите режим [a: DirectChat, b: Battle] (по умолчанию: {last_mode}): "
    choice = input(prompt).lower().strip()

    if not choice:
        mode = last_mode
    else:
        mode = mode_map.get(choice)
        if not mode:
            print(f"Недопустимый ввод, будет использовано значение по умолчанию: {last_mode}")
            mode = last_mode

    save_config_value("id_updater_last_mode", mode)
    print(f"Текущий режим: {mode.upper()}")
    
    if mode == 'battle':
        last_target = config.get("id_updater_battle_target", "A")
        target_prompt = f"Выберите сообщение для обновления [A (для модели search обязательно A) или B] (по умолчанию: {last_target}): "
        target_choice = input(target_prompt).upper().strip()

        if not target_choice:
            target = last_target
        elif target_choice in ["A", "B"]:
            target = target_choice
        else:
            print(f"Недопустимый ввод, будет использовано значение по умолчанию: {last_target}")
            target = last_target
        
        save_config_value("id_updater_battle_target", target)
        print(f"Цель в режиме Battle: Assistant {target}")
        print("Обратите внимание: независимо от выбора A или B, захваченные идентификаторы будут обновлены в основные session_id и message_id.")

    # Уведомляем главный сервер перед запуском слушателя
    if notify_api_server():
        run_server()
        print("Сервер завершил работу.")
    else:
        print("\nПроцесс обновления идентификаторов прерван из-за невозможности уведомить главный сервер.")