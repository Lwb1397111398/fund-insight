"""
预置板块-基金映射数据
将硬编码表 + 额外热门板块写入 SectorFundMapping 表（reviewed=True）
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.database import SessionLocal, SectorFundMapping, init_db

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


def seed_mappings():
    """将硬编码表 + 额外板块导入数据库（全部 reviewed=True）"""
    init_db()
    db = SessionLocal()

    try:
        added = 0
        skipped = 0
        updated = 0

        # 1. 导入硬编码表 SECTOR_FUND_MAP 的所有条目
        from src.constants.sector_fund_map import SECTOR_FUND_MAP
        for sector_name, fund_info in SECTOR_FUND_MAP.items():
            fund_code = fund_info.get('code', '')
            fund_name = fund_info.get('name', '')

            existing = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == sector_name
            ).first()

            if existing:
                if existing.fund_code == fund_code and existing.reviewed:
                    skipped += 1
                else:
                    existing.fund_code = fund_code
                    existing.fund_name = fund_name
                    existing.is_active = True
                    existing.reviewed = True
                    updated += 1
                    print(f"  [更新] {sector_name}: → {fund_name}({fund_code})")
            else:
                mapping = SectorFundMapping(
                    sector_name=sector_name,
                    fund_code=fund_code,
                    fund_name=fund_name,
                    reviewed=True
                )
                db.add(mapping)
                added += 1
                print(f"  [新增] {sector_name} → {fund_name} ({fund_code})")

        # 2. 导入额外的热门板块
        for sector_name, fund_code, fund_name in EXTRA_MAPPINGS:
            existing = db.query(SectorFundMapping).filter(
                SectorFundMapping.sector_name == sector_name
            ).first()

            if existing:
                if existing.fund_code == fund_code and existing.reviewed:
                    skipped += 1
                else:
                    existing.fund_code = fund_code
                    existing.fund_name = fund_name
                    existing.is_active = True
                    existing.reviewed = True
                    updated += 1
                    print(f"  [更新] {sector_name}: → {fund_name}({fund_code})")
            else:
                mapping = SectorFundMapping(
                    sector_name=sector_name,
                    fund_code=fund_code,
                    fund_name=fund_name,
                    reviewed=True
                )
                db.add(mapping)
                added += 1
                print(f"  [新增] {sector_name} → {fund_name} ({fund_code})")

        db.commit()
        print(f"\n完成: 新增 {added}, 更新 {updated}, 跳过 {skipped}")
        print(f"硬编码表: {len(SECTOR_FUND_MAP)} 条, 额外板块: {len(EXTRA_MAPPINGS)} 条")

    except Exception as e:
        db.rollback()
        print(f"失败: {e}")
        raise
    finally:
        db.close()


if __name__ == '__main__':
    seed_mappings()
