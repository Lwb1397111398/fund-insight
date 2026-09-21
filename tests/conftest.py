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

from src.models.database import Base


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
        raise AssertionError(
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
            raise AssertionError(
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
