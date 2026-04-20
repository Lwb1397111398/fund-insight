"""
重新分析已有帖子的预测周期
修复之前所有预测使用统一周期的问题
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.database import SessionLocal, Post, Prediction, Blogger
from src.analyzer.llm_analyzer import get_analyzer
from src.fund.fund_auto_manager import FundAutoManager
from datetime import datetime, date
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def reanalyze_posts(dry_run: bool = True, limit: int = None, reset_verified: bool = False,
                      start_date: date = None, end_date: date = None):
    """
    重新分析已有帖子的预测周期
    
    Args:
        dry_run: 是否只预览不实际修改
        limit: 限制处理的帖子数量（None表示全部）
        reset_verified: 是否重置已验证预测的状态（谨慎使用）
        start_date: 开始日期（包含）
        end_date: 结束日期（包含）
    """
    db = SessionLocal()
    llm_analyzer = get_analyzer()
    fund_auto_manager = FundAutoManager()
    
    try:
        query = db.query(Post).filter(Post.analyzed == True)
        
        if start_date:
            query = query.filter(Post.post_date >= start_date)
        if end_date:
            query = query.filter(Post.post_date <= end_date)
        
        query = query.order_by(Post.id.desc())
        
        if limit:
            query = query.limit(limit)
        
        posts = query.all()
        total = len(posts)
        
        print(f"\n{'='*60}")
        print(f"{'[预览模式]' if dry_run else '[执行模式]'} 重新分析帖子预测周期")
        print(f"{'='*60}")
        if start_date or end_date:
            date_range = f"{start_date} 到 {end_date}" if (start_date and end_date) else (f"从 {start_date} 开始" if start_date else f"到 {end_date} 结束")
            print(f"日期范围: {date_range}")
        print(f"待处理帖子数: {total}")
        print(f"重置已验证预测: {'是' if reset_verified else '否'}")
        print(f"{'='*60}\n")
        
        updated_count = 0
        unchanged_count = 0
        skipped_verified_count = 0
        error_count = 0
        
        for i, post in enumerate(posts, 1):
            try:
                print(f"\n[{i}/{total}] 处理帖子 ID={post.id}")
                print(f"  标题: {post.title[:50] if post.title else '无标题'}...")
                print(f"  发布日期: {post.post_date}")
                
                predictions = db.query(Prediction).filter(
                    Prediction.post_id == post.id,
                    Prediction.is_deleted == False
                ).all()
                
                if not predictions:
                    print(f"  无预测，跳过")
                    unchanged_count += 1
                    continue
                
                old_periods = {p.id: (p.prediction_period, p.target_date, p.status) for p in predictions}
                
                result = llm_analyzer.analyze_post(
                    title=post.title or "",
                    content=post.content or "",
                    post_date=post.post_date,
                    use_cache=False,
                    enable_ai_confirm=True
                )
                
                if not result.get('predictions'):
                    print(f"  AI分析无预测结果，跳过")
                    unchanged_count += 1
                    continue
                
                ai_predictions = result.get('predictions', [])
                
                updates = []
                for pred in predictions:
                    if pred.status in ['success', 'failed'] and not reset_verified:
                        print(f"  预测ID {pred.id} 已验证(status={pred.status})，跳过")
                        skipped_verified_count += 1
                        continue
                    
                    matching_ai_pred = None
                    for ai_pred in ai_predictions:
                        if (ai_pred.get('sector') == pred.sector or 
                            ai_pred.get('fund_code') == pred.fund_code):
                            matching_ai_pred = ai_pred
                            break
                    
                    if matching_ai_pred:
                        new_period = matching_ai_pred.get('prediction_period', pred.prediction_period)
                        
                        if new_period != pred.prediction_period:
                            old_target = pred.target_date
                            new_target = llm_analyzer.calculate_target_date(
                                post.post_date, new_period
                            )
                            new_next_verify = llm_analyzer.calculate_next_verify_date(
                                post.post_date, new_target
                            )
                            
                            updates.append({
                                'prediction': pred,
                                'old_period': pred.prediction_period,
                                'new_period': new_period,
                                'old_target': old_target,
                                'new_target': new_target,
                                'old_next_verify': pred.next_verify_date,
                                'new_next_verify': new_next_verify,
                                'old_status': pred.status,
                                'need_reset': pred.status in ['success', 'failed']
                            })
                
                if updates:
                    print(f"  发现 {len(updates)} 个预测需要更新:")
                    for u in updates:
                        print(f"    - 预测ID {u['prediction'].id}: "
                              f"周期 {u['old_period']} -> {u['new_period']}, "
                              f"目标日期 {u['old_target']} -> {u['new_target']}")
                        if u['need_reset']:
                            print(f"      ⚠️  将重置验证状态: {u['old_status']} -> pending")
                    
                    if not dry_run:
                        for u in updates:
                            pred = u['prediction']
                            pred.prediction_period = u['new_period']
                            pred.target_date = u['new_target']
                            pred.next_verify_date = u['new_next_verify']
                            
                            if u['need_reset']:
                                pred.status = 'pending'
                                pred.verify_result = None
                                pred.is_correct = None
                                pred.verify_score = None
                                pred.end_nav = None
                                pred.end_nav_date = None
                                pred.actual_change = None
                                pred.verify_count = 0
                                pred.verify_history = None
                                pred.last_verify_date = None
                        
                        db.commit()
                        print("  [OK] 已更新")
                    
                    updated_count += 1
                else:
                    print("  无需更新")
                    unchanged_count += 1
                
            except Exception as e:
                print(f"  [ERROR] 处理失败: {e}")
                error_count += 1
                db.rollback()
        
        print(f"\n{'='*60}")
        print(f"处理完成")
        print(f"{'='*60}")
        print(f"总帖子数: {total}")
        print(f"需要更新: {updated_count}")
        print(f"无需更新: {unchanged_count}")
        print(f"跳过已验证: {skipped_verified_count}")
        print(f"处理失败: {error_count}")
        
        if dry_run:
            print(f"\n⚠️  这是预览模式，未实际修改数据")
            print(f"如需执行，请运行: python scripts/reanalyze_posts.py --execute")
            print(f"如需重置已验证预测，请运行: python scripts/reanalyze_posts.py --execute --reset-verified")
        
    except Exception as e:
        print(f"脚本执行失败: {e}")
        import traceback
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='重新分析帖子预测周期')
    parser.add_argument('--execute', action='store_true', help='实际执行修改（默认为预览模式）')
    parser.add_argument('--limit', type=int, default=None, help='限制处理的帖子数量')
    parser.add_argument('--reset-verified', action='store_true', help='重置已验证预测的状态（谨慎使用）')
    parser.add_argument('--start-date', type=str, default=None, help='开始日期 (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, default=None, help='结束日期 (YYYY-MM-DD)')
    
    args = parser.parse_args()
    
    start_date = date.fromisoformat(args.start_date) if args.start_date else None
    end_date = date.fromisoformat(args.end_date) if args.end_date else None
    
    reanalyze_posts(
        dry_run=not args.execute,
        limit=args.limit,
        reset_verified=args.reset_verified,
        start_date=start_date,
        end_date=end_date
    )
