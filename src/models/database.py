"""
数据库模型 - 增强版
支持：自动分析、智能基金管理、定时验证、投资建议
"""
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, JSON, Date, UniqueConstraint, Index, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional
import logging

import sys
import os

logger = logging.getLogger(__name__)

# Fix: 只在直接运行此文件时添加路径
if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.core.config import config

# 支持 PostgreSQL 和 SQLite
DATABASE_URL = os.getenv("DATABASE_URL")
DB_TYPE = "sqlite"  # 默认值

if DATABASE_URL and DATABASE_URL.startswith("postgresql"):
    # PostgreSQL 数据库
    try:
        import psycopg2  # 检查驱动是否可用
        engine = create_engine(
            DATABASE_URL,
            echo=False,
            pool_size=10,
            max_overflow=10,
            pool_recycle=300,
            pool_pre_ping=True,
            pool_timeout=30,
            pool_use_lifo=True,
        )
        DB_TYPE = "postgresql"
        logger.info(f"[数据库] 使用 PostgreSQL 引擎（连接池: 5+5）")
    except ImportError:
        logger.warning(f"[数据库] psycopg2 未安装，回退到 SQLite")
        DB_PATH = Path(config.DB_PATH)
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f'sqlite:///{DB_PATH}', echo=False)
else:
    # 回退到 SQLite
    DB_PATH = Path(config.DB_PATH)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f'sqlite:///{DB_PATH}', echo=False)

Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)


class Blogger(Base):
    """博主表 - 多维度评级"""
    __tablename__ = 'bloggers'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    platform = Column(String(50), default='xiaohongshu')
    description = Column(Text)
    accuracy_rate = Column(Float, default=0.0)
    total_predictions = Column(Integer, default=0)
    correct_predictions = Column(Integer, default=0)
    total_verify_score = Column(Integer, default=0)
    
    grade = Column(String(5), default='C')
    
    ultra_short_accuracy = Column(Float, default=0.0)
    ultra_short_total = Column(Integer, default=0)
    ultra_short_correct = Column(Integer, default=0)
    
    sector_coverage = Column(Integer, default=0)
    avg_prediction_period = Column(Float, default=0.0)
    risk_warning_count = Column(Integer, default=0)
    
    last_prediction_date = Column(Date)
    prediction_frequency = Column(Float, default=0.0)
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index('ix_bloggers_platform', 'platform'),
        Index('ix_bloggers_is_active', 'is_active'),
    )


class Post(Base):
    """帖子表 - 支持自动分析"""
    __tablename__ = 'posts'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    blogger_id = Column(Integer, ForeignKey('bloggers.id'), nullable=False)
    title = Column(String(500))
    content = Column(Text, nullable=False)
    post_date = Column(Date, nullable=False)
    source_url = Column(String(500))
    
    analyzed = Column(Boolean, default=False)
    analysis_result = Column(JSON)
    auto_titled = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.now)

    blogger = relationship("Blogger", lazy="selectin")

    __table_args__ = (
        Index('ix_posts_blogger_id', 'blogger_id'),
        Index('ix_posts_post_date', 'post_date'),
        Index('ix_posts_blogger_date', 'blogger_id', 'post_date'),
    )


