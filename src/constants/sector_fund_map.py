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
    '酱香科技': '白酒',
    '茅子': '白酒',
    '茅台': '白酒',
    '喝白': '白酒',
    '喝酒': '白酒',

    # 医药
    '药': '创新药',
    '毒药': '创新药',
    '垃圾药': '创新药',
    '药罐子': '创新药',
    '生物医药': '医药',
    '吃药': '医药',
    '大医药': '医药',
    '药渣': '创新药',
    'CXO': '创新药',
    'cxo': '创新药',
    '减肥药': '创新药',
    'GLP': '创新药',
    '司美格鲁肽': '创新药',
    '血制品': '医药',
    '阿尔兹海默': '创新药',

    # 半导体
    '芯': '半导体',
    '沙子': '半导体',
    '泥巴': '半导体',
    '芯片': '半导体',
    '新芯': '半导体',
    '渣男': '半导体',
    '半导体渣男': '半导体',
    '芯片渣男': '半导体',
    '大半导体': '半导体',
    'HBM': '半导体',
    '先进封装': '半导体',
    '光刻': '半导体',
    '光刻机': '半导体',

    # 光伏
    '光': '光伏',
    '光伏狗': '光伏',
    '太阳能': '光伏',
    '光伏渣男': '光伏',
    '钙钛矿': '光伏',
    '碳中和三傻': '光伏',

    # 新能源
    '锂': '锂电池',
    '锂王': '锂电池',
    '电池': '锂电池',
    '车': '新能源车',
    '新能源车': '新能源',
    '电车': '新能源车',
    '锂电': '锂电池',
    '有锂走遍天下': '锂电池',
    '固态电池': '锂电池',
    '储能': '储能',
    '碳中和': '新能源',

    # 军工
    '军': '军工',
    '军工狗': '军工',
    '飞机大炮': '军工',
    '国防': '军工',
    '渣渣军': '军工',
    '军工渣男': '军工',
    '航天': '军工',
    '导弹': '军工',
    '无人机': '军工',
    '大飞机': '军工',

    # 银行
    '银': '银行',
    '银行狗': '银行',
    '四大行': '银行',
    '大金融': '银行',

    # 券商
    '券': '券商',
    '券商狗': '券商',
    '牛市旗手': '券商',
    '证券': '券商',
    '渣券': '券商',
    '券商渣男': '券商',

    # 房地产
    '房': '房地产',
    '地产狗': '房地产',
    '房子': '房地产',
    '地产': '房地产',
    '地产渣': '房地产',

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
    '算力': '算力',
    '数据中心': '算力',
    '云计算': '云计算',
    'AIGC': '人工智能',
    'Sora': '人工智能',
    '大模型': '人工智能',
    '人形机器人': '机器人',
    '具身智能': '机器人',

    # 科技细分
    '低空': '低空经济',
    '低空经济': '低空经济',
    '飞的': '低空经济',
    '鸿蒙': '鸿蒙',
    '信创': '信创',
    '数据要素': '数据要素',
    '卫星': '卫星互联网',
    '北斗': '卫星互联网',
    '脑机': '科技',
    '量子': '科技',
    '元宇宙': '元宇宙',
    'VR': '元宇宙',
    'AR': '元宇宙',
    '区块链': '区块链',

    # 消费
    '消费': '消费',
    '大消费': '消费',
    '旅游': '旅游',
    '免税': '旅游',
    '酒店': '旅游',
    '农业': '农业',
    '养殖': '养殖',
    '猪': '养殖',
    '猪肉': '养殖',
    '二师兄': '养殖',
    '猪周期': '养殖',

    # 汽车
    '汽车': '汽车',
    '智驾': '智能驾驶',
    '无人驾驶': '智能驾驶',
    '自动驾驶': '智能驾驶',

    # 有色
    '有色': '有色金属',
    '铜': '有色金属',
    '铝': '有色金属',
    '锡': '有色金属',
    '镍': '有色金属',
    '钴': '有色金属',
    '稀土': '稀土',

    # 电力
    '电': '电力',
    '绿电': '绿色电力',
    '核电': '核电',
    '水电': '水电',

    # 宽基
    '300': '沪深300',
    '500': '中证500',
    '创业板': '创业板',
    '科创板': '科创板',
    '科创50': '科创50',
    '北证': '科创板',
}


