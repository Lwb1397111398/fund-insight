# -*- coding: utf-8 -*-
"""前端变异体检与 pytest 会话之间的互斥闸（操作系统级文件锁）。

为什么要有：`scripts/mutation_proof_frontend.py` 会**就地改写** `web/index.html`
与 `web/*-manager.js`，再对这些文件跑 pytest。同一时刻另一个 pytest 会话读到的
就是被变异过的源码 —— 第 30 轮 B 实测并发时报出 12 条假 GREEN + 3 条锚点失配。
那条"不能并发"的话原本只写在体检的 docstring 里（第 32 轮 B 抓到：没有闸的承诺
等于没有承诺），这里把它变成会拦人的代码。

锁是 OS 级的：进程被强杀时操作系统自己释放，不留陈旧锁文件。
"""
import os
import sys
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = '.mutation-harness.lock'
# 体检自己起的子 pytest 会继承这个变量（父进程 PID），据此放行它自己。
ENV_PID = 'MUTATION_HARNESS_PID'


def lock_path(root):
    return Path(root) / LOCK_NAME


def _try_lock(fh):
    fh.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh):
    try:
        fh.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def held_exclusively(root):
    """体检用：拿到锁才继续。拿不到说明另一个体检或另一个会话在跑，直接拒绝。"""
    path = lock_path(root)
    fh = open(path, 'a+')
    if not _try_lock(fh):
        fh.close()
        raise RuntimeError(
            '另一个前端变异体检正在改写 web/（锁文件 %s）—— 等它跑完再启动，'
            '两边并发时报出来的红绿都不作数。' % path)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        yield path
    finally:
        _unlock(fh)
        fh.close()


def is_being_mutated(root):
    """pytest 用：True = 有体检正持有锁。体检自己起的子进程（带 ENV_PID）返回 False。"""
    path = lock_path(root)
    if not path.exists():
        return False
    fh = open(path, 'a+')
    try:
        got = _try_lock(fh)
        if got:
            _unlock(fh)
        return not got
    finally:
        fh.close()


def current_session_pid():
    return os.environ.get(ENV_PID)


def main():
    """`python -m src.utils.mutation_lock` —— 手工看锁此刻有没有被持有。"""
    root = Path(__file__).resolve().parents[2]
    print('lock held:', is_being_mutated(root), '| path:', lock_path(root))
    return 0


if __name__ == '__main__':
    sys.exit(main())
