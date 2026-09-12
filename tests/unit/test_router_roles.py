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
    def _mk_pi(self, tmp_path, providers: dict, keys: dict):
        pi = tmp_path / ".pi" / "agent"
        pi.mkdir(parents=True)
        (pi / "models.json").write_text(json.dumps({"providers": providers}))
        (pi / "auth.json").write_text(json.dumps(keys))
        return pi

    _NOVITA = {
        "api": "openai-completions",
        "baseUrl": "https://api.novita.ai/openai/v1",
        "models": [{"id": "pa/gpt-5.6-sol"}],
    }
    _DASH = {
        "api": "openai-completions",
        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": [{"id": "kimi-k3"}, {"id": "ZHIPU/GLM-5.3"}],
    }

    def test_no_dashscope_no_remap(self, tmp_path):
        """pi 配置里没有 dashscope → 不硬指 kimi-k3，research 落回默认 provider。"""
        pi = self._mk_pi(tmp_path, {"novita-gpt": self._NOVITA}, {"novita-gpt": {"key": "sk-n"}})
        router = LLMRouter.from_pi(pi_dir=pi, default_provider="novita-gpt")
        llm = router.get("research")
        assert llm.spec.model == "pa/gpt-5.6-sol"
        assert llm.spec.effort is None

    def test_gpt_present_takes_research_alt_and_judge(self, tmp_path):
        """gpt-5.6-sol 在场：research-alt 与 judge 走 GPT 异构 @ xhigh（novita 不支持 max）。"""
        pi = self._mk_pi(
            tmp_path,
            {"novita-gpt": self._NOVITA, "dashscope": self._DASH},
            {"novita-gpt": {"key": "sk-n"}, "dashscope": {"key": "sk-d"}},
        )
        router = LLMRouter.from_pi(pi_dir=pi, default_provider="novita-gpt")
        assert router.has_role("judge")
        for role in ("research-alt", "judge"):
            llm = router.get(role)
            assert llm.spec.model == "pa/gpt-5.6-sol"
            assert llm.spec.api_key == "sk-n"
            assert llm.spec.effort == "xhigh"
            assert llm._timeout == 300.0  # noqa: SLF001
        # 主力位不动：research 仍 kimi-k3@max
        assert router.get("research").spec.model == "kimi-k3"

    def test_gpt_absent_research_alt_falls_back_to_glm_no_judge(self, tmp_path):
        """gpt-5.6-sol 缺位：research-alt 回落 GLM-5.3@max，judge 角色不注册（调用方回落 research）。"""
        pi = self._mk_pi(tmp_path, {"dashscope": self._DASH}, {"dashscope": {"key": "sk-d"}})
        router = LLMRouter.from_pi(pi_dir=pi, default_provider="dashscope")
        alt = router.get("research-alt")
        assert alt.spec.model == "ZHIPU/GLM-5.3"
        assert alt.spec.effort == "max"
        assert not router.has_role("judge")

    _NOVITA_WITH_OPUS = {
        "api": "openai-completions",
        "baseUrl": "https://api.novita.ai/openai/v1",
        "models": [{"id": "pa/gpt-5.6-sol"}, {"id": "anthropic/claude-opus-5"}],
    }

    def test_opus_present_takes_research_lead(self, tmp_path):
        """claude-opus-5 在场：research 主力位交 opus-5@max（调研规划最关键位用最强模型）；
        kimi-k3 降为缺位兜底，research-alt/judge 仍 gpt-5.6-sol 异构交叉。"""
        pi = self._mk_pi(
            tmp_path,
            {"novita-gpt": self._NOVITA_WITH_OPUS, "dashscope": self._DASH},
            {"novita-gpt": {"key": "sk-n"}, "dashscope": {"key": "sk-d"}},
        )
        router = LLMRouter.from_pi(pi_dir=pi, default_provider="novita-gpt")
        lead = router.get("research")
        assert lead.spec.model == "anthropic/claude-opus-5"
        assert lead.spec.api_key == "sk-n"
        assert lead.spec.effort == "max"  # novita 实测 opus-5 接受 reasoning_effort=max
        assert lead._timeout == 300.0  # noqa: SLF001
        # 交叉补充位不受主力位影响
        assert router.get("research-alt").spec.model == "pa/gpt-5.6-sol"
        assert router.get("judge").spec.model == "pa/gpt-5.6-sol"

    def test_opus_without_dashscope_still_leads_research(self, tmp_path):
        """只有 novita-gpt（含 opus-5）：research 显式指 opus-5（不依赖 dashscope 存在）。"""
        pi = self._mk_pi(
            tmp_path, {"novita-gpt": self._NOVITA_WITH_OPUS}, {"novita-gpt": {"key": "sk-n"}}
        )
        router = LLMRouter.from_pi(pi_dir=pi, default_provider="novita-gpt")
        lead = router.get("research")
        assert lead.spec.model == "anthropic/claude-opus-5"
        assert lead.spec.effort == "max"


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


