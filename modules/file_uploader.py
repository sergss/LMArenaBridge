# modules/file_uploader.py
import httpx
import logging

logger = logging.getLogger(__name__)

from typing import Tuple

async def upload_to_file_bed(file_name: str, file_data: str, upload_url: str, api_key: str | None = None) -> Tuple[str | None, str | None]:
    """
    Загружает файл, закодированный в base64, на сервер файлового хранилища.

    :param file_name: Исходное имя файла.
    :param file_data: Base64 data URI (например, "data:image/png;base64,...").
    :param upload_url: URL конечной точки /upload файлового хранилища.
    :param api_key: (Необязательно) API-ключ для аутентификации.
    :return: Кортеж (filename, error_message). При успехе filename — строка, error_message — None;
             при неудаче filename — None, error_message — строка с описанием ошибки.
    """
    payload = {
        "file_name": file_name,
        "file_data": file_data,
        "api_key": api_key
    }
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(upload_url, json=payload)
            
            response.raise_for_status()  # Вызывает исключение при статусах 4xx или 5xx
            
            result = response.json()
            if result.get("success") and result.get("filename"):
                logger.info(f"Файл '{file_name}' успешно загружен в файловое хранилище, имя файла: {result['filename']}")
                return result["filename"], None
            else:
                error_msg = result.get("error", "Файловое хранилище вернуло неизвестную ошибку.")
                logger.error(f"Не удалось загрузить файл в хранилище: {error_msg}")
                return None, error_msg
                
    except httpx.HTTPStatusError as e:
        error_details = f"HTTP-ошибка: {e.response.status_code} - {e.response.text}"
        logger.error(f"Произошла ошибка при загрузке в файловое хранилище: {error_details}")
        return None, error_details
    except httpx.RequestError as e:
        error_details = f"Ошибка соединения: {e}"
        logger.error(f"Ошибка подключения к серверу файлового хранилища: {e}")
        return None, error_details
    except Exception as e:
        error_details = f"Неизвестная ошибка: {e}"
        logger.error(f"Произошла неизвестная ошибка при загрузке файла: {e}", exc_info=True)
        return None, error_details