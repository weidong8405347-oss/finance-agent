"""MetricStore：typed 观测/计算/研究产物/档案快照的物化索引（设计 §6.5）。

与 BitemporalStore（旧 Fact 版本链）并行存在，各自独立文件：
- 新模型事件（metric/asserted 等）为重建依据，本库是幂等投影索引；
- 观测按语义键版本化（append-only）：同语义键不同值 = 竞争版本（重述/冲突），
  旧版本永不改写；
- 裁决以 conflict_resolutions 追加记录（带 resolved_at）——历史状态不靠
  「清空旧行 flag」重建（§2.2「历史冲突状态不冻结」的整改）；
- 快照/计划/产物存完整 payload JSON，投影可重建。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from .errors import ConflictError
from .metrics import MetricObservation, SourceDocument, observation_from_dict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_documents (
    document_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    url TEXT,
    raw_hash TEXT,
    published_at TEXT,
    retrieved_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_identity ON source_documents(provider_id, url, raw_hash);

CREATE TABLE IF NOT EXISTS metric_observations (
    observation_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    metric_key TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT NOT NULL,
    frequency TEXT NOT NULL,
    nature TEXT NOT NULL,
    basis TEXT NOT NULL,
    value TEXT,
    unit TEXT,
    currency TEXT,
    knowledge_time TEXT NOT NULL,
    created_at TEXT NOT NULL,
    semantic_hash TEXT NOT NULL,
    version INTEGER NOT NULL,
    supersedes TEXT,
    conflict_flag INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_lookup ON metric_observations(
    namespace, entity_kind, entity_id, metric_key, period_end, knowledge_time);
CREATE INDEX IF NOT EXISTS idx_obs_semantic ON metric_observations(namespace, semantic_hash, version);

CREATE TABLE IF NOT EXISTS calculation_runs (
    calculation_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    formula_id TEXT NOT NULL,
    formula_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    unit TEXT,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    run_id TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calc_entity ON calculation_runs(namespace, entity_kind, entity_id, created_at);

CREATE TABLE IF NOT EXISTS research_plans (
    plan_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    objective TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    recipe_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    run_id TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_entity ON research_plans(namespace, entity_kind, entity_id, created_at);

CREATE TABLE IF NOT EXISTS research_claims (
    claim_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    question_id TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    statement TEXT NOT NULL,
    created_at TEXT NOT NULL,
    evidence_cutoff TEXT,
    run_id TEXT,
    superseded_by TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_entity
    ON research_claims(namespace, entity_kind, entity_id, created_at);

CREATE TABLE IF NOT EXISTS research_artifacts (
    artifact_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    plan_id TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    sufficiency TEXT NOT NULL DEFAULT 'partial',
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    evidence_cutoff TEXT,
    run_id TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_entity
    ON research_artifacts(namespace, entity_kind, entity_id, created_at);

CREATE TABLE IF NOT EXISTS dossier_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    as_of TEXT NOT NULL,
    data_hash TEXT NOT NULL,
    projector_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshot_dedup
    ON dossier_snapshots(namespace, entity_kind, entity_id, data_hash);
CREATE INDEX IF NOT EXISTS idx_snapshot_entity ON dossier_snapshots(namespace, entity_kind, entity_id, as_of);

CREATE TABLE IF NOT EXISTS conflict_resolutions (
    resolution_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    semantic_hash TEXT NOT NULL,
    keep_observation_id TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    note TEXT,
    run_id TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resolutions ON conflict_resolutions(namespace, semantic_hash, resolved_at);
"""

_OBS_COLUMNS = (
    "observation_id, namespace, entity_kind, entity_id, metric_key, period_start, period_end,"
    " frequency, nature, basis, value, unit, currency, knowledge_time, created_at,"
    " semantic_hash, version, supersedes, conflict_flag, status, payload_json"
)


class ConflictResolution(BaseModel):
    """一次带生效时刻的裁决记录（历史投影据此冻结「当时是否已裁决」）。"""

    resolution_id: str
    namespace: str
    entity_kind: str
    entity_id: str
    target_kind: str  # observation（预留 fact/document）
    semantic_hash: str
    keep_observation_id: str
    resolved_at: datetime
    note: str = ""
    run_id: str | None = None


