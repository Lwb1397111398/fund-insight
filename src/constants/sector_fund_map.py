"""
板块基金映射模块
统一的板块-基金映射表，所有模块引用此模块
"""
from typing import Dict, Optional


# 板块别名映射（黑话/简称 → 标准板块名称）
SECTOR_ALIASES = {
    # 白酒
    '酒': '白酒',
    '白酒哥': '白酒',
    '酒鬼': '白酒',
    '茅': '白酒',

    # 医药
    '药': '创新药',
    '毒药': '创新药',
    '垃圾药': '创新药',
    '药罐子': '创新药',
    '生物医药': '医药',

    # 半导体
    '芯': '半导体',
    '沙子': '半导体',
    '泥巴': '半导体',
    '芯片': '半导体',

    # 光伏
    '光': '光伏',
    '光伏狗': '光伏',
    '太阳能': '光伏',

    # 新能源
    '锂': '锂电池',
    '锂王': '锂电池',
    '电池': '锂电池',
    '车': '新能源车',
    '新能源车': '新能源',
    '电车': '新能源车',

    # 军工
    '军': '军工',
    '军工狗': '军工',
    '飞机大炮': '军工',
    '国防': '军工',

    # 银行
    '银': '银行',
    '银行狗': '银行',
    '四大行': '银行',

    # 券商
    '券': '券商',
    '券商狗': '券商',
    '牛市旗手': '券商',
    '证券': '券商',

    # 房地产
    '房': '房地产',
    '地产狗': '房地产',
    '房子': '房地产',
    '地产': '房地产',

    # 煤炭
    '煤': '煤炭',
    '黑金': '煤炭',
    '煤炭狗': '煤炭',

    # 石油
    '油': '石油',
    '黑油': '石油',
    '石油狗': '石油',
    '油气': '石油',

    # 黄金
    '金': '黄金',
    '黄金大妈': '黄金',

    # 港股
    '港': '恒生科技',
    '港仔': '恒生科技',
    '恒仔': '恒生科技',
    '港股': '恒生科技',

    # 人工智能
    'AI': '人工智能',
    '人工智能': '人工智能',
    '机器人': '机器人',
    '算力': '科技',
    '数据中心': '科技',
    '云计算': '云计算',
}


