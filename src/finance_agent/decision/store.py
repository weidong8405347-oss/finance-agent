"""DecisionStore：决策卡持久化（append-only；改卡 = 新卡）。"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .card import DecisionCard

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    card_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    conviction INTEGER NOT NULL,
    horizon TEXT NOT NULL,
    card_json TEXT NOT NULL,
    kb_snapshot_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    run_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_ns ON decisions(namespace, entity_kind, entity_id, created_at);
"""


class DecisionStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def insert(self, card: DecisionCard, *, namespace: str = "prod") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO decisions (card_id, namespace, entity_kind, entity_id, action,"
                " conviction, horizon, card_json, kb_snapshot_id, created_at, run_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    card.card_id,
                    namespace,
                    card.subject.kind,
                    card.subject.id,
                    card.action.value,
                    card.conviction,
                    card.horizon.value,
                    card.model_dump_json(),
                    card.kb_snapshot_id,
                    card.created_at.isoformat(),
                    card.run_id,
                ),
            )
            self._conn.commit()

    def get(self, card_id: str) -> DecisionCard:
        row = self._conn.execute(
            "SELECT card_json FROM decisions WHERE card_id = ?", (card_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"未知决策卡: {card_id}")
        return DecisionCard(**json.loads(row[0]))

    def list(self, *, namespace: str = "prod", entity_id: str | None = None) -> list[DecisionCard]:
        sql = "SELECT card_json FROM decisions WHERE namespace = ?"
        params: list = [namespace]
        if entity_id:
            sql += " AND entity_id = ?"
            params.append(entity_id)
        sql += " ORDER BY created_at"
        rows = self._conn.execute(sql, params).fetchall()
        return [DecisionCard(**json.loads(r[0])) for r in rows]

    def close(self) -> None:
        self._conn.close()
