"""
重新分析预测周期
用于修复预测周期异常（如大量1天周期）的问题
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import SessionLocal, Prediction, Post
from src.utils.time_parser import suggest_prediction_period
from datetime import date
from collections import Counter


def reanalyze_prediction_periods(dry_run: bool = True, target_date: date = None):
    """
    重新分析预测周期
    
    Args:
        dry_run: 如果为True，只打印日志不实际修改数据库
        target_date: 目标日期，默认今天
    """
    db = SessionLocal()
    
    try:
        if target_date is None:
            target_date = date.today()
        
        # 查询指定日期的所有预测
        predictions = db.query(Prediction).filter(
            Prediction.prediction_date == target_date
        ).all()
        
        print(f'重新分析 {target_date} 的预测周期')
        print('='*60)
        print(f'总预测数量: {len(predictions)}')
        
        if not predictions:
            print('没有需要处理的预测')
            return
        
        # 统计当前周期分布
        before_stats = Counter([p.prediction_period for p in predictions])
        print(f'\n修复前周期分布:')
        for period, count in before_stats.most_common():
            print(f'  {period}: {count}条')
        
        updated_count = 0
        unchanged_count = 0
        
        for pred in predictions:
            # 获取关联的帖子内容
            post = db.query(Post).filter(Post.id == pred.post_id).first()
            
            if not post:
                print(f'\n  预测 {pred.id}: 没有关联帖子，跳过')
                unchanged_count += 1
                continue
            
            # 优先使用预测内容分析周期（预测内容更能反映该预测的时间维度）
            pred_content = pred.prediction_content or ''
            
            # 如果预测内容足够长（>50字），优先使用预测内容
            if len(pred_content) > 50:
                text = pred_content
            else:
                # 否则合并帖子标题、内容和预测内容
                text = f"{post.title or ''} {post.content or ''} {pred_content}"
            
            # 使用修复后的时间解析器重新分析
            new_days, new_period, reason = suggest_prediction_period(text, pred.prediction_date)
            
            old_period = pred.prediction_period
            
            # 如果周期发生变化
            if old_period != new_period:
                print(f'\n  预测 {pred.id}:')
                print(f'    板块: {pred.sector}')
                print(f'    原周期: {old_period} → 新周期: {new_period}')
                print(f'    理由: {reason}')
                
                if not dry_run:
                    # 更新预测周期
                    pred.prediction_period = new_period
                    
                    # 更新目标日期
                    if pred.prediction_date:
                        from datetime import timedelta
                        pred.target_date = pred.prediction_date + timedelta(days=new_days)
                        
                        # 更新下次验证日期
                        from src.analyzer.llm_analyzer import get_analyzer
                        analyzer = get_analyzer()
                        pred.next_verify_date = analyzer.calculate_next_verify_date(
                            pred.prediction_date,
                            pred.target_date
                        )
                    
                    updated_count += 1
                else:
                    print(f'    [试运行模式，未保存]')
                    updated_count += 1
            else:
                unchanged_count += 1
        
        # 统计修复后周期分布
        if not dry_run:
            db.commit()
            
            # 重新查询统计
            predictions = db.query(Prediction).filter(
                Prediction.prediction_date == target_date
            ).all()
            
            after_stats = Counter([p.prediction_period for p in predictions])
            print(f'\n修复后周期分布:')
            for period, count in after_stats.most_common():
                print(f'  {period}: {count}条')
        
        print(f'\n处理完成:')
        print(f'  - 更新: {updated_count}')
        print(f'  - 未变: {unchanged_count}')
        
        if dry_run:
            print('\n[试运行模式] 未实际修改数据库')
            print('如需正式执行，请添加 --execute 参数')
        
    finally:
        db.close()


def reanalyze_all_short_periods(dry_run: bool = True):
    """
    重新分析所有短期预测（1-3天）
    
    Args:
        dry_run: 如果为True，只打印日志不实际修改数据库
    """
    db = SessionLocal()
    
    try:
        # 查询所有短期预测（1-3天）
        short_periods = ['1天', '2天', '3天']
        predictions = db.query(Prediction).filter(
            Prediction.prediction_period.in_(short_periods)
        ).all()
        
        print(f'重新分析所有短期预测（1-3天）')
        print('='*60)
        print(f'总预测数量: {len(predictions)}')
        
        if not predictions:
            print('没有需要处理的预测')
            return
        
        updated_count = 0
        unchanged_count = 0
        
        for pred in predictions:
            # 获取关联的帖子内容
            post = db.query(Post).filter(Post.id == pred.post_id).first()
            
            if not post:
                unchanged_count += 1
                continue
            
            # 合并标题和内容作为分析文本
            text = f"{post.title or ''} {post.content or ''} {pred.prediction_content or ''}"
            
            # 使用修复后的时间解析器重新分析
            new_days, new_period, reason = suggest_prediction_period(text, pred.prediction_date)
            
            old_period = pred.prediction_period
            
            # 如果周期发生变化（从短期变为中长期）
            if old_period != new_period and new_days >= 7:
                print(f'\n  预测 {pred.id}:')
                print(f'    板块: {pred.sector}')
                print(f'    原周期: {old_period} → 新周期: {new_period}')
                print(f'    理由: {reason}')
                
                if not dry_run:
                    pred.prediction_period = new_period
                    
                    if pred.prediction_date:
                        from datetime import timedelta
                        pred.target_date = pred.prediction_date + timedelta(days=new_days)
                        
                        from src.analyzer.llm_analyzer import get_analyzer
                        analyzer = get_analyzer()
                        pred.next_verify_date = analyzer.calculate_next_verify_date(
                            pred.prediction_date,
                            pred.target_date
                        )
                    
                    updated_count += 1
                else:
                    print(f'    [试运行模式，未保存]')
                    updated_count += 1
            else:
                unchanged_count += 1
        
        print(f'\n处理完成:')
        print(f'  - 更新: {updated_count}')
        print(f'  - 未变: {unchanged_count}')
        
        if dry_run:
            print('\n[试运行模式] 未实际修改数据库')
        
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="重新分析预测周期")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="正式执行（默认试运行）"
    )
    parser.add_argument(
        "--all-short",
        action="store_true",
        help="处理所有短期预测（1-3天）"
    )
    parser.add_argument(
        "--date",
        type=str,
        help="指定日期（格式：YYYY-MM-DD）"
    )
    
    args = parser.parse_args()
    
    dry_run = not args.execute
    
    if args.all_short:
        reanalyze_all_short_periods(dry_run=dry_run)
    else:
        target_date = None
        if args.date:
            target_date = date.fromisoformat(args.date)
        reanalyze_prediction_periods(dry_run=dry_run, target_date=target_date)
