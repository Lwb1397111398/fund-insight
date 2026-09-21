# -*- coding: utf-8 -*-
"""在**本地库的一份副本**上跑"老板批准 → 预测改标 → 重置验证"整链。

为什么用副本：真实库里被体检脚本换过标的的行必须留在"待审查"等老板自己点，
agent 替他批等于绕过人工审查（S4a-v7.2 第 5 条）。但"批准后到底会改多少条预测"
不能靠推断——所以在这份副本上把同一条生产代码路径真跑一遍，报实测数字，
跑完删副本，真实库零改动。

    python scripts/verify_realign_chain.py [id1 id2 ...]
"""
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'data', 'fund_insight.db')
COPY = os.path.join(ROOT, 'data', '_realign_chain_copy.db')
DEFAULT_IDS = [127, 134, 135, 136, 140]      # 本轮被确定性纠正的 5 行


def main():
    ids = [int(a) for a in sys.argv[1:] if a.isdigit()] or DEFAULT_IDS
    if not os.path.exists(SRC):
        # sqlite3.connect() 会顺手建库：源库不存在时整条链等于对着空库自证"通过"
        print('[abort] 源库不存在，拒绝凭空造一个空副本：%s' % SRC)
        return 2
    if os.path.exists(COPY):
        os.remove(COPY)
    src, dst = sqlite3.connect(SRC), sqlite3.connect(COPY)
    src.backup(dst)          # 不是拷文件：WAL 里未落盘的改动也得带上
    src.close()
    dst.close()
    url = 'sqlite:///' + COPY.replace('\\', '/')
    os.environ['DATABASE_URL'] = url          # 之后所有连接都只碰副本

    from src.models.database import (SessionLocal, SectorFundMapping, Prediction,
                                     FundInfo, engine)
    from src.services.sector_fund_service import SectorFundService
    db = SessionLocal()
    rows = db.query(SectorFundMapping).filter(
        SectorFundMapping.id.in_(ids)).order_by(SectorFundMapping.id).all()
    print('[副本] 批准前 reviewed=%s' % [bool(r.reviewed) for r in rows])
    result = SectorFundService(db).batch_mark_reviewed(ids, reviewed=True)
    db.expire_all()
    rows = db.query(SectorFundMapping).filter(
        SectorFundMapping.id.in_(ids)).all()
    print('[副本] batch_mark_reviewed → %s；批准后 reviewed=%s'
          % (result, [bool(r.reviewed) for r in rows]))
    db.close()

    env = dict(os.environ, DATABASE_URL=url, PYTHONIOENCODING='utf-8')
    out = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts',
                        'run_sector_sync.py'), '--apply',
                        '--run-id', 'realign-chain'],
                        cwd=ROOT, env=env, capture_output=True, text=True,
                        encoding='utf-8')
    print('[改标] rc=%d' % out.returncode)
    print('\n'.join((out.stdout or '').splitlines()[-10:]))
    if out.returncode:
        print((out.stderr or '')[-1200:])

    db = SessionLocal()
    for m in db.query(SectorFundMapping).filter(
            SectorFundMapping.id.in_(ids)).all():
        total = db.query(Prediction).filter(Prediction.sector == m.sector_name).count()
        moved = db.query(Prediction).filter(
            Prediction.sector == m.sector_name,
            Prediction.fund_code == m.fund_code).count()
        pend = db.query(Prediction).filter(
            Prediction.sector == m.sector_name,
            Prediction.is_correct.is_(None)).count()
        print('   %-6s %s %-22s 预测 %d 条 → 指向新标的 %d 条，待验证 %d 条'
              % (m.sector_name, m.fund_code, m.fund_name, total, moved, pend))
    db.close()
    engine.dispose()
    try:
        os.remove(COPY)
        print('[清理] 副本已删除，真实库未被触碰')
    except OSError as exc:
        print('[warn] 副本没删掉，请手工删 %s（%s）' % (COPY, exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
