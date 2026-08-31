"""BitemporalStore：双时态事实的 append-only 存储与 as_of(T) 投影。

纪律（DESIGN.md §4.1）：
- 更新 = 新增版本（supersedes 指向前版本），旧版本永不修改、永不删除；
- 值变化 → 新版本 conflict_flag=True（竞争版本 + 冲突标记，绝不静默覆盖）；
- as_of(T) 只过滤 knowledge_time ≤ T——event_time 不参与可见性判断；
- namespace 隔离生产（prod）与评估（eval:<run_id>）写入。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .errors import ConflictError, MissingEvidenceError
from .models import Evidence, Fact, FactRecord, PitGrade

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    url TEXT,
    verbatim_quote TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    available_at TEXT,
    pit_grade TEXT NOT NULL,
    raw_ref TEXT
);
CREATE TABLE IF NOT EXISTS facts (
    fact_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value_json TEXT NOT NULL,
    event_time TEXT,
    knowledge_time TEXT NOT NULL,
    version INTEGER NOT NULL,
    supersedes TEXT,
    conflict_flag INTEGER NOT NULL DEFAULT 0,
    evidence_ids TEXT NOT NULL,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_asof ON facts(namespace, entity_kind, entity_id, field, knowledge_time);
"""


def _same_event_time(latest: FactRecord, fact: Fact) -> bool:
    """同一事件时点：两者都有 event_time 且相等，或都无 event_time（同主题静态事实）。"""
    if latest.event_time is None or fact.event_time is None:
        return latest.event_time is None and fact.event_time is None
    return latest.event_time == fact.event_time


