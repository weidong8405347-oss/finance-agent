"""Hook：必达逻辑（模型不可跳过）。P0 实现 leakage-audit（防线 3 的执行点）。

协议：hook 实现 before_model(ctx) -> None；拒绝时抛 LeakageDetected。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..eventstore.events import LEAKAGE_ATTEMPT, Event
from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest, RunMode


class LeakageDetected(Exception):
    """leakage-audit 判定模型输入被污染。评估模式下本次调用必须中止。"""


@dataclass
class HookContext:
    manifest: RunManifest
    messages: list[dict[str, Any]]  # 即将发给模型的投影（来自 derive_messages）
    run_id: str
    turn: int
    step: int


class Hook(Protocol):
    def before_model(self, ctx: HookContext) -> None: ...


class LeakageAuditHook:
    """评估模式：审计投影给模型的每条工具结果的 provenance。

    - 缺 provenance → 拒绝（来源不明的数据不得进入评估上下文）；
    - available_at > eval_as_of → 拒绝（纵深防御：网关之后第二道闸）。
    生产模式不强制（生产允许使用无 PIT 保证的来源，但会留痕）。
    """

    def __init__(self, event_sink: EventStore | None = None):
        self._events = event_sink

    def before_model(self, ctx: HookContext) -> None:
        if ctx.manifest.mode is not RunMode.EVAL:
            return
        assert ctx.manifest.eval_as_of is not None
        as_of = ctx.manifest.eval_as_of
        for msg in ctx.messages:
            if msg.get("role") != "tool":
                continue
            provenance = msg.get("provenance")
            if not provenance:
                self._reject(ctx, "tool_result_missing_provenance", msg, None)
            for prov in provenance:
                raw = prov.get("available_at")
                available_at = datetime.fromisoformat(raw) if raw else None
                if available_at is None or available_at > as_of:
                    self._reject(ctx, "provenance_after_as_of", msg, prov)

    def _reject(self, ctx: HookContext, reason: str, msg: dict, prov: dict | None) -> None:
        if self._events is not None:
            self._events.append(
                Event(
                    run_id=ctx.run_id,
                    type=LEAKAGE_ATTEMPT,
                    turn=ctx.turn,
                    step=ctx.step,
                    payload={
                        "reason": reason,
                        "eval_as_of": ctx.manifest.eval_as_of.isoformat(),  # type: ignore[union-attr]
                        "provenance": prov,
                        "message_preview": str(msg.get("content"))[:200],
                    },
                )
            )
        raise LeakageDetected(f"评估上下文污染: {reason}")
