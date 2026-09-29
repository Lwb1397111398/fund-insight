"""删除后回收磁盘空间：SQLite VACUUM 缩库、表名白名单、开关。"""
import io
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, FundHistory
from src.services.db_space import (
    _vacuum_postgres,
    failure_detail,
    format_bytes,
    full_vacuum_max_rows,
    reclaim_space,
    skip_detail,
    space_reclaim_enabled,
)


def _file_db(tmp_path, rows: int = 4000):
    path = tmp_path / "space.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    from datetime import date, timedelta

    base = date(2026, 1, 1)
    session.bulk_save_objects([
        FundHistory(
            fund_code=f"F{i % 20:03d}",
            fund_name="空间测试基金",
            nav_date=base + timedelta(days=i // 20),
            nav=1.0 + i / 10000,
        )
        for i in range(rows)
    ])
    session.commit()
    return engine, session, path


def _logical_size(session) -> int:
    pages = session.execute(text("PRAGMA page_count")).scalar()
    page_size = session.execute(text("PRAGMA page_size")).scalar()
    return int(pages) * int(page_size)


def test_sqlite_vacuum_shrinks_database_after_delete(tmp_path):
    engine, session, path = _file_db(tmp_path)
    try:
        before = _logical_size(session)
        session.query(FundHistory).filter(FundHistory.nav_date < "2026-06-01").delete(
            synchronize_session=False
        )
        session.commit()
        # 删除本身不缩库
        assert _logical_size(session) == before

        result = reclaim_space(session, ["fund_history"])

        assert result["success"] is True
        assert result["dialect"] == "sqlite"
        assert result["bytes_freed"] > 0
        assert result["bytes_after"] < result["bytes_before"]
        assert _logical_size(session) < before
    finally:
        session.close()
        engine.dispose()


def test_reclaim_space_rejects_unsafe_table_names(tmp_path):
    engine, session, path = _file_db(tmp_path, rows=10)
    try:
        with pytest.raises(ValueError) as excinfo:
            reclaim_space(session, ['fund_history"; DROP TABLE predictions; --'])
        assert "unsafe table names" in str(excinfo.value)
    finally:
        session.close()
        engine.dispose()


def test_reclaim_space_can_be_disabled_by_env(tmp_path, monkeypatch):
    engine, session, path = _file_db(tmp_path, rows=10)
    monkeypatch.setenv("ENABLE_SPACE_RECLAIM", "false")
    try:
        result = reclaim_space(session, ["fund_history"])
        assert result["skipped"] is True
        assert result["reason"] == "ENABLE_SPACE_RECLAIM=false"
    finally:
        session.close()
        engine.dispose()


def test_reclaim_space_noop_without_tables(tmp_path):
    engine, session, path = _file_db(tmp_path, rows=10)
    try:
        assert reclaim_space(session, [])["reason"] == "no_tables"
    finally:
        session.close()
        engine.dispose()


def test_in_memory_database_is_skipped(test_db):
    result = reclaim_space(test_db, ["fund_history"])
    assert result["skipped"] is True
    assert result["reason"] == "in_memory_database"


def test_space_reclaim_switch_defaults_on(monkeypatch):
    monkeypatch.delenv("ENABLE_SPACE_RECLAIM", raising=False)
    assert space_reclaim_enabled() is True


def test_full_vacuum_threshold_reads_env(monkeypatch):
    monkeypatch.setenv("VACUUM_FULL_MAX_ROWS", "12345")
    assert full_vacuum_max_rows() == 12345
    monkeypatch.setenv("VACUUM_FULL_MAX_ROWS", "not-a-number")
    assert full_vacuum_max_rows() == 500_000


def test_format_bytes_is_human_readable():
    assert format_bytes(0) == "0 B"
    assert format_bytes(None) == "0 B"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_a_per_table_failure_puts_its_reason_where_the_page_reads_it(tmp_path):
    """按表 VACUUM 失败时，原因必须同时出现在**顶层**——页面上那一栏读的是顶层 error。

    PostgreSQL 那条路只能按表跑（VACUUM 不能进事务块，也没有整库 VACUUM），所以它的
    失败逐条落在 `tables[表].error` 里。上一版顶层只有 `success: False`，于是
    "数据库明明答了、页面上问不出来"：那一栏会老实说"数据库没给出原因"，
    而生产（PostgreSQL）恰好就是这一路——本地 sqlite 反而看不见。

    这里拿 sqlite 引擎喂 `_vacuum_postgres`：那些 `pg_*` / `VACUUM (FULL, ANALYZE)`
    语句在 sqlite 上必然被**数据库自己**拒掉 ⇒ 走的正是"每张表各自失败"那一档，
    原因是驱动给的，不是我编的。成功那一档本仓两个库都跑不出（今天两处都没有 PG 实例），
    所以这一格只判"失败的原因有没有递到页面上"，不替它作保。
    """
    engine, session, path = _file_db(tmp_path, rows=20)
    try:
        result = _vacuum_postgres(engine, ["fund_history", "posts"], force_full=None)
        assert result["success"] is False
        assert result["mode"] == "per-table"
        failing = [name for name, e in result["tables"].items() if not e["success"]]
        assert failing == ["fund_history", "posts"]
        assert result["error"], "顶层没有 error ⇒ 页面那一栏只能报'数据库没给出原因'"
        assert "数据库没给出原因" not in result["error"]
        for name in failing:
            assert name in result["error"]
        # 顶层那句与那把尺子的答案必须是同一句（两处消费方读同一份原因）
        assert failure_detail(result) == result["error"]
    finally:
        session.close()
        engine.dispose()


def test_the_failure_reason_has_one_home_and_prefers_what_the_database_gave():
    """「这次没跑成的原因是什么」全仓只许一处回答，且三档各有各的话。"""
    # ① 顶层已经写了（sqlite 整库 VACUUM 那一路）⇒ 原样带出，不另拼一句
    assert failure_detail({"error": "database is locked"}) == "database is locked"
    # ② 只有按表的失败（PostgreSQL 那一路）⇒ 逐表点名，成功的表不进这句话
    per_table = {
        "tables": {
            "fund_history": {"success": False, "error": "cannot VACUUM inside a transaction"},
            "posts": {"success": True},
        }
    }
    assert failure_detail(per_table) == "fund_history：cannot VACUUM inside a transaction"
    # ③ 两边都拿不到 ⇒ 明说拿不到，不许编一句原因，也不许退化成"完成"
    assert failure_detail({"tables": {}}) == "数据库没给出原因"


def test_every_skip_exit_says_its_reason_in_human_words(monkeypatch):
    """「没跑」的每一个出口都得自己带一句人话，而且不许退化成把机器键搬上屏幕。

    `skip_detail` 是全仓唯一那句「空间回收没跑：……」的家（第 72 轮 M-2：以前 `config.py`
    路由里还有第二份 `_RECLAIM_SKIP_SENTENCES` ⇒ 同一个 `no_tables` 在独立按钮那一路是人话、
    在清理任务那一路被原样搬上屏幕）。这一条问三件事：
    ① `_skipped()` 交回的 `reason_text` 必须**等于** `skip_detail(result)`（拼这句话的函数只有一处；
       两个消费方走的是同一个函数 —— 路由现调 `skip_detail(result)`，页面读预先盖好的 `reason_text`）；
    ② 已知三种原因各说各话；认不出的原因也不许变成空话、也不许把原因本身丢掉；
    ③ 方言不支持那一格两个库（sqlite / PG）今天都走不到 ⇒ 拿桩把 `get_bind` 换成 mysql
       方言喂进去，它必须说出是**哪个方言**，不许退化成一句"没跑"。
    """
    from types import SimpleNamespace

    from src.services import db_space

    known = {
        "ENABLE_SPACE_RECLAIM=false": "回收开关（ENABLE_SPACE_RECLAIM）",
        "no_tables": "没有要回收的表",
        "in_memory_database": "内存数据库",
    }
    for reason, phrase in known.items():
        result = db_space._skipped(reason)
        assert result["reason_text"] == skip_detail(result), result
        assert phrase in result["reason_text"], result
        # 键要给机器留着，但上屏幕的永远是那句人话
        assert reason in result["reason"], result

    odd = db_space._skipped("disk_full")
    assert odd["reason_text"].startswith("空间回收没跑"), odd
    assert "disk_full" in odd["reason_text"], odd

    class _Db:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

        def commit(self):
            pass

    monkeypatch.setattr(db_space, "space_reclaim_enabled", lambda: True)
    result = db_space.reclaim_space(_Db(), ["fund_history"])
    assert result["reason"] == "unsupported_dialect:mysql", result
    assert "mysql" in result["reason_text"], result
    assert "不支持" in result["reason_text"], result


def _skip_sentence_homes(pairs):
    """逐棵目录树找出「空间回收没跑」出现在哪些文件里（`pairs` = [(根, 后缀), ...]）。"""
    homes = []
    for label, root, suffixes in pairs:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if not name.endswith(suffixes):
                    continue
                full = os.path.join(dirpath, name)
                with io.open(full, encoding="utf-8") as fh:
                    if "空间回收没跑" in fh.read():
                        homes.append(os.path.join(label, os.path.relpath(full, root)))
    return homes


def test_the_skip_sentence_has_exactly_one_home_in_the_source(tmp_path):
    """「空间回收没跑」这句话在整个仓库里只许有一个家（第 72 轮 M-2 的结构账）。

    行为判据只能证明"这一路说对了"；"两个消费方有没有各拼一遍"由这一条盯着——
    再多一处拼这句话，就意味着有人把键名自己翻译了一遍，于是同一个原因会有两种口径。
    ⚠ 第 73 轮 MINOR-2：扫描面原本只有 `src/`，页面那一路压根不在闸里。现在把 `web/`
    也扫进来（今天 `web/` 命中 **0** 处 —— 现读 `grep -rn 空间回收没跑 web/` 为空：
    页面上那一栏拼的是自己的前缀「空间回收：没跑 —— 」再接服务层交回的 `reason_text`，
    它并不重拼这句话）。所以这一条今天钉的是"以后页面自己拼一遍也要当场被点名"。
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    homes = _skip_sentence_homes([
        ("src", os.path.join(root, "src"), (".py",)),
        ("web", os.path.join(root, "web"), (".html", ".js")),
    ])
    assert homes == [os.path.join("src", "services", "db_space.py")], homes

    # 空判对照：扫描面真的扩到 `web/` 了吗 —— 临时树里两棵各造一处家，两处都必须被点名，
    # 干净的那个文件不许被误伤。摘掉 `web/` 那一腿，这里就少报一处。
    fake = tmp_path
    (fake / "src").mkdir()
    (fake / "web").mkdir()
    (fake / "src" / "a.py").write_text("空间回收没跑：假的\n", encoding="utf-8")
    (fake / "src" / "b.py").write_text("没有那句话\n", encoding="utf-8")
    (fake / "web" / "c.html").write_text("空间回收没跑：页面自己拼的\n", encoding="utf-8")
    found = _skip_sentence_homes([
        ("src", str(fake / "src"), (".py",)),
        ("web", str(fake / "web"), (".html", ".js")),
    ])
    assert sorted(found) == sorted([
        os.path.join("src", "a.py"), os.path.join("web", "c.html"),
    ]), found
