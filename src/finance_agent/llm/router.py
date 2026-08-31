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
from pathlib import Path
from typing import Any

from .base import LLM, AssistantReply, OnDelta, ToolCall

Transport = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]

_KNOWN_PROVIDERS = ("openai", "anthropic", "zhipuai", "deepseek")


class ProviderConfigError(Exception):
    """provider 三件套未配置齐全（fail-closed）。"""


class LLMCallError(Exception):
    """LLM 调用失败（HTTP 错误必须携带响应体——400 的真实原因在 body 里）。"""


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """内部规范消息 → OpenAI 线格式投影。

    - assistant.tool_calls: call_id/name/arguments → id/type/function(name, arguments=JSON str)
    - tool 结果：call_id → tool_call_id；剥离 provenance/name 等内部审计字段
    - 空 tool_calls 不下发（严格网关会拒绝）
    """
    wire: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            wire.append(
                {
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id") or m.get("call_id"),
                    "content": m.get("content", ""),
                }
            )
        elif role == "assistant":
            out: dict[str, Any] = {"role": "assistant", "content": m.get("content", "")}
            tcs = m.get("tool_calls") or []
            if tcs:
                out["tool_calls"] = [
                    {
                        "id": tc["call_id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                        },
                    }
                    for tc in tcs
                ]
            wire.append(out)
        else:
            wire.append({"role": role, "content": m.get("content", "")})
    return wire


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
        timeout: float = 180.0,
    ):
        self.spec = spec
        self._transport = transport or self._httpx_transport
        self._tool_schemas = tool_schemas or {}
        self._timeout = timeout

    @property
    def model_name(self) -> str:
        """模型标识（过程透明：事件与 UI 展示用）。"""
        return self.spec.model

    def complete(self, messages: list[dict[str, Any]], tools: list[str]) -> AssistantReply:
        body: dict[str, Any] = {"model": self.spec.model, "messages": to_openai_messages(messages)}
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
        try:
            resp = self._transport(url, headers, body)
        except Exception as e:
            raise _normalize_llm_error(e) from e
        msg = resp["choices"][0]["message"]
        tool_calls = [
            ToolCall(
                call_id=tc["id"],
                name=tc["function"]["name"],
                arguments=json.loads(tc["function"].get("arguments") or "{}"),
            )
            for tc in (msg.get("tool_calls") or [])
        ]
        return AssistantReply(
            content=msg.get("content") or "", tool_calls=tool_calls, usage=resp.get("usage")
        )

    def stream_complete(
        self, messages: list[dict[str, Any]], tools: list[str], *, on_delta: OnDelta
    ) -> AssistantReply:
        """流式补全（OpenAI SSE）：文本 delta 经 on_delta 逐段回调；
        tool_calls 的增量分片按 index 累积，结束后聚合为 AssistantReply。"""
        body: dict[str, Any] = {
            "model": self.spec.model,
            "messages": to_openai_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},  # 末尾 usage 帧（provider 不支持则略）
        }
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

        content_parts: list[str] = []
        tc_acc: dict[int, dict[str, str]] = {}  # index → {id, name, arguments}
        usage: dict[str, int] | None = None
        import httpx  # lazy

        try:
            with httpx.stream(
                "POST", url, headers=headers, json=body, timeout=self._timeout
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    frame = json.loads(data)
                    choices = frame.get("choices") or []
                    if not choices:  # 心跳/usage-only 等无 choices 帧
                        if frame.get("usage"):
                            usage = frame["usage"]
                        continue
                    delta = choices[0].get("delta", {})
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                        on_delta(delta["content"])
                    for tc_delta in delta.get("tool_calls") or []:
                        slot = tc_acc.setdefault(tc_delta["index"], {"id": "", "name": "", "arguments": ""})
                        if tc_delta.get("id"):
                            slot["id"] = tc_delta["id"]
                        fn = tc_delta.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
        except Exception as e:
            raise _normalize_llm_error(e) from e

        return AssistantReply(
            content="".join(content_parts),
            tool_calls=[
                ToolCall(
                    call_id=slot["id"] or f"call-{i}",
                    name=slot["name"],
                    arguments=json.loads(slot["arguments"] or "{}"),
                )
                for i, (idx, slot) in enumerate(sorted(tc_acc.items()))
                if slot["name"]
            ],
            usage=usage,
        )

    def _httpx_transport(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        import httpx  # lazy：核心与测试不依赖网络库

        resp = httpx.post(url, headers=headers, json=body, timeout=self._timeout)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            # 响应体携带网关的真实原因（模型名错 / schema 不兼容 / 余额不足…）
            raise LLMCallError(f"{e} | body: {resp.text[:500]}") from e
        return resp.json()


def _read_dotenv(path: str = ".env") -> dict[str, str]:
    """极简 .env 解析（KEY=VALUE，忽略注释/空行）。文件不存在即空。"""
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _normalize_llm_error(e: Exception) -> Exception:
    """transport 层的 HTTP 错误 → LLMCallError（携带响应体，可诊断）。"""
    response = getattr(e, "response", None)
    if response is not None:
        body = getattr(response, "text", "")
        return LLMCallError(f"{e} | body: {str(body)[:500]}")
    return LLMCallError(str(e))


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
        if env is None:
            env = {**_read_dotenv(), **os.environ}  # .env 打底，环境变量优先
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

    @classmethod
    def from_pi(
        cls,
        *,
        default_provider: str = "novita-gpt",
        role_map: dict[str, str] | None = None,
        pi_dir: Path | None = None,
    ) -> LLMRouter:
        """直接复用 pi 的 provider 配置（~/.pi/agent/）作为单一真相源。

        只收录 openai-completions 协议的 provider；多模型 provider
        额外展开 <provider>:<model_id> 别名键。其余协议（如 anthropic-messages）跳过。
        """
        pi_dir = pi_dir or (Path.home() / ".pi" / "agent")
        specs: dict[str, ProviderSpec] = {}
        models_file, auth_file = pi_dir / "models.json", pi_dir / "auth.json"
        if models_file.exists() and auth_file.exists():
            providers = json.loads(models_file.read_text()).get("providers", {})
            auth = json.loads(auth_file.read_text())
            for name, prov in providers.items():
                if prov.get("api") != "openai-completions":
                    continue  # 非 OpenAI 协议暂不接入（fail-closed）
                key = (auth.get(name) or {}).get("key")
                models = prov.get("models") or []
                base = (prov.get("baseUrl") or "").rstrip("/")
                if not key or not models or not base:
                    continue
                specs[name] = ProviderSpec(
                    name=name, api_key=key, base_url=base, model=models[0]["id"]
                )
                for m in models:  # 全部模型建别名键（含首个），角色路由精确到模型
                    specs[f"{name}:{m['id']}"] = ProviderSpec(
                        name=f"{name}:{m['id']}", api_key=key, base_url=base, model=m["id"]
                    )
        default_role_map = {"fast": "dashscope:kimi-k3"}
        return cls(specs, default_provider=default_provider, role_map=role_map or default_role_map)

    def providers(self) -> list[str]:
        """已注册的 provider/别名键清单。"""
        return sorted(self._specs)

    def get(self, role: str | None = None, *, tool_schemas: dict[str, dict] | None = None) -> LLM:
        # 优先级：显式角色路由 > provider 名/别名直取 > 默认
        provider = self._role_map.get(role or "")
        if provider is None:
            provider = role if role in self._specs else self._default
        spec = self._specs.get(provider)
        if spec is None:
            raise ProviderConfigError(
                f"provider {provider!r} 未配置（需要 {provider.upper()}_API_KEY/BASE_URL/MODEL 三件套）"
            )
        return OpenAICompatLLM(spec, tool_schemas=tool_schemas)
