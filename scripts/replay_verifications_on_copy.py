# -*- coding: utf-8 -*-
"""在**库副本**上重放验证，比对判据改动前后的历史结论有没有漂移。

为什么要有这个脚本（S7 计划案的"准确率漂移闸门"）：改 `_check_fund_data_availability`
这类判据，光看"新代码不报错"是不够的 —— 必须证明它**没有偷偷改变**已经判过的结论。
在生产/镜像库上直接重跑会写真数据，所以一律在副本上做：
1. `shutil.copy2` 出一份临时库，`DATABASE_URL` 指过去（只影响本进程）；
2. 挑一批**已验证**的预测，把旧的 `is_correct/verify_score/actual_change/...` 记下来，
   清空后走真实 `verify_prediction` 重放；
3. 逐条比对，差异分成"可解释"（判据收紧后本该重判的）与"未解释"，
   **有未解释差异就退出码 3**，别把脏改动带进下一环。

用法：
    python scripts/replay_verifications_on_copy.py                 # 默认抽 30 条
    python scripts/replay_verifications_on_copy.py --limit 120
    python scripts/replay_verifications_on_copy.py --ids 1709,2847  # 只重放指定预测
    python scripts/replay_verifications_on_copy.py --keep           # 保留副本库便于复查
"""
import argparse
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from _db_guard import pin_local_sqlite  # noqa: E402  只为拿到源库路径

SOURCE_URL = pin_local_sqlite()

# 首版在这里 `from src.models.database import ...` —— **那是事故根源**：
# SQLAlchemy 的 engine 在 import 那一刻就按当时的 DATABASE_URL 建好并绑死，
# 之后再把 DATABASE_URL 改指副本毫无作用，两次"副本重放"其实写进了镜像库。
# 所以本脚本改成：副本建自己的 engine，并把 `src.models.database.SessionLocal`
# 换绑到副本（任何内部自己 new 会话的代码也只能落在副本上），
# 最后再断言"当前绑定的 URL 就是副本"，不匹配直接退出。
from datetime import date  # noqa: E402

VERDICT_FIELDS = ('is_correct', 'verify_score', 'actual_change', 'status',
                  'start_nav', 'start_nav_date', 'end_nav', 'end_nav_date')
# 只清"判据的输出"，**故意保留 start_nav / start_nav_date**：那是当时验证落定的输入
# （可能是帖子/LLM 给的起点净值），把它一起清掉等于换了输入再比结论，
# 测出来的差异就说不清是判据改的还是输入改的。首版就因此误报了 4 条"方向翻转"。
CLEAR_FIELDS = ('is_correct', 'verify_score', 'actual_change', 'status', 'is_expired',
                'end_nav', 'end_nav_date',
                'current_nav', 'current_nav_date', 'verified_at', 'last_verify_date',
                'verify_count')
# 浮点末位不算漂移：导出的 JSON 只有 15 位有效数字，重放算出来的是全精度，
# 首版脚本把 -5.96453821119964 vs -5.964538211199645 判成了"未解释差异"（5 条假阳性）。
FLOAT_TOL = 1e-6


def _same(a, b):
    if a is None or b is None:
        return a is b or a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        try:
            return abs(float(a) - float(b)) <= FLOAT_TOL * max(1.0, abs(float(a)), abs(float(b)))
        except (TypeError, ValueError):
            return a == b
    return a == b


def _explain(diffs, before, after, target, refused):
    """差异能不能解释。解释不了就必须留在报告里，别让"看起来合理"糊过去。"""
    if refused:
        return '新判据拒判（拿不到数据就不下结论）'
    old_end, new_end = before.get('end_nav_date'), after.get('end_nav_date')
    if isinstance(old_end, date) and isinstance(target, date) and old_end > target:
        if not isinstance(new_end, date) or new_end <= target:
            return '旧结论用了目标日之后的净值（防未来函数修正），本次按目标日及之前判定'
    if before.get('is_correct') == after.get('is_correct') and 'verify_score' not in diffs:
        return '数值端点差异（本地镜像与当时线上取到的行不同），方向结论未变'
    if before.get('is_correct') == after.get('is_correct') and set(diffs) == {'verify_score'}:
        return '只有分数差异（过程指标随端点取哪条而变），方向结论未变'
    if ('end_nav_date' not in diffs and before.get('end_nav_date') == after.get('end_nav_date')
            and 'end_nav' in diffs):
        return ('同一天标签下数值不同：旧结论写的是"当时能取到的更早净值"，而 `end_nav_date` '
                '存的是请求日（S7-2 之前的老口径），所以看不出来；与本轮判据无关')
    return None


def _sqlite_path(url):
    body = url.split('sqlite:///')[-1] if 'sqlite:///' in url else ''
    return body.replace('\\', '/')


def _sample(db, model, limit, ids):
    q = db.query(model).filter(model.is_deleted == False,
                               model.is_correct.isnot(None),
                               model.prediction_type != 'flat',
                               model.target_date.isnot(None))
    if ids:
        return q.filter(model.id.in_(ids)).all()
    # 分层抽样：按目标日升序取，覆盖不同周期与不同博主，而不是随手取前 N 条
    rows = q.order_by(model.target_date.asc()).all()
    if limit and len(rows) > limit:
        step = len(rows) / float(limit)
        rows = [rows[int(i * step)] for i in range(limit)]
    return rows


