# -*- coding: utf-8 -*-
"""把 `export_repaired_mappings.py` 生成的清单**定向**回写到生产（整批一次调用）。

为什么走专用接口而不是逐行 PUT：`PUT /api/config/sector-mappings/{id}` 的请求模型
`MappingUpdate` 只有 fund_code/fund_name 两列，Pydantic 会**静默丢掉**其余 11 个审计字段
（reviewed / owner_locked / reviewed_by / is_fetchable / match_source / match_kind /
confidence / verify_message / llm_reason / evidence / keywords），而
`service.update_mapping()` 当时还会把碰到的每行标成 `reviewed=True + owner_locked=True +
reviewed_by='owner'`（第 18 轮 MAJOR-1 后只给 `reviewed=True`，署名与锁定要显式
`owner_confirm`；但用它回写审计字段这件事仍然不成立）。实测过一遍：13 个字段只有 2 个落地，
降级旗标一个都没送到，行还被买到永久锁定（从此免于体检与 agent），脚本却照样打印"成功 N"。
现在整批发给 `POST /api/config/sector-mappings/-/audit-import`，由服务端按 sector_name
寻址、逐列照搬审计结论，并逐行回执 updated/created/unchanged/refused(原因)。

三道硬闸，缺一不动：
1. 默认 dry-run，只让服务端出计划；真写必须 `--confirm WRITE-TO-PROD`
   （它会原样进请求的 confirm 字段，服务端没有这个字面量就自动退回计划）；
2. 只按 `sector_name` 匹配，清单里的本地 id 一律不发 —— 两边 id 不同，按 id 写会写错行；
3. 每一行都读服务端回执：**老板锁定/署名的行会被服务端拒绝覆盖**（那是有意代理，
   如 债券→512000、SpaceX→159206），这类拒绝是预期的、只汇报；其余拒绝或写失败
   一律非 0 退出，不做静默跳过。绝不删除任何行。

口令只从环境变量 `ACCESS_PASSWORD` 读，绝不出现在命令行、代码或日志里。

    ACCESS_PASSWORD=... python scripts/push_sector_mappings_to_prod.py            # 看计划
    ACCESS_PASSWORD=... python scripts/push_sector_mappings_to_prod.py --limit 5  # 小批试算
    ACCESS_PASSWORD=... python scripts/push_sector_mappings_to_prod.py --confirm WRITE-TO-PROD
"""
import argparse
import io
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, 'docs', '迭代计划', 'run-2026-09-20',
                        'prod-writeback-sector-mappings.json')
DEFAULT_BASE = 'https://fund-insight.onrender.com'
# 已知生产域名的判据（第 43 轮 B-MAJOR-5）：`--base` 是可以随手指的，
# 而那一行自报以前**无条件**写"经 HTTP 写线上生产库"。B 席实测：
# `--base http://127.0.0.1:9/` ⇒ 屏幕上出现"[目标] 127.0.0.1:9 —— 经 HTTP 写**线上生产库**"，
# 与上一行"目标=http://127.0.0.1:9/"自相矛盾。这句话是操作者决定要不要按 `--confirm` 的依据，
# 它说错方向比不说更坏。
PROD_HOST_MARKS = ('onrender.com',)


def _target_line(base):
    host = urlparse(base).netloc
    known = host == urlparse(DEFAULT_BASE).netloc or any(m in host for m in PROD_HOST_MARKS)
    if known:
        return ('[目标] %s —— 经 HTTP 写**线上生产库**的写入口'
                '（不是本地镜像 `data/fund_insight.db`）' % host)
    return ('[目标] %s —— ⚠ **这不是已知的生产域名**（已知的只有 %s）。'
            '所以这一行不敢自称"动的是线上库"：HTTP 写的落点是**那个后端自己连的库**'
            '（本机 `serve_mirror.py` 就是镜像）。要写生产请把 --base 指回 %s。'
            % (host, urlparse(DEFAULT_BASE).netloc, DEFAULT_BASE))
ENDPOINT = '/api/config/sector-mappings/-/audit-import'
CONFIRM = 'WRITE-TO-PROD'