class StoredCalculation(BaseModel):
    calculation_id: str
    namespace: str
    entity_kind: str
    entity_id: str
    formula_id: str
    formula_version: int
    status: str
    result: str | None
    unit: str
    input_hash: str
    created_at: datetime
    run_id: str | None
    payload: dict[str, Any]


class MetricStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------------- 文档目录 ----------------

    def upsert_document(self, doc: SourceDocument) -> str:
        """文档登记：(provider_id, url, raw_hash) 幂等；更新 = 新 hash 新版本行。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT document_id FROM source_documents"
                " WHERE provider_id = ? AND url IS ? AND raw_hash IS ?",
                (doc.provider_id, doc.url, doc.raw_hash),
            ).fetchone()
            if row is not None:
                return row[0]
            document_id = doc.document_id or f"doc-{uuid.uuid4().hex[:12]}"
            try:
                self._conn.execute(
                    "INSERT INTO source_documents (document_id, provider_id, url, raw_hash,"
                    " published_at, retrieved_at, payload_json) VALUES (?,?,?,?,?,?,?)",
                    (
                        document_id, doc.provider_id, doc.url, doc.raw_hash,
                        doc.published_at.isoformat() if doc.published_at else None,
                        doc.retrieved_at.isoformat(),
                        doc.model_dump_json(),
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                raise ConflictError(f"document_id 重复: {document_id}") from e
            return document_id

    def get_document(self, document_id: str) -> SourceDocument | None:
        row = self._conn.execute(
            "SELECT payload_json FROM source_documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        return SourceDocument.model_validate_json(row[0]) if row else None

    def list_documents(self, *, provider_id: str | None = None, limit: int = 200) -> list[SourceDocument]:
        if provider_id:
            rows = self._conn.execute(
                "SELECT payload_json FROM source_documents WHERE provider_id = ?"
                " ORDER BY retrieved_at DESC LIMIT ?",
                (provider_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT payload_json FROM source_documents ORDER BY retrieved_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [SourceDocument.model_validate_json(r[0]) for r in rows]

    # ---------------- 观测（语义键版本链） ----------------

    def assert_observation(
        self, obs: MetricObservation, *, namespace: str = "prod"
    ) -> tuple[str, bool]:
        """登记观测。返回 (observation_id, created)。

        - 同语义键 + 同值 → 幂等（返回已有 id，created=False）；
        - 同语义键 + 不同值 → 新版本 conflict_flag=1（重述/竞争，append-only）；
        - 新语义键 → version=1。
        """
        sem = obs.semantic_hash()
        with self._lock:
            rows = self._conn.execute(
                "SELECT observation_id, value, version, status FROM metric_observations"
                " WHERE namespace = ? AND semantic_hash = ? ORDER BY version DESC",
                (namespace, sem),
            ).fetchall()
            value = obs.value
            for existing_id, existing_value, _v, existing_status in rows:
                if existing_value == value and existing_status == obs.status:
                    return existing_id, False  # 幂等：同语义键同值
            version = (rows[0][2] + 1) if rows else 1
            conflict = bool(rows) and any(r[1] != value for r in rows)
            observation_id = obs.observation_id or f"obs-{uuid.uuid4().hex[:12]}"
            payload = obs.model_dump(mode="json")
            payload["observation_id"] = observation_id
            self._conn.execute(
                "INSERT INTO metric_observations (observation_id, namespace, entity_kind, entity_id,"
                " metric_key, period_start, period_end, frequency, nature, basis, value, unit,"
                " currency, knowledge_time, created_at, semantic_hash, version, supersedes,"
                " conflict_flag, status, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    observation_id, namespace, obs.entity_kind, obs.entity_id, obs.metric_key,
                    obs.period.start.isoformat() if obs.period.start else None,
                    obs.period.end.isoformat(), obs.period.frequency, obs.nature, obs.basis,
                    obs.value, obs.unit, obs.currency, obs.knowledge_time.isoformat(),
                    obs.created_at.isoformat(), sem, version,
                    rows[0][0] if rows else None, 1 if conflict else 0, obs.status,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            self._conn.commit()
            return observation_id, True

    def get_observation(self, observation_id: str) -> MetricObservation | None:
        row = self._conn.execute(
            "SELECT payload_json FROM metric_observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        return observation_from_dict(json.loads(row[0])) if row else None

    def observations_as_of(
        self,
        entity_kind: str,
        entity_id: str,
        t: datetime,
        *,
        namespace: str = "prod",
        metric_key: str | None = None,
        frequency: str | None = None,
        nature: str | None = None,
    ) -> list[MetricObservation]:
        """as_of(T) 观测投影：每语义键取 knowledge_time ≤ T 的最新版本；
        已被裁决（resolved_at ≤ T）的语义键以裁决保留版本为当前值。"""
        sql = (
            f"SELECT {_OBS_COLUMNS} FROM metric_observations"
            " WHERE namespace = ? AND entity_kind = ? AND entity_id = ? AND knowledge_time <= ?"
        )
        args: list[Any] = [namespace, entity_kind, entity_id, t.isoformat()]
        if metric_key:
            sql += " AND metric_key = ?"
            args.append(metric_key)
        if frequency:
            sql += " AND frequency = ?"
            args.append(frequency)
        if nature:
            sql += " AND nature = ?"
            args.append(nature)
        rows = self._conn.execute(sql, args).fetchall()
        resolutions = {
            r.semantic_hash: r
            for r in self.resolutions_as_of(entity_kind, entity_id, t, namespace=namespace)
        }
        best: dict[str, tuple] = {}
        for r in rows:
            sem, kt, version = r[15], r[13], r[16]
            cur = best.get(sem)
            if cur is None or (kt, version) > (cur[13], cur[16]):
                best[sem] = r
        out: list[MetricObservation] = []
        for sem, r in best.items():
            res = resolutions.get(sem)
            if res is not None and res.keep_observation_id != r[0]:
                kept = self._conn.execute(
                    f"SELECT {_OBS_COLUMNS} FROM metric_observations"
                    " WHERE observation_id = ? AND knowledge_time <= ?",
                    (res.keep_observation_id, t.isoformat()),
                ).fetchone()
                if kept is not None:
                    r = kept
            out.append(observation_from_dict(json.loads(r[20])))
        out.sort(key=lambda o: (o.metric_key, o.period.end, o.period.frequency))
        return out

    def observation_history(
        self, semantic_hash: str, *, namespace: str = "prod"
    ) -> list[MetricObservation]:
        rows = self._conn.execute(
            "SELECT payload_json FROM metric_observations WHERE namespace = ?"
            " AND semantic_hash = ? ORDER BY version",
            (namespace, semantic_hash),
        ).fetchall()
        return [observation_from_dict(json.loads(r[0])) for r in rows]

    def conflicted_semantic_hashes(
        self, entity_kind: str, entity_id: str, *, namespace: str = "prod"
    ) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT semantic_hash FROM metric_observations"
            " WHERE namespace = ? AND entity_kind = ? AND entity_id = ? AND conflict_flag = 1",
            (namespace, entity_kind, entity_id),
        ).fetchall()
        return [r[0] for r in rows]

    # ---------------- 裁决（时态化） ----------------

    def add_resolution(self, res: ConflictResolution) -> str:
        with self._lock:
            self._conn.execute(
                "INSERT INTO conflict_resolutions (resolution_id, namespace, entity_kind,"
                " entity_id, target_kind, semantic_hash, keep_observation_id, resolved_at,"
                " note, run_id, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    res.resolution_id, res.namespace, res.entity_kind, res.entity_id,
                    res.target_kind, res.semantic_hash, res.keep_observation_id,
                    res.resolved_at.isoformat(), res.note, res.run_id,
                    res.model_dump_json(),
                ),
            )
            self._conn.commit()
        return res.resolution_id

    def resolutions_as_of(
        self, entity_kind: str, entity_id: str, t: datetime, *, namespace: str = "prod"
    ) -> list[ConflictResolution]:
        rows = self._conn.execute(
            "SELECT payload_json FROM conflict_resolutions WHERE namespace = ?"
            " AND entity_kind = ? AND entity_id = ? AND resolved_at <= ? ORDER BY resolved_at",
            (namespace, entity_kind, entity_id, t.isoformat()),
        ).fetchall()
        return [ConflictResolution.model_validate_json(r[0]) for r in rows]

    # ---------------- 计算 ----------------

    def save_calculation(
        self,
        *,
        calculation_id: str,
        namespace: str,
        entity_kind: str,
        entity_id: str,
        formula_id: str,
        formula_version: int,
        status: str,
        result: str | None,
        unit: str,
        input_hash: str,
        created_at: datetime,
        run_id: str | None,
        payload: dict[str, Any],
    ) -> str:
        with self._lock:
            existing = self._conn.execute(
                "SELECT calculation_id FROM calculation_runs WHERE namespace = ? AND input_hash = ?"
                " AND formula_id = ? AND formula_version = ?",
                (namespace, input_hash, formula_id, formula_version),
            ).fetchone()
            if existing is not None:
                return existing[0]  # 幂等：同输入同公式版本
            self._conn.execute(
                "INSERT INTO calculation_runs (calculation_id, namespace, entity_kind, entity_id,"
                " formula_id, formula_version, status, result, unit, input_hash, created_at,"
                " run_id, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    calculation_id, namespace, entity_kind, entity_id, formula_id,
                    formula_version, status, result, unit, input_hash,
                    created_at.isoformat(), run_id, json.dumps(payload, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        return calculation_id

    def get_calculation(self, calculation_id: str) -> StoredCalculation | None:
        row = self._conn.execute(
            "SELECT calculation_id, namespace, entity_kind, entity_id, formula_id,"
            " formula_version, status, result, unit, input_hash, created_at, run_id, payload_json"
            " FROM calculation_runs WHERE calculation_id = ?",
            (calculation_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredCalculation(
            calculation_id=row[0], namespace=row[1], entity_kind=row[2], entity_id=row[3],
            formula_id=row[4], formula_version=row[5], status=row[6], result=row[7],
            unit=row[8], input_hash=row[9], created_at=datetime.fromisoformat(row[10]),
            run_id=row[11], payload=json.loads(row[12]),
        )

    # ---------------- 研究计划 ----------------

    def save_plan(self, *, plan_id: str, namespace: str, payload: dict[str, Any]) -> str:
        p = payload
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO research_plans (plan_id, namespace, entity_kind,"
                " entity_id, mode, objective, recipe_id, recipe_version, status, created_at,"
                " run_id, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id, namespace, p["entity_kind"], p["entity_id"], p["mode"],
                    p["objective"], p["recipe_id"], p["recipe_version"],
                    p.get("status", "active"), p["created_at"], p.get("run_id"),
                    json.dumps(p, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        return plan_id

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload_json FROM research_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def plans_for(
        self, entity_kind: str, entity_id: str, *, namespace: str = "prod", limit: int = 20
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload_json FROM research_plans WHERE namespace = ? AND entity_kind = ?"
            " AND entity_id = ? ORDER BY created_at DESC LIMIT ?",
            (namespace, entity_kind, entity_id, limit),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    # ---------------- 研究论断 ----------------

    def save_claim(self, *, claim_id: str, namespace: str, payload: dict[str, Any]) -> str:
        c = payload
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO research_claims (claim_id, namespace, entity_kind,"
                " entity_id, kind, question_id, status, statement, created_at, evidence_cutoff,"
                " run_id, superseded_by, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    claim_id, namespace, c["entity_kind"], c["entity_id"], c["kind"],
                    c.get("question_id"), c.get("status", "draft"), c["statement"],
                    c["created_at"], c.get("evidence_cutoff"), c.get("run_id"),
                    c.get("superseded_by"), json.dumps(c, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        return claim_id

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload_json FROM research_claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def claims_as_of(
        self,
        entity_kind: str,
        entity_id: str,
        t: datetime,
        *,
        namespace: str = "prod",
        statuses: tuple[str, ...] = ("draft", "validated"),
    ) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT payload_json FROM research_claims WHERE namespace = ? AND entity_kind = ?"
            f" AND entity_id = ? AND created_at <= ? AND status IN ({marks}) ORDER BY created_at",
            (namespace, entity_kind, entity_id, t.isoformat(), *statuses),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    # ---------------- 研究产物（冻结研报） ----------------

    def save_artifact(self, *, artifact_id: str, namespace: str, payload: dict[str, Any]) -> str:
        a = payload
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO research_artifacts (artifact_id, namespace, entity_kind,"
                " entity_id, plan_id, status, sufficiency, title, created_at, evidence_cutoff,"
                " run_id, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id, namespace, a["entity_kind"], a["entity_id"], a.get("plan_id"),
                    a.get("status", "draft"), a.get("sufficiency", "partial"),
                    a.get("title", ""), a["created_at"], a.get("evidence_cutoff"),
                    a.get("run_id"), json.dumps(a, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        return artifact_id

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload_json FROM research_artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def artifacts_as_of(
        self,
        entity_kind: str,
        entity_id: str,
        t: datetime,
        *,
        namespace: str = "prod",
        include_superseded: bool = True,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT payload_json FROM research_artifacts WHERE namespace = ? AND entity_kind = ?"
            " AND entity_id = ? AND created_at <= ?"
        )
        if not include_superseded:
            sql += " AND status != 'superseded'"
        rows = self._conn.execute(
            sql + " ORDER BY created_at DESC", (namespace, entity_kind, entity_id, t.isoformat())
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    # ---------------- 档案快照 ----------------

    def save_snapshot(
        self, *, snapshot_id: str, namespace: str, payload: dict[str, Any]
    ) -> tuple[str, bool]:
        """快照落库（data_hash 幂等）。返回 (snapshot_id, created)。"""
        ctx = payload["context"]
        with self._lock:
            existing = self._conn.execute(
                "SELECT snapshot_id FROM dossier_snapshots WHERE namespace = ?"
                " AND entity_kind = ? AND entity_id = ? AND data_hash = ?",
                (namespace, payload["entity"]["kind"], payload["entity"]["id"],
                 payload.get("data_hash", "")),
            ).fetchone()
            if existing is not None:
                return existing[0], False
            try:
                self._conn.execute(
                    "INSERT INTO dossier_snapshots (snapshot_id, namespace, entity_kind,"
                    " entity_id, mode, as_of, data_hash, projector_version, created_at,"
                    " payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id, namespace, payload["entity"]["kind"], payload["entity"]["id"],
                        ctx["mode"], ctx["as_of"], payload.get("data_hash", ""),
                        ctx.get("projector_version", ""), ctx.get("generated_at", ""),
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT snapshot_id FROM dossier_snapshots WHERE namespace = ?"
                    " AND entity_kind = ? AND entity_id = ? AND data_hash = ?",
                    (namespace, payload["entity"]["kind"], payload["entity"]["id"],
                     payload.get("data_hash", "")),
                ).fetchone()
                return (row[0] if row else snapshot_id), False
        return snapshot_id, True

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload_json FROM dossier_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def latest_snapshot(
        self, entity_kind: str, entity_id: str, *, namespace: str = "prod"
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload_json FROM dossier_snapshots WHERE namespace = ?"
            " AND entity_kind = ? AND entity_id = ? ORDER BY created_at DESC LIMIT 1",
            (namespace, entity_kind, entity_id),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def list_entities_with_snapshots(self, *, namespace: str = "prod") -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT DISTINCT entity_kind, entity_id FROM dossier_snapshots WHERE namespace = ?",
            (namespace,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]


_OBS_ADAPTER: TypeAdapter[MetricObservation] = TypeAdapter(MetricObservation)  # noqa: F841 - 保留显式适配器引用
