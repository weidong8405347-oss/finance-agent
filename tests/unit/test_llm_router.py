"""LLMRouter + OpenAI 兼容客户端契约（移植 V1.0 的 provider 三件套约定）。"""

import pytest

from finance_agent.llm.base import AssistantReply
from finance_agent.llm.router import (
    LLMRouter,
    OpenAICompatLLM,
    ProviderConfigError,
    ProviderSpec,
)


def fake_transport(response: dict, recorder: list):
    def transport(url: str, headers: dict, body: dict) -> dict:
        recorder.append({"url": url, "headers": headers, "body": body})
        return response

    return transport


OPENAI_TOOL_RESPONSE = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "query_prices", "arguments": '{"ticker": "AAPL"}'},
                    }
                ],
            }
        }
    ]
}

OPENAI_TEXT_RESPONSE = {"choices": [{"message": {"role": "assistant", "content": "结论"}}]}


def test_openai_compat_request_and_parse_tool_calls():
    calls = []
    llm = OpenAICompatLLM(
        ProviderSpec(name="openai", api_key="sk-x", base_url="https://api.example/v1", model="gpt-x"),
        transport=fake_transport(OPENAI_TOOL_RESPONSE, calls),
    )
    reply = llm.complete([{"role": "user", "content": "hi"}], tools=["query_prices"])

    assert isinstance(reply, AssistantReply)
    assert reply.tool_calls[0].name == "query_prices"
    assert reply.tool_calls[0].arguments == {"ticker": "AAPL"}

    sent = calls[0]
    assert sent["url"] == "https://api.example/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer sk-x"
    assert sent["body"]["model"] == "gpt-x"
    # 工具 schema 被正确翻译为 OpenAI tools 格式
    assert sent["body"]["tools"][0]["function"]["name"] == "query_prices"


def test_openai_compat_text_reply():
    llm = OpenAICompatLLM(
        ProviderSpec(name="openai", api_key="k", base_url="https://x/v1", model="m"),
        transport=fake_transport(OPENAI_TEXT_RESPONSE, []),
    )
    reply = llm.complete([{"role": "user", "content": "hi"}], tools=[])
    assert reply.content == "结论" and reply.tool_calls == []


def test_router_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-a")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://a/v1")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-a")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-b")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://b/v1")
    monkeypatch.setenv("DEEPSEEK_MODEL", "ds-b")

    router = LLMRouter.from_env(default_provider="openai", role_map={"ticker_mapper": "deepseek"})
    assert router.get("ticker_mapper").spec.model == "ds-b"  # 角色路由
    assert router.get("research_planner").spec.model == "gpt-a"  # 未映射角色走默认


def test_router_missing_provider_config_fail_closed(monkeypatch):
    for var in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("finance_agent.llm.router._read_dotenv", lambda *a: {})  # 隔离真实 .env
    router = LLMRouter.from_env(default_provider="openai")
    with pytest.raises(ProviderConfigError):
        router.get("any")