SECTOR_FUND_MAP = {
    # 消费类
    '白酒': {'code': '161725', 'name': '招商中证白酒指数 (LOF)A'},
    '食品饮料': {'code': '160222', 'name': '国泰国证食品饮料行业指数'},
    '消费': {'code': '000083', 'name': '汇添富消费行业混合'},
    '家电': {'code': '159996', 'name': '家电ETF'},
    '消费电子': {'code': '159732', 'name': '消费电子ETF'},
    
    # 医药医疗
    '医药': {'code': '001017', 'name': '华夏医疗健康混合 A'},
    '医疗': {'code': '001017', 'name': '华夏医疗健康混合 A'},
    '医疗器械': {'code': '159898', 'name': '医疗器械ETF'},
    '创新药': {'code': '159858', 'name': '创新药ETF'},
    '中药': {'code': '159883', 'name': '中药ETF'},
    '生物科技': {'code': '501009', 'name': '生物科技LOF'},
    
    # 科技类
    '半导体': {'code': '512480', 'name': '国泰 CES 半导体芯片行业 ETF 联接 A'},
    '芯片': {'code': '512480', 'name': '国泰 CES 半导体芯片行业 ETF 联接 A'},
    '人工智能': {'code': '015719', 'name': '华夏中证人工智能主题ETF联接A'},
    'AI': {'code': '015719', 'name': '华夏中证人工智能主题ETF联接A'},
    '科技': {'code': '515000', 'name': '科技ETF'},
    '计算机': {'code': '512720', 'name': '计算机ETF'},
    '软件': {'code': '515230', 'name': '软件ETF'},
    '通信': {'code': '515880', 'name': '通信ETF'},
    '5G': {'code': '515050', 'name': '5GETF'},
    '云计算': {'code': '516510', 'name': '云计算ETF'},
    '大数据': {'code': '515700', 'name': '大数据ETF'},
    '机器人': {'code': '562500', 'name': '机器人ETF'},
    
    # 新能源
    '新能源': {'code': '516790', 'name': '国泰中证新能源汽车 ETF 联接 C'},
    '新能源车': {'code': '516790', 'name': '国泰中证新能源汽车 ETF 联接 C'},
    '光伏': {'code': '013013', 'name': '天弘中证光伏产业指数 A'},
    '储能': {'code': '159866', 'name': '储能ETF'},
    '锂电池': {'code': '159840', 'name': '锂电池ETF'},
    '风电': {'code': '516670', 'name': '风电ETF'},
    '氢能源': {'code': '159884', 'name': '氢能源ETF'},
    
    # 军工
    '军工': {'code': '005633', 'name': '华夏军工安全混合 A'},
    '国防': {'code': '512670', 'name': '国防ETF'},
    '船舶': {'code': '512660', 'name': '军工ETF'},
    
    # 金融地产
    '银行': {'code': '001594', 'name': '易方达中证银行指数 (LOF)A'},
    '券商': {'code': '501016', 'name': '国泰上证 180 金融 ETF 联接'},
    '证券': {'code': '512880', 'name': '证券ETF'},
    '保险': {'code': '167301', 'name': '保险主题LOF'},
    '房地产': {'code': '160218', 'name': '国泰国证房地产行业指数'},
    '地产': {'code': '160218', 'name': '国泰国证房地产行业指数'},
    '基建': {'code': '159619', 'name': '基建ETF'},
    
    # 周期资源
    '有色': {'code': '160221', 'name': '国泰国证有色金属行业指数'},
    '有色金属': {'code': '160221', 'name': '国泰国证有色金属行业指数'},
    '煤炭': {'code': '161724', 'name': '招商中证煤炭等权指数 (LOF)'},
    '钢铁': {'code': '515210', 'name': '钢铁ETF'},
    '化工': {'code': '159870', 'name': '化工ETF'},
    '石油': {'code': '501017', 'name': '石油LOF'},
    '油气': {'code': '501017', 'name': '石油LOF'},
    '黄金': {'code': '000218', 'name': '易方达黄金 ETF 联接 A'},
    '白银': {'code': '161226', 'name': '白银基金LOF'},
    '稀土': {'code': '516780', 'name': '稀土ETF'},
    '小金属': {'code': '516780', 'name': '稀土ETF'},
    '铜': {'code': '512680', 'name': '有色金属ETF'},
    
    # 公用事业
    '电力': {'code': '561170', 'name': '广发中证全指电力公用事业ETF'},
    '绿色电力': {'code': '561170', 'name': '电力ETF'},
    '绿电': {'code': '561170', 'name': '电力ETF'},
    '水电': {'code': '561170', 'name': '电力ETF'},
    '核电': {'code': '561170', 'name': '电力ETF'},
    '环保': {'code': '512580', 'name': '环保ETF'},
    '水务': {'code': '159881', 'name': '水务ETF'},
    '燃气': {'code': '159805', 'name': '燃气ETF'},
    
    # 交通物流
    '物流': {'code': '516910', 'name': '物流ETF'},
    '快递': {'code': '516910', 'name': '物流ETF'},
    '航空': {'code': '512380', 'name': '航空ETF'},
    '机场': {'code': '512380', 'name': '航空ETF'},
    '港口': {'code': '516750', 'name': '港口ETF'},
    
    # 港股/海外
    '恒生科技': {'code': '513180', 'name': '恒生科技ETF'},
    '恒科': {'code': '513180', 'name': '恒生科技ETF'},
    '港股科技': {'code': '513180', 'name': '恒生科技ETF'},
    '港股': {'code': '513180', 'name': '恒生科技ETF'},
    '港股互联网': {'code': '513180', 'name': '恒生科技ETF'},
    '港股医药': {'code': '159718', 'name': '恒生医药ETF'},
    '港股消费': {'code': '159735', 'name': '恒生消费ETF'},
    '港股通': {'code': '513990', 'name': '港股通ETF'},
    '中概互联': {'code': '164906', 'name': '中概互联网ETF'},
    '中概': {'code': '164906', 'name': '中概互联网ETF'},
    '日股': {'code': '513520', 'name': '华夏野村日经225ETF'},
    '日经': {'code': '513520', 'name': '日经225ETF'},
    '日本': {'code': '513520', 'name': '日经225ETF'},
    '美股': {'code': '513100', 'name': '纳斯达克ETF'},
    '纳斯达克': {'code': '513100', 'name': '纳斯达克ETF'},
    '标普': {'code': '513500', 'name': '标普500ETF'},
    '全球': {'code': '513100', 'name': '纳斯达克ETF'},
    
    # 互联网/传媒
    '互联网': {'code': '515000', 'name': '互联网ETF'},
    'A股互联网': {'code': '515000', 'name': '互联网ETF'},
    '游戏': {'code': '159869', 'name': '游戏ETF'},
    '传媒': {'code': '512980', 'name': '传媒ETF'},
    '影视': {'code': '512980', 'name': '传媒ETF'},
    '教育': {'code': '516360', 'name': '教育ETF'},
    
    # 宽基指数
    '沪深300': {'code': '110020', 'name': '易方达沪深 300ETF 联接 A'},
    '中证500': {'code': '160119', 'name': '南方中证 500ETF 联接 A'},
    '创业板': {'code': '110026', 'name': '易方达创业板 ETF 联接 A'},
    '科创板': {'code': '011608', 'name': '易方达科创板 50ETF 联接 A'},
    '科创50': {'code': '011608', 'name': '易方达科创板 50ETF 联接 A'},
    '上证50': {'code': '510100', 'name': '上证50ETF'},
    '中证1000': {'code': '159845', 'name': '中证1000ETF'},
    '双创': {'code': '159781', 'name': '双创ETF'},
    
    # 策略风格
    '红利': {'code': '510880', 'name': '红利ETF'},
    '红利低波': {'code': '512890', 'name': '红利低波ETF'},
    '高股息': {'code': '510880', 'name': '红利ETF'},
    '低波': {'code': '512890', 'name': '红利低波ETF'},
    '央企': {'code': '512950', 'name': '央企ETF'},
    '国企': {'code': '512810', 'name': '国企ETF'},
    '自由现金流': {'code': '159201', 'name': '自由现金流ETF'},
    '大盘': {'code': '510050', 'name': '上证50ETF'},
    
    # 商品/其他
    '豆粕': {'code': '159985', 'name': '豆粕ETF'},
    '能源化工': {'code': '159981', 'name': '能源化工ETF'},
    '有色金属商品': {'code': '159980', 'name': '有色ETF'},
}


