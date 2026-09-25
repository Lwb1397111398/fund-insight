"""
Pytest 配置文件
"""
import sys
import os
import tempfile
from pathlib import Path
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# 前端变异体检正在改写 web/ 时，这一轮 pytest 读到的源码不是老板那份 —— 拦掉。
# 体检自己起的子 pytest（带 MUTATION_HARNESS_PID）放行，见 src/utils/mutation_lock.py。
# **这里不许在模块顶层 import src.***：`src/__init__.py` 会拉起 `src.core.config`，
# 那早于下面第 49 行钉 `DATABASE_URL` ⇒ `src.models.database.engine` 绑的就是 `.env` 里的生产库。
# 这个坑是我自己踩出来的（第 33 轮）：一条单测因此把 `INSERT INTO fund_info` 发到了 Supabase。
_SESSION_LOCK = None


def pytest_configure(config):
    from src.utils import mutation_lock
    if mutation_lock.current_session_pid() is None and mutation_lock.is_being_mutated(project_root):
        pytest.exit('前端变异体检正在改写 web/，此时跑 pytest 得到的红绿都不作数；'
                    '请等它跑完（锁：%s）。' % mutation_lock.lock_path(project_root),
                    returncode=2)
    # 反向也要拦住（第 33 轮 A-MAJOR-3 / B-MINOR-11：只有上面那一半时，"先起 pytest 再起体检"
    # 依旧能让体检就地改写 web/）。拿不到不算错 —— 只要有一个会话握着，体检就会被挡住。
    global _SESSION_LOCK
    if mutation_lock.current_session_pid() is None:
        _SESSION_LOCK = mutation_lock.acquire_session_lock(project_root)
        if _SESSION_LOCK is None:
            # 第 46 轮 A-m7：pytest 与 pytest **不互斥**（这把锁只用来挡体检），
            # 而两个人同时在同一棵树上跑会给出**不同的数** —— 那一轮 A 席并发时段读到
            # 1064 passed、干净复跑 1063，`--collect-only` 两次都是 1079。
            # "跑不动"与"跑不绿"必须分得开，所以这里当场印一行：基线数字要串行测。
            print('\n[警告] 已经有一个 pytest 会话握着 %s ⇒ 本次是**并发**跑的：'
                  '通过条数可能与串行结果不同（实测差 1 条）。基线数字请串行重跑后再抄。'
                  % mutation_lock.session_lock_path(project_root), flush=True)


class BlockedRealHttp(BaseException):
    """单测里偷打真实接口的信号。**故意不派生自 Exception**。

    第 19 轮 MAJOR-3：派生自 `AssertionError` 时它会被业务代码里那些
    "站点抖动一律按没结论处理"的 `except Exception` 吞掉 —— 于是"桩失效"与"接口没结论"
    长得一模一样，`test_no_conclusion_never_accuses` 那一族在桩完全失灵时仍然全绿，
    而 `tests/conftest.py` 承诺的"漏网取数会变成看得见的失败"落空。
    吞异常本身在生产里是对的（不能因为体检坏了就让保存按钮报错），所以修的是信号：
    只有测试夹具抛这个类，生产路径永远不会遇到它，也就不会被任何 `except Exception` 吃掉。
    """

# 必须在导入应用配置前覆盖 DATABASE_URL，避免集成测试写入 Supabase。
_test_db_path = Path(tempfile.gettempdir()) / f"fund-insight-pytest-{os.getpid()}.db"
# 同名文件复用会把上一轮的**旧表结构**带进来（0007/0008/0009 之前建的文件尤其如此），
# 表现为随机 "no column named match_source" 之类的假故障；开跑前先删干净。
for _stale in (str(_test_db_path), str(_test_db_path) + '-wal', str(_test_db_path) + '-shm'):
    try:
        if os.path.exists(_stale):
            os.remove(_stale)
    except OSError:
        pass
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db_path.as_posix()}"
os.environ.pop("ALEMBIC_DATABASE_URL", None)
os.environ.setdefault("LLM_API_KEY", "test-key")
# 上面那行必须**先于任何 `src.*` 导入**执行；这条断言把顺序钉住（第 33 轮我自己破坏了它，
# 于是 `test_audit_fund_info_identity` 的四条夹具把 INSERT 发到了生产 Supabase）。
assert 'src' not in sys.modules and 'src.models.database' not in sys.modules, (
    'conftest 在钉 DATABASE_URL 之前就导入了 `src` 包 ⇒ 应用 engine 绑的是 `.env` 里的库'
    '（本机就是生产 Supabase）。任何顶层 import 都要挪到这条断言之后。')

from src.models.database import Base
from src.models.database import engine as _app_engine

# 光有上面那条"没提前导入"的断言不够：真正致命的是**后面 `init_db()` 会发 DDL**。
# 所以在碰任何表之前，先看一眼应用 engine 到底连的是哪个库（第 33 轮 A/B 两份复评同点：
# `test_database_url_routing.py` 是事后探测，那时夹具已经写进去了）。
# 只打印 drivername：连接串里有口令，绝不该出现在报错里。
assert str(_app_engine.url).startswith('sqlite'), (
    '应用 engine 连的不是临时 SQLite（driver=%s）⇒ 本轮所有测试会往那个库写。'
    '十有八九是 `DATABASE_URL` 被 .env/插件在钉库之后又改回去了。' % _app_engine.url.drivername)