# 与 src/api/routes/config.py 的 AuditMappingRow 对齐；sector_name 用于寻址。
ROW_FIELDS = ('sector_name', 'fund_code', 'fund_name', 'keywords', 'is_active', 'reviewed',
              'match_source', 'match_kind', 'confidence', 'verified_at', 'verify_message',
              'llm_reason', 'is_fetchable', 'evidence', 'reviewed_by', 'owner_locked')

# 服务端这些拒因是**设计如此**（老板的行机器不覆盖），只汇报不当失败。
EXPECTED_REFUSALS = ('owner_locked', 'reviewed_by_owner')
REASON_LABEL = {
    'owner_locked': '老板已锁定',
    'reviewed_by_owner': '老板署名已审查',
    'empty_sector_name': '板块名为空',
    'empty_fund_code': '基金代码为空',
    'bad_evidence_json': '证据不是合法 JSON',
    'bad_verified_at': 'verified_at 不是合法时间',
    'evidence_too_large': '证据过大',
}


def request(base, path, password, payload=None, method='GET', timeout=180):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(base.rstrip('/') + path, data=data, method=method,
                                 headers={'X-Access-Password': password,
                                          'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8', 'replace') or '{}')
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', 'replace')[:400]
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {'raw': body}
    except Exception as exc:
        return 0, {'error': str(exc)[:200]}


def build_rows(data, limit):
    """清单行 → 请求体：只带审计字段，**丢掉本地 id**（两边 id 不同，按 id 写会写错行）。

    显式 null 必须保留：`reviewed_by=None` 就是"退回未审查"的语义，
    服务端靠"字段在不在"区分照搬 null 与不动现值。
    """
    rows = []
    for m in (data.get('mappings') or [])[:limit]:
        rows.append({f: m.get(f) for f in ROW_FIELDS})
    return rows


