"""
市场数据服务模块
包含：指数数据、北向资金、政策新闻、情绪数据抓取
"""
import os
import re
import json
import time
import logging
import hashlib
import requests
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from enum import Enum

import sys
if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func

from src.models.database import (
    SessionLocal, MarketData, PolicyData, SentimentData, 
    SectorFundFlow, FundInfo
)

logger = logging.getLogger(__name__)


class MarketState(Enum):
    STABLE = "stable"
    VOLATILE = "volatile"
    TRENDING = "trending"
    POLICY_SENSITIVE = "policy"
    EXTREME = "extreme"


class DataSource(Enum):
    EASTMONEY = "eastmoney"
    SINA = "sina"
    TUSHARE = "tushare"
    MANUAL = "manual"


@dataclass
class IndexData:
    code: str
    name: str
    current: float
    change: float
    change_pct: float
    open: float
    high: float
    low: float
    volume: float
    amount: float
    timestamp: datetime


@dataclass
class NorthFlowData:
    date: date
    total_flow: float
    sh_flow: float
    sz_flow: float
    buy_amount: float
    sell_amount: float


@dataclass
class PolicyInfo:
    title: str
    content: str
    policy_type: str
    policy_level: str
    affected_sectors: List[str]
    keywords: List[str]
    source: str
    publish_time: datetime


class MarketDataFetcher:
    """市场数据抓取器"""
    
    INDEX_CODES = {
        "sh000001": "上证指数",
        "sh000300": "沪深300",
        "sz399006": "创业板指",
        "sh000016": "上证50",
        "sz399001": "深证成指"
    }
    
    CACHE_DURATION_MINUTES = 15
    
    def __init__(self, db: Session = None):
        self.db = db or SessionLocal()
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Referer': 'http://quote.eastmoney.com/'
        })
    
    def fetch_index_data(self, index_code: str = "sh000300") -> Optional[IndexData]:
        """抓取指数实时数据"""
        try:
            url = f"http://push2.eastmoney.com/api/qt/stock/get"
            params = {
                "secid": f"1.{index_code[2:]}" if index_code.startswith("sh") else f"0.{index_code[2:]}",
                "fields": "f43,f44,f45,f46,f47,f48,f50,f51,f52,f58,f60,f170,f171"
            }
            
            response = self.session.get(url, params=params, timeout=10)
            data = response.json()
            
            if data and 'data' in data and data['data']:
                d = data['data']
                return IndexData(
                    code=index_code,
                    name=self.INDEX_CODES.get(index_code, index_code),
                    current=d.get('f43', 0) / 100 if d.get('f43') else 0,
                    change=d.get('f50', 0) / 100 if d.get('f50') else 0,
                    change_pct=d.get('f170', 0) / 100 if d.get('f170') else 0,
                    open=d.get('f46', 0) / 100 if d.get('f46') else 0,
                    high=d.get('f44', 0) / 100 if d.get('f44') else 0,
                    low=d.get('f45', 0) / 100 if d.get('f45') else 0,
                    volume=d.get('f47', 0),
                    amount=d.get('f48', 0),
                    timestamp=datetime.now()
                )
        except Exception as e:
            logger.warning(f"[MarketData] 抓取指数数据失败: {e}")
        
        return None
    
    def fetch_all_indices(self) -> List[IndexData]:
        """抓取所有主要指数"""
        results = []
        for code in self.INDEX_CODES.keys():
            data = self.fetch_index_data(code)
            if data:
                results.append(data)
            time.sleep(0.2)
        return results
    
    def fetch_north_flow(self) -> Optional[NorthFlowData]:
        """抓取北向资金数据"""
        try:
            url = "http://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
            params = {
                "secid": "1.000300",
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56",
                "klt": 101,
                "lmt": 1
            }
            
            response = self.session.get(url, params=params, timeout=10)
            data = response.json()
            
            if data and 'data' in data and data['data'] and 'klines' in data['data']:
                klines = data['data']['klines']
                if klines:
                    latest = klines[-1].split(',')
                    return NorthFlowData(
                        date=date.today(),
                        total_flow=float(latest[1]) if len(latest) > 1 else 0,
                        sh_flow=float(latest[2]) if len(latest) > 2 else 0,
                        sz_flow=float(latest[3]) if len(latest) > 3 else 0,
                        buy_amount=float(latest[4]) if len(latest) > 4 else 0,
                        sell_amount=float(latest[5]) if len(latest) > 5 else 0
                    )
        except Exception as e:
            logger.warning(f"[MarketData] 抓取北向资金失败: {e}")
        
        return None
    
    def save_index_data(self, index_data: IndexData) -> bool:
        """保存指数数据"""
        try:
            record = MarketData(
                data_type='index',
                data_date=index_data.timestamp.date(),
                data_time=index_data.timestamp,
                index_code=index_data.code,
                index_name=index_data.name,
                current_value=index_data.current,
                change_value=index_data.change,
                change_pct=index_data.change_pct,
                open_value=index_data.open,
                high_value=index_data.high,
                low_value=index_data.low,
                volume=index_data.volume,
                amount=index_data.amount,
                data_source=DataSource.EASTMONEY.value
            )
            self.db.add(record)
            self.db.commit()
            return True
        except Exception as e:
            self.db.rollback()
            logger.error(f"[MarketData] 保存指数数据失败: {e}")
            return False
    
    def save_north_flow(self, flow_data: NorthFlowData) -> bool:
        """保存北向资金数据"""
        try:
            record = MarketData(
                data_type='north_flow',
                data_date=flow_data.date,
                data_time=datetime.now(),
                north_flow=flow_data.total_flow,
                north_buy=flow_data.buy_amount,
                north_sell=flow_data.sell_amount,
                raw_data={
                    'sh_flow': flow_data.sh_flow,
                    'sz_flow': flow_data.sz_flow
                },
                data_source=DataSource.EASTMONEY.value
            )
            self.db.add(record)
            self.db.commit()
            return True
        except Exception as e:
            self.db.rollback()
            logger.error(f"[MarketData] 保存北向资金失败: {e}")
            return False
    
    def get_latest_index_data(self, index_code: str = "sh000300") -> Optional[MarketData]:
        """获取最新的指数数据（优先缓存）"""
        cutoff = datetime.now() - timedelta(minutes=self.CACHE_DURATION_MINUTES)
        
        record = self.db.query(MarketData).filter(
            and_(
                MarketData.data_type == 'index',
                MarketData.index_code == index_code,
                MarketData.data_time >= cutoff
            )
        ).order_by(MarketData.data_time.desc()).first()
        
        return record
    
    def get_latest_north_flow(self) -> Optional[MarketData]:
        """获取最新的北向资金数据"""
        cutoff = datetime.now() - timedelta(minutes=self.CACHE_DURATION_MINUTES)
        
        record = self.db.query(MarketData).filter(
            and_(
                MarketData.data_type == 'north_flow',
                MarketData.data_time >= cutoff
            )
        ).order_by(MarketData.data_time.desc()).first()
        
        return record