class TestStreamRetryBoundary:
    """流式重试边界（2026-09-12 合成断流事故）：
    - 只累积 tool_calls（无文本 chunk 落事件）→ 传输层断流可安全重试；
    - 文本已回调 on_delta（已落 assistant/chunk 事件）→ 不重试（防 UI/日志重复）。
    """

    def _resp(self, lines):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            def iter_lines(self):
                for line in lines:
                    yield line

        return _Resp()

    def test_tool_call_only_drop_retries(self, monkeypatch):
        import httpx

        calls = {"n": 0}

        # 断流注入：iter_lines 中途抛连接错误
        class _DropMidway:
            def __init__(self, line):
                self._line = line

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            def iter_lines(self):
                yield self._line
                raise httpx.RemoteProtocolError("peer closed connection")

        # 合法 JSON 的工具调用增量帧（无任何文本 chunk）
        tc_frame = 'data: ' + json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "submit_structures", "arguments": "{\"a\":"}
        }]}}]})
        ok_frame = 'data: ' + json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1",
             "function": {"name": "submit_structures", "arguments": "{}"}}
        ]}}]})

        def fake_stream_drop(method, url, headers=None, json=None, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _DropMidway(tc_frame)
            return self._resp([ok_frame, "data: [DONE]"])

        monkeypatch.setattr(httpx, "stream", fake_stream_drop)
        monkeypatch.setattr("time.sleep", lambda s: None)  # 测试不等退避
        llm = OpenAICompatLLM(_spec(), retry_attempts=2)
        reply = llm.stream_complete([{"role": "user", "content": "hi"}], ["submit_structures"],
                                    on_delta=lambda d: None)
        assert calls["n"] == 2  # 重试发生了
        assert reply.tool_calls[0].name == "submit_structures"

    def test_text_emitted_retry_safe_because_chunks_leave_model_context(self, monkeypatch):
        """文本已输出后断流也重试：assistant/chunk 不进模型上下文（derive_messages
        只消费 assistant/message），重复仅限 UI replay 残影；run 存活优先。
        4xx 参数错误仍不重试。"""
        import httpx

        calls = {"n": 0}
        deltas: list[str] = []

        class _DropAfterText:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            def iter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"部分文本"}}]}'
                raise httpx.RemoteProtocolError("peer closed connection")

        def fake_stream(method, url, headers=None, json=None, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _DropAfterText()
            return self._resp([
                'data: {"choices":[{"delta":{"content":"完整回答"}}]}',
                "data: [DONE]",
            ])

        monkeypatch.setattr(httpx, "stream", fake_stream)
        monkeypatch.setattr("time.sleep", lambda s: None)

        llm = OpenAICompatLLM(_spec(), retry_attempts=3)
        reply = llm.stream_complete([{"role": "user", "content": "hi"}], [],
                                    on_delta=deltas.append)
        assert calls["n"] == 2  # 传输层断流 → 重试成功
        assert reply.content == "完整回答"  # 最终内容来自成功的那次（模型上下文无重复）
        # UI replay 会见到残影（部分文本 + 完整回答），可接受，不断言无重复

    def test_4xx_no_retry(self, monkeypatch):
        import httpx

        calls = {"n": 0}

        class _Resp400:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                req = httpx.Request("POST", "http://x")
                resp = httpx.Response(400, request=req, text="bad request")
                raise httpx.HTTPStatusError("400", request=req, response=resp)

            def iter_lines(self):
                return iter(())

        def fake_stream(method, url, headers=None, json=None, timeout=None):
            calls["n"] += 1
            return _Resp400()

        monkeypatch.setattr(httpx, "stream", fake_stream)
        llm = OpenAICompatLLM(_spec(), retry_attempts=3)
        with pytest.raises(Exception):  # noqa: B017 - LLMCallError
            llm.stream_complete([{"role": "user", "content": "hi"}], [], on_delta=lambda d: None)
        assert calls["n"] == 1  # 4xx 参数类错误重试无意义（fail-loud）


class TestGateway400Retry:
    """novita→Bedrock 桥瞬时 400（2026-09-12 合成步骤实测）：
    - 携带 Bedrock Runtime/ValidationException 标记的 400 → 可重试（重放同请求可成功）；
    - 普通 400（真参数错误）→ 不重试；
    - 流式错误的响应体必须进 LLMCallError 消息（不得是 <unread streaming response>）。
    """

    def test_gateway_bridge_400_retryable(self):
        import httpx

        from finance_agent.llm.router import _is_retryable
        req = httpx.Request("POST", "http://x")
        resp = httpx.Response(
            400, request=req,
            text='{"message":"InvokeModelWithResponseStream: Bedrock Runtime ...'
                 'ValidationException: tool_use.id"}',
        )
        e = httpx.HTTPStatusError("400", request=req, response=resp)
        assert _is_retryable(e) is True

    def test_plain_400_not_retryable(self):
        import httpx

        from finance_agent.llm.router import _is_retryable
        req = httpx.Request("POST", "http://x")
        resp = httpx.Response(400, request=req,
                              text='{"error":{"message":"model not found"}}')
        e = httpx.HTTPStatusError("400", request=req, response=resp)
        assert _is_retryable(e) is False

    def test_stream_400_body_captured(self, monkeypatch):
        """流式路径的错误体不再被吞（先 resp.read() 再取 text）。"""
        import httpx

        class _Resp400Stream:
            def __init__(self):
                self.status_code = 400
                self._text = ""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                self._text = "Bedrock Runtime ValidationException: tool_use.id 不合法"
                return self._text.encode()

            @property
            def text(self):
                if not self._text:
                    raise httpx.ResponseNotRead()  # 未读时取 text 抛错（修复前的事故形态）
                return self._text

            @property
            def headers(self):
                return {}

            def raise_for_status(self):
                req = httpx.Request("POST", "http://x")
                # 忠实于 httpx：流式响应的 raise_for_status 以自身为 e.response
                self.request = req
                raise httpx.HTTPStatusError("400", request=req, response=self)

            def iter_lines(self):
                return iter(())

        monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Resp400Stream())
        monkeypatch.setattr("time.sleep", lambda s: None)
        llm = OpenAICompatLLM(_spec(), retry_attempts=1)
        with pytest.raises(Exception) as exc_info:  # noqa: B017
            llm.stream_complete([{"role": "user", "content": "hi"}], [], on_delta=lambda d: None)
        assert "unread streaming response" not in str(exc_info.value)
        assert "Bedrock Runtime" in str(exc_info.value)  # 真实原因进错误消息
