"""
基金异常数据处理模块

功能：
1. 分红/拆分检测
2. 异常值标记
3. 重试队列管理
4. 数据修复
"""
import requests
import re
import json
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple
from sqlalchemy.orm import Session

from src.models.database import (
    FundInfo, FundHistory, FundSyncRetry, SessionLocal
)


class SpecialEventDetector:
    """
    特殊事件检测器
    
    检测类型：
    - 分红：净值骤降，但投资者获得现金
    - 拆分：净值降低，份额增加
    - 合并：净值提高，份额减少
    """
    
    EVENT_TYPES = {
        'dividend': {
            'name': '分红',
            'nav_change_range': (-50, -5),
            'description': '基金分红，净值下降'
        },
        'split': {
            'name': '拆分',
            'nav_change_range': (-90, -30),
            'description': '基金拆分，净值降低，份额增加'
        },
        'merger': {
            'name': '合并',
            'nav_change_range': (30, 200),
            'description': '基金合并，净值提高，份额减少'
        }
    }
    
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://fund.eastmoney.com/'
        }
        self.timeout = 10
    
    def detect_event(self, fund_code: str, prev_nav: float, curr_nav: float,
                    event_date: date = None, db: Session = None) -> Optional[Dict]:
        """
        检测是否发生特殊事件
        
        Args:
            fund_code: 基金代码
            prev_nav: 前一日净值
            curr_nav: 当前净值
            event_date: 事件日期
            db: 数据库会话
        
        Returns:
            {
                'event_type': 事件类型,
                'event_name': 事件名称,
                'change_pct': 变化百分比,
                'amount': 分红金额（如适用）,
                'confirmed': 是否已确认
            }
        """
        if not prev_nav or prev_nav <= 0:
            return None
        
        change_pct = (curr_nav - prev_nav) / prev_nav * 100
        
        for event_type, event_info in self.EVENT_TYPES.items():
            min_change, max_change = event_info['nav_change_range']
            
            if min_change <= change_pct <= max_change:
                event_data = {
                    'event_type': event_type,
                    'event_name': event_info['name'],
                    'change_pct': change_pct,
                    'description': event_info['description'],
                    'confirmed': False
                }
                
                if event_type == 'dividend':
                    dividend_info = self._fetch_dividend_info(fund_code, event_date)
                    if dividend_info:
                        event_data['amount'] = dividend_info.get('amount')
                        event_data['record_date'] = dividend_info.get('record_date')
                        event_data['pay_date'] = dividend_info.get('pay_date')
                        event_data['confirmed'] = True
                
                return event_data
        
        return None
    
    def _fetch_dividend_info(self, fund_code: str, event_date: date = None) -> Optional[Dict]:
        """从天天基金获取分红信息"""
        try:
            url = f"http://fund.eastmoney.com/f10/fhsp_{fund_code}.html"
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'
            
            text = response.text
            
            pattern = r'<tr[^>]*>.*?(\d{4}-\d{2}-\d{2}).*?(\d+\.\d+).*?</tr>'
            matches = re.findall(pattern, text, re.DOTALL)
            
            if matches:
                latest = matches[0]
                dividend_date = datetime.strptime(latest[0], '%Y-%m-%d').date()
                
                if event_date and abs((dividend_date - event_date).days) <= 7:
                    return {
                        'amount': float(latest[1]),
                        'record_date': str(dividend_date),
                        'pay_date': str(dividend_date + timedelta(days=7))
                    }
            
        except Exception as e:
            print(f"[SpecialEvent] 获取分红信息失败 {fund_code}: {e}")
        
        return None
    
    def check_and_mark_fund(self, fund_code: str, db: Session) -> Dict:
        """
        检查基金历史数据，标记特殊事件
        
        Returns:
            {
                'checked': 检查的记录数,
                'events_found': 发现的事件数,
                'events': 事件列表
            }
        """
        history = db.query(FundHistory).filter(
            FundHistory.fund_code == fund_code
        ).order_by(FundHistory.nav_date.asc()).all()
        
        if len(history) < 2:
            return {'checked': len(history), 'events_found': 0, 'events': []}
        
        events = []
        for i in range(1, len(history)):
            prev = history[i-1]
            curr = history[i]
            
            event = self.detect_event(
                fund_code,
                prev.nav,
                curr.nav,
                curr.nav_date,
                db
            )
            
            if event:
                events.append({
                    'date': str(curr.nav_date),
                    **event
                })
                
                curr.data_quality = 'special_event'
                curr.quality_note = f"{event['event_name']}，变化{event['change_pct']:+.2f}%"
        
        if events:
            db.commit()
        
        return {
            'checked': len(history),
            'events_found': len(events),
            'events': events
        }


