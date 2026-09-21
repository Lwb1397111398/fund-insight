# -*- coding: utf-8 -*-
"""覆盖式导入的两道闸（第 14 轮 MAJOR-4）。

`POST /api/config/import` 的 `replace=True` 会先 TRUNCATE 再导入，失败就是一片空表。
它是全仓 12 个破坏性入口里**唯一**一个既不要总开关也不要确认头的，上一轮补了闸，
但那条安全边界本身没有任何测试 ⇒ 一旦有人"顺手简化"掉，测试不会红，只有生产会炸。

这里直接调路由函数（不起 TestClient，避免把 LLM/网络配置卷进来）。
"""
import types

import pytest
from fastapi import HTTPException

from src.api.routes import config as config_routes
from src.api.routes.config import ImportDataRequest


class _BG:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *a, **kw):
        self.tasks.append(fn)


def _req(replace):
    return ImportDataRequest(data={'predictions': []}, replace=replace)


def _request(headers):
    return types.SimpleNamespace(headers=headers)


def test_replace_import_requires_the_kill_switch(monkeypatch, test_db):
    monkeypatch.delenv('ENABLE_DATABASE_IMPORT', raising=False)
    with pytest.raises(HTTPException) as err:
        config_routes.import_data(_req(True), _request({'X-Danger-Confirm': 'replace-database-import'}),
                                  _BG(), db=test_db)
    assert err.value.status_code == 403
    assert 'ENABLE_DATABASE_IMPORT' in err.value.detail


def test_replace_import_requires_the_confirm_header(monkeypatch, test_db):
    monkeypatch.setenv('ENABLE_DATABASE_IMPORT', 'true')
    with pytest.raises(HTTPException) as err:
        config_routes.import_data(_req(True), _request({}), _BG(), db=test_db)
    assert err.value.status_code == 403
    assert 'X-Danger-Confirm' in err.value.detail

    # 两道闸都齐了才放行（UI 的"覆盖模式"现在会带上这个头）
    ok = config_routes.import_data(
        _req(True), _request({'X-Danger-Confirm': 'replace-database-import'}),
        _BG(), db=test_db)
    assert ok['success'] is True


def test_merge_import_is_untouched(monkeypatch, test_db):
    """日常用的合并式导入不能被这道新闸误伤。"""
    monkeypatch.delenv('ENABLE_DATABASE_IMPORT', raising=False)
    bg = _BG()
    result = config_routes.import_data(_req(False), _request({}), bg, db=test_db)
    assert result['success'] is True
    assert result.get('async') is True
