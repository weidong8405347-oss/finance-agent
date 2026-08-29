"""EventStore：append-only 事件日志，全系统唯一真相源（DESIGN.md §3.4）。

设计要点：
- 只有 append/read/derive_messages 三个动作；没有 update/delete。
- seq 全局单调递增，是事件的全序依据。
- derive_messages() 把日志投影成模型上下文——「模型可见 = 已记录」
  由 kernel 只能从这里取上下文来保证。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from .events import (
    ASSISTANT_MESSAGE,
    CONTEXT_INJECT,
    MODEL_VISIBLE_TYPES,
    TOOL_RESULT,
    USER_MESSAGE,
    Event,
    StoredEvent,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    turn INTEGER NOT NULL DEFAULT 0,
    step INTEGER NOT NULL DEFAULT 0,
    correlation_id TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
"""


class EventStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[StoredEvent], None]] = []
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---------------- 写（唯一入口） ----------------

    def append(self, event: Event) -> int:
        """追加事件，返回全局单调 seq。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (run_id, type, payload, turn, step, correlation_id, ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.run_id,
                    event.type,
                    json.dumps(event.payload, ensure_ascii=False, default=str),
                    event.turn,
                    event.step,
                    event.correlation_id,
                    event.ts.isoformat(),
                ),
            )
            self._conn.commit()
            seq = int(cur.lastrowid)  # type: ignore[arg-type]

        if self._subscribers:
            stored = StoredEvent(
                seq=seq, run_id=event.run_id, type=event.type, payload=event.payload,
                turn=event.turn, step=event.step, correlation_id=event.correlation_id,
                ts=event.ts,
            )
            for fn in self._subscribers:
                # 订阅者（如日志镜像）异常不得反噬真相源
                with contextlib.suppress(Exception):
                    fn(stored)
        return seq

    # ---------------- 订阅（错误镜像等运维通道） ----------------

    def subscribe(self, fn: Callable[[StoredEvent], None]) -> None:
        """订阅新事件（append 提交后同步回调）。订阅者异常绝不反噬落库。"""
        self._subscribers.append(fn)

    # ---------------- 读 ----------------

    def head_seq(self, run_id: str) -> int:
        """当前 run 的日志水位（最大 seq；无事件为 0）。供 kernel 记录模型调用时刻。"""
        row = self._conn.execute("SELECT MAX(seq) FROM events WHERE run_id = ?", (run_id,)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def read(
        self,
        run_id: str,
        *,
        types: Iterable[str] | None = None,
        up_to_seq: int | None = None,
    ) -> list[StoredEvent]:
        sql = "SELECT seq, run_id, type, payload, turn, step, correlation_id, ts FROM events WHERE run_id = ?"
        params: list[Any] = [run_id]
        if up_to_seq is not None:
            sql += " AND seq <= ?"
            params.append(up_to_seq)
        sql += " ORDER BY seq"
        rows = self._conn.execute(sql, params).fetchall()
        out = [self._row_to_event(r) for r in rows]
        if types is not None:
            wanted = set(types)
            out = [e for e in out if e.type in wanted]
        return out

    # ---------------- 投影 ----------------

    def derive_messages(self, run_id: str, *, up_to_seq: int | None = None) -> list[dict[str, Any]]:
        """把模型可见事件投影为 chat 风格消息列表。

        - user/message → {"role": "user", ...}
        - assistant/message → {"role": "assistant", ...}
        - tool/result → {"role": "tool", ...}（含 provenance，供 leakage-audit 审计）
        - context/inject → role 由 payload 指定（默认 user）
        其他事件类型不进入模型上下文。
        """
        events = self.read(run_id, up_to_seq=up_to_seq)
        messages: list[dict[str, Any]] = []
        for e in events:
            if e.type not in MODEL_VISIBLE_TYPES:
                continue
            if e.type == USER_MESSAGE:
                messages.append({"role": "user", **e.payload})
            elif e.type == ASSISTANT_MESSAGE:
                messages.append({"role": "assistant", **e.payload})
            elif e.type == TOOL_RESULT:
                messages.append({"role": "tool", **e.payload})
            elif e.type == CONTEXT_INJECT:
                role = e.payload.get("role", "user")
                rest = {k: v for k, v in e.payload.items() if k != "role"}
                messages.append({"role": role, **rest})
        return messages

    # ---------------- 内部 ----------------

    @staticmethod
    def _row_to_event(row: tuple) -> StoredEvent:
        seq, run_id, type_, payload, turn, step, correlation_id, ts = row
        return StoredEvent(
            seq=seq,
            run_id=run_id,
            type=type_,
            payload=json.loads(payload),
            turn=turn,
            step=step,
            correlation_id=correlation_id,
            ts=datetime.fromisoformat(ts),
        )

    def close(self) -> None:
        self._conn.close()
