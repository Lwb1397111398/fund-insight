# -*- coding: utf-8 -*-
"""本地镜像库连接守卫。

为什么存在：`.env` 里的 DATABASE_URL 指向**生产 Supabase**，任何直接
`import src.models.database` 的脚本默认都在连线上库；覆盖导入/批量写库这类
操作一旦误连就是线上事故。所以脚本必须先调用本模块，再 import 任何 ORM。
"""
import ipaddress
import os
import re
import sys

# 本机控制台默认 GBK，中文/符号（如 ✗）会直接抛 UnicodeEncodeError。
# 所有脚本统一在导入本模块时把 stdout/stderr 切成 UTF-8 + 替换模式。
# `line_buffering=True` 是第 43 轮 B-M4 补的：stdout 进管道（Render 日志、`> log 2>&1`、cron）
# 时是**块缓冲**，而 alembic / logging 走 stderr ⇒ "[库] 我要动谁"会排到它承诺领先的那件事后面，
# 进程被杀时那一行一个字都看不见。一句改成行缓冲，各脚本的自报点一起生效
# （第 45 轮 A-m5：这里曾写死"19 个自报点"——没有任何命令印得出这个数，
# 加一个自报行它就过时，属于"会漂移的数不留文字版"那一族）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "fund_insight.db")
# 本项目那台线上库的**注册域**（不是子串！见 `_is_the_production_host`）。
# 两份实现共用同一张表：`src/services/verdict_evidence._PROD_DB_DOMAINS` 必须逐字相等，
# 由 `tests/unit/test_database_label_targets.py` 钉住。
_PROD_DB_DOMAINS = ("supabase.com", "supabase.co", "supabase.in")


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


def _url_of(obj):
    """从一个**真的是一条连接**的对象问出它连的目标：Engine / Connection / Session /
    sessionmaker / scoped_session。

    三遍学费，都是这个文件自己写出来又被打脸的：
    ① 按"有没有 url 属性"猜 ⇒ `sqlalchemy.engine` 是个**叫 engine 的子模块**，
       每个导入过 sqlalchemy 的进程都被判成"绑在远程"（12 条 in-process 用例一起红）；
    ② 改成"类型属于 sqlalchemy"仍然不够 ⇒ `_FunctionGenerator`（`func.url(...)` 那类东西）
       与 `CrawlerArticleRecord.url` 这样的**模型列**都能 `str()` 出一串不像连接串的东西，
       于是扫描把 ORM 的列当成了"一条远程连接"（第 45 轮由我自己的两条对照抓红）；
    ③ 现在 **isinstance 认具体的类**，并且结果必须长得像连接串（含 `://` 或 `=`）才算数。

    边界照旧要说清：调用方自己包装的连接（不是这几个类）与**函数局部变量**里的引用，
    这一道照不到 —— 那一半仍然只能靠"把钉库提到所有 src.* 导入之前"。
    """
    try:
        import sqlalchemy as sa
        from sqlalchemy.orm import Session, scoped_session, sessionmaker
        kinds = (sa.engine.Engine, sa.engine.Connection, Session, sessionmaker, scoped_session)
    except Exception:                                       # noqa: BLE001  没装 sqlalchemy 就谈不上
        return None
    if not isinstance(obj, kinds):
        return None
    candidates = []
    for pick in (lambda: str(obj.url),                                   # Engine
                 lambda: str(obj.engine.url),                            # Connection
                 lambda: str(obj.get_bind().url),                        # Session / scoped_session
                 lambda: str(obj.kw['bind'].url),                        # sessionmaker(bind=…)
                 lambda: str(obj.registry.kw['bind'].url)):              # scoped_session 的工厂
        try:
            candidates.append(pick())
        except Exception:                                     # noqa: BLE001 形状不对就换下一个
            continue
    for url in candidates:
        if url and ('://' in url or '=' in url):
            return url
    return None


