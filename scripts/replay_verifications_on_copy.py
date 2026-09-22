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

from _db_guard import pin_local_sqlite  # noqa: E402

# 钉库动作放在 main() 里：本模块的 `_explain` 要被单测直接 import，
# 顶层调用 `pin_local_sqlite(use_mirror_default=True)` 会在测试进程里改 DATABASE_URL。

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
    """差异能不能解释。解释不了就必须留在报告里，别让"看起来合理"糊过去。

    第 17 轮两份复评共同指出：原来的第 3/4 桶只约束"端点取的是哪条净值"，
    **不约束方向结论** ⇒ 9 条 `is_correct` 翻转（含 1.39%→-71.6%、-3.6%→+143.9%）
    被贴上一句"与本轮判据无关"就免检了。留红 2602 只是因为它连日期标签都变了 ——
    谁红不该由"标签变没变"决定。所以现在立一条总则：
    **方向翻转（`is_correct`/`status` 变了）一律不解释**，只有两类例外，
    它们本身就在说"这条结论原本就是错的/原本根本不该有结论"：
      ① 新判据直接拒判（`refused`）；
      ② 旧结论用了目标日之后的净值（防未来函数修正）。
    """
    if refused:
        return '新判据拒判（拿不到数据就不下结论）'
    old_end, new_end = before.get('end_nav_date'), after.get('end_nav_date')
    if isinstance(old_end, date) and isinstance(target, date) and old_end > target:
        if not isinstance(new_end, date) or new_end <= target:
            return '旧结论用了目标日之后的净值（防未来函数修正），本次按目标日及之前判定'
    flipped = (before.get('is_correct') != after.get('is_correct')) or \
              ('status' in diffs and before.get('is_correct') is not None
               and after.get('is_correct') is not None)
    if flipped:
        return None
    if before.get('is_correct') == after.get('is_correct') and 'verify_score' not in diffs:
        return '数值端点差异（本地镜像与当时线上取到的行不同），方向结论未变'
    if set(diffs) == {'verify_score'}:
        return '只有分数差异（过程指标随端点取哪条而变），方向结论未变'
    if ('end_nav_date' not in diffs and before.get('end_nav_date') == after.get('end_nav_date')
            and 'end_nav' in diffs):
        return ('同一天标签下数值不同：旧结论写的是"当时能取到的更早净值"，而 `end_nav_date` '
                '存的是请求日（S7-2 之前的老口径），所以看不出来；方向结论未变')
    return None


def last_entry_is_llm_verify(row) -> bool:
    """这条预测**最后一次**验证走过 LLM 复核那一腿吗（台账末条 `verify_type=llm_verify`）。

    为什么单独判它：那一腿只在边界情形介入（分数 20~80、涨跌幅在震荡阈值附近、
    或方向与实际相反，见 `prediction_verify_service.py:1009-1022`），且**只抬分不降分**
    （`if llm_score > score` ⇒ `is_correct = llm_score >= 60`）。离线重放不含 LLM，
    这类行的 `is_correct` 必然与库里存的不同 —— 那是"少一条腿"，不是判据回归。
    第 16 轮 MAJOR-1 把我上一版的"可解释桶"证伪了：桶的条件全是旧值侧证据，
    对重放出来的值毫无约束 ⇒ 确定性判据整体坏掉（把边界行算成 0 分）也会被它吞掉，
    而盲区恰好覆盖闸门最该盯的那批边界行。所以这里**不做解释、只做排除**：
    离线模式把这些行从抽样里拿掉并如实报数，闸门不许拿它们充当"0 未解释"的分母。
    """
    ledger = row.verify_history or []
    return bool(ledger) and ledger[-1].get('verify_type') == 'llm_verify'


def _sqlite_path(url):
    body = url.split('sqlite:///')[-1] if 'sqlite:///' in url else ''
    return body.replace('\\', '/')


