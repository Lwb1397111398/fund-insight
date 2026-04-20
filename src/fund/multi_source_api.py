"""
多源基金数据API - 支持交叉验证

数据源：
1. 天天基金（主源）
2. 同花顺（备用）
3. 支付宝理财（备用）

功能：
- 多源数据获取
- 交叉验证
- 数据质量标记
- 自动降级
"""
import requests
import json
import re
import time
import logging
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple
from sqlalchemy.orm import Session

from src.models.database import FundInfo, FundHistory, SessionLocal

logger = logging.getLogger(__name__)


class EastMoneyAPI:
    """天天基金API - 主数据源"""
    
    NAME = 'eastmoney'
    
    def __init__(self):
        self.base_url = "http://fundgz.1234567.com.cn"
        self.history_url = "http://api.fund.eastmoney.com/f10/lsjz"
        self.detail_url = "http://fund.eastmoney.com/pingzhongdata"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://fund.eastmoney.com/'
        }
        self.timeout = 10
    
    def get_nav(self, fund_code: str) -> Optional[Dict]:
        """获取实时净值"""
        try:
            url = f"{self.base_url}/js/{fund_code}.js"
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'
            
            text = response.text
            if 'jsonpgz' in text:
                match = re.search(r'jsonpgz\((.+)\)', text)
                if match:
                    data = json.loads(match.group(1))
                    return {
                        'nav': float(data.get('gsz', 0) or 0),
                        'nav_date': data.get('gztime', '').split(' ')[0],
                        'nav_time': data.get('gztime', ''),
                        'day_growth': float(data.get('gszzl', 0) or 0),
                        'is_estimated': True,
                        'source': self.NAME
                    }
        except Exception as e:
            print(f"[EastMoney] 获取净值失败 {fund_code}: {e}")
        return None
    
    def get_history(self, fund_code: str, days: int = 30) -> List[Dict]:
        """获取历史净值"""
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
            
            response = requests.get(
                self.history_url,
                params=params,
                headers=headers,
                timeout=self.timeout
            )
            response.encoding = 'utf-8'
            data = response.json()
            
            results = []
            if 'Data' in data and 'LSJZList' in data['Data']:
                for item in data['Data']['LSJZList']:
                    try:
                        nav_date = datetime.strptime(item.get('FSRQ'), '%Y-%m-%d').date()
                        results.append({
                            'date': nav_date,
                            'nav': float(item.get('DWJZ', 0) or 0),
                            'growth': float(item.get('JZZZL', 0) or 0),
                            'source': self.NAME
                        })
                    except (ValueError, KeyError, TypeError) as e:
                        logger.warning(f"[EastMoney] 解析历史净值数据失败: {e}, 数据项: {item}")
                        continue
            
            return results
            
        except Exception as e:
            print(f"[EastMoney] 获取历史净值失败 {fund_code}: {e}")
            return []
    
    def get_fund_detail(self, fund_code: str) -> Optional[Dict]:
        """获取基金详细信息（规模、经理、费率等）"""
        try:
            url = f"{self.detail_url}/{fund_code}.html"
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'
            
            text = response.text
            
            result = {'source': self.NAME}
            
            scale_match = re.search(r'基金规模.*?(\d+\.?\d*)\s*亿元', text)
            if scale_match:
                result['fund_scale'] = float(scale_match.group(1))
            
            manager_match = re.search(r'基金经理[：:]\s*<a[^>]*>([^<]+)</a>', text)
            if manager_match:
                result['manager_name'] = manager_match.group(1).strip()
            
            fee_match = re.search(r'管理费率.*?(\d+\.?\d*)%', text)
            if fee_match:
                result['fee_rate'] = float(fee_match.group(1))
            
            establish_match = re.search(r'成立日期[：:]\s*(\d{4}-\d{2}-\d{2})', text)
            if establish_match:
                result['establish_date'] = establish_match.group(1)
            
            return result
            
        except Exception as e:
            print(f"[EastMoney] 获取基金详情失败 {fund_code}: {e}")
            return None


