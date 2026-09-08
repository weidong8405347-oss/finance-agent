"""最小 agent kernel：稳定循环，一切优化走 hooks/tools（原则 1/2）。

一个 turn 的事件序（全部 append-only 落 EventStore）：
  turn/start → user/message → (step/start → [hooks] → assistant/message
  → [tool/call → tool/result]* → step/end)* → turn/end

不变量：发给模型的消息 == EventStore.derive_messages() 的投影——
kernel 没有任何第二条上下文通道（原则 3）。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from ..eventstore.events import (
    ASSISTANT_CHUNK,
    ASSISTANT_MESSAGE,
    RESEARCH_BUDGET,
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
        budget: Any | None = None,
    ):
        self._store = store
        self._llm = llm
        self._manifest = manifest
        self._tools = tools or {}
        self._hooks = hooks or []
        self._max_steps = max_steps
        #: RunBudget（audit §3.3）：在 LLM/工具入口真实扣减，耗尽即停本 turn。
        #: None = 不设限（旧行为兼容）。
        self._budget = budget
        self._turn = 0
        #: 预算终止原因（非 None = 本 turn 因预算提前结束，供上层归因）
        self.budget_stop: str | None = None

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
        usage_acc: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        n_calls = 0
        for step in range(1, self._max_steps + 1):
            self._emit(STEP_START, turn=turn, step=step)
            messages = self._store.derive_messages(run_id)
            watermark = self._store.head_seq(run_id)

            # 预算准入（audit §3.3）：耗尽就不发请求——不发必然超时/超额的调用
            if self._budget is not None:
                ok, reason = self._budget.admit_llm_call()
                if not ok:
                    self.budget_stop = reason
                    self._emit_budget("llm_call_denied", reason, turn=turn, step=step)
                    self._emit(STEP_END, turn=turn, step=step)
                    break
                self._apply_llm_budget()

            ctx = HookContext(manifest=self._manifest, messages=messages, run_id=run_id, turn=turn, step=step)
            for hook in self._hooks:  # 必达：泄漏审计等；拒绝即中断本 turn
                hook.before_model(ctx)

            stream = getattr(self._llm, "stream_complete", None)  # 可选能力：流式
            if stream is not None:

                def on_delta(text: str, *, _turn: int = turn, _step: int = step) -> None:
                    self._emit(ASSISTANT_CHUNK, payload={"text": text}, turn=_turn, step=_step)

                reply = stream(messages, tools=list(self._tools), on_delta=on_delta)
            else:
                reply = self._llm.complete(messages, tools=list(self._tools))
            if hasattr(self._llm, "received_seqs"):  # 测试探针：记录调用时日志水位
                self._llm.received_seqs.append(watermark)  # type: ignore[attr-defined]

            self._emit(
                ASSISTANT_MESSAGE,
                payload={
                    "content": reply.content,
                    "tool_calls": [tc.model_dump() for tc in reply.tool_calls],
                    "model": getattr(self._llm, "model_name", None),  # 过程透明：哪个模型在说
                },
                turn=turn,
                step=step,
            )
            final_content = reply.content
            if reply.usage:
                n_calls += 1
                for k in usage_acc:
                    v = reply.usage.get(k)
                    usage_acc[k] += v if isinstance(v, int) else 0  # 嵌套明细跳过
            if self._budget is not None:
                # 缺 usage 不按零计费：按上下文长度估算并单独计数（tokens_estimated）
                self._budget.record_usage(
                    reply.usage,
                    context_chars=len(str(messages)) + len(reply.content or ""),
                )

            for tc in reply.tool_calls:
                self._emit(TOOL_CALL, payload=tc.model_dump(), turn=turn, step=step)
                if self._budget is not None:
                    ok, reason = self._budget.admit_tool()
                    if not ok:
                        self.budget_stop = reason
                        self._emit_budget("tool_call_denied", reason, turn=turn, step=step)
                        self._emit(
                            TOOL_RESULT,
                            payload={"call_id": tc.call_id, "name": tc.name,
                                     "content": f"error: {reason}", "provenance": []},
                            turn=turn, step=step,
                        )
                        continue
                result = self._execute_tool(tc.name, tc.arguments)
                self._emit(
                    TOOL_RESULT,
                    payload={"call_id": tc.call_id, "name": tc.name, **result},
                    turn=turn,
                    step=step,
                )

            self._emit(STEP_END, turn=turn, step=step)
            if self.budget_stop is not None or not reply.tool_calls:
                break

        end_payload: dict[str, Any] = {}
        if n_calls:
            end_payload["usage"] = usage_acc
            end_payload["llm_calls"] = n_calls
        model_name = getattr(self._llm, "model_name", None)
        if model_name:
            end_payload["model"] = model_name
        if self.budget_stop:
            end_payload["budget_stop"] = self.budget_stop
        if self._budget is not None:
            end_payload["budget"] = self._budget.snapshot().as_payload()
        self._emit(TURN_END, payload=end_payload, turn=turn)
        return final_content

    def _apply_llm_budget(self) -> None:
        """把剩余预算下推到 LLM 客户端：单请求 timeout 不超过剩余墙钟；重试也吃预算。"""
        timeout = self._budget.llm_timeout(getattr(self._llm, "timeout", None))
        setter = getattr(self._llm, "set_request_timeout", None)
        if setter is not None and timeout:
            with contextlib.suppress(Exception):  # 客户端不支持时不阻断研究
                setter(timeout)
        gate = getattr(self._llm, "set_retry_gate", None)
        if gate is not None:
            with contextlib.suppress(Exception):
                gate(self._budget.admit_retry)

    def _emit_budget(self, action: str, reason: str, *, turn: int = 0, step: int = 0) -> None:
        """预算动作落事件（可回放：什么时候、因为哪个维度停了）。"""
        self._emit(
            RESEARCH_BUDGET,
            payload={
                "action": action, "reason": reason, "run_id": self._manifest.run_id,
                "budget": self._budget.snapshot().as_payload() if self._budget else {},
            },
            turn=turn, step=step,
        )

    def _execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # 模型输出的工具参数 JSON 损坏 → 明确错误回给模型自我修正（不熔断 turn）
        if isinstance(arguments, dict) and "__parse_error__" in arguments:
            return {"content": f"error: 工具参数 JSON 解析失败（模型输出格式错误）。"
                               f"请重新调用 {name}，arguments 用合法 JSON。",
                    "provenance": []}
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