class Prediction(Base):
    """
    预测记录表 - 支持多次验证、软删除机制
    """
    __tablename__ = 'predictions'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    post_id = Column(Integer, ForeignKey('posts.id', ondelete='RESTRICT'), nullable=False)
    blogger_id = Column(Integer, ForeignKey('bloggers.id'), nullable=False)
    
    fund_code = Column(String(20))
    fund_name = Column(String(100))
    sector = Column(String(100))
    sector_type = Column(String(50))
    
    prediction_type = Column(String(20), nullable=False)
    prediction_content = Column(Text)
    confidence = Column(Integer, default=50)
    
    prediction_date = Column(Date, nullable=False)
    prediction_period = Column(String(20), default='1个月')
    target_date = Column(Date)
    
    status = Column(String(20), default='pending')
    
    start_nav = Column(Float)
    start_nav_date = Column(Date)
    current_nav = Column(Float)
    current_nav_date = Column(Date)
    end_nav = Column(Float)
    end_nav_date = Column(Date)
    actual_change = Column(Float)
    
    is_correct = Column(Boolean)
    verify_score = Column(Integer, default=0)
    ai_judgment = Column(Text)
    verified_at = Column(DateTime)
    
    verify_count = Column(Integer, default=0)
    verify_history = Column(JSON)
    last_verify_date = Column(Date)
    next_verify_date = Column(Date)
    
    is_expired = Column(Boolean, default=False)
    has_active_prediction = Column(Boolean, default=True)
    
    is_deleted = Column(Boolean, default=False)
    deleted_at = Column(DateTime)
    deleted_by = Column(String(50))
    delete_reason = Column(String(200))
    restore_before = Column(Date)
    
    created_at = Column(DateTime, default=datetime.now)
    
    blogger = relationship("Blogger", foreign_keys=[blogger_id], lazy="selectin")
    post = relationship("Post", foreign_keys=[post_id], lazy="selectin")

    __table_args__ = (
        Index('ix_predictions_blogger_id', 'blogger_id'),
        Index('ix_predictions_status', 'status'),
        Index('ix_predictions_fund_code', 'fund_code'),
        Index('ix_predictions_is_deleted', 'is_deleted'),
        Index('ix_predictions_blogger_status', 'blogger_id', 'status', 'is_deleted'),
        Index('ix_predictions_target_date', 'target_date'),
    )


class Viewpoint(Base):
    """博主观点表 - 支持爬虫来源、生命周期管理、标签体系"""
    __tablename__ = 'viewpoints'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    blogger_id = Column(Integer, ForeignKey('bloggers.id'), nullable=True)
    post_id = Column(Integer, ForeignKey('posts.id', ondelete='SET NULL'), nullable=True)
    
    fund_code = Column(String(20))
    fund_name = Column(String(100))
    content = Column(Text)
    author = Column(String(100), default='网友')
    source = Column(String(50), default='manual')
    
    article_id = Column(String(100))
    article_url = Column(String(500))
    content_hash = Column(String(32))
    
    market_direction = Column(String(20))
    confidence = Column(Integer, default=50)
    sectors_bullish = Column(JSON)
    sectors_bearish = Column(JSON)
    reasoning = Column(Text)
    summary = Column(String(200))
    
    time_horizon = Column(String(20), default='medium')
    validity_period = Column(String(20), default='1个月')
    valid_until = Column(Date)
    
    viewpoint_date = Column(Date, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    
    score = Column(Float, default=0.0)
    content_depth = Column(Float, default=0.0)
    timeliness = Column(Float, default=0.0)
    data_support = Column(Float, default=0.0)
    reference_value = Column(Float, default=0.0)
    
    viewpoint_type = Column(String(50), default='深度分析')
    credibility_score = Column(Integer, default=50)
    credibility_factors = Column(JSON)
    
    tags = Column(JSON)
    analysis_tags = Column(JSON)
    
    is_expired = Column(Boolean, default=False)
    needs_reassessment = Column(Boolean, default=False)
    reassessment_reason = Column(String(200))
    
    weight = Column(Float, default=1.0)
    source_authority = Column(Float, default=0.5)
    
    read_count = Column(Integer, default=0)
    is_vip = Column(Boolean, default=False)
    
    action_suggestion = Column(String(20))
    risk_level = Column(String(20), default='medium')
    
    analysis_summary = Column(Text)
    
    is_deleted = Column(Boolean, default=False)
    deleted_at = Column(DateTime)
    deleted_by = Column(String(50))
    delete_reason = Column(String(200))
    restore_before = Column(Date)
    
    is_summary = Column(Boolean, default=False)
    original_count = Column(Integer, default=0)
    original_ids = Column(JSON)
    topics = Column(JSON)
    
    def calculate_weight(self) -> float:
        base_weight = self.source_authority or 0.5
        
        if self.credibility_score and self.credibility_score >= 80:
            base_weight += 0.2
        elif self.credibility_score and self.credibility_score >= 60:
            base_weight += 0.1
        
        if self.is_vip:
            base_weight += 0.15
        
        if self.read_count and self.read_count > 1000:
            base_weight += 0.1
        
        return min(2.0, max(0.3, base_weight))
    
    def update_expiry_status(self):
        if self.valid_until and date.today() > self.valid_until:
            self.is_expired = True
    
    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'author': self.author,
            'source': self.source,
            'content': self.content[:200] if self.content else '',
            'market_direction': self.market_direction,
            'confidence': self.confidence,
            'sectors_bullish': self.sectors_bullish or [],
            'sectors_bearish': self.sectors_bearish or [],
            'reasoning': self.reasoning,
            'time_horizon': self.time_horizon,
            'valid_until': self.valid_until.isoformat() if self.valid_until else None,
            'viewpoint_date': self.viewpoint_date.isoformat() if self.viewpoint_date else None,
            'score': self.score,
            'viewpoint_type': self.viewpoint_type,
            'credibility_score': self.credibility_score,
            'tags': self.tags or [],
            'is_expired': self.is_expired,
            'needs_reassessment': self.needs_reassessment,
            'weight': self.weight,
            'action_suggestion': self.action_suggestion,
            'risk_level': self.risk_level
        }

    __table_args__ = (
        Index('ix_viewpoints_is_deleted', 'is_deleted'),
        Index('ix_viewpoints_viewpoint_date', 'viewpoint_date'),
        Index('ix_viewpoints_blogger_id', 'blogger_id'),
        Index('ix_viewpoints_source', 'source'),
    )