class AnomalyDataHandler:
    """
    异常数据处理器
    
    功能：
    1. 检测异常净值
    2. 标记数据质量
    3. 尝试数据修复
    """
    
    ANOMALY_THRESHOLD = {
        'single_day_change': 10.0,
        'nav_zero': True,
        'nav_negative': True,
        'nav_too_high': 1000.0
    }
    
    def detect_anomaly(self, prev_nav: float, curr_nav: float) -> Dict:
        """
        检测净值异常
        
        Returns:
            {
                'is_anomaly': 是否异常,
                'anomaly_type': 异常类型,
                'severity': 严重程度,
                'description': 描述
            }
        """
        if curr_nav is None:
            return {
                'is_anomaly': True,
                'anomaly_type': 'missing',
                'severity': 'high',
                'description': '净值数据缺失'
            }
        
        if curr_nav <= 0:
            return {
                'is_anomaly': True,
                'anomaly_type': 'invalid',
                'severity': 'critical',
                'description': f'净值无效: {curr_nav}'
            }
        
        if curr_nav > self.ANOMALY_THRESHOLD['nav_too_high']:
            return {
                'is_anomaly': True,
                'anomaly_type': 'abnormal_high',
                'severity': 'medium',
                'description': f'净值异常高: {curr_nav}'
            }
        
        if prev_nav and prev_nav > 0:
            change_pct = abs((curr_nav - prev_nav) / prev_nav * 100)
            
            if change_pct > self.ANOMALY_THRESHOLD['single_day_change']:
                return {
                    'is_anomaly': True,
                    'anomaly_type': 'large_change',
                    'severity': 'medium',
                    'description': f'单日变化{change_pct:.2f}%超过阈值'
                }
        
        return {
            'is_anomaly': False,
            'anomaly_type': None,
            'severity': None,
            'description': '正常'
        }
    
    def mark_fund_data_quality(self, fund_code: str, db: Session) -> Dict:
        """
        标记基金数据质量
        
        Returns:
            {
                'total': 总记录数,
                'normal': 正常数,
                'anomaly': 异常数,
                'anomaly_details': 异常详情
            }
        """
        history = db.query(FundHistory).filter(
            FundHistory.fund_code == fund_code
        ).order_by(FundHistory.nav_date.asc()).all()
        
        fund = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
        
        normal_count = 0
        anomaly_count = 0
        anomaly_details = []
        
        for i, record in enumerate(history):
            prev_nav = history[i-1].nav if i > 0 else None
            curr_nav = record.nav
            
            result = self.detect_anomaly(prev_nav, curr_nav)
            
            if result['is_anomaly']:
                anomaly_count += 1
                record.data_quality = 'anomaly'
                record.quality_note = result['description']
                anomaly_details.append({
                    'date': str(record.nav_date),
                    **result
                })
            else:
                normal_count += 1
                record.data_quality = 'normal'
                record.quality_note = None
        
        if fund:
            if anomaly_count == 0:
                fund.data_quality = 'normal'
                fund.data_quality_note = None
            elif anomaly_count <= len(history) * 0.1:
                fund.data_quality = 'warning'
                fund.data_quality_note = f'存在{anomaly_count}条异常数据'
            else:
                fund.data_quality = 'error'
                fund.data_quality_note = f'异常数据占比{anomaly_count/len(history)*100:.1f}%'
        
        db.commit()
        
        return {
            'total': len(history),
            'normal': normal_count,
            'anomaly': anomaly_count,
            'anomaly_details': anomaly_details
        }


