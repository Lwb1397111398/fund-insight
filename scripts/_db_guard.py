# -*- coding: utf-8 -*-
"""本地镜像库连接守卫。

为什么存在：`.env` 里的 DATABASE_URL 指向**生产 Supabase**，任何直接
`import src.models.database` 的脚本默认都在连线上库；覆盖导入/批量写库这类
操作一旦误连就是线上事故。所以脚本必须先调用本模块，再 import 任何 ORM。
"""
import os
import re
import sys

# 本机控制台默认 GBK，中文/符号（如 ✗）会直接抛 UnicodeEncodeError。
# 所有脚本统一在导入本模块时把 stdout/stderr 切成 UTF-8 + 替换模式。
# `line_buffering=True` 是第 43 轮 B-M4 补的：stdout 进管道（Render 日志、`> log 2>&1`、cron）
# 时是**块缓冲**，而 alembic / logging 走 stderr ⇒ "[库] 我要动谁"会排到它承诺领先的那件事后面，
# 进程被杀时那一行一个字都看不见。一句改成行缓冲，19 个自报点一起生效。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
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


def already_built_url():
    """那个**已经建好**的全局 engine 指向哪儿：`None`＝还没建，`'?'`＝模块只导了一半判不出来。

    为什么这条要紧：`src/models/database.py` **顶层**就写着 `engine = create_engine(DATABASE_URL)`，
    而 `create_engine` 不连线、只把 URL 焊进 engine 对象。于是 `import src.…` 排在钉库之前时，
    engine 已经按 `.env` 里那串生产地址建好，之后再怎么改 `os.environ` 都救不回来 ——
    第 33 轮 `tests/conftest.py` 那次"4 条夹具把数据写进 Supabase"
    与第 41 轮 `backtest_l1_weighting.py` 都是这一条。
    """
    mod = sys.modules.get('src.models.database')
    if mod is None:
        return None
    try:
        return str(mod.engine.url)
    except Exception:                                     # noqa: BLE001 判不出来就按危险处理
        return '?'


def _refuse_to_pin_a_dead_engine(who):
    """engine 已经按别的目标建好时，钉库不许"改了环境变量"当成成功。

    为什么不只在静态判据里管：AST 只能可靠地看**顶层** import 的顺序；仓库里 100 多处
    `from src.…` 写在函数体里（定义处在前、执行处却可能在后面，反之亦然），按行号比大小
    要么冤枉一片、要么干脆漏掉（第 42 轮 B-MAJOR-4）。运行期问一句 `sys.modules` 才是真顺序。

    为什么"已经建好"还分两档：`tests/conftest.py` 把全局 engine 建在一个临时 SQLite 上，
    那种情形写进去的是测试库而不是生产 —— 一律 abort 会把 pytest 会话里
    "先导入 ORM、再 in-process 跑脚本 `main()`"的用例一起打死（第一版实测：**8 条**，
    分布是 `test_audit_fund_info_identity.py` ×4 + `test_sector_seed_route_honesty.py` ×2
    + `test_snapshot_prod_mappings.py` ×2 —— 注意我上一版把这 8 条记成了
    `test_seed_owner_proxies_gate.py` 那 8 条，那是**归错文件的数**（第 43 轮 A 席抓到
    "8"是手抄的）；复核命令：把那两档合成一档后 `pytest tests/unit -q` 看失败清单），
    而那些用例测的恰恰是"脚本只写它说的那个库"。所以：
      · 已建在**非 SQLite**（或判不出来）⇒ `[abort]` 退 4：这条路上钉库救不了任何东西；
      · 已建在 SQLite ⇒ 印一条 `[警告]` 说清"接下来写的就是这个文件，改环境变量改不动它"，
        再照常往下走。今天这种情形是**静默**的。
    """
    built = already_built_url()
    if built is None:
        return
    if built.startswith('sqlite'):
        print("[警告] 全局 engine 已经建在 %s —— 现在改 DATABASE_URL **改不动它**，"
              "本脚本接下来写的就是这个文件。要写别的库，请把 %s 提到任何 src.* 导入之前。"
              % (machine_name(built), who))
        sys.stdout.flush()
        return
    target = '(判不出来：`src.models.database` 只导入了一半)' if built == '?' \
        else machine_name(built)
    print("[abort] %s 来得太晚了：`src.models.database` 已在 sys.modules 里，"
          "全局 engine 已按当时可见的目标（%s）建好。现在改环境变量**改不动那个 engine**，"
          "钉库只是看起来成功 —— 而写会落进上面那个库。"
          "要么把这一句提到任何 src.* 导入之前，要么只想读就改用 read_only_connect()。"
          % (who, target))
    raise SystemExit(4)