class CrawlerArticleRecord(Base):
    """爬虫文章记录表 - 用于去重"""
    __tablename__ = 'crawler_article_records'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    article_id = Column(String(100), unique=True, nullable=False)
    source = Column(String(50), nullable=False)
    title = Column(String(500))
    content_hash = Column(String(32))
    url = Column(String(500))
    author = Column(String(100))
    
    is_adopted = Column(Boolean, default=False)
    viewpoint_id = Column(Integer)
    
    capture_score = Column(Float, default=0.0)
    skip_reason = Column(String(200))
    
    fetched_at = Column(DateTime, default=datetime.now)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_crawler_records_source', 'source'),
    )


class FundHistory(Base):
    """基金历史净值表"""
    __tablename__ = 'fund_history'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    fund_code = Column(String(20), nullable=False, index=True)
    fund_name = Column(String(100))
    nav_date = Column(Date, nullable=False, index=True)
    nav = Column(Float, nullable=False)
    day_growth = Column(Float)
    data_quality = Column(String(20), default='normal')
    quality_note = Column(String(200))
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint('fund_code', 'nav_date', name='uix_fund_history_code_date'),
        Index('ix_fund_history_code_date', 'fund_code', 'nav_date'),
    )


class FundInfo(Base):
    """
    基金信息表 - 增强版
    
    字段分组：
    - 基本信息：fund_code, fund_name, fund_type, sector_type
    - 净值数据：latest_nav, nav_date, day/week/month_growth
    - 净值区分：estimated_nav, actual_nav, nav_source
    - 基本面数据：fund_scale, establish_date, manager_name, fee_rate
    - 风险指标：sharpe_ratio, max_drawdown, since_inception_return
    - 数据质量：data_quality, data_quality_note
    - 分析日期：last_analyze_date
    - 技术指标：support_level, resistance_level, ma5, ma10, ma20
    - 相对表现：vs_sector, vs_market, performance_type
    - 其他：active_predictions, can_delete, is_core_fund
    """
    __tablename__ = 'fund_info'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    fund_code = Column(String(20), unique=True, nullable=False)
    fund_name = Column(String(100))
    fund_type = Column(String(50))
    sector_type = Column(String(50))
    
    latest_nav = Column(Float)
    nav_date = Column(Date)
    day_growth = Column(Float)
    week_growth = Column(Float)
    month_growth = Column(Float)
    
    estimated_nav = Column(Float)
    estimated_nav_time = Column(DateTime)
    actual_nav = Column(Float)
    actual_nav_time = Column(DateTime)
    nav_source = Column(String(20), default='eastmoney')
    
    fund_scale = Column(Float)
    establish_date = Column(Date)
    manager_name = Column(String(100))
    fee_rate = Column(Float)
    sharpe_ratio = Column(Float)
    max_drawdown = Column(Float)
    since_inception_return = Column(Float)
    
    data_quality = Column(String(20), default='normal')
    data_quality_note = Column(String(200))
    
    last_analyze_date = Column(Date)
    
    support_level = Column(Float)
    resistance_level = Column(Float)
    ma5 = Column(Float)
    ma10 = Column(Float)
    ma20 = Column(Float)
    
    vs_sector = Column(Float)
    vs_market = Column(Float)
    performance_type = Column(String(20))
    
    active_predictions = Column(Integer, default=0)
    can_delete = Column(Boolean, default=True)
    is_core_fund = Column(Boolean, default=False)
    
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class SectorFundMapping(Base):
    """板块-基金映射表 - 自动匹配"""
    __tablename__ = 'sector_fund_mapping'

    id = Column(Integer, primary_key=True, autoincrement=True)
    sector_name = Column(String(50), unique=True, nullable=False)
    fund_code = Column(String(20), nullable=False)
    fund_name = Column(String(100))
    keywords = Column(JSON)
    is_active = Column(Boolean, default=True)
    reviewed = Column(Boolean, default=False)  # 是否经过人工审查
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class SectorAlias(Base):
    """板块别名表 - 用户自定义黑话映射"""
    __tablename__ = 'sector_alias'

    id = Column(Integer, primary_key=True, autoincrement=True)
    alias_name = Column(String(50), unique=True, nullable=False)
    sector_name = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.now)


