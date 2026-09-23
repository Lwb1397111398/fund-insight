"""
预置板块-基金映射数据
把内置表 + 额外热门板块里**库里还没有**的板块补进 SectorFundMapping
（`reviewed=False` + `match_source='seed_builtin'`，交给身份体检与人工审查再决定是否盖章）

第 36 轮 B-MAJOR-1：旧版无条件写 `reviewed=True` 并就地改已有行的 `fund_code`，
在镜像上一次会"改 21 行 + 新建 128 行全部已审查" ⇒ 现在只补缺、不覆盖，真写要 --confirm。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _db_guard import pin_local_sqlite as _pin_local_db
_pin_local_db(use_mirror_default=True)   # 钉死本地镜像库：.env 的 DATABASE_URL 指向生产，先 import ORM 就会连线上

from src.models.database import SessionLocal, SectorFundMapping, init_db

CONFIRM_TOKEN = 'SEED-MAP'

# 额外的热门板块（不在硬编码表中的）
EXTRA_MAPPINGS = [
    # 新兴科技
    ('低空经济', '159852', '低空经济ETF'),
    ('算力', '159515', '算力ETF'),
    ('数据要素', '159523', '数据ETF'),
    ('信创', '562570', '信创ETF'),
    ('区块链', '159645', '区块链ETF'),
    ('元宇宙', '159786', '元宇宙ETF'),
    ('鸿蒙', '159768', '鸿蒙概念ETF'),
    ('卫星互联网', '159819', '卫星ETF'),
    ('量子计算', '159819', '卫星ETF'),
    ('AI PC', '159732', '消费电子ETF'),
    ('AI手机', '159732', '消费电子ETF'),
    ('CPO', '515880', '通信ETF'),
    ('光模块', '515880', '通信ETF'),
    ('HBM', '512480', '半导体ETF'),
    ('先进封装', '512480', '半导体ETF'),
    ('AIGC', '515070', '人工智能ETF'),
    ('Sora', '515070', '人工智能ETF'),
    ('智能驾驶', '516110', '汽车ETF'),
    ('无人驾驶', '516110', '汽车ETF'),
    ('人形机器人', '562500', '机器人ETF'),
    ('固态电池', '159840', '锂电池ETF'),
    # 消费
    ('旅游', '159766', '旅游ETF'),
    ('免税', '159766', '旅游ETF'),
    ('酒店', '159766', '旅游ETF'),
    ('零食', '159928', '消费ETF'),
    ('预制菜', '159928', '消费ETF'),
    ('啤酒', '512690', '酒ETF'),
    ('医美', '159898', '医疗器械ETF'),
    # 农业养殖
    ('农业', '159825', '农业ETF'),
    ('养殖', '159865', '养殖ETF'),
    ('猪肉', '159865', '养殖ETF'),
    ('猪', '159865', '养殖ETF'),
    ('鸡肉', '159865', '养殖ETF'),
    ('种子', '159825', '农业ETF'),
    ('化肥', '159870', '化工ETF'),
    # 汽车
    ('汽车', '516110', '汽车ETF'),
    ('智能汽车', '516110', '汽车ETF'),
    ('汽车零部件', '516110', '汽车ETF'),
    # 地产链
    ('建材', '159619', '基建ETF'),
    ('水泥', '159619', '基建ETF'),
    ('玻璃', '159619', '基建ETF'),
    ('家居', '159996', '家电ETF'),
    ('装修', '159996', '家电ETF'),
    # 医药细分
    ('CXO', '512010', '医药ETF'),
    ('减肥药', '515120', '创新药ETF'),
    ('阿尔兹海默', '515120', '创新药ETF'),
    ('血制品', '512290', '生物医药ETF'),
    # 周期资源
    ('锡', '512400', '有色金属ETF'),
    ('镍', '512400', '有色金属ETF'),
    ('钴', '512400', '有色金属ETF'),
    ('锂', '159840', '锂电池ETF'),
    ('铁矿石', '515210', '钢铁ETF'),
    # 金融细分
    ('AMC', '512880', '证券ETF'),
    ('不良资产', '512880', '证券ETF'),
    # 军工细分
    ('航天', '512660', '军工ETF'),
    ('导弹', '512660', '军工ETF'),
    ('无人机', '512660', '军工ETF'),
    ('大飞机', '512660', '军工ETF'),
    # 其他
    ('养老', '159928', '消费ETF'),
    ('体育', '159928', '消费ETF'),
    ('彩票', '512980', '传媒ETF'),
    ('网红经济', '512980', '传媒ETF'),
    ('直播', '512980', '传媒ETF'),
    ('短剧', '512980', '传媒ETF'),
]


def seed_mappings(dry_run=False, confirm=None, db=None):
    """把内置表里**库里还没有**的板块落成映射行（`reviewed=False` + `match_source='seed_builtin'`）。

    第 36 轮 B-MAJOR-1 把旧语义量出来了：它曾无条件 `reviewed=True` 并**就地改 `fund_code`**，
    在镜像上一次就是"改 21 行（其中 15 行已审查、8 行署名 agent）+ 新建 128 行全部已审查"。
    所以这里改成只补缺、不覆盖：已存在的行交回给体检与人工审查那条路（`retag_prediction` 才是
    唯一允许改标的的入口）。新行也一律 `reviewed=False` —— 门禁要求 `reviewed=1` 必须带
    `match_source + verified_at + confidence`，脚本给不出这些，就不该盖章。
    """
    if not dry_run and confirm != CONFIRM_TOKEN:
        print('[abort] 这会往库里插映射行。真写必须 --confirm %s（只看计划加 --dry-run）'
              % CONFIRM_TOKEN)
        return 4

    from src.services.verdict_evidence import database_label

    owns_session = db is None
    if owns_session:
        if not dry_run:
            # dry-run 不碰结构：只读计划不该有建表这个副作用
            init_db()
        db = SessionLocal()
    print('[库] %s' % database_label(db))
    added = skipped_existing = owner_backed = code_diff = 0
    planned_new = []
    try:
        from src.constants.sector_fund_map import SECTOR_FUND_MAP
        plan = []
        for sector_name, info in SECTOR_FUND_MAP.items():
            plan.append((sector_name, info.get('code', ''), info.get('name', '')))
        for sector_name, code, name in EXTRA_MAPPINGS:
            if sector_name not in SECTOR_FUND_MAP:
                plan.append((sector_name, code, name))

        for sector_name, fund_code, fund_name in plan:
            existing = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == sector_name).first()
            if existing is not None:
                skipped_existing += 1
                if existing.fund_code != fund_code:
                    code_diff += 1
                    print('  [跳过：不覆盖] %s 库里是 %s，内置表想要 %s（要改标请走页面/体检）'
                          % (sector_name, existing.fund_code, fund_code))
                if getattr(existing, 'owner_locked', None) or \
                        (getattr(existing, 'reviewed_by', None) or '') == 'owner':
                    owner_backed += 1
                    print('  [跳过：老板已确认] %s → %s' % (sector_name, existing.fund_code))
                continue
            if dry_run:
                planned_new.append('%s→%s' % (sector_name, fund_code))
                added += 1
                continue
            db.add(SectorFundMapping(sector_name=sector_name, fund_code=fund_code,
                                     fund_name=fund_name, reviewed=False,
                                     match_source='seed_builtin', is_active=True))
            added += 1
            print('  [新增，待审查] %s → %s (%s)' % (sector_name, fund_name, fund_code))

        print('\nSEED-RECEIPT: %s 新增=%d 已存在跳过=%d 其中码不一致=%d 老板行=%d 内置计划=%d'
              % ('dry-run' if dry_run else '真写', added, skipped_existing, code_diff,
                 owner_backed, len(plan)))
        for item in planned_new:
            print('   将新建 %s' % item)
        if dry_run:
            print('dry-run：未写库。真写：--confirm %s' % CONFIRM_TOKEN)
            db.rollback()
            return 0
        db.commit()
        print('完成: 新增 %d（全部 reviewed=False 待审查）, 跳过已存在 %d' % (added, skipped_existing))
        print('内置表: %d 条, 额外板块: %d 条' % (len(SECTOR_FUND_MAP) + len(EXTRA_MAPPINGS), len(EXTRA_MAPPINGS)))
        return 0

    except Exception as e:
        db.rollback()
        print('失败: %s' % e)
        raise
    finally:
        if owns_session:
            db.close()


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='把内置板块表里库里没有的行补进 sector_fund_mapping（只补缺、不覆盖）')
    ap.add_argument('--dry-run', action='store_true', help='只出计划，不写库')
    ap.add_argument('--confirm', help='真写必须等于 %s' % CONFIRM_TOKEN)
    args = ap.parse_args()
    raise SystemExit(seed_mappings(dry_run=args.dry_run, confirm=args.confirm))
