"""
基金映射配置 - 升级版

特性：
1. 核心基金 + 备选基金
2. 筛选条件（规模、成立时间、跟踪误差）
3. 持仓相似度匹配
4. LLM推荐优化
"""
from typing import Dict, List, Optional, Tuple
from datetime import date, timedelta
from sqlalchemy.orm import Session


SECTOR_FUND_MAP_V2 = {
    '白酒': {
        'core': {
            'code': '161725',
            'name': '招商中证白酒指数(LOF)A',
            'scale': 500,
            'track_error': 0.15,
            'establish_days': 3000,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '512690', 'name': '酒ETF', 'scale': 100, 'track_error': 0.20, 'establish_days': 2000},
            {'code': '000854', 'name': '白酒基金', 'scale': 50, 'track_error': 0.25, 'establish_days': 1500}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 365,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['贵州茅台', '五粮液', '泸州老窖', '洋河股份']
    },
    
    '新能源': {
        'core': {
            'code': '516790',
            'name': '国泰中证新能源汽车ETF联接C',
            'scale': 200,
            'track_error': 0.18,
            'establish_days': 2000,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '515030', 'name': '新能源ETF', 'scale': 150, 'track_error': 0.20, 'establish_days': 1800},
            {'code': '013013', 'name': '天弘中证光伏产业指数A', 'scale': 80, 'track_error': 0.22, 'establish_days': 1200}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 365,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['宁德时代', '比亚迪', '隆基绿能', '通威股份']
    },
    
    '半导体': {
        'core': {
            'code': '512480',
            'name': '国泰CES半导体芯片行业ETF联接A',
            'scale': 150,
            'track_error': 0.20,
            'establish_days': 1800,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '012631', 'name': '国泰中证半导体材料设备主题ETF联接A', 'scale': 60, 'track_error': 0.25, 'establish_days': 1000},
            {'code': '008281', 'name': '华夏国证半导体芯片ETF联接A', 'scale': 80, 'track_error': 0.22, 'establish_days': 1200}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 365,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['中芯国际', '北方华创', '韦尔股份', '兆易创新']
    },
    
    '医药': {
        'core': {
            'code': '001017',
            'name': '华夏医疗健康混合A',
            'scale': 80,
            'track_error': None,
            'establish_days': 3000,
            'fund_type': 'mixed'
        },
        'alternatives': [
            {'code': '512010', 'name': '医药ETF', 'scale': 120, 'track_error': 0.18, 'establish_days': 4000},
            {'code': '013929', 'name': '永赢中证全指医疗器械ETF联接A', 'scale': 40, 'track_error': 0.20, 'establish_days': 800}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 365,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf', 'mixed']
        },
        'representative_stocks': ['药明康德', '恒瑞医药', '迈瑞医疗', '爱尔眼科']
    },
    
    '人工智能': {
        'core': {
            'code': '015719',
            'name': '华夏中证人工智能主题ETF联接A',
            'scale': 60,
            'track_error': 0.22,
            'establish_days': 800,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '515070', 'name': 'AI龙头ETF', 'scale': 30, 'track_error': 0.25, 'establish_days': 600},
            {'code': '012536', 'name': '国泰中证计算机主题ETF联接A', 'scale': 50, 'track_error': 0.20, 'establish_days': 1000}
        ],
        'selection_criteria': {
            'min_scale': 3,
            'min_establish_days': 180,
            'max_track_error': 0.6,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['科大讯飞', '海康威视', '大华股份', '寒武纪']
    },
    
    '光伏': {
        'core': {
            'code': '013013',
            'name': '天弘中证光伏产业指数A',
            'scale': 80,
            'track_error': 0.18,
            'establish_days': 1200,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '515790', 'name': '光伏ETF', 'scale': 100, 'track_error': 0.15, 'establish_days': 1500},
            {'code': '013179', 'name': '易方达中证储能产业指数A', 'scale': 40, 'track_error': 0.22, 'establish_days': 800}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 365,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['隆基绿能', '通威股份', '阳光电源', '晶科能源']
    },
    
    '军工': {
        'core': {
            'code': '512660',
            'name': '军工ETF',
            'scale': 120,
            'track_error': 0.18,
            'establish_days': 2500,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '010364', 'name': '国泰中证军工ETF联接A', 'scale': 60, 'track_error': 0.20, 'establish_days': 1800},
            {'code': '004224', 'name': '易方达国防军工混合', 'scale': 100, 'track_error': None, 'establish_days': 2000}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 365,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf', 'mixed']
        },
        'representative_stocks': ['中航沈飞', '中航西飞', '航发动力', '中航光电']
    },
    
    '消费': {
        'core': {
            'code': '000083',
            'name': '汇添富消费行业混合',
            'scale': 100,
            'track_error': None,
            'establish_days': 4000,
            'fund_type': 'mixed'
        },
        'alternatives': [
            {'code': '159928', 'name': '消费ETF', 'scale': 80, 'track_error': 0.15, 'establish_days': 5000},
            {'code': '160222', 'name': '国泰国证食品饮料行业指数', 'scale': 60, 'track_error': 0.18, 'establish_days': 3000}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 365,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf', 'mixed']
        },
        'representative_stocks': ['贵州茅台', '五粮液', '伊利股份', '海天味业']
    },
    
    '有色金属': {
        'core': {
            'code': '160221',
            'name': '国泰国证有色金属行业指数',
            'scale': 80,
            'track_error': 0.20,
            'establish_days': 3500,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '512400', 'name': '有色金属ETF', 'scale': 60, 'track_error': 0.18, 'establish_days': 2000},
            {'code': '518880', 'name': '黄金ETF', 'scale': 150, 'track_error': 0.10, 'establish_days': 4000}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 365,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['紫金矿业', '洛阳钼业', '北方稀土', '中国铝业']
    },
    
    '银行': {
        'core': {
            'code': '512800',
            'name': '银行ETF',
            'scale': 100,
            'track_error': 0.12,
            'establish_days': 3000,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '001595', 'name': '招商中证银行指数A', 'scale': 50, 'track_error': 0.15, 'establish_days': 2500},
            {'code': '161720', 'name': '招商沪深300高贝塔指数', 'scale': 30, 'track_error': 0.20, 'establish_days': 2000}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 365,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['招商银行', '宁波银行', '平安银行', '兴业银行']
    },
    
    '券商': {
        'core': {
            'code': '512880',
            'name': '证券ETF',
            'scale': 200,
            'track_error': 0.15,
            'establish_days': 3500,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '501016', 'name': '国泰中证全指证券公司ETF联接A', 'scale': 80, 'track_error': 0.18, 'establish_days': 2000},
            {'code': '161720', 'name': '券商指数基金', 'scale': 40, 'track_error': 0.22, 'establish_days': 1500}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 365,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['中信证券', '东方财富', '海通证券', '华泰证券']
    },
    
    '沪深300': {
        'core': {
            'code': '110020',
            'name': '易方达沪深300ETF联接A',
            'scale': 300,
            'track_error': 0.08,
            'establish_days': 5000,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '510300', 'name': '沪深300ETF', 'scale': 500, 'track_error': 0.05, 'establish_days': 6000},
            {'code': '000961', 'name': '沪深300指数A', 'scale': 100, 'track_error': 0.10, 'establish_days': 4000}
        ],
        'selection_criteria': {
            'min_scale': 10,
            'min_establish_days': 365,
            'max_track_error': 0.3,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['贵州茅台', '宁德时代', '中国平安', '招商银行']
    },
    
    '恒生科技': {
        'core': {
            'code': '513180',
            'name': '恒生科技ETF',
            'scale': 80,
            'track_error': 0.15,
            'establish_days': 1500,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '159742', 'name': '恒生科技指数ETF', 'scale': 40, 'track_error': 0.18, 'establish_days': 1000},
            {'code': '016970', 'name': '广发恒生科技ETF联接A', 'scale': 20, 'track_error': 0.20, 'establish_days': 800}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 180,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['腾讯控股', '阿里巴巴-SW', '美团-W', '小米集团-W', '快手-W']
    },
    
    '港股科技': {
        'core': {
            'code': '513180',
            'name': '恒生科技ETF',
            'scale': 80,
            'track_error': 0.15,
            'establish_days': 1500,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '159742', 'name': '恒生科技指数ETF', 'scale': 40, 'track_error': 0.18, 'establish_days': 1000},
            {'code': '513060', 'name': '港股通科技ETF', 'scale': 30, 'track_error': 0.20, 'establish_days': 800}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 180,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['腾讯控股', '阿里巴巴-SW', '美团-W', '小米集团-W', '京东集团-SW']
    },
    
    '港股互联网': {
        'core': {
            'code': '513180',
            'name': '恒生科技ETF',
            'scale': 80,
            'track_error': 0.15,
            'establish_days': 1500,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '164906', 'name': '中概互联网ETF', 'scale': 60, 'track_error': 0.18, 'establish_days': 2000},
            {'code': '513050', 'name': '中概互联ETF', 'scale': 50, 'track_error': 0.20, 'establish_days': 1500}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 365,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['腾讯控股', '阿里巴巴-SW', '美团-W', '京东集团-SW', '拼多多']
    },
    
    '港股医药': {
        'core': {
            'code': '159718',
            'name': '恒生医药ETF',
            'scale': 30,
            'track_error': 0.18,
            'establish_days': 1000,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '513060', 'name': '港股通医药ETF', 'scale': 20, 'track_error': 0.20, 'establish_days': 800},
            {'code': '159718', 'name': '恒生医疗保健ETF', 'scale': 25, 'track_error': 0.18, 'establish_days': 900}
        ],
        'selection_criteria': {
            'min_scale': 3,
            'min_establish_days': 180,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['药明生物', '石药集团', '中国生物制药', '信达生物', '百济神州']
    },
    
    '港股消费': {
        'core': {
            'code': '159735',
            'name': '恒生消费ETF',
            'scale': 20,
            'track_error': 0.18,
            'establish_days': 800,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '513070', 'name': '港股通消费ETF', 'scale': 15, 'track_error': 0.20, 'establish_days': 600}
        ],
        'selection_criteria': {
            'min_scale': 3,
            'min_establish_days': 180,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['安踏体育', '李宁', '海底捞', '呷哺呷哺', '农夫山泉']
    },
    
    '中概互联': {
        'core': {
            'code': '164906',
            'name': '中概互联网ETF',
            'scale': 60,
            'track_error': 0.18,
            'establish_days': 2000,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '513050', 'name': '中概互联ETF', 'scale': 50, 'track_error': 0.20, 'establish_days': 1500},
            {'code': '159605', 'name': '中概互联网ETF', 'scale': 40, 'track_error': 0.22, 'establish_days': 1200}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 365,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['腾讯控股', '阿里巴巴-SW', '美团-W', '京东集团-SW', '拼多多', '百度-SW']
    },
    
    '互联网': {
        'core': {
            'code': '515000',
            'name': '互联网ETF',
            'scale': 50,
            'track_error': 0.18,
            'establish_days': 2000,
            'fund_type': 'index'
        },
        'alternatives': [
            {'code': '515050', 'name': '中证互联网ETF', 'scale': 30, 'track_error': 0.20, 'establish_days': 1500},
            {'code': '015719', 'name': '华夏中证人工智能主题ETF联接A', 'scale': 40, 'track_error': 0.22, 'establish_days': 800}
        ],
        'selection_criteria': {
            'min_scale': 5,
            'min_establish_days': 365,
            'max_track_error': 0.5,
            'prefer_type': ['index', 'etf']
        },
        'representative_stocks': ['东方财富', '同花顺', '三七互娱', '完美世界', '昆仑万维']
    }
}


