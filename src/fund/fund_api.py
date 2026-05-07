"""
基金数据模块 - 支持每日自动抓取和历史存储
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import re
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional
from sqlalchemy.orm import Session
import sys
import os
import logging

if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.core.config import config
from src.models.database import FundInfo, FundHistory, SessionLocal

logger = logging.getLogger(__name__)


class FundAPI:
    """天天基金API封装"""
    
    def __init__(self):
        self.base_url = "http://fundgz.1234567.com.cn"
        self.search_url = "http://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
        self.history_url = "http://api.fund.eastmoney.com/f10/lsjz"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://fund.eastmoney.com/'
        }
        self.timeout = config.FUND_API_TIMEOUT

        self.session = requests.Session()
        retry_strategy = Retry(
            total=config.FUND_API_MAX_RETRIES,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def close(self):
        """关闭 Session，释放连接池资源"""
        if self.session:
            self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
    
    def get_fund_info(self, fund_code: str) -> Optional[Dict]:
        """获取基金实时信息"""
        try:
            url = f"{self.base_url}/js/{fund_code}.js"
            response = self.session.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'
            
            text = response.text
            if 'jsonpgz' in text:
                match = re.search(r'jsonpgz\((.+)\)', text)
                if match:
                    data = json.loads(match.group(1))
                    
                    return {
                        'fund_code': data.get('fundcode'),
                        'fund_name': data.get('name'),
                        'nav': float(data.get('gsz', 0) or 0),
                        'nav_date': data.get('gztime', '').split(' ')[0],
                        'day_growth': float(data.get('gszzl', 0) or 0),
                        'fund_type': data.get('fundtype', '')
                    }
        except requests.exceptions.Timeout:
            logger.warning(f"获取基金{fund_code}信息超时")
        except requests.exceptions.RequestException as e:
            logger.warning(f"获取基金{fund_code}网络错误: {e}")
        except Exception as e:
            logger.error(f"获取基金{fund_code}信息失败: {e}")
        return None
    
    def get_fund_history(self, fund_code: str, days: int = 30) -> List[Dict]:
        """获取基金历史净值"""
        try:
            params = {
                'fundCode': fund_code,
                'pageIndex': 1,
                'pageSize': min(days, 60),
                'startDate': '',
                'endDate': '',
                'perFundType': ''
            }
            
            headers = self.headers.copy()
            headers['Referer'] = f'https://fund.eastmoney.com/f10/jjjz_{fund_code}.html'
            
            response = self.session.get(
                self.history_url,
                params=params,
                headers=headers,
                timeout=self.timeout
            )
            response.encoding = 'utf-8'
            data = response.json()
            
            results = []
            if 'Data' in data and 'LSJZList' in data['Data']:
                lsjz_list = data['Data']['LSJZList']
                
                if not lsjz_list:
                    logger.debug(f"基金 {fund_code} 历史净值列表为空")
                    return []
                
                for item in lsjz_list:
                    try:
                        nav_date = datetime.strptime(item.get('FSRQ'), '%Y-%m-%d').date()
                        results.append({
                            'date': nav_date,
                            'nav': float(item.get('DWJZ', 0) or 0),
                            'growth': float(item.get('JZZZL', 0) or 0)
                        })
                    except Exception as e:
                        logger.warning(f"解析基金 {fund_code} 历史净值数据失败: {e}, 数据项: {item}")
                        continue
            else:
                logger.warning(f"基金 {fund_code} API返回数据格式异常")
            
            return results
            
        except Exception as e:
            logger.error(f"获取基金{fund_code}历史数据失败: {e}")
            return []
    
    def search_fund(self, keyword: str) -> List[Dict]:
        """搜索基金"""
        try:
            params = {
                'm': '1',
                'key': keyword
            }
            response = self.session.get(
                self.search_url,
                params=params,
                timeout=self.timeout
            )
            response.encoding = 'utf-8'
            data = response.json()
            
            results = []
            if 'Datas' in data:
                for item in data['Datas'][:10]:
                    results.append({
                        'fund_code': item.get('CODE'),
                        'fund_name': item.get('NAME'),
                        'fund_type': item.get('FUNDTYPE', '')
                    })
            return results
        except Exception as e:
            logger.error(f"搜索基金失败: {e}")
            return []


class FundDataManager:
    """基金数据管理器 - 处理数据库存储和查询"""
    
    def __init__(self):
        self.api = FundAPI()
    
    def update_fund_info(self, fund_code: str, db: Session = None) -> Optional[FundInfo]:
        """更新基金信息到数据库
        
        注意：日涨幅(day_growth)使用历史净值中的实际涨跌幅，而不是估值涨跌幅
        """
        info = self.api.get_fund_info(fund_code)
        if not info:
            return None
        
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        
        try:
            fund = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            nav_date = None
            if info.get('nav_date'):
                try:
                    nav_date = datetime.strptime(info['nav_date'], '%Y-%m-%d').date()
                except (ValueError, TypeError) as e:
                    logger.warning(f"日期解析失败: {info.get('nav_date')}, 错误: {e}")
            
            # 获取历史净值中的实际涨跌幅（更准确）
            history = self.api.get_fund_history(fund_code, days=1)
            actual_day_growth = None
            actual_nav_date = None
            
            if history:
                latest = history[0]
                actual_day_growth = latest.get('growth')
                try:
                    actual_nav_date = latest.get('date')
                    if isinstance(actual_nav_date, str):
                        actual_nav_date = datetime.strptime(actual_nav_date, '%Y-%m-%d').date()
                except (ValueError, TypeError) as e:
                    logger.debug(f"解析历史净值日期失败: {e}")
            
            # 优先使用历史净值中的实际数据
            day_growth = actual_day_growth if actual_day_growth is not None else info.get('day_growth')
            if actual_nav_date:
                nav_date = actual_nav_date
            
            if fund:
                fund.fund_name = info.get('fund_name')
                fund.fund_type = info.get('fund_type')
                fund.latest_nav = info.get('nav')
                fund.nav_date = nav_date
                fund.day_growth = day_growth
            else:
                fund = FundInfo(
                    fund_code=fund_code,
                    fund_name=info.get('fund_name'),
                    fund_type=info.get('fund_type'),
                    latest_nav=info.get('nav'),
                    nav_date=nav_date,
                    day_growth=day_growth
                )
                db.add(fund)
            
            db.commit()
            db.refresh(fund)
            return fund
            
        except Exception as e:
            logger.error(f"更新基金信息失败: {e}")
            db.rollback()
            return None
        finally:
            if close_db:
                db.close()
    
    def update_fund_history(self, fund_code: str, days: int = 30, db: Session = None) -> int:
        """更新基金历史净值到数据库"""
        history = self.api.get_fund_history(fund_code, days)
        if not history:
            return 0
        
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        
        try:
            count = 0
            for item in history:
                existing = db.query(FundHistory).filter(
                    FundHistory.fund_code == fund_code,
                    FundHistory.nav_date == item['date']
                ).first()
                
                if existing:
                    existing.nav = item['nav']
                    existing.day_growth = item['growth']
                else:
                    fund_info = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
                    record = FundHistory(
                        fund_code=fund_code,
                        fund_name=fund_info.fund_name if fund_info else '',
                        nav_date=item['date'],
                        nav=item['nav'],
                        day_growth=item['growth']
                    )
                    db.add(record)
                    count += 1
            
            # 计算周涨跌幅和月涨跌幅
            self._calculate_growth_rates(fund_code, db)
            
            db.commit()
            return count
            
        except Exception as e:
            logger.error(f"更新历史净值失败: {e}")
            db.rollback()
            return 0
        finally:
            if close_db:
                db.close()
    
    def _calculate_growth_rates(self, fund_code: str, db: Session):
        """计算周涨跌幅和月涨跌幅"""
        try:
            latest = db.query(FundHistory).filter(
                FundHistory.fund_code == fund_code
            ).order_by(FundHistory.nav_date.desc()).first()
            
            if not latest or latest.nav is None:
                return
            
            fund = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            if not fund:
                return
            
            week_ago = db.query(FundHistory).filter(
                FundHistory.fund_code == fund_code,
                FundHistory.nav_date < latest.nav_date
            ).order_by(FundHistory.nav_date.desc()).offset(4).first()
            
            if week_ago and week_ago.nav and week_ago.nav > 0:
                fund.week_growth = round((latest.nav - week_ago.nav) / week_ago.nav * 100, 2)
            else:
                fund.week_growth = None
            
            month_ago = db.query(FundHistory).filter(
                FundHistory.fund_code == fund_code,
                FundHistory.nav_date < latest.nav_date
            ).order_by(FundHistory.nav_date.desc()).offset(19).first()
            
            if month_ago and month_ago.nav and month_ago.nav > 0:
                fund.month_growth = round((latest.nav - month_ago.nav) / month_ago.nav * 100, 2)
            else:
                fund.month_growth = None
            
        except Exception as e:
            logger.error(f"计算涨跌幅失败: {e}")
    
    def get_nav_by_date(self, fund_code: str, target_date: date, db: Session = None) -> Optional[float]:
        """
        获取指定日期的净值
        如果当天没有数据，返回最近的交易日净值
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        
        try:
            record = db.query(FundHistory).filter(
                FundHistory.fund_code == fund_code,
                FundHistory.nav_date <= target_date
            ).order_by(FundHistory.nav_date.desc()).first()
            
            if record:
                return record.nav
            
            history = self.api.get_fund_history(fund_code, days=60)
            for item in history:
                if item['date'] <= target_date:
                    return item['nav']
            
            return None
            
        except Exception as e:
            logger.error(f"获取历史净值失败: {e}")
            return None
        finally:
            if close_db:
                db.close()
    
    def calculate_change(self, fund_code: str, start_date: date, end_date: date, 
                         db: Session = None) -> Optional[Dict]:
        """
        计算两个日期之间的涨跌幅
        
        返回:
        {
            'start_nav': float,
            'end_nav': float,
            'change': float,  # 涨跌幅百分比
            'start_date': date,
            'end_date': date
        }
        """
        start_nav = self.get_nav_by_date(fund_code, start_date, db)
        end_nav = self.get_nav_by_date(fund_code, end_date, db)
        
        if start_nav is None or end_nav is None or start_nav == 0:
            return None
        
        change = (end_nav - start_nav) / start_nav * 100
        
        return {
            'start_nav': start_nav,
            'end_nav': end_nav,
            'change': round(change, 2),
            'start_date': start_date,
            'end_date': end_date
        }


fund_api = FundAPI()
fund_data_manager = FundDataManager()


if __name__ == '__main__':
    api = FundAPI()
    
    info = api.get_fund_info('000001')
    print("基金信息:", info)
    
    history = api.get_fund_history('000001', days=7)
    print("历史净值:", history)
    
    dm = FundDataManager()
    dm.update_fund_info('000001')
    dm.update_fund_history('000001', days=30)
    
    from datetime import date
    change = dm.calculate_change('000001', date(2024, 1, 1), date(2024, 1, 31))
    print("月涨跌:", change)
