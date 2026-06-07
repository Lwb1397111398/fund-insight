"""
LLM分析模块 - 增强版
支持：自动生成标题、智能基金识别、投资建议生成、容错处理
增强功能：指数退避重试、熔断机制、结果缓存、结构化日志、性能监控、并发控制
"""
import os
import json
import re
import time
import logging
import hashlib
import threading
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta, date
from dataclasses import dataclass, field
from functools import lru_cache
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor
import asyncio

try:
    import json5
    JSON5_AVAILABLE = True
except ImportError:
    JSON5_AVAILABLE = False

import sys

if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.core.config import config
from src.utils.prediction_utils import PERIOD_MAP, ULTRA_SHORT_PERIODS, parse_period_to_days


logger = logging.getLogger(__name__)

# 可重试的 HTTP 状态码
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass
class CircuitBreakerState:
    enabled: bool = True
    failure_count: int = 0
    last_failure_time: float = 0
    state: str = "closed"
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_failure(self):
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold:
                self.state = "open"
                logger.warning(f"[CircuitBreaker] 熔断器打开，连续失败 {self.failure_count} 次")

    def record_success(self):
        with self._lock:
            self.failure_count = 0
            self.state = "closed"

    def can_execute(self) -> bool:
        with self._lock:
            if not self.enabled:
                return True
            if self.state == "closed":
                return True
            if self.state == "open":
                if time.time() - self.last_failure_time >= self.recovery_timeout:
                    self.state = "half_open"
                    logger.info("[CircuitBreaker] 熔断器进入半开状态，尝试恢复")
                    return True
                return False
            return True


@dataclass
class AnalysisResultCache:
    _cache: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    max_size: int = 1000
    ttl_seconds: int = 3600
    
    def _get_cache_key(self, content: str, analysis_type: str = "default") -> str:
        content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
        return f"{analysis_type}:{content_hash}"
    
    def get(self, content: str, analysis_type: str = "default") -> Optional[Dict]:
        with self._lock:
            key = self._get_cache_key(content, analysis_type)
            if key in self._cache:
                entry = self._cache[key]
                if time.time() - entry['timestamp'] < self.ttl_seconds:
                    logger.debug(f"[Cache] 命中缓存: {key[:20]}...")
                    return entry['result']
                else:
                    del self._cache[key]
        return None
    
    def set(self, content: str, result: Dict, analysis_type: str = "default"):
        with self._lock:
            if len(self._cache) >= self.max_size:
                oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k]['timestamp'])
                del self._cache[oldest_key]
                logger.debug(f"[Cache] 清理最旧缓存条目")
            
            key = self._get_cache_key(content, analysis_type)
            self._cache[key] = {
                'result': result,
                'timestamp': time.time()
            }
            logger.debug(f"[Cache] 缓存结果: {key[:20]}...")
    
    def clear(self):
        with self._lock:
            self._cache.clear()
            logger.info("[Cache] 缓存已清空")