SELECTION_CRITERIA_DEFAULT = {
    'min_scale': 5,
    'min_establish_days': 365,
    'max_track_error': 0.5,
    'prefer_type': ['index', 'etf']
}


class EnhancedFundMatcher:
    """
    增强型基金匹配器
    
    功能：
    1. 核心基金优先匹配
    2. 备选基金降级匹配
    3. 用户绑定最高优先级
    4. 持仓相似度匹配
    5. LLM智能推荐
    """
    
    def __init__(self):
        self.sector_map = SECTOR_FUND_MAP_V2
    
    def match_fund(self, sector: str, db: Session = None, 
                   prefer_user_binding: bool = True) -> Dict:
        """
        匹配板块对应的基金
        
        Returns:
            {
                'fund_code': 基金代码,
                'fund_name': 基金名称,
                'source': 匹配来源,
                'is_core': 是否核心基金,
                'match_reason': 匹配原因
            }
        """
        sector = self._normalize_sector_name(sector)
        
        if prefer_user_binding and db:
            user_binding = self._get_user_binding(sector, db)
            if user_binding:
                return {
                    'fund_code': user_binding.fund_code,
                    'fund_name': user_binding.fund_name,
                    'source': 'user_binding',
                    'is_core': False,
                    'match_reason': f'用户自定义绑定: {user_binding.user_note or "无备注"}'
                }
        
        sector_config = self.sector_map.get(sector)
        if sector_config:
            core_fund = sector_config['core']
            criteria = sector_config.get('selection_criteria', SELECTION_CRITERIA_DEFAULT)
            
            if self._validate_fund_quality(core_fund, criteria, db):
                return {
                    'fund_code': core_fund['code'],
                    'fund_name': core_fund['name'],
                    'source': 'core_mapping',
                    'is_core': True,
                    'match_reason': f'核心基金，规模{core_fund.get("scale", "?")}亿'
                }
            
            for alt in sector_config.get('alternatives', []):
                if self._validate_fund_quality(alt, criteria, db):
                    return {
                        'fund_code': alt['code'],
                        'fund_name': alt['name'],
                        'source': 'alternative_mapping',
                        'is_core': False,
                        'match_reason': f'备选基金，规模{alt.get("scale", "?")}亿'
                    }
        
        if db:
            holding_match = self._match_by_holdings(sector, db)
            if holding_match:
                return {
                    'fund_code': holding_match['fund_code'],
                    'fund_name': holding_match['fund_name'],
                    'source': 'holding_similarity',
                    'is_core': False,
                    'match_reason': f'持仓相似度匹配，重合度{holding_match["similarity"]:.1%}'
                }
        
        return self._recommend_with_llm(sector)
    
    def _normalize_sector_name(self, sector: str) -> str:
        """标准化板块名称"""
        sector = sector.strip()
        
        aliases = {
            '白酒板块': '白酒',
            '新能源板块': '新能源',
            '新能源汽车': '新能源',
            '芯片': '半导体',
            '医疗': '医药',
            'AI': '人工智能',
            '光伏板块': '光伏',
            '国防军工': '军工',
            '消费板块': '消费',
            '有色': '有色金属',
            '证券': '券商',
            '大盘': '沪深300',
            '恒科': '恒生科技',
            '恒生科技指数': '恒生科技',
            '港股': '港股科技',
            '港股权重': '港股科技',
            '中概': '中概互联',
            '中概股': '中概互联',
            '互联网板块': '互联网',
            'A股互联网': '互联网',
            '港股科技板块': '港股科技',
            '港股互联网板块': '港股互联网',
            '恒生医药': '港股医药',
            '恒生消费': '港股消费'
        }
        
        return aliases.get(sector, sector)
    
    def _get_user_binding(self, sector: str, db: Session):
        """获取用户自定义绑定"""
        from src.models.database import UserFundBinding
        
        return db.query(UserFundBinding).filter(
            UserFundBinding.sector == sector,
            UserFundBinding.is_primary == True
        ).first()
    
    def _validate_fund_quality(self, fund: Dict, criteria: Dict, 
                               db: Session = None) -> bool:
        """验证基金是否符合筛选条件"""
        if fund.get('scale', 0) < criteria.get('min_scale', 5):
            return False
        
        if fund.get('establish_days', 0) < criteria.get('min_establish_days', 365):
            return False
        
        track_error = fund.get('track_error')
        if track_error is not None:
            if track_error > criteria.get('max_track_error', 0.5):
                return False
        
        if db:
            from src.models.database import FundInfo
            db_fund = db.query(FundInfo).filter(
                FundInfo.fund_code == fund['code']
            ).first()
            
            if db_fund:
                if db_fund.data_quality == 'error':
                    return False
        
        return True
    
    def _match_by_holdings(self, sector: str, db: Session) -> Optional[Dict]:
        """按持仓相似度匹配"""
        sector_config = self.sector_map.get(sector)
        if not sector_config:
            return None
        
        representative_stocks = sector_config.get('representative_stocks', [])
        if not representative_stocks:
            return None
        
        from src.models.database import FundHolding, FundInfo
        
        holdings = db.query(FundHolding).join(FundInfo).filter(
            FundHolding.stock_name.in_(representative_stocks),
            FundInfo.fund_scale >= 5
        ).all()
        
        if not holdings:
            return None
        
        fund_holdings = {}
        for h in holdings:
            if h.fund_code not in fund_holdings:
                fund_holdings[h.fund_code] = {
                    'fund_code': h.fund_code,
                    'fund_name': h.fund_name,
                    'matched_stocks': [],
                    'total_ratio': 0
                }
            fund_holdings[h.fund_code]['matched_stocks'].append(h.stock_name)
            fund_holdings[h.fund_code]['total_ratio'] += h.holding_ratio or 0
        
        if fund_holdings:
            best_match = max(fund_holdings.values(), key=lambda x: x['total_ratio'])
            best_match['similarity'] = len(best_match['matched_stocks']) / len(representative_stocks)
            
            if best_match['similarity'] >= 0.5:
                return best_match
        
        return None
    
    def _recommend_with_llm(self, sector: str) -> Dict:
        """使用LLM推荐基金"""
        try:
            from src.analyzer.llm_analyzer import get_analyzer
            import json
            import re
            
            analyzer = get_analyzer()
            
            prompt = f"""请推荐一个与"{sector}"板块相关的指数基金或ETF。

请返回JSON格式：
{{
    "code": "基金代码（6位数字）",
    "name": "基金名称",
    "scale": 规模（亿元）,
    "reason": "推荐理由（30字以内）"
}}

要求：
1. 必须是真实存在的基金
2. 优先选择ETF或指数基金
3. 选择规模较大（≥5亿）、流动性好的基金
4. 优先选择成立时间≥1年的基金
"""
            
            response = analyzer._call_llm(prompt, task_type='extraction', max_tokens=200)
            
            json_match = re.search(r'\{[\s\S]+\}', response)
            if json_match:
                result = json.loads(json_match.group())
                return {
                    'fund_code': result.get('code'),
                    'fund_name': result.get('name'),
                    'source': 'llm_recommendation',
                    'is_core': False,
                    'match_reason': result.get('reason', 'LLM推荐')
                }
        except Exception as e:
            print(f"[FundMatcher] LLM推荐失败: {e}")
        
        return {
            'fund_code': None,
            'fund_name': None,
            'source': 'failed',
            'is_core': False,
            'match_reason': f'无法匹配板块 {sector}'
        }
    
    def get_all_core_funds(self) -> List[str]:
        """获取所有核心基金代码"""
        return [config['core']['code'] for config in self.sector_map.values()]
    
    def get_sector_by_fund(self, fund_code: str) -> Optional[str]:
        """根据基金代码反查板块"""
        for sector, config in self.sector_map.items():
            if config['core']['code'] == fund_code:
                return sector
            for alt in config.get('alternatives', []):
                if alt['code'] == fund_code:
                    return sector
        return None


enhanced_fund_matcher = EnhancedFundMatcher()


if __name__ == '__main__':
    matcher = EnhancedFundMatcher()
    
    result = matcher.match_fund('白酒')
    print("匹配结果:", result)
    
    print("所有核心基金:", matcher.get_all_core_funds())
