"""分角色模型路由（effort / timeout / 自有配置文件）的契约测试。

research-capability-upgrade §4.4：
- role_options 的 effort 必须真的进请求体（complete + stream 两条路）
- 分角色 timeout 覆盖全局默认
- llm-providers.json 自有配置：别名展开、env: 密钥间接引用、fail-closed 校验
- from_pi 条件化默认：dashscope 缺位时不硬指 kimi-k3
"""

import json

import pytest

from finance_agent.llm.router import (
    LLMRouter,
    OpenAICompatLLM,
    ProviderConfigError,
    ProviderSpec,
)


def _spec(effort: str | None = None) -> ProviderSpec:
    return ProviderSpec(name="t", api_key="k", base_url="http://x", model="m", effort=effort)


class TestEffortWireFormat:
    def test_complete_sends_reasoning_effort_when_configured(self):
        captured = {}

        def transport(url, headers, body):
            captured["body"] = body
            return {"choices": [{"message": {"content": "ok"}}]}

        OpenAICompatLLM(_spec(effort="max"), transport=transport).complete(
            [{"role": "user", "content": "hi"}], []
        )
        assert captured["body"]["reasoning_effort"] == "max"

    def test_complete_omits_reasoning_effort_by_default(self):
        """未配置 effort 的角色/provider 不下发该参数（未知 provider 乱发会被 400）。"""
        captured = {}

        def transport(url, headers, body):
            captured["body"] = body
            return {"choices": [{"message": {"content": "ok"}}]}

        OpenAICompatLLM(_spec(), transport=transport).complete(
            [{"role": "user", "content": "hi"}], []
        )
        assert "reasoning_effort" not in captured["body"]

    def test_stream_sends_reasoning_effort(self, monkeypatch):
        import httpx

        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            def iter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"你好"}}]}'
                yield 'data: {"choices":[],"usage":{"total_tokens":3}}'
                yield "data: [DONE]"

        def fake_stream(method, url, headers=None, json=None, timeout=None):
            captured["body"] = json
            return _Resp()

        monkeypatch.setattr(httpx, "stream", fake_stream)
        reply = OpenAICompatLLM(_spec(effort="high")).stream_complete(
            [{"role": "user", "content": "hi"}], [], on_delta=lambda d: None
        )
        assert captured["body"]["reasoning_effort"] == "high"
        assert reply.content == "你好"


class TestRoleOptions:
    def _router(self) -> LLMRouter:
        return LLMRouter(
            {
                "dash:kimi-k3": _spec(),
                "dash": _spec(),
            },
            default_provider="dash",
            role_map={"research": "dash:kimi-k3"},
            role_options={"research": {"effort": "max", "timeout": 300}},
            timeout=180.0,
        )

    def test_role_options_apply_effort_and_timeout(self):
        llm = self._router().get("research")
        assert llm.spec.effort == "max"
        assert llm._timeout == 300.0  # noqa: SLF001

    def test_unlisted_role_keeps_defaults(self):
        llm = self._router().get("fast")  # 未配置 role_options → 默认 provider + 全局 timeout
        assert llm.spec.effort is None
        assert llm._timeout == 180.0  # noqa: SLF001


