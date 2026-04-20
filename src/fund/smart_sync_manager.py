"""
智能基金同步管理器 V2

功能：
1. 精细化同步策略（核心/活跃/普通分级）
2. 同步监控与告警
3. 增量同步
4. 同步报告生成
"""
import json
import logging
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.database import (
    FundInfo, FundHistory, Prediction, SyncLog, 
    FundSyncRetry, SessionLocal
)
from src.fund.multi_source_api import multi_source_api
from src.fund.anomaly_handler import retry_queue_manager, anomaly_handler
from src.fund.fund_mapping_v2 import enhanced_fund_matcher


class SmartSyncStrategy:
    """
    智能同步策略
    
    分级规则：
    - 核心基金：每日同步（大盘指数、主流板块ETF）
    - 活跃预测基金：每日同步（有未到期预测的基金）
    - 普通基金：每周同步（周六）
    """
    
    SYNC_RULES = {
        'core': {
            'frequency': 'daily',
            'description': '核心基金，每日同步',
            'priority': 1
        },
        'active_prediction': {
            'frequency': 'daily',
            'description': '有活跃预测的基金，每日同步',
            'priority': 2
        },
        'recent_prediction': {
            'frequency': 'daily',
            'description': '近7天有预测的基金，每日同步',
            'priority': 3
        },
        'normal': {
            'frequency': 'weekly',
            'day': 5,
            'description': '普通基金，每周六同步',
            'priority': 4
        }
    }
    
    ALERT_THRESHOLD = 0.2
    
    def __init__(self):
        self.core_funds = enhanced_fund_matcher.get_all_core_funds()
    
    def get_sync_candidates(self, db: Session, force_all: bool = False) -> Dict:
        """
        获取需要同步的基金列表
        
        Returns:
            {
                'core': [核心基金列表],
                'active_prediction': [活跃预测基金列表],
                'recent_prediction': [近期预测基金列表],
                'normal': [普通基金列表],
                'total': 总数,
                'sync_reason': 同步原因说明
            }
        """
        today = date.today()
        weekday = today.weekday()
        
        result = {
            'core': [],
            'active_prediction': [],
            'recent_prediction': [],
            'normal': [],
            'total': 0,
            'sync_reason': []
        }
        
        all_funds = db.query(FundInfo).all()
        all_fund_codes = {f.fund_code for f in all_funds}
        
        result['core'] = [f for f in self.core_funds if f in all_fund_codes]
        if result['core']:
            result['sync_reason'].append(f"核心基金{len(result['core'])}个（每日同步）")
        
        active_predictions = db.query(Prediction).filter(
            Prediction.is_expired == False,
            Prediction.fund_code.isnot(None)
        ).all()
        
        active_fund_codes = list(set(p.fund_code for p in active_predictions if p.fund_code))
        result['active_prediction'] = [f for f in active_fund_codes if f in all_fund_codes]
        if result['active_prediction']:
            result['sync_reason'].append(f"活跃预测基金{len(result['active_prediction'])}个（每日同步）")
        
        week_ago = today - timedelta(days=7)
        recent_predictions = db.query(Prediction).filter(
            Prediction.prediction_date >= week_ago,
            Prediction.fund_code.isnot(None)
        ).all()
        
        recent_fund_codes = list(set(p.fund_code for p in recent_predictions if p.fund_code))
        recent_fund_codes = [f for f in recent_fund_codes if f not in result['core'] and f not in result['active_prediction']]
        result['recent_prediction'] = [f for f in recent_fund_codes if f in all_fund_codes]
        if result['recent_prediction']:
            result['sync_reason'].append(f"近期预测基金{len(result['recent_prediction'])}个（每日同步）")
        
        if force_all or weekday == 5:
            synced_codes = set(result['core'] + result['active_prediction'] + result['recent_prediction'])
            result['normal'] = [f.fund_code for f in all_funds if f.fund_code not in synced_codes]
            if result['normal']:
                result['sync_reason'].append(f"普通基金{len(result['normal'])}个（每周同步）")
        
        result['total'] = len(result['core']) + len(result['active_prediction']) + len(result['recent_prediction']) + len(result['normal'])
        
        return result
    
    def sync_single_fund(self, fund_code: str, db: Session) -> Dict:
        """
        同步单个基金数据
        
        Returns:
            {
                'success': 是否成功,
                'fund_code': 基金代码,
                'nav': 净值,
                'nav_date': 净值日期,
                'quality': 数据质量,
                'error': 错误信息
            }
        """
        result = {
            'success': False,
            'fund_code': fund_code,
            'nav': None,
            'nav_date': None,
            'quality': None,
            'error': None
        }
        
        try:
            nav_result = multi_source_api.get_nav_with_validation(fund_code)
            
            if nav_result.get('nav') is None:
                result['error'] = nav_result.get('quality_note', '无法获取净值')
                retry_queue_manager.add_to_retry_queue(
                    fund_code, 'nav', result['error'], db
                )
                return result
            
            fund = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            
            if fund:
                prev_nav = fund.latest_nav
                
                fund.latest_nav = nav_result['nav']
                fund.nav_date = datetime.strptime(nav_result['nav_date'], '%Y-%m-%d').date() if nav_result.get('nav_date') else date.today()
                fund.nav_source = nav_result.get('sources', {}).get(list(nav_result['sources'].keys())[0], {}).get('source', 'unknown') if nav_result.get('sources') else 'unknown'
                fund.data_quality = nav_result['quality']
                fund.data_quality_note = nav_result.get('quality_note')
                fund.updated_at = datetime.now()
                
                if nav_result.get('is_estimated'):
                    fund.estimated_nav = nav_result['nav']
                    fund.estimated_nav_time = datetime.now()
                else:
                    fund.actual_nav = nav_result['nav']
                    fund.actual_nav_time = datetime.now()
                
                if prev_nav and prev_nav > 0:
                    fund.day_growth = round((nav_result['nav'] - prev_nav) / prev_nav * 100, 2)
                    
                    validation = anomaly_handler.detect_anomaly(prev_nav, nav_result['nav'])
                    if validation['is_anomaly']:
                        fund.data_quality = 'warning'
                        fund.data_quality_note = validation['description']
                
                db.commit()
                
                result['success'] = True
                result['nav'] = nav_result['nav']
                result['nav_date'] = str(fund.nav_date)
                result['quality'] = nav_result['quality']
            
        except Exception as e:
            result['error'] = str(e)
            retry_queue_manager.add_to_retry_queue(fund_code, 'nav', str(e), db)
        
        return result
    
    def sync_fund_history(self, fund_code: str, days: int, db: Session) -> Dict:
        """
        同步基金历史净值
        
        Returns:
            {
                'success': 是否成功,
                'fund_code': 基金代码,
                'count': 同步条数,
                'quality': 数据质量,
                'error': 错误信息
            }
        """
        result = {
            'success': False,
            'fund_code': fund_code,
            'count': 0,
            'quality': None,
            'error': None
        }
        
        try:
            history_result = multi_source_api.get_history_with_validation(fund_code, days)
            
            if not history_result['history']:
                result['error'] = history_result.get('quality_note', '无法获取历史净值')
                retry_queue_manager.add_to_retry_queue(fund_code, 'history', result['error'], db)
                return result
            
            fund = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            
            count = 0
            for item in history_result['history']:
                existing = db.query(FundHistory).filter(
                    FundHistory.fund_code == fund_code,
                    FundHistory.nav_date == item['date']
                ).first()
                
                if existing:
                    existing.nav = item['nav']
                    existing.day_growth = item.get('growth')
                else:
                    record = FundHistory(
                        fund_code=fund_code,
                        fund_name=fund.fund_name if fund else '',
                        nav_date=item['date'],
                        nav=item['nav'],
                        day_growth=item.get('growth')
                    )
                    db.add(record)
                    count += 1
            
            db.commit()
            
            result['success'] = True
            result['count'] = count
            result['quality'] = history_result['quality']
            
        except Exception as e:
            result['error'] = str(e)
            retry_queue_manager.add_to_retry_queue(fund_code, 'history', str(e), db)
        
        return result
    
    def execute_sync(self, db: Session, sync_type: str = 'incremental',
                     include_history: bool = False, history_days: int = 30) -> Dict:
        """
        执行同步
        
        Args:
            db: 数据库会话
            sync_type: 同步类型 (incremental/full)
            include_history: 是否同步历史数据
            history_days: 历史数据天数
        
        Returns:
            同步报告
        """
        sync_log = SyncLog(
            sync_type=sync_type,
            sync_date=datetime.now(),
            status='running'
        )
        db.add(sync_log)
        db.commit()
        
        start_time = datetime.now()
        
        candidates = self.get_sync_candidates(db, force_all=(sync_type == 'full'))
        
        all_fund_codes = (
            candidates['core'] + 
            candidates['active_prediction'] + 
            candidates['recent_prediction'] + 
            candidates['normal']
        )
        
        result = {
            'total': len(all_fund_codes),
            'success': 0,
            'failed': 0,
            'failed_funds': [],
            'candidates': candidates,
            'history_synced': 0
        }
        
        for fund_code in all_fund_codes:
            sync_result = self.sync_single_fund(fund_code, db)
            
            if sync_result['success']:
                result['success'] += 1
            else:
                result['failed'] += 1
                result['failed_funds'].append({
                    'code': fund_code,
                    'reason': sync_result['error']
                })
            
            if include_history:
                history_result = self.sync_fund_history(fund_code, history_days, db)
                if history_result['success']:
                    result['history_synced'] += history_result['count']
        
        duration = (datetime.now() - start_time).total_seconds()
        
        sync_log.total_count = result['total']
        sync_log.success_count = result['success']
        sync_log.failed_count = result['failed']
        sync_log.failed_funds = result['failed_funds']
        sync_log.duration_seconds = duration
        sync_log.status = 'completed'
        db.commit()
        
        if result['total'] > 0 and result['failed'] / result['total'] >= self.ALERT_THRESHOLD:
            self._send_alert(result, db)
        
        return result
    
    def _send_alert(self, result: Dict, db: Session):
        """发送告警"""
        alert_message = (
            f"[基金同步告警]\n"
            f"失败率: {result['failed']/result['total']*100:.1f}%\n"
            f"失败数: {result['failed']}/{result['total']}\n"
            f"失败基金: {json.dumps(result['failed_funds'][:5], ensure_ascii=False)}"
        )
        
        logging.warning(alert_message)
        
        print(f"\n{'='*50}")
        print(alert_message)
        print(f"{'='*50}\n")


