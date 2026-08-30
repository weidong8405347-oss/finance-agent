"""最小 agent kernel：稳定循环，一切优化走 hooks/tools（原则 1/2）。

一个 turn 的事件序（全部 append-only 落 EventStore）：
  turn/start → user/message → (step/start → [hooks] → assistant/message
  → [tool/call → tool/result]* → step/end)* → turn/end

不变量：发给模型的消息 == EventStore.derive_messages() 的投影——
kernel 没有任何第二条上下文通道（原则 3）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..eventstore.events import (
    ASSISTANT_MESSAGE,
    STEP_END,
    STEP_START,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_END,
    TURN_START,
    USER_MESSAGE,
    Event,
)
from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest
from ..llm.base import LLM
from .hooks import Hook, HookContext

ToolFn = Callable[[dict[str, Any]], dict[str, Any]]  # arguments → {"content", "provenance"?}


class AgentKernel:
    def __init__(
        self,
        *,
        store: EventStore,
        llm: LLM,
        manifest: RunManifest,
        tools: dict[str, ToolFn] | None = None,
        hooks: list[Hook] | None = None,
        max_steps: int = 8,
    ):
        self._store = store
        self._llm = llm
        self._manifest = manifest
        self._tools = tools or {}
        self._hooks = hooks or []
        self._max_steps = max_steps
        self._turn = 0

    def run_turn(self, user_input: str | None = None) -> str:
        """跑一个 turn。user_input=None：输入已由调用方落库（inbox 语义），
        kernel 只从 derive_messages 投影取上下文，不重复记录。

        turn 编号从 store 派生（已有 turn/start 数 + 1）——kernel 本身无状态，
        上下文永远来自投影（「模型可见 = 已记录」，D1 无状态重建）。
        """
        run_id = self._manifest.run_id
        turn = len(self._store.read(run_id, types={TURN_START})) + 1
        self._turn = turn
        self._emit(TURN_START, turn=turn)
        if user_input is not None:
            self._emit(USER_MESSAGE, payload={"content": user_input}, turn=turn)

        final_content = ""
        for step in range(1, self._max_steps + 1):
            self._emit(STEP_START, turn=turn, step=step)
            messages = self._store.derive_messages(run_id)
            watermark = self._store.head_seq(run_id)

            ctx = HookContext(manifest=self._manifest, messages=messages, run_id=run_id, turn=turn, step=step)
            for hook in self._hooks:  # 必达：泄漏审计等；拒绝即中断本 turn
                hook.before_model(ctx)

            reply = self._llm.complete(messages, tools=list(self._tools))
            if hasattr(self._llm, "received_seqs"):  # 测试探针：记录调用时日志水位
                self._llm.received_seqs.append(watermark)  # type: ignore[attr-defined]

            self._emit(
                ASSISTANT_MESSAGE,
                payload={
                    "content": reply.content,
                    "tool_calls": [tc.model_dump() for tc in reply.tool_calls],
                },
                turn=turn,
                step=step,
            )
            final_content = reply.content

            for tc in reply.tool_calls:
                self._emit(TOOL_CALL, payload=tc.model_dump(), turn=turn, step=step)
                result = self._execute_tool(tc.name, tc.arguments)
                self._emit(
                    TOOL_RESULT,
                    payload={"call_id": tc.call_id, "name": tc.name, **result},
                    turn=turn,
                    step=step,
                )

            self._emit(STEP_END, turn=turn, step=step)
            if not reply.tool_calls:
                break

        self._emit(TURN_END, turn=turn)
        return final_content

    def _execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        fn = self._tools.get(name)
        if fn is None:
            return {"content": f"error: 未注册的工具 {name}", "provenance": []}
        # Tool 弹性（原则 4）：工具异常 → 错误内容回给模型自我修正，不熔断整个 turn。
        # 注意：不兜底 provenance 键——缺失正是 leakage-audit 要捕获的信号
        try:
            return fn(arguments)
        except Exception as e:
            return {"content": f"error: 工具 {name} 执行失败：{type(e).__name__}: {e}"}

    def _emit(self, type_: str, payload: dict | None = None, *, turn: int = 0, step: int = 0) -> int:
        return self._store.append(
            Event(run_id=self._manifest.run_id, type=type_, payload=payload or {}, turn=turn, step=step)
        )
