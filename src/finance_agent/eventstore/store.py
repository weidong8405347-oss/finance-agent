"""EventStore：append-only 事件日志，全系统唯一真相源（DESIGN.md §3.4）。

设计要点：
- 常规路径只有 append/read/derive_messages 三个动作；没有 update。
- seq 全局单调递增，是事件的全序依据。
- derive_messages() 把日志投影成模型上下文——「模型可见 = 已记录」
  由 kernel 只能从这里取上下文来保证。
- 删除是**显式例外**（用户发起的会话清理）：只有 `delete_run()` 一个入口，
  必须级联子 run，删除前先写审计记录（`session/deleted` 落到 `system-purge` run），
  且拒绍删正在跑的会话（除非 force）——不存在静默、部分或不可追溯的删除。
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
    COMMAND_RUN,
    CONTEXT_INJECT,
    MODEL_VISIBLE_TYPES,
    RUN_CREATED,
    TOOL_RESULT,
    TURN_START,
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

#: 投影/发布副作用事件：一个 run 若**只有**这些事件，它不是会话，而是脚本/服务
#: 在会话之外落的审计痕迹（典型：`migration-shadow` 只有 27 条 dossier/published、
#: `dossier` 只有快照发布）——进 Sessions 列表就是「空闲、点开什么都没有」的幽灵会话。
PROJECTION_ONLY_TYPES: frozenset[str] = frozenset({
    "dossier/published", "dossier/publish_failed", "report/published",
    "metric/asserted", "metric/revised", "calculation/completed",
    "fact/asserted", "fact/superseded", "fact/conflict_raised",
    "fact/conflict_resolved", "gateway/preflight", "hook/verdict",
    "leakage/attempt", "research/assessment", "research/artifact_created",
    "session/deleted",
})

#: 系统/脚本 run 前缀（知识维护审计、迁移影子、快照发布、评估回放）
SYSTEM_RUN_PREFIXES: tuple[str, ...] = (
    "kb-", "migration-", "dossier", "eval-", "system-", "live-gateway",
)

#: 删除审计记录落的 run（不属于任何会话，也不进列表）
PURGE_AUDIT_RUN = "system-purge"

#: 子 run 命名约定的嵌套分隔符（`<会话>--<step>`，如 live-xxx--cmd-1-1-research）
RUN_NESTING_SEP = "--"


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

    def unsubscribe(self, fn: Callable[[StoredEvent], None]) -> None:
        """退订（command 进度桥接等临时订阅的清理）。"""
        with contextlib.suppress(ValueError):
            self._subscribers.remove(fn)

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

    # ---------------- 会话识别与显式删除（用户发起的清理） ----------------

    def list_runs(self) -> list[dict[str, Any]]:
        """全部 run 的聚合投影（run_id / 事件数 / 首末时刻）。"""
        rows = self._conn.execute(
            "SELECT run_id, COUNT(*), MIN(ts), MAX(ts) FROM events"
            " GROUP BY run_id ORDER BY MAX(ts) DESC"
        ).fetchall()
        return [
            {"run_id": r[0], "event_count": r[1], "started_at": r[2], "last_active": r[3]}
            for r in rows
        ]

    def child_run_ids(self) -> set[str]:
        """子 run 集合：不单独进会话列表。

        两条识别路径（任一命中即算子 run）：
        1. `run/created` 带 parent_run_id（正规路径）；
        2. run_id 含嵌套分隔符 `--`（命名约定 `<会话>--<step>`）——兼容历史上
           漏落 run/created 的子 run（实测：委员会 CIO 综合 run 就是这样
           变成幽灵会话的）。
        """
        rows = self._conn.execute(
            "SELECT DISTINCT run_id FROM events WHERE type = ?"
            " AND json_extract(payload, '$.parent_run_id') IS NOT NULL",
            (RUN_CREATED,),
        ).fetchall()
        linked = {r[0] for r in rows}
        nested = {
            r["run_id"] for r in self.list_runs() if RUN_NESTING_SEP in r["run_id"]
        }
        return linked | nested

    def is_session_run(self, run_id: str) -> bool:
        """是否是真正的会话（而不是投影/迁移/维护副作用 run）。

        两条判据，任一命中即排除：
        1. 系统 run 前缀（kb- / migration- / dossier / eval- / system- / live-gateway）；
        2. 全部事件都是投影/发布副作用（PROJECTION_ONLY_TYPES）——这种 run 没有
           任何可读的对话/命令内容，点开必然空白。
        """
        if run_id.startswith(SYSTEM_RUN_PREFIXES):
            return False
        placeholders = ",".join("?" for _ in PROJECTION_ONLY_TYPES)
        row = self._conn.execute(
            f"SELECT 1 FROM events WHERE run_id = ? AND type NOT IN ({placeholders}) LIMIT 1",
            (run_id, *sorted(PROJECTION_ONLY_TYPES)),
        ).fetchone()
        return row is not None

    def session_runs(self) -> list[dict[str, Any]]:
        """会话列表（排除子 run 与系统/空壳 run）。"""
        children = self.child_run_ids()
        return [
            r for r in self.list_runs()
            if r["run_id"] not in children and self.is_session_run(r["run_id"])
        ]

    def is_active(self, run_id: str) -> bool:
        """会话是否还在跑（有未闭合的 command 或 turn）——删除前必须先停。"""
        open_commands: set[str] = set()
        open_turns = 0
        for e in self.read(run_id):
            if e.type == COMMAND_RUN:
                open_commands.add(str(e.payload.get("command_id") or ""))
            elif e.type == "command/done":
                open_commands.discard(str(e.payload.get("command_id") or ""))
            elif e.type == TURN_START:
                open_turns += 1
            elif e.type == "turn/end":
                open_turns = max(0, open_turns - 1)
        return bool(open_commands) or open_turns > 0

    def delete_run(self, run_id: str, *, cascade: bool = True, reason: str = "") -> dict[str, Any]:
        """删除一个 run 的全部事件（显式、可审计、级联子 run）。

        纪律：
        - 删除后写 `session/deleted` 审计事件到 `system-purge` run（记录被删 run、
          事件数、时间范围、原因）——删除本身可追溯，不静默；
        - cascade=True（默认）同时删子 run（step agent / 维度组 worker），
          否则会话列表会留下一堆无父孤儿；
        - 正在跑的会话由调用方先 stop（本方法不猜意图，只按 force 执行）。
        """
        targets = [run_id]
        if cascade:
            targets += sorted(
                r["run_id"] for r in self.list_runs()
                if r["run_id"].startswith(f"{run_id}{RUN_NESTING_SEP}")
            )
        deleted: dict[str, int] = {}
        ranges: dict[str, list[str]] = {}
        with self._lock:
            for target in targets:
                row = self._conn.execute(
                    "SELECT COUNT(*), MIN(ts), MAX(ts) FROM events WHERE run_id = ?", (target,)
                ).fetchone()
                if not row or not row[0]:
                    continue
                deleted[target] = int(row[0])
                ranges[target] = [row[1] or "", row[2] or ""]
                self._conn.execute("DELETE FROM events WHERE run_id = ?", (target,))
            self._conn.commit()
        total = sum(deleted.values())
        if total:
            self.append(Event(
                run_id=PURGE_AUDIT_RUN,
                type="session/deleted",
                payload={
                    "run_id": run_id, "deleted_runs": sorted(deleted),
                    "event_counts": deleted, "ts_ranges": ranges,
                    "total_events": total, "reason": reason,
                },
            ))
        return {"run_id": run_id, "deleted_runs": sorted(deleted),
                "total_events": total, "counts": deleted}

    def close(self) -> None:
        self._conn.close()