class SyncMonitor:
    """
    同步监控器
    
    功能：
    1. 监控同步状态
    2. 生成同步报告
    3. 数据质量统计
    """
    
    def get_sync_report(self, days: int = 7, db: Session = None) -> Dict:
        """
        获取同步报告
        
        Args:
            days: 报告天数
            db: 数据库会话
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        
        try:
            start_date = datetime.now() - timedelta(days=days)
            
            logs = db.query(SyncLog).filter(
                SyncLog.sync_date >= start_date
            ).order_by(SyncLog.sync_date.desc()).all()
            
            total_syncs = len(logs)
            total_success = sum(l.success_count for l in logs)
            total_failed = sum(l.failed_count for l in logs)
            avg_duration = sum(l.duration_seconds or 0 for l in logs) / total_syncs if total_syncs > 0 else 0
            
            daily_stats = {}
            for log in logs:
                day = log.sync_date.date().isoformat()
                if day not in daily_stats:
                    daily_stats[day] = {
                        'sync_count': 0,
                        'success': 0,
                        'failed': 0
                    }
                daily_stats[day]['sync_count'] += 1
                daily_stats[day]['success'] += log.success_count or 0
                daily_stats[day]['failed'] += log.failed_count or 0
            
            total_funds = db.query(FundInfo).count()
            normal_quality = db.query(FundInfo).filter(
                FundInfo.data_quality == 'normal'
            ).count()
            warning_quality = db.query(FundInfo).filter(
                FundInfo.data_quality == 'warning'
            ).count()
            error_quality = db.query(FundInfo).filter(
                FundInfo.data_quality == 'error'
            ).count()
            
            pending_retries = db.query(FundSyncRetry).filter(
                FundSyncRetry.status == 'pending'
            ).count()
            
            return {
                'period': f'最近{days}天',
                'summary': {
                    'total_syncs': total_syncs,
                    'total_success': total_success,
                    'total_failed': total_failed,
                    'success_rate': round(total_success / (total_success + total_failed) * 100, 1) if (total_success + total_failed) > 0 else 0,
                    'avg_duration': round(avg_duration, 2)
                },
                'daily_stats': daily_stats,
                'data_quality': {
                    'total_funds': total_funds,
                    'normal': normal_quality,
                    'warning': warning_quality,
                    'error': error_quality,
                    'quality_rate': round(normal_quality / total_funds * 100, 1) if total_funds > 0 else 0
                },
                'retry_queue': {
                    'pending': pending_retries
                },
                'recent_logs': [
                    {
                        'id': l.id,
                        'sync_date': l.sync_date.isoformat(),
                        'sync_type': l.sync_type,
                        'total': l.total_count,
                        'success': l.success_count,
                        'failed': l.failed_count,
                        'duration': l.duration_seconds,
                        'status': l.status
                    }
                    for l in logs[:10]
                ]
            }
            
        finally:
            if close_db:
                db.close()
    
    def get_fund_sync_status(self, fund_code: str, db: Session) -> Dict:
        """获取单个基金的同步状态"""
        fund = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
        
        if not fund:
            return {'exists': False}
        
        return {
            'exists': True,
            'fund_code': fund.fund_code,
            'fund_name': fund.fund_name,
            'latest_nav': fund.latest_nav,
            'nav_date': fund.nav_date.isoformat() if fund.nav_date else None,
            'nav_source': fund.nav_source,
            'data_quality': fund.data_quality,
            'data_quality_note': fund.data_quality_note,
            'updated_at': fund.updated_at.isoformat() if fund.updated_at else None,
            'is_core_fund': fund.is_core_fund,
            'days_since_update': (date.today() - fund.updated_at.date()).days if fund.updated_at else None
        }


smart_sync_strategy = SmartSyncStrategy()
sync_monitor = SyncMonitor()


if __name__ == '__main__':
    db = SessionLocal()
    
    candidates = smart_sync_strategy.get_sync_candidates(db)
    print("同步候选:", json.dumps(candidates, ensure_ascii=False, indent=2, default=str))
    
    report = sync_monitor.get_sync_report(days=7, db=db)
    print("同步报告:", json.dumps(report, ensure_ascii=False, indent=2, default=str))
    
    db.close()