class LLMAnalyzer:
    """LLM分析器 - 增强版（支持多模型、熔断器、缓存、指数退避重试）"""
    
    SYSTEM_PROMPT = """你是一个专业的基金投资分析助手，具备以下能力：
1. 精准识别基金代码、板块名称和投资方向
2. 分析博主观点的信心程度和预测周期
3. 过滤广告、闲聊等无关内容，提取核心投资观点
4. 输出结构化的JSON格式分析结果

分析原则：
- 客观中立，不添加个人判断
- 严格遵循JSON格式要求
- 对模糊表述给予较低信心度
- 板块名称必须填写，不能为空

输出规则（必须严格遵守）：
- 只输出一个JSON对象，不要输出任何其他文字
- 不要用```json```代码块包裹
- 不要输出解释、分析过程、备注
- JSON必须是合法的、可被json.loads()解析的格式"""
    
    BLOGGER_JARGON_GUIDE = """
【基金博主黑话速查表】
以下是博主常用的简称和术语，仅用于理解其在说什么板块，不代表其观点方向。看涨/看跌/观望请根据上下文自行判断。

一、板块简称 → 标准板块名称
- 酒/白酒哥/酒鬼/茅 → 白酒
- 药/毒药/药罐子 → 医药/创新药
- 芯/沙子/泥巴 → 半导体
- 光/光伏狗/太阳能 → 光伏
- 锂/锂王/电池 → 锂电池
- 车/电车 → 新能源车
- 军/军工狗 → 军工
- 银/四大行 → 银行
- 券/券商狗/牛市旗手 → 券商
- 房/地产狗/房子 → 房地产
- 煤/黑金 → 煤炭
- 油/黑油 → 石油
- 金/黄金大妈 → 黄金
- 港/港仔/恒仔 → 恒生科技
- AI → 人工智能

二、个股昵称 → 所属板块
- 茅台/茅大哥 → 白酒
- 宁王/宁德 → 新能源
- 迪王/比亚迪 → 新能源
- 药明/药茅 → 医药
- 中芯 → 半导体
- 隆基 → 光伏
- 招行 → 银行
- 中信 → 券商

三、常见术语（仅释义，不代表观点）
- 上车/建仓/抄底 = 买入；下车/减仓/止盈 = 卖出
- 做T/高抛低吸 = 日内短线；波段 = 中期操作
- 卧倒/躺平 = 不动持有
- 满仓/梭哈 = 全仓买入；轻仓/试仓 = 小仓位
- 诱多 = 假涨引人买入；诱空 = 假跌引人卖出
- 砸盘/跳水 = 大跌；拉升/起飞 = 大涨
- 洗盘 = 短期震荡清洗浮筹
"""

    def _get_jargon_guide(self) -> str:
        """获取黑话指南（硬编码 + 用户自定义别名）"""
        guide = self.BLOGGER_JARGON_GUIDE

        # 追加用户自定义别名
        try:
            from src.models.database import SessionLocal, SectorAlias
            db = SessionLocal()
            try:
                aliases = db.query(SectorAlias).all()
                if aliases:
                    guide += "\n\n六、用户自定义别名\n"
                    for a in aliases:
                        guide += f"- {a.alias_name} → {a.sector_name}板块\n"
            finally:
                db.close()
        except Exception:
            pass

        return guide

    def _get_sector_fund_map(self) -> Dict:
        """获取板块-基金映射（优先数据库，后备硬编码）"""
        from src.constants import SECTOR_FUND_MAP
        try:
            from src.services.sector_fund_service import get_sector_fund_service
            service = get_sector_fund_service()
            db_mappings = service.get_all_mappings()
            if db_mappings:
                return {**SECTOR_FUND_MAP, **db_mappings}
        except Exception as e:
            logger.debug(f"[LLM] 获取数据库板块映射失败: {e}")
        
        return SECTOR_FUND_MAP
    
    def __init__(self):
        self.provider = config.LLM_PROVIDER
        self.api_key = config.LLM_API_KEY
        self.base_url = config.LLM_BASE_URL
        self.model = config.LLM_MODEL
        self.light_model = config.LLM_LIGHT_MODEL
        self.strategy = config.LLM_STRATEGY
        
        self.volcengine_api_key = config.VOLCENGINE_API_KEY
        self.volcengine_base_url = config.VOLCENGINE_BASE_URL
        self.volcengine_model = config.VOLCENGINE_MODEL
        self.volcengine_light_model = config.VOLCENGINE_LIGHT_MODEL
        
        if self.provider == 'volcengine':
            if not self.volcengine_api_key:
                raise ValueError("使用火山引擎时，请设置 VOLCENGINE_API_KEY 环境变量")
            self.client = OpenAI(
                api_key=self.volcengine_api_key,
                base_url=self.volcengine_base_url
            )
            self.model = self.volcengine_model
            self.light_model = self.volcengine_light_model
            logger.info(f"[LLM] 使用火山引擎 (主力模型: {self.model}, 辅助模型: {self.light_model})")
        else:
            if not self.api_key:
                raise ValueError("请设置 LLM_API_KEY 环境变量")
            client_kwargs = {'api_key': self.api_key}
            if self.base_url:
                client_kwargs['base_url'] = self.base_url
            self.client = OpenAI(**client_kwargs)
            logger.info(f"[LLM] 使用 {self.provider} (模型: {self.model})")
        
        self.circuit_breaker = CircuitBreakerState(
            enabled=True,
            failure_threshold=getattr(config, 'CIRCUIT_BREAKER_THRESHOLD', 5),
            recovery_timeout=float(getattr(config, 'CIRCUIT_BREAKER_RECOVERY', 60))
        )
        
        self.result_cache = AnalysisResultCache(
            max_size=getattr(config, 'CACHE_MAX_SIZE', 1000),
            ttl_seconds=getattr(config, 'CACHE_TTL', 3600)
        )
        
        self._call_stats = {
            'total_calls': 0,
            'successful_calls': 0,
            'failed_calls': 0,
            'cache_hits': 0,
            'total_tokens': 0,
            'total_duration': 0.0,
            'total_cost': 0.0,
            'model_usage': {}
        }
        
        self._semaphore = threading.Semaphore(getattr(config, 'LLM_MAX_CONCURRENT', 5))
        
        self._request_queue = []
        self._queue_lock = threading.Lock()
        
        self._downgrade_state = {
            'enabled': True,
            'failure_count': 0,
            'last_failure_time': 0,
            'cooldown_seconds': 300  # 5分钟冷却期
        }
        
        logger.info(f"[LLM] 分析器初始化完成 (模型: {self.model}, 轻量模型: {self.light_model})")
    
    def _select_model(self, task_type: str = 'default', complexity: str = 'medium') -> str:
        """
        根据任务类型和复杂度选择模型
        
        Args:
            task_type: 任务类型
                - 'core': 核心分析任务（帖子分析、投资建议）
                - 'simple': 简单任务（标题生成、验证）
                - 'analysis': 深度分析任务（基金趋势、预测验证）
                - 'extraction': 信息提取任务
                - 'summary': 摘要生成任务
                - 'assistant': 投资助手
                - 'default': 默认
            complexity: 任务复杂度
                - 'high': 复杂任务 → 强制使用主模型
                - 'medium': 中等任务 → 根据策略选择
                - 'low': 简单任务 → 强制使用轻量模型
        
        Returns:
            模型名称
        """
        # 检查是否需要智能降级
        if self._should_downgrade() and task_type != 'core':
            logger.info("[LLM] 智能降级：使用轻量模型")
            return self.light_model
        
        # 根据复杂度强制选择
        if complexity == 'high':
            return self.model
        elif complexity == 'low':
            return self.light_model
        
        # 根据策略选择
        if self.strategy == 'high_quality':
            return self.model
        elif self.strategy == 'light':
            return self.light_model
        else:  # auto
            # 扩展的任务类型映射
            task_model_map = {
                'core': self.model,      # 核心任务用主模型
                'analysis': self.model,  # 深度分析用主模型
                'advice': self.model,    # 投资建议生成用主模型
                'simple': self.light_model,   # 简单任务用轻量模型
                'extraction': self.light_model,  # 信息提取用轻量模型
                'summary': self.light_model,     # 摘要用轻量模型
                'assistant': self.light_model,   # 助手默认用轻量模型（后续根据问题复杂度动态调整）
                'default': self.light_model
            }
            return task_model_map.get(task_type, self.light_model)
    
    def _should_downgrade(self) -> bool:
        """判断是否应该智能降级"""
        if not self._downgrade_state['enabled']:
            return False
        
        import time
        current_time = time.time()
        
        # 如果在冷却期内，不降级
        if current_time - self._downgrade_state['last_failure_time'] > self._downgrade_state['cooldown_seconds']:
            self._downgrade_state['failure_count'] = 0
            return False
        
        # 如果连续失败次数超过阈值，降级
        return self._downgrade_state['failure_count'] >= 3
    
    def _calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """
        计算API调用成本（基于SiliconFlow定价）
        
        Args:
            model: 模型名称
            input_tokens: 输入token数
            output_tokens: 输出token数
        
        Returns:
            成本（元）
        """
        # SiliconFlow 定价（元/千tokens）
        pricing = {
            'deepseek-ai/DeepSeek-V3.2': {'input': 0.001, 'output': 0.002},
            'Qwen/Qwen2.5-7B-Instruct': {'input': 0.00035, 'output': 0.00035},
            'Qwen/Qwen2.5-3B-Instruct': {'input': 0.0001, 'output': 0.0001},
        }
        
        # 默认定价
        default_pricing = {'input': 0.001, 'output': 0.002}
        
        model_pricing = pricing.get(model, default_pricing)
        
        cost = (input_tokens * model_pricing['input'] + output_tokens * model_pricing['output']) / 1000
        
        return cost
    
    def _call_llm(self, prompt: str, task_type: str = 'default', max_tokens: int = 500, temperature: float = 0.7, 
                   use_cache: bool = True, retry_count: int = 3) -> str:
        """
        调用 LLM（支持指数退避重试、熔断器、缓存、并发控制）
        
        Args:
            prompt: 提示词
            task_type: 任务类型（影响模型选择）
            max_tokens: 最大token数
            temperature: 温度
            use_cache: 是否使用缓存
            retry_count: 重试次数
        
        Returns:
            LLM 响应文本
        """
        if use_cache:
            cached = self.result_cache.get(prompt, task_type)
            if cached:
                self._call_stats['cache_hits'] += 1
                logger.debug(f"[LLM] 缓存命中，跳过调用")
                return cached.get('response', '')
        
        if not self.circuit_breaker.can_execute():
            logger.warning("[LLM] 熔断器处于开启状态，拒绝请求")
            raise Exception("熔断器开启，服务暂时不可用")
        
        with self._semaphore:
            return self._call_llm_internal(prompt, task_type, max_tokens, temperature, use_cache, retry_count)
    
    def _call_llm_internal(self, prompt: str, task_type: str = 'default', max_tokens: int = 500, temperature: float = 0.7, 
                            use_cache: bool = True, retry_count: int = 3) -> str:
        """LLM调用内部实现"""
        model = self._select_model(task_type)
        start_time = time.time()
        last_error = None
        
        for attempt in range(retry_count):
            try:
                self._call_stats['total_calls'] += 1
                
                response = self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens
                )
                
                result = response.choices[0].message.content.strip()
                
                self.circuit_breaker.record_success()
                self._call_stats['successful_calls'] += 1
                
                duration = time.time() - start_time
                self._call_stats['total_duration'] += duration
                
                # 记录成本和模型使用统计
                if hasattr(response, 'usage') and response.usage:
                    tokens_used = response.usage.total_tokens
                    self._call_stats['total_tokens'] += tokens_used
                    
                    # 计算成本
                    cost = self._calculate_cost(
                        model, 
                        response.usage.prompt_tokens, 
                        response.usage.completion_tokens
                    )
                    self._call_stats['total_cost'] += cost
                    
                    # 更新模型使用统计
                    if model not in self._call_stats['model_usage']:
                        self._call_stats['model_usage'][model] = {
                            'calls': 0,
                            'tokens': 0,
                            'cost': 0.0
                        }
                    self._call_stats['model_usage'][model]['calls'] += 1
                    self._call_stats['model_usage'][model]['tokens'] += tokens_used
                    self._call_stats['model_usage'][model]['cost'] += cost
                    
                    logger.info(f"[LLM] 调用成功 (模型: {model}, 耗时: {duration:.2f}s, tokens: {tokens_used}, 成本: ¥{cost:.4f})")
                else:
                    logger.info(f"[LLM] 调用成功 (模型: {model}, 耗时: {duration:.2f}s)")
                
                if use_cache:
                    self.result_cache.set(prompt, {'response': result}, task_type)
                
                return result
                
            except Exception as e:
                last_error = e
                self._call_stats['failed_calls'] += 1
                self.circuit_breaker.record_failure()

                # 记录失败用于智能降级
                self._downgrade_state['failure_count'] += 1
                self._downgrade_state['last_failure_time'] = time.time()

                # 检查是否为可重试错误
                status_code = getattr(e, 'status_code', None)
                if status_code is not None and status_code not in RETRYABLE_STATUS_CODES:
                    logger.error(f"[LLM] 不可重试错误 (模型: {model}, status_code={status_code}): {e}")
                    raise e

                if attempt < retry_count - 1:
                    backoff_time = (2 ** attempt) + (0.5 * attempt)
                    logger.warning(f"[LLM] 调用失败 (模型: {model}, 尝试: {attempt + 1}/{retry_count}): {e}, {backoff_time:.1f}秒后重试")
                    time.sleep(backoff_time)
        
        logger.error(f"[LLM] 所有重试失败 (模型: {model}): {last_error}")
        raise last_error
    
    def generate_title(self, content: str, retry_count: int = 2) -> str:
        """根据帖子内容自动生成简短标题（使用轻量级模型）"""
        prompt = f"""请为以下基金博主帖子生成一个简短的标题（15字以内）：

【帖子内容】
{content[:500]}

要求：
1. 标题要概括帖子核心内容
2. 不要使用引号
3. 直接返回标题文字，不要其他内容

示例：
- 看好白酒板块反弹
- 减仓新能源，加仓消费
- 市场震荡，建议观望
"""
        
        for attempt in range(retry_count):
            try:
                result = self._call_llm(prompt, task_type='simple', max_tokens=50, temperature=0.7, use_cache=False)
                
                title = result.strip()
                title = title.replace('"', '').replace('"', '').replace('"', '')
                if len(title) > 30:
                    title = title[:30]
                return title
                
            except Exception as e:
                logger.warning(f"[LLM] 生成标题失败 (尝试 {attempt + 1}/{retry_count}): {e}")
        
        return "基金观点分享"
    
    def analyze_post(self, title: str, content: str, post_date: str = None, retry_count: int = 3,
                     use_cache: bool = True, enable_ai_confirm: bool = True) -> Dict:
        """分析帖子内容，提取预测观点（使用核心模型）
        
        增强功能：
        1. 时间表达式预处理 - 在调用LLM前先提取时间表达式
        2. 基于发布日期计算相对时间
        3. 支持更多周期粒度
        4. 两阶段时间判断：规则预处理 + AI二次确认
        """
        cache_key = f"{title}:{content[:200]}:{post_date}"
        if use_cache:
            cached = self.result_cache.get(cache_key, 'analyze_post')
            if cached:
                logger.info("[LLM] 帖子分析缓存命中")
                return cached
        
        parsed_date = self._parse_post_date(post_date)
        full_text = f"{title} {content}"
        
        # 简化：本地规则快速判断参考周期，直接交给主力LLM分析
        rule_days, rule_period, rule_reason, rule_confidence, rule_expression = \
            self._get_period_with_confidence(full_text, parsed_date)
        
        logger.info(f"[时间判断] 规则参考: {rule_period}, 置信度: {rule_confidence}")
        
        # 直接让主力LLM分析，不再调用辅助LLM
        time_context = self._build_time_context_simplified(
            title, content, parsed_date, rule_period, rule_days, rule_confidence
        )
        
        prompt = f"""你是一个专业的基金投资分析助手。请分析以下基金博主的帖子，提取关键预测信息。

【帖子标题】
{title}

【帖子内容】
{content}

{time_context}

{self._get_jargon_guide()}

【重要：区分"描述"与"预测"】
只提取真正的"预测"，不要提取"描述"！

❌ 以下情况是"描述"，不要提取为预测：
- 描述当天/已经发生的走势："今天在飙升"、"一早就狂飙"、"已经涨了"、"正在拉升"
- 描述当前状态："目前处于低位"、"现在估值偏高"、"当前趋势向上"
- 描述历史表现："昨天大涨"、"上周跌了很多"、"过去一个月表现不错"
- 纯粹的市场回顾："开盘后一路上涨"、"尾盘跳水"、"全天震荡"

✅ 以下情况才是"预测"，应该提取：
- 对未来的判断："明天会涨"、"下周看反弹"、"中线看好"
- 预期走势："预计会继续上涨"、"有望突破"、"可能回调"
- 操作建议："建议逢低买入"、"可以加仓"、"应该减仓"
- 趋势预判："上涨趋势确立"、"下跌趋势未改"、"即将反转"
- 目标价位："看到3000点"、"目标涨幅20%"

请仔细分析并提取以下信息，以JSON格式返回（只返回JSON，不要其他内容）：

1. predictions（预测列表）：博主对未来市场或基金的预测（必须是预测，不能是描述）
   每个预测包含：
   - sector: 板块名称（必须填写，不能为空）- 识别博主在说哪个板块
   - sector_type: 板块类型（如：消费、医药、科技、新能源等）
   - prediction_type: 预测方向（up/down/flat）
   - prediction_content: 预测的具体内容描述（必须是预测，不能是描述）
   （注意：不需要填写fund_code和fund_name，系统会根据sector自动匹配基金）
   - prediction_period: 预测周期，根据该预测的具体时间表述和上下文独立判断。
     【重要】每个预测的时间周期必须独立判断，不同预测可以有不同周期！
     【默认规则】无明确时间表述时，默认1周（不是3个月）

     判断规则（结合上下文）：
     - "今天/今日/马上/立即" + 操作 → 1天（即时操作）
     - "明天/次日" → 1天
     - "后天" → 2天
     - "短期/短线/近期" → 3天
     - "下周" → 1周
     - "中线/中期/波段" → 1个月
     - "下个月" → 1个月
     - "长期/长线/持有" → 6个月
     - "定投+低估/看好" → 6个月（长期）
     - "定投+回调" → 3个月（中期）
     - "继续定投/保持定投" → 3个月
     - **无明确时间表述 → 1周（默认，不是3个月）**
   - confidence: 信心程度 0-100（根据仓位词汇和表述确定）

2. viewpoint（整体观点）
3. summary（总结）

【⚠️ 返回前自检清单 - 必须严格遵守】
1. sector字段是否非空？
2. prediction_type是否仅为up/down/flat之一？
3. **【最重要】prediction_period是否完全独立判断？**
    - ⚠️ 警告：不同预测的时间很可能不同，必须独立判断！
    - ⚠️ 警告：不要复制粘贴相同的周期！
    - ⚠️ 警告：规则参考不重要，请根据实际表述判断！
    - "今天/今日/马上" + 操作 → 1天（即时操作）
    - "明天/次日" → 1天
    - "后天" → 2天
    - "短期/近期" → 3天
    - "下周" → 1周
    - "中线/中期" → 1个月
    - "长期/持有/定投" → 3-6个月
    - **无明确时间 → 1周（默认）**
4. confidence是否根据仓位词汇和时间明确程度确定？
   - 时间明确（明天/短期）→ 高置信度（80-95）
   - 时间较明确（中期/定投）→ 中置信度（70-80）
   - 时间模糊（无明确表述）→ 低置信度（60-70）
5. **prediction_content是否是"预测"而非"描述"？**

【⚠️ 最终检查】
- 如果帖子中有3个预测，它们的时间周期是否完全相同？如果是，请重新独立判断！
- 是否只是简单使用了参考周期？如果是，请根据实际表述重新判断！

返回格式示例（注意：不需要填写fund_code和fund_name，系统自动匹配）：
{{
    "predictions": [
        {{
            "sector": "白酒",
            "sector_type": "消费",
            "prediction_type": "up",
            "prediction_content": "茅茅调整到位，中线看反弹",
            "prediction_period": "1个月",
            "confidence": 75
        }},
        {{
            "sector": "医药",
            "sector_type": "医药",
            "prediction_type": "up",
            "prediction_content": "短期最看好创新药，保持3倍定投",
            "prediction_period": "3天",
            "confidence": 80
        }},
        {{
            "sector": "绿电",
            "sector_type": "新能源",
            "prediction_type": "up",
            "prediction_content": "绿电还在低估，按照纪律继续定投",
            "prediction_period": "3个月",
            "confidence": 70
        }}
    ],
    "viewpoint": {{
        "market_direction": "bullish",
        "confidence": 75,
        "sectors_bullish": ["白酒", "医药", "绿电"],
        "sectors_bearish": [],
        "reasoning": "白酒中线反弹，医药短期看好，绿电长期定投"
    }},
    "summary": "博主看好白酒中线反弹，医药短期机会，绿电长期定投"
}}
"""
        
        for attempt in range(retry_count):
            try:
                result_text = self._call_llm(prompt, task_type='core', max_tokens=2000, temperature=0.3, use_cache=False)
                
                result = self._parse_json_with_fallback(result_text)
                if result:
                    result = self._normalize_prediction_periods(result)
                    # 后处理：根据 sector 自动匹配基金（LLM 不再负责填 fund_code）
                    self._fill_fund_from_sector(result)
                    result['_period_analysis'] = {
                        'rule_period': rule_period,
                        'rule_confidence': rule_confidence
                    }
                    if use_cache:
                        self.result_cache.set(cache_key, result, 'analyze_post')
                    logger.info(f"[LLM] 帖子分析完成，提取到 {len(result.get('predictions', []))} 个预测")
                    return result
                else:
                    logger.warning(f"[LLM] JSON解析失败 (尝试 {attempt + 1}/{retry_count})")
                    
            except Exception as e:
                logger.warning(f"[LLM] 分析失败 (尝试 {attempt + 1}/{retry_count}): {e}")
            
            if attempt < retry_count - 1:
                backoff_time = (2 ** attempt) + (0.5 * attempt)
                logger.info(f"[LLM] {backoff_time:.1f}秒后重试...")
                time.sleep(backoff_time)
        
        logger.error("[LLM] 帖子分析所有重试失败")
        return self._empty_result()
    
    def _parse_post_date(self, post_date) -> 'date':
        """解析帖子发布日期"""
        from datetime import datetime, date
        if not post_date:
            return date.today()
        try:
            if isinstance(post_date, str):
                return datetime.strptime(post_date, '%Y-%m-%d').date()
            elif isinstance(post_date, date):
                return post_date
        except:
            pass
        return date.today()
    
    def _get_period_with_confidence(self, text: str, post_date) -> Tuple[int, str, str, str, str]:
        """获取预测周期（带置信度）"""
        try:
            from src.utils.time_parser import suggest_period_with_confidence
            return suggest_period_with_confidence(text, post_date)
        except Exception as e:
            logger.debug(f"[LLM] 时间解析失败: {e}")
            return 30, "1个月", "默认值", "none", ""
    
    def _build_time_context_simplified(
        self, 
        title: str, 
        content: str, 
        post_date, 
        rule_period: str,
        rule_days: int,
        rule_confidence: str
    ) -> str:
        """构建简化版时间上下文 - 只提供参考，让LLM自行判断每个预测的时间"""
        from datetime import date
        
        if not post_date:
            post_date = date.today()
        
        weekday = self._get_weekday_name(post_date)
        
        return f"""【帖子发布日期】{post_date.strftime('%Y-%m-%d')}（{weekday}）

【时间参考信息】（⚠️ 参考重要性很低，仅作提醒，请忽略规则判断，完全根据帖子内容独立分析）
- 规则参考：{rule_period}（置信度：{rule_confidence}）
- 【注意】规则判断经常不准确，请勿依赖！

【⚠️ 重要提醒】
1. 每个预测的时间周期必须完全独立判断，不同预测的时间很可能不同！
2. 不要所有预测都用相同周期，必须根据每个预测的具体表述独立分析！
3. 规则参考的重要性很低，请主要根据帖子内容的实际表述判断！

【时间判断规则】（请根据每个预测的实际表述慎重判断）：
- "今天/今日/马上" + 操作 → 1天（即时操作）
- "明天/次日" → 1天
- "后天" → 2天
- "短期/近期" → 3天
- "下周" → 1周
- "中线/中期" → 1个月
- "长期/持有/定投" → 3-6个月
- 无明确时间 → 1周（默认）

【自检】返回前请检查：
- 不同预测的时间周期是否可能不同？如果是，请确保它们不同！
- 是否只是简单复制了参考周期？如果是，请重新独立判断！"""
    
    def _build_time_context(self, title: str, content: str, post_date) -> str:
        """
        构建时间上下文信息
        
        Args:
            title: 帖子标题
            content: 帖子内容
            post_date: 帖子发布日期
        
        Returns:
            时间上下文字符串
        """
        from datetime import datetime, date
        
        context_parts = []
        
        if post_date:
            try:
                if isinstance(post_date, str):
                    parsed_date = datetime.strptime(post_date, '%Y-%m-%d').date()
                elif isinstance(post_date, date):
                    parsed_date = post_date
                else:
                    parsed_date = date.today()
            except (ValueError, TypeError) as e:
                logger.debug(f"解析帖子日期失败: {e}")
                parsed_date = date.today()
            
            context_parts.append(f"【帖子发布日期】{parsed_date.strftime('%Y-%m-%d')}（{self._get_weekday_name(parsed_date)}）")
            
            try:
                from src.utils.time_parser import suggest_prediction_period
                
                full_text = f"{title} {content}"
                suggested_days, suggested_period, reason = suggest_prediction_period(full_text, parsed_date)
                
                context_parts.append(f"【时间表达式识别】")
                context_parts.append(f"- 系统建议周期：{suggested_period}（{suggested_days}天）")
                context_parts.append(f"- 识别依据：{reason}")
                
                context_parts.append(f"")
                context_parts.append(f"【时间识别规则】（必须结合发布日期计算）")
                context_parts.append(f"- 发布日期：{parsed_date.strftime('%Y-%m-%d')}")
                context_parts.append(f"- '明天/次日/下个交易日' → 1天")
                context_parts.append(f"- '后天' → 2天")
                context_parts.append(f"- '大后天/近三天' → 3天")
                context_parts.append(f"- '短线/短期/近期' → 3天（默认）")
                context_parts.append(f"- '下周' → 1周（从发布日期起算）")
                context_parts.append(f"- '月底' → 计算到当月最后一天")
                context_parts.append(f"- '中线/中期' → 1个月（默认）")
                context_parts.append(f"- '长线/长期' → 3个月（默认）")
            except Exception as e:
                logger.debug(f"[LLM] 时间表达式预处理失败: {e}")
                context_parts.append(f"【时间识别规则】")
                context_parts.append(f"- '明天/次日' → 1天")
                context_parts.append(f"- '后天' → 2天")
                context_parts.append(f"- '短线/短期' → 3天")
                context_parts.append(f"- '下周' → 1周")
                context_parts.append(f"- '中线/中期' → 1个月")
                context_parts.append(f"- '长线/长期' → 3个月")
        else:
            context_parts.append(f"【时间识别规则】")
            context_parts.append(f"- '明天/次日' → 1天")
            context_parts.append(f"- '后天' → 2天")
            context_parts.append(f"- '短线/短期' → 3天")
            context_parts.append(f"- '下周' → 1周")
            context_parts.append(f"- '中线/中期' → 1个月")
            context_parts.append(f"- '长线/长期' → 3个月")
        
        return "\n".join(context_parts)
    
    def _get_weekday_name(self, d) -> str:
        """获取星期名称"""
        weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
        return weekdays[d.weekday()]
    
    def _normalize_prediction_periods(self, result: Dict) -> Dict:
        """
        标准化预测周期
        
        Args:
            result: LLM分析结果
        
        Returns:
            标准化后的结果
        """
        from src.utils.prediction_utils import normalize_period, parse_period_to_days, days_to_standard_period
        
        predictions = result.get('predictions', [])
        for pred in predictions:
            period = pred.get('prediction_period', '1个月')
            
            if period in ['1天', '2天', '3天', '4天', '5天', '6天', '1周', '2周', '3周', 
                          '1个月', '2个月', '3个月', '6个月', '1年']:
                continue
            
            normalized = normalize_period(period)
            if normalized != period:
                logger.debug(f"[LLM] 周期标准化: {period} -> {normalized}")
                pred['prediction_period'] = normalized
        
        return result
    
    def _parse_json_with_fallback(self, text: str) -> Optional[Dict]:
        """
        容错解析 JSON（多级降级策略）

        Args:
            text: LLM 返回的文本

        Returns:
            解析后的字典，失败返回 None
        """
        # 0. 先清理 markdown 代码块包裹
        cleaned = text.strip()
        if cleaned.startswith('```'):
            # 去掉 ```json ... ``` 包裹
            cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned)
            cleaned = re.sub(r'\n?```\s*$', '', cleaned)
            cleaned = cleaned.strip()

        # 1. 尝试直接解析整个文本
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # 2. 用大括号计数提取完整的顶层 JSON 对象（解决嵌套问题）
        json_str = self._extract_top_level_json(cleaned)
        if json_str:
            try:
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                logger.debug(f"[JSON Parse] 标准JSON解析失败: {e}")

            if JSON5_AVAILABLE:
                try:
                    return json5.loads(json_str)
                except Exception as e:
                    logger.debug(f"[JSON Parse] JSON5解析失败: {e}")

            try:
                fixed_json = self._fix_json_errors(json_str)
                return json.loads(fixed_json)
            except Exception as e:
                logger.debug(f"[JSON Parse] 修复后JSON解析失败: {e}")

        # 3. 兜底：手动提取关键字段
        logger.info("[JSON Parse] 尝试手动提取字段")
        return self._extract_basic_fields(text)

    def _extract_top_level_json(self, text: str) -> Optional[str]:
        """
        用大括号计数提取第一个完整的顶层 JSON 对象。
        解决正则非贪婪匹配嵌套 JSON 时截断的问题。
        """
        start = text.find('{')
        if start == -1:
            return None

        depth = 0
        in_string = False
        escape_next = False

        for i in range(start, len(text)):
            ch = text[i]

            if escape_next:
                escape_next = False
                continue

            if ch == '\\' and in_string:
                escape_next = True
                continue

            if ch == '"' and not escape_next:
                in_string = not in_string
                continue

            if in_string:
                continue

            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]

        # 大括号没闭合，返回从 start 到末尾，让后续修复逻辑处理
        return text[start:]
    
    def _fix_json_errors(self, json_str: str) -> str:
        """
        修复常见的 JSON 错误
        
        Args:
            json_str: JSON 字符串
        
        Returns:
            修复后的 JSON 字符串
        """
        # 去除注释
        json_str = re.sub(r'//.*$', '', json_str, flags=re.MULTILINE)
        json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
        
        # 修复多余的逗号
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        
        # 修复百分号（如 -4.21% -> -4.21）
        json_str = re.sub(r'(-?\d+\.?\d*)%', r'\1', json_str)

        # 去除 markdown 代码块标记
        json_str = re.sub(r'```json\s*', '', json_str)
        json_str = re.sub(r'```\s*', '', json_str)
        
        return json_str
    
    def _extract_basic_fields(self, text: str) -> Optional[Dict]:
        """
        从文本中手动提取关键字段构建基础 JSON
        
        Args:
            text: LLM 返回的文本
        
        Returns:
            基础 JSON 字典
        """
        logger.info("[JSON Parse] 执行手动字段提取")
        
        sector_patterns = [
            r'板块[：:]\s*["\']?([^"\',，\n]+)["\']?',
            r'sector[：:]\s*["\']?([^"\',，\n]+)["\']?',
        ]
        sector = None
        for pattern in sector_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                sector = match.group(1).strip()
                break
        
        prediction_type = None
        if re.search(r'看涨|上涨|up|bullish', text, re.IGNORECASE):
            prediction_type = 'up'
        elif re.search(r'看跌|下跌|down|bearish', text, re.IGNORECASE):
            prediction_type = 'down'
        elif re.search(r'震荡|持平|flat|neutral', text, re.IGNORECASE):
            prediction_type = 'flat'
        
        if sector and prediction_type:
            logger.info(f"[JSON Parse] 手动提取成功: sector={sector}, type={prediction_type}")
            return {
                "predictions": [{
                    "fund_code": self.get_fund_for_sector(sector).get("code", "") if self.get_fund_for_sector(sector) else "",
                    "fund_name": self.get_fund_for_sector(sector).get("name", sector) if self.get_fund_for_sector(sector) else sector,
                    "sector": sector,
                    "sector_type": "其他",
                    "prediction_type": prediction_type,
                    "prediction_content": f"博主对{sector}板块的观点",
                    "prediction_period": "1个月",
                    "confidence": 50
                }],
                "viewpoint": {
                    "market_direction": "neutral",
                    "confidence": 50,
                    "sectors_bullish": [sector] if prediction_type == 'up' else [],
                    "sectors_bearish": [sector] if prediction_type == 'down' else [],
                    "reasoning": "手动提取"
                },
                "summary": f"博主对{sector}板块持{'看涨' if prediction_type == 'up' else '看跌' if prediction_type == 'down' else '中性'}观点"
            }
        
        logger.warning("[JSON Parse] 手动提取失败，无法识别关键信息")
        return None
    
    def _empty_result(self) -> Dict:
        """返回空结果"""
        return {
            "predictions": [],
            "viewpoint": {
                "market_direction": "neutral",
                "confidence": 0,
                "sectors_bullish": [],
                "sectors_bearish": [],
                "reasoning": ""
            },
            "summary": "分析失败"
        }
    
    def get_fund_for_sector(self, sector: str) -> Optional[Dict]:
        """根据板块名称获取对应基金"""
        sector = sector.strip()

        sector_fund_map = self._get_sector_fund_map()
        for key, fund_info in sector_fund_map.items():
            if key in sector or sector in key:
                return fund_info

        return None

    def _fill_fund_from_sector(self, result: Dict):
        """根据 sector 自动匹配基金代码和名称（多层匹配 + 自动学习）

        优先级：已审查DB > 硬编码表 > 未审查DB > FundInfo > API搜索
        """
        from src.constants.sector_fund_map import get_fund_for_sector, normalize_sector_name

        for pred in result.get('predictions', []):
            sector = pred.get('sector', '')
            if not sector:
                continue

            # 标准化板块名
            standard_sector = normalize_sector_name(sector)
            if standard_sector != sector:
                pred['sector'] = standard_sector
                logger.info(f"[基金匹配] 板块标准化: '{sector}' → '{standard_sector}'")

            # 第1层：已审查的数据库映射（用户编辑/审查过的，最高优先级）
            fund = self._find_fund_in_db_mapping(standard_sector, reviewed_only=True)
            if fund:
                pred['fund_code'] = fund.get('code', '')
                pred['fund_name'] = fund.get('name', '')
                logger.info(f"[基金匹配] 已审查DB命中: {standard_sector} → {fund.get('name', '')}")
                continue

            # 第2层：硬编码表（136个验证过的板块）
            fund = get_fund_for_sector(standard_sector)
            if fund:
                pred['fund_code'] = fund.get('code', '')
                pred['fund_name'] = fund.get('name', '')
                logger.info(f"[基金匹配] 硬编码表命中: {standard_sector} → {fund.get('name', '')}")
                continue

            # 第3层：未审查的数据库映射（自动学习的，待用户确认）
            fund = self._find_fund_in_db_mapping(standard_sector, reviewed_only=False)
            if fund:
                pred['fund_code'] = fund.get('code', '')
                pred['fund_name'] = fund.get('name', '')
                logger.info(f"[基金匹配] 未审查DB命中: {standard_sector} → {fund.get('name', '')}")
                continue

            # 第4层：FundInfo 表（命中后自动保存到待审查队列）
            fund = self._find_fund_in_fundinfo(standard_sector)
            if fund:
                pred['fund_code'] = fund.get('code', '')
                pred['fund_name'] = fund.get('name', '')
                logger.info(f"[基金匹配] FundInfo命中: {standard_sector} → {fund.get('name', '')}")
                continue

            # 第5层：天天基金 API 搜索（命中后自动保存到待审查队列）
            fund = self._search_fund_via_api(standard_sector)
            if fund:
                pred['fund_code'] = fund.get('code', '')
                pred['fund_name'] = fund.get('name', '')
                self._save_fund_mapping(standard_sector, fund.get('code', ''), fund.get('name', ''), reviewed=False)
                logger.info(f"[基金匹配] API搜索命中: {standard_sector} → {fund.get('name', '')}，已自动保存到待审查")
                continue

            # 第6层：都没找到，留空
            logger.warning(f"[基金匹配] 板块 '{standard_sector}' 无对应基金，留空")

    def _find_fund_in_db_mapping(self, sector: str, reviewed_only: bool = False) -> Optional[Dict]:
        """查数据库 SectorFundMapping（reviewed_only=True 只返回已审查的映射）"""
        try:
            from src.services.sector_fund_service import get_sector_fund_service
            service = get_sector_fund_service()
            fund = service.get_fund_by_sector(sector)
            if fund and reviewed_only and not fund.get('reviewed'):
                return None
            return fund
        except Exception:
            return None

    def _find_fund_in_fundinfo(self, sector: str) -> Optional[Dict]:
        """第4层：查 FundInfo 表，按 sector_type 模糊匹配（命中后自动保存到待审查队列）"""
        try:
            from src.models.database import SessionLocal, FundInfo
            db = SessionLocal()
            try:
                # 精确匹配
                fund = db.query(FundInfo).filter(
                    FundInfo.sector_type == sector
                ).first()
                if fund:
                    result = {'code': fund.fund_code, 'name': fund.fund_name}
                    self._save_fund_mapping(sector, result['code'], result['name'], reviewed=False, db=db)
                    return result

                # 模糊匹配
                fund = db.query(FundInfo).filter(
                    FundInfo.sector_type.contains(sector)
                ).first()
                if fund:
                    result = {'code': fund.fund_code, 'name': fund.fund_name}
                    self._save_fund_mapping(sector, result['code'], result['name'], reviewed=False, db=db)
                    return result

                # 反向匹配（sector 包含 sector_type）
                funds = db.query(FundInfo).filter(
                    FundInfo.sector_type != None
                ).all()
                for f in funds:
                    if f.sector_type and (f.sector_type in sector or sector in f.sector_type):
                        result = {'code': f.fund_code, 'name': f.fund_name}
                        self._save_fund_mapping(sector, result['code'], result['name'], reviewed=False, db=db)
                        return result
            finally:
                db.close()
        except Exception:
            pass
        return None

    def _search_fund_via_api(self, sector: str) -> Optional[Dict]:
        """第4层：调天天基金 API 搜索"""
        try:
            from src.fund.fund_api import FundAPI
            api = FundAPI()
            results = api.search_fund(sector)
            if results:
                # 取第一个结果
                r = results[0]
                return {'code': r.get('fund_code', ''), 'name': r.get('fund_name', '')}
        except Exception as e:
            logger.warning(f"[基金匹配] API搜索失败: {e}")
        return None

    def _save_fund_mapping(self, sector: str, fund_code: str, fund_name: str, reviewed: bool = False, db=None):
        """自动保存映射到数据库（reviewed=False 表示待审查）"""
        try:
            from src.models.database import SessionLocal, SectorFundMapping
            close_db = db is None
            if db is None:
                db = SessionLocal()
            try:
                mapping = SectorFundMapping(
                    sector_name=sector,
                    fund_code=fund_code,
                    fund_name=fund_name,
                    reviewed=reviewed
                )
                db.merge(mapping)
                db.commit()
                logger.info(f"[基金匹配] 自动保存映射: {sector} → {fund_name} ({fund_code}) [reviewed={reviewed}]")
            except Exception as e:
                db.rollback()
                raise
            finally:
                if close_db:
                    db.close()
        except Exception as e:
            logger.warning(f"[基金匹配] 保存映射失败: {e}")
    
    def calculate_target_date(self, prediction_date: date, period: str) -> date:
        """根据预测周期计算目标验证日期"""
        days = PERIOD_MAP.get(period, 30)
        return prediction_date + timedelta(days=days)
    
    def calculate_next_verify_date(self, prediction_date: date, target_date: date) -> date:
        """计算下次验证日期（每5天验证一次）"""
        today = date.today()
        days_since_prediction = (today - prediction_date).days
        
        verify_interval = 5
        next_verify_days = ((days_since_prediction // verify_interval) + 1) * verify_interval
        
        next_date = prediction_date + timedelta(days=next_verify_days)
        
        if next_date > target_date:
            return target_date
        
        return next_date
    
    def verify_prediction(self, prediction_content: str, actual_change: float,
                          prediction_type: str, confidence: int,
                          verify_count: int = 0, flat_threshold: float = 1.0, relative_performance: Dict = None,
                          blogger_context: Dict = None, is_ultra_short: bool = False,
                          direction_only: bool = False, process_metrics: Dict = None,
                          trend_description: str = None) -> Dict:
        """智能验证预测结果（使用轻量级模型）
        
        Args:
            prediction_content: 预测内容
            actual_change: 实际涨跌幅
            prediction_type: 预测类型（up/down/flat）
            confidence: 预测信心度
            verify_count: 验证次数
            flat_threshold: 震荡判断阈值（动态，根据预测周期调整）
            relative_performance: 相对表现数据（目标基金 vs 板块 vs 大盘）
            blogger_context: 博主上下文（历史准确率、评级等）
            is_ultra_short: 是否为超短期预测（1-3天）
            direction_only: 是否只看方向不看百分比
            process_metrics: 过程指标（峰值、时间占比等）
            trend_description: 趋势描述（本地计算的阶段分析）
        """
        if is_ultra_short and direction_only:
            is_correct = False
            if prediction_type == 'up' and actual_change > 0:
                is_correct = True
            elif prediction_type == 'down' and actual_change < 0:
                is_correct = True
            elif prediction_type == 'flat' and actual_change == 0:
                is_correct = True
            
            return {
                "is_correct": is_correct,
                "is_expired": True,
                "analysis": f"超短期预测验证：实际涨跌{actual_change:+.2f}%，方向{'正确' if is_correct else '错误'}",
                "score": 100 if is_correct else 0,
                "trend": "up" if actual_change > 0 else "down" if actual_change < 0 else "flat",
                "prediction_status": "success" if is_correct else "failed"
            }
        
        relative_context = ""
        if relative_performance:
            sector_change = relative_performance.get('sector_change')
            market_change = relative_performance.get('market_change')
            vs_sector = relative_performance.get('vs_sector')
            vs_market = relative_performance.get('vs_market')
            
            relative_context = f"""

【相对表现对比】
- 目标基金涨跌：{actual_change:+.2f}%
- 板块指数涨跌：{sector_change:+.2f}% {'（数据缺失）' if sector_change is None else ''}
- 大盘指数涨跌：{market_change:+.2f}% {'（数据缺失）' if market_change is None else ''}
- 跑赢板块：{vs_sector:+.2f}% {'（数据缺失）' if vs_sector is None else ''}
- 跑赢大盘：{vs_market:+.2f}% {'（数据缺失）' if vs_market is None else ''}"""
        
        blogger_info_context = ""
        if blogger_context:
            blogger_info_context = f"""

【博主历史表现】
- 博主名称：{blogger_context.get('name', '未知')}
- 博主评级：{blogger_context.get('grade', 'C')}级
- 历史准确率：{blogger_context.get('accuracy_rate', 0):.1f}%
- 历史预测数：{blogger_context.get('total_predictions', 0)}次
- 正确预测数：{blogger_context.get('correct_predictions', 0)}次"""

        process_context = ""
        if process_metrics:
            max_change = process_metrics.get('max_change', 0)
            min_change = process_metrics.get('min_change', 0)
            peak_hit_ratio = process_metrics.get('peak_hit_ratio', 0)
            peak_hit_days = process_metrics.get('peak_hit_days', 0)
            total_days = process_metrics.get('total_days', 0)
            
            process_context = f"""

【过程指标】
- 最大涨幅: {max_change:+.2f}%
- 最大跌幅: {min_change:+.2f}%
- 方向正确: {peak_hit_days}/{total_days}天({int(peak_hit_ratio*100)}%)"""

        trend_context = ""
        if trend_description:
            trend_context = f"""

【趋势分析】
{trend_description}"""

        prompt = f"""验证预测（边界情况，需综合判断）：

预测: {prediction_type} ({'涨' if prediction_type == 'up' else '跌' if prediction_type == 'down' else '震荡'})
最终: {actual_change:+.2f}%{process_context}{trend_context}

返回JSON:
{{"is_correct":true/false,"score":0-100,"analysis":"简析(20字)"}}

规则:
- 最终方向对=100分
- 过程≥50%时间方向对=60-90分
- 有同向阶段=40-60分
- 方向全错=0分"""

        try:
            result_text = self._call_llm(prompt, task_type='simple', max_tokens=150, temperature=0.3)

            result = self._parse_json_with_fallback(result_text)
            if result:
                if 'score' in result:
                    result['score'] = min(100, max(0, int(result['score'])))
                return result
        except Exception as e:
            logger.warning(f"[LLM] 验证分析失败: {e}")
        
        # 降级：使用动态阈值判断
        is_correct = False
        if prediction_type == 'up' and actual_change > 0:
            is_correct = True
        elif prediction_type == 'down' and actual_change < 0:
            is_correct = True
        elif prediction_type == 'flat' and abs(actual_change) < flat_threshold:
            is_correct = True
        
        return {
            "is_correct": is_correct,
            "is_expired": False,
            "analysis": f"实际涨跌{actual_change:+.2f}%（震荡阈值{flat_threshold}%）",
            "score": 100 if is_correct else 0,
            "trend": "up" if actual_change > 0 else "down" if actual_change < 0 else "flat",
            "prediction_status": "ongoing"
        }
    
    def evaluate_prediction(self, prediction_content: str, prediction_type: str, confidence: int,
                           actual_change: float, is_correct: bool, sector: str = "",
                           blogger_name: str = "", prediction_period: str = "1个月") -> Dict:
        """LLM评价已验证的预测结果"""
        prompt = f"""请对以下已验证的基金预测进行专业评价：

【博主】{blogger_name}
【板块】{sector}
【预测周期】{prediction_period}

【预测内容】
{prediction_content}

【预测方向】
{prediction_type} ({'看涨' if prediction_type == 'up' else '看跌' if prediction_type == 'down' else '震荡'})

【预测信心】
{confidence}%

【实际结果】
涨跌幅: {actual_change:+.2f}%
验证结果: {'✅ 预测正确' if is_correct else '❌ 预测错误'}

请从以下角度进行专业评价：
1. 预测逻辑是否合理
2. 信心度是否恰当
3. 预测周期选择是否合适
4. 对投资者的参考价值

请返回JSON格式评价：
{{
    "rating": "优秀/良好/一般/较差",
    "score": 1-100,
    "logic_analysis": "预测逻辑分析（50字以内）",
    "confidence_comment": "信心度评价（30字以内）",
    "period_comment": "周期选择评价（30字以内）",
    "investment_value": "投资参考价值评价（30字以内）",
    "suggestion": "给博主的建议（50字以内）",
    "summary": "一句话总结（30字以内）"
}}
"""
        
        try:
            result_text = self._call_llm(prompt, task_type='simple', max_tokens=500, temperature=0.3)
            result = self._parse_json_with_fallback(result_text)
            if result:
                return result
        except Exception as e:
            logger.warning(f"[LLM] 预测评价失败: {e}")
        
        if is_correct:
            return {
                "rating": "良好",
                "score": 75,
                "logic_analysis": "预测方向与实际走势一致",
                "confidence_comment": "信心度合理",
                "period_comment": "周期选择适中",
                "investment_value": "具有一定参考价值",
                "suggestion": "继续保持分析质量",
                "summary": f"预测正确，实际{actual_change:+.2f}%"
            }
        else:
            return {
                "rating": "一般",
                "score": 40,
                "logic_analysis": "预测方向与实际走势相反",
                "confidence_comment": "信心度需要调整",
                "period_comment": "可考虑调整预测周期",
                "investment_value": "参考价值有限",
                "suggestion": "加强市场分析，提高预测准确率",
                "summary": f"预测错误，实际{actual_change:+.2f}%"
            }
    
    def generate_investment_advice(self, bloggers: List[Dict], predictions: List[Dict], 
                                    fund_trends: Dict = None, viewpoints: List[Dict] = None) -> Dict:
        """生成投资建议（纳入观点数据）"""
        blogger_info = []
        for b in bloggers[:10]:
            blogger_info.append({
                "name": b.get('name', ''),
                "accuracy_rate": b.get('accuracy_rate', 0),
                "grade": b.get('grade', 'C'),
                "recent_view": b.get('recent_view', '')
            })
        
        prediction_info = []
        for p in predictions[:20]:
            prediction_info.append({
                "blogger": p.get('blogger_name', ''),
                "sector": p.get('sector', ''),
                "prediction_type": p.get('prediction_type', ''),
                "confidence": p.get('confidence', 50),
                "status": p.get('status', 'pending')
            })
        
        # 构建观点信息（权重较低，作为辅助参考）
        viewpoint_info = []
        if viewpoints:
            for v in viewpoints[:15]:  # 最多取15个观点
                viewpoint_info.append({
                    "source": v.get('source', ''),
                    "author": v.get('author', ''),
                    "direction": v.get('market_direction', 'neutral'),
                    "confidence": v.get('confidence', 50),
                    "sectors_bullish": v.get('sectors_bullish', []),
                    "sectors_bearish": v.get('sectors_bearish', []),
                    "summary": v.get('summary', ''),
                    "is_summary": v.get('is_summary', False)
                })
        
        fund_context = ""
        if fund_trends:
            fund_context = f"\n\n基金趋势数据：\n{json.dumps(fund_trends, ensure_ascii=False, indent=2)}"
        
        viewpoint_context = ""
        if viewpoint_info:
            viewpoint_context = f"""

【市场观点】（来自抓取的文章，权重较低，作为辅助参考）
{json.dumps(viewpoint_info, ensure_ascii=False, indent=2)}

观点说明：
- 以上观点来自各大财经平台抓取的文章分析
- is_summary=true 表示汇总观点（当天多观点的精华总结，权重较高）
- is_summary=false 表示原始观点
- 这些观点作为辅助参考，权重低于博主预测
- 重点关注观点中的板块倾向和市场情绪"""
        
        prompt = f"""你是一个专业的基金投资顾问。请根据以下数据，给出今天的投资建议。

【博主信息】（按准确率排序，权重最高）
{json.dumps(blogger_info, ensure_ascii=False, indent=2)}

【活跃预测】（博主发布的预测，权重高）
{json.dumps(prediction_info, ensure_ascii=False, indent=2)}
{viewpoint_context}
{fund_context}

请综合分析并给出投资建议，返回JSON格式：
{{
    "advice_type": "buy/sell/hold/watch",  // 建议：买入/卖出/持有/观望
    "advice_content": "具体建议内容（100字以内）",
    "market_sentiment": "greedy/fearful/neutral",  // 市场情绪：贪婪/恐惧/中性
    "confidence": 0-100,
    "suggested_sectors": ["板块1", "板块2"],  // 建议关注的板块
    "avoid_sectors": ["板块1"],  // 建议回避的板块
    "reasoning": "建议理由（150字以内）",
    "risk_warning": "风险提示（50字以内）"
}}

分析要点：
1. 优先参考高准确率博主的观点（权重最高）
2. 综合多个博主的一致观点（权重高）
3. 考虑抓取的市场观点作为辅助参考（权重较低）
4. 分析观点中的板块倾向，辅助判断热门板块
5. 考虑市场情绪和风险
6. 给出明确的操作建议

权重说明：
- 博主准确率 > 博主预测 > 抓取观点
- 抓取观点主要用于辅助验证和板块参考
"""
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一个专业的基金投资顾问。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.5,
                max_tokens=1000
            )
            
            result_text = response.choices[0].message.content.strip()
            result = self._parse_json_with_fallback(result_text)
            if result:
                return result
        except Exception as e:
            logger.warning(f"[LLM] 生成投资建议失败: {e}")
        
        return {
            "advice_type": "hold",
            "advice_content": "建议观望，等待更明确的市场信号",
            "market_sentiment": "neutral",
            "confidence": 50,
            "short_term": {
                "strategy": "wait",
                "watch_sectors": [],
                "action": "观望",
                "reasoning": "市场信号不明确",
                "risk_level": "medium",
                "valid_days": 3
            },
            "mid_term": {
                "strategy": "wait",
                "buy_sectors": [],
                "action": "观望",
                "reasoning": "等待更明确的趋势",
                "risk_level": "medium",
                "valid_days": 7
            },
            "avoid_sectors": [],
            "avoid_reasoning": "",
            "risk_warning": "投资有风险，入市需谨慎"
        }
    
    def analyze_viewpoints_stage1(self, viewpoints: List[Dict]) -> Dict:
        """第一阶段：分析观点，生成观点摘要"""
        if not viewpoints:
            return {
                "summary": "暂无市场观点",
                "market_sentiment": "neutral",
                "hot_sectors": [],
                "risk_sectors": [],
                "key_points": []
            }
        
        viewpoint_data = []
        for v in viewpoints:
            credibility = v.get('credibility_score', 50)
            weight = v.get('weight', 1.0)
            effective_score = int(credibility * weight)
            viewpoint_data.append({
                "d": v.get('market_direction', 'neutral')[0].upper(),
                "c": v.get('confidence', 50),
                "s": effective_score,
                "up": v.get('sectors_bullish', [])[:3],
                "dn": v.get('sectors_bearish', [])[:3],
                "txt": v.get('summary', '')[:60]
            })
        
        prompt = f"""分析市场观点(7天内{len(viewpoint_data)}条):
{json.dumps(viewpoint_data, ensure_ascii=False)}

字段:d=方向(U涨/D跌/N震荡),c=信心,s=综合分,up=看涨板块,dn=看跌板块,txt=摘要
规则:s>=80高权重,s<40低权重

返回JSON:
{{"summary":"整体摘要(80字)","market_sentiment":"bullish/bearish/neutral","hot_sectors":["板块"],"risk_sectors":["板块"],"key_points":["观点1","观点2","观点3"],"confidence_avg":0-100}}"""
        
        try:
            result_text = self._call_llm(prompt, task_type='analysis', max_tokens=800, temperature=0.3)
            result = self._parse_json_with_fallback(result_text)
            if result:
                return result
        except Exception as e:
            logger.warning(f"[LLM] 观点分析失败: {e}")
        
        return {
            "summary": "观点分析失败",
            "market_sentiment": "neutral",
            "hot_sectors": [],
            "risk_sectors": [],
            "key_points": [],
            "confidence_avg": 50
        }
    
    def analyze_predictions_stage2(self, predictions: List[Dict], viewpoint_summary: Dict, bloggers: List[Dict] = None) -> Dict:
        """第二阶段：分析预测，结合观点摘要生成预测分析报告"""
        if not predictions:
            return {
                "summary": "暂无有效预测",
                "near_term_trend": "neutral",
                "mid_term_trend": "neutral",
                "high_confidence_predictions": [],
                "sector_predictions": {},
                "blogger_accuracy": {}
            }
        
        blogger_info_map = {}
        if bloggers:
            for b in bloggers:
                name = b.get('name', '')
                if name:
                    blogger_info_map[name] = {
                        'accuracy_rate': b.get('accuracy_rate', 0),
                        'grade': b.get('grade', 'C')
                    }
        
        near_count = sum(1 for p in predictions if p.get('term') == 'near')
        mid_count = len(predictions) - near_count
        flat_count = sum(1 for p in predictions if p.get('prediction_type') == 'flat')

        prediction_data = []
        for p in predictions[:30]:
            blogger_name = p.get('blogger_name', '')
            blogger_info = blogger_info_map.get(blogger_name, {})
            grade = blogger_info.get('grade', 'C')
            pred_type = p.get('prediction_type', '')
            content = p.get('prediction_content', '')
            entry = {
                "b": blogger_name[:8],
                "g": grade,
                "s": p.get('sector', ''),
                "t": pred_type[0].upper() if pred_type else 'N',
                "c": p.get('confidence', 50),
                "d": p.get('days_to_target', 0),
                "tm": p.get('term', 'mid')[0]
            }
            if content:
                entry["txt"] = content[:80]
            prediction_data.append(entry)

        prompt = f"""分析预测数据(共{len(prediction_data)}条):
近期(7天内):{near_count}条,中期:{mid_count}条,观望:{flat_count}条

【观点摘要】
{json.dumps(viewpoint_summary, ensure_ascii=False)}

【预测数据】
{json.dumps(prediction_data, ensure_ascii=False)}

字段说明:
- b=博主, g=等级(A-D), s=板块, c=信心, d=剩余天数, tm=周期(n近/m中)
- t=方向: U=看涨(博主明确看好), D=看跌(博主明确看空), N=观望(博主未下判断)
- txt=博主原话摘要

【重要】N(观望/中性)的含义:
- N代表博主认为"现在不是下手的好时机"，需要再观望
- N不是方向判断，不构成看涨或看跌信号
- 统计板块趋势时，N不应计入bullish或bearish
- N仅代表市场存在犹豫情绪，可作为风险参考

规则:
- A级博主权重最高,D级权重最低
- 高置信度预测(high_confidence_predictions)只包含U和D，不包含N
- 板块预测统计时，N不计入方向计数

返回JSON:
{{"summary":"预测摘要(80字)","near_term_trend":"bullish/bearish/neutral","mid_term_trend":"bullish/bearish/neutral","high_confidence_predictions":[{{"b":"博主","s":"板块","t":"U/D","c":80}}],"sector_predictions":{{"板块":{{"bullish":3,"bearish":1,"neutral":2}}}},"key_insights":["洞察1","洞察2"]}}"""
        
        try:
            result_text = self._call_llm(prompt, task_type='analysis', max_tokens=1000, temperature=0.3)
            result = self._parse_json_with_fallback(result_text)
            if result:
                return result
        except Exception as e:
            logger.warning(f"[LLM] 预测分析失败: {e}")
        
        return {
            "summary": "预测分析失败",
            "near_term_trend": "neutral",
            "mid_term_trend": "neutral",
            "high_confidence_predictions": [],
            "sector_predictions": {},
            "blogger_accuracy": {},
            "key_insights": []
        }
    
    def generate_advice_stage3(self, viewpoint_summary: Dict, prediction_analysis: Dict, 
                                bloggers: List[Dict]) -> Dict:
        """第三阶段：基于观点摘要和预测分析，生成投资建议（短期/中期分层）"""
        blogger_info = []
        for b in bloggers[:10]:
            blogger_info.append({
                "name": b.get('name', ''),
                "accuracy_rate": b.get('accuracy_rate', 0),
                "grade": b.get('grade', 'C')
            })
        
        near_trend = prediction_analysis.get('near_term_trend', 'neutral')
        mid_trend = prediction_analysis.get('mid_term_trend', 'neutral')
        sector_preds = prediction_analysis.get('sector_predictions', {})
        
        prompt = f"""你是一个专业的基金投资顾问。请基于以下分析报告，给出分层投资建议。

【观点摘要】
{json.dumps(viewpoint_summary, ensure_ascii=False, indent=2)}

【预测分析】
{json.dumps(prediction_analysis, ensure_ascii=False, indent=2)}

【博主信息】（按准确率排序）
{json.dumps(blogger_info, ensure_ascii=False, indent=2)}

【关键趋势参考】
- 近期趋势（7天内）: {near_trend}
- 中期趋势（1个月内）: {mid_trend}
- 板块预测汇总: {json.dumps(sector_preds, ensure_ascii=False)}

请综合分析并返回JSON格式：
{{
    "advice_type": "buy/sell/hold/watch",
    "advice_content": "整体建议摘要（80字以内）",
    "market_sentiment": "greedy/fearful/neutral",
    "confidence": 0-100,
    "reasoning": "详细的分析理由（150字以内，说明为什么给出这个建议）",
    "suggested_sectors": ["板块1", "板块2"],
    "short_term": {{
        "strategy": "momentum/watch/wait",
        "watch_sectors": ["板块1", "板块2"],
        "action": "追涨/观望/减仓",
        "reasoning": "短期理由（50字以内）",
        "risk_level": "high/medium/low",
        "valid_days": 3
    }},
    "mid_term": {{
        "strategy": "position/reduce/wait",
        "buy_sectors": ["板块1", "板块2"],
        "action": "布局/减仓/观望",
        "reasoning": "中期理由（50字以内）",
        "risk_level": "high/medium/low",
        "valid_days": 7
    }},
    "avoid_sectors": ["板块1"],
    "avoid_reasoning": "回避理由（30字以内）",
    "risk_warning": "风险提示（50字以内）"
}}

分析要点：
1. 短期建议（1-3天）：基于观点的热度和近期趋势（{near_trend}），适合动量策略或观望
2. 中期建议（1-2周）：基于预测的方向（{mid_trend}）和博主准确率，适合布局策略
3. 短期和中期可以不同：比如短期观望等回调，中期逐步布局
4. 明确区分"追涨"（短期动量）和"布局"（中期价值）
5. 给出具体的有效天数
6. reasoning字段要详细说明分析逻辑和依据
7. 参考板块预测汇总中的数据，看涨数量多的板块更适合关注
8. suggested_sectors字段填写综合短期和中期最值得关注的板块（最多3个）
"""
        
        try:
            result_text = self._call_llm(prompt, task_type='advice', max_tokens=1000, temperature=0.5)
            result = self._parse_json_with_fallback(result_text)
            if result:
                return result
        except Exception as e:
            logger.warning(f"[LLM] 投资建议生成失败: {e}")
        
        return {
            "advice_type": "hold",
            "advice_content": "建议观望，等待更明确的市场信号",
            "market_sentiment": "neutral",
            "confidence": 50,
            "reasoning": "当前市场信号不明确，建议保持观望态度",
            "suggested_sectors": [],
            "short_term": {
                "strategy": "wait",
                "watch_sectors": [],
                "action": "观望",
                "reasoning": "市场信号不明确",
                "risk_level": "medium",
                "valid_days": 3
            },
            "mid_term": {
                "strategy": "wait",
                "buy_sectors": [],
                "action": "观望",
                "reasoning": "等待更明确的趋势",
                "risk_level": "medium",
                "valid_days": 7
            },
            "avoid_sectors": [],
            "avoid_reasoning": "",
            "risk_warning": "投资有风险，入市需谨慎"
        }
    
    def generate_investment_advice_three_stage(self, bloggers: List[Dict], predictions: List[Dict],
                                                viewpoints: List[Dict]) -> Dict:
        """三阶段分析生成投资建议"""
        logger.info("[LLM] 开始三阶段分析...")
        
        # 第一阶段：分析观点
        logger.info("[LLM] 第一阶段：分析观点...")
        viewpoint_summary = self.analyze_viewpoints_stage1(viewpoints)
        
        # 第二阶段：分析预测（传入博主信息）
        logger.info("[LLM] 第二阶段：分析预测...")
        prediction_analysis = self.analyze_predictions_stage2(predictions, viewpoint_summary, bloggers)
        
        # 第三阶段：生成投资建议
        logger.info("[LLM] 第三阶段：生成投资建议...")
        advice = self.generate_advice_stage3(viewpoint_summary, prediction_analysis, bloggers)
        
        # 保存中间结果
        advice["viewpoint_summary"] = viewpoint_summary
        advice["prediction_analysis"] = prediction_analysis
        
        logger.info("[LLM] 三阶段分析完成")
        return advice
    
    def analyze_fund_trend(self, fund_code: str, fund_name: str, history: List[Dict]) -> Dict:
        """分析基金趋势（使用辅助LLM）"""
        if not history:
            return {"trend": "unknown", "analysis": "无历史数据"}
        
        prompt = f"""请分析以下基金的趋势：

【基金信息】
代码：{fund_code}
名称：{fund_name}

【近期净值数据】（最新10天）
{json.dumps(history[:10], ensure_ascii=False, indent=2)}

请分析并返回JSON格式：
{{
    "trend": "up/down/flat",  // 趋势方向
    "strength": "strong/medium/weak",  // 趋势强度
    "analysis": "趋势分析（50字以内）",
    "support_level": 支撑位价格,
    "resistance_level": 阻力位价格,
    "recommendation": "buy/sell/hold"  // 操作建议
}}
"""
        
        try:
            response = self._call_llm(prompt, task_type='analysis', max_tokens=500)
            result = self._parse_json_with_fallback(response)
            if result:
                return result
        except Exception as e:
            logger.warning(f"[LLM] 分析基金趋势失败: {e}")
        
        return {"trend": "unknown", "analysis": "分析失败"}
    
    def analyze_fund_trend_detailed(self, fund_code: str, fund_name: str, history: List[Dict], 
                                          existing_periods: List[Dict] = None) -> Dict:
        """
        增量版基金趋势分析（使用辅助LLM，节省token）
        
        优化策略：
        1. 只发送最近N天的数据（而不是全部历史）
        2. 接收已有的阶段分析，让AI判断是否需要合并/拆分
        3. 大幅减少token消耗
        
        Args:
            fund_code: 基金代码
            fund_name: 基金名称
            history: 最近N天的净值数据（建议30天）
            existing_periods: 已有的阶段分析结果
        """
        if not history or len(history) < 5:
            return {
                "trend_summary": "数据不足",
                "periods": [],
                "fund_code": fund_code,
                "fund_name": fund_name
            }
        
        existing_context = ""
        if existing_periods:
            existing_context = f"""

【已有阶段分析】
{json.dumps(existing_periods, ensure_ascii=False, indent=2)}

请基于已有分析，判断是否需要：
1. 岿跌阶段延续： 保持不变
2. 新数据形成新阶段: 添加新阶段
3. 阶段合并: 合并相邻同趋势阶段
"""
        
        prompt = f"""分析基金趋势（增量更新）:

【基金信息】
代码: {fund_code}
名称: {fund_name}

【最近净值数据】（{len(history)}天）
{json.dumps(history, ensure_ascii=False, indent=2)}
{existing_context}
请返回JSON格式:
{{
    "trend_summary": "整体趋势一句话总结",
    "periods": [
        {{
            "start_date": "开始日期",
            "end_date": "结束日期",
            "trend": "up/down/flat",
            "change_percent": 涨跌幅百分比,
            "trend_desc": "趋势描述（如：震荡上涨）"
        }}
    ]
}}

分析要求：
1. 结合已有阶段和最新数据，更新阶段分析
2. 如果新数据延续当前趋势，保持阶段不变
3. 如果新数据形成新趋势。添加新阶段
4. 给出整体趋势的一句话总结
5. 每个阶段用简短描述说明趋势特点
"""
        
        try:
            response = self._call_llm(prompt, task_type='extraction', max_tokens=600)
            result = self._parse_json_with_fallback(response)
            if result:
                result = self._validate_trend_result(result, fund_code, fund_name)
                return result
        except Exception as e:
            logger.warning(f"[LLM] 分析基金趋势失败: {e}")
            logger.debug(f"[LLM] 原始响应: {response[:500] if 'response' in locals() else 'N/A'}")
        
        return {
            "trend_summary": "分析失败",
            "periods": [],
            "fund_code": fund_code,
            "fund_name": fund_name
        }
    
    def _clean_json_string(self, json_str: str) -> str:
        """清理 JSON 字符串中的常见问题"""
        json_str = re.sub(r'//.*$', '', json_str, flags=re.MULTILINE)
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        json_str = json_str.replace("'", '"')
        json_str = re.sub(r'(-?\d+\.?\d*)%', r'\1', json_str)
        json_str = re.sub(r'```json\s*', '', json_str)
        json_str = re.sub(r'```\s*', '', json_str)
        return json_str
    
    def _validate_trend_result(self, result: Dict, fund_code: str, fund_name: str) -> Dict:
        """验证趋势分析结果"""
        if 'periods' not in result:
            result['periods'] = []
        
        if 'trend_summary' not in result:
            result['trend_summary'] = "分析完成"
        
        validated_periods = []
        for period in result.get('periods', []):
            if not isinstance(period, dict):
                continue
            
            validated_period = {
                'start_date': period.get('start_date', '未知'),
                'end_date': period.get('end_date', '未知'),
                'trend': period.get('trend', 'flat'),
                'change_percent': period.get('change_percent', 0),
                'trend_desc': period.get('trend_desc', '')
            }
            
            if validated_period['trend'] not in ['up', 'down', 'flat']:
                validated_period['trend'] = 'flat'
            
            try:
                validated_period['change_percent'] = float(validated_period['change_percent'])
            except (ValueError, TypeError):
                validated_period['change_percent'] = 0
            
            validated_periods.append(validated_period)
        
        result['periods'] = validated_periods
        result["fund_code"] = fund_code
        result["fund_name"] = fund_name
        result["analysis_date"] = date.today().isoformat()
        return result
    
    def analyze_sector_trend(self, sector_name: str, funds_data: List[Dict]) -> Dict:
        """
        分析整个板块的趋势
        接收多个基金的数据，分析板块整体走势
        """
        if not funds_data:
            return {
                "sector_name": sector_name,
                "trend_summary": "无数据",
                "overall_trend": "unknown",
                "fund_count": 0,
                "consensus": "unknown"
            }
        
        # 准备基金数据摘要
        funds_summary = []
        for fund in funds_data:
            funds_summary.append({
                "code": fund.get("fund_code"),
                "name": fund.get("fund_name"),
                "latest_nav": fund.get("latest_nav"),
                "day_growth": fund.get("day_growth"),
                "week_growth": fund.get("week_growth"),
                "month_growth": fund.get("month_growth"),
                "trend_summary": fund.get("trend_summary", "未分析")
            })
        
        prompt = f"""请分析以下板块的整体趋势：

【板块名称】
{sector_name}

【板块内基金数据】
{json.dumps(funds_summary, ensure_ascii=False, indent=2)}

请分析并返回JSON格式：
{{
    "sector_name": "板块名称",
    "trend_summary": "板块总体趋势描述（50字以内）",
    "overall_trend": "up/down/flat/mixed",
    "trend_strength": "strong/medium/weak",
    "fund_count": 基金数量,
    "up_count": 上涨基金数量,
    "down_count": 下跌基金数量,
    "flat_count": 持平基金数量,
    "consensus": "一致/分歧",  // 板块内基金走势是否一致
    "avg_change": 平均涨跌幅,
    "leader_fund": "领涨/领跌基金代码",
    "risk_level": "high/medium/low",
    "recommendation": "buy/sell/hold/watch"
}}

分析要点：
1. 综合多个基金的走势判断板块整体趋势
2. 判断板块内基金走势是否一致（共识度）
3. 找出领涨或领跌的代表基金
4. 评估板块风险等级
"""
        
        try:
            response = self._call_llm(prompt, task_type='analysis', max_tokens=800)
            result = self._parse_json_with_fallback(response)
            if result:
                result["analysis_date"] = date.today().isoformat()
                result["funds_analyzed"] = len(funds_data)
                return result
        except Exception as e:
            logger.warning(f"[LLM] 分析板块趋势失败: {e}")
            logger.debug(f"[LLM] 原始响应: {response[:500] if 'response' in locals() else 'N/A'}")
        
        return {
            "sector_name": sector_name,
            "trend_summary": "分析失败",
            "overall_trend": "unknown",
            "fund_count": len(funds_data),
            "consensus": "unknown"
        }
    
    def analyze_image(self, image_url: str, question: str = "请描述这张图片的内容") -> str:
        """
        分析图片内容（使用火山引擎主力模型的多模态能力）
        """
        if self.provider != 'volcengine':
            logger.warning("[LLM] 图片分析仅支持火山引擎")
            return "图片分析功能需要使用火山引擎"
        
        try:
            model = self.model
            
            if image_url.startswith('data:image'):
                image_content = {
                    "type": "image_url",
                    "image_url": {"url": image_url}
                }
            else:
                image_content = {
                    "type": "image_url", 
                    "image_url": {"url": image_url}
                }
            
            response = self.client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            image_content,
                            {"type": "text", "text": question}
                        ]
                    }
                ],
                max_tokens=1000
            )
            
            return response.choices[0].message.content
            
        except Exception as e:
            logger.error(f"[LLM] 图片分析失败: {e}")
            return f"图片分析失败: {str(e)}"
    
    def analyze_image_lite(self, image_url: str, question: str = "请描述这张图片的内容") -> str:
        """
        分析图片内容（使用火山引擎辅助模型的多模态能力）
        """
        if self.provider != 'volcengine':
            logger.warning("[LLM] 图片分析仅支持火山引擎")
            return "图片分析功能需要使用火山引擎"
        
        try:
            model = self.light_model
            
            if image_url.startswith('data:image'):
                image_content = {
                    "type": "image_url",
                    "image_url": {"url": image_url}
                }
            else:
                image_content = {
                    "type": "image_url",
                    "image_url": {"url": image_url}
                }
            
            response = self.client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            image_content,
                            {"type": "text", "text": question}
                        ]
                    }
                ],
                max_tokens=1000
            )
            
            return response.choices[0].message.content
            
        except Exception as e:
            logger.error(f"[LLM] 图片分析失败: {e}")
            return f"图片分析失败: {str(e)}"

    def _call_llm_with_model(self, model: str, prompt: str, max_tokens: int = 500, temperature: float = 0.7) -> str:
        """使用指定模型调用LLM（用于测试特定模型）"""
        response = self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=temperature,
            max_tokens=max_tokens
        )
        return response.choices[0].message.content.strip()


    def get_stats(self) -> Dict:
        """获取LLM调用统计信息"""
        return {
            **self._call_stats,
            'circuit_breaker_state': self.circuit_breaker.state,
            'circuit_breaker_failures': self.circuit_breaker.failure_count,
            'cache_size': len(self.result_cache._cache)
        }
    
    def reset_stats(self):
        """重置统计信息"""
        self._call_stats = {
            'total_calls': 0,
            'successful_calls': 0,
            'failed_calls': 0,
            'cache_hits': 0,
            'total_tokens': 0,
            'total_duration': 0.0
        }
        self.circuit_breaker.record_success()
        logger.info("[LLM] 统计信息已重置")