class TestFromConfig:
    def _write(self, tmp_path, cfg: dict):
        p = tmp_path / "llm-providers.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False))
        return p

    def test_loads_providers_aliases_and_roles(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-secret")
        path = self._write(tmp_path, {
            "providers": {
                "dashscope": {
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/",
                    "api_key": "env:MY_KEY",
                    "models": ["kimi-k3", "deepseek-v4-flash-0731"],
                }
            },
            "default_provider": "dashscope:kimi-k3",
            "role_map": {"research": "dashscope:kimi-k3"},
            "role_options": {"research": {"effort": "max"}},
        })
        router = LLMRouter.from_config(path)
        assert "dashscope:kimi-k3" in router.providers()
        assert "dashscope:deepseek-v4-flash-0731" in router.providers()
        llm = router.get("research")
        assert llm.spec.api_key == "sk-secret"  # env: 间接引用解析
        assert llm.spec.effort == "max"
        # base_url 尾斜杠规整
        assert not llm.spec.base_url.endswith("/")

    def test_missing_env_key_fail_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOPE_KEY", raising=False)
        path = self._write(tmp_path, {
            "providers": {"x": {"base_url": "http://a", "api_key": "env:NOPE_KEY", "models": ["m"]}}
        })
        with pytest.raises(ProviderConfigError, match="配置不全"):
            LLMRouter.from_config(path)

    def test_malformed_json_fail_closed(self, tmp_path):
        p = tmp_path / "llm-providers.json"
        p.write_text("{oops")
        with pytest.raises(ProviderConfigError, match="非法 JSON"):
            LLMRouter.from_config(p)

    def test_empty_providers_fail_closed(self, tmp_path):
        path = self._write(tmp_path, {"providers": {}})
        with pytest.raises(ProviderConfigError, match="没有任何 provider"):
            LLMRouter.from_config(path)


class TestFromPiConditionalDefaults:
    def test_no_dashscope_no_remap(self, tmp_path):
        """pi 配置里没有 dashscope → 不硬指 kimi-k3，research 落回默认 provider。"""
        pi = tmp_path / ".pi" / "agent"
        pi.mkdir(parents=True)
        (pi / "models.json").write_text(json.dumps({"providers": {
            "novita-gpt": {
                "api": "openai-completions",
                "baseUrl": "https://api.novita.ai/openai/v1",
                "models": [{"id": "pa/gpt-5.6-sol"}],
            }
        }}))
        (pi / "auth.json").write_text(json.dumps({"novita-gpt": {"key": "sk-n"}}))
        router = LLMRouter.from_pi(pi_dir=pi, default_provider="novita-gpt")
        llm = router.get("research")
        assert llm.spec.model == "pa/gpt-5.6-sol"
        assert llm.spec.effort is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


class TestRetryOn429:
    """429/5xx/瞬断退避重试（并行 worker 架构的必需品，2026-09-01 dashscope 429 实测驱动）。"""

    def _resp(self, text="ok"):
        return {"choices": [{"message": {"content": text}}]}

    def test_429_retried_until_success(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)  # 测试不等真实退避
        calls = {"n": 0}

        class _E(Exception):
            response = type("R", (), {"status_code": 429, "headers": {}, "text": "rate limited"})()

        def transport(url, headers, body):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _E("429")
            return self._resp()

        llm = OpenAICompatLLM(_spec(), transport=transport)
        reply = llm.complete([{"role": "user", "content": "hi"}], [])
        assert reply.content == "ok" and calls["n"] == 3

    def test_400_not_retried(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        calls = {"n": 0}

        class _E(Exception):
            response = type("R", (), {"status_code": 400, "headers": {}, "text": "bad request"})()

        def transport(url, headers, body):
            calls["n"] += 1
            raise _E("400")

        from finance_agent.llm.router import LLMCallError

        llm = OpenAICompatLLM(_spec(), transport=transport)
        with pytest.raises(LLMCallError):
            llm.complete([{"role": "user", "content": "hi"}], [])
        assert calls["n"] == 1  # 参数错误重试无意义

    def test_retry_exhaustion_raises(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        calls = {"n": 0}

        def transport(url, headers, body):
            calls["n"] += 1
            raise ConnectionError("连接断开")

        from finance_agent.llm.router import LLMCallError

        llm = OpenAICompatLLM(_spec(), transport=transport)
        with pytest.raises(LLMCallError):
            llm.complete([{"role": "user", "content": "hi"}], [])
        assert calls["n"] == 4  # 1 + 3 次重试


class TestMalformedToolArgs:
    """模型输出坏 JSON 不熔断（2026-09-01 F3 JSONDecodeError 事故）。"""

    def test_bad_json_args_become_parse_error_placeholder(self):
        from finance_agent.llm.router import _parse_tool_arguments

        assert _parse_tool_arguments('{"a": 1')["__parse_error__"]
        assert _parse_tool_arguments('{"a": 1}') == {"a": 1}
        assert _parse_tool_arguments("") == {}

    def test_kernel_turns_parse_error_into_tool_feedback(self):
        """内核把 __parse_error__ 转成工具错误回给模型，而不是熔断 turn。"""
        from finance_agent.eventstore.store import EventStore
        from finance_agent.harness.manifest import RunManifest, RunMode
        from finance_agent.loop.kernel import AgentKernel

        class BadJsonLLM:
            model_name = "bad-json"

            def __init__(self):
                self._n = 0

            def complete(self, messages, tools):
                from finance_agent.llm.base import AssistantReply
                from finance_agent.llm.base import ToolCall as TC

                self._n += 1
                if self._n == 1:
                    return AssistantReply(content="", tool_calls=[
                        TC(call_id="b1", name="some_tool",
                           arguments={"__parse_error__": '{"broken"'})])
                return AssistantReply(content="修好了")

        events = EventStore(":memory:")
        kernel = AgentKernel(
            store=events, llm=BadJsonLLM(),
            manifest=RunManifest(run_id="t", mode=RunMode.LIVE),
            tools={"some_tool": lambda a: {"content": "ok", "provenance": []}},
        )
        out = kernel.run_turn("go")
        assert out == "修好了"  # turn 没有熔断
        results = [e for e in events.read("t") if e.type == "tool/result"]
        assert "JSON 解析失败" in results[0].payload["content"]
