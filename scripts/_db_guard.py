# -*- coding: utf-8 -*-
"""本地镜像库连接守卫。

为什么存在：`.env` 里的 DATABASE_URL 指向**生产 Supabase**，任何直接
`import src.models.database` 的脚本默认都在连线上库；覆盖导入/批量写库这类
操作一旦误连就是线上事故。所以脚本必须先调用本模块，再 import 任何 ORM。
"""
import os
import sys

# 本机控制台默认 GBK，中文/符号（如 ✗）会直接抛 UnicodeEncodeError。
# 所有脚本统一在导入本模块时把 stdout/stderr 切成 UTF-8 + 替换模式。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "fund_insight.db")


def pin_local_sqlite(allow_env_override="LOCAL_DB_URL"):
    """把 DATABASE_URL 钉到本地 SQLite；指向非 SQLite 时直接退出。"""
    configured = os.environ.get("DATABASE_URL", "")
    override = os.environ.get(allow_env_override, "")
    if configured and not configured.lower().startswith("sqlite"):
        if not override:
            print("[abort] DATABASE_URL 指向非 SQLite（%s）。"
                  "本脚本只操作本地镜像库：设 LOCAL_DB_URL=sqlite:///<路径> 或清掉 DATABASE_URL。"
                  % configured.split("@")[-1])
            raise SystemExit(4)
        os.environ["DATABASE_URL"] = override
    elif not configured:
        os.environ["DATABASE_URL"] = "sqlite:///" + DEFAULT_DB.replace("\\", "/")
    url = os.environ["DATABASE_URL"]
    print("[env] DATABASE_URL = %s" % url)
    sys.stdout.flush()
    return url