_analyzer: Optional[LLMAnalyzer] = None
_analyzer_api_key: Optional[str] = None


def get_analyzer() -> LLMAnalyzer:
    """获取分析器单例（支持配置热更新）"""
    global _analyzer, _analyzer_api_key
    
    current_api_key = config.LLM_API_KEY
    if _analyzer is None or _analyzer_api_key != current_api_key:
        logger.info(f"[LLM] 初始化分析器 (API Key: {'已设置' if current_api_key else '未设置'})")
        _analyzer = LLMAnalyzer()
        _analyzer_api_key = current_api_key
    
    return _analyzer


def reset_analyzer():
    """重置分析器（用于配置更新后）"""
    global _analyzer, _analyzer_api_key
    _analyzer = None
    _analyzer_api_key = None
    logger.info("[LLM] 分析器已重置，下次调用将重新初始化")


# ==================== 合并预测分析功能 ====================

def merge_predictions_analysis(
    blogger_name: str,
    fund_code: str,
    fund_name: str,
    predictions: List[Dict]
) -> Dict:
    """
    合并分析同一博主对同一基金的多个预测
    
    Args:
        blogger_name: 博主名称
        fund_code: 基金代码
        fund_name: 基金名称
        predictions: 预测列表，每个预测包含 prediction_date, prediction_content, prediction_type, confidence 等
    
    Returns:
        合并分析结果
    """
    if not predictions:
        return {
            "success": False,
            "error": "没有预测数据可供分析"
        }
    
    if len(predictions) == 1:
        return {
            "success": True,
            "overall_sentiment": predictions[0].get("prediction_type", "neutral"),
            "merged_content": "只有一个预测，无需合并分析",
            "prediction_count": 1
        }
    
    analyzer = get_analyzer()
    
    # 准备预测数据摘要
    predictions_summary = []
    for i, pred in enumerate(predictions, 1):
        predictions_summary.append({
            "序号": i,
            "日期": pred.get("prediction_date", "未知"),
            "类型": pred.get("prediction_type", "unknown"),
            "信心": pred.get("confidence", 50),
            "内容": pred.get("prediction_content", "")[:100]  # 只取前100字
        })
    
    prompt = f"""请分析以下博主对同一基金的多个预测，生成综合观点分析。

【博主】{blogger_name}
【基金】{fund_name} ({fund_code})

【预测记录】（按时间顺序）
{json.dumps(predictions_summary, ensure_ascii=False, indent=2)}

请综合分析以上预测，返回JSON格式：
{{
    "overall_sentiment": "bullish/bearish/neutral",  // 整体观点
    "sentiment_evolution": "观点演变描述（100字以内）",
    "short_term_analysis": "短期分析（1周内，80字以内）",
    "medium_term_analysis": "中期分析（1个月内，80字以内）",
    "long_term_analysis": "长期分析（3个月内，80字以内）",
    "consistency_score": 0-100,  // 观点一致性评分
    "key_time_points": ["时间点1", "时间点2"],  // 关键时间节点
    "risk_factors": "风险提示（80字以内）",
    "merged_content": "完整的综合分析（300字以内）"
}}

分析要点：
1. 识别观点的变化趋势（从看涨到看跌/从谨慎到乐观等）
2. 分析短期、中期、长期的时间维度观点
3. 评估观点的一致性和矛盾点
4. 提取关键时间节点（如"3月中旬"、"月底"等）
5. 给出风险提示
6. 生成简洁但全面的综合观点
"""
    
    try:
        response = analyzer._call_llm(prompt, task_type='core', max_tokens=1500, temperature=0.5)
        result = analyzer._parse_json_with_fallback(response)
        if result:
            result["success"] = True
            result["prediction_count"] = len(predictions)
            return result
        else:
            raise ValueError("无法从响应中提取JSON")
            
    except Exception as e:
        logger.warning(f"[LLM] 合并预测分析失败: {e}")
        return {
            "success": False,
            "error": str(e),
            "overall_sentiment": "neutral",
            "merged_content": "分析失败，请重试",
            "prediction_count": len(predictions)
        }


