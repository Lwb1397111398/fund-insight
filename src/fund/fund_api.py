"""
基金数据模块 - 支持每日自动抓取和历史存储
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import re
import time
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
    
    def _history_fallback_info(self, fund_code: str) -> Optional[Dict]:
        """fundgz 实时接口失效时，用最近一天历史净值兜底。

        名称/类型刻意返回 None，避免覆盖库内已有字段。
        """
        history = self.get_fund_history(fund_code, days=1)
        if not history:
            return None

        latest = history[0]
        nav_date = latest.get('date')
        return {
            'fund_code': fund_code,
            'fund_name': None,
            'fund_type': None,
            'nav': latest.get('nav'),
            'nav_date': (
                nav_date.strftime('%Y-%m-%d')
                if hasattr(nav_date, 'strftime')
                else (str(nav_date) if nav_date else None)
            ),
            'estimate_nav': None,
            'estimate_date': None,
            'day_growth': latest.get('growth'),
        }

    def get_fund_info(self, fund_code: str, allow_fallback: bool = True) -> Optional[Dict]:
        """获取基金实时信息

        返回数据包含：
        - nav/nav_date: 实际净值和净值日期（来自 jzrq/dwjz）
        - estimate_nav/estimate_date: 估值和估值时间（来自 gsz/gztime）
        - day_growth: 估值涨跌幅

        fundgz 实时接口失效（404 HTML / 超时 / 网络错误）时，
        自动用历史净值接口兜底，保证基金更新链路可用。

        allow_fallback=False 时仅尝试实时接口，失效直接返回 None，
        供抓取验证等场景复用，避免一次验证触发多次历史接口调用（防限流）。
        """
        try:
            url = f"{self.base_url}/js/{fund_code}.js"
            response = self.session.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'

            text = response.text
            if 'jsonpgz' in text:
                match = re.search(r'jsonpgz\((.+)\)', text)
                if match:
                    data = json.loads(match.group(1))

                    # 实际净值日期（如 "2026-06-04"）
                    actual_nav_date = data.get('jzrq', '')
                    # 估值时间（如 "2026-06-05 15:00"）
                    estimate_time = data.get('gztime', '').split(' ')[0]

                    return {
                        'fund_code': data.get('fundcode'),
                        'fund_name': data.get('name'),
                        # 实际净值（收盘后确认的净值）
                        'nav': float(data.get('dwjz', 0) or 0),
                        'nav_date': actual_nav_date,
                        # 估值数据（盘中实时）
                        'estimate_nav': float(data.get('gsz', 0) or 0),
                        'estimate_date': estimate_time,
                        # 涨跌幅使用估值涨跌幅
                        'day_growth': float(data.get('gszzl', 0) or 0),
                        'fund_type': data.get('fundtype', '')
                    }

            # fundgz 接口失效时（返回 404 页面等），用历史净值兜底
            logger.warning(f"基金{fund_code}实时接口无有效数据，尝试历史净值兜底")
            return self._history_fallback_info(fund_code) if allow_fallback else None
        except requests.exceptions.Timeout:
            logger.warning(f"获取基金{fund_code}信息超时，尝试历史净值兜底")
            return self._history_fallback_info(fund_code) if allow_fallback else None
        except requests.exceptions.RequestException as e:
            logger.warning(f"获取基金{fund_code}网络错误: {e}，尝试历史净值兜底")
            return self._history_fallback_info(fund_code) if allow_fallback else None
        except Exception as e:
            logger.error(f"获取基金{fund_code}信息失败: {e}，尝试历史净值兜底")
            if not allow_fallback:
                return None
            try:
                return self._history_fallback_info(fund_code)
            except Exception:
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
    
    def verify_fund_fetchable(self, fund_code: str, input_name: Optional[str] = None) -> Dict:
        """验证基金代码是否能从数据源正常抓取。

        依次调用实时信息接口与历史净值接口，返回结构化验证结果。
        供"板块映射审查 / 添加基金"流程在保存前做抓取可行性检查。

        Args:
            fund_code: 6 位数字基金代码
            input_name: 用户填写的基金名称（可选，仅用于回填对比，不作为通过依据）

        Returns:
            dict 包含：
            - ok: 是否可正常抓取（信息或历史至少其一有效）
            - code/input_name/api_name/api_nav/nav_date/history_count
            - message: 面向用户的一句话结论
        """
        code = (fund_code or '').strip()
        if not re.fullmatch(r'\d{6}', code):
            return {
                'ok': False,
                'code': code,
                'input_name': input_name,
                'api_name': None,
                'api_nav': None,
                'nav_date': None,
                'history_count': 0,
                'message': '基金代码格式不正确，应为 6 位数字'
            }

        info = None
        try:
            # 仅尝试实时接口（不触发历史兜底），用于补充官方名称/实时净值
            info = self.get_fund_info(code, allow_fallback=False)
        except Exception as e:
            logger.warning(f"验证基金{code}时信息接口异常: {e}")

        history: List[Dict] = []
        try:
            # 历史净值是判断"能否抓取"的权威依据，只调一次
            history = self.get_fund_history(code, days=7)
        except Exception as e:
            logger.warning(f"验证基金{code}时历史接口异常: {e}")

        nav = (info or {}).get('nav')
        api_name = (info or {}).get('fund_name')
        nav_date = (info or {}).get('nav_date')
        # 实时接口没给净值日期时，用历史最新一条兜底展示
        if not nav_date and history:
            latest_date = history[0].get('date')
            nav_date = (
                latest_date.strftime('%Y-%m-%d')
                if hasattr(latest_date, 'strftime')
                else (str(latest_date) if latest_date else None)
            )
        ok = bool((nav and nav > 0) or history)

        if ok:
            if api_name:
                message = f'验证通过：{api_name}'
            else:
                message = '验证通过：可抓取净值数据（接口未返回名称）'
        elif info is None and not history:
            message = '验证失败：接口无有效数据，该基金可能已停牌/清盘或代码有误'
        else:
            message = '验证失败：未抓取到有效净值数据'

        return {
            'ok': ok,
            'code': code,
            'input_name': input_name,
            'api_name': api_name,
            'api_nav': nav,
            'nav_date': nav_date,
            'history_count': len(history),
            'message': message
        }

    def verify_funds_batch(self, items: List[Dict], delay: float = 0.3) -> Dict:
        """批量验证多只基金能否从数据源抓取，用于一键排查问题基金。

        Args:
            items: [{'sector_name','fund_code','fund_name'}, ...]
            delay: 相邻两次网络验证之间的间隔秒数，降低被数据源限流概率。

        Returns:
            dict：total / ok_count / problem_count / results / problems
            - results: 每个输入条目一条（含验证结果与所属板块）
            - problems: 仅抓取失败的条目
        相同基金代码只发起一次网络验证，结果复用到所有引用它的板块。
        """
        cache: Dict[str, Dict] = {}
        results: List[Dict] = []
        for idx, item in enumerate(items or []):
            code = (item.get('fund_code') or '').strip()
            name = item.get('fund_name') or ''
            sector = item.get('sector_name') or ''
            if code in cache:
                verify = dict(cache[code])
            else:
                # 仅在真正发起新的一次网络验证前做节流间隔
                if cache and delay > 0:
                    time.sleep(delay)
                verify = self.verify_fund_fetchable(code, name)
                cache[code] = verify
            row = {
                'sector_name': sector,
                'fund_code': verify.get('code') or code,
                'fund_name': name,
                'ok': verify.get('ok', False),
                'api_name': verify.get('api_name'),
                'api_nav': verify.get('api_nav'),
                'nav_date': verify.get('nav_date'),
                'history_count': verify.get('history_count', 0),
                'message': verify.get('message', ''),
            }
            results.append(row)

        problems = [r for r in results if not r['ok']]
        return {
            'total': len(results),
            'checked_codes': len(cache),
            'ok_count': len(results) - len(problems),
            'problem_count': len(problems),
            'results': results,
            'problems': problems,
        }

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
                if info.get('fund_name'):
                    fund.fund_name = info['fund_name']
                if info.get('fund_type'):
                    fund.fund_type = info['fund_type']
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

            # 仅当自建会话时提交；外部传入的 db 由调用方控制事务边界，
            # 避免提前提交调用方 session 上的其它 pending 改动（与 update_fund_history 一致）
            if close_db:
                db.commit()
                db.refresh(fund)
            else:
                db.flush()
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
            # 批量查询已存在的日期，避免逐条查询
            existing_dates = set(
                r[0] for r in db.query(FundHistory.nav_date).filter(
                    FundHistory.fund_code == fund_code
                ).all()
            )

            # 批量查询已存在的日期，避免逐条查询
            existing_dates = set(
                r[0] for r in db.query(FundHistory.nav_date).filter(
                    FundHistory.fund_code == fund_code
                ).all()
            )

            # 获取基金信息（只查询一次）
            fund_info = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            fund_name = fund_info.fund_name if fund_info else ''

            # 一次性把已存在记录载入内存映射，消除循环内逐条查询的 N+1
            existing_map = {
                r.nav_date: r for r in db.query(FundHistory).filter(
                    FundHistory.fund_code == fund_code,
                    FundHistory.nav_date.in_([it['date'] for it in history if it['date'] in existing_dates])
                ).all()
            } if existing_dates else {}

            count = 0
            for item in history:
                if item['date'] in existing_dates:
                    # 更新已存在的记录（内存查找，无额外查询）
                    existing = existing_map.get(item['date'])
                    if existing:
                        existing.nav = item['nav']
                        existing.day_growth = item['growth']
                else:
                    # 插入新记录
                    record = FundHistory(
                        fund_code=fund_code,
                        fund_name=fund_name,
                        nav_date=item['date'],
                        nav=item['nav'],
                        day_growth=item['growth']
                    )
                    db.add(record)
                    count += 1

            # 计算周涨跌幅和月涨跌幅
            self._calculate_growth_rates(fund_code, db)

            # 仅当使用内部创建的 session 时才提交，外部 session 由调用方管理事务
            if close_db:
                db.commit()
            return count

        except Exception as e:
            logger.error(f"更新历史净值失败: {e}")
            if close_db:
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
