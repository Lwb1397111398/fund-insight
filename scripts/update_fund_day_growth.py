"""
更新基金日涨幅数据
使用历史净值中的实际涨跌幅，而不是估值涨跌幅
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, FundInfo, FundHistory
from src.fund.fund_api import FundAPI
from datetime import datetime


def update_fund_day_growth(dry_run: bool = True):
    """更新所有基金的日涨幅数据"""
    db = SessionLocal()
    fund_api = FundAPI()
    
    try:
        funds = db.query(FundInfo).all()
        
        print(f'更新基金日涨幅数据')
        print('='*60)
        print(f'总基金数量: {len(funds)}')
        
        updated_count = 0
        failed_count = 0
        
        for fund in funds:
            try:
                # 获取历史净值中的实际涨跌幅
                history = fund_api.get_fund_history(fund.fund_code, days=1)
                
                if history:
                    latest = history[0]
                    actual_day_growth = latest.get('growth')
                    actual_nav_date = latest.get('date')
                    
                    # 检查是否需要更新
                    if fund.day_growth != actual_day_growth:
                        print(f'\n基金: {fund.fund_name} ({fund.fund_code})')
                        print(f'  估值涨跌幅: {fund.day_growth}%')
                        print(f'  实际涨跌幅: {actual_day_growth}%')
                        print(f'  净值日期: {actual_nav_date}')
                        
                        if not dry_run:
                            fund.day_growth = actual_day_growth
                            if actual_nav_date:
                                if isinstance(actual_nav_date, str):
                                    actual_nav_date = datetime.strptime(actual_nav_date, '%Y-%m-%d').date()
                                fund.nav_date = actual_nav_date
                            updated_count += 1
                        else:
                            print(f'  [试运行模式，未保存]')
                            updated_count += 1
                    else:
                        # 日涨幅一致，无需更新
                        pass
                else:
                    print(f'\n基金: {fund.fund_name} ({fund.fund_code}) - 无法获取历史净值')
                    failed_count += 1
                    
            except Exception as e:
                print(f'\n基金: {fund.fund_name} ({fund.fund_code}) - 更新失败: {e}')
                failed_count += 1
        
        if not dry_run:
            db.commit()
            print(f'\n已提交数据库')
        
        print(f'\n处理完成:')
        print(f'  - 需要更新: {updated_count}')
        print(f'  - 失败: {failed_count}')
        
        if dry_run:
            print('\n[试运行模式] 未实际修改数据库')
            print('如需正式执行，请添加 --execute 参数')
        
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="更新基金日涨幅数据")
    parser.add_argument("--execute", action="store_true", help="正式执行（默认试运行）")
    
    args = parser.parse_args()
    update_fund_day_growth(dry_run=not args.execute)