class InvestmentAdvice(Base):
    """投资建议表"""
    __tablename__ = 'investment_advice'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    advice_date = Column(Date, nullable=False)
    advice_type = Column(String(20))
    advice_content = Column(Text)
    reasoning = Column(Text)
    risk_warning = Column(Text)
    suggested_sectors = Column(JSON)
    avoid_sectors = Column(JSON)
    short_term_advice = Column(JSON)
    mid_term_advice = Column(JSON)
    avoid_reasoning = Column(Text)
    referenced_bloggers = Column(JSON)
    referenced_predictions = Column(JSON)
    market_sentiment = Column(String(20))
    confidence = Column(Integer)
    data_hash = Column(String(32))
    created_at = Column(DateTime, default=datetime.now)


class VerificationTask(Base):
    """验证任务表 - 定时验证"""
    __tablename__ = 'verification_tasks'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    prediction_id = Column(Integer, ForeignKey('predictions.id'), nullable=False)
    task_date = Column(Date, nullable=False)
    status = Column(String(20), default='pending')
    result = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_verification_task_prediction', 'prediction_id'),
    )


class PredictionGroup(Base):
    """
    预测组 - 同一博主对同一基金在相近时间的多个预测组合
    
    功能：
    - 将同一博主对同一基金在相近时间的预测分组
    - 选一个代表预测用于验证
    - 保留所有原始预测，但默认隐藏
    - 保持页面整洁，同时不丢失信息
    
    分组规则：
    - 相同预测周期（1周、1月等）
    - 相邻预测间隔 ≤ 周期天数
    - 每组至少2个预测
    """
    __tablename__ = 'prediction_groups'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    blogger_id = Column(Integer, ForeignKey('bloggers.id'), nullable=False, index=True)
    fund_code = Column(String(20), nullable=False, index=True)
    fund_name = Column(String(100))
    
    # 预测周期（用于分组）
    prediction_period = Column(String(20))  # 1 周、1 个月等
    
    # 预测 ID 列表
    prediction_ids = Column(JSON)  # 所有成员的 ID
    representative_id = Column(Integer)  # 代表预测的 ID（用于验证）
    prediction_count = Column(Integer, default=0)
    
    # 时间范围
    start_date = Column(Date)  # 最早预测日期
    end_date = Column(Date)    # 最晚预测日期
    
    # 合并分析结果
    overall_sentiment = Column(String(20))  # bullish/bearish/neutral
    merged_content = Column(Text)  # 综合分析内容
    consistency_score = Column(Integer)  # 一致性评分 0-100
    
    # 验证结果（跟随代表预测）
    is_verified = Column(Boolean, default=False)
    verify_result = Column(JSON)  # 验证结果
    
    # 状态
    is_active = Column(Boolean, default=True)  # 是否活跃（可解散）
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class BatchAnalysisTask(Base):
    """
    批量分析任务表 - 支持断点续传
    
    功能：
    - 记录批量分析任务的进度
    - 支持中断后继续执行
    - 记录已处理的帖子 ID
    - 记录异常信息
    """
    __tablename__ = 'batch_analysis_tasks'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_type = Column(String(50), nullable=False)  # posts/predictions/viewpoints
    status = Column(String(20), default='pending')  # pending/running/completed/failed/cancelled
    
    # 总数和进度
    total_count = Column(Integer, default=0)
    processed_count = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    
    # 已处理的 ID 列表（JSON 格式）
    processed_ids = Column(JSON)  # [1, 2, 3, ...]
    failed_ids = Column(JSON)  # [{"id": 1, "error": "错误信息"}, ...]
    
    # 异常信息
    error_message = Column(Text)
    error_stack = Column(Text)
    
    # 时间记录
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 任务参数（JSON 格式）
    task_params = Column(JSON)  # {"limit": 100, "blogger_id": 1, ...}
    
    # 结果摘要
    result_summary = Column(JSON)  # {"analyzed": 50, "failed": 2, ...}


