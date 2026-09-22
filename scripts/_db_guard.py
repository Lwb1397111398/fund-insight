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


def _dotenv_database_url():
    """从仓库根 `.env` 里读 DATABASE_URL（不依赖 dotenv，也不打日志）。

    为什么要读文件：`src.core.config` 是在**被 import 时**才把 `.env` 灌进进程环境，
    而守卫跑在任何 import 之前 ⇒ 只看 `os.environ` 会以为"没配远程库"，
    于是即使设了 LOCAL_DB_URL 也走到"用默认镜像"的分支（第 22 轮评审的探针
    就是这样把写操作落进了 data/fund_insight.db）。
    """
    env = os.path.join(ROOT, ".env")
    try:
        with open(env, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line.startswith("DATABASE_URL=") or line.startswith("DATABASE_URL ="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def pin_local_sqlite(allow_env_override="LOCAL_DB_URL", use_mirror_default=False):
    """把 DATABASE_URL 钉到本地 SQLite；指向非 SQLite 时直接退出。

    优先级：`LOCAL_DB_URL` > 进程环境 `DATABASE_URL` > `.env` 里的 `DATABASE_URL` > 默认镜像。
    """
    configured = os.environ.get("DATABASE_URL", "") or _dotenv_database_url()
    override = os.environ.get(allow_env_override, "")
    if override:
        # 逃生口**无条件生效、并且先校验**：以前只有 `configured` 是远程库时才看 override，
        # 而 `.env` 里的远程 URL 在这一步还没进进程环境 ⇒ 设了 LOCAL_DB_URL 也被静默忽略，
        # 于是"只操作本地镜像库"的脚本把写操作落进了 data/fund_insight.db（第 22 轮实测）。
        if "://" in override and not override.lower().startswith("sqlite"):
            # 报错信息教操作者设的那玩意，自己也不能是 postgres://
            print("[abort] %s 也必须指向 SQLite（%s）：本脚本只操作本地镜像库"
                  % (allow_env_override, override.split("@")[-1]))
            raise SystemExit(4)
        if "://" not in override:
            # 允许只给一个文件路径，统一转成 sqlite URL
            override = "sqlite:///" + os.path.abspath(override).replace("\\", "/")
        os.environ["DATABASE_URL"] = override
    elif configured and not configured.lower().startswith("sqlite"):
        if use_mirror_default:
            # 脚本自己声明"我要的就是本地镜像"（这些是文档推荐的本地运维命令，
            # 不该要求操作者去设环境变量）。显式传参 = 可 grep、可审；
            # 而一次性探针/别人写的脚本走到这里仍然会 abort —— 那正是要拦的对象。
            os.environ["DATABASE_URL"] = "sqlite:///" + DEFAULT_DB.replace("\\", "/")
            print("[env] 脚本显式声明使用本地镜像（.env 指向远程库，已忽略）：%s"
                  % os.environ["DATABASE_URL"])
        else:
            print("[abort] DATABASE_URL 指向非 SQLite（%s）。"
                  "本脚本只操作本地镜像库：设 LOCAL_DB_URL=sqlite:///<路径>，"
                  "或在脚本里显式 pin_local_sqlite(use_mirror_default=True)。"
                  % configured.split("@")[-1])
            raise SystemExit(4)
    elif configured:
        # `.env` 里本来就是 SQLite：把值写回进程环境，否则下面按 key 取会 KeyError
        # （`os.environ.get` 才是我上一轮改成"看得见 .env"之后该有的写法）
        os.environ["DATABASE_URL"] = configured
    else:
        os.environ["DATABASE_URL"] = "sqlite:///" + DEFAULT_DB.replace("\\", "/")
    url = os.environ.get("DATABASE_URL", "")
    # 兜底断言（不是 assert：`python -O` 会把 assert 整条剥掉）
    if not url.lower().startswith("sqlite"):
        print("[abort] 最终 DATABASE_URL 仍非 SQLite（%s）" % url.split("@")[-1])
        raise SystemExit(4)
    print("[env] DATABASE_URL = %s" % url)
    sys.stdout.flush()
    return url
