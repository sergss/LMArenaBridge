# update_script.py
import os
import shutil
import time
import subprocess
import sys
import json
import re

def _parse_jsonc(jsonc_string: str) -> dict:
    """
    Надёжно парсит JSONC-строку, удаляя комментарии.
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

def load_jsonc_values(path):
    """Загружает данные из файла .jsonc, игнорируя комментарии, возвращает только пары ключ-значение."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        return _parse_jsonc(content)
    except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
        print(f"Ошибка при загрузке или разборе значений из {path}: {e}")
        return None

def get_all_relative_paths(directory):
    """Получает набор относительных путей всех файлов и пустых папок в директории."""
    paths = set()
    for root, dirs, files in os.walk(directory):
        # Добавление файлов
        for name in files:
            path = os.path.join(root, name)
            paths.add(os.path.relpath(path, directory))
        # Добавление пустых папок
        for name in dirs:
            dir_path = os.path.join(root, name)
            if not os.listdir(dir_path):
                paths.add(os.path.relpath(dir_path, directory) + os.sep)
    return paths

def main():
    print("--- Скрипт обновления запущен ---")
    
    # 1. Ожидание завершения работы основного приложения
    print("Ожидание завершения работы основного приложения (3 секунды)...")
    time.sleep(3)
    
    # 2. Определение путей
    destination_dir = os.getcwd()
    update_dir = "update_temp"
    source_dir_inner = os.path.join(update_dir, "LMArenaBridge-main")
    config_filename = 'config.jsonc'
    models_filename = 'models.json'
    model_endpoint_map_filename = 'model_endpoint_map.json'
    
    if not os.path.exists(source_dir_inner):
        print(f"Ошибка: исходная директория {source_dir_inner} не найдена. Обновление не выполнено.")
        return
        
    print(f"Исходная директория: {os.path.abspath(source_dir_inner)}")
    print(f"Целевая директория: {os.path.abspath(destination_dir)}")

    # 3. Резервное копирование ключевых файлов
    print("Создание резервной копии текущих файлов конфигурации и моделей...")
    old_config_path = os.path.join(destination_dir, config_filename)
    old_models_path = os.path.join(destination_dir, models_filename)
    old_config_values = load_jsonc_values(old_config_path)
    
    # 4. Определение файлов и папок, которые нужно сохранить
    # Сохраняем update_temp, .git и любые скрытые файлы/папки, добавленные пользователем
    preserved_items = {update_dir, ".git", ".github"}

    # 5. Получение списков новых и текущих файлов
    new_files = get_all_relative_paths(source_dir_inner)
    # Исключаем директории .git и .github, так как они не должны быть развернуты
    new_files = {f for f in new_files if not (f.startswith('.git') or f.startswith('.github'))}

    current_files = get_all_relative_paths(destination_dir)

    print("\n--- Анализ изменений файлов ---")
    print("[*] Функция удаления файлов отключена для защиты пользовательских данных. Выполняется только копирование файлов и обновление конфигурации.")

    # 7. Копирование новых файлов (кроме конфигурационных)
    print("\n[+] Копирование новых файлов...")
    try:
        new_config_template_path = os.path.join(source_dir_inner, config_filename)
        
        for item in os.listdir(source_dir_inner):
            s = os.path.join(source_dir_inner, item)
            d = os.path.join(destination_dir, item)
            
            # Пропускаем директории .git и .github
            if item in {".git", ".github"}:
                continue
            
            if os.path.basename(s) == config_filename:
                continue # Пропускаем основной файл конфигурации, он обрабатывается позже
            
            if os.path.basename(s) == model_endpoint_map_filename:
                continue # Пропускаем файл сопоставления конечных точек моделей, сохраняем пользовательскую версию

            if os.path.basename(s) == models_filename:
                continue # Пропускаем файл models.json, сохраняем пользовательскую версию

            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        print("Копирование файлов успешно завершено.")

    except Exception as e:
        print(f"Ошибка при копировании файлов: {e}")
        return

    # 8. Интеллектуальное объединение конфигурации
    if old_config_values and os.path.exists(new_config_template_path):
        print("\n[*] Выполняется интеллектуальное объединение конфигурации (с сохранением комментариев)...")
        try:
            with open(new_config_template_path, 'r', encoding='utf-8') as f:
                new_config_content = f.read()

            new_version_values = load_jsonc_values(new_config_template_path)
            new_version = new_version_values.get("version", "unknown")
            old_config_values["version"] = new_version

            for key, value in old_config_values.items():
                if isinstance(value, str):
                    replacement_value = f'"{value}"'
                elif isinstance(value, bool):
                    replacement_value = str(value).lower()
                else:
                    replacement_value = str(value)
                
                pattern = re.compile(f'("{key}"\s*:\s*)(?:".*?"|true|false|[\d\.]+)')
                if pattern.search(new_config_content):
                    new_config_content = pattern.sub(f'\\g<1>{replacement_value}', new_config_content)

            with open(old_config_path, 'w', encoding='utf-8') as f:
                f.write(new_config_content)
            print("Объединение конфигурации успешно завершено.")

        except Exception as e:
            print(f"Критическая ошибка при объединении конфигурации: {e}")
    else:
        print("Невозможно выполнить интеллектуальное объединение, будет использован новый файл конфигурации.")
        if os.path.exists(new_config_template_path):
            shutil.copy2(new_config_template_path, old_config_path)

    # 9. Очистка временной папки
    print("\n[*] Очистка временных файлов...")
    try:
        shutil.rmtree(update_dir)
        print("Очистка завершена.")
    except Exception as e:
        print(f"Ошибка при очистке временных файлов: {e}")

    # 10. Перезапуск основного приложения
    print("\n[*] Перезапуск основного приложения...")
    try:
        main_script_path = os.path.join(destination_dir, "api_server.py")
        if not os.path.exists(main_script_path):
            print(f"Ошибка: основной скрипт {main_script_path} не найден.")
            return
        
        subprocess.Popen([sys.executable, main_script_path])
        print("Основное приложение перезапущено в фоновом режиме.")
    except Exception as e:
        print(f"Не удалось перезапустить основное приложение: {e}")
        print(f"Пожалуйста, запустите {main_script_path} вручную.")

    print("--- Обновление завершено ---")

if __name__ == "__main__":
    main()