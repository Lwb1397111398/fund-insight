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


def machine_name(url):
    """自报"连的是哪台"用的名字：sqlite 给文件路径，远程给 `scheme://host/db`。

    为什么不印 `DATABASE_URL` 这五个字（第 41 轮 B-MAJOR-1）：变量名不告诉你任何事 ——
    本地 `.env` 里它指生产，Render 上它指生产，CI 里可能指测试库。
    口令一个字符都不出现：只取 `@` 后面那一段。
    """
    if not url or "://" not in url:
        return url or "(空)"
    scheme, rest = url.split("://", 1)
    where = rest.split("@")[-1].split("?", 1)[0]
    if scheme.startswith("sqlite"):
        return where
    return "%s://%s" % (scheme, where)


def production_requested(argv=None):
    return "--production" in (sys.argv[1:] if argv is None else list(argv))


_READ_TARGET = []   # [(url, is_production)]：一次进程只定一次库，第二次直接复用（别再印两行 [env]）


def resolve_read_target(argv=None):
    """**定库**（并把镜像钉进环境），但不建连接。返回 `(url, is_production)`。

    为什么单独有这个函数（第 41 轮 B-MINOR-1）：`src/services/l1_weighting.py:16` 写着
    `from src.models.database import Prediction` —— 只要顶层 import 了它，全局 `engine`
    就在那一刻按**当时的** `DATABASE_URL` 建出来，而 `.env` 里那条指生产（实测：
    设成 `postgresql://…invalid.invalid/nope` 后导入，`src.models.database.engine.url`
    就是那串远程地址）。所以"先决定连哪儿"必须排在"先 import 任何 src.*"**之前**，
    和 `tests/conftest.py` 那条规矩同源。
    """
    if _READ_TARGET:
        return _READ_TARGET[0]
    if production_requested(argv):
        url = (os.environ.get("DATABASE_URL", "") or _dotenv_database_url()).strip()
        if not url.lower().startswith(("postgres", "postgresql")):
            print("[abort] --production 要求 DATABASE_URL 指向 PostgreSQL，当前解析到的是 %s"
                  % machine_name(url))
            raise SystemExit(4)
        _READ_TARGET.append((url, True))
    else:
        _READ_TARGET.append((pin_local_sqlite(use_mirror_default=True), False))
    return _READ_TARGET[0]


def _sqlite_ro_url(url):
    """把 `sqlite:///路径` 改写成 `file:…?mode=ro&uri=true`，让**引擎**去挡写。

    为什么要自己拼：实测 `create_engine("sqlite:///C:/…/x.db", connect_args={'uri': True})`
    并没有被当成只读 URI（写照样成功），而 `sqlite:///file:…?mode=ro&uri=true` 才会
    报 `attempt to write a readonly database`。正则挡不住写，引擎挡得住。
    """
    body = url[len("sqlite:///"):] if url.lower().startswith("sqlite:///") else url
    body = body.split("?", 1)[0]
    import urllib.parse
    quoted = urllib.parse.quote(body.replace("\\", "/"), safe="/:@")
    return "sqlite:///file:%s?mode=ro&nolookup=1&uri=true" % quoted


def _write_probe(engine, ddl):
    """真试一次写，只有数据库自己说"不许写"才算只读成立（返回 None）。

    第 25/26 轮在 `scripts/q.py` 上翻过两次车的教训沿用：读一个刚设进去的会话变量是
    恒真核对，`isolation_level='READ ONLY'` 根本不是 psycopg2 方言的合法值。
    """
    with engine.connect() as conn:
        try:
            conn.exec_driver_sql(ddl)
        except Exception as exc:                      # noqa: BLE001  报错正是我们要的
            msg = str(exc).lower()
            if "readonly" in msg or "read-only" in msg or "read only" in msg:
                return None
            return '只读探针报错但不是"只读"错：%s' % str(exc)[:160]
        finally:
            conn.rollback()
        return "探针写入竟然成功 ⇒ 这条连接不是只读，拒绝继续"


def read_only_connect(argv=None):
    """只读分析脚本的统一连库口。返回 `(engine, session, label)`。

    三件事是硬的（起因：第 41 轮 B-MAJOR-1 —— `audit_l3_clear_labels.py` /
    `estimate_l3_vague_labels.py` / `backtest_l1_weighting.py` 都写着
    "`os.getenv("DATABASE_URL")` 一有值就连它"，而 `.env` 默认就是生产 Supabase；
    守卫扫描只盯"能不能写"，于是这三个**读侧**脚本从没被问过连哪儿。
    它们还会把结果写进 `docs/L3_*.md` 报告，标签只是 "DATABASE_URL" ⇒ 一份不知道出自
    哪个库的数字进了文档）：

    1) 默认连**本地镜像**（`pin_local_sqlite(use_mirror_default=True)`）。
       要读线上必须命令行显式出现 `--production`：`.env` 里躺着生产串不算"人说过要连"。
    2) 两条路都是**引擎级只读 + 真写探针**，探针不通直接 abort。
    3) 第一行自报**机器名**（`[库] …`），并且这个 label 要进报告正文。
    """
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker
    url, is_production = resolve_read_target(argv)
    if is_production:
        engine = sa.create_engine(url, pool_pre_ping=True,
                                  execution_options={"postgresql_readonly": True})
        why = _write_probe(engine, "CREATE TEMP TABLE _db_guard_probe(x int)")
        if why:
            print("[abort] %s" % why)
            raise SystemExit(4)
        label = "线上生产库 %s（引擎级只读，写探针已被数据库拒绝）" % machine_name(url)
    else:
        url = pin_local_sqlite(use_mirror_default=True)
        engine = sa.create_engine(_sqlite_ro_url(url), connect_args={"uri": True})
        why = _write_probe(engine, "CREATE TABLE _db_guard_probe(x int)")
        if why:
            print("[abort] %s" % why)
            raise SystemExit(4)
        label = ("本地镜像库 %s（引擎级只读；要分析线上数据得显式 --production，"
                 "等价命令 scripts/q.py --production）" % machine_name(url))
    print("[库] %s" % label)
    sys.stdout.flush()
    return engine, sessionmaker(bind=engine)(), label
