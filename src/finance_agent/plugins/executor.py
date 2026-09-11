"""ToolExecutor：参数/结果校验、超时、重试（吃预算）、错误码、trace。

方案 §6.2 的宿主验收义务：
- 重试也扣预算（RunBudget.admit_retry）；
- 错误不被包装为空结果（error envelope 带 error_code/retryable，与 empty 区分）；
- 写入使用幂等键（由 handler/受控 writer 负责；executor 保证同参数重放的
  trace 可归因）；
- 每次执行落 tool/plugin_trace 事件（plugin/tool/耗时/结果/错误码），
  失败可见性三通道的事件通道。

边界：超时用工作线程实现——到点返回 timeout 错误，但**不杀**在飞的 handler
线程（Python 限制）；网络型 handler 自身应带 socket 超时（adapter 已有）。
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..eventstore.events import Event
from ..eventstore.store import EventStore
from .contracts import ToolDefinition

logger = logging.getLogger("finance_agent.plugins.executor")

#: 工具执行 trace 事件（与 kernel 的 tool/result 并行：这里记插件维度归因）
PLUGIN_TOOL_TRACE = "plugins/tool_trace"

#: 重试的错误码（其余错误立即返回——重试无益的不烧预算）
_RETRYABLE_CODES = frozenset({"timeout", "handler_unavailable", "transient_error"})


class ToolExecutor:
    """薄执行层：校验 → 超时执行 → 有界重试 → 错误封装 → trace。"""

    def __init__(
        self, *, events: EventStore | None = None, budget: Any | None = None,
        run_id: str = "", max_retries: int = 1,
        pool: ThreadPoolExecutor | None = None,
    ):
        self._events = events
        self._budget = budget
        self._run_id = run_id
        self._max_retries = max_retries
        # 缺省自建小池（测试/独立使用）；生产经 RuntimeBinder 传进程级共享池
        # （有界 32 worker，serve 模式不随 run 数泄漏线程）
        self._pool = pool or ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="plugin-tool")

    # ---------------- 校验 ----------------

    @staticmethod
    def validate_arguments(tool: ToolDefinition, args: Any) -> str | None:
        """轻量参数校验（required + 顶层类型）；返回 None = 通过，否则错误说明。

        不做完整 JSON Schema 求值（宿主无 jsonschema 依赖）：required 缺失与
        顶层类型错配已覆盖绝大多数模型误用；深层形状由各 handler 的服务端
        门禁兜底（fail-loud，不会静默通过）。
        """
        if not isinstance(args, dict):
            return f"参数必须是 object（收到 {type(args).__name__}）"
        params = (tool.schema_ or {}).get("parameters") or {}
        required = params.get("required") or []
        missing = [r for r in required if r not in args or args[r] in (None, "")]
        if missing:
            return f"缺少必填参数 {missing}（schema required={required}）"
        props = params.get("properties") or {}
        type_map = {
            "string": str, "integer": int, "number": (int, float),
            "boolean": bool, "array": list, "object": dict,
        }
        for name, value in args.items():
            spec = props.get(name)
            if not spec or value is None:
                continue
            expected = type_map.get(str(spec.get("type") or ""))
            if expected is None:
                continue
            if isinstance(expected, tuple):
                ok = isinstance(value, expected) and not isinstance(value, bool)
            elif expected is int:
                ok = isinstance(value, int) and not isinstance(value, bool)
            else:
                ok = isinstance(value, expected)
            if not ok:
                return (f"参数 {name} 类型错误：期望 {spec.get('type')}，"
                        f"收到 {type(value).__name__}")
        return None

    # ---------------- 执行 ----------------

    def execute(self, tool: ToolDefinition, args: dict[str, Any]) -> dict[str, Any]:
        """执行一次工具调用：永远返回 {content, provenance, ...} 形态（错误也是）。"""
        started = time.monotonic()
        problem = self.validate_arguments(tool, args)
        if problem is not None:
            return self._fail(tool, "invalid_arguments", problem, started, retryable=False)
        attempt = 0
        while True:
            outcome, payload = self._attempt(tool, args)
            if outcome == "ok":
                self._trace(tool, args, "ok", "", started, attempt)
                return payload
            code, message, retryable = payload
            if retryable and attempt < self._max_retries and self._admit_retry():
                attempt += 1
                logger.warning(
                    "插件工具 %s 重试 %d/%d（%s：%s）",
                    tool.name, attempt, self._max_retries, code, message[:120],
                )
                continue
            return self._fail(tool, code, message, started, retryable, attempts=attempt)

    def _admit_retry(self) -> bool:
        if self._budget is None:
            return True
        admit = getattr(self._budget, "admit_retry", None)
        if admit is None:
            return True
        ok, _reason = admit()
        return bool(ok)

    def _attempt(self, tool: ToolDefinition, args: dict[str, Any]):
        future = self._pool.submit(tool.handler, args)
        try:
            result = future.result(timeout=tool.timeout_s)
        except TimeoutError:
            return "error", ("timeout", f"工具执行超过 {tool.timeout_s:.0f}s 上限", True)
        except Exception as e:  # noqa: BLE001 - 错误封装为 envelope，不裸抛给 kernel
            transient = _looks_transient(e)
            return "error", (
                "transient_error" if transient else "handler_error",
                f"{type(e).__name__}: {e}", transient,
            )
        if not isinstance(result, dict) or "content" not in result:
            return "error", (
                "invalid_tool_result",
                f"handler 返回非法形态（需要 {{content, provenance}}，收到 "
                f"{type(result).__name__}）", False,
            )
        return "ok", result

    def _fail(
        self, tool: ToolDefinition, code: str, message: str, started: float,
        retryable: bool, attempts: int = 0,
    ) -> dict[str, Any]:
        self._trace(tool, {}, code, code, started, attempts)
        logger.warning("插件工具 %s 失败（%s）：%s", tool.name, code, message[:200])
        return {
            "content": json.dumps({
                "error_code": code, "message": message, "retryable": retryable,
                "tool": tool.name, "plugin_id": tool.plugin_id,
            }, ensure_ascii=False),
            "provenance": [],
            "error_code": code,
        }

    def _trace(
        self, tool: ToolDefinition, args: dict[str, Any], outcome: str,
        error_code: str, started: float, attempt: int,
    ) -> None:
        if self._events is None:
            return
        self._events.append(Event(
            run_id=self._run_id or "plugins",
            type=PLUGIN_TOOL_TRACE,
            payload={
                "plugin_id": tool.plugin_id, "tool": tool.name, "rw": tool.rw,
                "outcome": outcome, "error_code": error_code or None,
                "attempt": attempt,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                # 参数只记键与长度（值可能含长文本；密钥类参数不存在于 args）
                "arg_keys": sorted(args)[:20],
            },
        ))


def _looks_transient(e: Exception) -> bool:
    """连接类错误可重试；语义/校验类错误重试无益。"""
    if isinstance(e, (ConnectionError, TimeoutError)):
        return True
    name = type(e).__name__.lower()
    text = str(e).lower()
    if any(k in name for k in ("timeout", "connect", "network")):
        return True
    return any(k in text for k in (
        "429", "502", "503", "504", "rate limit", "timed out",
        "connection", "refused", "reset by peer", "temporarily", "broken pipe",
    ))


__all__ = ["ToolExecutor", "PLUGIN_TOOL_TRACE"]