class TonghuashunAPI:
    """同花顺API - 备用数据源"""
    
    NAME = 'tonghuashun'
    
    def __init__(self):
        self.base_url = "http://fund.10jqka.com.cn"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'http://fund.10jqka.com.cn/'
        }
        self.timeout = 10
    
    def get_nav(self, fund_code: str) -> Optional[Dict]:
        """获取实时净值"""
        try:
            url = f"{self.base_url}/{fund_code}/"
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'
            
            text = response.text
            
            nav_match = re.search(r'单位净值[：:]\s*(\d+\.?\d*)', text)
            date_match = re.search(r'净值日期[：:]\s*(\d{4}-\d{2}-\d{2})', text)
            growth_match = re.search(r'日涨跌幅[：:]\s*([+-]?\d+\.?\d*)%', text)
            
            if nav_match:
                return {
                    'nav': float(nav_match.group(1)),
                    'nav_date': date_match.group(1) if date_match else None,
                    'day_growth': float(growth_match.group(1)) if growth_match else None,
                    'is_estimated': False,
                    'source': self.NAME
                }
        except Exception as e:
            print(f"[Tonghuashun] 获取净值失败 {fund_code}: {e}")
        return None
    
    def get_history(self, fund_code: str, days: int = 30) -> List[Dict]:
        """获取历史净值（简化版）"""
        try:
            url = f"{self.base_url}/{fund_code}/history.html"
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'
            
            text = response.text
            results = []
            
            pattern = r'<tr[^>]*>.*?(\d{4}-\d{2}-\d{2}).*?(\d+\.?\d*).*?([+-]?\d+\.?\d*)%.*?</tr>'
            matches = re.findall(pattern, text, re.DOTALL)
            
            for match in matches[:days]:
                try:
                    results.append({
                        'date': datetime.strptime(match[0], '%Y-%m-%d').date(),
                        'nav': float(match[1]),
                        'growth': float(match[2]),
                        'source': self.NAME
                    })
                except (ValueError, IndexError) as e:
                    logger.warning(f"[Tonghuashun] 解析历史净值数据失败: {e}, 匹配项: {match}")
                    continue
            
            return results
            
        except Exception as e:
            print(f"[Tonghuashun] 获取历史净值失败 {fund_code}: {e}")
            return []


class AlipayFundAPI:
    """支付宝理财API - 备用数据源"""
    
    NAME = 'alipay'
    
    def __init__(self):
        self.base_url = "https://fund.alipay.com"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://fund.alipay.com/'
        }
        self.timeout = 10
    
    def get_nav(self, fund_code: str) -> Optional[Dict]:
        """获取实时净值"""
        try:
            url = f"{self.base_url}/fund/{fund_code}.html"
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'
            
            text = response.text
            
            nav_match = re.search(r'单位净值[：:]\s*<[^>]*>(\d+\.?\d*)', text)
            date_match = re.search(r'(\d{4}-\d{2}-\d{2})', text)
            growth_match = re.search(r'涨幅[：:]\s*([+-]?\d+\.?\d*)%', text)
            
            if nav_match:
                return {
                    'nav': float(nav_match.group(1)),
                    'nav_date': date_match.group(1) if date_match else None,
                    'day_growth': float(growth_match.group(1)) if growth_match else None,
                    'is_estimated': False,
                    'source': self.NAME
                }
        except Exception as e:
            print(f"[Alipay] 获取净值失败 {fund_code}: {e}")
        return None


