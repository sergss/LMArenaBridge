# update_script.py
import os
import shutil
import time
import subprocess
import sys
import json
import re

def load_jsonc(path):
    """从一个 .jsonc 文件中加载数据，忽略注释。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        # 移除注释
        content = re.sub(r'//.*', '', content)
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
        print(f"加载或解析 {path} 时出错: {e}")
        return None

def save_jsonc(path, data):
    """将数据以格式化的 JSON 形式保存到文件。"""
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"保存到 {path} 时出错: {e}")
        return False

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

    # 3. 备份旧的用户配置
    print("正在备份当前配置...")
    old_config_path = os.path.join(destination_dir, config_filename)
    old_config = load_jsonc(old_config_path)
    if old_config:
        print("备份成功。")
    else:
        print("警告：无法加载当前配置，用户的设置可能不会被保留。")

    # 4. 复制新文件
    print("正在复制新文件...")
    try:
        for item in os.listdir(source_dir_inner):
            s = os.path.join(source_dir_inner, item)
            d = os.path.join(destination_dir, item)
            if os.path.isdir(s):
                if os.path.exists(d):
                    shutil.rmtree(d)
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                # 不要直接复制配置文件，我们稍后会处理它
                if os.path.basename(s) != config_filename:
                    shutil.copy2(s, d)
        
        # 单独复制新的配置文件以供合并
        shutil.copy2(os.path.join(source_dir_inner, config_filename), os.path.join(destination_dir, config_filename))
        print("文件复制成功。")

    except Exception as e:
        print(f"文件复制过程中发生错误: {e}")
        return

    # 5. 智能合并配置
    if old_config:
        print("正在智能合并配置...")
        new_config_path = os.path.join(destination_dir, config_filename)
        new_config = load_jsonc(new_config_path)

        if new_config:
            # 保留旧配置的值，同时接受新配置的键
            merged_config = new_config.copy()
            for key, value in old_config.items():
                if key in merged_config and key != "version":
                    merged_config[key] = value
            
            # 确保版本号始终是新的
            merged_config["version"] = new_config.get("version", old_config.get("version"))

            if save_jsonc(new_config_path, merged_config):
                print("配置合并成功，用户设置已保留。")
            else:
                print("错误：无法写回合并后的配置。")
        else:
            print("错误：无法加载新配置，跳过合并步骤。")


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
        subprocess.Popen([sys.executable, "local_openai_history_server.py"])
        print("主程序已在后台重新启动。")
    except Exception as e:
        print(f"重启主程序失败: {e}")
        print("请手动运行 local_openai_history_server.py")

    print("--- 更新完成 ---")

if __name__ == "__main__":
    main()