def _stray_remote_engines():
    """进程里**别的模块**已经握着非 SQLite 的 engine/SessionLocal —— 钉库救不了它们。

    第 44 轮 B-MAJOR-6：`already_built_url()` 只问 `src.models.database` 那一个模块，
    于是 `del sys.modules['src.models.database']` 再重新 import（或任何把那条标记抹掉的写法）
    就能让钉库"看起来成功"，而已经 `from src.models.database import engine` 的那些调用方
    仍绑在生产串上。所以钉库之前把本仓库模块里的**每一个值**都问一遍"像不像一条连接"。

    三处刻意的克制（每一条都是被真实故障教出来的）：
    ① 只读 `vars(模块)` 里**已经存在**的名字，不用 `getattr` 逐个试 ——
       `sqlalchemy`/`typing` 这类包实现了惰性 `__getattr__`，去问一个不存在的属性会**触发导入**；
    ② 只认 `type(x) is ModuleType` 的**真模块**，并且只看"这个仓库里的模块"（`src.*` 或
       `__file__` 落在仓库内）—— Windows 上 `ctypes` 会把 `kernel32.dll` 这类对象塞进
       `sys.modules`，对它们取 `vars()` 会直接抛 `ffi.error: symbol ... not found`
       （第一版就是这么把 12 条 in-process 用例打崩的）；
    ③ 顺一层已经是模块的属性（`src.models` 上挂着的 `database`），
       因为 `del sys.modules[...]` 抹不掉那条引用。

    看得见与看不见的边界要写清楚（别把这句念成"任何引用都跑不掉"）：
    函数**局部变量**里的 engine 引用 AST 与 `sys.modules` 都照不到 —— 那一半仍然只能靠
    "把钉库提到所有 src.* 导入之前"这条规矩。
    """
    import types
    out = []
    seen = set()
    root_norm = os.path.normcase(os.path.realpath(ROOT)) + os.sep

    def _ours(name, mod):
        if name.startswith('src') or name == '__main__':
            # `__main__` 也算：一次性探针就是它（第 45 轮 B-M-3 的 H 样品：探针脚本自己
            # 的 `__file__` 在仓库外，以前整族跳过 ⇒ "钉库前先看谁还连着"对它不成立）
            return True
        try:
            file_ = getattr(mod, '__file__', '') or ''
            return bool(file_) and os.path.normcase(os.path.realpath(file_)).startswith(root_norm)
        except Exception:                                     # noqa: BLE001 问不动就当不是我们的
            return False

    def _one(obj, label):
        url = _url_of(obj)
        if url and not url.lower().startswith('sqlite'):
            out.append((label, url))

    def _ask(mod, label):
        if id(mod) in seen:
            return
        seen.add(id(mod))
        try:
            values = list(vars(mod).values())
            children = [v for v in values if type(v) is types.ModuleType]
        except Exception as exc:                              # noqa: BLE001 怪对象不许弄坏守卫
            print("[警告] 探测散落连接时读不了模块 `%s`（%s）⇒ 这一道没跑完"
                  % (label, str(exc)[:80]))
            return
        # **不按属性名筛**（第 45 轮 B-M-3 / A-m1：上一版只认 `engine`/`SessionLocal`/
        # `async_engine` 三个名字 —— 改名 `_engine`、`DB_ENGINE`，或塞进 dict / 类属性就隐身，
        # 而本仓 `src/api/main.py:391` 那种命名风格就在旁边）。认的是"这个值像不像一条连接"：
        # `_url_of` 先按类型过滤（`type(obj).__module__` 以 sqlalchemy 开头），所以既不会
        # 误伤 `sqlalchemy.engine` 那个**叫 engine 的子模块**，也不需要知道它叫什么。
        for obj in values:
            _one(obj, label)
            if isinstance(obj, dict):
                for value in list(obj.values())[:200]:
                    _one(value, label + '（dict 里的值）')
            elif isinstance(obj, (list, tuple, set)) and len(obj) <= 200:
                for value in obj:
                    _one(value, label + '（容器里的元素）')
            elif isinstance(obj, type):
                for value in list(vars(obj).values())[:200]:
                    _one(value, label + '（类属性）')
        for child in children:
            child_name = getattr(child, '__name__', '')
            if child_name == 'src.models.database' and \
                    sys.modules.get(child_name) is child:
                # 只有"确实还挂在那个键下"才交给 `already_built_url()` 去报（同一个对象报两遍
                # 是噪音）。`del sys.modules['src.models.database']` 之后，父包属性上那个模块
                # 对象**还在**、engine 也还绑着生产串，而 `already_built_url()` 已经看不见它了
                # —— 那正是第 44 轮 B-MAJOR-6 要堵的绕法，按名字一律跳过等于给它留了门。
                continue
            _ask(child, child_name or '?')

    for name, mod in list(sys.modules.items()):
        if name == 'src.models.database' or type(mod) is not types.ModuleType:
            continue                      # 那一个由 `already_built_url()` 负责，别报两遍
        if not _ours(name, mod):
            continue
        _ask(mod, name)
    return out


