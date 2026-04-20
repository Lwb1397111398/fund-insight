"""
AI 帖子分析器 - 增强版（兼容性包装器）

⚠️ 注意：此模块已迁移到 src.analyzer.post_analyzer
此文件保留用于向后兼容，新代码请直接从 analyzer 模块导入

原功能：
1. AI 筛选：多维度评分判断帖子价值
2. AI 分析：生成详细的分析报告（含可信度支撑因素）
3. 板块标准化：统一板块名称

迁移说明：
- 所有功能已整合到 src.analyzer.post_analyzer
- EnhancedAIAnalyzer 现在只是 PostAnalyzer 的别名
"""
import warnings

# 从新的位置导入
from src.analyzer.post_analyzer import (
    PostAnalyzer,
    PostAnalysisResult,
    get_post_analyzer,
    ScoringDimension,
    CredibilityFactor,
    SECTOR_ALIAS_MAP,
    SOURCE_AUTHORITY_SCORES,
)

# 发出迁移警告
warnings.warn(
    "crawler.enhanced_ai_analyzer 已迁移到 analyzer.post_analyzer，"
    "请更新导入语句: from src.analyzer.post_analyzer import ...",
    DeprecationWarning,
    stacklevel=2
)

# 兼容性别名
EnhancedAIAnalyzer = PostAnalyzer
EnhancedAnalysisResult = PostAnalysisResult
get_enhanced_analyzer = get_post_analyzer

# 导出所有符号，保持兼容性
__all__ = [
    'EnhancedAIAnalyzer',
    'EnhancedAnalysisResult',
    'get_enhanced_analyzer',
    'PostAnalyzer',
    'PostAnalysisResult',
    'get_post_analyzer',
    'ScoringDimension',
    'CredibilityFactor',
    'SECTOR_ALIAS_MAP',
    'SOURCE_AUTHORITY_SCORES',
]