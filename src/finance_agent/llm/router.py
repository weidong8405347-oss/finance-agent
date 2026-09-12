"""LLMRouter：多 provider 接入（移植 V1.0 的 env 三件套约定）。

每家 provider 三件套：<NAME>_API_KEY / <NAME>_BASE_URL / <NAME>_MODEL，
统一走 OpenAI 兼容协议。按 agent 角色路由 provider（config 的 role_map）。
fail-closed：三件套不齐 = 该 provider 不存在（ProviderConfigError）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .base import LLM, AssistantReply, OnDelta, ToolCall

Transport = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]

_KNOWN_PROVIDERS = ("openai", "anthropic", "zhipuai", "deepseek")

#: LLM 读超时默认值（大上下文研究轮流式生成可超 60s；失败成本不对称，宁等勿断）
DEFAULT_LLM_TIMEOUT = 180.0
#: 读超时环境变量覆盖（from_env/from_pi 均识别；显式 timeout 参数优先）
TIMEOUT_ENV_VAR = "FINANCE_AGENT_LLM_TIMEOUT"
#: 重试次数覆盖（429/5xx/瞬断的退避重试；测试可设 1 加速失败面）
RETRY_ENV_VAR = "FINANCE_AGENT_LLM_RETRY_ATTEMPTS"


def _resolve_timeout(explicit: float | None, env: Mapping[str, str]) -> float:
    """显式参数 > 环境变量 > 默认值。环境变量非法值 fail-loud（配置错误不该被静默吞掉）。"""
    if explicit is not None:
        return explicit
    raw = env.get(TIMEOUT_ENV_VAR)
    if raw is None or raw.strip() == "":
        return DEFAULT_LLM_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{TIMEOUT_ENV_VAR}={raw!r} 不是合法秒数") from None


class ProviderConfigError(Exception):
    """provider 三件套未配置齐全（fail-closed）。"""


#: 网关桥瞬时 400 的响应体标记（2026-09-12 实测：novita→Bedrock 翻译层抖动——
#: 同一请求体重放即 200，错误体每次都含 Bedrock Runtime/ValidationException；
#: 真·参数错误（模型名/schema/余额）不携带这些标记，保持 fail-loud 不重试）
_GATEWAY_400_MARKERS = ("Bedrock Runtime", "ValidationException", "InvokeModelWithResponseStream")


def _is_retryable(e: Exception) -> bool:
    """可重试的错误：429 / 5xx / 连接级错误；4xx 参数类错误重试无意义——
    例外：携带网关桥标记的 400（Bedrock 翻译层抖动，重放同请求可成功）可重试。"""
    resp = getattr(e, "response", None)
    status = getattr(resp, "status_code", None)
    if status is None:
        return True  # 连接断开/超时等传输层错误
    if status == 429 or status >= 500:
        return True
    if status == 400 and resp is not None:
        try:
            body = resp.text or ""
        except Exception:  # noqa: BLE001 - 流式响应未读时 .text 抛 ResponseNotRead
            body = ""
        return any(m in body for m in _GATEWAY_400_MARKERS)
    return False


def _retry_after_s(e: Exception) -> float | None:
    resp = getattr(e, "response", None)
    if resp is None:
        return None
    try:
        raw = resp.headers.get("Retry-After")
        return float(raw) if raw else None
    except Exception:
        return None


def _with_retry(fn, *, attempts: int = 4, base: float = 2.0, gate=None):
    """限流/瞬断重试（指数退避 + Retry-After 遵从 + 抖动）。

    并行 worker 架构的必需品：N 路并发打同一 provider 必然偶发 429，
    没有退避重试的 fan-out 是脆弱的（2026-09-01 实测 dashscope 429 即死）。

    gate（audit §3.3）：重试也吃预算——每次重试前问 RunBudget，不允就报错上浮，
    不让退避 sleep 把剩余墙钟吃掉。
    """
    import random
    import time

    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            if i == attempts - 1 or not _is_retryable(e):
                raise
            if gate is not None and not gate():
                raise
            delay = _retry_after_s(e) or (base * (2 ** i) + random.uniform(0, 0.5))
            time.sleep(min(delay, 60.0))


class LLMCallError(Exception):
    """LLM 调用失败（HTTP 错误必须携带响应体——400 的真实原因在 body 里）。"""


def _parse_tool_arguments(raw: str) -> dict[str, Any]:
    """工具参数 JSON 容错解析（2026-09-01 实测：flash 模型偶发输出坏 JSON，
    不能把整个 turn 烧死——转成 __parse_error__ 占位，内核回给模型自我修正）。"""
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"__parse_error__": (raw or "")[:200]}


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
    #: reasoning effort 档位（如 "max"）；None = 不下发该参数（provider 默认档）。
    #: 仅按角色显式配置才下发——未知 provider 乱发参数会被 400 拒（fail-loud 可诊断）。
    effort: str | None = None


class OpenAICompatLLM:
    """OpenAI 兼容 chat/completions 客户端。transport 可注入（测试/离线）。"""

    def __init__(
        self,
        spec: ProviderSpec,
        *,
        transport: Transport | None = None,
        tool_schemas: dict[str, dict] | None = None,
        timeout: float = DEFAULT_LLM_TIMEOUT,
        retry_attempts: int | None = None,
    ):
        self.spec = spec
        self._transport = transport or self._httpx_transport
        self._tool_schemas = tool_schemas or {}
        self._timeout = timeout
        # 重试预算：显式参数 > 环境变量 > 默认 4
        if retry_attempts is not None:
            self._retry_attempts = retry_attempts
        else:
            raw = os.environ.get(RETRY_ENV_VAR)
            self._retry_attempts = max(1, int(raw)) if raw and raw.isdigit() else 4
        #: 重试准入闸（RunBudget 注入；None = 不设限）
        self._retry_gate: Callable[[], bool] | None = None

    @property
    def model_name(self) -> str:
        """模型标识（过程透明：事件与 UI 展示用）。"""
        return self.spec.model

    @property
    def timeout(self) -> float:
        """当前单请求读超时（秒）。"""
        return self._timeout

    def set_request_timeout(self, cap: float) -> None:
        """把单请求 timeout 向下钳到剩余预算（audit §3.3）。

        只降不升：角色配置的 timeout（如 research 300s）不得因预算宽松而被括大。
        """
        if cap and cap > 0:
            self._timeout = min(self._timeout, float(cap))

    def set_retry_gate(self, gate: Callable[[], bool] | None) -> None:
        """注入重试准入闸（RunBudget.admit_retry）：重试也吃预算。"""
        self._retry_gate = gate

    def complete(self, messages: list[dict[str, Any]], tools: list[str]) -> AssistantReply:
        body: dict[str, Any] = {"model": self.spec.model, "messages": to_openai_messages(messages)}
        if self.spec.effort:
            body["reasoning_effort"] = self.spec.effort
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
            # 重试在调用点（策略层）：注入式 transport 也享受 429/5xx 退避
            resp = _with_retry(
                lambda: self._transport(url, headers, body), attempts=self._retry_attempts,
                gate=self._retry_gate,
            )
        except Exception as e:
            raise _normalize_llm_error(e) from e
        msg = resp["choices"][0]["message"]
        tool_calls = [
            ToolCall(
                call_id=tc["id"],
                name=tc["function"]["name"],
                arguments=_parse_tool_arguments(tc["function"].get("arguments") or "{}"),
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
        if self.spec.effort:
            body["reasoning_effort"] = self.spec.effort
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
        import random
        import time

        import httpx  # lazy

        for attempt in range(self._retry_attempts):  # 限流/瞬断重试（同 complete 路径纪律）
            content_parts.clear()
            tc_acc.clear()
            try:
                with httpx.stream(
                    "POST", url, headers=headers, json=body, timeout=self._timeout
                ) as resp:
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError:
                        # 流式错误体必须先 read 再取（否则 .text 抛 ResponseNotRead，
                        # 真实 400 原因被掩盖成 <unread streaming response>——
                        # 2026-09-12 合成 400 排查即被此阻塞）
                        import contextlib as _cl

                        with _cl.suppress(Exception):
                            resp.read()
                        raise
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
                            slot = tc_acc.setdefault(
                                tc_delta["index"], {"id": "", "name": "", "arguments": ""}
                            )
                            if tc_delta.get("id"):
                                slot["id"] = tc_delta["id"]
                            fn = tc_delta.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                break  # 成功
            except Exception as e:
                # 重试边界（2026-09-12 合成断流事故，novita 长流式生成被中途切断）：
                # assistant/chunk 事件只保 UI replay 保真、不进模型上下文（derive_messages
                # 只消费 assistant/message），因此传输层断流重试不会让模型看到重复文本；
                # 代价仅是 UI 重放出现残影（可接受）——长结构提交（submit_structures
                # 大 JSON）被切断即整个 run 失败，run 存活优先。4xx 参数错误不重试。
                if attempt == self._retry_attempts - 1 or not _is_retryable(e):
                    raise _normalize_llm_error(e) from e
                if self._retry_gate is not None and not self._retry_gate():
                    raise _normalize_llm_error(e) from e  # 重试预算耗尽：不退避硬等
                delay = _retry_after_s(e) or (2.0 * (2 ** attempt) + random.uniform(0, 0.5))
                time.sleep(min(delay, 60.0))

        return AssistantReply(
            content="".join(content_parts),
            tool_calls=[
                ToolCall(
                    call_id=slot["id"] or f"call-{i}",
                    name=slot["name"],
                    arguments=_parse_tool_arguments(slot["arguments"]),
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
            err = LLMCallError(f"{e} | body: {resp.text[:500]}")
            err.response = resp  # type: ignore[attr-defined] - 重试判定要 status_code
            raise err from e
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
    if isinstance(e, LLMCallError):
        return e  # 已归一化（transport 内已带响应体），防双重包装
    response = getattr(e, "response", None)
    if response is not None:
        try:
            body = response.text
        except Exception:
            # 流式响应未读时 .text 抛 ResponseNotRead——不能让它掩盖原始 HTTP 错误
            # （2026-09 P2 联调实测：502 死 provider 的真实状态码被掩盖成 ResponseNotRead）
            body = "<unread streaming response>"
        return LLMCallError(f"{e} | body: {str(body)[:500]}")
    return LLMCallError(str(e))


class LLMRouter:
    def __init__(
        self,
        specs: dict[str, ProviderSpec],
        *,
        default_provider: str,
        role_map: dict[str, str] | None = None,
        role_options: dict[str, dict] | None = None,
        timeout: float = DEFAULT_LLM_TIMEOUT,
    ):
        self._specs = specs
        self._default = default_provider
        self._role_map = role_map or {}
        #: 分角色选项：{"effort": str, "timeout": float}（research-capability-upgrade §4.4）
        self._role_options = role_options or {}
        self._timeout = timeout

    @classmethod
    def from_env(
        cls,
        *,
        default_provider: str = "openai",
        role_map: dict[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
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
        return cls(
            specs,
            default_provider=default_provider,
            role_map=role_map,
            timeout=_resolve_timeout(timeout, env),
        )

    @classmethod
    def from_pi(
        cls,
        *,
        default_provider: str = "novita-gpt",
        role_map: dict[str, str] | None = None,
        role_options: dict[str, dict] | None = None,
        pi_dir: Path | None = None,
        timeout: float | None = None,
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
        # 默认角色路由（2026-09 裁决修订链：主力位实测 kimi-k3/GLM-5.3@max 超 pa/gpt-5.6-sol
        # → 后修订为「调研规划最关键位用 claude-opus-5，gpt-5.6-sol 做异构交叉补充」）。
        # 优先级：research = opus-5 > kimi-k3；research-alt = gpt-5.6-sol > GLM-5.3；
        # judge = gpt-5.6-sol（独立裁判）；fast = GLM-5.3 默认档。
        # 全部条件化：别名不存在时不动上一层默认（缺 provider 不该硬指）。
        default_role_map: dict[str, str] = {}
        default_role_options: dict[str, dict] = {}
        if "dashscope:kimi-k3" in specs:
            default_role_map["research"] = "dashscope:kimi-k3"
            default_role_options["research"] = {"effort": "max", "timeout": 300.0}
            default_role_map["fast"] = "dashscope:kimi-k3"
        if "dashscope:ZHIPU/GLM-5.3" in specs:
            default_role_map["fast"] = "dashscope:ZHIPU/GLM-5.3"
            # 第二强模型兜底（P4 双强交叉）：GLM-5.3 @ max 档
            default_role_map["research-alt"] = "dashscope:ZHIPU/GLM-5.3"
            default_role_options["research-alt"] = {"effort": "max", "timeout": 300.0}
        # GPT 异构补强（2026-09 裁决修订：主力 research 位实测 kimi/GLM 更强，不动；
        # research-alt 与 judge 改用 GPT 家族——双强综合与独立裁判吃异构先验，
        # 同源模型交叉验证/自我裁判会打折）。judge 未接线时调用方回落 research。
        # novita 实测：reasoning_effort 不支持 "max"（400 列明 none/low/medium/high/xhigh），
        # 最高档取 "xhigh"。
        if "novita-gpt:pa/gpt-5.6-sol" in specs:
            default_role_map["research-alt"] = "novita-gpt:pa/gpt-5.6-sol"
            default_role_options["research-alt"] = {"effort": "xhigh", "timeout": 300.0}
            default_role_map["judge"] = "novita-gpt:pa/gpt-5.6-sol"
            default_role_options["judge"] = {"effort": "xhigh", "timeout": 300.0}
        # 调研规划主力位（最关键位用最强模型）：claude-opus-5 在场 → research 交给 opus-5，
        # kimi-k3 降为缺位兜底。novita 实测 opus-5 接受 reasoning_effort（max/xhigh 均不 400）。
        if "novita-gpt:anthropic/claude-opus-5" in specs:
            default_role_map["research"] = "novita-gpt:anthropic/claude-opus-5"
            default_role_options["research"] = {"effort": "max", "timeout": 300.0}
        # 三 flash worker 池（Q4 裁决：重吞吐轻判断的维度研究并行，三源分工/冗余）。
        # 条件化：别名不存在时回落到已配置的 flash，宁重复不悬空（worker 绝不该落到强模型价）。
        _flashes = ("dashscope:deepseek-v4-flash-0731", "dashscope:ZHIPU/GLM-5.3-Flash",
                    "dashscope:qwen3.8-flash")
        available = [f for f in _flashes if f in specs]
        if available:
            for i in range(3):
                default_role_map[f"research-worker-{i + 1}"] = (
                    available[i] if i < len(available) else available[0]
                )
        return cls(
            specs,
            default_provider=default_provider,
            role_map=role_map or default_role_map,
            role_options=role_options or default_role_options,
            timeout=_resolve_timeout(timeout, os.environ),
        )

    @classmethod
    def from_config(
        cls,
        path: Path,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> LLMRouter:
        """finance-agent 自有 provider 配置（research-capability-upgrade §4.4 前端可自配的后端）。

        文件存在即优先于 pi 配置（cli._router 的优先级链：自有配置 > pi > .env）。
        格式：
          {
            "providers": {"<name>": {"base_url": "...", "api_key": "env:VAR 或明文",
                                      "models": ["model-id", ...]}},
            "default_provider": "<name>",
            "role_map": {"research": "<name>:<model-id>"},
            "role_options": {"research": {"effort": "max", "timeout": 300}}
          }
        api_key 支持 "env:VAR" 间接引用（避免明文落盘）；fail-closed：
        JSON 损坏 / provider 缺字段 / env 变量不存在 → ProviderConfigError。
        """
        if env is None:
            env = os.environ
        try:
            cfg = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ProviderConfigError(f"LLM 配置文件不可读/非法 JSON：{path}（{e}）") from e
        specs: dict[str, ProviderSpec] = {}
        for name, prov in (cfg.get("providers") or {}).items():
            base = (prov.get("base_url") or "").rstrip("/")
            key = prov.get("api_key") or ""
            models = prov.get("models") or []
            if key.startswith("env:"):
                key = env.get(key[4:], "")
            if not base or not key or not models:
                raise ProviderConfigError(
                    f"provider {name!r} 配置不全（需要 base_url/api_key/models 三项；"
                    "api_key 用 env:VAR 时该环境变量必须存在）"
                )
            specs[name] = ProviderSpec(name=name, api_key=key, base_url=base, model=models[0])
            for mid in models:  # 全部模型建别名键（含首个），角色路由精确到模型
                specs[f"{name}:{mid}"] = ProviderSpec(
                    name=f"{name}:{mid}", api_key=key, base_url=base, model=mid
                )
        if not specs:
            raise ProviderConfigError(f"LLM 配置文件没有任何 provider：{path}")
        return cls(
            specs,
            default_provider=cfg.get("default_provider") or next(iter(specs)),
            role_map=cfg.get("role_map") or {},
            role_options=cfg.get("role_options") or {},
            timeout=_resolve_timeout(timeout, env),
        )

    def providers(self) -> list[str]:
        """已注册的 provider/别名键清单。"""
        return sorted(self._specs)

    def describe(self) -> dict[str, Any]:
        """配置自配页用的脱敏视图（P5）：api_key 永不外泄，只报是否已配置。

        specs 含主键与别名键（name 与 name:model-id）——视图按主键聚合 models。
        """
        providers: list[dict[str, Any]] = []
        for key, spec in self._specs.items():
            if ":" in key:  # 别名键，models 由主键行汇总
                continue
            models = sorted(
                {spec.model, *(s.model for k, s in self._specs.items() if k.startswith(f"{key}:"))}
            )
            providers.append({
                "name": spec.name,
                "base_url": spec.base_url,
                "models": models,
                "has_key": bool(spec.api_key),
            })
        return {
            "providers": sorted(providers, key=lambda p: p["name"]),
            "default_provider": self._default,
            "role_map": dict(self._role_map),
            "role_options": dict(self._role_options),
        }

    def has_role(self, role: str) -> bool:
        """角色是否有显式路由或同名 provider（装配层据此选兜底）。"""
        return role in self._role_map or role in self._specs

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
        opts = self._role_options.get(role or "") or {}
        if opts.get("effort"):
            spec = replace(spec, effort=str(opts["effort"]))
        role_timeout = float(opts["timeout"]) if opts.get("timeout") else self._timeout
        return OpenAICompatLLM(spec, tool_schemas=tool_schemas, timeout=role_timeout)