def _bind_session_factory_to(copy_path):
    """建副本自己的 engine，并把 `src.models.database.SessionLocal` 换绑过去。

    换绑是为了让"内部自己 new 会话"的代码也只能落到副本上；
    返回换绑后的工厂，调用方必须核对它真的指向副本。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import src.models.database as models

    engine = create_engine('sqlite:///' + copy_path.replace('\\', '/'))
    models.SessionLocal = sessionmaker(bind=engine)
    return models.SessionLocal


def main():
    ap = argparse.ArgumentParser(description='库副本上的验证重放（判据漂移闸门）')
    ap.add_argument('--limit', type=int, default=30)
    ap.add_argument('--ids', help='逗号分隔的 prediction id，指定后忽略 --limit')
    ap.add_argument('--offline', action='store_true',
                    help='副本上不许打数据源（默认打，因为补拉是验证流程的一部分）')
    ap.add_argument('--keep', action='store_true', help='保留副本库文件')
    ap.add_argument('--json', help='把逐条对照写成 JSON')
    args = ap.parse_args()

    src = _sqlite_path(SOURCE_URL)
    if not os.path.exists(src):
        print('[abort] 找不到源库：%s' % src)
        return 4
    tmp = os.path.join(tempfile.gettempdir(), 'fi_replay_%s.db' % date.today().isoformat())
    shutil.copy2(src, tmp)
    os.environ['DATABASE_URL'] = 'sqlite:///' + tmp.replace('\\', '/')
    print('[副本] %s → %s' % (src, tmp))

    SessionLocal = _bind_session_factory_to(tmp)
    from src.models.database import Prediction

    bound = str(SessionLocal().bind.url)
    if os.path.basename(tmp) not in bound:
        print('[abort] 会话绑到了 %s 而不是副本 %s —— 拒绝运行，绝不写源库'
              % (bound, tmp))
        return 5
    print('[副本] 会话已绑定副本：%s' % bound)

    from src.services.prediction_verify_service import PredictionVerifyService

    if args.offline:
        from src.fund import fund_api as fa

        class _NoNet:
            @staticmethod
            def backfill_history_range(*a, **kw):
                return 0
        fa.fund_data_manager = _NoNet()

    probe = SessionLocal()
    wanted = [int(x) for x in args.ids.split(',')] if args.ids else []
    rows = _sample(probe, Prediction, args.limit, wanted)
    ids = [p.id for p in rows]
    expected = {p.id: {f: getattr(p, f) for f in VERDICT_FIELDS} for p in rows}
    print('[抽样] 已验证预测 %d 条重放' % len(ids))
    probe.close()

    db = SessionLocal()
    service = PredictionVerifyService(db)
    drift, unexplained, errors = [], [], []
    try:
        for pid in ids:
            p = db.query(Prediction).filter(Prediction.id == pid).first()
            if p is None:
                continue
            before = expected[pid]
            for f in CLEAR_FIELDS:
                setattr(p, f, None)
            db.commit()
            try:
                result = service.verify_prediction(pid, force=True)
            except Exception as exc:
                errors.append({'id': pid, 'error': str(exc)[:200]})
                continue
            after = {f: getattr(p, f) for f in VERDICT_FIELDS}
            diffs = {k: (before.get(k), after.get(k)) for k in VERDICT_FIELDS
                     if not _same(before.get(k), after.get(k))}
            if not diffs:
                continue
            # 差异要能解释：新判据拒判、旧结论用了目标日之后的净值、或只是本地镜像
            # 与当时线上取到的行不同但方向没变。解释不了的必须留在报告里挡住合入。
            refused = (result.get('success') is False
                       and after.get('is_correct') is None
                       and before.get('is_correct') is not None)
            why = _extend_explanation = None
            why = _explain(diffs, before, after, p.target_date, refused)
            entry = {'id': pid, 'fund_code': p.fund_code, 'target_date': str(p.target_date),
                     'diffs': {k: [str(v[0]), str(v[1])] for k, v in diffs.items()},
                     'reason': ((result.get('data') or {}).get('data_status') or {}).get('reason'),
                     'message': (result.get('message') or '')[:180],
                     'explained': why}
            drift.append(entry)
            if not why:
                unexplained.append(entry)
        print('[对照] 有差异 %d 条，其中可解释 %d 条、未解释 %d 条、异常 %d 条'
              % (len(drift), len(drift) - len(unexplained), len(unexplained), len(errors)))
        by_kind = {}
        for e in drift:
            kind = e['explained'] or '未解释'
            by_kind[kind] = by_kind.get(kind, 0) + 1
        for kind, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            print('   %-46s %d' % (kind, n))
        for e in drift[:40]:
            flag = '可解释' if e['explained'] else '!! 未解释'
            print('   [%s] id=%-5s %s 目标%s 解释=%s 差异=%s'
                  % (flag, e['id'], e['fund_code'], e['target_date'],
                     (e['explained'] or e['reason'] or '-')[:44],
                     '; '.join('%s:%s→%s' % (k, v[0], v[1]) for k, v in e['diffs'].items())[:120]))
        for e in errors[:10]:
            print('   [异常] id=%s %s' % (e['id'], e['error']))
        if args.json:
            with io.open(args.json, 'w', encoding='utf-8') as f:
                json.dump({'copy_db': tmp, 'sampled': len(ids), 'drift': drift,
                           'unexplained': unexplained, 'errors': errors},
                          f, ensure_ascii=False, indent=1)
            print('[ok] 对照明细：%s' % args.json)
        return 3 if unexplained or errors else 0
    finally:
        db.close()
        if not args.keep and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                print('[warn] 副本没删掉（可能还有连接）：%s' % tmp)


if __name__ == '__main__':
    raise SystemExit(main())