def _sample(db, model, limit, ids, offline=False):
    """挑一批**已验证**的预测来重放。offline 时先把"末轮走过 LLM 复核那一腿"的行整体让出去。

    返回 (要重放的行, 被让出的行)。让出的那些不参与比对、也不算通过比对 ——
    闸门不许拿它们当"未解释漂移=0"的分母（第 16 轮 MAJOR-1）。
    """
    q = db.query(model).filter(model.is_deleted == False,
                               model.is_correct.isnot(None),
                               model.prediction_type != 'flat',
                               model.target_date.isnot(None))
    rows = q.filter(model.id.in_(ids)).all() if ids else q.order_by(model.target_date.asc()).all()
    deferred = []
    if offline:
        keep = [p for p in rows if not last_entry_is_llm_verify(p)]
        deferred = [p for p in rows if last_entry_is_llm_verify(p)]
        rows = keep
    # 分层抽样：按目标日升序取，覆盖不同周期与不同博主，而不是随手取前 N 条
    if not ids and limit and len(rows) > limit:
        step = len(rows) / float(limit)
        rows = [rows[int(i * step)] for i in range(limit)]
    return rows, deferred


def _bind_session_factory_to(copy_path):
    """建副本自己的 engine，并把 `src.models.database.SessionLocal` 换绑过去。

    换绑是为了让"内部自己 new 会话"的代码也只能落到副本上；
    返回换绑后的工厂，调用方必须核对它真的指向副本。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import src.models.database as models

    engine = create_engine('sqlite:///' + copy_path.replace('\\', '/'))
    # `class_` 要照抄生产那份工厂（`_RetrySession`，见 `src/models/database.py:120`）：
    # 裸 sessionmaker 会让副本上的会话形态与生产不同，重放出的"一致"就不算数
    # （第 16 轮 BLOCKER-1 的附带项）。
    kwargs = {'bind': engine}
    original_class = getattr(models.SessionLocal, 'class_', None)
    if original_class is not None:
        kwargs['class_'] = original_class
    for key in ('expire_on_commit', 'autoflush'):
        if key in getattr(models.SessionLocal, 'kw', {}):
            kwargs[key] = models.SessionLocal.kw[key]
    models.SessionLocal = sessionmaker(**kwargs)
    return models.SessionLocal


def _assert_factories_bound_to(copy_path):
    """进程内**所有** `SessionLocal` 引用都必须绑在副本上，返回还指向别处的模块。

    第 16 轮 BLOCKER-1：上一版这里写的是 `hasattr(factory, 'bind')`，而 SQLAlchemy 2.0
    的 `sessionmaker` **没有** `.bind`（`.bind` 在 Session 实例上），于是每个候选都被
    skip、函数永远返回空列表 —— 我修一条同义反复自检，转头又写了一条更隐蔽的。
    工厂上的绑定要从 `factory.kw['bind']` 拿。
    真正会重演事故的是：某个模块在换绑之前就把旧工厂抓进了自己的命名空间 ——
    `src/fund/fund_api.py`、`src/fund/fund_sync_manager.py`、`src/models/__init__.py`
    都是模块级 `from src.models.database import SessionLocal`，它们写起来照样落在源库上。
    """
    import sys

    needle = os.path.basename(copy_path).lower()
    stale = []
    for module in list(sys.modules.values()):
        factory = getattr(module, 'SessionLocal', None)
        bind = getattr(factory, 'kw', {}).get('bind') if factory is not None else None
        if bind is None:
            continue                       # 不是 sessionmaker 工厂（或没绑东西）
        url = str(getattr(bind, 'url', bind))
        if needle not in url.lower():
            stale.append('%s → %s' % (getattr(module, '__name__', '?'), url))
    return stale


def _fingerprint(path):
    """源库文件的 (大小, sha256)：重放结束后再算一遍，变了就说明保护失效过。"""
    import hashlib

    stat = os.stat(path)
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return (stat.st_size, digest.hexdigest())


def main():
    ap = argparse.ArgumentParser(description='库副本上的验证重放（判据漂移闸门）')
    ap.add_argument('--limit', type=int, default=30)
    ap.add_argument('--ids', help='逗号分隔的 prediction id，指定后忽略 --limit')
    ap.add_argument('--offline', action='store_true',
                    help='副本上不许打数据源（默认打，因为补拉是验证流程的一部分）')
    ap.add_argument('--keep', action='store_true', help='保留副本库文件')
    ap.add_argument('--json', help='把逐条对照写成 JSON')
    args = ap.parse_args()

    src = _sqlite_path(pin_local_sqlite(use_mirror_default=True))     # 必须在 import ORM 之前
    if not os.path.exists(src):
        print('[abort] 找不到源库：%s' % src)
        return 4
    tmp = os.path.join(tempfile.gettempdir(), 'fi_replay_%s.db' % date.today().isoformat())
    shutil.copy2(src, tmp)
    os.environ['DATABASE_URL'] = 'sqlite:///' + tmp.replace('\\', '/')
    print('[副本] %s → %s' % (src, tmp))
    # 源库指纹：副本保护失效时（首版就是这么把 88 行写进镜像库的）唯一能**事后**
    # 抓住它的证据就是"源文件变了"。绑定自检挡得住"工厂指错库"，挡不住"某个模块
    # 在换绑前就把旧工厂抓进自己命名空间"，所以两道都要。
    src_fp = _fingerprint(src)

    SessionLocal = _bind_session_factory_to(tmp)
    from src.models.database import Prediction

    bound = str(SessionLocal().bind.url)
    if os.path.basename(tmp) not in bound:
        print('[abort] 换绑后的工厂指向 %s 而不是副本 %s —— 拒绝运行' % (bound, tmp))
        return 5
    print('[副本] 会话已绑定副本：%s' % bound)

    from src.services.prediction_verify_service import PredictionVerifyService

    if args.offline:
        import importlib

        import requests as _requests

        # `from src.fund import fund_api as fa` 拿到的是 **`FundAPI` 实例** ——
        # `src/fund/__init__.py` 把包属性 `fund_api` 重绑成了实例，在它身上赋值
        # `fund_data_manager` 等于什么都没做，而验证侧走的是
        # `from src.fund.fund_api import fund_data_manager`（函数级 import，取模块属性）。
        # 第 16 轮 BLOCKER-1 实测：号称 `--offline` 的重放真的打了东财 `f10/lsjz`，
        # 副本里凭空多出镜像没有的 4 行净值和一条凭据 ⇒ 之前那句"未解释漂移=0"
        # 根本不是可复现判据。要改模块属性必须走 sys.modules。
        fa = importlib.import_module('src.fund.fund_api')

        class _NoNet:
            @staticmethod
            def backfill_history_range(*a, **kw):
                return 0
        fa.fund_data_manager = _NoNet()

        # 只桩调用点不够（`get_nav_by_date` 还有接口兜底等多条取数路径）：
        # 直接把 HTTP 层掐掉，任何漏网的取数都会变成**看得见的异常**，
        # 而不是悄悄改掉结论。
        def _refuse(self, request, *args, **kwargs):
            raise RuntimeError('--offline 禁止真实外呼：%s' % getattr(request, 'url', request))
        _requests.Session.send = _refuse

        # `--offline` 还必须关掉 LLM 那一腿，否则"未解释漂移=0"是环境依赖的：
        # 边界分（20~80）、涨跌幅在阈值附近、方向相反这几类都会去问 LLM 改判
        # （`prediction_verify_service.py:1009-1072`），有密钥时闸门带随机性并花配额。
        # 关掉之后本闸门只复现**确定性判据**，差异归因才说得清（第 15 轮 MAJOR-3）。
        class _NoLLM:
            def verify_prediction(self, *a, **kw):
                return None
        PredictionVerifyService.llm_analyzer = property(lambda self: _NoLLM())
        print('[offline] 已掐 requests 外呼 + 断数据源补拉 + 断 LLM 复核腿：只复现确定性判据')

    # 进程内**所有** SessionLocal 引用都必须指向副本（见 `_assert_factories_bound_to`）：
    # 检查放在这里，是因为要被点名的模块（`src/fund/fund_api.py`、
    # `src/fund/fund_sync_manager.py` 都是模块级 `from src.models.database import SessionLocal`）
    # 到这会儿才全部 import 完。
    stale = _assert_factories_bound_to(tmp)
    if stale:
        print('[abort] 这些模块手里的会话工厂还指着副本之外，重放会写进源库：\n   %s'
              % '\n   '.join(stale))
        return 5

    probe = SessionLocal()
    wanted = [int(x) for x in args.ids.split(',')] if args.ids else []
    rows, deferred = _sample(probe, Prediction, args.limit, wanted, offline=args.offline)
    if args.offline:
        # 端点证据已经与"当前标的的净值表"对不上的行，重放必然判得不同 —— 那是数据问题
        # （判据住在 `src/services/verdict_evidence.py`，与前端标记、每日跑批共用一份），
        # 不是判据回归。继续放进比对只会把真信号埋掉（第 17 轮 MAJOR：上一轮
        # "未解释 1"里藏着 9 条方向翻转），所以**让出并如实报数**：既不解释也不计分母。
        from src.services.verdict_evidence import evidence_statuses

        statuses = evidence_statuses(probe, rows)
        stale = [p for p in rows if statuses.get(p.id)]
        rows = [p for p in rows if not statuses.get(p.id)]
        if stale:
            print('[离线] 让出 %d 条"端点证据已与当前标的对不上"的预测（数据侧缺陷，'
                  '重放判不同属正常）：%s' % (len(stale), [p.id for p in stale][:20]))
        deferred = deferred + stale
    ids = [p.id for p in rows]
    expected = {p.id: {f: getattr(p, f) for f in VERDICT_FIELDS} for p in rows}
    print('[抽样] 已验证预测 %d 条重放' % len(ids))
    if deferred:
        print('[离线] 共让出 %d 条：重放不含 LLM 那一腿、或端点证据已失效的行，'
              '既不计入"可解释"也不计入"未解释"（要核这批就带密钥跑一次不加 --offline）：%s'
              % (len(deferred), [p.id for p in deferred][:20]))
    probe.close()

    db = SessionLocal()
    service = PredictionVerifyService(db)
    # 副本的"取证据"基线：离线模式下这次跑批不该新增任何净值或凭据。
    # 这是 B-1 那类"桩其实是空操作"的**唯一硬证据**（打印出来的一切声明都替代不了它）。
    from src.models.database import FundHistory, SystemConfig

    def _copy_evidence_counts():
        """副本的"取数证据"指纹：行数 + 凭据条数 + 净值内容合计。

        只数行数对 `update_fund_history` 那种**就地改写 nav** 完全失明（第 17 轮 MINOR-2），
        所以再压一个 SUM(nav)：任何被改写过的净值都会让它变。
        """
        from sqlalchemy import func

        hist = db.query(FundHistory).count()
        total = db.query(func.sum(FundHistory.nav)).scalar() or 0.0
        proofs = db.query(SystemConfig).filter(
            SystemConfig.config_key.like('nav_backfill_proof:%')).count()
        return (hist, round(float(total), 4), proofs)

    evidence0 = _copy_evidence_counts()
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
                           'unexplained': unexplained, 'errors': errors,
                           'deferred_llm_leg': [p.id for p in deferred]},
                          f, ensure_ascii=False, indent=1)
            print('[ok] 对照明细：%s' % args.json)
        rc = 3 if unexplained or errors else 0
        if args.offline:
            if _copy_evidence_counts() != evidence0:
                print('[!!] 离线重放改变了副本的取数证据：%s → %s\n'
                      '     （净值行数 / SUM(nav) / 凭据条数）说明"断网"里有桩没盖住的路径'
                      '—— 第 16 轮 BLOCKER-1 就是这个形状：桩打在了被重绑的包属性上，'
                      '那次号称离线的跑批真的取了新数据。这次的差异归因不可信。'
                      % (evidence0, _copy_evidence_counts()))
                rc = 7
            else:
                print('[ok] 离线重放未新增、未改写任何净值/凭据（证据指纹 %s 未变）'
                      % (evidence0,))
        # 事后证据：源库文件一个字节都不该变。工厂扫描挡"指向错库"，这一道挡
        # "某个模块早就抓走了旧工厂"这类扫不到的写法 —— 首版事故就是它把 88 行
        # 写进了镜像库，当时两道检查都没有（第 15 轮 m-5）。
        try:
            if _fingerprint(src) != src_fp:
                print('[!!] 源库文件在这次重放里被改动了：%s\n'
                      '     副本保护失效，这次结果不能当"只动了副本"用，先核对镜像库。' % src)
                rc = 6
            else:
                print('[ok] 源库未被改动（大小与 sha256 与开跑前一致）：%s' % os.path.basename(src))
        except OSError as exc:
            print('[warn] 源库指纹读不了，无法证明没被写：%s' % exc)
            rc = rc or 6
        return rc
    finally:
        db.close()
        if not args.keep and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                print('[warn] 副本没删掉（可能还有连接）：%s' % tmp)


if __name__ == '__main__':
    raise SystemExit(main())