def pin_local_sqlite(allow_env_override="LOCAL_DB_URL", use_mirror_default=False):
    """把 DATABASE_URL 钉到本地 SQLite；指向非 SQLite 时直接退出。

    优先级：`LOCAL_DB_URL` > 进程环境 `DATABASE_URL` > `.env` 里的 `DATABASE_URL` > 默认镜像。
    """
    _refuse_to_pin_a_dead_engine("pin_local_sqlite()")
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
    if scheme.startswith("sqlite"):
        # 自报要照着**打得开的那个路径**报（第 41 轮 B-MINOR-3）。SQLAlchemy 的 sqlite URL 里
        # 前导斜杠的个数有意义：`sqlite:///data/x.db` 是**相对**路径、
        # `sqlite:////home/x.db` 才是 POSIX 绝对路径、Windows 写成 `sqlite:///E:/x.db`
        # 或 `sqlite:////E:/x.db`。所以先精确剥掉 `sqlite:///` 这三斜杠前缀，再按剩下的形状修。
        body = url[len("sqlite:///"):] if url.lower().startswith("sqlite:///") else rest
        body = body.split("?", 1)[0]
        if body.startswith("//"):
            body = body.lstrip("/")
        if re.match(r"^/[A-Za-z]:[\\/]", body):        # 四斜杠 Windows 绝对：/E:/… → E:/…
            body = body[1:]
        return body or "(当前目录里的 sqlite 文件)"
    where = rest.split("@")[-1].split("?", 1)[0]
    return "%s://%s" % (scheme, where)


def is_the_mirror(name):
    """这个 sqlite 目标**是不是**真镜像：比规范路径（同一个文件），不比后缀。

    第 43 轮 B-MINOR-1：`sqlite:///C:/backup/2026-09/data/fund_insight.db`（某次备份）
    以 `data/fund_insight.db` 结尾，用 `endswith` 会被印成和真镜像一字不差的"本地镜像库" ——
    而"拿别的库的数当系统的数"正是这个项目代价最大的那次错。
    """
    if not name or name == ':memory:':
        return False
    candidate = name.replace('\\', '/')
    if not os.path.isabs(candidate):
        candidate = os.path.join(ROOT, candidate)
    try:
        return os.path.realpath(candidate) == os.path.realpath(DEFAULT_DB)
    except OSError:
        return False


def db_kind(url):
    """连接串 → "这是哪个库"那半句话。与 `src/services/verdict_evidence.describe_url()` 同一套词。

    两份实现是没法合并的（src 不能去 import scripts，而 `_db_guard` 必须能在
    "连哪个库还没定"之前被导入 ⇒ 它也不能 import src），所以由
    `tests/unit/test_database_label_targets.py` 拿一批**样品笛卡尔积**逐条钉相等
    （第 42 轮只钉了 10 个样品，第 43 轮 B-MINOR-1 就在样品外量到分叉 ⇒ 样品要成批生成）。
    """
    name = machine_name(url)
    if url.lower().startswith('sqlite'):
        if name == ':memory:':
            return '内存 sqlite（不落盘，通常是测试夹具）'
        if is_the_mirror(name):
            return '本地镜像库（data/fund_insight.db）'
        return '本地 sqlite 文件（不是镜像库）：%s' % name
    low = url.lower()
    if low.startswith(('postgres', 'postgresql')):
        return '线上生产库（%s）' % name
    if low.startswith('mysql'):
        return 'MySQL 库（%s）' % name
    # 认不出的 scheme：两边都必须走同一条 fallback（第 43 轮笛卡尔积样品 `''`/`'://x'`
    # 一喂下去就量到分叉：src 印 `" 库（(空)）"`、守卫印 `"远端库（(空)）"`）。
    return '%s 库（%s）' % (low.split('://')[0], name)


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
        memo_url, memo_prod = _READ_TARGET[0]
        want_prod = production_requested(argv)
        if want_prod and not memo_prod:
            # 同进程里"先按镜像定过库、后面又要求读线上"：静默复用镜像 = 报告里的数
            # 来自一个没人想要的库。至少把这件事说破（今天唯一调用方是 backtest，旗子来自
            # 同一个 sys.argv，踩不到；但这条边界不写出来，下一个人就会踩）。
            print("[警告] 本进程已在开始时把目标定成本地镜像（%s），后来的 --production"
                  " 不会改判；要读线上请单独跑一次" % machine_name(memo_url))
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