class PolicyDataFetcher:
    """政策数据抓取器"""
    
    POLICY_KEYWORDS = [
        "降息", "降准", "加息", "货币政策", "财政政策",
        "监管", "证监会", "央行", "银保监会", "发改委",
        "两会", "经济会议", "政治局会议", "国务院",
        "行业政策", "税收", "补贴", "扶持"
    ]
    
    def __init__(self, db: Session = None):
        self.db = db or SessionLocal()
        self.session = requests.Session()
    
    def fetch_financial_news(self, limit: int = 20) -> List[PolicyInfo]:
        """抓取财经新闻"""
        results = []
        
        try:
            url = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html"
            response = self.session.get(url, timeout=10)
            data = response.json()
            
            if data and 'batch' in data:
                for item in data['batch'][:limit]:
                    title = item.get('title', '')
                    content = item.get('digest', title)
                    
                    keywords = self._extract_keywords(title + content)
                    affected_sectors = self._extract_sectors(title + content)
                    
                    policy_level = self._determine_policy_level(title, content)
                    policy_type = self._determine_policy_type(title, content)
                    
                    results.append(PolicyInfo(
                        title=title,
                        content=content,
                        policy_type=policy_type,
                        policy_level=policy_level,
                        affected_sectors=affected_sectors,
                        keywords=keywords,
                        source="东方财富",
                        publish_time=datetime.now()
                    ))
        except Exception as e:
            logger.warning(f"[PolicyData] 抓取财经新闻失败: {e}")
        
        return results
    
    def _extract_keywords(self, text: str) -> List[str]:
        """提取关键词"""
        found = []
        for kw in self.POLICY_KEYWORDS:
            if kw in text:
                found.append(kw)
        return found[:5]
    
    def _extract_sectors(self, text: str) -> List[str]:
        """提取相关板块"""
        sector_keywords = {
            "白酒": ["白酒", "酒类", "茅台", "五粮液"],
            "新能源": ["新能源", "光伏", "风电", "锂电", "宁德时代"],
            "医药": ["医药", "医疗", "药", "疫苗"],
            "科技": ["科技", "芯片", "半导体", "人工智能", "AI"],
            "金融": ["银行", "券商", "保险", "金融"],
            "地产": ["地产", "房地产", "楼市"]
        }
        
        found = []
        for sector, keywords in sector_keywords.items():
            for kw in keywords:
                if kw in text:
                    found.append(sector)
                    break
        return found
    
    def _determine_policy_level(self, title: str, content: str) -> str:
        """判断政策级别"""
        text = title + content
        
        if any(kw in text for kw in ["国务院", "两会", "政治局", "中央"]):
            return "critical"
        elif any(kw in text for kw in ["证监会", "央行", "银保监会", "发改委"]):
            return "major"
        else:
            return "minor"
    
    def _determine_policy_type(self, title: str, content: str) -> str:
        """判断政策类型"""
        text = title + content
        
        if any(kw in text for kw in ["降息", "降准", "货币政策"]):
            return "monetary"
        elif any(kw in text for kw in ["税收", "补贴", "财政"]):
            return "fiscal"
        elif any(kw in text for kw in ["监管", "规范"]):
            return "regulation"
        else:
            return "other"
    
    def save_policy_data(self, policy: PolicyInfo) -> bool:
        """保存政策数据"""
        try:
            record = PolicyData(
                policy_date=policy.publish_time.date(),
                policy_time=policy.publish_time,
                title=policy.title,
                content=policy.content[:2000],
                policy_type=policy.policy_type,
                policy_level=policy.policy_level,
                affected_sectors=policy.affected_sectors,
                keywords=policy.keywords,
                source=policy.source
            )
            self.db.add(record)
            self.db.commit()
            return True
        except Exception as e:
            self.db.rollback()
            logger.error(f"[PolicyData] 保存政策数据失败: {e}")
            return False