SECTOR_FUND_MAP = {
    # 消费类
    '白酒': {'code': '161725', 'name': '招商中证白酒指数(LOF)A'},
    '食品饮料': {'code': '160222', 'name': '国泰国证食品饮料行业(LOF)A'},
    '消费': {'code': '159928', 'name': '消费ETF汇添富'},
    '家电': {'code': '159996', 'name': '家电ETF国泰'},
    '消费电子': {'code': '159732', 'name': '消费电子ETF华夏'},
    
    # 医药医疗
    '医药': {'code': '512010', 'name': '医药ETF易方达'},
    '医疗': {'code': '512010', 'name': '医药ETF易方达'},
    '医疗器械': {'code': '159898', 'name': '医疗器械ETF招商'},
    '创新药': {'code': '159858', 'name': '创新药ETF南方'},
    '中药': {'code': '159647', 'name': '中药ETF鹏华'},   # 第 27 轮体检改码：原标的官方名与板块无关
    '生物科技': {'code': '501009', 'name': '汇添富中证生物科技指数(LOF)A'},
    
    # 科技类
    '半导体': {'code': '512480', 'name': '半导体ETF国联安'},
    '芯片': {'code': '159995', 'name': '芯片ETF华夏'},
    '人工智能': {'code': '515070', 'name': '人工智能ETF华夏'},
    'AI': {'code': '515070', 'name': '人工智能ETF华夏'},
    '科技': {'code': '515000', 'name': '科技ETF华宝'},
    '计算机': {'code': '512720', 'name': '计算机ETF国泰'},
    '软件': {'code': '515230', 'name': '软件ETF国泰'},
    '通信': {'code': '515880', 'name': '通信ETF国泰'},
    '5G': {'code': '159811', 'name': '5GETF博时'},   # 第 27 轮体检改码：515050 的官方名其实是"通信ETF华夏"，标签写的是 5GETF
    '云计算': {'code': '516510', 'name': '云计算ETF易方达'},
    '大数据': {'code': '515400', 'name': '大数据ETF富国'},   # 第 27 轮二次改码：158066 前海开源在源端 0 条净值（实测），取不到数就验不了
    '机器人': {'code': '562500', 'name': '机器人ETF华夏'},
    
    # 新能源
    '新能源': {'code': '516160', 'name': '新能源ETF南方'},
    '新能源车': {'code': '515030', 'name': '新能源车ETF华夏'},
    '光伏': {'code': '515790', 'name': '光伏ETF华泰柏瑞'},
    '储能': {'code': '159305', 'name': '储能电池ETF广发'},   # 第 27 轮体检改码：原标的官方名与板块无关
    '锂电池': {'code': '159840', 'name': '锂电池ETF工银'},
    
    # 军工
    '军工': {'code': '512660', 'name': '军工ETF国泰'},
    '国防': {'code': '512670', 'name': '国防ETF鹏华'},
    '船舶': {'code': '560710', 'name': '船舶ETF富国'},   # 第 27 轮体检改码：原来借的是 512660 军工ETF，名册里有字面对口的
    
    # 金融地产
    '银行': {'code': '001594', 'name': '天弘中证银行ETF联接A'},
    '券商': {'code': '512000', 'name': '券商ETF华宝'},
    '证券': {'code': '512880', 'name': '证券ETF国泰'},
    '保险': {'code': '167301', 'name': '方正富邦中证保险A'},
    '房地产': {'code': '160218', 'name': '国泰国证房地产行业指数A'},
    '地产': {'code': '160218', 'name': '国泰国证房地产行业指数A'},
    '基建': {'code': '159619', 'name': '基建ETF国泰'},
    
    # 周期资源
    '有色': {'code': '160221', 'name': '国泰国证有色金属行业指数(LOF)A'},
    '有色金属': {'code': '160221', 'name': '国泰国证有色金属行业指数(LOF)A'},
    '铜': {'code': '160221', 'name': '国泰国证有色金属行业指数(LOF)A'},   # 见 SECTOR_PROXY_ALLOWED
    '煤炭': {'code': '161724', 'name': '招商中证煤炭等权指数(LOF)A'},
    '钢铁': {'code': '515210', 'name': '钢铁ETF国泰'},
    '化工': {'code': '159870', 'name': '化工ETF鹏华'},
    '石油': {'code': '159148', 'name': '石油ETF富国'},   # 第 27 轮体检改码：原标的官方名与板块无关
    '油气': {'code': '159309', 'name': '油气ETF汇添富'},   # 第 27 轮体检改码：原标的官方名与板块无关
    '黄金': {'code': '518880', 'name': '黄金ETF华安'},
    '白银': {'code': '161226', 'name': '国投瑞银白银期货(LOF)A'},
    '稀土': {'code': '516780', 'name': '稀土ETF华泰柏瑞'},
    '小金属': {'code': '516780', 'name': '稀土ETF华泰柏瑞'},
    # '铜'/'小金属' 都借在有色那两只上：全名册 27905 只里没有一只名字含「铜」，
    # 含「小金属」的也是 0 只 —— agent 面对的是同一份名册，变不出更对口的标的。
    # 这类"确实没有对口基金"的借用必须写进 SECTOR_PROXY_ALLOWED，由守护用例盯着。

    # 公用事业
    '电力': {'code': '159146', 'name': '电力ETF华宝'},   # 第 27 轮体检改码：561170 官方名是"绿色电力ETF富国"，管不了全电力
    '绿色电力': {'code': '561170', 'name': '绿色电力ETF富国'},
    '绿电': {'code': '561170', 'name': '绿色电力ETF富国'},
    # 第 27 轮体检删除 '水电'/'核电'：全网名册里"水电""核电"两个词各 0 只基金，
    # 借 561170 绿色电力ETF 属硬凑 ⇒ 交给带验证的 agent/映射表，与风电/氢能源同一条处置
    '环保': {'code': '512580', 'name': '环保ETF广发'},
    '水务': {'code': '508006', 'name': '富国首创水务REIT'},   # 第 27 轮体检改码：原标的官方名与板块无关
    
    # 交通物流
    '物流': {'code': '516910', 'name': '物流ETF富国'},
    '快递': {'code': '516910', 'name': '物流ETF富国'},
    '航空': {'code': '158009', 'name': '航空航天ETF南方'},   # 第 27 轮体检改码：原标的官方名与板块无关
    
    # 港股/海外
    '恒生科技': {'code': '513180', 'name': '恒生科技ETF华夏'},
    '恒科': {'code': '513180', 'name': '恒生科技ETF华夏'},
    '港股科技': {'code': '513180', 'name': '恒生科技ETF华夏'},
    '港股': {'code': '513180', 'name': '恒生科技ETF华夏'},
    '港股互联网': {'code': '513180', 'name': '恒生科技ETF华夏'},
    '港股医药': {'code': '159718', 'name': '港股医药ETF平安'},
    '港股消费': {'code': '159735', 'name': '港股消费ETF银华'},
    '港股通': {'code': '513990', 'name': '港股通ETF招商'},
    '中概互联': {'code': '164906', 'name': '交银中证海外中国互联网指数(LOF)A'},
    '中概': {'code': '164906', 'name': '交银中证海外中国互联网指数(LOF)A'},
    '日股': {'code': '513520', 'name': '日经ETF华夏'},
    '日经': {'code': '513520', 'name': '日经ETF华夏'},
    '日本': {'code': '513520', 'name': '日经ETF华夏'},
    '美股': {'code': '513100', 'name': '纳指ETF国泰'},
    '纳斯达克': {'code': '513100', 'name': '纳指ETF国泰'},
    '标普': {'code': '513500', 'name': '标普500ETF博时'},
    '全球': {'code': '513100', 'name': '纳指ETF国泰'},
    
    # 互联网/传媒
    '互联网': {'code': '517200', 'name': '互联网ETF嘉实'},   # 第 27 轮体检改码：原标的官方名与板块无关
    '游戏': {'code': '159869', 'name': '游戏ETF华夏'},
    '传媒': {'code': '512980', 'name': '传媒ETF广发'},
    '影视': {'code': '159855', 'name': '影视ETF银华'},   # 第 27 轮体检改码：原来借的是 512980 传媒ETF
    '教育': {'code': '513360', 'name': '教育ETF博时'},   # 第 27 轮体检改码：原标的官方名与板块无关
    
    # 宽基指数
    '沪深300': {'code': '510300', 'name': '沪深300ETF华泰柏瑞'},
    '中证500': {'code': '510500', 'name': '中证500ETF南方'},
    '创业板': {'code': '159915', 'name': '创业板ETF易方达'},
    '科创板': {'code': '588000', 'name': '科创50ETF华夏'},
    '科创50': {'code': '588000', 'name': '科创50ETF华夏'},
    '上证50': {'code': '510100', 'name': '上证50ETF易方达'},
    '中证1000': {'code': '159845', 'name': '中证1000ETF华夏'},
    '双创': {'code': '159781', 'name': '科创创业ETF易方达'},
    
    # 策略风格
    '红利': {'code': '510880', 'name': '红利ETF华泰柏瑞'},
    '红利低波': {'code': '512890', 'name': '红利低波ETF华泰柏瑞'},
    '高股息': {'code': '159207', 'name': '高股息ETF广发'},   # 第 27 轮体检改码：原来借的是 510880 红利ETF
    '低波': {'code': '512890', 'name': '红利低波ETF华泰柏瑞'},
    '央企': {'code': '512950', 'name': '央企改革ETF华夏'},
    '国企': {'code': '510270', 'name': '国企ETF中银'},   # 第 27 轮体检改码：原标的官方名与板块无关
    '自由现金流': {'code': '159201', 'name': '自由现金流ETF华夏'},
    '大盘': {'code': '510050', 'name': '上证50ETF华夏'},
    
    # 商品/其他
    '豆粕': {'code': '159985', 'name': '豆粕ETF华夏'},
    '能源化工': {'code': '159981', 'name': '能源化工ETF建信'},
    '有色金属商品': {'code': '159980', 'name': '有色ETF大成'},

    # 新兴板块
    '算力': {'code': '158041', 'name': '创业板算力ETF华夏'},   # 第 27 轮体检改码：原标的官方名与板块无关
    '信创': {'code': '562570', 'name': '信创ETF华夏'},
    '元宇宙': {'code': '159786', 'name': 'VRETF银华'},
    '智能驾驶': {'code': '516520', 'name': '智能驾驶ETF华泰柏瑞'},   # 第 27 轮体检改码：原来借的是 516110 汽车ETF
    '旅游': {'code': '159766', 'name': '旅游ETF富国'},
    '农业': {'code': '159825', 'name': '农业ETF富国'},
    '养殖': {'code': '159865', 'name': '养殖ETF国泰'},
    '汽车': {'code': '516110', 'name': '汽车ETF国泰'},
    '建材': {'code': '159619', 'name': '基建ETF国泰'},
    '家居': {'code': '159996', 'name': '家电ETF国泰'},
}