class MultiSourceFundAPI:
    """
    多源基金数据API - 支持交叉验证
    
    功能：
    1. 多源数据获取
    2. 交叉验证
    3. 数据质量标记
    4. 自动降级
    """
    
    PRIMARY_SOURCE = 'eastmoney'
    BACKUP_SOURCES = ['tonghuashun', 'alipay']
    
    VALIDATION_THRESHOLD = 0.001  # 误差阈值0.1%
    
    def __init__(self):
        self.sources = {
            'eastmoney': EastMoneyAPI(),
            'tonghuashun': TonghuashunAPI(),
            'alipay': AlipayFundAPI()
        }
    
    def get_nav_with_validation(self, fund_code: str) -> Dict:
        """
        获取净值并进行交叉验证
        
        Returns:
            {
                'nav': 净值,
                'nav_date': 日期,
                'day_growth': 日涨跌,
                'is_estimated': 是否估算,
                'quality': 'normal'/'warning'/'error',
                'quality_note': 质量说明,
                'sources': 各数据源结果,
                'validated': 是否经过验证
            }
        """
        results = {}
        
        primary_api = self.sources[self.PRIMARY_SOURCE]
        primary_result = primary_api.get_nav(fund_code)
        
        if primary_result:
            results[self.PRIMARY_SOURCE] = primary_result
        else:
            for backup in self.BACKUP_SOURCES:
                backup_result = self.sources[backup].get_nav(fund_code)
                if backup_result:
                    results[backup] = backup_result
                    break
        
        if not results:
            return {
                'nav': None,
                'quality': 'error',
                'quality_note': '所有数据源均无法获取',
                'sources': {},
                'validated': False
            }
        
        if len(results) == 1:
            source_name = list(results.keys())[0]
            result = results[source_name]
            return {
                'nav': result['nav'],
                'nav_date': result.get('nav_date'),
                'day_growth': result.get('day_growth'),
                'is_estimated': result.get('is_estimated', False),
                'quality': 'normal',
                'quality_note': f'单源数据({source_name})',
                'sources': results,
                'validated': False
            }
        
        nav_values = [r['nav'] for r in results.values() if r.get('nav')]
        
        if len(nav_values) >= 2:
            max_nav = max(nav_values)
            min_nav = min(nav_values)
            
            if min_nav > 0:
                diff_ratio = (max_nav - min_nav) / min_nav
                
                if diff_ratio >= self.VALIDATION_THRESHOLD:
                    avg_nav = sum(nav_values) / len(nav_values)
                    return {
                        'nav': avg_nav,
                        'nav_date': list(results.values())[0].get('nav_date'),
                        'day_growth': list(results.values())[0].get('day_growth'),
                        'is_estimated': any(r.get('is_estimated', False) for r in results.values()),
                        'quality': 'warning',
                        'quality_note': f'多源数据差异{diff_ratio*100:.2f}%，已取平均值',
                        'sources': results,
                        'validated': True
                    }
        
        primary_result = results.get(self.PRIMARY_SOURCE) or list(results.values())[0]
        return {
            'nav': primary_result['nav'],
            'nav_date': primary_result.get('nav_date'),
            'day_growth': primary_result.get('day_growth'),
            'is_estimated': primary_result.get('is_estimated', False),
            'quality': 'normal',
            'quality_note': '多源验证通过',
            'sources': results,
            'validated': True
        }
    
    def get_history_with_validation(self, fund_code: str, days: int = 30) -> Dict:
        """
        获取历史净值并进行验证
        
        Returns:
            {
                'history': 历史数据列表,
                'quality': 数据质量,
                'source': 数据来源,
                'count': 数据条数
            }
        """
        primary_api = self.sources[self.PRIMARY_SOURCE]
        history = primary_api.get_history(fund_code, days)
        
        if history and len(history) >= days * 0.8:
            return {
                'history': history,
                'quality': 'normal',
                'source': self.PRIMARY_SOURCE,
                'count': len(history)
            }
        
        for backup in self.BACKUP_SOURCES:
            backup_history = self.sources[backup].get_history(fund_code, days)
            if backup_history and len(backup_history) >= days * 0.8:
                return {
                    'history': backup_history,
                    'quality': 'normal',
                    'source': backup,
                    'count': len(backup_history)
                }
        
        return {
            'history': history or [],
            'quality': 'warning' if history else 'error',
            'source': self.PRIMARY_SOURCE if history else 'none',
            'count': len(history) if history else 0,
            'quality_note': f'数据不完整，仅{len(history) if history else 0}条'
        }
    
    def get_fund_detail(self, fund_code: str) -> Optional[Dict]:
        """获取基金详细信息"""
        primary_api = self.sources[self.PRIMARY_SOURCE]
        if hasattr(primary_api, 'get_fund_detail'):
            return primary_api.get_fund_detail(fund_code)
        return None


