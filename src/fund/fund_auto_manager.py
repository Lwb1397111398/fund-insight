"""
智能基金自动管理模块
功能：
1. 根据板块自动抓取对应基金
2. 智能分类到合适板块
3. 自动创建新板块
4. 基金去重和更新
"""
import json
import re
import logging
from typing import Dict, List, Optional, Tuple
from datetime import date, datetime
from sqlalchemy.orm import Session

from src.models.database import FundInfo, SessionLocal
from src.fund.fund_api import fund_api
from src.constants import (
    SECTOR_CATEGORIES,
    get_fund_for_sector,
    get_category_for_sector as get_category_for_sector_constant
)

logger = logging.getLogger(__name__)


class FundAutoManager:
    """智能基金自动管理器"""
    
    SECTOR_CATEGORIES = SECTOR_CATEGORIES
    
    SECTOR_TO_CATEGORY = {}
    
    def __init__(self):
        self._build_category_map()
    
    def _build_category_map(self):
        """构建板块到分类的映射表"""
        for category, sectors in self.SECTOR_CATEGORIES.items():
            for sector in sectors:
                self.SECTOR_TO_CATEGORY[sector] = category
    
    def get_category_for_sector(self, sector: str) -> str:
        """获取板块所属的标准分类"""
        return get_category_for_sector_constant(sector)
    
    def _parse_nav_date(self, date_str: str) -> date:
        """解析净值日期字符串"""
        if not date_str:
            return date.today()
        try:
            return datetime.strptime(date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return date.today()
    
    _sector_category_cache = {}
    
    def _classify_sector_with_llm(self, sector: str) -> str:
        """使用LLM智能分类新板块（带缓存）"""
        if sector in self._sector_category_cache:
            return self._sector_category_cache[sector]
        
        categories_str = ', '.join(self.SECTOR_CATEGORIES.keys())
        
        prompt = f"""请将以下板块分类到合适的类别中：

【板块名称】
{sector}

【可选类别】
{categories_str}

请只返回类别名称，不要解释。
如果无法匹配现有类别，请返回"其他"。
"""
        
        try:
            from src.analyzer.llm_analyzer import get_analyzer
            analyzer = get_analyzer()
            
            response = analyzer._call_llm(prompt, task_type='extraction', max_tokens=50)
            category = response.strip()
            
            if category in self.SECTOR_CATEGORIES:
                self._sector_category_cache[sector] = category
                return category
            
            for cat in self.SECTOR_CATEGORIES.keys():
                if cat in category or category in cat:
                    self._sector_category_cache[sector] = cat
                    return cat
                    
        except Exception as e:
            logger.warning(f"LLM分类失败: {e}")
        
        self._sector_category_cache[sector] = "其他"
        return "其他"
    
    def auto_fetch_fund_for_sector(self, sector: str) -> Optional[Dict]:
        """
        自动抓取板块对应的基金
        1. 从映射表中查找
        2. 如果找不到，使用LLM推荐
        3. 验证基金代码有效性
        """
        fund_info = get_fund_for_sector(sector)
        if fund_info:
            if self._verify_fund_exists(fund_info['code']):
                return fund_info
        
        recommended_fund = self._recommend_fund_with_llm(sector)
        if recommended_fund and self._verify_fund_exists(recommended_fund['code']):
            return recommended_fund
        
        return None
    
    def _verify_fund_exists(self, fund_code: str) -> bool:
        """验证基金代码是否有效，并排除不适合的基金类型"""
        try:
            fund_info = fund_api.get_fund_info(fund_code)
            if not fund_info or not fund_info.get('fund_name'):
                return False
            
            # 排除不适合验证股票预测的基金类型
            fund_name = fund_info.get('fund_name', '')
            excluded_keywords = ['债券', '债', '货币', '理财', '短债', '纯债', '利率债', '信用债']
            
            if any(kw in fund_name for kw in excluded_keywords):
                logger.debug(f"排除债券/货币型基金: {fund_name}")
                return False
            
            return True
        except Exception as e:
            logger.warning(f"验证基金代码 {fund_code} 失败: {e}")
            return False
    
    def _recommend_fund_with_llm(self, sector: str) -> Optional[Dict]:
        """使用LLM推荐板块对应的基金（增强版Prompt）"""
        
        # 构建板块说明和示例
        sector_hints = {
            '绿色电力': '指清洁能源发电，包括风电、光伏、水电、核电等电力公用事业板块',
            '绿电': '指清洁能源发电，包括风电、光伏、水电、核电等电力公用事业板块',
            '电力': '指电力公用事业板块，包括火电、水电、风电、光伏、核电等',
            '日股': '指日本股票市场，推荐日经225指数基金',
            '日经': '指日本股票市场，推荐日经225指数基金',
            '日本': '指日本股票市场，推荐日经225指数基金',
            '红利低波': '指高股息且波动率低的股票组合，通常是红利+低波动双因子策略',
            '红利': '指高股息股票组合',
            '高股息': '指高股息股票组合',
            '低波': '指低波动率股票组合',
            '美股': '指美国股票市场，推荐纳斯达克100或标普500指数基金',
            '纳斯达克': '指美国纳斯达克市场，推荐纳斯达克100指数基金',
            '标普': '指美国标普500指数',
            '游戏': '指游戏行业，包括游戏开发、发行等',
            '传媒': '指传媒行业，包括影视、广告、出版等',
            '稀土': '指稀土永磁材料行业',
            '储能': '指储能电池、储能系统行业',
            '锂电池': '指锂离子电池产业链',
            '风电': '指风力发电设备、风电场运营',
            '钢铁': '指钢铁行业',
            '化工': '指化工行业',
            '石油': '指石油石化行业',
            '油气': '指石油天然气行业',
            '环保': '指环保行业，包括污水处理、固废处理等',
            '央企': '指中央企业相关股票',
            '国企': '指国有企业相关股票',
            '豆粕': '指豆粕期货ETF',
            '能源化工': '指能源化工期货ETF',
        }
        
        sector_hint = sector_hints.get(sector, f'指{sector}相关板块')
        
        prompt = f"""请推荐一个与"{sector}"板块相关的指数基金或ETF。

【板块说明】
{sector_hint}

【常见基金类型参考】
- ETF基金：交易型开放式指数基金，代码通常是159xxx或51xxxx
- LOF基金：上市型开放式基金，代码通常是16xxxx
- 场外指数基金：代码通常是00xxxx或01xxxx

【推荐原则】
1. 优先选择ETF基金（流动性好）
2. 基金规模建议≥5亿元
3. 成立时间建议≥1年
4. 必须是真实存在的基金代码

请返回JSON格式：
{{
    "code": "基金代码（6位数字）",
    "name": "基金名称",
    "reason": "推荐理由（20字以内）"
}}

只返回JSON，不要其他内容。"""
        
        try:
            from src.analyzer.llm_analyzer import get_analyzer
            analyzer = get_analyzer()
            
            response = analyzer._call_llm(prompt, task_type='extraction', max_tokens=200)
            
            # 解析JSON
            json_match = re.search(r'\{[\s\S]+\}', response)
            if json_match:
                result = json.loads(json_match.group())
                fund_code = result.get('code')
                fund_name = result.get('name')
                
                # 验证基金代码格式
                if fund_code and len(str(fund_code)) == 6 and str(fund_code).isdigit():
                    return {
                        'code': str(fund_code),
                        'name': fund_name or sector
                    }
                else:
                    logger.warning(f"LLM返回的基金代码格式无效: {fund_code}")
                    
        except Exception as e:
            logger.error(f"LLM推荐基金失败: {e}")
        
        return None
    
    def auto_add_fund_for_prediction(self, sector: str, db: Session = None) -> Tuple[bool, str, Optional[FundInfo]]:
        """
        为预测自动添加基金
        
        Returns:
            (success, message, fund)
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        
        try:
            # 1. 获取板块对应基金
            fund_info = self.auto_fetch_fund_for_sector(sector)
            if not fund_info:
                return False, f"无法找到板块 '{sector}' 对应的基金", None
            
            fund_code = fund_info['code']
            fund_name = fund_info['name']
            
            # 2. 检查基金是否已存在
            existing = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            if existing:
                return True, f"基金 {fund_name} 已存在", existing
            
            # 3. 确定板块分类
            category = self.get_category_for_sector(sector)
            
            # 4. 获取基金详细信息
            api_info = fund_api.get_fund_info(fund_code)
            if not api_info:
                return False, f"无法获取基金 {fund_code} 的详细信息", None
            
            # 5. 创建基金记录
            from datetime import datetime
            new_fund = FundInfo(
                fund_code=fund_code,
                fund_name=api_info.get('fund_name', fund_name),
                fund_type=api_info.get('fund_type', '未知类型'),
                sector_type=sector,
                latest_nav=api_info.get('nav'),
                nav_date=self._parse_nav_date(api_info.get('nav_date')),
                day_growth=api_info.get('day_growth'),
                can_delete=True
            )
            
            db.add(new_fund)
            db.commit()
            db.refresh(new_fund)
            
            try:
                from src.fund.fund_api import fund_data_manager
                fund_data_manager.update_fund_history(fund_code, days=30, db=db)
            except ImportError:
                pass
            
            return True, f"成功添加基金 {new_fund.fund_name} 到板块 '{sector}'（分类：{category}）", new_fund
            
        except Exception as e:
            db.rollback()
            return False, f"添加基金失败: {str(e)}", None
        finally:
            if close_db:
                db.close()
    
    def get_or_create_sector(self, sector: str, db: Session = None) -> Tuple[str, bool]:
        """
        获取或创建板块
        
        Returns:
            (sector_name, is_new)
        """
        from src.analyzer.llm_analyzer import LLMAnalyzer
        
        # 标准化板块名称
        sector = sector.strip()
        
        # 检查是否是已知板块
        for key in LLMAnalyzer.SECTOR_FUND_MAP.keys():
            if key in sector or sector in key:
                return key, False
        
        # 新板块，返回原始名称
        return sector, True


# 全局实例
fund_auto_manager = FundAutoManager()


def get_auto_manager() -> FundAutoManager:
    """获取基金自动管理器实例"""
    return fund_auto_manager