def _refuse_when_a_stray_engine_is_remote(who):
    try:
        strays = _stray_remote_engines()
    except Exception as exc:                                  # noqa: BLE001 扫描坏了要看得见，别弄死脚本
        print("[警告] 探测散落连接这一步没跑成（%s）⇒ 只核对了全局 engine 那一条。"
              % str(exc)[:100])
        strays = []
    for name, url in strays:
        print("[abort] %s 来得太晚了：模块 `%s` 里已经有一个连接绑在 %s（不是 SQLite）。"
              "钉库只改 `DATABASE_URL` 这个**变量**，改不动那个已经建好的对象 —— "
              "凡是从它取出的会话照样写线上。请把钉库提到所有 src.* 导入之前，"
              "或只想读就改用 read_only_connect()。"
              % (who, name, machine_name(url)))
        sys.stdout.flush()
        raise SystemExit(4)


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
    _refuse_when_a_stray_engine_is_remote("pin_local_sqlite()")
    configured = os.environ.get("DATABASE_URL", "") or _dotenv_database_url()
    override = os.environ.get(allow_env_override, "")
    if override:
        # 逃生口**无条件生效、并且先校验**：以前只有 `configured` 是远程库时才看 override，
        # 而 `.env` 里的远程 URL 在这一步还没进进程环境 ⇒ 设了 LOCAL_DB_URL 也被静默忽略，
        # 于是"只操作本地镜像库"的脚本把写操作落进了 data/fund_insight.db（第 22 轮实测）。
        if "://" in override and not override.lower().startswith("sqlite"):
            # 报错信息教操作者设的那玩意，自己也不能是 postgres://
            print("[abort] %s 也必须指向 SQLite（%s）：本脚本只操作本地镜像库"
                  % (allow_env_override, machine_name(override)))
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
                  % machine_name(configured))
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
        print("[abort] 最终 DATABASE_URL 仍非 SQLite（%s）" % machine_name(url))
        raise SystemExit(4)
    print("[env] DATABASE_URL = %s" % url)
    sys.stdout.flush()
    return url


_DSN_KEEP_KEYS = ('host', 'hosts', 'port', 'dbname', 'database')
# 一个键值对：值可以是 `'带空格的'`、`"带空格的"` 或裸词。分隔符按 libpq 与 URI 两种写法都认
# （`&` 与 `,` 以前不切，`host=h&password=X` 整串被当成**一个**值原样印出来 —— 第 45 轮 A-m2）。
_DSN_PAIR = re.compile(r"(?P<k>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
                       r"(?:'(?P<q1>[^']*)'|\"(?P<q2>[^\"]*)\"|(?P<p>[^\s'\",;=&]*))")
_DSN_SECRET_KEYS = ('password', 'passwd', 'pwd', 'secret')


def _conninfo_target(text):
    """**没有** `://` 的连接串（libpq/pgbouncer 的 `key=value` 写法）→ 只留不涉密的键。

    第 44 轮 A-m6：旧写法"认不出 scheme 就原样返回"，于是
    `host=db.example.com user=u password=真口令 dbname=proddb` 会被自报行整条印进
    stdout / Render 日志 / `docs/` 报告 —— 报库名的机制自己成了凭据泄露面。
    与 `src/services/verdict_evidence._conninfo_target()` 是同一件事的两份实现，
    两者逐条相等由 `tests/unit/test_database_label_targets.py` 钉住。
    """
    if '=' not in text:
        # 认不出键值写法时按"@ 之后"处理：宁可少说，不可把凭据多说出去。
        return text.split('@')[-1] or '(空)'
    # 引号不配对 ⇒ 后面的键值切分不可信（第 45 轮 B-m1 的复现形状：
    # `password='two words dbname=LEAKED'` 会被 naive 的按空格切分当成两个键，
    # 于是口令的第二个词以 `dbname=` 的名义印出来）。这种情况下只留 host/port。
    unbalanced = (text.count("'") % 2) or (text.count('"') % 2)
    kept, secret_seen = [], False
    for m in _DSN_PAIR.finditer(text):
        key = (m.group('k') or '').strip().lower()
        value = next((g for g in (m.group('q1'), m.group('q2'), m.group('p'))
                      if g is not None), '').strip()
        if key in _DSN_SECRET_KEYS:
            secret_seen = True
            continue
        if key in _DSN_KEEP_KEYS and value and not (unbalanced and key not in ('host', 'port')):
            kept.append('%s=%s' % (key, value))
    out = ' '.join(kept)
    if unbalanced and secret_seen:
        out += '（引号不配对，其余字段已隐去）'
    return out or '(DSN：只留下非凭据字段，其余已隐去)'