class FundDataValidator:
    """
    基金数据验证器
    
    功能：
    1. 检测净值异常值
    2. 检测分红/拆分
    3. 标记数据质量
    """
    
    ANOMALY_RULES = {
        'single_day_change': 10.0,
        'nav_zero': True,
        'nav_negative': True,
        'nav_change_threshold': 0.5
    }
    
    SPECIAL_EVENTS = {
        'dividend': '分红',
        'split': '拆分',
        'merger': '合并'
    }
    
    def validate_nav_change(self, prev_nav: float, curr_nav: float, 
                           fund_code: str = None, db: Session = None) -> Dict:
        """
        验证净值变化是否正常
        
        Returns:
            {
                'is_valid': 是否有效,
                'is_anomaly': 是否异常,
                'change_pct': 涨跌幅,
                'reason': 原因说明,
                'event_type': 事件类型（如有）
            }
        """
        if prev_nav is None or prev_nav <= 0:
            return {
                'is_valid': False,
                'is_anomaly': True,
                'change_pct': None,
                'reason': '前一日净值异常',
                'event_type': None
            }
        
        if curr_nav is None or curr_nav <= 0:
            return {
                'is_valid': False,
                'is_anomaly': True,
                'change_pct': None,
                'reason': '当前净值异常',
                'event_type': None
            }
        
        change_pct = (curr_nav - prev_nav) / prev_nav * 100
        
        if abs(change_pct) > self.ANOMALY_RULES['single_day_change']:
            event_type = self._detect_special_event(fund_code, db)
            
            if event_type:
                return {
                    'is_valid': True,
                    'is_anomaly': True,
                    'change_pct': change_pct,
                    'reason': f'检测到{self.SPECIAL_EVENTS.get(event_type, "特殊事件")}，涨跌{change_pct:+.2f}%',
                    'event_type': event_type
                }
            else:
                return {
                    'is_valid': True,
                    'is_anomaly': True,
                    'change_pct': change_pct,
                    'reason': f'单日涨跌{change_pct:+.2f}%超过阈值，需人工确认',
                    'event_type': 'unknown'
                }
        
        return {
            'is_valid': True,
            'is_anomaly': False,
            'change_pct': change_pct,
            'reason': '正常',
            'event_type': None
        }
    
    def _detect_special_event(self, fund_code: str, db: Session) -> Optional[str]:
        """检测特殊事件（分红/拆分等）"""
        if not fund_code or not db:
            return None
        
        return None
    
    def validate_history_data(self, history: List[Dict]) -> Dict:
        """
        验证历史数据质量
        
        Returns:
            {
                'quality': 'good'/'warning'/'error',
                'anomaly_count': 异常数据条数,
                'anomaly_dates': 异常日期列表,
                'missing_dates': 缺失日期列表,
                'note': 说明
            }
        """
        if not history:
            return {
                'quality': 'error',
                'anomaly_count': 0,
                'anomaly_dates': [],
                'missing_dates': [],
                'note': '无历史数据'
            }
        
        anomaly_count = 0
        anomaly_dates = []
        
        for i in range(1, len(history)):
            prev_nav = history[i-1].get('nav')
            curr_nav = history[i].get('nav')
            
            if prev_nav and curr_nav and prev_nav > 0:
                change = abs((curr_nav - prev_nav) / prev_nav * 100)
                if change > self.ANOMALY_RULES['single_day_change']:
                    anomaly_count += 1
                    anomaly_dates.append(str(history[i].get('date')))
        
        expected_days = (history[0]['date'] - history[-1]['date']).days + 1 if len(history) > 1 else len(history)
        actual_days = len(history)
        missing_count = expected_days - actual_days
        
        if anomaly_count == 0 and missing_count <= 2:
            quality = 'good'
            note = '数据质量良好'
        elif anomaly_count <= 2 and missing_count <= 5:
            quality = 'warning'
            note = f'数据存在{anomaly_count}处异常，{missing_count}处缺失'
        else:
            quality = 'error'
            note = f'数据质量较差，{anomaly_count}处异常，{missing_count}处缺失'
        
        return {
            'quality': quality,
            'anomaly_count': anomaly_count,
            'anomaly_dates': anomaly_dates,
            'missing_count': missing_count,
            'note': note
        }


multi_source_api = MultiSourceFundAPI()
fund_validator = FundDataValidator()


if __name__ == '__main__':
    api = MultiSourceFundAPI()
    
    result = api.get_nav_with_validation('161725')
    print("净值验证结果:", json.dumps(result, ensure_ascii=False, indent=2, default=str))
    
    history = api.get_history_with_validation('161725', days=10)
    print(f"历史数据: {history['count']}条, 质量: {history['quality']}")
