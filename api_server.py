# api_server.py
# Новое поколение серверной части LMArena Bridge

import asyncio
import json
import logging
import os
import sys
import subprocess
import time
import uuid
import re
import threading
import random
import mimetypes
from datetime import datetime
from contextlib import asynccontextmanager

import uvicorn
import requests
from packaging.version import parse as parse_version
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, Response

# --- Импорт внутренних модулей ---
from modules.file_uploader import upload_to_file_bed

# --- Базовая конфигурация ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Глобальные состояния и конфигурация ---
CONFIG = {}  # Хранит конфигурацию, загруженную из config.jsonc
# browser_ws хранит WebSocket-соединение с единственным скриптом Tampermonkey.
# Примечание: данная архитектура предполагает работу только с одной вкладкой браузера.
# Для поддержки нескольких параллельных вкладок необходимо расширить до словаря для управления множеством соединений.
browser_ws: WebSocket | None = None
# response_channels хранит очередь ответов для каждого API-запроса.
# Ключ — request_id, значение — asyncio.Queue.
response_channels: dict[str, asyncio.Queue] = {}
last_activity_time = None  # Время последней активности
idle_monitor_thread = None  # Поток мониторинга простоя
main_event_loop = None  # Главный цикл событий
# Новое: отслеживание обновления из-за проверки на человекоподобность
IS_REFRESHING_FOR_VERIFICATION = False

# --- Сопоставление моделей ---
# MODEL_NAME_TO_ID_MAP теперь хранит более сложные объекты: { "model_name": {"id": "...", "type": "..."} }
MODEL_NAME_TO_ID_MAP = {}
MODEL_ENDPOINT_MAP = {}  # Новое: хранит сопоставление моделей с идентификаторами сессии/сообщения
DEFAULT_MODEL_ID = None  # Идентификатор модели по умолчанию: None

def load_model_endpoint_map():
    """Загружает сопоставление моделей с конечными точками из model_endpoint_map.json."""
    global MODEL_ENDPOINT_MAP
    try:
        with open('model_endpoint_map.json', 'r', encoding='utf-8') as f:
            content = f.read()
            # Разрешает пустой файл
            if not content.strip():
                MODEL_ENDPOINT_MAP = {}
            else:
                MODEL_ENDPOINT_MAP = json.loads(content)
        logger.info(f"Успешно загружено {len(MODEL_ENDPOINT_MAP)} сопоставлений моделей из 'model_endpoint_map.json'.")
    except FileNotFoundError:
        logger.warning("Файл 'model_endpoint_map.json' не найден. Используется пустое сопоставление.")
        MODEL_ENDPOINT_MAP = {}
    except json.JSONDecodeError as e:
        logger.error(f"Не удалось загрузить или разобрать 'model_endpoint_map.json': {e}. Используется пустое сопоставление.")
        MODEL_ENDPOINT_MAP = {}

def _parse_jsonc(jsonc_string: str) -> dict:
    """
    Надёжно парсит строку JSONC, удаляя комментарии.
    """
    lines = jsonc_string.splitlines()
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

    return json.loads("\n".join(no_comments_lines))

def load_config():
    """Загружает конфигурацию из config.jsonc, обрабатывая комментарии JSONC."""
    global CONFIG
    try:
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            content = f.read()
        CONFIG = _parse_jsonc(content)
        logger.info("Конфигурация успешно загружена из 'config.jsonc'.")
        # Вывод состояния ключевых настроек
        logger.info(f"  - Режим Таверны (Tavern Mode): {'✅ Включён' if CONFIG.get('tavern_mode_enabled') else '❌ Отключён'}")
        logger.info(f"  - Режим обхода (Bypass Mode): {'✅ Включён' if CONFIG.get('bypass_enabled') else '❌ Отключён'}")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Не удалось загрузить или разобрать 'config.jsonc': {e}. Используется конфигурация по умолчанию.")
        CONFIG = {}

def load_model_map():
    """Загружает сопоставление моделей из models.json, поддерживая формат 'id:type'."""
    global MODEL_NAME_TO_ID_MAP
    try:
        with open('models.json', 'r', encoding='utf-8') as f:
            raw_map = json.load(f)
            
        processed_map = {}
        for name, value in raw_map.items():
            if isinstance(value, str) and ':' in value:
                parts = value.split(':', 1)
                model_id = parts[0] if parts[0].lower() != 'null' else None
                model_type = parts[1]
                processed_map[name] = {"id": model_id, "type": model_type}
            else:
                # Обработка по умолчанию или старого формата
                processed_map[name] = {"id": value, "type": "text"}

        MODEL_NAME_TO_ID_MAP = processed_map
        logger.info(f"Успешно загружено и разобрано {len(MODEL_NAME_TO_ID_MAP)} моделей из 'models.json'.")

    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Не удалось загрузить 'models.json': {e}. Используется пустой список моделей.")
        MODEL_NAME_TO_ID_MAP = {}

# --- Обработка объявлений ---
def check_and_display_announcement():
    """Проверяет и отображает одноразовое объявление."""
    announcement_file = "announcement-lmarena.json"
    if os.path.exists(announcement_file):
        try:
            logger.info("="*60)
            logger.info("📢 Обнаружено объявление об обновлении, содержимое:")
            with open(announcement_file, 'r', encoding='utf-8') as f:
                announcement = json.load(f)
                title = announcement.get("title", "Объявление")
                content = announcement.get("content", [])
                
                logger.info(f"   --- {title} ---")
                for line in content:
                    logger.info(f"   {line}")
                logger.info("="*60)

        except json.JSONDecodeError:
            logger.error(f"Не удалось разобрать файл объявления '{announcement_file}'. Возможно, файл содержит невалидный JSON.")
        except Exception as e:
            logger.error(f"Ошибка при чтении файла объявления: {e}")
        finally:
            try:
                os.remove(announcement_file)
                logger.info(f"Файл объявления '{announcement_file}' удалён.")
            except OSError as e:
                logger.error(f"Не удалось удалить файл объявления '{announcement_file}': {e}")

# --- Проверка обновлений ---
GITHUB_REPO = "Lianues/LMArenaBridge"