def _write_probe(engine, statements):
    """真试一次写，只有数据库自己说"不许写"才算只读成立（返回 None）。

    第 25/26 轮在 `scripts/q.py` 上翻过两次车的教训沿用：读一个刚设进去的会话变量是
    恒真核对，`isolation_level='READ ONLY'` 根本不是 psycopg2 方言的合法值。

    **第 41 轮 B-MAJOR-2：探针自己不许在目标库里留下任何东西。** 上一版 sqlite 腿只发一条
    `CREATE TABLE`，而 pysqlite 对 DDL 是隐式提交的 —— 于是"只读不成立"那种情况
    （正是要报警的那种）反而是**由探针把表建进了那个库**再报错：实测跑完 `sqlite_master`
    里多出 `_db_guard_probe`。改成"建完立刻删"两条一起发：只读库在第一条就被拒（判定成立），
    可写库两条都过（判定不成立，但库里没残渣）。
    为什么不用事务回滚：实测 `with engine.begin(): CREATE TABLE …` 抛异常退出后表**仍在**
    （pysqlite 不把 DDL 纳进那个事务）；`BEGIN IMMEDIATE` 也分不出来（只读库照样成功）；
    `CREATE TEMP TABLE` 在 sqlite 上更不行（TEMP 库不受主库 `mode=ro` 约束 ⇒ 假报警）。
    """
    refused = None
    residue = []
    if isinstance(statements, str):        # 允许传一条（旧调用形状），别把字符串当序列逐字符跑
        statements = [statements]
    with engine.connect() as conn:
        for i, ddl in enumerate(statements):
            try:
                conn.exec_driver_sql(ddl)
            except Exception as exc:                  # noqa: BLE001  报错可能正是要的
                msg = str(exc).lower()
                if i == 0 and ('readonly' in msg or 'read-only' in msg or 'read only' in msg):
                    return None
                if i == 0:
                    return '只读探针报错但不是"只读"错：%s' % str(exc)[:160]
                # 第一条写得动、第二条 cleanup 失败 ⇒ 残渣留在目标库里，必须说出来
                residue.append('%s（%s）' % (ddl.split()[0:3], str(exc)[:80]))
            finally:
                conn.rollback()
            if i == 0:
                refused = '探针写入竟然成功 ⇒ 这条连接不是只读，拒绝继续'
        if residue:
            return '这条连接不是只读，且**探针自己的清理没成功 ⇒ 目标库里可能留下残渣，请人工核对**：' \
                   + '；'.join(residue)[:220]
        return refused or '探针什么都没发 ⇒ 判定不成立'


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
    2) 两条路都是**引擎级只读 + 真写探针**，探针不通直接 abort；探针自己**不在目标库里留东西**
       （sqlite 腿建完立刻删 —— 见 `_write_probe`）。
    3) **动手之前**先自报机器名（`[库] 准备以引擎级只读连 …`），这样连不上时也知道是谁；
       连接/探针过了之后再补一行"已核"。口令一个字符都不出现。
    """
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker
    url, is_production = resolve_read_target(argv)
    engine = None
    if is_production:
        print("[库] 准备以引擎级只读连 %s" % db_kind(url))
        try:
            engine = sa.create_engine(url, pool_pre_ping=True,
                                      execution_options={"postgresql_readonly": True})
            why = _write_probe(engine, ["CREATE TEMP TABLE _db_guard_probe(x int)",
                                        "DROP TABLE _db_guard_probe"])
        except Exception as exc:                      # noqa: BLE001  连不上也要说清是谁
            print("[abort] 连不上 %s：%s" % (db_kind(url), str(exc)[:160]))
            raise SystemExit(4)
        if why:
            print("[abort] %s" % why)
            raise SystemExit(4)
        label = "%s（引擎级只读，写探针已被数据库拒绝）" % db_kind(url)
    else:
        # 不再调第二次 `pin_local_sqlite`（第 41 轮 B-MINOR-1：上一版这里再 pin 一次，
        # 与 `resolve_read_target` 的 memo 注释"别再印两行 [env]"自相矛盾，实测 stdout 真有两行）
        #
        # 第 43 轮 B-MAJOR-3：这一句以前无条件印"本地镜像库 <路径>" —— 于是拿
        # `LOCAL_DB_URL=一份 8 月备份` 跑分析时，屏幕上、以及被 `estimate_l3_vague_labels.py`
        # 原样写进 `docs/L3_VAGUE_LABEL_ESTIMATE.md` 的"数据源"那一行，都写着"本地镜像库"。
        # 类别词换成 `db_kind()` 之后，副本/夹具会说自己是副本/夹具；`LOCAL_DB_URL` 仍然有效，
        # 只是不再被叫错名字。
        print("[库] 准备以引擎级只读连 %s" % db_kind(url))
        try:
            engine = sa.create_engine(_sqlite_ro_url(url), connect_args={"uri": True})
            why = _write_probe(engine, ["CREATE TABLE _db_guard_probe(x int)",
                                        "DROP TABLE _db_guard_probe"])
        except Exception as exc:                      # noqa: BLE001  连不上也要说清是谁
            print("[abort] 连不上 %s：%s" % (db_kind(url), str(exc)[:160]))
            raise SystemExit(4)
        if why:
            print("[abort] %s" % why)
            raise SystemExit(4)
        label = ("%s（引擎级只读；要分析线上数据得显式 --production，"
                 "等价命令 scripts/q.py --production）" % db_kind(url))
    print("[库] %s" % label)
    sys.stdout.flush()
    return engine, sessionmaker(bind=engine)(), label