@pytest.fixture(scope="session", autouse=True)
def initialize_application_database():
    """让 API 集成测试在独立数据库中也拥有完整表结构。"""
    from src.models.database import init_db

    init_db()


@pytest.fixture
def test_db():
    """内存 SQLite 测试数据库，每次测试自动隔离"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    db = TestSession()
    yield db
    db.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def db_session():
    """数据库会话 fixture（使用真实数据库，仅用于集成测试）"""
    from src.models.database import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        # 回滚所有未提交的操作，避免测试数据污染
        db.rollback()
        db.close()


@pytest.fixture(autouse=True)
def _isolate_sector_service_caches():
    """板块映射有两处**进程内**缓存：`SectorFundService` 的类属性映射表 + 绑着某个
    session 的服务单例 `_sector_fund_service`，外加身份体检的静态表拒绝集。

    它们都是跨用例脏读的来源：单例还绑着上一个用例已经关闭的 session，按全量顺序跑时
    会把 A 文件的映射当成 B 文件的"现状"（实测 `test_machine_swap_flag_survives_nothing_after_a_manual_edit`
    单跑 11 passed、全量跑就挂）。逐个测试文件自己清太容易漏——新增一个文件就得记得抄一次，
    所以统一放在根 conftest 里，进出各清一遍。
    """
    from src.services import sector_fund_service as sfs
    from src.services import sector_identity_audit as audit

    def _reset():
        sfs.SectorFundService._cache = {}
        sfs.SectorFundService._cache_loaded = False
        sfs._sector_fund_service = None
        audit.invalidate_denied_cache()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _block_real_http(monkeypatch):
    """零网络**由夹具强制**，不再靠每个文件自觉写桩（第 16 轮实测：单测里有 2 条真打了东财）。

    为什么必须换做法：`from src.fund import fund_api` 拿到的是 **`FundAPI` 实例**
    （`src/fund/__init__.py` 把包属性 `fund_api` 重绑成了实例），所以在它身上
    `setattr(..., 'fund_data_manager', 桩)` 是空操作，而验证侧走
    `from src.fund.fund_api import fund_data_manager`（模块属性）拿到的还是真管理器 ——
    用例照样绿，结论却取决于第三方实时数据。与其一处一处修，不如把 HTTP 层本身掐掉：
    漏网的取数会变成**看得见的失败**，而不是悄悄改变的判定。

    两条腿都要掐（第 17 轮 MINOR-1）：基金数据走 `requests`，LLM 走
    `openai → httpx`。只掐 requests 的话，"零网络由夹具强制"这句只兑现了一半。
    """
    import requests

    def _refuse_requests(self, request, *args, **kwargs):
        raise BlockedRealHttp(
            '测试禁止真实外呼（请把这条取数路径注入桩）：%s'
            % getattr(request, 'url', request))

    monkeypatch.setattr(requests.Session, 'send', _refuse_requests)
    try:
        import httpx
    except ImportError:                       # 没装 httpx 就少一条腿，不影响 requests 那一条
        httpx = None
    if httpx is not None:
        # Starlette 的 TestClient 就是 httpx.Client 的子类，请求走同一个 `send`。
        # 一刀切会把所有 API 用例打死（第 17 轮我就这么干过一次），所以放行 ASGI 传输
        # 用的占位域名，其余一律拒绝 —— 判据是"要不要出网"，不是"用的是哪个库"。
        local_hosts = {'testserver', 'testserver.local'}
        original_client_send = httpx.Client.send

        def _refuse_httpx(self, request, *args, **kwargs):
            url = getattr(request, 'url', None)
            host = getattr(url, 'host', '') or ''
            if host in local_hosts:
                return original_client_send(self, request, *args, **kwargs)
            raise BlockedRealHttp(
                '测试禁止真实外呼（LLM 走 httpx，请把 analyzer 注入桩）：%s' % url)

        monkeypatch.setattr(httpx.Client, 'send', _refuse_httpx)
        monkeypatch.setattr(httpx.AsyncClient, 'send', _refuse_httpx)


@pytest.fixture
def sample_blogger_data():
    """示例博主数据（使用 UUID 避免名称冲突）"""
    import uuid
    return {
        "name": f"测试博主_{uuid.uuid4().hex[:8]}",
        "platform": "xiaohongshu",
        "description": "这是一个测试博主"
    }


@pytest.fixture
def sample_post_data():
    """示例帖子数据"""
    from datetime import date
    return {
        "blogger_id": 1,
        "title": "测试帖子",
        "content": "这是一篇测试帖子的内容，用于单元测试。",
        "post_date": date.today()
    }


@pytest.fixture
def sample_prediction_data():
    """示例预测数据"""
    from datetime import date
    import uuid
    return {
        "post_id": 1,
        "blogger_id": 1,
        "fund_code": f"TEST{uuid.uuid4().hex[:6].upper()}",
        "fund_name": "测试基金",
        "sector": "白酒",
        "prediction_type": "bullish",
        "prediction_content": "看好白酒板块",
        "confidence": 70,
        "prediction_date": date.today(),
        "target_date": date.today()
    }
