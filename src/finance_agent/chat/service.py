"""ChatService：会话 turn 的认领与串行化（inbox 语义，Q1 异步唤醒 + D1 无状态重建）。

- 输入先落库（API 落 user/message；command 完成时 wake() 落 context/inject）；
- per-run 串行 drain：同一会话的输入排队，一个 turn 处理一批，直到没有未认领输入；
- 失败三通道：turn 异常 → turn/error 事件（用户可见）+ 日志（运维可见）+ 状态投影（API）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from ..eventstore.events import CONTEXT_INJECT, USER_MESSAGE, Event
from ..eventstore.store import EventStore

logger = logging.getLogger("finance_agent.chat")

MainAgentFactory = Callable[[str], "MainAgentLike"]


class MainAgentLike:
    def run_turn(self) -> str: ...  # pragma: no cover - 协议占位


class ChatService:
    def __init__(
        self,
        *,
        events: EventStore,
        make_main_agent: MainAgentFactory,
    ):
        self._events = events
        self._make_agent = make_main_agent
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._consumed: dict[str, int] = {}  # run_id → 已认领到的 seq 水位

    # ---------------- 入口 ----------------

    def begin_session(self, run_id: str) -> None:
        """新会话创建时调用：先落 system 契约（保证模型上下文首条 = 契约）。"""
        self._make_agent(run_id).ensure_contract()  # type: ignore[attr-defined]

    def submit_message(self, run_id: str) -> None:
        """user/message 已落库（API 职责）；安排认领。"""
        self._schedule(run_id)

    def wake(self, run_id: str, content: str) -> None:
        """系统事件唤醒主 agent（command/done 汇报等）：落 context/inject 并认领。"""
        self._events.append(
            Event(run_id=run_id, type=CONTEXT_INJECT, payload={"role": "user", "content": content})
        )
        self._schedule(run_id)

    # ---------------- 内部 ----------------

    def _pending(self, run_id: str) -> bool:
        watermark = self._consumed.get(run_id, 0)
        for e in self._events.read(run_id):
            if e.seq <= watermark:
                continue
            if e.type == USER_MESSAGE:
                return True
            if e.type == CONTEXT_INJECT and e.payload.get("role", "user") == "user":
                return True
        return False

    def _schedule(self, run_id: str) -> None:
        with self._lock:
            if run_id in self._active:
                return
            self._active.add(run_id)
        threading.Thread(target=self._drain, args=(run_id,), daemon=True).start()

    def _drain(self, run_id: str) -> None:
        try:
            while self._pending(run_id):
                try:
                    self._make_agent(run_id).run_turn()
                except Exception as e:  # 失败不得静默：turn/error 事件 + 日志
                    logger.exception("main agent turn failed (run=%s)", run_id)
                    self._events.append(
                        Event(run_id=run_id, type="turn/error", payload={"reason": str(e)})
                    )
                self._consumed[run_id] = self._events.head_seq(run_id)
        finally:
            with self._lock:
                self._active.discard(run_id)
            # 关窗竞态：discard 后到达的输入重新调度
            if self._pending(run_id):
                self._schedule(run_id)