def machine_name(url):
    """自报"连的是哪台"用的名字：sqlite 给文件路径，远程给 `scheme://host/db`。

    为什么不印 `DATABASE_URL` 这五个字（第 41 轮 B-MAJOR-1）：变量名不告诉你任何事 ——
    本地 `.env` 里它指生产，Render 上它指生产，CI 里可能指测试库。
    口令一个字符都不出现：只取 `@` 后面那一段。
    """
    if not url:
        return "(空)"
    if "://" not in url:
        return _conninfo_target(url)
    scheme, rest = url.split("://", 1)
    if scheme.lower().startswith("sqlite"):
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
    # 剪掉 query / fragment / `;` 参数：`postgresql://h/db?password=X` 里最涉密的那一段
    # 不能跟着"这是哪个库"进日志（第 45 轮 A-m2 / B-m1）
    where = re.split(r"[?;&#]", rest.split("@")[-1], 1)[0]
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


def _db_host(url):
    """连接串里的**主机名**（小写、去端口、口令一个字符都不取）—— 判"这台是不是本机"用。"""
    if "://" in url:
        rest = url.split("://", 1)[1]
        where = re.split(r"[?;&#]", rest.split("@")[-1], 1)[0]
        netloc = where.split("/")[0]
        if netloc.startswith("["):
            # 带方括号的 IPv6 字面量（`postgresql://u@[::1]:5432/db`）：端口在 `]` **之后**，
            # 按 `:` 切会把主机切成一个 `[`（第 45 轮 B-m5 的对照组当场照出来）
            end = netloc.find("]")
            return netloc[:end + 1].lower() if end > 0 else netloc.lower()
        return netloc.split(":")[0].lower()
    m = re.search(r"(?:^|[\s;,])hosts?\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|([^\s'\",;=&]*))",
                  url, re.I)
    if m:
        return (m.group(1) or m.group(2) or m.group(3) or '').lower()
    return ""


def _is_a_local_host(host):
    """空主机 / localhost / 回环 / 私网 ⇒ 这台**不可能**是线上生产库（第 45 轮 B-m5）。"""
    if not host or host in ("localhost", "::1", "[::1]"):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False                      # 域名：不是"显然本机"，也不许被判成本机
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def _is_the_production_host(host):
    """主机名**就是**（或挂在）Supabase 的域下面 ⇒ 才是本项目那台线上库。

    第 45 轮 B-m5 的第二半：判据写成 `'supabase' in host` 会被 `notsupabase.evil.example`
    买通 —— 与第 43 轮 `push_sector_mappings_to_prod.py` 上那条"子串匹配的生产域名"同一课。
    所以只认"精确等于或以这几个注册域结尾"（`x.supabase.co.attacker.example` 不算）。
    本项目真要换托管商，改这张表，别把判据改回子串。
    """
    host = (host or '').lower()
    return any(host == s or host.endswith('.' + s) for s in _PROD_DB_DOMAINS)