def download_and_extract_update(version):
    """Скачивает и распаковывает новую версию во временную папку."""
    update_dir = "update_temp"
    if not os.path.exists(update_dir):
        os.makedirs(update_dir)

    try:
        zip_url = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"
        logger.info(f"Скачивание новой версии с {zip_url}...")
        response = requests.get(zip_url, timeout=60)
        response.raise_for_status()

        # Требуется импорт zipfile и io
        import zipfile
        import io
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            z.extractall(update_dir)
        
        logger.info(f"Новая версия успешно скачана и распакована в папку '{update_dir}'.")
        return True
    except requests.RequestException as e:
        logger.error(f"Не удалось скачать обновление: {e}")
    except zipfile.BadZipFile:
        logger.error("Скачанный файл не является валидным архивом ZIP.")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при распаковке обновления: {e}")
    
    return False

def check_for_updates():
    """Проверяет наличие новой версии на GitHub."""
    if not CONFIG.get("enable_auto_update", True):
        logger.info("Автоматическое обновление отключено, проверка пропущена.")
        return

    current_version = CONFIG.get("version", "0.0.0")
    logger.info(f"Текущая версия: {current_version}. Проверка обновлений на GitHub...")

    try:
        config_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/config.jsonc"
        response = requests.get(config_url, timeout=10)
        response.raise_for_status()

        jsonc_content = response.text
        remote_config = _parse_jsonc(jsonc_content)
        
        remote_version_str = remote_config.get("version")
        if not remote_version_str:
            logger.warning("В удалённой конфигурации не найдён номер версии, проверка обновления пропущена.")
            return

        if parse_version(remote_version_str) > parse_version(current_version):
            logger.info("="*60)
            logger.info(f"🎉 Обнаружена новая версия! 🎉")
            logger.info(f"  - Текущая версия: {current_version}")
            logger.info(f"  - Последняя версия: {remote_version_str}")
            if download_and_extract_update(remote_version_str):
                logger.info("Подготовка к применению обновления. Сервер будет закрыт через 5 секунд и запущен скрипт обновления.")
                time.sleep(5)
                update_script_path = os.path.join("modules", "update_script.py")
                # Запуск независимого процесса с помощью Popen
                subprocess.Popen([sys.executable, update_script_path])
                # Грациозный выход из текущего процесса сервера
                os._exit(0)
            else:
                logger.error(f"Не удалось выполнить автоматическое обновление. Пожалуйста, скачайте вручную с https://github.com/{GITHUB_REPO}/releases/latest.")
            logger.info("="*60)
        else:
            logger.info("Ваша программа уже обновлена до последней версии.")

    except requests.RequestException as e:
        logger.error(f"Не удалось проверить обновления: {e}")
    except json.JSONDecodeError:
        logger.error("Не удалось разобрать удалённую конфигурацию.")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при проверке обновлений: {e}")

# --- Обновление моделей ---
def extract_models_from_html(html_content):
    """
    Извлекает полный JSON-объект моделей из HTML-контента, используя сопоставление скобок для обеспечения целостности.
    """
    models = []
    model_names = set()
    
    # Поиск всех возможных начальных позиций JSON-объектов моделей
    for start_match in re.finditer(r'\{\\"id\\":\\"[a-f0-9-]+\\"', html_content):
        start_index = start_match.start()
        
        # Начиная с начальной позиции, выполняем сопоставление фигурных скобок
        open_braces = 0
        end_index = -1
        
        # Оптимизация: устанавливаем разумный предел поиска, чтобы избежать бесконечного цикла
        search_limit = start_index + 10000  # Предполагаем, что определение модели не превышает 10000 символов
        
        for i in range(start_index, min(len(html_content), search_limit)):
            if html_content[i] == '{':
                open_braces += 1
            elif html_content[i] == '}':
                open_braces -= 1
                if open_braces == 0:
                    end_index = i + 1
                    break
        
        if end_index != -1:
            # Извлечение полного, экранированного JSON-строки
            json_string_escaped = html_content[start_index:end_index]
            
            # Деэкранирование
            json_string = json_string_escaped.replace('\\"', '"').replace('\\\\', '\\')
            
            try:
                model_data = json.loads(json_string)
                model_name = model_data.get('publicName')
                
                # Дедупликация по publicName
                if model_name and model_name not in model_names:
                    models.append(model_data)
                    model_names.add(model_name)
            except json.JSONDecodeError as e:
                logger.warning(f"Ошибка при разборе извлечённого JSON-объекта: {e} - Содержимое: {json_string[:150]}...")
                continue

    if models:
        logger.info(f"Успешно извлечено и разобрано {len(models)} уникальных моделей.")
        return models
    else:
        logger.error("Ошибка: в HTML-ответе не найдено ни одного подходящего полного JSON-объекта модели.")
        return None

def save_available_models(new_models_list, models_path="available_models.json"):
    """
    Сохраняет список извлечённых полных объектов моделей в указанный JSON-файл.
    """
    logger.info(f"Обнаружено {len(new_models_list)} моделей, обновление '{models_path}'...")
    
    try:
        with open(models_path, 'w', encoding='utf-8') as f:
            # Записываем полный список объектов моделей в файл
            json.dump(new_models_list, f, indent=4, ensure_ascii=False)
        logger.info(f"✅ Файл '{models_path}' успешно обновлён, содержит {len(new_models_list)} моделей.")
    except IOError as e:
        logger.error(f"❌ Ошибка при записи в файл '{models_path}': {e}")

# --- Логика автоматического перезапуска ---
def restart_server():
    """Грациозно уведомляет клиентов о необходимости обновления и перезапускает сервер."""
    logger.warning("="*60)
    logger.warning("Обнаружен тайм-аут простоя сервера, подготовка к автоматическому перезапуску...")
    logger.warning("="*60)
    
    # 1. (Асинхронно) Уведомление браузера об обновлении
    async def notify_browser_refresh():
        if browser_ws:
            try:
                # Приоритетно отправляем команду 'reconnect', чтобы фронтенд знал, что это плановый перезапуск
                await browser_ws.send_text(json.dumps({"command": "reconnect"}, ensure_ascii=False))
                logger.info("Команда 'reconnect' отправлена в браузер.")
            except Exception as e:
                logger.error(f"Не удалось отправить команду 'reconnect': {e}")
    
    # Запуск асинхронной функции уведомления в главном цикле событий
    # Используем `asyncio.run_coroutine_threadsafe` для обеспечения безопасности потоков
    if browser_ws and browser_ws.client_state.name == 'CONNECTED' and main_event_loop:
        asyncio.run_coroutine_threadsafe(notify_browser_refresh(), main_event_loop)
    
    # 2. Задержка на несколько секунд, чтобы сообщение успело отправиться
    time.sleep(3)
    
    # 3. Выполнение перезапуска
    logger.info("Перезапуск сервера...")
    os.execv(sys.executable, ['python'] + sys.argv)

