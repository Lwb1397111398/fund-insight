"""思考型模型（火山方舟 GLM-5.2/Doubao 等）默认关闭深度思考的守卫测试。

关闭思考后结构化提取延迟可降数倍（实测 GLM-5.2 同提示词 22s→4.8s）；
拒绝 thinking 参数的模型必须自动回退且不影响其他模型。
"""
from types import SimpleNamespace

from src.analyzer.llm_analyzer import LLMAnalyzer


class _FakeResp:
    def __init__(self, content="ok"):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
        self.usage = None


class _FakeClient:
    """记录 create 调用；reject_thinking=True 时模拟拒绝 extra_body 的模型。"""

    def __init__(self, reject_thinking=False):
        self.calls = []
        self.reject = reject_thinking
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject and "extra_body" in kwargs:
            raise Exception("Error code: 400 - Unknown parameter: thinking.")
        return _FakeResp()


def _bare_analyzer(client):
    analyzer = object.__new__(LLMAnalyzer)
    analyzer.client = client
    analyzer.disable_thinking = True
    analyzer._thinking_rejected_models = set()
    analyzer._call_stats = {
        "total_calls": 0,
        "successful_calls": 0,
        "failed_calls": 0,
        "cache_hits": 0,
        "total_tokens": 0,
        "total_duration": 0.0,
        "total_cost": 0.0,
        "model_usage": {},
    }
    analyzer.circuit_breaker = SimpleNamespace(
        record_success=lambda: None, record_failure=lambda: None
    )
    analyzer.strategy = "auto"
    analyzer.model = "main-model"
    analyzer.light_model = "light-model"
    analyzer._downgrade_state = {"enabled": False, "failure_count": 0, "last_failure_time": 0}
    return analyzer


def test_thinking_disabled_by_default():
    client = _FakeClient()
    analyzer = _bare_analyzer(client)

    result = analyzer._call_llm_internal("测试提示词", task_type="core", use_cache=False, retry_count=1)

    assert result == "ok"
    assert len(client.calls) == 1
    assert client.calls[0].get("extra_body") == {"thinking": {"type": "disabled"}}


def test_thinking_param_rejection_falls_back_once():
    client = _FakeClient(reject_thinking=True)
    analyzer = _bare_analyzer(client)

    result = analyzer._call_llm_internal("测试提示词", task_type="core", use_cache=False, retry_count=1)

    assert result == "ok"
    # 首次带参数被拒 → 记录该模型 → 无参数重试成功
    assert len(client.calls) == 2
    assert "extra_body" in client.calls[0]
    assert "extra_body" not in client.calls[1]
    assert "main-model" in analyzer._thinking_rejected_models

    # 第二次调用：该模型已记录，不再附带参数、不再浪费一次失败调用
    client.calls.clear()
    analyzer._call_llm_internal("再来一次", task_type="core", use_cache=False, retry_count=1)
    assert len(client.calls) == 1
    assert "extra_body" not in client.calls[0]


def test_thinking_kept_when_flag_off():
    client = _FakeClient()
    analyzer = _bare_analyzer(client)
    analyzer.disable_thinking = False

    analyzer._call_llm_internal("测试提示词", task_type="core", use_cache=False, retry_count=1)

    assert len(client.calls) == 1
    assert "extra_body" not in client.calls[0]


def test_unrelated_error_still_raises():
    client = _FakeClient()
    analyzer = _bare_analyzer(client)

    def boom(**kwargs):
        raise RuntimeError("网络超时")

    client.chat.completions.create = boom

    try:
        analyzer._call_llm_internal("测试提示词", task_type="core", use_cache=False, retry_count=1)
        raise AssertionError("应当抛出异常")
    except RuntimeError as e:
        assert "网络超时" in str(e)
