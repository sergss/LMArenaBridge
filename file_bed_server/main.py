# file_bed_server/main.py
import base64
import os
import uuid
import time
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import logging
from apscheduler.schedulers.background import BackgroundScheduler

# --- Базовая конфигурация ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Конфигурация путей ---
# Указываем директорию для загрузки в той же папке, где находится main.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "Uploads")
API_KEY = "your_secret_api_key"  # Простой ключ для аутентификации
CLEANUP_INTERVAL_MINUTES = 1 # Частота выполнения задачи очистки (в минутах)
FILE_MAX_AGE_MINUTES = 10 # Максимальное время хранения файлов (в минутах)

# --- Функция очистки ---
def cleanup_old_files():
    """Просматривает директорию загрузок и удаляет файлы, старше указанного времени."""
    now = time.time()
    cutoff = now - (FILE_MAX_AGE_MINUTES * 60)
    
    logger.info(f"Выполняется задача очистки, удаление файлов, созданных ранее {datetime.fromtimestamp(cutoff).strftime('%Y-%m-%d %H:%M:%S')}...")
    
    deleted_count = 0
    try:
        for filename in os.listdir(UPLOAD_DIR):
            file_path = os.path.join(UPLOAD_DIR, filename)
            if os.path.isfile(file_path):
                try:
                    file_mtime = os.path.getmtime(file_path)
                    if file_mtime < cutoff:
                        os.remove(file_path)
                        logger.info(f"Удалён устаревший файл: {filename}")
                        deleted_count += 1
                except OSError as e:
                    logger.error(f"Ошибка при удалении файла '{file_path}': {e}")
    except Exception as e:
        logger.error(f"Произошла неизвестная ошибка при очистке старых файлов: {e}", exc_info=True)

    if deleted_count > 0:
        logger.info(f"Задача очистки завершена, удалено {deleted_count} файлов.")
    else:
        logger.info("Задача очистки завершена, файлов для удаления не найдено.")

# --- События жизненного цикла FastAPI ---
scheduler = BackgroundScheduler(timezone="UTC")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Запускает фоновые задачи при старте сервера и останавливает их при завершении."""
    # Запуск планировщика и добавление задачи
    scheduler.add_job(cleanup_old_files, 'interval', minutes=CLEANUP_INTERVAL_MINUTES)
    scheduler.start()
    logger.info(f"Фоновая задача очистки файлов запущена, выполняется каждые {CLEANUP_INTERVAL_MINUTES} минут.")
    yield
    # Остановка планировщика
    scheduler.shutdown()
    logger.info("Фоновая задача очистки файлов остановлена.")

app = FastAPI(lifespan=lifespan)

# --- Проверка существования директории загрузок ---
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)
    logger.info(f"Директория загрузок '{UPLOAD_DIR}' создана.")

# --- Монтирование статической директории для доступа к файлам ---
app.mount(f"/Uploads", StaticFiles(directory=UPLOAD_DIR), name="Uploads")

# --- Определение модели Pydantic ---
class UploadRequest(BaseModel):
    file_name: str
    file_data: str # Принимает полный base64 data URI
    api_key: str | None = None

# --- API-эндпоинты ---
@app.post("/upload")
async def upload_file(request: UploadRequest, http_request: Request):
    """
    Принимает файл, закодированный в base64, сохраняет его и возвращает доступный URL.
    """
    # Простая аутентификация по API-ключу
    if API_KEY and request.api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Недействительный API-ключ")

    try:
        # 1. Разбор base64 data URI
        header, encoded_data = request.file_data.split(',', 1)
        
        # 2. Декодирование base64-данных
        file_data = base64.b64decode(encoded_data)
        
        # 3. Генерация уникального имени файла для избежания конфликтов
        file_extension = os.path.splitext(request.file_name)[1]
        if not file_extension:
            # Попытка определить расширение по mime-типу из заголовка
            import mimetypes
            mime_type = header.split(';')[0].split(':')[1]
            guessed_extension = mimetypes.guess_extension(mime_type)
            file_extension = guessed_extension if guessed_extension else '.bin'

        unique_filename = f"{uuid.uuid4()}{file_extension}"
        file_path = os.path.join(UPLOAD_DIR, unique_filename)

        # 4. Сохранение файла
        with open(file_path, "wb") as f:
            f.write(file_data)
        
        # 5. Возврат успешного ответа с уникальным именем файла
        logger.info(f"Файл '{request.file_name}' успешно сохранён как '{unique_filename}'.")
        
        return JSONResponse(
            status_code=200,
            content={"success": True, "filename": unique_filename}
        )

    except (ValueError, IndexError) as e:
        logger.error(f"Ошибка при разборе base64-данных: {e}")
        raise HTTPException(status_code=400, detail=f"Недействительный формат base64 data URI: {e}")
    except Exception as e:
        logger.error(f"Произошла неизвестная ошибка при обработке загрузки файла: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка сервера: {e}")

@app.get("/")
def read_root():
    return {"message": "Сервер файлового хранилища LMArena Bridge работает."}

# --- Точка входа программы ---
if __name__ == "__main__":
    import uvicorn
    logger.info("🚀 Сервер файлового хранилища запускается...")
    logger.info("   - Адрес прослушивания: http://127.0.0.1:5180")
    logger.info(f"   - Эндпоинт загрузки: http://127.0.0.1:5180/upload")
    logger.info(f"   - Путь доступа к файлам: /Uploads")
    uvicorn.run(app, host="0.0.0.0", port=5180)