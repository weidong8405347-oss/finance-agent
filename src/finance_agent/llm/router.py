"""LLMRouter：多 provider 接入（移植 V1.0 的 env 三件套约定）。

每家 provider 三件套：<NAME>_API_KEY / <NAME>_BASE_URL / <NAME>_MODEL，
统一走 OpenAI 兼容协议。按 agent 角色路由 provider（config 的 role_map）。
fail-closed：三件套不齐 = 该 provider 不存在（ProviderConfigError）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .base import LLM, AssistantReply, ToolCall

Transport = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]

_KNOWN_PROVIDERS = ("openai", "anthropic", "zhipuai", "deepseek")


class ProviderConfigError(Exception):
    """provider 三件套未配置齐全（fail-closed）。"""


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    api_key: str
    base_url: str  # 不含尾斜杠，如 https://api.openai.com/v1
    model: str


class OpenAICompatLLM:
    """OpenAI 兼容 chat/completions 客户端。transport 可注入（测试/离线）。"""

    def __init__(
        self,
        spec: ProviderSpec,
        *,
        transport: Transport | None = None,
        tool_schemas: dict[str, dict] | None = None,
        timeout: float = 60.0,
    ):
        self.spec = spec
        self._transport = transport or self._httpx_transport
        self._tool_schemas = tool_schemas or {}
        self._timeout = timeout

    def complete(self, messages: list[dict[str, Any]], tools: list[str]) -> AssistantReply:
        body: dict[str, Any] = {"model": self.spec.model, "messages": messages}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": self._tool_schemas.get(
                        name, {"name": name, "parameters": {"type": "object", "properties": {}}}
                    ),
                }
                for name in tools
            ]
        url = f"{self.spec.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.spec.api_key}", "Content-Type": "application/json"}
        resp = self._transport(url, headers, body)
        msg = resp["choices"][0]["message"]
        tool_calls = [
            ToolCall(
                call_id=tc["id"],
                name=tc["function"]["name"],
                arguments=json.loads(tc["function"].get("arguments") or "{}"),
            )
            for tc in (msg.get("tool_calls") or [])
        ]
        return AssistantReply(content=msg.get("content") or "", tool_calls=tool_calls)

    def _httpx_transport(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        import httpx  # lazy：核心与测试不依赖网络库

        resp = httpx.post(url, headers=headers, json=body, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()


class LLMRouter:
    def __init__(
        self,
        specs: dict[str, ProviderSpec],
        *,
        default_provider: str,
        role_map: dict[str, str] | None = None,
    ):
        self._specs = specs
        self._default = default_provider
        self._role_map = role_map or {}

    @classmethod
    def from_env(
        cls,
        *,
        default_provider: str = "openai",
        role_map: dict[str, str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> LLMRouter:
        env = env or os.environ
        specs: dict[str, ProviderSpec] = {}
        for name in _KNOWN_PROVIDERS:
            prefix = name.upper()
            key, base, model = (
                env.get(f"{prefix}_API_KEY"),
                env.get(f"{prefix}_BASE_URL"),
                env.get(f"{prefix}_MODEL"),
            )
            if key and base and model:
                specs[name] = ProviderSpec(name=name, api_key=key, base_url=base.rstrip("/"), model=model)
        return cls(specs, default_provider=default_provider, role_map=role_map)

    def get(self, role: str | None = None) -> LLM:
        provider = self._role_map.get(role or "", self._default)
        spec = self._specs.get(provider)
        if spec is None:
            raise ProviderConfigError(
                f"provider {provider!r} 未配置（需要 {provider.upper()}_API_KEY/BASE_URL/MODEL 三件套）"
            )
        return OpenAICompatLLM(spec)