def idle_monitor():
    """Работает в фоновом потоке, отслеживает простой сервера."""
    global last_activity_time
    
    # Ожидаем, пока last_activity_time не будет установлено
    while last_activity_time is None:
        time.sleep(1)
        
    logger.info("Поток мониторинга простоя запущен.")
    
    while True:
        if CONFIG.get("enable_idle_restart", False):
            timeout = CONFIG.get("idle_restart_timeout_seconds", 300)
            
            # Если тайм-аут равен -1, отключаем проверку перезапуска
            if timeout == -1:
                time.sleep(10)  # Спим, чтобы избежать занятого цикла
                continue

            idle_time = (datetime.now() - last_activity_time).total_seconds()
            
            if idle_time > timeout:
                logger.info(f"Время простоя сервера ({idle_time:.0f}с) превысило порог ({timeout}с).")
                restart_server()
                break  # Выходим из цикла, так как процесс будет заменён
                
        # Проверяем каждые 10 секунд
        time.sleep(10)

# --- События жизненного цикла FastAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Функция жизненного цикла, выполняемая при запуске сервера."""
    global idle_monitor_thread, last_activity_time, main_event_loop
    main_event_loop = asyncio.get_running_loop()  # Получаем главный цикл событий
    load_config()  # Сначала загружаем конфигурацию
    
    # --- Вывод текущего режима работы ---
    mode = CONFIG.get("id_updater_last_mode", "direct_chat")
    target = CONFIG.get("id_updater_battle_target", "A")
    logger.info("="*60)
    logger.info(f"  Текущий режим работы: {mode.upper()}")
    if mode == 'battle':
        logger.info(f"  - Цель режима Battle: Assistant {target}")
    logger.info("  (Режим можно изменить, запустив id_updater.py)")
    logger.info("="*60)

    # обновления отключены чтобы не было сюрпризов
    # т.к. они идут на оригинальный репозиторий Lianues/LMArenaBridge (см функцию check_for_updates)
    # check_for_updates()  # Проверка обновлений программы
    load_model_map()  # Повторная загрузка моделей
    load_model_endpoint_map()  # Загрузка сопоставления конечных точек моделей
    logger.info("Сервер успешно запущен. Ожидание подключения скрипта Tampermonkey...")

    # Проверка и отображение объявления в конце, чтобы оно было более заметным
    check_and_display_announcement()

    # После обновления моделей устанавливаем начальную точку времени активности
    last_activity_time = datetime.now()
    
    # Запуск потока мониторинга простоя
    if CONFIG.get("enable_idle_restart", False):
        idle_monitor_thread = threading.Thread(target=idle_monitor, daemon=True)
        idle_monitor_thread.start()
        
    yield
    logger.info("Сервер завершает работу.")

app = FastAPI(lifespan=lifespan)

# --- Конфигурация CORS middleware ---
# Разрешаем все источники, методы и заголовки — безопасно для локальных инструментов разработки.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Вспомогательные функции ---
def save_config():
    """Сохраняет текущий объект CONFIG обратно в config.jsonc, сохраняя комментарии."""
    try:
        # Читаем исходный файл, чтобы сохранить комментарии
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Безопасная замена значений с помощью регулярного выражения
        def replacer(key, value, content):
            # Это регулярное выражение ищет ключ, затем его значение до запятой или закрывающей скобки
            pattern = re.compile(rf'("{key}"\s*:\s*").*?("?)(,?\s*)$', re.MULTILINE)
            replacement = rf'\g<1>{value}\g<2>\g<3>'
            if not pattern.search(content):  # Если ключ не найден, добавляем его в конец файла (упрощённая обработка)
                content = re.sub(r'}\s*$', f'  ,"{key}": "{value}"\n}}', content)
            else:
                content = pattern.sub(replacement, content)
            return content

        content_str = "".join(lines)
        content_str = replacer("session_id", CONFIG["session_id"], content_str)
        content_str = replacer("message_id", CONFIG["message_id"], content_str)
        
        with open('config.jsonc', 'w', encoding='utf-8') as f:
            f.write(content_str)
        logger.info("✅ Информация о сессии успешно обновлена в config.jsonc.")
    except Exception as e:
        logger.error(f"❌ Ошибка при записи в config.jsonc: {e}", exc_info=True)

async def _process_openai_message(message: dict) -> dict:
    """
    Обрабатывает сообщения OpenAI, разделяя текст и вложения.
    - Разбирает список мультимодального контента на чистый текст и список вложений.
    - Логика файлового хранилища перенесена в предварительную обработку chat_completions, здесь только стандартная сборка вложений.
    - Убедитесь, что пустое содержимое роли user заменяется пробелом, чтобы избежать ошибок LMArena.
    """
    content = message.get("content")
    role = message.get("role")
    attachments = []
    text_content = ""

    if isinstance(content, list):
        text_parts = []
        for part in content:
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                # URL здесь может быть base64 или http (уже заменён препроцессором)
                image_url_data = part.get("image_url", {})
                url = image_url_data.get("url")
                original_filename = image_url_data.get("detail")

                try:
                    # Для base64 извлекаем content_type
                    if url.startswith("data:"):
                        content_type = url.split(';')[0].split(':')[1]
                    else:
                        # Для http URL пытаемся угадать content_type
                        content_type = mimetypes.guess_type(url)[0] or 'application/octet-stream'

                    file_name = original_filename or f"image_{uuid.uuid4()}.{mimetypes.guess_extension(content_type).lstrip('.') or 'png'}"
                    
                    attachments.append({
                        "name": file_name,
                        "contentType": content_type,
                        "url": url
                    })

                except (AttributeError, IndexError, ValueError) as e:
                    logger.warning(f"Ошибка при обработке URL вложения: {url[:100]}... Ошибка: {e}")

        text_content = "\n\n".join(text_parts)
    elif isinstance(content, str):
        text_content = content

    if role == "user" and not text_content.strip():
        text_content = " "

    return {
        "role": role,
        "content": text_content,
        "attachments": attachments
    }