SECTOR_CATEGORIES = {
    '消费': ['白酒', '食品饮料', '家电', '农业', '养殖', '消费电子'],
    '医药': ['医药', '医疗', '医疗器械', '创新药', '中药', '生物科技'],
    '科技': ['科技', '半导体', '芯片', '存储', '人工智能', 'AI', '机器人', 
            '软件', '计算机', '云计算', '大数据', '5G', '通信', '卫星', '北斗'],
    '新能源': ['新能源', '新能源车', '光伏', '锂电池', '储能', '风电', '核电', '氢能源'],
    '周期': ['有色', '有色金属', '铝', '铜', '稀土', '煤炭', '钢铁', '化工', '石油', '油气'],
    '金融': ['银行', '券商', '保险', '房地产', '地产', '基建', '建筑'],
    '制造': ['军工', '国防', '船舶', '机械', '汽车', '智能汽车', '高铁'],
    '公用': ['电力', '水务', '燃气', '环保'],
    '交通': ['物流', '快递', '航空', '机场', '港口'],
    '传媒': ['互联网', '港股', '传媒', '游戏', '动漫', '影视', '教育'],
    '资源': ['黄金', '白银'],
    '宽基': ['沪深300', '中证500', '创业板', '科创板', '上证50', '中证1000', '双创'],
    '策略': ['央企', '国企', '红利', '高股息'],
    '固收': ['债券', '国债', '货币基金'],
    '混合': ['混合型', '偏股混合', '偏债混合', '灵活配置'],
    '国际': ['美股', '纳斯达克', '标普500', '港股通', '全球'],
}


