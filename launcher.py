#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Fund Insight 启动器
打包后的入口文件，负责启动后端服务和打开浏览器
"""
import os
import sys
import time
import webbrowser
import threading
from pathlib import Path

# 获取打包后的资源路径
def get_resource_path(relative_path):
    """获取资源文件的绝对路径（支持打包后的环境）"""
    if hasattr(sys, '_MEIPASS'):
        # PyInstaller 打包后的临时目录
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

# 设置工作目录
if hasattr(sys, '_MEIPASS'):
    os.chdir(sys._MEIPASS)

# 添加项目路径
project_root = get_resource_path('.')
if project_root not in sys.path:
    sys.path.insert(0, project_root)

def start_server():
    """启动服务器"""
    try:
        from src.__main__ import start_server
        print("[启动器] 正在启动 Fund Insight 服务器...")
        start_server(host="127.0.0.1", port=8014)
    except Exception as e:
        print(f"[启动器] 启动服务器失败: {e}")
        import traceback
        traceback.print_exc()

def open_browser():
    """打开浏览器"""
    time.sleep(3)  # 等待服务器启动
    url = "http://localhost:8014"
    print(f"[启动器] 正在打开浏览器: {url}")
    webbrowser.open(url)

def main():
    """主函数"""
    print("=" * 60)
    print("Fund Insight - 基金博主分析系统")
    print("=" * 60)
    print()
    
    # 确保数据目录存在
    data_dir = get_resource_path('data')
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
        print(f"[启动器] 创建数据目录: {data_dir}")
    
    # 在新线程中打开浏览器
    browser_thread = threading.Thread(target=open_browser)
    browser_thread.daemon = True
    browser_thread.start()
    
    # 启动服务器（主线程）
    start_server()

if __name__ == "__main__":
    main()