async def convert_openai_to_lmarena_payload(openai_data: dict, session_id: str, message_id: str, mode_override: str = None, battle_target_override: str = None) -> dict:
    """
    Преобразует тело запроса OpenAI в упрощённую нагрузку для скрипта Tampermonkey, применяя режимы Таверны, обхода и Battle.
    Добавлены параметры переопределения режима для поддержки специфичных для модели режимов сессий.
    """
    # 1. Нормализация ролей и обработка сообщений
    #    - Преобразование нестандартной роли 'developer' в 'system' для повышения совместимости.
    #    - Разделение текста и вложений.
    messages = openai_data.get("messages", [])
    for msg in messages:
        if msg.get("role") == "developer":
            msg["role"] = "system"
            logger.info("Нормализация роли сообщения: преобразование 'developer' в 'system'.")
            
    processed_messages = []
    for msg in messages:
        processed_msg = await _process_openai_message(msg.copy())
        processed_messages.append(processed_msg)

    # 2. Применение режима Таверны (Tavern Mode)
    if CONFIG.get("tavern_mode_enabled"):
        system_prompts = [msg['content'] for msg in processed_messages if msg['role'] == 'system']
        other_messages = [msg for msg in processed_messages if msg['role'] != 'system']
        
        merged_system_prompt = "\n\n".join(system_prompts)
        final_messages = []
        
        if merged_system_prompt:
            # Системные сообщения не должны содержать вложения
            final_messages.append({"role": "system", "content": merged_system_prompt, "attachments": []})
        
        final_messages.extend(other_messages)
        processed_messages = final_messages

    # 3. Определение идентификатора целевой модели
    model_name = openai_data.get("model", "claude-3-5-sonnet-20241022")
    model_info = MODEL_NAME_TO_ID_MAP.get(model_name, {})  # Ключевое исправление: model_info всегда словарь
    
    target_model_id = None
    if model_info:
        target_model_id = model_info.get("id")
    else:
        logger.warning(f"Модель '{model_name}' не найдена в 'models.json'. Запрос будет отправлен без идентификатора модели.")

    if not target_model_id:
        logger.warning(f"Для модели '{model_name}' не найден идентификатор в 'models.json'. Запрос будет отправлен без идентификатора модели.")

    # 4. Формирование шаблонов сообщений
    message_templates = []
    for msg in processed_messages:
        message_templates.append({
            "role": msg["role"],
            "content": msg.get("content", ""),
            "attachments": msg.get("attachments", [])
        })
    
    # 4.5. Специальная обработка: если последнее сообщение пользователя содержит --bypass и изображения, создаём фальшивое сообщение ассистента
    if message_templates and message_templates[-1]["role"] == "user":
        last_msg = message_templates[-1]
        if last_msg["content"].strip().endswith("--bypass") and last_msg.get("attachments"):
            has_images = False
            for attachment in last_msg.get("attachments", []):
                if attachment.get("contentType", "").startswith("image/"):
                    has_images = True
                    break
            
            if has_images:
                logger.info("Обнаружен маркер --bypass и вложения-изображения, создание фальшивого сообщения ассистента")
                
                # Удаляем маркер --bypass из сообщения пользователя
                last_msg["content"] = last_msg["content"].strip()[:-9].strip()
                
                # Формируем фальшивое сообщение ассистента, используя изображения из сообщения пользователя
                fake_assistant_msg = {
                    "role": "assistant",
                    "content": "",  # Пустое содержимое
                    "attachments": last_msg.get("attachments", []).copy()  # Копируем изображения пользователя
                }
                
                # Очищаем список вложений исходного сообщения пользователя
                last_msg["attachments"] = []
                
                # Вставляем фальшивое сообщение ассистента перед сообщением пользователя
                message_templates.insert(len(message_templates)-1, fake_assistant_msg)
                
                # Проверяем, нужно ли добавить фальшивое сообщение пользователя в начало
                if message_templates[0]["role"] == "assistant":
                    logger.info("Обнаружено, что первое сообщение — от ассистента, добавляется фальшивое сообщение пользователя...")
                    fake_user_msg = {
                        "role": "user",
                        "content": "Hi",
                        "attachments": []
                    }
                    message_templates.insert(0, fake_user_msg)

    # 5. Применение режима обхода (Bypass Mode) — действует только для текстовых моделей
    model_type = model_info.get("type", "text")
    if CONFIG.get("bypass_enabled") and model_type == "text":
        # Режим обхода всегда добавляет сообщение пользователя с позицией 'a'
        logger.info("Режим обхода включён, добавляется пустое сообщение пользователя.")
        message_templates.append({"role": "user", "content": " ", "participantPosition": "a", "attachments": []})

    # 6. Применение позиции участника (Participant Position)
    # Приоритетно используем переопределённый режим, иначе возвращаемся к глобальной конфигурации
    mode = mode_override or CONFIG.get("id_updater_last_mode", "direct_chat")
    target_participant = battle_target_override or CONFIG.get("id_updater_battle_target", "A")
    target_participant = target_participant.lower()  # Убедимся, что это строчные буквы

    logger.info(f"Установка позиций участников в соответствии с режимом '{mode}' (цель: {target_participant if mode == 'battle' else 'N/A'})...")

    for msg in message_templates:
        if msg['role'] == 'system':
            if mode == 'battle':
                # Режим Battle: системное сообщение на той же стороне, что и выбранный ассистент (A — a, B — b)
                msg['participantPosition'] = target_participant
            else:
                # Режим DirectChat: системное сообщение всегда 'b'
                msg['participantPosition'] = 'b'
        elif mode == 'battle':
            # В режиме Battle несистемные сообщения используют выбранную цель участника
            msg['participantPosition'] = target_participant
        else:  # Режим DirectChat
            # В режиме DirectChat несистемные сообщения используют 'a' по умолчанию
            msg['participantPosition'] = 'a'

    return {
        "message_templates": message_templates,
        "target_model_id": target_model_id,
        "session_id": session_id,
        "message_id": message_id
    }

