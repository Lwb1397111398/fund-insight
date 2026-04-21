"""
WSGI入口文件 - 用于PythonAnywhere部署
将FastAPI应用转换为WSGI应用
"""
import sys
import os

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from a2wsgi import ASGIMiddleware
from src.api.main import app

application = ASGIMiddleware(app)