_SECTOR_TO_CATEGORY = None


def _build_sector_to_category_map() -> Dict[str, str]:
    """构建板块到分类的映射表"""
    global _SECTOR_TO_CATEGORY
    if _SECTOR_TO_CATEGORY is None:
        _SECTOR_TO_CATEGORY = {}
        for category, sectors in SECTOR_CATEGORIES.items():
            for sector in sectors:
                _SECTOR_TO_CATEGORY[sector] = category
    return _SECTOR_TO_CATEGORY


def get_fund_for_sector(sector: str) -> Optional[Dict]:
    """
    获取板块对应的基金信息

    Args:
        sector: 板块名称（支持黑话/别名）

    Returns:
        {'code': 基金代码, 'name': 基金名称} 或 None
    """
    sector = sector.strip()

    # 1. 直接匹配
    if sector in SECTOR_FUND_MAP:
        return SECTOR_FUND_MAP[sector]

    # 2. 别名匹配（黑话 → 标准板块名称）
    if sector in SECTOR_ALIASES:
        standard_sector = SECTOR_ALIASES[sector]
        if standard_sector in SECTOR_FUND_MAP:
            return SECTOR_FUND_MAP[standard_sector]

    # 3. 模糊匹配（包含关系）
    for key, fund_info in SECTOR_FUND_MAP.items():
        if key in sector or sector in key:
            return fund_info

    # 4. 别名模糊匹配
    for alias, standard_sector in SECTOR_ALIASES.items():
        if alias in sector or sector in alias:
            if standard_sector in SECTOR_FUND_MAP:
                return SECTOR_FUND_MAP[standard_sector]

    return None


def get_category_for_sector(sector: str) -> str:
    """
    获取板块所属的标准分类

    Args:
        sector: 板块名称（支持黑话/别名）

    Returns:
        分类名称
    """
    sector = sector.strip()
    category_map = _build_sector_to_category_map()

    # 1. 直接匹配
    if sector in category_map:
        return category_map[sector]

    # 2. 别名匹配
    if sector in SECTOR_ALIASES:
        standard_sector = SECTOR_ALIASES[sector]
        if standard_sector in category_map:
            return category_map[standard_sector]

    # 3. 模糊匹配
    for key, category in category_map.items():
        if key in sector or sector in key:
            return category

    # 4. 别名模糊匹配
    for alias, standard_sector in SECTOR_ALIASES.items():
        if alias in sector or sector in alias:
            if standard_sector in category_map:
                return category_map[standard_sector]

    return "其他"


def get_all_sector_fund_mappings() -> Dict:
    """获取所有板块基金映射"""
    return SECTOR_FUND_MAP.copy()


def get_all_sector_categories() -> Dict:
    """获取所有板块分类"""
    return SECTOR_CATEGORIES.copy()


def normalize_sector_name(sector: str) -> str:
    """
    标准化板块名称（将别名/黑话转换为标准名称）

    Args:
        sector: 板块名称（可能是别名）

    Returns:
        标准板块名称
    """
    if not sector:
        return sector

    sector = sector.strip()

    # 1. 直接是标准名称
    if sector in SECTOR_FUND_MAP:
        return sector

    # 2. 别名匹配
    if sector in SECTOR_ALIASES:
        return SECTOR_ALIASES[sector]

    # 3. 模糊匹配（检查是否包含标准板块名称）
    for key in SECTOR_FUND_MAP.keys():
        if key in sector:
            return key

    # 4. 别名模糊匹配
    for alias, standard_sector in SECTOR_ALIASES.items():
        if alias in sector:
            return standard_sector

    # 5. 无法识别，返回原值
    return sector


def get_all_aliases() -> Dict[str, str]:
    """获取所有别名映射"""
    return SECTOR_ALIASES.copy()
