# -*- coding: utf-8 -*-
"""前端变异体检与 pytest 的互斥闸（`src/utils/mutation_lock.py`）。

为什么要有：`scripts/mutation_proof_frontend.py` 会就地改写 `web/index.html` 再跑 pytest。
第 30 轮 B 实测并发跑会假报 12 条 GREEN + 3 条锚点失配，而当时"不能并发"这句话
**只写在体检的 docstring 里**（第 32 轮 B 抓到：没有代码拦着的承诺等于没有承诺）。
这里把两头都钉住：体检抢不到锁要走开、别的 pytest 会话看到锁要拒绝。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.utils import mutation_lock

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_a_second_holder_is_refused_and_the_release_is_automatic(tmp_path):
    """锁必须真的互斥，且持有者退出后自己放开（否则一次崩溃就把仓库永久锁死）。"""
    with mutation_lock.held_exclusively(tmp_path):
        assert mutation_lock.is_being_mutated(tmp_path) is True
        with pytest.raises(RuntimeError) as exc:
            with mutation_lock.held_exclusively(tmp_path):
                pass
        assert '等它跑完' in str(exc.value)
    assert mutation_lock.is_being_mutated(tmp_path) is False


def test_the_lock_file_is_not_tracked_by_git(tmp_path):
    """锁落在仓库根目录：不 gitignore 的话每次体检都会往工作树里塞一个脏文件。"""
    ignore = (PROJECT_ROOT / '.gitignore').read_text(encoding='utf-8')
    assert mutation_lock.LOCK_NAME in ignore, '锁文件没进 .gitignore'


def test_both_sides_are_actually_wired():
    """两头都得真的接线：只留一个工具函数不叫闸。"""
    harness = (PROJECT_ROOT / 'scripts' / 'mutation_proof_frontend.py').read_text(encoding='utf-8')
    assert 'held_exclusively(' in harness, '体检没抢锁就能开始改写 web/'
    # 放行标记要真的传进子进程：只写 `env = dict(os.environ)` 而不 `env=env` 是死的
    assert 'env[mutation_lock.ENV_PID]' in harness, '体检没给自己起的子 pytest 准备放行标记'
    assert 'env=env' in harness, '体检起了子 pytest 但没把放行标记传过去 ⇒ 它会把自己拦死'
    conftest = (PROJECT_ROOT / 'tests' / 'conftest.py').read_text(encoding='utf-8')
    assert 'is_being_mutated(' in conftest, 'pytest 看到锁被持有时不会拦'


def test_a_stray_pytest_session_is_blocked_while_the_harness_holds_the_lock():
    """端到端：锁真的在仓库根上时，一条普通 pytest 命令要非零退出并说明原因。"""
    guard = mutation_lock.held_exclusively(PROJECT_ROOT)
    guard.__enter__()
    try:
        env = {k: v for k, v in os.environ.items() if k != mutation_lock.ENV_PID}
        # 子 pytest 的中文报错必须按 UTF-8 出：中文用户名的机器默认 cp936，父进程按 utf-8
        # 解码就拿到替换字符 ⇒ 下面那条"报错里要说清原因"永不成立（第 33 轮两份复评同抓，
        # 我在自己的 UTF-8 环境里跑是绿的 —— 仓里 `test_database_url_routing.py` 早写过这条）。
        env['PYTHONIOENCODING'] = 'utf-8'
        r = subprocess.run([sys.executable, '-m', 'pytest',
                            'tests/unit/test_mutation_lock.py::test_both_sides_are_actually_wired',
                            '-q', '--no-header', '-p', 'no:cacheprovider'],
                           cwd=str(PROJECT_ROOT), capture_output=True, env=env,
                           text=True, encoding='utf-8', errors='replace')
        assert r.returncode != 0, '锁被持有时普通 pytest 仍然跑完了 ⇒ 闸是假的'
        assert '变异体检正在改写' in (r.stdout + r.stderr)
    finally:
        guard.__exit__(None, None, None)


def test_both_directions_are_blocked(tmp_path):
    """互斥必须是双向的（第 33 轮 A-MAJOR-3 / B-MINOR-11）。

    只有"体检持锁 → pytest 让路"那一半时，先起 pytest 再起体检照样能让体检改写 `web/`。
    现在 pytest 会话自己握一把 `.pytest-session.lock`，体检启动前先问它。
    """
    holder = mutation_lock.acquire_session_lock(tmp_path)
    assert holder is not None, '第一把会话锁就该拿不到 ⇒ 锁根本没互斥'
    assert mutation_lock.session_lock_is_held(tmp_path) is True
    assert mutation_lock.harness_may_start(tmp_path) is False, '体检仍可在 pytest 跑着时启动'
    second = mutation_lock.acquire_session_lock(tmp_path)
    assert second is None, '两个 pytest 会话应该能并存（只是体检会被其中一个挡住）'
    holder.close()
    assert mutation_lock.session_lock_is_held(tmp_path) is False
    assert mutation_lock.harness_may_start(tmp_path) is True


def test_the_harness_checks_the_session_lock_before_starting():
    """闸要在**体检那一侧**真的被调用，不是只写着一个函数。"""
    harness = (PROJECT_ROOT / 'scripts' / 'mutation_proof_frontend.py').read_text(encoding='utf-8')
    assert 'harness_may_start(' in harness, '体检启动时没问"有没有 pytest 在跑"'
    assert 'return [\'pytest-session-holds-the-lock\']' in harness, '问到了却没拦住'
    conftest = (PROJECT_ROOT / 'tests' / 'conftest.py').read_text(encoding='utf-8')
    assert 'acquire_session_lock(' in conftest, 'pytest 会话没握锁 ⇒ 反向拦截是空的'


def test_the_harness_own_child_is_let_through():
    """体检自己起的子 pytest 必须放行，否则它每次跑批都会把自己拦死。"""
    guard = mutation_lock.held_exclusively(PROJECT_ROOT)
    guard.__enter__()
    env = dict(os.environ)
    env[mutation_lock.ENV_PID] = str(os.getpid())
    env['PYTHONIOENCODING'] = 'utf-8'   # 同上：断言里要比中文字串
    try:
        r = subprocess.run([sys.executable, '-m', 'pytest',
                            'tests/unit/test_mutation_lock.py::test_both_sides_are_actually_wired',
                            '-q', '--no-header', '-p', 'no:cacheprovider'],
                           cwd=str(PROJECT_ROOT), capture_output=True, env=env,
                           text=True, encoding='utf-8', errors='replace')
        assert r.returncode == 0, '体检自己的子会话被闸拦掉了：\n%s' % (r.stdout + r.stderr)[-800:]
    finally:
        guard.__exit__(None, None, None)