def summarize_viewpoints_by_date(viewpoints: List[Dict], target_date: str) -> Dict:
    """
    汇总指定日期的所有观点
    
    Args:
        viewpoints: 观点列表，每个观点包含 summary, market_direction, confidence, sectors_bullish, sectors_bearish
        target_date: 目标日期
    
    Returns:
        汇总结果
    """
    if not viewpoints:
        return {
            "success": False,
            "error": "没有观点需要汇总"
        }
    
    analyzer = get_analyzer()
    
    viewpoints_data = []
    for i, v in enumerate(viewpoints, 1):
        viewpoints_data.append({
            "序号": i,
            "摘要": v.get("summary", "")[:150] if v.get("summary") else "",
            "方向": v.get("market_direction", "neutral"),
            "信心": v.get("confidence", 50),
            "看多板块": v.get("sectors_bullish", [])[:5],
            "看空板块": v.get("sectors_bearish", [])[:5]
        })
    
    prompt = f"""你是一个专业的市场观点分析师。请对以下同一天的多个市场观点进行详细汇总，为投资决策提供充分的数据支撑。

【汇总原则】
1. 识别相同主题：将表达同一事件的不同表述合并（如"中东局势"、"伊朗冲突"、"海湾局势"视为同一主题）
2. 合并重复信息：同一主题下的重复内容只保留一次，但要保留不同来源的佐证
3. 保留不同角度：同一主题下不同角度的观点要分别列出，包括多空双方的观点
4. 筛选关键数据：自行判断哪些数字、百分比、时间节点对投资决策重要，保留重要的，略去次要的
5. 动态调整详细度：观点多则详细展开，观点少则精炼总结，由你自行判断
6. 语言简明：用最精炼的语言表达，不啰嗦，但绝不遗漏任何关键信息

【日期】{target_date}
【观点数量】{len(viewpoints)}

【观点列表】
{json.dumps(viewpoints_data, ensure_ascii=False, indent=2)}

请返回JSON格式：
{{
    "market_direction": "bullish/bearish/neutral",
    "confidence": 0-100,
    "topics": [
        {{
            "topic_name": "主题名称（如：地缘冲突、PMI数据、政策动向）",
            "sentiment": "bullish/bearish/neutral",
            "key_points": [
                "简明要点1（保留对投资决策重要的数据）",
                "..."
            ],
            "affected_sectors": ["相关板块"],
            "confidence_avg": 0-100,
            "detail_analysis": "该主题的详细分析（简明扼要，包含事件背景、市场影响、投资机会或风险，根据观点数量动态调整详细度）"
        }}
    ],
    "sectors_bullish": ["看多板块（附理由）"],
    "sectors_bearish": ["看空板块（附理由）"],
    "content": "详细汇总内容（简明扼要地按主题展开，包含对投资决策重要的数据，根据观点数量动态调整篇幅）",
    "reasoning": "综合分析（简明扼要地分析多空因素、市场情绪、投资建议，根据观点数量动态调整详细度）"
}}

【重要要求】
1. topics 数组要包含所有识别出的主题
2. key_points 根据观点数量动态调整，观点多则多列，观点少则精炼
3. 语言要简明扼要，但绝不遗漏任何关键信息
4. content 字段是最重要的，要完整覆盖所有关键信息
5. reasoning 要分析多空博弈逻辑，给出投资方向建议
"""

    try:
        response = analyzer._call_llm(prompt, task_type='core', max_tokens=10000, temperature=0.3)
        result = analyzer._parse_json_with_fallback(response)
        if result:
            result["success"] = True
            result["original_count"] = len(viewpoints)
            return result
        else:
            raise ValueError("无法从响应中提取JSON")
            
    except Exception as e:
        logger.warning(f"[LLM] 观点汇总失败: {e}")
        return {
            "success": False,
            "error": str(e),
            "summary": "汇总失败，请重试",
            "market_direction": "neutral",
            "confidence": 50,
            "topics": [],
            "sectors_bullish": [],
            "sectors_bearish": [],
            "reasoning": "汇总失败",
            "original_count": len(viewpoints)
        }