class AnalysisLog(Base):
    """
    分析日志表 - 详细记录分析过程
    
    功能：
    - 记录每个帖子的分析过程
    - 便于问题排查
    - 性能分析
    """
    __tablename__ = 'analysis_logs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer)  # 关联批量分析任务
    post_id = Column(Integer)
    
    # 分析过程
    llm_model = Column(String(50))
    prompt_tokens = Column(Integer)
    completion_tokens = Column(Integer)
    total_tokens = Column(Integer)
    
    # LLM 原始返回
    llm_response = Column(Text)
    
    # 解析结果
    parse_success = Column(Boolean)
    parse_method = Column(String(20))  # standard/json5/fixed/manual
    parse_error = Column(Text)
    
    # 基金匹配
    fund_match_level = Column(Integer)  # 1/2/3
    fund_code = Column(String(20))
    fund_name = Column(String(100))
    
    # 时间记录
    analysis_duration = Column(Float)  # 秒
    created_at = Column(DateTime, default=datetime.now)


class UserFundBinding(Base):
    """
    用户自定义板块-基金绑定表
    
    功能：
    - 用户手动绑定板块-基金关系
    - 手动绑定优先级高于AI匹配
    - 记录用户备注和绑定原因
    """
    __tablename__ = 'user_fund_bindings'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    sector = Column(String(50), nullable=False, index=True)
    fund_code = Column(String(20), nullable=False)
    fund_name = Column(String(100))
    user_note = Column(String(200))
    is_primary = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