class RetryQueueManager:
    """
    重试队列管理器
    
    功能：
    1. 管理失败的抓取任务
    2. 定时重试
    3. 记录重试历史
    """
    
    DEFAULT_RETRY_INTERVAL = timedelta(hours=1)
    MAX_RETRY = 3
    
    def add_to_retry_queue(self, fund_code: str, retry_type: str, 
                          error_message: str, db: Session = None) -> FundSyncRetry:
        """
        添加到重试队列
        
        Args:
            fund_code: 基金代码
            retry_type: 重试类型 (nav/info/history)
            error_message: 错误信息
            db: 数据库会话
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        
        try:
            existing = db.query(FundSyncRetry).filter(
                FundSyncRetry.fund_code == fund_code,
                FundSyncRetry.retry_type == retry_type,
                FundSyncRetry.status == 'pending'
            ).first()
            
            if existing:
                existing.retry_count += 1
                existing.error_message = error_message
                existing.updated_at = datetime.now()
                db.commit()
                return existing
            
            retry_task = FundSyncRetry(
                fund_code=fund_code,
                retry_type=retry_type,
                error_message=error_message,
                retry_count=0,
                max_retry=self.MAX_RETRY,
                next_retry_time=datetime.now() + self.DEFAULT_RETRY_INTERVAL,
                status='pending'
            )
            
            db.add(retry_task)
            db.commit()
            db.refresh(retry_task)
            
            return retry_task
            
        finally:
            if close_db:
                db.close()
    
    def get_pending_retries(self, db: Session = None) -> List[FundSyncRetry]:
        """获取待重试的任务"""
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        
        try:
            tasks = db.query(FundSyncRetry).filter(
                FundSyncRetry.status == 'pending',
                FundSyncRetry.next_retry_time <= datetime.now(),
                FundSyncRetry.retry_count < FundSyncRetry.max_retry
            ).all()
            
            return tasks
            
        finally:
            if close_db:
                db.close()
    
    def process_retry(self, task: FundSyncRetry, db: Session) -> Dict:
        """
        处理重试任务
        
        Returns:
            {
                'success': 是否成功,
                'message': 消息,
                'should_retry': 是否应该继续重试
            }
        """
        from src.fund.multi_source_api import multi_source_api
        
        task.retry_count += 1
        task.updated_at = datetime.now()
        
        try:
            if task.retry_type == 'nav':
                result = multi_source_api.get_nav_with_validation(task.fund_code)
                success = result.get('nav') is not None
                
            elif task.retry_type == 'history':
                result = multi_source_api.get_history_with_validation(task.fund_code)
                success = result.get('count', 0) > 0
                
            elif task.retry_type == 'info':
                result = multi_source_api.get_fund_detail(task.fund_code)
                success = result is not None
                
            else:
                success = False
                result = None
            
            if success:
                task.status = 'success'
                db.commit()
                return {
                    'success': True,
                    'message': f'{task.retry_type}重试成功',
                    'should_retry': False
                }
            else:
                if task.retry_count >= task.max_retry:
                    task.status = 'failed'
                    db.commit()
                    return {
                        'success': False,
                        'message': f'{task.retry_type}重试{task.retry_count}次后失败',
                        'should_retry': False
                    }
                else:
                    task.next_retry_time = datetime.now() + self.DEFAULT_RETRY_INTERVAL
                    db.commit()
                    return {
                        'success': False,
                        'message': f'{task.retry_type}重试失败，将在1小时后重试',
                        'should_retry': True
                    }
                    
        except Exception as e:
            task.error_message = str(e)
            
            if task.retry_count >= task.max_retry:
                task.status = 'failed'
            else:
                task.next_retry_time = datetime.now() + self.DEFAULT_RETRY_INTERVAL
            
            db.commit()
            
            return {
                'success': False,
                'message': f'{task.retry_type}重试异常: {str(e)}',
                'should_retry': task.retry_count < task.max_retry
            }
    
    def process_all_pending(self, db: Session = None) -> Dict:
        """
        处理所有待重试任务
        
        Returns:
            {
                'total': 总任务数,
                'success': 成功数,
                'failed': 失败数,
                'pending': 仍待处理数
            }
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        
        try:
            tasks = self.get_pending_retries(db)
            
            result = {
                'total': len(tasks),
                'success': 0,
                'failed': 0,
                'pending': 0,
                'details': []
            }
            
            for task in tasks:
                retry_result = self.process_retry(task, db)
                
                if retry_result['success']:
                    result['success'] += 1
                elif not retry_result['should_retry']:
                    result['failed'] += 1
                else:
                    result['pending'] += 1
                
                result['details'].append({
                    'fund_code': task.fund_code,
                    'retry_type': task.retry_type,
                    'retry_count': task.retry_count,
                    'result': retry_result['message']
                })
            
            return result
            
        finally:
            if close_db:
                db.close()


special_event_detector = SpecialEventDetector()
anomaly_handler = AnomalyDataHandler()
retry_queue_manager = RetryQueueManager()


if __name__ == '__main__':
    detector = SpecialEventDetector()
    
    event = detector.detect_event('161725', 1.5, 1.35)
    print("检测事件:", event)
    
    handler = AnomalyDataHandler()
    result = handler.detect_anomaly(1.5, 1.35)
    print("异常检测:", result)