# --- Вспомогательные функции форматирования OpenAI (обеспечивают надёжную JSON-сериализацию) ---
def format_openai_chunk(content: str, model: str, request_id: str) -> str:
    """Форматирует в потоковый блок OpenAI."""
    chunk = {
        "id": request_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

def format_openai_finish_chunk(model: str, request_id: str, reason: str = 'stop') -> str:
    """Форматирует в завершающий блок OpenAI."""
    chunk = {
        "id": request_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"

def format_openai_error_chunk(error_message: str, model: str, request_id: str) -> str:
    """Форматирует в блок ошибки OpenAI."""
    content = f"\n\n[LMArena Bridge Error]: {error_message}"
    return format_openai_chunk(content, model, request_id)

def format_openai_non_stream_response(content: str, model: str, request_id: str, reason: str = 'stop') -> dict:
    """Формирует тело ответа OpenAI для непотокового режима."""
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": reason,
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": len(content) // 4,
            "total_tokens": len(content) // 4,
        },
    }

async def _process_lmarena_stream(request_id: str):
    """
    Основной внутренний генератор: обрабатывает поток сырых данных из браузера и выдаёт структурированные события.
    Типы событий: ('content', str), ('finish', str), ('error', str)
    """
    global IS_REFRESHING_FOR_VERIFICATION
    queue = response_channels.get(request_id)
    if not queue:
        logger.error(f"PROCESSOR [ID: {request_id[:8]}]: Не найден канал ответа.")
        yield 'error', 'Внутренняя ошибка сервера: канал ответа не найден.'
        return

    buffer = ""
    timeout = CONFIG.get("stream_response_timeout_seconds", 360)
    text_pattern = re.compile(r'[ab]0:"((?:\\.|[^"\\])*)"')
    # Новое: регулярное выражение для извлечения URL изображений
    image_pattern = re.compile(r'[ab]2:(\[.*?\])')
    finish_pattern = re.compile(r'[ab]d:(\{.*?"finishReason".*?\})')
    error_pattern = re.compile(r'(\{\s*"error".*?\})', re.DOTALL)
    cloudflare_patterns = [r'<title>Just a moment...</title>', r'Enable JavaScript and cookies to continue']
    
    has_yielded_content = False  # Отмечаем, был ли выдан валидный контент

    try:
        while True:
            try:
                raw_data = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: Тайм-аут ожидания данных браузера ({timeout} секунд).")
                yield 'error', f'Ответ превысил время ожидания ({timeout} секунд).'
                return

            # --- Обработка проверки Cloudflare на человекоподобность ---
            def handle_cloudflare_verification():
                global IS_REFRESHING_FOR_VERIFICATION
                if not IS_REFRESHING_FOR_VERIFICATION:
                    logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: Первое обнаружение проверки на человекоподобность, отправка команды обновления.")
                    IS_REFRESHING_FOR_VERIFICATION = True
                    if browser_ws:
                        asyncio.create_task(browser_ws.send_text(json.dumps({"command": "refresh"}, ensure_ascii=False)))
                    return "Обнаружена проверка на человекоподобность, отправлена команда обновления, пожалуйста, повторите попытку позже."
                else:
                    logger.info(f"PROCESSOR [ID: {request_id[:8]}]: Обнаружена проверка на человекоподобность, но обновление уже выполняется, ожидание.")
                    return "Ожидание завершения проверки на человекоподобность..."

            # 1. Проверка прямых ошибок от WebSocket
            if isinstance(raw_data, dict) and 'error' in raw_data:
                error_msg = raw_data.get('error', 'Неизвестная ошибка браузера')
                if isinstance(error_msg, str):
                    if '413' in error_msg or 'too large' in error_msg.lower():
                        friendly_error_msg = "Ошибка загрузки: размер вложения превышает ограничения сервера LMArena (обычно около 5 МБ). Попробуйте сжать файл или загрузить меньший."
                        logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: Обнаружена ошибка превышения размера вложения (413).")
                        yield 'error', friendly_error_msg
                        return
                    if any(re.search(p, error_msg, re.IGNORECASE) for p in cloudflare_patterns):
                        yield 'error', handle_cloudflare_verification()
                        return
                yield 'error', error_msg
                return

            # 2. Проверка сигнала [DONE]
            if raw_data == "[DONE]":
                # Логика сброса состояния перенесена в websocket_endpoint, чтобы гарантировать сброс при восстановлении соединения
                if has_yielded_content and IS_REFRESHING_FOR_VERIFICATION:
                    logger.info(f"PROCESSOR [ID: {request_id[:8]}]: Запрос успешен, состояние проверки на человекоподобность будет сброшено при следующем соединении.")
                break

            # 3. Накопление буфера и проверка содержимого
            buffer += "".join(str(item) for item in raw_data) if isinstance(raw_data, list) else raw_data

            if any(re.search(p, buffer, re.IGNORECASE) for p in cloudflare_patterns):
                yield 'error', handle_cloudflare_verification()
                return
            
            if (error_match := error_pattern.search(buffer)):
                try:
                    error_json = json.loads(error_match.group(1))
                    yield 'error', error_json.get("error", "Неизвестная ошибка от LMArena")
                    return
                except json.JSONDecodeError: pass

            # Приоритетная обработка текстового содержимого
            while (match := text_pattern.search(buffer)):
                try:
                    text_content = json.loads(f'"{match.group(1)}"')
                    if text_content:
                        has_yielded_content = True
                        yield 'content', text_content
                except (ValueError, json.JSONDecodeError): pass
                buffer = buffer[match.end():]

            # Новое: обработка содержимого изображений
            while (match := image_pattern.search(buffer)):
                try:
                    image_data_list = json.loads(match.group(1))
                    if isinstance(image_data_list, list) and image_data_list:
                        image_info = image_data_list[0]
                        if image_info.get("type") == "image" and "image" in image_info:
                            # Оборачиваем URL в Markdown-формат и выдаём как блок контента
                            markdown_image = f"![Image]({image_info['image']})"
                            yield 'content', markdown_image
                except (json.JSONDecodeError, IndexError) as e:
                    logger.warning(f"Ошибка при разборе URL изображения: {e}, буфер: {buffer[:150]}")
                buffer = buffer[match.end():]

            if (finish_match := finish_pattern.search(buffer)):
                try:
                    finish_data = json.loads(finish_match.group(1))
                    yield 'finish', finish_data.get("finishReason", "stop")
                except (json.JSONDecodeError, IndexError): pass
                buffer = buffer[finish_match.end():]

    except asyncio.CancelledError:
        logger.info(f"PROCESSOR [ID: {request_id[:8]}]: Задача отменена.")
    finally:
        if request_id in response_channels:
            del response_channels[request_id]
            logger.info(f"PROCESSOR [ID: {request_id[:8]}]: Канал ответа очищен.")