def show_items(items, limit=15):
    interesting = [i for i in items if i.get('outcome') in ('updated', 'created', 'refused')]
    for item in interesting[:limit]:
        extra = ''
        if item['outcome'] == 'refused':
            reason = item.get('reason') or ''
            label = REASON_LABEL.get(reason, reason)
            extra = '（现 %s）' % item.get('current_fund_code') if reason in EXPECTED_REFUSALS else ''
            print('   [拒·%s] %-12s %s%s' % (label, item['sector_name'],
                                              item.get('fund_code') or '', extra))
        else:
            print('   [%s] %-12s → %s 改 %d 列：%s'
                  % ('新建' if item['outcome'] == 'created' else '更新',
                     item['sector_name'], item.get('fund_code'),
                     len(item.get('changed_fields') or ()),
                     '、'.join((item.get('changed_fields') or [])[:6])))
    if len(interesting) > limit:
        print('   ...其余 %d 行省略' % (len(interesting) - limit))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default=os.getenv('APP_BASE_URL', DEFAULT_BASE))
    ap.add_argument('--confirm', default='', help='必须等于 %s 才真写' % CONFIRM)
    ap.add_argument('--limit', type=int, default=None, help='只处理前 N 行（先小批验证）')
    ap.add_argument('--allow-unservable', action='store_true',
                    help='连"生产压根没有这只基金档案/净值"的行也一起发（默认剔除，见检查单 §7）')
    args = ap.parse_args()

    password = os.getenv('ACCESS_PASSWORD', '')
    if not password:
        print('[abort] 环境变量 ACCESS_PASSWORD 未设置：口令只走环境变量，不进命令行/代码')
        return 2
    if not os.path.exists(MANIFEST):
        print('[abort] 没有清单，先跑 python scripts/export_repaired_mappings.py')
        return 2
    data = json.load(io.open(MANIFEST, encoding='utf-8'))
    rows = build_rows(data, args.limit)
    dropped = 0          # 预检剔掉的行数；回执里必须说出来，不然"已落在被审计的状态"是假话
    apply_write = args.confirm == CONFIRM
    print('[计划] 目标=%s 清单 %d 行（指纹 %s，生成于 %s）→ %s'
          % (args.base, len(rows), data.get('sha256'), data.get('generated_at'),
             '真写' if apply_write else 'dry-run'))
    print(_target_line(args.base))

    # 真写之前先问服务端"这些标的在你这儿定得了价"。
    # 为什么不能信清单：`is_fetchable` 是**在镜像上**算的，而第 27 轮生产实测有 31 行
    # 连 `fund_info` 档案都没有（压着 249 条活预测）—— 拿清单当闸门会把这 31 行全放成"可服务"。
    if apply_write:
        pre_status, pre_body = request(args.base, ENDPOINT, password,
                                       {'mappings': rows, 'dry_run': True}, method='POST')
        if pre_status != 200 or not isinstance(pre_body, dict) or 'data' not in pre_body:
            print('[abort] 预检没走通（HTTP %s），一行都不发：%s'
                  % (pre_status, str(pre_body)[:200]))
            return 3
        items = pre_body['data'].get('items') or []
        # 第 38 轮 B 席 MAJOR：判据原先只认 `nav_priced_here is False` ⇒
        # 对端是"有 audit-import 但还没这一列"的旧版时，**每一行都算过了闸**，
        # 于是这条预检在最需要它的时候是开着的。"没回答"与"回答不可服务"不是一回事，
        # 但两者都不许真写：拒收要显式，不许静默放行。
        missing = [i for i in items if 'nav_priced_here' not in i]
        # 第 39 轮两份同点（B-MINOR / A-MINOR-6）：服务端在写这一列**之前**就有几条
        # `continue`（空板块名、字段超长、evidence 太大/坏 JSON…），那种行天生不带这列。
        # 旧写法把它们一起算成"预检没答完"，于是整批 145 行退码 3，消息还把人支去升级生产。
        # 现在分开：**没给理由**才算旧构建（拒发整批）；**给了理由**的是服务端已经拒收的行，
        # 剔掉它们继续，并把剔除数计进 `dropped`，回执里说得出总数。
        refused = [i for i in missing if i.get('reason')]
        silent = [i for i in missing if not i.get('reason')]
        if silent or len(items) != len(rows):
            print('[abort] 预检没答完：回执 %d 行 / 发出 %d 行，其中 %d 行既没带 `nav_priced_here`'
                  ' 也没给理由 —— 一行都不发（对端多半是还没上"能不能定价"那一列的旧构建）'
                  % (len(items), len(rows), len(silent)))
            return 3
        if refused:
            print('[预检] 服务端在"能不能定价"之前就拒了 %d 行（行本身被挡，不是定不了价）：' % len(refused))
            for i in refused[:12]:
                print('   %-14s %-8s %s' % (i.get('sector_name'), i.get('fund_code'), i.get('reason')))
            blocked = {i.get('sector_name') for i in refused}
            before = len(rows)
            rows = [r for r in rows if r.get('sector_name') not in blocked]
            dropped += before - len(rows)
            print('[预检] 已剔除这 %d 行（它们本来就写不进去），剩 %d 行继续'
                  % (before - len(rows), len(rows)))
        bad = [(i.get('sector_name'), i.get('fund_code'), i.get('nav_priced_here_note'))
               for i in items
               if i.get('nav_priced_here') is False]
        if bad:
            print('[预检] 生产定不了价的 %d 行（发过去就是造出无法定价的映射）：' % len(bad))
            for sector, code, note in bad[:40]:
                print('   %-14s %-8s %s' % (sector, code, note))
            if len(bad) > 40:
                print('   ...其余 %d 行省略' % (len(bad) - 40))
            if not args.allow_unservable:
                drop = {s for s, _c, _n in bad}
                # 两路剔除用**同一把尺**（第 41 轮 A-m3 / B-MINOR-4）：上面那路量的是
                # `before - len(rows)`（行数），这一路以前量 `len(drop)`（去重后的板块名）。
                # 今天 145 行=145 板块所以两个数相等，一旦出现"同板块两行"就会少报剔除数。
                before = len(rows)
                rows = [r for r in rows if r.get('sector_name') not in drop]
                dropped += before - len(rows)
                print('[预检] 已剔除 %d 行（涉及 %d 个板块），剩 %d 行待发；'
                      '确实要把不可服务的行也发上去，加 --allow-unservable'
                      % (before - len(rows), len(drop), len(rows)))
                if not rows:
                    print('[abort] 剔除后没有可发的行了')
                    return 5

    # 第 40 轮 B 的 M-2：上面"早期拒收"那一格剔完没有"剔光了就停"的闸（"定不了价"那一格有），
    # 于是整批被拒时仍会发出一个 0 行的真写请求、打印"[完成] 生产已落在被审计的状态"、退码 0
    # —— 一次什么都没写进去的生产回写以成功收场，按退码判断的人（包括下一轮的我）会记成"已回写"。
    if apply_write and not rows:
        print('[abort] 预检把这批 %d 行全剔完了（早期拒收 / 生产定不了价），一行都没发 —— '
              '这不是"完成"，是按计划什么都没写。逐行原因见上面两段预检清单。' % dropped)
        return 5

    status, body = request(args.base, ENDPOINT, password,
                           {'mappings': rows, 'dry_run': not apply_write,
                            'confirm': CONFIRM if apply_write else None},
                           method='POST')
    if status in (404, 405):
        print('[abort] 生产尚无审计回写接口（HTTP %s）：先把本轮代码部署上去再回写' % status)
        return 3
    if status == 422:
        print('[abort] 生产不接受该请求体（HTTP 422），多半是接口版本不匹配：%s'
              % str(body)[:220])
        return 3
    if not isinstance(body, dict) or status != 200 or 'data' not in body:
        print('[abort] 回写接口未正常响应（HTTP %s）：%s' % (status, str(body)[:220]))
        return 3

    counts = (body.get('data') or {}).get('counts') or {}
    items = (body.get('data') or {}).get('items') or []
    reasons = (body.get('data') or {}).get('refused_reasons') or {}
    print('[回执] 服务端：%s' % body.get('message', ''))
    print('       updated=%s created=%s unchanged=%s refused=%s（written=%s，dry_run=%s）'
          % (counts.get('updated'), counts.get('created'), counts.get('unchanged'),
             counts.get('refused'), body.get('written'), body.get('dry_run')))
    if reasons:
        print('       拒因：%s' % '、'.join(
            '%s×%d' % (REASON_LABEL.get(k, k), v) for k, v in reasons.items()))
    show_items(items)
    if body.get('dry_run'):
        print('\n[dry-run] 未写生产。确认无误后加 --confirm %s（建议先 --limit 5 小批验证）'
              % CONFIRM)
        if not apply_write:
            return 0
        # 带了 confirm 却被服务端退回 dry-run：口令/字段没对上，必须当失败处理
        print('[abort] 已带 --confirm 但服务端仍判为 dry-run（confirm_ok=%s）'
              % body.get('confirm_ok'))
        return 3

    unexpected = [i for i in items if i.get('outcome') == 'refused'
                  and (i.get('reason') not in EXPECTED_REFUSALS)]
    owner_rows = [i for i in items if i.get('outcome') == 'refused'
                  and i.get('reason') in EXPECTED_REFUSALS]
    if owner_rows:
        print('[保留] %d 行是老板锁定/署名的有意代理，服务端已按规则不覆盖' % len(owner_rows))
    if unexpected:
        print('[异常] %d 行被服务端拒绝或写失败，请逐条核对后重跑：' % len(unexpected))
        for i in unexpected[:20]:
            print('   %-12s %s → %s：%s' % (i['sector_name'], i.get('mapping_id'),
                                             i.get('fund_code'), i.get('reason')))
        return 4
    print('[完成] 生产已落在被审计的状态：更新 %d、新建 %d、本就一致 %d%s'
          % (counts.get('updated', 0), counts.get('created', 0), counts.get('unchanged', 0),
             '；另有 %d 行**没发**（早期拒收 / 生产定不了价，逐行原因见上面预检清单）' % dropped
             if dropped else ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
