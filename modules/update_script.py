# update_script.py
import os
import shutil
import time
import subprocess
import sys
import json
import re

def load_jsonc_values(path):
    """从一个 .jsonc 文件中加载数据，忽略注释，只返回键值对。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        content = re.sub(r'//.*', '', content)
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
        print(f"加载或解析 {path} 的值时出错: {e}")
        return None

def main():
    print("--- 更新脚本已启动 ---")
    
    # 1. 等待主程序退出
    print("等待主程序关闭 (3秒)...")
    time.sleep(3)
    
    # 2. 定义路径
    source_dir_inner = os.path.join("update_temp", "LMArenaBridge-main")
    destination_dir = os.getcwd()
    config_filename = 'config.jsonc'
    
    if not os.path.exists(source_dir_inner):
        print(f"错误：找不到源目录 {source_dir_inner}。更新失败。")
        return
        
    print(f"源目录: {os.path.abspath(source_dir_inner)}")
    print(f"目标目录: {os.path.abspath(destination_dir)}")

    # 3. 备份旧的用户配置值
    print("正在备份当前配置值...")
    old_config_path = os.path.join(destination_dir, config_filename)
    old_config_values = load_jsonc_values(old_config_path)
    if old_config_values:
        print("配置值备份成功。")
    else:
        print("警告：无法加载当前配置值，用户的设置可能不会被保留。")

    # 4. 复制新文件（除配置文件外）
    print("正在复制新文件...")
    try:
        new_config_template_path = os.path.join(source_dir_inner, config_filename)
        
        for item in os.listdir(source_dir_inner):
            s = os.path.join(source_dir_inner, item)
            d = os.path.join(destination_dir, item)
            if os.path.basename(s) == config_filename:
                continue # 跳过配置文件
            if os.path.isdir(s):
                if os.path.exists(d):
                    shutil.rmtree(d)
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        print("文件复制成功。")

    except Exception as e:
        print(f"文件复制过程中发生错误: {e}")
        return

    # 5. 智能合并配置（基于文本）
    if old_config_values and os.path.exists(new_config_template_path):
        print("正在智能合并配置（保留注释）...")
        try:
            with open(new_config_template_path, 'r', encoding='utf-8') as f:
                new_config_content = f.read()

            # 获取新版本号
            new_version_values = load_jsonc_values(new_config_template_path)
            new_version = new_version_values.get("version", "unknown")

            # 始终使用新版本号
            old_config_values["version"] = new_version

            # 基于新模板，替换旧的值
            for key, value in old_config_values.items():
                # 构建正则表达式来查找键值对并替换值
                # 这个表达式会寻找 "key": value, 的形式，并处理字符串、布尔值和数字
                if isinstance(value, str):
                    replacement_value = f'"{value}"'
                elif isinstance(value, bool):
                    replacement_value = str(value).lower()
                else: # numbers
                    replacement_value = str(value)
                
                # 正则表达式：匹配 "key" 后面的冒号，然后是任意空白，最后是值
                # (?<=")key(?="\s*:) 匹配键
                # :\s* 匹配冒号和任意空格
                # (?:".*?"|true|false|[\d\.]+) 匹配值
                pattern = re.compile(f'("{key}"\s*:\s*)(?:".*?"|true|false|[\d\.]+)')
                new_config_content = pattern.sub(f'\\g<1>{replacement_value}', new_config_content)

            with open(old_config_path, 'w', encoding='utf-8') as f:
                f.write(new_config_content)
            print("配置合并成功，注释和格式已保留。")

        except Exception as e:
            print(f"配置合并过程中发生严重错误: {e}")
    else:
         # 如果无法进行智能合并，就直接复制新文件
        print("无法进行智能合并，将直接使用新版配置文件。")
        shutil.copy2(new_config_template_path, old_config_path)


    # 6. 清理临时文件夹
    print("正在清理临时文件...")
    try:
        shutil.rmtree("update_temp")
        print("清理完毕。")
    except Exception as e:
        print(f"清理临时文件时发生错误: {e}")

    # 7. 重启主程序
    print("正在重启主程序...")
    try:
        # 使用 sys.executable 确保我们用的是同一个 Python 解释器
        main_script_path = os.path.join(destination_dir, "local_openai_history_server.py")
        subprocess.Popen([sys.executable, main_script_path])
        print("主程序已在后台重新启动。")
    except Exception as e:
        print(f"重启主程序失败: {e}")
        print("请手动运行 local_openai_history_server.py")

    print("--- 更新完成 ---")

if __name__ == "__main__":
    main()