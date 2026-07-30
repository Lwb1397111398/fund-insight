"""
基金匹配共享工具
提供三级降级基金匹配机制
"""
from typing import Optional, Tuple
from sqlalchemy.orm import Session

from src.constants.sector_fund_map import get_fund_for_sector as get_mapped_fund
from src.models.database import FundInfo

# 与 fund_auto_manager._verify_fund_exists 同源的本地过滤词：
# 快速路径跳过 HTTP 验证，但债基排除语义必须保留。
_EXCLUDED_FUND_KEYWORDS = ("债券", "债", "货币", "理财", "短债", "纯债", "利率债", "信用债")


def match_fund_with_fallback(
    pred: dict,
    sector: str,
    fund_auto_manager,
    llm_analyzer,
    db: Session
) -> Tuple[Optional[str], Optional[str]]:
    """
    三级降级基金匹配机制

    优先级（按可靠性排序）：
    1. 使用 fund_auto_manager 自动匹配（优先查本地映射表）
    2. 使用 LLM 分析器的板块映射表（经过验证的映射）
    3. 使用本地默认映射表
    4. 使用LLM返回的fund_code（作为最后补充，需严格验证）

    Args:
        pred: 预测字典
        sector: 板块名称
        fund_auto_manager: 基金自动管理器
        llm_analyzer: LLM分析器
        db: 数据库会话

    Returns:
        (fund_code, fund_name)
    """
    # 第零级：零网络快速路径。
    # 与第一级 auto_add 内部同源的硬编码映射表查 code，若该基金已入库则直接返回，
    # 省掉 auto_add 每次都发起的 fund_api.get_fund_info HTTP 验证
    # （Render 海外访问中国基金站点极慢，是批量分析的主要延迟来源）。
    # 映射表未命中或基金未入库时原样落到第一级，新基金自动入库语义完全保留。
    if sector:
        try:
            mapped = get_mapped_fund(sector)
            if mapped and mapped.get("code"):
                existing = db.query(FundInfo).filter(
                    FundInfo.fund_code == str(mapped["code"])
                ).first()
                if existing and not any(
                    keyword in (existing.fund_name or "")
                    for keyword in _EXCLUDED_FUND_KEYWORDS
                ):
                    return existing.fund_code, existing.fund_name
        except Exception as e:
            print(f"[Fund Match] Level 0 (Fast Path) failed: {e}")

    # 第一级：使用 fund_auto_manager 自动匹配（优先查本地映射表）
    try:
        success, message, fund = fund_auto_manager.auto_add_fund_for_prediction(sector, db)
        if success and fund:
            print(f"[Fund Match] Level 1 (Auto Manager): {message}")
            return fund.fund_code, fund.fund_name
    except Exception as e:
        print(f"[Fund Match] Level 1 failed: {e}")

    # 第二级：使用 LLM 分析器的板块映射表（经过验证的映射）
    try:
        fund_info = llm_analyzer.get_fund_for_sector(sector)
        if fund_info:
            fund_code = fund_info.get("code")
            fund_name = fund_info.get("name")
            print(f"[Fund Match] Level 2 (LLM Mapper): {fund_name} ({fund_code})")
            return fund_code, fund_name
    except Exception as e:
        print(f"[Fund Match] Level 2 failed: {e}")

    # 第三级：使用本地默认映射表
    DEFAULT_FUND_MAP = {
        '白酒': ('161725', '招商中证白酒指数'),
        '医药': ('001017', '华夏医疗健康混合A'),
        '半导体': ('512480', '国泰CES半导体芯片ETF'),
        '新能源': ('516790', '国泰中证新能源汽车ETF'),
        '军工': ('005633', '华夏军工安全混合A'),
        '银行': ('001594', '易方达中证银行指数LOF'),
        '券商': ('512880', '证券ETF'),
        '房地产': ('160218', '国泰国证房地产指数'),
        '有色金属': ('160221', '国泰国证有色金属行业指数'),
        '煤炭': ('161724', '招商中证煤炭等权指数LOF'),
        '黄金': ('000218', '易方达黄金ETF联接A'),
        '港股': ('513180', '恒生科技ETF'),
        '恒生科技': ('513180', '恒生科技ETF'),
        '互联网': ('515000', '互联网ETF'),
        '人工智能': ('015719', '华夏中证人工智能主题ETF联接A'),
        '消费': ('000083', '汇添富消费行业混合'),
        '电力': ('561170', '广发中证全指电力公用事业ETF'),
        '化工': ('159870', '鹏华中证细分化工产业ETF'),
        '石油': ('501017', '石油LOF'),
        '机器人': ('159770', '机器人ETF'),
        '通信': ('515880', '通信ETF'),
        '创新药': ('159858', '创新药ETF'),
        '消费电子': ('159732', '消费电子ETF'),
    }

    if sector in DEFAULT_FUND_MAP:
        fund_code, fund_name = DEFAULT_FUND_MAP[sector]
        print(f"[Fund Match] Level 3 (Default Mapper): {fund_name} ({fund_code})")
        return fund_code, fund_name

    # 第四级：使用LLM返回的fund_code（作为最后补充，需严格验证）
    llm_fund_code = pred.get("fund_code")
    llm_fund_name = pred.get("fund_name")

    if llm_fund_code and str(llm_fund_code).strip():
        # 严格验证：必须是6位数字
        if len(str(llm_fund_code)) == 6 and str(llm_fund_code).isdigit():
            # 检查基金是否已存在于数据库（更可靠）
            existing_fund = db.query(FundInfo).filter(
                FundInfo.fund_code == str(llm_fund_code)
            ).first()

            if existing_fund:
                print(f"[Fund Match] Level 4 (LLM Result - Verified): {existing_fund.fund_name} ({llm_fund_code})")
                return existing_fund.fund_code, existing_fund.fund_name
            else:
                # 基金不存在于数据库，LLM返回的代码可能不可靠
                print(f"[Fund Match] Level 4 (LLM Result - Unverified): {llm_fund_name} ({llm_fund_code}) - 基金不存在，跳过")

    # 最终降级：返回None
    print(f"[Fund Match] All levels failed, using sector name: {sector}")
    return None, None
