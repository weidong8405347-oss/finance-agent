"""审批服务：milestone 档的人工闸（扩预算/高风险操作需人工批准）。

- request() 挂起请求并落 approval/asked 事件；
- 后台线程 wait() 阻塞等待；UI 经 SSE 看到 asked 事件后内联呈现审批卡；
- decide() 落 approval/decided 事件并放行/取消；超时 = rejected（fail-closed）；
- 豁免走 approval/waived（当次有效，带用户原话/flag 依据，可审计）——由调用方落。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..eventstore.events import APPROVAL_ASKED, APPROVAL_DECIDED, Event
from ..eventstore.store import EventStore

APPROVAL_REQUESTED = APPROVAL_ASKED  # 兼容别名（统一词汇：asked/decided）
APPROVAL_RESOLVED = APPROVAL_DECIDED


@dataclass
class ApprovalRequest:
    approval_id: str
    run_id: str
    detail: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: str = "pending"  # pending / approved / rejected
    _event: threading.Event = field(default_factory=threading.Event, repr=False)


class ApprovalService:
    def __init__(self, events: EventStore | None = None):
        self._events = events
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()

    def request(self, run_id: str, detail: dict[str, Any]) -> str:
        approval_id = f"appr-{uuid.uuid4().hex[:8]}"
        req = ApprovalRequest(approval_id=approval_id, run_id=run_id, detail=detail)
        with self._lock:
            self._requests[approval_id] = req
        self._emit(APPROVAL_REQUESTED, run_id, {"approval_id": approval_id, "detail": detail})
        return approval_id

    def wait(self, approval_id: str, timeout: float = 600.0) -> bool:
        """阻塞等待审批结果；返回 approved。超时按拒绝处理（fail-closed）。"""
        req = self._requests[approval_id]
        if not req._event.wait(timeout=timeout):
            self.decide(approval_id, False)
        return req.status == "approved"

    def decide(self, approval_id: str, approved: bool) -> None:
        with self._lock:
            req = self._requests[approval_id]
            if req.status != "pending":
                return
            req.status = "approved" if approved else "rejected"
            req._event.set()
        self._emit(
            APPROVAL_RESOLVED,
            req.run_id,
            {"approval_id": approval_id, "approved": approved},
        )

    def pending(self) -> list[ApprovalRequest]:
        with self._lock:
            return [r for r in self._requests.values() if r.status == "pending"]

    def _emit(self, type_: str, run_id: str, payload: dict) -> None:
        if self._events is not None:
            self._events.append(Event(run_id=run_id, type=type_, payload=payload))
