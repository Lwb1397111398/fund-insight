#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Fund Insight 打包脚本
使用 PyInstaller 打包成独立可执行文件
"""
import PyInstaller.__main__
import os
import sys

# 项目根目录
project_root = os.path.dirname(os.path.abspath(__file__))

# PyInstaller 参数
args = [
    'launcher.py',  # 入口文件
    '--name=FundInsight',  # 应用名称
    '--onefile',  # 打包成单个文件
    '--windowed',  # Windows 下不显示控制台窗口
    '--icon=NONE',  # 可以添加图标文件路径
    '--add-data=web;web',  # 添加前端文件
    '--add-data=data;data',  # 添加数据目录
    '--add-data=.env;.env',  # 添加环境变量文件
    '--hidden-import=src.api.main',
    '--hidden-import=src.models.database',
    '--hidden-import=src.core.config',
    '--hidden-import=src.analyzer.llm_analyzer',
    '--hidden-import=src.fund.fund_api',
    '--hidden-import=src.crawler.ai_analyzer',
    '--hidden-import=uvicorn',
    '--hidden-import=fastapi',
    '--hidden-import=sqlalchemy',
    '--hidden-import=requests',
    '--hidden-import=apscheduler',
    '--hidden-import=pydantic',
    '--hidden-import=python-dotenv',
    '--clean',  # 清理临时文件
    '--noconfirm',  # 不确认覆盖
    f'--distpath={os.path.join(project_root, "dist")}',  # 输出目录
    f'--workpath={os.path.join(project_root, "build")}',  # 工作目录
]

print("=" * 60)
print("开始打包 Fund Insight...")
print("=" * 60)
print(f"项目目录: {project_root}")
print(f"输出目录: {os.path.join(project_root, 'dist')}")
print()

# 执行打包
PyInstaller.__main__.run(args)

print()
print("=" * 60)
print("打包完成!")
print(f"可执行文件位于: {os.path.join(project_root, 'dist', 'FundInsight.exe')}")
print("=" * 60)
