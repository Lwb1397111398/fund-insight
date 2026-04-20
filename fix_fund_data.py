"""
修复基金数据 - 解决以下问题:
1. 更新所有基金的 sector_type(板块类型)
2. 计算所有基金的 week_growth 和 month_growth
3. 修复预测记录的 has_active_prediction 字段
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.models.database import (
    SessionLocal, FundInfo, FundHistory, Prediction, Blogger
)
from src.fund.fund_api import fund_data_manager
from datetime import date, timedelta


def fix_fund_sector_type():
    """修复基金板块类型"""
    print("=" * 50)
    print("正在修复基金板块类型...")
    print("=" * 50)
    
    db = SessionLocal()
    try:
        funds = db.query(FundInfo).filter(FundInfo.active_predictions > 0).all()
        
        for fund in funds:
            predictions = db.query(Prediction).filter(
                Prediction.fund_code == fund.fund_code,
                Prediction.sector_type != '',
                Prediction.sector_type != None
            ).limit(5).all()
            
            if predictions and (not fund.sector_type or fund.sector_type == fund.fund_type):
                sector_types = [p.sector_type for p in predictions if p.sector_type]
                if sector_types:
                    most_common = max(set(sector_types), key=sector_types.count)
                    print(f"  {fund.fund_code} ({fund.fund_name}): {fund.sector_type} -> {most_common}")
                    fund.sector_type = most_common
        
        db.commit()
        print(f"✓ 板块类型修复完成\n")
    finally:
        db.close()


def fix_fund_growth_data():
    """修复基金周/月涨跌数据"""
    print("=" * 50)
    print("正在计算基金周/月涨跌...")
    print("=" * 50)
    
    db = SessionLocal()
    try:
        funds = db.query(FundInfo).filter(FundInfo.active_predictions > 0).all()
        
        for fund in funds:
            history = db.query(FundHistory).filter(
                FundHistory.fund_code == fund.fund_code
            ).order_by(FundHistory.nav_date.desc()).limit(30).all()
            
            if len(history) >= 5:
                old_week = fund.week_growth
                if history[4].nav and history[4].nav != 0:
                    fund.week_growth = round(
                        (history[0].nav - history[4].nav) / history[4].nav * 100, 2
                    )
                else:
                    fund.week_growth = None
                
                if old_week != fund.week_growth:
                    print(f"  {fund.fund_code}: 周涨跌 {old_week} -> {fund.week_growth}%")
            
            if len(history) >= 20:
                old_month = fund.month_growth
                if history[19].nav and history[19].nav != 0:
                    fund.month_growth = round(
                        (history[0].nav - history[19].nav) / history[19].nav * 100, 2
                    )
                else:
                    fund.month_growth = None
                
                if old_month != fund.month_growth:
                    print(f"  {fund.fund_code}: 月涨跌 {old_month} -> {fund.month_growth}%")
        
        db.commit()
        print(f"✓ 涨跌数据计算完成\n")
    finally:
        db.close()


def fix_prediction_active_status():
    """修复预测记录的活跃状态"""
    print("=" * 50)
    print("正在修复预测记录活跃状态...")
    print("=" * 50)
    
    db = SessionLocal()
    try:
        predictions = db.query(Prediction).all()
        
        for pred in predictions:
            if pred.is_expired or pred.status == 'verified':
                pred.has_active_prediction = False
            else:
                pred.has_active_prediction = True
        
        db.commit()
        print(f"✓ 预测状态修复完成\n")
    finally:
        db.close()


def update_all_fund_data():
    """更新所有基金的最新数据"""
    print("=" * 50)
    print("正在更新所有基金最新数据...")
    print("=" * 50)
    
    db = SessionLocal()
    try:
        funds = db.query(FundInfo).filter(FundInfo.active_predictions > 0).all()
        
        for fund in funds:
            print(f"  更新 {fund.fund_code} ({fund.fund_name})...")
            fund_data_manager.update_fund_info(fund.fund_code, db)
            fund_data_manager.update_fund_history(fund.fund_code, days=30, db=db)
        
        db.commit()
        print(f"✓ 基金数据更新完成\n")
    finally:
        db.close()


def show_fund_summary():
    """显示基金数据摘要"""
    print("=" * 50)
    print("基金数据摘要")
    print("=" * 50)
    
    db = SessionLocal()
    try:
        funds = db.query(FundInfo).filter(FundInfo.active_predictions > 0).all()
        
        print(f"\n共有 {len(funds)} 个活跃基金:\n")
        
        for fund in funds:
            predictions_count = db.query(Prediction).filter(
                Prediction.fund_code == fund.fund_code
            ).count()
            
            print(f"  {fund.fund_code} - {fund.fund_name}")
            print(f"    板块类型：{fund.sector_type or '未知'}")
            print(f"    预测数量：{predictions_count}")
            print(f"    日涨跌：{fund.day_growth}%")
            print(f"    周涨跌：{fund.week_growth}%")
            print(f"    月涨跌：{fund.month_growth}%")
            print()
    finally:
        db.close()


if __name__ == '__main__':
    print("\n基金数据修复工具\n")
    
    show_fund_summary()
    
    update_all_fund_data()
    fix_fund_sector_type()
    fix_fund_growth_data()
    fix_prediction_active_status()
    
    print("=" * 50)
    print("所有修复完成!")
    print("=" * 50)
    
    show_fund_summary()