def _canon(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class BitemporalStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---------------- 证据 ----------------

    def add_evidence(self, ev: Evidence) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO evidence (evidence_id, source_id, url, verbatim_quote,"
                    " retrieved_at, available_at, pit_grade, raw_ref) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        ev.evidence_id,
                        ev.source_id,
                        ev.url,
                        ev.verbatim_quote,
                        ev.retrieved_at.isoformat(),
                        ev.available_at.isoformat() if ev.available_at else None,
                        ev.pit_grade.value,
                        ev.raw_ref,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                raise ConflictError(f"evidence_id 重复: {ev.evidence_id}") from e

    def get_evidence(self, evidence_id: str) -> Evidence:
        row = self._conn.execute(
            "SELECT evidence_id, source_id, url, verbatim_quote, retrieved_at, available_at,"
            " pit_grade, raw_ref FROM evidence WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        if row is None:
            raise MissingEvidenceError(f"未登记的 evidence_id: {evidence_id}")
        return Evidence(
            evidence_id=row[0],
            source_id=row[1],
            url=row[2],
            verbatim_quote=row[3],
            retrieved_at=datetime.fromisoformat(row[4]),
            available_at=datetime.fromisoformat(row[5]) if row[5] else None,
            pit_grade=PitGrade(row[6]),
            raw_ref=row[7],
        )

    # ---------------- 事实（append-only 版本链） ----------------

    def assert_fact(self, fact: Fact, *, namespace: str = "prod") -> str:
        """写入新版本事实，返回 fact_id。

        - 引用的证据必须全部已登记（缺一个即拒绝，不落任何行）；
        - version = 该 (namespace, entity, field) 的最大版本 + 1；
        - supersedes 指向前一最新版本；
        - **冲突语义（2026-08-30 收窄）**：只有「同一 event_time 出现不同值」才是真冲突
          （同时点矛盾/重述）；event_time 不同 = 正常演进，不标冲突。
        """
        for eid in fact.evidence_ids:
            self.get_evidence(eid)  # raises MissingEvidenceError

        with self._lock:
            latest = self._latest_locked(namespace, fact.entity_kind, fact.entity_id, fact.field)
            version = (latest.version + 1) if latest else 1
            conflict = (
                bool(latest)
                and _canon(latest.value) != _canon(fact.value)
                and _same_event_time(latest, fact)  # 同时点才算冲突；跨期演进不算
            )
            fact_id = f"fact-{uuid.uuid4().hex[:12]}"
            self._conn.execute(
                "INSERT INTO facts (fact_id, namespace, entity_kind, entity_id, field, value_json,"
                " event_time, knowledge_time, version, supersedes, conflict_flag, evidence_ids, run_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fact_id,
                    namespace,
                    fact.entity_kind,
                    fact.entity_id,
                    fact.field,
                    _canon(fact.value),
                    fact.event_time.isoformat() if fact.event_time else None,
                    fact.knowledge_time.isoformat(),
                    version,
                    latest.fact_id if latest else None,
                    1 if conflict else 0,
                    json.dumps(fact.evidence_ids, ensure_ascii=False),
                    fact.run_id,
                ),
            )
            self._conn.commit()
        return fact_id

    # ---------------- as_of 投影 ----------------

    def view(
        self,
        entity_kind: str,
        entity_id: str,
        t: datetime,
        *,
        namespace: str = "prod",
    ) -> dict[str, FactRecord]:
        """档案视图：prod = 纯生产库；eval 命名空间 = 生产 as_of(T) + eval 增量叠加。"""
        if namespace == "prod":
            return self.as_of(entity_kind, entity_id, t, namespace="prod")
        return self.as_of_overlay(namespace, t, entity_kind, entity_id)

    def as_of_overlay(
        self,
        eval_namespace: str,
        t: datetime,
        entity_kind: str,
        entity_id: str,
    ) -> dict[str, FactRecord]:
        """eval 回放的有效档案：prod as_of(T) 叠加 eval 命名空间增量（D7）。

        字段级竞合：取 (knowledge_time, version) 更大者——「T 时点可知的一切」
        不区分是谁写入的。反向隔离不受影响（eval 写入对 prod 查询不可见）。
        """
        merged = dict(self.as_of(entity_kind, entity_id, t, namespace="prod"))
        for field, rec in self.as_of(entity_kind, entity_id, t, namespace=eval_namespace).items():
            cur = merged.get(field)
            # eval/cf 命名空间代表「更新的研究状态」，同分时赢（反事实扰动依赖此语义）
            if cur is None or (rec.knowledge_time, rec.version) >= (cur.knowledge_time, cur.version):
                merged[field] = rec
        return merged

    def as_of(
        self,
        entity_kind: str,
        entity_id: str,
        t: datetime,
        *,
        namespace: str = "prod",
    ) -> dict[str, FactRecord]:
        """实体在 t 时点的档案投影：每个 field 取 knowledge_time ≤ t 的最新版本。"""
        rows = self._conn.execute(
            "SELECT fact_id, namespace, entity_kind, entity_id, field, value_json, event_time,"
            " knowledge_time, version, supersedes, conflict_flag, evidence_ids, run_id"
            " FROM facts WHERE namespace = ? AND entity_kind = ? AND entity_id = ?"
            " AND knowledge_time <= ?",
            (namespace, entity_kind, entity_id, t.isoformat()),
        ).fetchall()
        best: dict[str, FactRecord] = {}
        for r in rows:
            rec = self._row_to_fact(r)
            cur = best.get(rec.field)
            if cur is None or (rec.knowledge_time, rec.version) > (cur.knowledge_time, cur.version):
                best[rec.field] = rec
        return best

    def history(
        self,
        entity_kind: str,
        entity_id: str,
        field: str,
        *,
        namespace: str = "prod",
    ) -> list[FactRecord]:
        rows = self._conn.execute(
            "SELECT fact_id, namespace, entity_kind, entity_id, field, value_json, event_time,"
            " knowledge_time, version, supersedes, conflict_flag, evidence_ids, run_id"
            " FROM facts WHERE namespace = ? AND entity_kind = ? AND entity_id = ? AND field = ?"
            " ORDER BY version",
            (namespace, entity_kind, entity_id, field),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def open_conflicts(
        self,
        entity_kind: str,
        entity_id: str,
        *,
        namespace: str = "prod",
    ) -> list[FactRecord]:
        rows = self._conn.execute(
            "SELECT fact_id, namespace, entity_kind, entity_id, field, value_json, event_time,"
            " knowledge_time, version, supersedes, conflict_flag, evidence_ids, run_id"
            " FROM facts WHERE namespace = ? AND entity_kind = ? AND entity_id = ?"
            " AND conflict_flag = 1 ORDER BY knowledge_time",
            (namespace, entity_kind, entity_id),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    # ---------------- 内部 ----------------

    def resolve_conflict(
        self,
        entity_kind: str,
        entity_id: str,
        field: str,
        *,
        keep_fact_id: str,
        namespace: str = "prod",
    ) -> int:
        """裁决闭环：清掉该字段全部竞争版本的 conflict_flag（保留 keep_fact_id 为当前值）。

        注意：裁决不改写历史值（append-only 不破），只是清除「待裁决」标记；
        若 keep 的不是最新版本，调用方应再写一条新事实（同值）落最新版。
        返回清除的条数。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE facts SET conflict_flag = 0"
                " WHERE namespace = ? AND entity_kind = ? AND entity_id = ? AND field = ?"
                " AND conflict_flag = 1",
                (namespace, entity_kind, entity_id, field),
            )
            self._conn.commit()
            return cur.rowcount

    def _latest_locked(self, namespace: str, kind: str, entity_id: str, field: str) -> FactRecord | None:
        row = self._conn.execute(
            "SELECT fact_id, namespace, entity_kind, entity_id, field, value_json, event_time,"
            " knowledge_time, version, supersedes, conflict_flag, evidence_ids, run_id"
            " FROM facts WHERE namespace = ? AND entity_kind = ? AND entity_id = ? AND field = ?"
            " ORDER BY version DESC LIMIT 1",
            (namespace, kind, entity_id, field),
        ).fetchone()
        return self._row_to_fact(row) if row else None

    @staticmethod
    def _row_to_fact(row: tuple) -> FactRecord:
        return FactRecord(
            fact_id=row[0],
            namespace=row[1],
            entity_kind=row[2],
            entity_id=row[3],
            field=row[4],
            value=json.loads(row[5]),
            event_time=datetime.fromisoformat(row[6]) if row[6] else None,
            knowledge_time=datetime.fromisoformat(row[7]),
            version=row[8],
            supersedes=row[9],
            conflict_flag=bool(row[10]),
            evidence_ids=json.loads(row[11]),
            run_id=row[12],
        )

    def close(self) -> None:
        self._conn.close()