async def stream_generator(request_id: str, model: str):
    """Форматирует поток внутренних событий в SSE-ответ OpenAI."""
    response_id = f"chatcmpl-{uuid.uuid4()}"
    logger.info(f"STREAMER [ID: {request_id[:8]}]: Потоковый генератор запущен.")
    
    finish_reason_to_send = 'stop'  # Причина завершения по умолчанию

    async for event_type, data in _process_lmarena_stream(request_id):
        if event_type == 'content':
            yield format_openai_chunk(data, model, response_id)
        elif event_type == 'finish':
            # Сохраняем причину завершения, но не завершаем немедленно, ждём [DONE] от браузера
            finish_reason_to_send = data
            if data == 'content-filter':
                warning_msg = "\n\nОтвет прерван, вероятно, из-за превышения контекста или внутренней цензуры модели (наиболее вероятно)."
                yield format_openai_chunk(warning_msg, model, response_id)
        elif event_type == 'error':
            logger.error(f"STREAMER [ID: {request_id[:8]}]: Ошибка в потоке: {data}")
            yield format_openai_error_chunk(str(data), model, response_id)
            yield format_openai_finish_chunk(model, response_id, reason='stop')
            return  # При ошибке немедленно завершаем

    # Выполняется только после естественного завершения _process_lmarena_stream (т.е. получения [DONE])
    yield format_openai_finish_chunk(model, response_id, reason=finish_reason_to_send)
    logger.info(f"STREAMER [ID: {request_id[:8]}]: Потоковый генератор завершён нормально.")

async def non_stream_response(request_id: str, model: str):
    """Агрегирует поток внутренних событий и возвращает единый JSON-ответ OpenAI."""
    response_id = f"chatcmpl-{uuid.uuid4()}"
    logger.info(f"NON-STREAM [ID: {request_id[:8]}]: Начало обработки непотокового ответа.")
    
    full_content = []
    finish_reason = "stop"
    
    async for event_type, data in _process_lmarena_stream(request_id):
        if event_type == 'content':
            full_content.append(data)
        elif event_type == 'finish':
            finish_reason = data
            if data == 'content-filter':
                full_content.append("\n\nОтвет прерван, вероятно, из-за превышения контекста или внутренней цензуры модели (наиболее вероятно).")
            # Не прерываем здесь, ждём сигнала [DONE] от браузера, чтобы избежать состояния гонки
        elif event_type == 'error':
            logger.error(f"NON-STREAM [ID: {request_id[:8]}]: Ошибка при обработке: {data}")
            
            # Унифицируем коды ошибок для потоковых и непотоковых ответов
            status_code = 413 if "вложения превышает" in str(data) else 500

            error_response = {
                "error": {
                    "message": f"[LMArena Bridge Error]: {data}",
                    "type": "bridge_error",
                    "code": "attachment_too_large" if status_code == 413 else "processing_error"
                }
            }
            return Response(content=json.dumps(error_response, ensure_ascii=False), status_code=status_code, media_type="application/json")

    final_content = "".join(full_content)
    response_data = format_openai_non_stream_response(final_content, model, response_id, reason=finish_reason)
    
    logger.info(f"NON-STREAM [ID: {request_id[:8]}]: Агрегация ответа завершена.")
    return Response(content=json.dumps(response_data, ensure_ascii=False), media_type="application/json")

# --- WebSocket-эндпоинт ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Обрабатывает WebSocket-соединение от скрипта Tampermonkey."""
    global browser_ws, IS_REFRESHING_FOR_VERIFICATION
    await websocket.accept()
    if browser_ws is not None:
        logger.warning("Обнаружено новое подключение скрипта Tampermonkey, старое соединение будет заменено.")
    
    # Новое соединение означает завершение процесса проверки на человекоподобность (или его отсутствие)
    if IS_REFRESHING_FOR_VERIFICATION:
        logger.info("✅ Установлено новое WebSocket-соединение, состояние проверки на человекоподобность автоматически сброшено.")
        IS_REFRESHING_FOR_VERIFICATION = False
        
    logger.info("✅ Скрипт Tampermonkey успешно подключился к WebSocket.")
    browser_ws = websocket
    try:
        while True:
            # Ожидаем и принимаем сообщения от скрипта Tampermonkey
            message_str = await websocket.receive_text()
            message = json.loads(message_str)
            
            request_id = message.get("request_id")
            data = message.get("data")

            if not request_id or data is None:
                logger.warning(f"Получено недействительное сообщение от браузера: {message}")
                continue

            # Помещаем полученные данные в соответствующий канал ответа
            if request_id in response_channels:
                await response_channels[request_id].put(data)
            else:
                logger.warning(f"⚠️ Получен ответ для неизвестного или закрытого запроса: {request_id}")

    except WebSocketDisconnect:
        logger.warning("❌ Клиент скрипта Tampermonkey отключился.")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при обработке WebSocket: {e}", exc_info=True)
    finally:
        browser_ws = None
        # Очищаем все ожидающие каналы ответа, чтобы избежать зависания запросов
        for queue in response_channels.values():
            await queue.put({"error": "Браузер отключился во время операции"})
        response_channels.clear()
        logger.info("WebSocket-соединение очищено.")

# --- Совместимые с OpenAI API эндпоинты ---
@app.get("/v1/models")
async def get_models():
    """Предоставляет список моделей, совместимый с OpenAI."""
    if not MODEL_NAME_TO_ID_MAP:
        return JSONResponse(
            status_code=404,
            content={"error": "Список моделей пуст или файл 'models.json' не найден."}
        )
    
    return {
        "object": "list",
        "data": [
            {
                "id": model_name, 
                "object": "model",
                "created": int(time.time()),
                "owned_by": "LMArenaBridge"
            }
            for model_name in MODEL_NAME_TO_ID_MAP.keys()
        ],
    }