class SyncLog(Base):
    """
    同步日志表 - 记录基金数据同步过程
    
    功能：
    - 记录同步成功/失败情况
    - 支持失败率告警
    - 便于问题排查
    """
    __tablename__ = 'sync_logs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    sync_type = Column(String(20))  # full/incremental/single
    sync_date = Column(DateTime, default=datetime.now)
    
    total_count = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    
    failed_funds = Column(JSON)  # [{'code': 'xxx', 'reason': '...'}]
    duration_seconds = Column(Float)
    
    status = Column(String(20), default='pending')  # pending/running/completed/failed
    error_message = Column(Text)


class FundHolding(Base):
    """
    基金持仓表 - 记录基金持仓股票
    
    功能：
    - 支持按持仓相似度匹配基金
    - 分析基金投资风格
    """
    __tablename__ = 'fund_holdings'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    fund_code = Column(String(20), nullable=False, index=True)
    stock_code = Column(String(20), nullable=False)
    stock_name = Column(String(100))
    holding_ratio = Column(Float)  # 持仓占比
    holding_shares = Column(Float)  # 持仓股数
    holding_value = Column(Float)  # 持仓市值
    
    report_date = Column(Date)  # 报告日期
    created_at = Column(DateTime, default=datetime.now)


class FundSyncRetry(Base):
    """
    基金同步重试队列表
    
    功能：
    - 记录抓取失败的基金
    - 支持定时重试
    - 避免重复失败
    """
    __tablename__ = 'fund_sync_retry'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    fund_code = Column(String(20), nullable=False, index=True)
    retry_type = Column(String(20))  # nav/info/history
    error_message = Column(Text)
    retry_count = Column(Integer, default=0)
    max_retry = Column(Integer, default=3)
    next_retry_time = Column(DateTime)
    status = Column(String(20), default='pending')  # pending/retrying/success/failed
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MarketEvent(Base):
    """
    市场事件表 - 记录影响市场的重大事件
    
    功能：
    - 触发动态分析
    - 记录政策利好/利空
    - 关联板块影响
    """
    __tablename__ = 'market_events'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_date = Column(DateTime, nullable=False)
    event_type = Column(String(20))  # policy/news/shock/other
    event_level = Column(String(20))  # critical/major/minor
    title = Column(String(200))
    content = Column(Text)
    
    affected_sectors = Column(JSON)  # ['白酒', '新能源']
    affected_funds = Column(JSON)  # ['161725', '516790']
    
    trigger_analysis = Column(Boolean, default=False)
    analysis_triggered_at = Column(DateTime)
    
    source = Column(String(100))
    created_at = Column(DateTime, default=datetime.now)


class CleanupLog(Base):
    """清理日志表"""
    __tablename__ = 'cleanup_logs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    trigger_type = Column(String(20))
    trigger_user = Column(String(100))
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    duration_ms = Column(Integer)
    
    status = Column(String(20))
    total_items = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    skipped_count = Column(Integer, default=0)
    
    details = Column(JSON)
    errors = Column(JSON)
    rules_snapshot = Column(JSON)
    
    created_at = Column(DateTime, default=datetime.now)


class CleanupItemLog(Base):
    """清理明细日志表"""
    __tablename__ = 'cleanup_item_logs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    log_id = Column(Integer, ForeignKey('cleanup_logs.id'))
    
    data_type = Column(String(20))
    data_id = Column(Integer)
    data_title = Column(String(500))
    data_source = Column(String(50))
    
    action = Column(String(20))
    reason = Column(String(200))
    
    original_date = Column(Date)
    deleted_at = Column(DateTime)
    
    can_restore = Column(Boolean, default=True)
    restore_before = Column(Date)
    
    created_at = Column(DateTime, default=datetime.now)


class CleanupTask(Base):
    """清理任务表"""
    __tablename__ = 'cleanup_tasks'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(36), unique=True)
    
    status = Column(String(20), default='pending')
    progress = Column(Integer, default=0)
    current_item = Column(Integer, default=0)
    total_items = Column(Integer, default=0)
    
    cleanup_types = Column(JSON)
    cleanup_params = Column(JSON)
    
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    
    result = Column(JSON)
    error = Column(Text)
    
    created_at = Column(DateTime, default=datetime.now)