class MarketStateDetector:
    """市场状态检测器"""
    
    def __init__(self, db: Session = None):
        self.db = db or SessionLocal()
    
    def detect(self) -> MarketState:
        """检测当前市场状态"""
        index_data = self._get_latest_index()
        
        if not index_data:
            return MarketState.STABLE
        
        change_pct = index_data.change_pct or 0
        
        if abs(change_pct) >= 3:
            return MarketState.EXTREME
        elif abs(change_pct) >= 2:
            return MarketState.TRENDING
        elif abs(change_pct) >= 1:
            return MarketState.VOLATILE
        
        if self._has_major_policy():
            return MarketState.POLICY_SENSITIVE
        
        return MarketState.STABLE
    
    def _get_latest_index(self) -> Optional[MarketData]:
        """获取最新指数数据"""
        return self.db.query(MarketData).filter(
            MarketData.data_type == 'index',
            MarketData.index_code == 'sh000300'
        ).order_by(MarketData.data_time.desc()).first()
    
    def _has_major_policy(self) -> bool:
        """检查是否有重大政策"""
        today = date.today()
        count = self.db.query(PolicyData).filter(
            and_(
                PolicyData.policy_date >= today - timedelta(days=1),
                PolicyData.policy_level.in_(['critical', 'major'])
            )
        ).count()
        return count > 0


