"""
检查基金日涨幅数据
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, FundInfo, FundHistory
from datetime import date, timedelta


def check_fund_day_growth():
    db = SessionLocal()
    
    try:
        # 查询几个基金的信息
        funds = db.query(FundInfo).limit(5).all()
        
        print('基金日涨幅数据检查')
        print('='*60)
        
        for fund in funds:
            print(f'\n基金: {fund.fund_name} ({fund.fund_code})')
            print(f'  FundInfo.day_growth: {fund.day_growth}%')
            print(f'  FundInfo.latest_nav: {fund.latest_nav}')
            print(f'  FundInfo.nav_date: {fund.nav_date}')
            
            # 查询最新的历史净值
            latest_history = db.query(FundHistory).filter(
                FundHistory.fund_code == fund.fund_code
            ).order_by(FundHistory.nav_date.desc()).first()
            
            if latest_history:
                print(f'  FundHistory.nav_date: {latest_history.nav_date}')
                print(f'  FundHistory.nav: {latest_history.nav}')
                print(f'  FundHistory.day_growth: {latest_history.day_growth}%')
                
                # 检查是否一致
                if fund.nav_date == latest_history.nav_date:
                    if fund.day_growth != latest_history.day_growth:
                        print(f'  ⚠️  日涨幅不一致!')
                        print(f'      FundInfo: {fund.day_growth}%')
                        print(f'      FundHistory: {latest_history.day_growth}%')
                else:
                    print(f'  ⚠️  净值日期不一致!')
            else:
                print(f'  无历史净值数据')
        
    finally:
        db.close()


if __name__ == "__main__":
    check_fund_day_growth()