class CleanupRule(Base):
    """清理规则配置表"""
    __tablename__ = 'cleanup_rules'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    rule_key = Column(String(100), unique=True)
    data_type = Column(String(50))
    source = Column(String(50), default='all')
    importance = Column(String(20), default='normal')
    
    retention_days = Column(Integer, default=7)
    soft_delete_days = Column(Integer, default=7)
    enabled = Column(Boolean, default=True)
    
    description = Column(String(200))
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class CleanupSchedule(Base):
    """清理调度配置表"""
    __tablename__ = 'cleanup_schedules'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    enabled = Column(Boolean, default=True)
    cron_expression = Column(String(50), default='0 2 * * *')
    cleanup_types = Column(JSON)
    notify_before_minutes = Column(Integer, default=60)
    
    last_run = Column(DateTime)
    next_run = Column(DateTime)
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MarketData(Base):
    """市场数据表 - 指数、北向资金、汇率等宏观数据"""
    __tablename__ = 'market_data'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    data_type = Column(String(50), nullable=False)
    data_date = Column(Date, nullable=False)
    data_time = Column(DateTime)
    
    # 指数数据
    index_code = Column(String(20))
    index_name = Column(String(50))
    current_value = Column(Float)
    change_value = Column(Float)
    change_pct = Column(Float)
    open_value = Column(Float)
    high_value = Column(Float)
    low_value = Column(Float)
    volume = Column(Float)
    amount = Column(Float)
    
    # 北向资金
    north_flow = Column(Float)
    north_buy = Column(Float)
    north_sell = Column(Float)
    
    # 汇率/利率
    rate_value = Column(Float)
    rate_change = Column(Float)
    
    # 原始数据
    raw_data = Column(JSON)
    
    data_source = Column(String(50))
    is_valid = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_market_data_type_date', 'data_type', 'data_date'),
    )


class PolicyData(Base):
    """政策数据表 - 财经新闻、政策公告"""
    __tablename__ = 'policy_data'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    policy_date = Column(Date, nullable=False)
    policy_time = Column(DateTime)
    
    title = Column(String(500))
    content = Column(Text)
    summary = Column(Text)
    
    # 分类
    policy_type = Column(String(50))
    policy_level = Column(String(20))
    affected_sectors = Column(JSON)
    affected_funds = Column(JSON)
    
    # 关键词提取
    keywords = Column(JSON)
    sentiment = Column(String(20))
    importance_score = Column(Integer, default=50)
    
    # 来源
    source = Column(String(100))
    source_url = Column(String(500))
    
    is_processed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_policy_data_date', 'policy_date'),
    )


class SentimentData(Base):
    """情绪数据表 - 股吧、微博、评论区情绪分析"""
    __tablename__ = 'sentiment_data'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    sentiment_date = Column(Date, nullable=False)
    sentiment_time = Column(DateTime)
    
    # 数据来源
    platform = Column(String(50))
    target_type = Column(String(20))
    target_code = Column(String(20))
    target_name = Column(String(100))
    
    # 情绪指标
    bullish_ratio = Column(Float)
    bearish_ratio = Column(Float)
    neutral_ratio = Column(Float)
    
    # 热度指标
    mention_count = Column(Integer)
    discussion_count = Column(Integer)
    heat_score = Column(Integer)
    
    # 关键词
    hot_keywords = Column(JSON)
    
    # 原始数据
    sample_comments = Column(JSON)
    
    data_source = Column(String(50))
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_sentiment_data_date', 'sentiment_date'),
    )


