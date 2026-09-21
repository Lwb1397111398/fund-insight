#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Fund Insight - 统一入口模块

使用方法:
    python -m src              # 启动服务器
    python -m src --port 8000  # 指定端口启动
    python -m src --init-db    # 初始化数据库
"""
import sys
import os
import argparse

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

def init_database():
    """初始化数据库"""
    print("[初始化] 正在初始化数据库...")
    from src.models.database import init_db
    init_db()
    print("[初始化] 数据库初始化完成")

def start_server(host="0.0.0.0", port=None):
    """启动服务器"""
    import uvicorn
    from src.core.config import config
    
    if port is None:
        port = config.SERVER_PORT
    
    uvicorn.run("src.api.main:app", host=host, port=port, reload=False)

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="Fund Insight - 基金博主分析系统")
    parser.add_argument("--port", type=int, help="服务器端口")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="服务器主机")
    parser.add_argument("--init-db", action="store_true", help="初始化数据库")
    
    args = parser.parse_args()
    
    data_dir = os.path.join(project_root, "data")
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
    
    if args.init_db:
        # `.env` 的 DATABASE_URL 指向**生产 Supabase**，而 `python -m src --init-db`
        # 是文档里推荐的本机验证命令 —— 误跑一次就是在远端库上建表/补列。
        # 非 SQLite 必须显式授权才执行初始化（AGENTS.md 的"启动级检查"因此只能在本地跑）。
        url = os.getenv("DATABASE_URL", "")
        if url and not url.lower().startswith("sqlite") \
                and os.getenv("ALLOW_REMOTE_INIT_DB") != "1":
            print("[abort] DATABASE_URL 指向非 SQLite（%s）：--init-db 只允许本地镜像库。"
                  "确实要在远端库上初始化，设 ALLOW_REMOTE_INIT_DB=1 再跑一次。"
                  % url.split("@")[-1])
            sys.exit(2)
        init_database()
    else:
        start_server(host=args.host, port=args.port)

if __name__ == "__main__":
    main()
