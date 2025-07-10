import requests
import json
import re

def send_test_request():
    """
    读取请求文件，提取 JSON 内容，并将其发送到本地测试服务器。
    """
    try:
        # 读取包含请求数据的文件
        with open('接收到的内容', 'r', encoding='utf-8') as f:
            full_content = f.read()

        # 使用正则表达式提取被日志包围的 JSON 内容
        # re.DOTALL 使得 '.' 可以匹配包括换行在内的任意字符
        match = re.search(r'---\s*接收到 OpenAI 格式的请求体\s*---\n(.*?)\n------------------------------------', full_content, re.DOTALL)
        
        if not match:
            print("❌ 错误: 在 '接收到的内容' 文件中未能找到有效的 JSON 请求体。")
            print("请确保文件内容格式正确，包含 '--- 接收到 OpenAI 格式的请求体 ---' 和 '------------------------------------' 分隔符。")
            return

        json_str = match.group(1).strip()

        try:
            # 解析提取出的字符串为 JSON 对象
            request_data = json.loads(json_str)
            print("✅ 成功从文件解析 JSON 请求体。")
        except json.JSONDecodeError as e:
            print(f"❌ 错误: 解析 JSON 失败: {e}")
            return

        # 定义服务器地址
        url = "http://127.0.0.1:5102/v1/chat/completions"
        headers = {"Content-Type": "application/json"}

        print(f"🚀 正在向 {url} 发送 POST 请求...")

        # 发送请求
        # stream=True 用于接收流式响应
        response = requests.post(url, headers=headers, json=request_data, stream=True)

        # 检查响应
        print(f"📡 服务器响应状态码: {response.status_code}")
        
        if response.status_code == 200:
            print("\n--- 接收到服务器的流式响应 ---")
            for chunk in response.iter_lines():
                if chunk:
                    # 将字节解码为字符串并打印
                    print(chunk.decode('utf-8'))
            print("---------------------------------\n")
            print("✅ 流式响应接收完毕。")
        else:
            print("\n--- 服务器返回错误 ---")
            print(response.text)
            print("-----------------------\n")

    except FileNotFoundError:
        print("❌ 错误: '接收到的内容' 文件未找到。请确保该文件与脚本在同一目录下。")
    except requests.exceptions.RequestException as e:
        print(f"❌ 错误: 请求失败: {e}")

if __name__ == '__main__':
    send_test_request()