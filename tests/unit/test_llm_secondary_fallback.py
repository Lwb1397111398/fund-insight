"""辅助 API 渠道的守卫测试。

主动渠道（任意 OpenAI 兼容端点）重试耗尽或熔断时必须整体切到辅助渠道
（换 client 也换模型）；只配主动渠道时行为与旧版完全一致。
"""
import threading
import time
from types import SimpleNamespace

from src.analyzer.llm_analyzer import CircuitBreakerState, LLMAnalyzer


class _FakeResp:
    def __init__(self, content="ok"):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
        self.usage = None


class _FakeClient:
    """可编程的 OpenAI 兼容假客户端：fail_with 非 None 时每次调用抛该异常。"""

    def __init__(self, content="ok", fail_with=None):
        self.calls = []
        self.content = content
        self.fail_with = fail_with
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return _FakeResp(self.content)


def _err(status_code, msg="api error"):
    exc = Exception(f"Error code: {status_code} - {msg}")
    exc.status_code = status_code
    return exc


def _bare_analyzer(primary, secondary=None):
    analyzer = object.__new__(LLMAnalyzer)
    analyzer.client = primary
    analyzer.secondary_client = secondary
    analyzer.secondary_model = "sec-main"
    analyzer.secondary_light_model = "sec-light"
    analyzer.circuit_breaker = CircuitBreakerState()
    analyzer.secondary_circuit_breaker = CircuitBreakerState()
    analyzer.disable_thinking = False
    analyzer._thinking_rejected_models = set()
    analyzer.strategy = "auto"
    analyzer.model = "main-model"
    analyzer.light_model = "light-model"
    analyzer.result_cache = SimpleNamespace(
        get=lambda *a, **k: None,
        set=lambda *a, **k: None,
    )
    analyzer._call_stats = {
        "total_calls": 0, "successful_calls": 0, "failed_calls": 0,
        "cache_hits": 0, "total_tokens": 0, "total_duration": 0.0,
        "total_cost": 0.0, "model_usage": {},
    }
    analyzer._downgrade_state = {
        "enabled": False, "failure_count": 0, "last_failure_time": 0,
    }
    analyzer._semaphore = threading.Semaphore(1)
    return analyzer


def test_primary_fail_then_secondary_succeeds():
    primary = _FakeClient(fail_with=_err(500))
    secondary = _FakeClient(content="来自辅助渠道")
    analyzer = _bare_analyzer(primary, secondary)

    result = analyzer._call_llm_internal("测试", task_type="core", use_cache=False, retry_count=1)

    assert result == "来自辅助渠道"
    assert len(primary.calls) == 1
    assert len(secondary.calls) == 1
    # 辅助渠道收到的模型是它自己渠道的主模型
    assert secondary.calls[0]["model"] == "sec-main"


def test_secondary_light_model_follows_primary_selection():
    primary = _FakeClient(fail_with=_err(503))
    secondary = _FakeClient(content="ok")
    analyzer = _bare_analyzer(primary, secondary)

    # strategy=auto + task_type=simple ⇒ 主渠道选轻量模型 ⇒ 辅助也该用它的轻量模型
    analyzer._call_llm_internal("测试", task_type="simple", use_cache=False, retry_count=1)

    assert secondary.calls[0]["model"] == "sec-light"


def test_non_retryable_error_switches_channel_without_retry():
    primary = _FakeClient(fail_with=_err(401, "invalid api key"))
    secondary = _FakeClient(content="ok")
    analyzer = _bare_analyzer(primary, secondary)

    result = analyzer._call_llm_internal("测试", task_type="core", use_cache=False, retry_count=3)

    assert result == "ok"
    # 主动渠道 401 不可重试 ⇒ 没把 3 次重试耗完，一发就换渠道
    assert len(primary.calls) == 1
    assert len(secondary.calls) == 1


def test_no_secondary_raises_primary_error():
    primary = _FakeClient(fail_with=_err(500, "主渠道挂了"))
    analyzer = _bare_analyzer(primary, secondary=None)

    try:
        analyzer._call_llm_internal("测试", task_type="core", use_cache=False, retry_count=1)
        raise AssertionError("应当抛出主动渠道的异常")
    except Exception as e:
        assert "主渠道挂了" in str(e)
    assert len(primary.calls) == 1


def test_both_channels_fail_raises_last_error():
    primary = _FakeClient(fail_with=_err(500, "主动失败"))
    secondary = _FakeClient(fail_with=_err(503, "辅助也失败"))
    analyzer = _bare_analyzer(primary, secondary)

    try:
        analyzer._call_llm_internal("测试", task_type="core", use_cache=False, retry_count=1)
        raise AssertionError("应当抛出异常")
    except Exception as e:
        # 最后抛的是辅助渠道（最后一个渠道）的错误
        assert "辅助" in str(e)


def test_primary_success_never_touches_secondary():
    primary = _FakeClient(content="主动渠道答")
    secondary = _FakeClient()
    analyzer = _bare_analyzer(primary, secondary)

    result = analyzer._call_llm_internal("测试", task_type="core", use_cache=False, retry_count=3)

    assert result == "主动渠道答"
    assert len(secondary.calls) == 0


def test_open_fused_primary_skipped_entirely():
    primary = _FakeClient()
    secondary = _FakeClient(content="ok")
    analyzer = _bare_analyzer(primary, secondary)
    # 主动渠道熔断器已打开 ⇒ 不碰它，直接走辅助渠道
    analyzer.circuit_breaker.state = "open"
    analyzer.circuit_breaker.last_failure_time = time.time()

    result = analyzer._call_llm_internal("测试", task_type="core", use_cache=False, retry_count=1)

    assert result == "ok"
    assert len(primary.calls) == 0
    assert len(secondary.calls) == 1