class SectorFundFlow(Base):
    """板块资金流向表"""
    __tablename__ = 'sector_fund_flow'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    flow_date = Column(Date, nullable=False)
    flow_time = Column(DateTime)
    
    sector_name = Column(String(50), nullable=False)
    sector_code = Column(String(20))
    
    # 资金流向
    main_inflow = Column(Float)
    main_outflow = Column(Float)
    main_net_flow = Column(Float)
    
    retail_inflow = Column(Float)
    retail_outflow = Column(Float)
    retail_net_flow = Column(Float)
    
    total_inflow = Column(Float)
    total_outflow = Column(Float)
    total_net_flow = Column(Float)
    
    # 涨跌幅
    sector_change_pct = Column(Float)
    
    # 成交数据
    turnover = Column(Float)
    turnover_ratio = Column(Float)
    
    data_source = Column(String(50))
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_sector_fund_flow_date', 'flow_date'),
    )


class UserProfile(Base):
    """用户画像表"""
    __tablename__ = 'user_profiles'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(50), unique=True)
    
    # 风险偏好
    risk_level = Column(String(20), default='moderate')
    
    # 持仓信息
    holdings = Column(JSON)
    watch_list = Column(JSON)
    
    # 投资偏好
    investment_period = Column(String(20), default='medium')
    preferred_sectors = Column(JSON)
    
    # 经验等级
    experience_level = Column(String(20), default='beginner')
    
    # 定投设置
    auto_invest_enabled = Column(Boolean, default=False)
    auto_invest_funds = Column(JSON)
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class AdviceReasoning(Base):
    """建议决策依据表"""
    __tablename__ = 'advice_reasoning'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    advice_id = Column(Integer, ForeignKey('investment_advice.id'))

    # 核心支撑数据
    supporting_data = Column(JSON)
    
    # 核心风险点
    risk_points = Column(JSON)
    
    # 权重分布
    weight_distribution = Column(JSON)
    
    # 决策链路
    decision_chain = Column(JSON)
    
    # 市场状态
    market_state = Column(String(20))
    
    created_at = Column(DateTime, default=datetime.now)


class AdvicePerformance(Base):
    """建议效果跟踪表"""
    __tablename__ = 'advice_performance'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    advice_id = Column(Integer, ForeignKey('investment_advice.id'))

    # 建议信息
    advice_type = Column(String(20))
    suggested_sectors = Column(JSON)
    advice_date = Column(Date)
    
    # 效果数据
    sector_change_1d = Column(Float)
    sector_change_3d = Column(Float)
    sector_change_7d = Column(Float)
    
    # 判断结果
    is_correct_1d = Column(Boolean)
    is_correct_3d = Column(Boolean)
    is_correct_7d = Column(Boolean)
    
    # 用户反馈
    user_feedback = Column(String(20))
    feedback_detail = Column(Text)
    
    calculated_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)


class SystemConfig(Base):
    """系统配置表 - 持久化存储API配置（解决Render部署配置丢失问题）"""
    __tablename__ = 'system_config'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    config_key = Column(String(100), unique=True, nullable=False, index=True)
    config_value = Column(Text)
    description = Column(String(200))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    created_at = Column(DateTime, default=datetime.now)


class AdviceFeedback(Base):
    """建议反馈表"""
    __tablename__ = 'advice_feedback'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    advice_id = Column(Integer, ForeignKey('investment_advice.id'))
    user_id = Column(String(50))
    
    feedback_type = Column(String(20))
    feedback_score = Column(Integer)
    feedback_detail = Column(Text)
    
    # 跟进调整
    action_taken = Column(String(20))
    result_note = Column(Text)
    
    created_at = Column(DateTime, default=datetime.now)


def init_db():
    """初始化数据库 - 创建所有表（带重试，处理 Supabase SSL 断开）"""
    import time
    max_retries = 3
    for attempt in range(max_retries):
        try:
            Base.metadata.create_all(engine)
            if DB_TYPE == "postgresql":
                logger.info(f"[数据库] 已初始化: PostgreSQL")
            else:
                logger.info(f"[数据库] 已初始化: SQLite: {DB_PATH}")
            return
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(f"[数据库] 初始化失败，重试 {attempt + 1}/{max_retries}: {e}")
                time.sleep(2)
            else:
                logger.error(f"[数据库] 初始化失败: {e}")
                raise