@app.post("/internal/request_model_update")
async def request_model_update():
    """
    Принимает запрос от model_updater.py и отправляет команду через WebSocket,
    чтобы скрипт Tampermonkey отправил исходный код страницы.
    """
    if not browser_ws:
        logger.warning("MODEL UPDATE: Получен запрос на обновление, но браузер не подключён.")
        raise HTTPException(status_code=503, detail="Клиент браузера не подключён.")
    
    try:
        logger.info("MODEL UPDATE: Получен запрос на обновление, отправка команды через WebSocket...")
        await browser_ws.send_text(json.dumps({"command": "send_page_source"}))
        logger.info("MODEL UPDATE: Команда 'send_page_source' успешно отправлена.")
        return JSONResponse({"status": "success", "message": "Запрос на отправку исходного кода страницы отправлен."})
    except Exception as e:
        logger.error(f"MODEL UPDATE: Ошибка при отправке команды: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Не удалось отправить команду через WebSocket.")

@app.post("/internal/update_available_models")
async def update_available_models_endpoint(request: Request):
    """
    Принимает HTML страницы от скрипта Tampermonkey, извлекает и обновляет available_models.json.
    """
    html_content = await request.body()
    if not html_content:
        logger.warning("Запрос на обновление моделей не содержит HTML-содержимого.")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "HTML-содержимое не получено."}
        )
    
    logger.info("Получено содержимое страницы от скрипта Tampermonkey, начало извлечения доступных моделей...")
    new_models_list = extract_models_from_html(html_content.decode('utf-8'))
    
    if new_models_list:
        save_available_models(new_models_list)
        return JSONResponse({"status": "success", "message": "Файл доступных моделей обновлён."})
    else:
        logger.error("Не удалось извлечь данные моделей из HTML, предоставленного скриптом Tampermonkey.")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Не удалось извлечь данные моделей из HTML."}
        )

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    Обрабатывает запросы на завершение чата.
    Принимает запросы в формате OpenAI, преобразует их в формат LMArena,
    отправляет через WebSocket скрипту Tampermonkey и возвращает результат в потоковом режиме.
    """
    global last_activity_time
    last_activity_time = datetime.now()  # Обновляем время активности
    logger.info(f"Получен API-запрос, время активности обновлено: {last_activity_time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        openai_req = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Недействительное тело запроса JSON")

    model_name = openai_req.get("model")
    model_info = MODEL_NAME_TO_ID_MAP.get(model_name, {})  # Ключевое исправление: возвращаем пустой словарь, если модель не найдена
    model_type = model_info.get("type", "text")  # По умолчанию текст

    # --- Новое: логика на основе типа модели ---
    if model_type == 'image':
        logger.info(f"Обнаружен тип модели '{model_name}' — 'image', обработка через основной интерфейс чата.")
        # Для моделей изображений больше не вызываем отдельный обработчик, используем основную логику чата,
        # так как _process_lmarena_stream теперь может обрабатывать данные изображений.
        # Это означает, что генерация изображений теперь нативно поддерживает потоковые и непотоковые ответы.
        pass  # Продолжаем с общей логикой чата
    # --- Конец логики генерации изображений ---

    # Если модель не для изображений, выполняем стандартную логику генерации текста
    load_config()  # Загружаем последнюю конфигурацию в реальном времени, чтобы гарантировать актуальность идентификаторов сессии
    # --- Проверка API-ключа ---
    api_key = CONFIG.get("api_key")
    if api_key:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            raise HTTPException(
                status_code=401,
                detail="API-ключ не предоставлен. Укажите его в заголовке Authorization в формате 'Bearer YOUR_KEY'."
            )
        
        provided_key = auth_header.split(' ')[1]
        if provided_key != api_key:
            raise HTTPException(
                status_code=401,
                detail="Предоставлен неверный API-ключ."
            )

    # --- Улучшенная проверка соединения для устранения состояния гонки после проверки на человекоподобность ---
    if IS_REFRESHING_FOR_VERIFICATION and not browser_ws:
        raise HTTPException(
            status_code=503,
            detail="Ожидание обновления браузера для завершения проверки на человекоподобность, повторите попытку через несколько секунд."
        )

    if not browser_ws:
        raise HTTPException(
            status_code=503,
            detail="Клиент скрипта Tampermonkey не подключён. Убедитесь, что страница LMArena открыта и скрипт активирован."
        )

    # --- Логика сопоставления моделей и идентификаторов сессий ---
    session_id, message_id = None, None
    mode_override, battle_target_override = None, None

    if model_name and model_name in MODEL_ENDPOINT_MAP:
        mapping_entry = MODEL_ENDPOINT_MAP[model_name]
        selected_mapping = None

        if isinstance(mapping_entry, list) and mapping_entry:
            selected_mapping = random.choice(mapping_entry)
            logger.info(f"Для модели '{model_name}' случайным образом выбран один из списков сопоставлений.")
        elif isinstance(mapping_entry, dict):
            selected_mapping = mapping_entry
            logger.info(f"Для модели '{model_name}' найден единственный сопоставленный эндпоинт (старый формат).")
        
        if selected_mapping:
            session_id = selected_mapping.get("session_id")
            message_id = selected_mapping.get("message_id")
            # Ключевое: получение информации о режиме
            mode_override = selected_mapping.get("mode")  # Может быть None
            battle_target_override = selected_mapping.get("battle_target")  # Может быть None
            log_msg = f"Будет использован Session ID: ...{session_id[-6:] if session_id else 'N/A'}"
            if mode_override:
                log_msg += f" (режим: {mode_override}"
                if mode_override == 'battle':
                    log_msg += f", цель: {battle_target_override or 'A'}"
                log_msg += ")"
            logger.info(log_msg)

    # Если session_id всё ещё None, переходим к логике глобального отката
    if not session_id:
        if CONFIG.get("use_default_ids_if_mapping_not_found", True):
            session_id = CONFIG.get("session_id")
            message_id = CONFIG.get("message_id")
            # При использовании глобальных идентификаторов не устанавливаем переопределение режима
            mode_override, battle_target_override = None, None
            logger.info(f"Для модели '{model_name}' не найдено действительное сопоставление, используется глобальный Session ID по умолчанию: ...{session_id[-6:] if session_id else 'N/A'}")
        else:
            logger.error(f"Модель '{model_name}' не имеет действительного сопоставления в 'model_endpoint_map.json', и откат к идентификаторам по умолчанию отключён.")
            raise HTTPException(
                status_code=400,
                detail=f"Для модели '{model_name}' не настроен отдельный идентификатор сессии. Добавьте действительное сопоставление в 'model_endpoint_map.json' или включите 'use_default_ids_if_mapping_not_found' в 'config.jsonc'."
            )

    # --- Проверка окончательно определённой информации о сессии ---
    if not session_id or not message_id or "YOUR_" in session_id or "YOUR_" in message_id:
        raise HTTPException(
            status_code=400,
            detail="Окончательно определённые идентификаторы сессии или сообщения недействительны. Проверьте конфигурацию в 'model_endpoint_map.json' и 'config.jsonc' или запустите `id_updater.py` для обновления значений по умолчанию."
        )

    if not model_name or model_name not in MODEL_NAME_TO_ID_MAP:
        logger.warning(f"Запрошенная модель '{model_name}' отсутствует в models.json, будет использован идентификатор модели по умолчанию.")

    request_id = str(uuid.uuid4())
    response_channels[request_id] = asyncio.Queue()
    logger.info(f"API CALL [ID: {request_id[:8]}]: Создан канал ответа.")

    try:
        # --- Предобработка вложений (включая загрузку в файловое хранилище) ---
        # Обрабатываем все вложения до взаимодействия с браузером. При ошибке немедленно возвращаем ошибку.
        messages_to_process = openai_req.get("messages", [])
        for message in messages_to_process:
            content = message.get("content")
            if isinstance(content, list):
                for i, part in enumerate(content):
                    if part.get("type") == "image_url" and CONFIG.get("file_bed_enabled"):
                        image_url_data = part.get("image_url", {})
                        base64_url = image_url_data.get("url")
                        original_filename = image_url_data.get("detail")
                        
                        if not (base64_url and base64_url.startswith("data:")):
                            raise ValueError(f"Недействительный формат данных изображения: {base64_url[:100] if base64_url else 'None'}")

                        upload_url = CONFIG.get("file_bed_upload_url")
                        if not upload_url:
                            raise ValueError("Файловое хранилище включено, но 'file_bed_upload_url' не настроен.")
                        
                        # Убедимся, что экранированные слэши обработаны
                        upload_url = upload_url.replace('\\/', '/')

                        api_key = CONFIG.get("file_bed_api_key")
                        file_name = original_filename or f"image_{uuid.uuid4()}.png"
                        
                        logger.info(f"Предобработка файлового хранилища: загрузка '{file_name}'...")
                        uploaded_filename, error_message = await upload_to_file_bed(file_name, base64_url, upload_url, api_key)

                        if error_message:
                            raise IOError(f"Ошибка загрузки в файловое хранилище: {error_message}")
                        
                        # Формируем конечный URL на основе префикса URL из конфигурации
                        url_prefix = upload_url.rsplit('/', 1)[0]
                        final_url = f"{url_prefix}/uploads/{uploaded_filename}"
                        
                        part["image_url"]["url"] = final_url
                        logger.info(f"URL вложения успешно заменён на: {final_url}")

        # 1. Преобразование запроса (вложения уже обработаны)
        lmarena_payload = await convert_openai_to_lmarena_payload(
            openai_req,
            session_id,
            message_id,
            mode_override=mode_override,
            battle_target_override=battle_target_override
        )
        
        # Ключевое дополнение: если модель — для изображений, явно указываем это скрипту Tampermonkey
        if model_type == 'image':
            lmarena_payload['is_image_request'] = True
        
        # 2. Формируем сообщение для отправки в браузер
        message_to_browser = {
            "request_id": request_id,
            "payload": lmarena_payload
        }
        
        # 3. Отправляем через WebSocket
        logger.info(f"API CALL [ID: {request_id[:8]}]: Отправка нагрузки скрипту Tampermonkey через WebSocket.")
        await browser_ws.send_text(json.dumps(message_to_browser))

        # 4. Определяем тип ответа в зависимости от параметра stream
        is_stream = openai_req.get("stream", False)

        if is_stream:
            # Возвращаем потоковый ответ
            return StreamingResponse(
                stream_generator(request_id, model_name or "default_model"),
                media_type="text/event-stream"
            )
        else:
            # Возвращаем непотоковый ответ
            return await non_stream_response(request_id, model_name or "default_model")
    except (ValueError, IOError) as e:
        # Обрабатываем ошибки обработки вложений
        logger.error(f"API CALL [ID: {request_id[:8]}]: Ошибка предобработки вложений: {e}")
        if request_id in response_channels:
            del response_channels[request_id]
        # Возвращаем форматированный JSON-ответ с ошибкой
        return JSONResponse(
            status_code=500,
            content={"error": {"message": f"[LMArena Bridge Error] Ошибка обработки вложений: {e}", "type": "attachment_error"}}
        )
    except Exception as e:
        # Обрабатываем все остальные ошибки
        if request_id in response_channels:
            del response_channels[request_id]
        logger.error(f"API CALL [ID: {request_id[:8]}]: Критическая ошибка при обработке запроса: {e}", exc_info=True)
        # Убедимся, что возвращается форматированный JSON
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(e), "type": "internal_server_error"}}
        )

# --- Внутренний коммуникационный эндпоинт ---
@app.post("/internal/start_id_capture")
async def start_id_capture():
    """
    Принимает уведомление от id_updater.py и отправляет команду через WebSocket
    для активации режима захвата идентификаторов в скрипте Tampermonkey.
    """
    if not browser_ws:
        logger.warning("ID CAPTURE: Получен запрос на активацию, но браузер не подключён.")
        raise HTTPException(status_code=503, detail="Клиент браузера не подключён.")
    
    try:
        logger.info("ID CAPTURE: Получен запрос на активацию, отправка команды через WebSocket...")
        await browser_ws.send_text(json.dumps({"command": "activate_id_capture"}))
        logger.info("ID CAPTURE: Команда активации успешно отправлена.")
        return JSONResponse({"status": "success", "message": "Команда активации отправлена."})
    except Exception as e:
        logger.error(f"ID CAPTURE: Ошибка при отправке команды активации: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Не удалось отправить команду через WebSocket.")

# --- Точка входа программы ---
if __name__ == "__main__":
    # Рекомендуется считывать порт из config.jsonc, здесь временно закодирован
    api_port = 5102
    logger.info(f"🚀 LMArena Bridge v2.0 API-сервер запускается...")
    logger.info(f"   - Адрес прослушивания: http://127.0.0.1:{api_port}")
    logger.info(f"   - WebSocket-эндпоинт: ws://127.0.0.1:{api_port}/ws")
    
    uvicorn.run(app, host="0.0.0.0", port=api_port)