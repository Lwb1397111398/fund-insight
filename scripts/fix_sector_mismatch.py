"""
修复板块匹配错误的预测
从预测内容中重新提取正确的板块和基金
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Prediction, Post, FundInfo
from datetime import date


SECTOR_KEYWORDS = {
    '化工': ['化工', '化学', '化纤', '聚氨酯', 'PTA', 'PVC', '纯碱', '烧碱', '甲醇'],
    '石油': ['石油', '原油', '油气', '页岩油', '石油气', '天然气', 'LNG'],
    '煤炭': ['煤炭', '焦煤', '焦炭', '动力煤'],
    '电力': ['电力', '电网', '发电', '水电', '火电', '核电', '绿电', '绿色电力', '电力设备'],
    '半导体': ['半导体', '芯片', '集成电路', '晶圆', '光刻', 'GPU', 'CPU', '算力芯片'],
    '人工智能': ['AI', '人工智能', '机器学习', '深度学习', '大模型', 'GPT', 'LLM'],
    '消费电子': ['消费电子', '手机', '折叠屏', 'iPhone', '智能手表'],
    '通信': ['通信', '5G', '6G', '光通信', 'CPO', 'NPO', '光纤'],
    '新能源': ['新能源', '光伏', '储能', '锂电池', '逆变器', '硅料', '硅片'],
    '电池': ['电池', '锂电池', '钠电池', '固态电池', '动力电池'],
    '机器人': ['机器人', '人形机器人', 'Optimus', '工业机器人'],
    '有色金属': ['有色', '有色金属', '铜', '铝', '锂', '稀土', '钴', '镍'],
    '医药': ['医药', '医疗', '创新药', '生物制药', 'CXO', '医疗器械'],
    '白酒': ['白酒', '茅台', '五粮液', '酒'],
    '消费': ['消费', '零售', '家电', '食品饮料'],
    '银行': ['银行', '商业银行', '股份制银行'],
    '券商': ['券商', '证券', '投行'],
    '房地产': ['房地产', '地产', '房企', '物业'],
    '军工': ['军工', '国防', '航空航天'],
    '黄金': ['黄金', '贵金属', '金条'],
    '港股': ['港股', '港科技', '恒生', '南向资金'],
    '互联网': ['互联网', '电商', '社交', '游戏'],
}

SECTOR_FUND_MAP = {
    '化工': ('159870', '化工ETF'),
    '石油': ('501017', '石油LOF'),
    '煤炭': ('161724', '煤炭指数LOF'),
    '电力': ('561170', '电力ETF'),
    '半导体': ('512480', '半导体ETF'),
    '人工智能': ('015719', '人工智能ETF联接A'),
    '消费电子': ('159732', '消费电子ETF'),
    '通信': ('515880', '通信ETF'),
    '新能源': ('516790', '新能源车ETF'),
    '电池': ('159840', '锂电池ETF'),
    '机器人': ('159770', '机器人ETF'),
    '有色金属': ('160221', '有色金属指数'),
    '医药': ('001017', '华夏医疗健康混合A'),
    '创新药': ('159858', '创新药ETF'),
    '白酒': ('161725', '招商中证白酒指数'),
    '消费': ('000083', '汇添富消费行业混合'),
    '银行': ('001594', '易方达中证银行指数LOF'),
    '券商': ('512880', '证券ETF'),
    '房地产': ('160218', '国泰国证房地产指数'),
    '军工': ('005633', '华夏军工安全混合A'),
    '黄金': ('000218', '易方达黄金ETF联接A'),
    '港股': ('513180', '恒生科技ETF'),
    '互联网': ('515000', '互联网ETF'),
}


def extract_sector_from_content(content: str) -> str:
    """从内容中提取板块"""
    if not content:
        return ""
    
    if '：' in content[:30]:
        prefix = content.split('：')[0]
        prefix = prefix.replace('板块', '').replace('线', '').strip()
        
        for sector, keywords in SECTOR_KEYWORDS.items():
            if prefix in keywords or prefix == sector:
                return sector
            for kw in keywords:
                if kw in prefix:
                    return sector
    
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw in content[:50]:
                return sector
    
    return ""


def get_fund_for_sector(sector: str, db) -> tuple:
    """获取板块对应的基金"""
    if sector in SECTOR_FUND_MAP:
        fund_code, fund_name = SECTOR_FUND_MAP[sector]
        existing = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
        if existing:
            return existing.fund_code, existing.fund_name
        return fund_code, fund_name
    
    from src.analyzer.llm_analyzer import get_analyzer
    analyzer = get_analyzer()
    fund_info = analyzer.get_fund_for_sector(sector)
    if fund_info:
        return fund_info.get('code'), fund_info.get('name')
    
    return None, None


def fix_sector_mismatch(dry_run: bool = True):
    """修复板块匹配错误的预测"""
    db = SessionLocal()
    
    try:
        today = date.today()
        
        predictions = db.query(Prediction).filter(
            Prediction.prediction_date >= today
        ).order_by(Prediction.id).all()
        
        print(f'检查板块匹配错误的预测')
        print('='*60)
        print(f'总预测数量: {len(predictions)}')
        
        fixed_count = 0
        unchanged_count = 0
        
        for pred in predictions:
            content = pred.prediction_content or ''
            current_sector = pred.sector or ''
            
            correct_sector = extract_sector_from_content(content)
            
            if correct_sector and correct_sector != current_sector:
                print(f'\n预测 {pred.id}:')
                print(f'  当前板块: {current_sector}')
                print(f'  正确板块: {correct_sector}')
                print(f'  内容: {content[:60]}...')
                
                fund_code, fund_name = get_fund_for_sector(correct_sector, db)
                
                if fund_code:
                    print(f'  基金: {fund_name} ({fund_code})')
                    
                    if not dry_run:
                        pred.sector = correct_sector
                        pred.fund_code = fund_code
                        pred.fund_name = fund_name
                        fixed_count += 1
                    else:
                        print(f'  [试运行模式，未保存]')
                        fixed_count += 1
                else:
                    print(f'  未找到对应基金')
                    unchanged_count += 1
            else:
                unchanged_count += 1
        
        if not dry_run:
            db.commit()
            print(f'\n已提交数据库')
        
        print(f'\n处理完成:')
        print(f'  - 修复: {fixed_count}')
        print(f'  - 未变: {unchanged_count}')
        
        if dry_run:
            print('\n[试运行模式] 未实际修改数据库')
            print('如需正式执行，请添加 --execute 参数')
        
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="修复板块匹配错误的预测")
    parser.add_argument("--execute", action="store_true", help="正式执行（默认试运行）")
    
    args = parser.parse_args()
    fix_sector_mismatch(dry_run=not args.execute)
