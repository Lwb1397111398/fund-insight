"""
重新匹配预测中的基金
用于修复之前未成功匹配基金的预测记录
"""
import os
import sys

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from sqlalchemy.orm import Session
from src.models.database import Prediction, FundInfo, SessionLocal
from src.fund.fund_auto_manager import fund_auto_manager


def rematch_predictions(dry_run: bool = True):
    """
    重新匹配没有fund_code的预测
    
    Args:
        dry_run: 如果为True，只打印日志不实际修改数据库
    """
    db = SessionLocal()
    
    try:
        # 查询所有fund_code为空或无效的预测
        predictions = db.query(Prediction).filter(
            (Prediction.fund_code == None) | 
            (Prediction.fund_code == '') |
            (Prediction.fund_code == 'None')
        ).all()
        
        print(f"[Rematch] 找到 {len(predictions)} 条需要重新匹配的预测")
        
        if not predictions:
            print("[Rematch] 没有需要重新匹配的预测")
            return
        
        success_count = 0
        failed_count = 0
        skipped_count = 0
        
        for pred in predictions:
            sector = pred.sector
            
            if not sector:
                print(f"[Rematch] 预测 {pred.id}: 板块名称为空，跳过")
                skipped_count += 1
                continue
            
            print(f"\n[Rematch] 处理预测 {pred.id}: 板块={sector}")
            
            # 尝试匹配基金
            try:
                success, message, fund = fund_auto_manager.auto_add_fund_for_prediction(sector, db)
                
                if success and fund:
                    if not dry_run:
                        # 更新预测记录
                        pred.fund_code = fund.fund_code
                        pred.fund_name = fund.fund_name
                        db.commit()
                        print(f"[Rematch] ✓ 成功匹配: {fund.fund_name} ({fund.fund_code})")
                    else:
                        print(f"[Rematch] ✓ 可匹配: {fund.fund_name} ({fund.fund_code}) [试运行模式，未保存]")
                    success_count += 1
                else:
                    print(f"[Rematch] ✗ 匹配失败: {message}")
                    failed_count += 1
                    
            except Exception as e:
                print(f"[Rematch] ✗ 处理异常: {e}")
                failed_count += 1
        
        print(f"\n[Rematch] 处理完成:")
        print(f"  - 成功: {success_count}")
        print(f"  - 失败: {failed_count}")
        print(f"  - 跳过: {skipped_count}")
        
        if dry_run:
            print("\n[Rematch] 当前为试运行模式，未实际修改数据库")
            print("[Rematch] 如需正式执行，请设置 dry_run=False")
        
    finally:
        db.close()


def rematch_all_predictions(dry_run: bool = True):
    """
    重新匹配所有预测（包括已有fund_code的，用于更新基金信息）
    
    Args:
        dry_run: 如果为True，只打印日志不实际修改数据库
    """
    db = SessionLocal()
    
    try:
        # 查询所有预测
        predictions = db.query(Prediction).all()
        
        print(f"[Rematch All] 找到 {len(predictions)} 条预测记录")
        
        success_count = 0
        failed_count = 0
        skipped_count = 0
        already_matched = 0
        
        for pred in predictions:
            sector = pred.sector
            current_fund_code = pred.fund_code
            
            if not sector:
                print(f"[Rematch All] 预测 {pred.id}: 板块名称为空，跳过")
                skipped_count += 1
                continue
            
            # 如果已有fund_code，检查是否有效
            if current_fund_code:
                existing_fund = db.query(FundInfo).filter(
                    FundInfo.fund_code == current_fund_code
                ).first()
                
                if existing_fund:
                    print(f"[Rematch All] 预测 {pred.id}: 已有有效基金 {current_fund_code}，跳过")
                    already_matched += 1
                    continue
            
            print(f"\n[Rematch All] 处理预测 {pred.id}: 板块={sector}")
            
            # 尝试匹配基金
            try:
                success, message, fund = fund_auto_manager.auto_add_fund_for_prediction(sector, db)
                
                if success and fund:
                    if not dry_run:
                        pred.fund_code = fund.fund_code
                        pred.fund_name = fund.fund_name
                        db.commit()
                        print(f"[Rematch All] ✓ 成功匹配: {fund.fund_name} ({fund.fund_code})")
                    else:
                        print(f"[Rematch All] ✓ 可匹配: {fund.fund_name} ({fund.fund_code}) [试运行模式]")
                    success_count += 1
                else:
                    print(f"[Rematch All] ✗ 匹配失败: {message}")
                    failed_count += 1
                    
            except Exception as e:
                print(f"[Rematch All] ✗ 处理异常: {e}")
                failed_count += 1
        
        print(f"\n[Rematch All] 处理完成:")
        print(f"  - 成功: {success_count}")
        print(f"  - 失败: {failed_count}")
        print(f"  - 跳过: {skipped_count}")
        print(f"  - 已有有效基金: {already_matched}")
        
        if dry_run:
            print("\n[Rematch All] 当前为试运行模式，未实际修改数据库")
        
    finally:
        db.close()


def show_unmatched_predictions():
    """显示所有未匹配的预测"""
    db = SessionLocal()
    
    try:
        predictions = db.query(Prediction).filter(
            (Prediction.fund_code == None) | 
            (Prediction.fund_code == '') |
            (Prediction.fund_code == 'None')
        ).all()
        
        print(f"\n[Unmatched] 未匹配的预测列表 (共 {len(predictions)} 条):")
        print("-" * 80)
        
        # 按板块统计
        sector_stats = {}
        for pred in predictions:
            sector = pred.sector or "未知板块"
            if sector not in sector_stats:
                sector_stats[sector] = []
            sector_stats[sector].append(pred.id)
        
        for sector, pred_ids in sorted(sector_stats.items(), key=lambda x: -len(x[1])):
            print(f"\n板块: {sector}")
            print(f"  数量: {len(pred_ids)}")
            print(f"  预测ID: {', '.join(map(str, pred_ids[:10]))}{'...' if len(pred_ids) > 10 else ''}")
        
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="重新匹配预测中的基金")
    parser.add_argument(
        "--execute", 
        action="store_true", 
        help="正式执行（默认试运行）"
    )
    parser.add_argument(
        "--all", 
        action="store_true", 
        help="处理所有预测（包括已有fund_code的）"
    )
    parser.add_argument(
        "--show", 
        action="store_true", 
        help="只显示未匹配的预测列表"
    )
    
    args = parser.parse_args()
    
    dry_run = not args.execute
    
    if args.show:
        show_unmatched_predictions()
    elif args.all:
        rematch_all_predictions(dry_run=dry_run)
    else:
        rematch_predictions(dry_run=dry_run)