# model_updater.py
import requests
import time
import logging

# --- Конфигурация ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
API_SERVER_URL = "http://127.0.0.1:5102" # Соответствует порту, указанному в api_server.py

def trigger_model_update():
    """
    Уведомляет главный сервер о начале процесса обновления списка моделей.
    """
    try:
        logging.info("Отправка запроса на обновление списка моделей на главный сервер...")
        response = requests.post(f"{API_SERVER_URL}/internal/request_model_update")
        response.raise_for_status()
        
        if response.json().get("status") == "success":
            logging.info("✅ Запрос на обновление списка моделей успешно отправлен на сервер.")
            logging.info("Убедитесь, что страница LMArena открыта, скрипт автоматически извлечёт актуальный список моделей со страницы.")
            logging.info("Сервер сохранит результаты в файле `available_models.json`.")
        else:
            logging.error(f"❌ Сервер вернул ошибку: {response.json().get('message')}")

    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Не удалось подключиться к главному серверу ({API_SERVER_URL}).")
        logging.error("Убедитесь, что `api_server.py` запущен.")
    except Exception as e:
        logging.error(f"Произошла неизвестная ошибка: {e}")

if __name__ == "__main__":
    trigger_model_update()
    # Скрипт автоматически завершает работу после выполнения
    time.sleep(2)
