"""OpenAI 线格式转换契约（400 事故根因修复）：

derive_messages 的内部规范格式（call_id/provenance 等审计字段）≠ OpenAI 线格式。
OpenAICompatLLM 必须在发送前做投影：
- assistant 的 tool_calls → {"id","type":"function","function":{"name","arguments": JSON 字符串}}
- tool 结果 → {"role":"tool","tool_call_id","content"}，剥离 provenance/name 等内部字段
- system/user 只透传 role+content

另：HTTP 错误必须把响应体带进异常（400 的原因在 body 里）。
"""

import json

import pytest

from finance_agent.llm.router import (
    LLMCallError,
    OpenAICompatLLM,
    ProviderSpec,
    to_openai_messages,
)


def test_to_openai_messages_converts_tool_protocol():
    canonical = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"call_id": "c1", "name": "query_prices", "arguments": {"ticker": "A"}}],
        },
        {
            "role": "tool",
            "call_id": "c1",
            "name": "query_prices",
            "content": "[{\"close\": 1}]",
            "provenance": [{"source_id": "prices"}],
        },
        {"role": "assistant", "content": "结论", "tool_calls": []},
    ]
    wire = to_openai_messages(canonical)

    assert wire[0] == {"role": "system", "content": "s"}
    assert wire[1] == {"role": "user", "content": "u"}
    # assistant 工具调用 → OpenAI 格式
    assert wire[2]["tool_calls"] == [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "query_prices", "arguments": json.dumps({"ticker": "A"})},
        }
    ]
    # tool 结果：tool_call_id + content，审计字段被剥离
    assert wire[3] == {"role": "tool", "tool_call_id": "c1", "content": '[{"close": 1}]'}
    assert "provenance" not in wire[3] and "call_id" not in wire[3]
    # 空 tool_calls 不下发（部分网关拒绝空数组）
    assert "tool_calls" not in wire[4]


def test_wire_body_uses_converted_messages():
    sent = {}

    def transport(url, headers, body):
        sent.update(body)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    llm = OpenAICompatLLM(
        ProviderSpec(name="x", api_key="k", base_url="https://x/v1", model="m"),
        transport=transport,
    )
    llm.complete(
        [{"role": "tool", "call_id": "c9", "content": "r", "provenance": []}],
        tools=[],
    )
    assert sent["messages"] == [{"role": "tool", "tool_call_id": "c9", "content": "r"}]


def test_http_error_carries_response_body():
    import httpx

    def transport(url, headers, body):
        resp = httpx.Response(400, json={"error": {"message": "tools[0] invalid schema"}})
        raise httpx.HTTPStatusError("bad", request=None, response=resp)

    llm = OpenAICompatLLM(
        ProviderSpec(name="x", api_key="k", base_url="https://x/v1", model="m"),
        transport=transport,
    )
    with pytest.raises(LLMCallError, match="tools\\[0\\] invalid schema"):
        llm.complete([{"role": "user", "content": "hi"}], tools=[])
