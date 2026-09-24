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

def _safe_target(url: str) -> str:
    """把连接串变成"认得出是哪台"的名字，**口令一个字符都不出现**。

    以前这里写 `url.split("@")[-1]` —— 那是 `_db_guard.machine_name()` 的第 N 份手抄，
    而且串里没有 `@` 时它把整串原样印出来（第 43 轮 A-MINOR-2）。
    src 侧不能用 scripts 下那把（`_db_guard` 与 src 是两个方向都不能 import 的关系），
    所以这里走 `verdict_evidence.target_name`，两份实现由用例逐条钉相等。
    """
    try:
        from src.services.verdict_evidence import target_name
        return target_name(url)
    except Exception:      # noqa: BLE001  报错那一行本身不许再把口令带出来
        return "(读不出目标，见 ALLOW_REMOTE_INIT_DB 那条说明)"


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
                  % _safe_target(url))
            sys.exit(2)
        init_database()
    else:
        start_server(host=args.host, port=args.port)

if __name__ == "__main__":
    main()
