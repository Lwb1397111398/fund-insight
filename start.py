#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Fund Insight - 一键启动脚本

修复说明:
    - 使用 python -m src 作为统一入口
    - 修复了模块导入问题
"""
import os
import sys
import subprocess
import socket
import webbrowser
import time
import argparse

def find_available_port(start_port=8013, max_port=8020):
    """查找可用的端口"""
    for port in range(start_port, max_port + 1):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('0.0.0.0', port))
            sock.close()
            return port
        except OSError:
            continue
    return None

def check_python():
    """检查 Python 环境"""
    try:
        version = subprocess.check_output(['python', '--version'], 
                                         stderr=subprocess.STDOUT,
                                         universal_newlines=True)
        print(f"  {version.strip()}")
        return True
    except:
        print("  [错误] 未找到 Python，请先安装 Python 3.8+")
        return False

def check_dependencies():
    """检查并安装依赖"""
    required_packages = [
        'fastapi', 'uvicorn', 'sqlalchemy', 'python-dotenv', 
        'openai', 'requests', 'aiohttp', 'beautifulsoup4', 'pydantic'
    ]
    
    try:
        subprocess.check_output(['pip', 'show', 'fastapi'], 
                               stderr=subprocess.STDOUT,
                               universal_newlines=True)
        print("  依赖已就绪")
        return True
    except:
        print("  正在安装依赖，请稍候...")
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-q'] + required_packages,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("  依赖已安装")
        return True

def init_database():
    """初始化数据库"""
    if not os.path.exists('data'):
        os.makedirs('data')
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    init_script = os.path.join(script_dir, 'init_db.py')
    
    if os.path.exists(init_script):
        subprocess.run([sys.executable, init_script],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("  数据库已就绪")

def start_server(port):
    """启动服务器"""
    print("")
    print("=" * 50)
    print(f"  访问地址：http://localhost:{port}")
    print(f"  API 文档：http://localhost:{port}/docs")
    print("")
    print("  按 Ctrl+C 停止服务")
    print("=" * 50)
    print("")
    
    # 打开浏览器（直接打开主页）
    webbrowser.open(f'http://localhost:{port}/index.html')
    
    # 启动服务器（使用新的统一入口）
    subprocess.call([sys.executable, '-m', 'src', '--port', str(port)])

def main():
    """主函数"""
    # 切换到脚本目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    
    # 将项目根目录添加到 Python 路径
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    
    print("=" * 50)
    print("  Fund Insight - 基金博主分析系统")
    print("  一键启动")
    print("=" * 50)
    print("")
    
    # 检查 Python
    print("[1/3] 检查 Python 环境...")
    if not check_python():
        input("按回车退出")
        return
    
    # 检查依赖
    print("")
    print("[2/3] 检查依赖...")
    check_dependencies()
    
    # 初始化数据库
    print("")
    print("[3/3] 初始化数据库...")
    init_database()
    
    # 查找可用端口
    print("")
    print("正在启动服务器...")
    port = find_available_port()
    
    if port is None:
        print("[错误] 无法找到可用端口")
        input("按回车退出")
        return
    
    print(f"  使用端口：{port}")
    
    # 启动服务器
    start_server(port)

if __name__ == '__main__':
    main()
