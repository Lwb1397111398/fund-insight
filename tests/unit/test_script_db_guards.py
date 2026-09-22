# -*- coding: utf-8 -*-
"""能改数据的脚本必须说清楚"我连的是哪个库"（第 28 轮 F-MINOR-6）。

`scripts/run_three_bucket_retention.py` 以前直接 `from src.models.database import SessionLocal`
且不设守卫 —— 而 `.env` 里的 `DATABASE_URL` 指向**生产 Supabase**。它是那批无守卫脚本里
唯一带**硬删**的：跑起来默认就在生产上算删除候选，还能 `--execute --confirm` 真删，
而回执里连"哪个库"都不印。修完之后，这条规矩由本用例盯着，不再靠我记得。

合格的写法任一即可：
  * `pin_local_sqlite(...)`：默认钉本地镜像（本地运维脚本的标准做法，AGENTS.md 已写）；
  * `os.environ['DATABASE_URL'] = ...`：自己把连接串钉到副本上（如 `verify_realign_chain.py`）；
  * `--against-production` 这类**显式**远程开关：默认不碰生产，要碰必须说（`sync_db_columns.py`）；
  * `database_label(...)`：至少把真实库名印到回执第一行。
"""
import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'

# 带写开关 / 确认口令、**且直连 ORM** 的 CLI 脚本才受这条规矩管。
# 只走 HTTP 的工具（如 `push_sector_mappings_to_prod.py`）不在这里：它印的是目标站点，
# 库由服务端决定，"连哪个库"这句话不归它说。
WRITE_SWITCH = re.compile(r'"--(apply|execute|hard-delete|import)"|--confirm\b|confirm_token')
DIRECT_DB = re.compile(r'src\.models\.database|SessionLocal')
GUARD_OK = re.compile(
    r'pin_local_sqlite|os\.environ\[[\'"]DATABASE_URL[\'"]\]\s*=|against-production|database_label')


def _write_scripts():
    out = []
    for py in sorted(SCRIPTS.glob('*.py')):
        if py.name.startswith('_'):
            continue
        text = py.read_text(encoding='utf-8', errors='replace')
        if WRITE_SWITCH.search(text) and DIRECT_DB.search(text):
            out.append((py.name, text))
    return out


def test_there_are_scripts_left_to_guard():
    """用例不能变成空判：至少要有若干受管脚本。"""
    assert len(_write_scripts()) >= 5, '受管脚本少于 5 个 ⇒ 这条扫描大概失效了，先确认再放行'


def test_every_write_switch_script_declares_its_database():
    naked = [name for name, text in _write_scripts() if not GUARD_OK.search(text)]
    assert not naked, (
        '这些脚本能改数据，却没说自己连的是哪个库（默认会连 .env 里的生产）：%s'
        % '、'.join(naked))


def test_hard_delete_scripts_pin_the_mirror_by_default():
    """硬删的脚本必须**默认**钉本地镜像；要动生产只能是显式开关。"""
    offenders = []
    for py in sorted(SCRIPTS.glob('*.py')):
        text = py.read_text(encoding='utf-8', errors='replace')
        if '.delete(' not in text:
            continue
        if 'pin_local_sqlite' not in text:
            offenders.append(py.name)
        elif 'use_mirror_default=True' not in text and '--against-production' not in text:
            # 只设 LOCAL_DB_URL 不设默认 ⇒ .env 指向生产时会 abort（那是有意的），
            # 但"能一路跑到生产"的写法要报出来
            offenders.append('%s（pin 未声明 use_mirror_default，也没显式远程开关）' % py.name)
    assert not offenders, '这些脚本能物理删数据但默认不钉镜像：%s' % '、'.join(offenders)