class WeightAdjuster:
    """权重动态调整器"""
    
    WEIGHT_MATRIX = {
        MarketState.STABLE: {
            "blogger": 0.50,
            "fund_data": 0.20,
            "macro_policy": 0.20,
            "sentiment": 0.10,
            "technical": 0.00
        },
        MarketState.VOLATILE: {
            "blogger": 0.40,
            "fund_data": 0.20,
            "macro_policy": 0.30,
            "sentiment": 0.10,
            "technical": 0.00
        },
        MarketState.TRENDING: {
            "blogger": 0.20,
            "fund_data": 0.50,
            "macro_policy": 0.20,
            "sentiment": 0.10,
            "technical": 0.00
        },
        MarketState.POLICY_SENSITIVE: {
            "blogger": 0.20,
            "fund_data": 0.20,
            "macro_policy": 0.40,
            "sentiment": 0.20,
            "technical": 0.00
        },
        MarketState.EXTREME: {
            "blogger": 0.10,
            "fund_data": 0.30,
            "macro_policy": 0.30,
            "sentiment": 0.10,
            "technical": 0.20
        }
    }
    
    def __init__(self, db: Session = None):
        self.db = db or SessionLocal()
        self.state_detector = MarketStateDetector(db)
    
    def get_weights(self) -> Dict[str, float]:
        """获取当前权重配置"""
        state = self.state_detector.detect()
        return self.WEIGHT_MATRIX.get(state, self.WEIGHT_MATRIX[MarketState.STABLE])
    
    def get_market_state(self) -> str:
        """获取市场状态"""
        return self.state_detector.detect().value


class DataQualityValidator:
    """数据质量校验器"""
    
    RULES = {
        "blogger": {
            "min_accuracy_rate": 50,
            "min_predictions": 5,
            "min_confidence": 30
        },
        "prediction": {
            "min_confidence": 40,
            "require_clear_direction": True,
            "max_age_days": 30
        },
        "viewpoint": {
            "min_content_length": 50,
            "min_credibility": 40
        },
        "market_data": {
            "max_age_minutes": 15
        }
    }
    
    def validate_blogger(self, blogger_data: Dict) -> Tuple[bool, str]:
        """校验博主数据"""
        rules = self.RULES["blogger"]
        
        if blogger_data.get("accuracy_rate", 0) < rules["min_accuracy_rate"]:
            return False, f"准确率低于{rules['min_accuracy_rate']}%"
        
        if blogger_data.get("total_predictions", 0) < rules["min_predictions"]:
            return False, f"预测数少于{rules['min_predictions']}次"
        
        return True, ""
    
    def validate_prediction(self, prediction_data: Dict) -> Tuple[bool, str]:
        """校验预测数据"""
        rules = self.RULES["prediction"]
        
        if prediction_data.get("confidence", 0) < rules["min_confidence"]:
            return False, f"置信度低于{rules['min_confidence']}"
        
        if rules["require_clear_direction"]:
            pred_type = prediction_data.get("prediction_type", "")
            if pred_type not in ["up", "down"]:
                return False, "预测方向不明确"
        
        return True, ""
    
    def validate_viewpoint(self, viewpoint_data: Dict) -> Tuple[bool, str]:
        """校验观点数据"""
        rules = self.RULES["viewpoint"]
        
        content = viewpoint_data.get("content", "")
        if len(content) < rules["min_content_length"]:
            return False, f"内容长度少于{rules['min_content_length']}字"
        
        if viewpoint_data.get("credibility_score", 0) < rules["min_credibility"]:
            return False, f"可信度低于{rules['min_credibility']}"
        
        return True, ""
    
    def filter_valid_bloggers(self, bloggers: List[Dict]) -> List[Dict]:
        """过滤有效博主"""
        return [b for b in bloggers if self.validate_blogger(b)[0]]
    
    def filter_valid_predictions(self, predictions: List[Dict]) -> List[Dict]:
        """过滤有效预测"""
        return [p for p in predictions if self.validate_prediction(p)[0]]
    
    def filter_valid_viewpoints(self, viewpoints: List[Dict]) -> List[Dict]:
        """过滤有效观点"""
        return [v for v in viewpoints if self.validate_viewpoint(v)[0]]


from typing import Tuple


def get_market_fetcher(db: Session = None) -> MarketDataFetcher:
    return MarketDataFetcher(db)


def get_policy_fetcher(db: Session = None) -> PolicyDataFetcher:
    return PolicyDataFetcher(db)


def get_weight_adjuster(db: Session = None) -> WeightAdjuster:
    return WeightAdjuster(db)


def get_quality_validator() -> DataQualityValidator:
    return DataQualityValidator()