SECTOR_CATEGORIES = {
    '消费': ['白酒', '食品饮料', '家电', '农业', '养殖', '消费电子', '旅游', '免税', '酒店', '家居'],
    '医药': ['医药', '医疗', '医疗器械', '创新药', '中药', '生物科技', '医美'],
    '科技': ['科技', '半导体', '芯片', '存储', '人工智能', 'AI', '机器人',
            '软件', '计算机', '云计算', '大数据', '5G', '通信', '卫星', '北斗',
            '算力', '低空经济', '信创', '鸿蒙', '数据要素', '元宇宙', '区块链',
            '卫星互联网', '智能驾驶', '人形机器人'],
    '新能源': ['新能源', '新能源车', '光伏', '锂电池', '储能', '风电', '核电', '氢能源', '固态电池'],
    '周期': ['有色', '有色金属', '铝', '铜', '稀土', '煤炭', '钢铁', '化工', '石油', '油气'],
    '金融': ['银行', '券商', '保险', '房地产', '地产', '基建', '建筑', '建材'],
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
_DB_ALIASES_CACHE = None


def _load_db_aliases() -> Dict[str, str]:
    """从数据库加载用户自定义别名（带缓存）"""
    global _DB_ALIASES_CACHE
    if _DB_ALIASES_CACHE is not None:
        return _DB_ALIASES_CACHE

    try:
        from src.models.database import SessionLocal, SectorAlias
        db = SessionLocal()
        try:
            rows = db.query(SectorAlias).all()
            _DB_ALIASES_CACHE = {row.alias_name: row.sector_name for row in rows}
        finally:
            db.close()
    except Exception:
        _DB_ALIASES_CACHE = {}
    return _DB_ALIASES_CACHE


def refresh_db_aliases_cache():
    """刷新数据库别名缓存（添加/删除别名后调用）"""
    global _DB_ALIASES_CACHE
    _DB_ALIASES_CACHE = None
    return _load_db_aliases()


def _build_sector_to_category_map() -> Dict[str, str]:
    """构建板块到分类的映射表"""
    global _SECTOR_TO_CATEGORY
    if _SECTOR_TO_CATEGORY is None:
        _SECTOR_TO_CATEGORY = {}
        for category, sectors in SECTOR_CATEGORIES.items():
            for sector in sectors:
                _SECTOR_TO_CATEGORY[sector] = category
    return _SECTOR_TO_CATEGORY


# 这些板块**故意不给静态标的**。依据是一条可复现的实测（2026-09-22，东财全名册 27905 只，
# 用 `scripts/audit_static_sector_map.py` 同一套核心词判据）：**下面 13 个词在整个名册里的
# 命中数都是 0** —— 一只名字含「风电」「区块链」「卫星互联网」的基金都不存在，
# 硬套只能是别的主题。出路是带验证的 agent / 已审查的映射表（agent 面对同一份名册，
# 但它可以按主题就近选并留下证据，比如 卫星互联网 → 名字含「卫星」的那只）。
#
# 为什么光把键从 `SECTOR_FUND_MAP` 里删掉不够：`get_fund_for_sector` 的第 5~7 步是
# **子串模糊匹配**，第 27 轮实测删掉键之后 `get_fund_for_sector('卫星互联网')` 仍返回
# 517200 互联网ETF嘉实（被表里更短的键 '互联网' 吸走）⇒ "不许硬凑"必须写成显式名单，
# 在别名与模糊匹配之前返回 None。反过来，老板在 `sector_alias` 表里登记的别名压过本名单
# （人的决定优先于代码里的默认），这条由守护用例盯着。
SECTOR_NO_STATIC_FUND = {
    '风电', '氢能源', '燃气', '机场', '港口', '低空经济', '数据要素', '鸿蒙',
    '卫星互联网', '区块链', 'A股互联网', '水电', '核电',
}


def _literal_block_hit(sector: str) -> bool:
    """输入是否落在"不许硬凑"名单上——**包括名单词的更长说法**。

    第 27 轮 C-M2 实测：名单原来只挡逐字相同的键，于是 `卫星互联网产业` 绕过名单，
    再被 `get_fund_for_sector` 第 5 步的子串匹配吸到表里更短的键 `互联网` 上，
    拿回的正是本轮声称要挡住的那只 517200 互联网ETF嘉实。

    比较只能在"**同一个词的内部**"做（第 28 轮 I-MAJOR 实测我上一版写错了方向）：
    只有当表里存在一个**包含该名单词的更长键**时才让键赢（假想例：名单有 '水电'
    而表里出现 '水利水电' —— 今天表里没有这种键，这条纯属防以后）。
    我上一版比的是"整串里任意更长的表键"，于是 `核电与半导体`、`风电和人工智能`
    这种"名单词 + 一个不相干的长键"整体逃逸屏蔽，照旧返回 512480 / 515070
    —— 把两件不相干的事拼成一个板块名，不该让其中一件替另一件放行。
    """
    for blocked in sorted((w for w in SECTOR_NO_STATIC_FUND if w in sector),
                          key=len, reverse=True):
        longer_key = max((len(k) for k in SECTOR_FUND_MAP
                          if len(k) >= 3 and k != blocked and blocked in k), default=0)
        if longer_key <= len(blocked):
            return True
    return False


def get_fund_for_sector(sector: str) -> Optional[Dict]:
    """
    获取板块对应的基金信息

    Args:
        sector: 板块名称（支持黑话/别名）

    Returns:
        {'code': 基金代码, 'name': 基金名称} 或 None
    """
    sector = sector.strip()
    db_aliases = _load_db_aliases()

    # 1. 直接匹配
    if sector in SECTOR_FUND_MAP:
        return SECTOR_FUND_MAP[sector]

    # 2. 数据库自定义别名（老板在界面上登记的）先于下面任何规则：
    #    "不许硬凑"是代码里的默认，用户明确登记了别名就是人的决定，压过默认。
    if sector in db_aliases and db_aliases[sector] in SECTOR_FUND_MAP:
        return SECTOR_FUND_MAP[db_aliases[sector]]

    # 3. 明确"不许硬凑"的板块：硬编码别名与模糊匹配都不给，直接交给 agent/映射表
    if _literal_block_hit(sector):
        return None

    # 4. 硬编码别名匹配
    if sector in SECTOR_ALIASES:
        standard_sector = SECTOR_ALIASES[sector]
        if standard_sector in SECTOR_FUND_MAP:
            return SECTOR_FUND_MAP[standard_sector]

    # 5. 模糊匹配（包含关系）
    #    长度门槛：短键（"光"、"车"、"药"）做子串匹配会把不相干的板块吸到同一只基金上，
    #    这正是"识别出来的基金离板块差十万八千里"的成因之一。短别名只能通过
    #    SECTOR_ALIASES / sector_alias 表显式登记，不走子串。
    for key, fund_info in SECTOR_FUND_MAP.items():
        if len(key) >= 3 and (key in sector or sector in key):
            return fund_info

    # 6. 硬编码别名模糊匹配
    for alias, standard_sector in SECTOR_ALIASES.items():
        if len(alias) >= 3 and (alias in sector or sector in alias):
            if standard_sector in SECTOR_FUND_MAP:
                return SECTOR_FUND_MAP[standard_sector]

    # 7. 数据库别名模糊匹配
    for alias, standard_sector in db_aliases.items():
        if len(alias) >= 3 and (alias in sector or sector in alias):
            if standard_sector in SECTOR_FUND_MAP:
                return SECTOR_FUND_MAP[standard_sector]

    return None




# 显式登记的"合法代理"：官方名与板块字面无关，但名册里确实找不到更对口的基金。
# 守护用例（tests/unit/test_sector_map_guard.py）两头都卡：
#   ① 表里每一个非字面命中的条目都必须出现在这里且写明理由 —— 新增不登记就红；
#   ② 登记了却其实字面命中的（如 '日股'→日经ETF 共享"日"字）算死条目，也要红 ——
#      否则这张表会慢慢变成"什么都往里塞"的免检通道。
SECTOR_PROXY_ALLOWED = {
    '美股': "名册里没有名字含「美股」的基金；纳指ETF是美股宽基里规模与历史都不错的代理",
    '全球': "同上：全球视角先给美国经济，若要更全面应换成含发达+新兴的宽基（待老板定）",
    '元宇宙': "名册里没有含「元宇宙」的基金（0 只）；VRETF银华 跟踪 VR/产业主题，是最接近的代理",
    '港股互联网': "恒生科技指数覆盖港股互联网平台主体，名册里没有含该板块词的基金",
    '小金属': "名册里没有含「小金属」的基金（0 只）；516780 稀土ETF 是有色里最贴小金属口径的场内标的",
    '铜': "全名册 27905 只里没有一只名字含「铜」（0 只），agent 也只能从这份名册里挑 ⇒ "
          "借 160221 国泰国证有色金属行业指数（铜是有色金属的子板块）比「查无基金」更实用",
    '快递': "名册里没有含「快递」的基金（0 只）；516910 物流ETF 跟踪中证现代物流指数，快递主体在里面",
    '大盘': "名册里带「大盘」的场内只有 超大盘/大盘成长/大盘价值（都是细分风格），"
            "博主说「大盘」通常指沪深大盘股 ⇒ 上证50ETF 比硬套一个风格指数更贴近",
    '日股': "名册里带「日」的场内只有 513520 日经ETF华夏（日本/日经/日股 三个键共用它）；"
            "体检按字面判它「是日经板块的标的」，属同义而非错配",
    '港股': "SECTOR_ALIASES 把 港/港仔/恒仔/港股 一律归一到恒生科技，静态表跟着它走；"
            "名册里带「港股」的宽基是 513990 港股通ETF，改挂哪个属老板的口径决定",
}


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

    # 2. 硬编码别名匹配
    if sector in SECTOR_ALIASES:
        standard_sector = SECTOR_ALIASES[sector]
        if standard_sector in category_map:
            return category_map[standard_sector]

    # 3. 数据库自定义别名匹配
    db_aliases = _load_db_aliases()
    if sector in db_aliases:
        standard_sector = db_aliases[sector]
        if standard_sector in category_map:
            return category_map[standard_sector]

    # 4. 模糊匹配
    for key, category in category_map.items():
        if key in sector or sector in key:
            return category

    # 5. 硬编码别名模糊匹配
    for alias, standard_sector in SECTOR_ALIASES.items():
        if alias in sector or sector in alias:
            if standard_sector in category_map:
                return category_map[standard_sector]

    # 6. 数据库别名模糊匹配
    for alias, standard_sector in db_aliases.items():
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

    # 2. 硬编码别名匹配
    if sector in SECTOR_ALIASES:
        return SECTOR_ALIASES[sector]

    # 3. 数据库自定义别名匹配
    db_aliases = _load_db_aliases()
    if sector in db_aliases:
        return db_aliases[sector]

    # 3.5 "不许硬凑"的板块**本身就是标准名**，不能再被子串归到别的板块上去
    #     （含它的更长说法：`卫星互联网产业` 不许被归一成 `互联网`，见 `_literal_block_hit`）。
    #     第 27 轮实测：把 'A股互联网'/'卫星互联网' 从表里删掉之后，下面第 4 步会把它们
    #     归一成 '互联网' —— 于是板块身份判据（sector_core）也跟着变，
    #     卫星互联网的预测就能顶着"名字含互联网"的相似度被挂到 互联网ETF 上。
    #     归一化不该取决于"这个板块在静态表里有没有基金"。
    if _literal_block_hit(sector):
        return sector

    # 4. 模糊匹配（检查是否包含标准板块名称）
    for key in SECTOR_FUND_MAP.keys():
        if key in sector:
            return key

    # 5. 硬编码别名模糊匹配
    for alias, standard_sector in SECTOR_ALIASES.items():
        if alias in sector:
            return standard_sector

    # 6. 数据库别名模糊匹配
    for alias, standard_sector in db_aliases.items():
        if alias in sector:
            return standard_sector

    # 7. 无法识别，返回原值
    return sector


def get_all_aliases() -> Dict[str, str]:
    """获取所有别名映射"""
    return SECTOR_ALIASES.copy()