def _postgres_words(url, name):
    """"这是一个 PostgreSQL"这句话要说到的**三档**，不许一档糊过去。

    第 45 轮 B-m5：旧写法看见 `postgres*://` 就印"线上生产库"。可 `postgresql://u@127.0.0.1/db`
    是本机起的一个 Postgres，`postgresql://u@10.0.0.5/db` 是内网某台 ——
    把不是生产的东西说成生产，比报"远程库"更坏：操作员会照着这句话决定要不要按 `--confirm`。
    现在只有"主机名里带 supabase"（本项目那台的确切形状）才叫线上生产库，
    其余远程 PostgreSQL 单列一档并明说它不是。
    """
    host = _db_host(url)
    if _is_a_local_host(host):
        return "本机 PostgreSQL（%s，不是线上生产库）" % name
    if _is_the_production_host(host):
        return "线上生产库（%s）" % name
    return "远程 PostgreSQL（%s）—— 不是本项目那台 Supabase 生产库" % name


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
        return _postgres_words(url, name)
    if low.startswith('mysql'):
        return 'MySQL 库（%s）' % name
    # 认不出的 scheme：两边都必须走同一条 fallback（第 43 轮笛卡尔积样品 `''`/`'://x'`
    # 一喂下去就量到分叉：src 印 `" 库（(空)）"`、守卫印 `"远端库（(空)）"`）。
    # 第 44 轮 A-m6 的另一半：**不许把原串回显出来** —— `low.split('://')[0]` 在没有 `://` 时
    # 是整个输入，`host=h password=真口令 dbname=d` 里最涉密那段会跟着"这是哪个库"进日志。
    scheme = low.split('://', 1)[0] if '://' in low else ''
    if not re.match(r'^[a-z][a-z0-9+._-]{0,19}$', scheme):
        return '认不出 scheme 的连接串（目标：%s）' % name
    return '%s 库（%s）' % (scheme, name)


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

    ⚠ 它锁的只是**主库**：从这条连接 `ATTACH` 一个可写文件、往**那个库**写照样成功
    （第 44 轮 B-m1）。补的那一道在 `_enforce_query_only()`，由 `read_only_connect()`
    在探针通过之后挂上。
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


def _enforce_query_only(engine):
    """给这条 sqlite 引擎补一道**整条连接**的只读（含 `ATTACH` 进来的别的库）。

    第 44 轮 B-m1：`mode=ro` 只锁主库。从一条 ro 连接里 `ATTACH '别的东西.db'` 再往**那个库**
    写，SQLite 是答应的 —— 于是"引擎级只读"这句话留着一个能把写带出去的侧门。
    `PRAGMA query_only=ON` 管的是整条连接，attach 进来的一起算。

    **顺序是硬的**：必须排在 `_write_probe` 之后，并且 `dispose()` 掉探针用过的那条连接。
    先开 pragma 的话，一条**可写**连接上的探针也会被 pragma 拒绝 ⇒
    那道"数据库自己说不许写才算只读"的 fail-closed 闸门当场变成恒真。
    """
    from sqlalchemy import event

    def _pragma(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA query_only=ON")
        cursor.close()

    event.listen(engine, "connect", _pragma)
    engine.dispose()          # 池里那条旧连接不带 pragma ⇒ 留着它等于没锁


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
       （sqlite 腿建完立刻删 —— 见 `_write_probe`）。sqlite 腿在探针通过之后再补一道
       `PRAGMA query_only=ON`（`_enforce_query_only`）：`mode=ro` 只锁主库，
       `ATTACH` 一个可写文件再往**那个库**写照样能成 —— 第 44 轮 B-m1 的那道侧门。
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
            # 第二条必须是 `DROP TABLE IF EXISTS`：PG 的 DDL 是**事务性**的，探针下面那句
            # `conn.rollback()` 会把刚建的临时表一起带走 ⇒ 用裸 `DROP TABLE`  cleanup 必然报
            # "表不存在"，于是判定虽然仍然拒写（方向没错），话却指挥人去线上查一张根本没留下的表
            # （第 45 轮 A-m8 / B-m6）。SQLite 腿不受影响：那边 DDL 隐式提交，表是真会留下的。
            why = _write_probe(engine, ["CREATE TEMP TABLE _db_guard_probe(x int)",
                                        "DROP TABLE IF EXISTS _db_guard_probe"])
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
        _enforce_query_only(engine)     # 探针之后才锁：见 `_enforce_query_only` 的"顺序是硬的"
        label = ("%s（引擎级只读；要分析线上数据得显式 --production，"
                 "等价命令 scripts/q.py --production）" % db_kind(url))
    print("[库] %s" % label)
    sys.stdout.flush()
    return engine, sessionmaker(bind=engine)(), label
