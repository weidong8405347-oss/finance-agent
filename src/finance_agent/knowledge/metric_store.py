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

import contextlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
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

-- 论断版本链（review R3：核验/降级等修订必须是时态修订而非原位覆盖）：
-- save_claim 每次写入追加一行（recorded_at = 系统获知的逻辑时刻）；
-- research_claims 仍是「当前指针」，claims_as_of 按版本链取 recorded_at ≤ T 的
-- 最新版本重建历史状态——今天降级一条论断，不再改写过去时点的查询结果。
CREATE TABLE IF NOT EXISTS research_claim_versions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_versions
    ON research_claim_versions(namespace, claim_id, recorded_at, seq);

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

-- 修订/失效记录（audit §3.2 修复方案 6）：错误观测不原位修改，而是追加修订行，
-- 保留旧版本完整 payload 作为审计链；历史投影按 revised_at 判断当时是否已失效。
CREATE TABLE IF NOT EXISTS metric_revisions (
    revision_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    replacement_observation_id TEXT,
    dependent_refs TEXT NOT NULL DEFAULT '[]',
    revised_at TEXT NOT NULL,
    run_id TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revisions_obs ON metric_revisions(namespace, observation_id, revised_at);
CREATE INDEX IF NOT EXISTS idx_revisions_entity
    ON metric_revisions(namespace, entity_kind, entity_id, revised_at);

-- 论断失效记录（tools-plugins 方案 §9.3，P2-B）：依赖变更时追加失效行，
-- 不原位改冻结历史——旧快照按 invalidated_at 时态隔离，保持原样。
CREATE TABLE IF NOT EXISTS claim_invalidations (
    invalidation_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_refs TEXT NOT NULL DEFAULT '[]',
    invalidated_at TEXT NOT NULL,
    run_id TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (namespace, invalidation_id)
);
CREATE INDEX IF NOT EXISTS idx_claim_inval ON claim_invalidations(namespace, claim_id, invalidated_at);

-- 档案整合提交台账（P2-B）：change_set_id 幂等，重跑不重复落失效记录。
CREATE TABLE IF NOT EXISTS profile_update_commits (
    change_set_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    expected_base_hash TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    run_id TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (namespace, change_set_id)
);
CREATE INDEX IF NOT EXISTS idx_profile_commits
    ON profile_update_commits(namespace, entity_kind, entity_id, committed_at);
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
        # additive 迁移：版本表建立前入库的存量论断补一条基线版本
        # （recorded_at = created_at，幂等），历史投影从升级时刻起可重建
        with self._lock:
            self._conn.execute(
                "INSERT INTO research_claim_versions (claim_id, namespace, entity_kind,"
                " entity_id, recorded_at, payload_json)"
                " SELECT c.claim_id, c.namespace, c.entity_kind, c.entity_id,"
                " c.created_at, c.payload_json FROM research_claims c"
                " WHERE NOT EXISTS (SELECT 1 FROM research_claim_versions v"
                "                 WHERE v.namespace = c.namespace"
                "                 AND v.claim_id = c.claim_id)"
            )
            self._conn.commit()

    # ---------------- 实体删除（用户发起；由 knowledge/purge.py 協调） ----------------

    #: 带 entity_kind/entity_id 列的表（逐表计数删除，不静默漏表）
    _ENTITY_TABLES: tuple[str, ...] = (
        "metric_observations", "calculation_runs", "research_plans", "research_claims",
        "research_claim_versions", "research_artifacts", "dossier_snapshots",
        "conflict_resolutions", "metric_revisions", "claim_invalidations",
        "profile_update_commits",
    )

    def delete_entity(
        self, entity_kind: str, entity_id: str, *, namespace: str | None = None
    ) -> dict[str, int]:
        """硬删除实体在 typed 库的全部行（观测/计算/计划/论断/产物/快照/裁决/修订）。

        返回逐表删除行数（可审计：删了什么、删了多少，不笼统报「已清理」）。
        source_documents 是 provider 级文档目录，不随实体删（可能被其他实体引用）。
        """
        counts: dict[str, int] = {}
        with self._lock:
            for table in self._ENTITY_TABLES:
                sql = f"DELETE FROM {table} WHERE entity_kind = ? AND entity_id = ?"
                args: list[str] = [entity_kind, entity_id]
                if namespace is not None:
                    sql += " AND namespace = ?"
                    args.append(namespace)
                cur = self._conn.execute(sql, args)
                if cur.rowcount:
                    counts[table] = cur.rowcount
            self._conn.commit()
        return counts

    def referenced_evidence_ids(self, *, namespace: str | None = None) -> set[str]:
        """仍被观测/论断引用的证据 id（孤儿证据清理用）。"""
        out: set[str] = set()
        queries = [
            ("SELECT payload_json FROM metric_observations", "evidence_refs"),
            ("SELECT payload_json FROM research_claims", "support_refs"),
            ("SELECT payload_json FROM research_claims", "counter_refs"),
        ]
        for sql, key in queries:
            args: list[str] = []
            if namespace is not None:
                sql += " WHERE namespace = ?"
                args.append(namespace)
            for (raw,) in self._conn.execute(sql, args).fetchall():
                with contextlib.suppress(Exception):
                    payload = json.loads(raw)
                # 逐层找 ev- 引用（嵌套结构也不漏）
                stack: list[Any] = [payload]
                while stack:
                    node = stack.pop()
                    if isinstance(node, dict):
                        stack.extend(node.values())
                    elif isinstance(node, list):
                        stack.extend(node)
                    elif isinstance(node, str) and node.startswith("ev-"):
                        out.add(node)
            del key
        return out

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

    def get_ref_meta(self, ref_id: str) -> dict[str, Any] | None:
        """引用上下文元数据（review #4/#5）：namespace/实体/可知时刻——
        计算与论断的引用必须同命名空间同实体才可信。"""
        specs = (
            ("obs-", "metric_observations", "observation_id", "metric_key"),
            ("calc-", "calculation_runs", "calculation_id", "formula_id"),
            ("claim-", "research_claims", "claim_id", None),
            ("artifact-", "research_artifacts", "artifact_id", None),
            ("plan-", "research_plans", "plan_id", None),
        )
        for prefix, table, id_col, extra_col in specs:
            if not ref_id.startswith(prefix):
                continue
            cols = "namespace, entity_kind, entity_id, created_at" if table != "metric_observations" \
                else "namespace, entity_kind, entity_id, knowledge_time"
            if extra_col:
                cols += f", {extra_col}"
            row = self._conn.execute(
                f"SELECT {cols} FROM {table} WHERE {id_col} = ?",  # noqa: S608 - 表名来自白名单
                (ref_id,),
            ).fetchone()
            if row is None:
                return None
            meta = {
                "kind": prefix.rstrip("-"), "namespace": row[0], "entity_kind": row[1],
                "entity_id": row[2], "knowledge_time": row[3],
            }
            if extra_col:
                meta["label"] = row[4]
            return meta
        return None

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
        invalidated = self.invalidated_observation_ids(namespace=namespace, as_of=t)
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
            if r[0] in invalidated:
                continue  # 已失效（修订记录生效时刻 ≤ T）：不进当前投影，旧行仍在审计链
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
        self, semantic_hash: str, *, namespace: str = "prod", as_of: datetime | None = None,
    ) -> list[MetricObservation]:
        """版本链；as_of 限定时只返回当时可知的版本（历史页面不泄露未来重述）。"""
        sql = "SELECT payload_json FROM metric_observations WHERE namespace = ? AND semantic_hash = ?"
        args: list[Any] = [namespace, semantic_hash]
        if as_of is not None:
            sql += " AND knowledge_time <= ?"
            args.append(as_of.isoformat())
        rows = self._conn.execute(sql + " ORDER BY version", args).fetchall()
        return [observation_from_dict(json.loads(r[0])) for r in rows]

    def conflicted_semantic_hashes(
        self, entity_kind: str, entity_id: str, *, namespace: str = "prod",
        as_of: datetime | None = None,
        exclude_resolved: bool = False,
    ) -> list[str]:
        """竞争语义键。as_of 限定：只有 knowledge_time ≤ T 的竞争版本才算「当时可见冲突」
        （review #3：历史页面不得提前泄露未来才可知的竞争值）。"""
        sql = (
            "SELECT DISTINCT semantic_hash FROM metric_observations"
            " WHERE namespace = ? AND entity_kind = ? AND entity_id = ? AND conflict_flag = 1"
        )
        args: list[Any] = [namespace, entity_kind, entity_id]
        if as_of is not None:
            sql += " AND knowledge_time <= ?"
            args.append(as_of.isoformat())
            # 同一语义键在 T 前只有一个版本时，竞争标记来自未来版本 → 不算当时冲突
            sql += (
                " AND (SELECT COUNT(*) FROM metric_observations m2"
                " WHERE m2.namespace = metric_observations.namespace"
                " AND m2.semantic_hash = metric_observations.semantic_hash"
                " AND m2.knowledge_time <= ?) > 1"
            )
            args.append(as_of.isoformat())
        rows = self._conn.execute(sql, args).fetchall()
        sems = {r[0] for r in rows}
        if exclude_resolved:
            t = as_of or datetime.now()
            resolved = {
                r.semantic_hash
                for r in self.resolutions_as_of(entity_kind, entity_id, t, namespace=namespace)
            }
            sems -= resolved
        return sorted(sems)

    # ---------------- 修订/失效记录（audit §3.2 修复方案 6） ----------------

    def save_revision(
        self, *, observation_id: str, namespace: str, action: str, reason: str,
        entity_kind: str, entity_id: str,
        replacement_observation_id: str | None = None,
        dependent_refs: list[str] | None = None,
        payload: dict[str, Any] | None = None,
        revised_at: datetime | None = None,
        run_id: str | None = None,
    ) -> str:
        """追加一条修订记录（append-only）：不原位修改冻结历史。

        action: invalidated（作废）/ corrected（更正，带 replacement）/
                needs_review（待重审）。
        """
        if action not in ("invalidated", "corrected", "needs_review", "superseded"):
            raise ConflictError(f"未知修订动作 {action!r}")
        revision_id = f"rev-{uuid.uuid4().hex[:12]}"
        at = (revised_at or datetime.now(UTC)).isoformat()
        record = {
            "revision_id": revision_id, "namespace": namespace,
            "observation_id": observation_id, "entity_kind": entity_kind,
            "entity_id": entity_id, "action": action, "reason": reason,
            "replacement_observation_id": replacement_observation_id,
            "dependent_refs": list(dependent_refs or []), "revised_at": at,
            "run_id": run_id, "observation": payload or {},
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO metric_revisions (revision_id, namespace, observation_id,"
                " entity_kind, entity_id, action, reason, replacement_observation_id,"
                " dependent_refs, revised_at, run_id, payload_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (revision_id, namespace, observation_id, entity_kind, entity_id, action,
                 reason, replacement_observation_id,
                 json.dumps(list(dependent_refs or []), ensure_ascii=False), at, run_id,
                 json.dumps(record, ensure_ascii=False, default=str)),
            )
            self._conn.commit()
        return revision_id

    def revisions_for(
        self, observation_id: str, *, namespace: str = "prod"
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload_json FROM metric_revisions WHERE namespace = ?"
            " AND observation_id = ? ORDER BY revised_at",
            (namespace, observation_id),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def invalidated_observation_ids(
        self, *, namespace: str = "prod", as_of: datetime | None = None
    ) -> set[str]:
        """已失效的观测 id（revised_at ≤ as_of）：历史投影不泄露「今天才作废」的状态。"""
        sql = ("SELECT observation_id FROM metric_revisions WHERE namespace = ?"
               " AND action IN ('invalidated', 'superseded')")
        args: list[Any] = [namespace]
        if as_of is not None:
            sql += " AND revised_at <= ?"
            args.append(as_of.isoformat())
        return {r[0] for r in self._conn.execute(sql, args).fetchall()}

    def refs_to(
        self, ref_id: str, *, namespace: str = "prod",
        entity_kind: str | None = None, entity_id: str | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, list[str]]:
        """引用了某 ref 的下游记录（重审依赖用）：派生观测 / 计算 / 论断 / 产物。

        review R7：任意 ref 类型可查（观测/计算/论断均可作起点），依赖闭包遍历
        沿它进行；entity/as_of 收窄保证闭包不跨主体、不泄露 as_of 之后才登记的
        下游记录（历史投影纪律与 observations_as_of 一致）。自身引用行被排除
        （payload 含自身 id，不自匹配）。
        """
        out: dict[str, list[str]] = {
            "observations": [], "calculations": [], "claims": [], "artifacts": []}
        like = f"%{ref_id}%"
        specs = (
            ("metric_observations", "observation_id", "observations", "knowledge_time"),
            ("calculation_runs", "calculation_id", "calculations", "created_at"),
            ("research_claims", "claim_id", "claims", "created_at"),
            ("research_artifacts", "artifact_id", "artifacts", "created_at"),
        )
        for table, key, bucket, time_col in specs:
            sql = (f"SELECT {key} FROM {table} WHERE namespace = ?"  # noqa: S608 - 表白名单
                   " AND payload_json LIKE ?")
            args: list[Any] = [namespace, like]
            if entity_kind is not None and entity_id is not None:
                sql += " AND entity_kind = ? AND entity_id = ?"
                args.extend([entity_kind, entity_id])
            if as_of is not None:
                sql += f" AND {time_col} <= ?"
                args.append(as_of.isoformat())
            rows = self._conn.execute(sql, args).fetchall()
            out[bucket] = [r[0] for r in rows if r[0] != ref_id]
        return out

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

    # ---------------- 论断失效与整合提交（P2-B，追加式时态记录） ----------------

    def save_claim_invalidation(
        self, *, invalidation_id: str, namespace: str, entity_kind: str, entity_id: str,
        claim_id: str, reason: str, source_refs: list[str], invalidated_at: datetime,
        run_id: str | None = None,
    ) -> str:
        """追加论断失效记录（不改冻结历史；读侧按 invalidated_at 时态合并）。"""
        payload = {
            "invalidation_id": invalidation_id, "namespace": namespace,
            "entity_kind": entity_kind, "entity_id": entity_id,
            "claim_id": claim_id, "reason": reason,
            "source_refs": [str(r) for r in source_refs],
            "invalidated_at": invalidated_at.isoformat(), "run_id": run_id,
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO claim_invalidations (invalidation_id, namespace, entity_kind,"
                " entity_id, claim_id, reason, source_refs, invalidated_at, run_id,"
                " payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (invalidation_id, namespace, entity_kind, entity_id, claim_id, reason,
                 json.dumps(payload["source_refs"], ensure_ascii=False),
                 invalidated_at.isoformat(), run_id,
                 json.dumps(payload, ensure_ascii=False)),
            )
            self._conn.commit()
        return invalidation_id

    def invalidations_as_of(
        self, entity_kind: str, entity_id: str, t: datetime, *, namespace: str = "prod",
    ) -> list[dict[str, Any]]:
        """T 时点已生效的失效记录（历史快照不泄露「今天才作废」的状态）。"""
        rows = self._conn.execute(
            "SELECT payload_json FROM claim_invalidations WHERE namespace = ?"
            " AND entity_kind = ? AND entity_id = ? AND invalidated_at <= ?"
            " ORDER BY invalidated_at",
            (namespace, entity_kind, entity_id, t.isoformat()),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_profile_update_commit(
        self, *, change_set_id: str, namespace: str, entity_kind: str, entity_id: str,
        expected_base_hash: str, committed_at: datetime, run_id: str | None,
        payload: dict[str, Any],
    ) -> bool:
        """整合提交台账（change_set_id 幂等）：返回是否首次提交。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO profile_update_commits (change_set_id, namespace,"
                " entity_kind, entity_id, expected_base_hash, committed_at, run_id,"
                " payload_json) VALUES (?,?,?,?,?,?,?,?)",
                (change_set_id, namespace, entity_kind, entity_id, expected_base_hash,
                 committed_at.isoformat(), run_id,
                 json.dumps(payload, ensure_ascii=False, default=str)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_profile_update_commit(
        self, change_set_id: str, *, namespace: str = "prod",
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload_json FROM profile_update_commits"
            " WHERE namespace = ? AND change_set_id = ?",
            (namespace, change_set_id),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def record_profile_update(
        self, *, commit: dict[str, Any], invalidations: list[dict[str, Any]],
    ) -> bool:
        """档案整合提交的原子落库（review R6）：失效记录与提交台账在同一事务，
        要么全落要么全不落——不再逐条提交导致「第一条已写入、第二条被拒」的半截状态。

        change_set_id 幂等：台账已存在（含并发竞争）→ 整体回滚并返回 False
        （重放语义：首次提交的失效记录保持，重跑不重复追加）。
        commit/invalidations 的字段契约见 consolidator.commit_update。
        """
        namespace = str(commit["namespace"])
        with self._lock:
            existing = self._conn.execute(
                "SELECT change_set_id FROM profile_update_commits"
                " WHERE namespace = ? AND change_set_id = ?",
                (namespace, str(commit["change_set_id"])),
            ).fetchone()
            if existing is not None:
                return False
            try:
                for inv in invalidations:
                    payload = {
                        "invalidation_id": inv["invalidation_id"], "namespace": namespace,
                        "entity_kind": commit["entity_kind"], "entity_id": commit["entity_id"],
                        "claim_id": inv["claim_id"], "reason": inv["reason"],
                        "source_refs": list(inv.get("source_refs") or []),
                        "invalidated_at": inv["invalidated_at"], "run_id": inv.get("run_id"),
                    }
                    self._conn.execute(
                        "INSERT INTO claim_invalidations (invalidation_id, namespace,"
                        " entity_kind, entity_id, claim_id, reason, source_refs,"
                        " invalidated_at, run_id, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (inv["invalidation_id"], namespace, commit["entity_kind"],
                         commit["entity_id"], inv["claim_id"], inv["reason"],
                         json.dumps(payload["source_refs"], ensure_ascii=False),
                         inv["invalidated_at"], inv.get("run_id"),
                         json.dumps(payload, ensure_ascii=False)),
                    )
                self._conn.execute(
                    "INSERT INTO profile_update_commits (change_set_id, namespace,"
                    " entity_kind, entity_id, expected_base_hash, committed_at, run_id,"
                    " payload_json) VALUES (?,?,?,?,?,?,?,?)",
                    (str(commit["change_set_id"]), namespace, commit["entity_kind"],
                     commit["entity_id"], str(commit["expected_base_hash"]),
                     str(commit["committed_at"]), commit.get("run_id"),
                     json.dumps(commit["payload"], ensure_ascii=False, default=str)),
                )
            except sqlite3.IntegrityError:
                # 并发窗口：另一提交先落——回滚本次全部写入（含失效记录），按重放处理
                self._conn.rollback()
                return False
            except Exception:
                self._conn.rollback()
                raise
            self._conn.commit()
            return True

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

    def update_plan_question(
        self, plan_id: str, question_id: str, patch: dict[str, Any], *, namespace: str = "prod",
    ) -> dict[str, Any] | None:
        """原子更新单个问题（review #8）：读-改-写全程持锁，并行 worker
        各自回答不同问题不互相覆盖。patch 语义：status/conclusion 覆盖，
        support_refs/counter_refs/unresolved/attempts 合并去重。返回更新后的问题。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM research_plans WHERE plan_id = ? AND namespace = ?",
                (plan_id, namespace),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row[0])
            question = next(
                (q for q in payload.get("questions", []) if q.get("question_id") == question_id),
                None,
            )
            if question is None:
                return None
            for key in ("status", "conclusion"):
                if patch.get(key):
                    question[key] = patch[key]
            for key in ("support_refs", "counter_refs", "unresolved", "attempts"):
                incoming = [str(x) for x in patch.get(key) or []]
                if incoming:
                    question[key] = sorted({*question.get(key, []), *incoming})
            # updated_at：历史投影据此判断问题状态是否在 as_of 后被改过（review #10）
            payload["updated_at"] = datetime.now(UTC).isoformat()
            self._conn.execute(
                "UPDATE research_plans SET payload_json = ? WHERE plan_id = ? AND namespace = ?",
                (json.dumps(payload, ensure_ascii=False), plan_id, namespace),
            )
            self._conn.commit()
            return dict(question)

    def append_plan_subquestion(
        self, plan_id: str, question_id: str, sub: dict[str, Any], *, namespace: str = "prod",
    ) -> dict[str, Any] | None:
        """追加内部子问题（方案 §8.1：冻结目标，允许内部研究路径演进）。

        硬约束：只写 question.sub_questions，**不触碰 budgets/objective/questions 集合**
        （不允许自动扩大投资范围或预算）；同文本幂等（返回已有条目）。
        锁内读-改-写，与 update_plan_question 同纪律（并行 worker 不互盖）。
        """
        import uuid as _uuid

        text = str(sub.get("text") or "").strip()
        if not text:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM research_plans WHERE plan_id = ? AND namespace = ?",
                (plan_id, namespace),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row[0])
            question = next(
                (q for q in payload.get("questions", [])
                 if q.get("question_id") == question_id),
                None,
            )
            if question is None:
                return None
            subs = question.setdefault("sub_questions", [])
            existing = next((s for s in subs if str(s.get("text") or "").strip() == text), None)
            if existing is not None:
                return dict(existing)  # 幂等：同文本不重复追加
            entry = {
                "sub_id": f"sub-{_uuid.uuid4().hex[:8]}",
                "parent_question_id": question_id,
                "text": text,
                "trigger_evidence": [str(r) for r in (sub.get("trigger_evidence") or [])][:8],
                "priority": str(sub.get("priority") or "medium"),
                "exit_condition": str(sub.get("exit_condition") or ""),
                "status": "open",
                "created_at": datetime.now(UTC).isoformat(),
            }
            subs.append(entry)
            payload["updated_at"] = datetime.now(UTC).isoformat()
            self._conn.execute(
                "UPDATE research_plans SET payload_json = ? WHERE plan_id = ? AND namespace = ?",
                (json.dumps(payload, ensure_ascii=False), plan_id, namespace),
            )
            self._conn.commit()
            return dict(entry)

    def set_plan_status(self, plan_id: str, status: str, *, namespace: str = "prod") -> None:
        """计划收尾状态（同样锁内读改写，不与问题更新互踩）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM research_plans WHERE plan_id = ? AND namespace = ?",
                (plan_id, namespace),
            ).fetchone()
            if row is None:
                return
            payload = json.loads(row[0])
            payload["status"] = status
            self._conn.execute(
                "UPDATE research_plans SET status = ?, payload_json = ?"
                " WHERE plan_id = ? AND namespace = ?",
                (status, json.dumps(payload, ensure_ascii=False), plan_id, namespace),
            )
            self._conn.commit()

    def list_calculation_ids(
        self, entity_kind: str, entity_id: str, t: datetime, *, namespace: str = "prod",
        limit: int = 200,
    ) -> list[str]:
        rows = self._conn.execute(
            "SELECT calculation_id FROM calculation_runs WHERE namespace = ? AND entity_kind = ?"
            " AND entity_id = ? AND created_at <= ? ORDER BY created_at DESC LIMIT ?",
            (namespace, entity_kind, entity_id, t.isoformat(), limit),
        ).fetchall()
        return [r[0] for r in rows]

    def save_plan(self, *, plan_id: str, namespace: str, payload: dict[str, Any]) -> str:
        p = payload
        p.setdefault("created_at", datetime.now(UTC).isoformat())
        p["updated_at"] = p.get("updated_at") or p["created_at"]
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
        self, entity_kind: str, entity_id: str, *, namespace: str = "prod", limit: int = 20,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """计划列表；as_of 限定时只返回当时已创建的计划（review #10）。

        历史投影的问题状态说明：本库不存事件，无法回放 T 时点的逐问题状态；
        投影层（projector）因此对历史快照只展示计划范围，问题状态标为
        「历史投影不可分辨」，不直接复用今日状态冒充当时进展。"""
        sql = (
            "SELECT payload_json FROM research_plans WHERE namespace = ?"
            " AND entity_kind = ? AND entity_id = ?"
        )
        args: list[Any] = [namespace, entity_kind, entity_id]
        if as_of is not None:
            sql += " AND created_at <= ?"
            args.append(as_of.isoformat())
        rows = self._conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", (*args, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]

    # ---------------- 研究论断 ----------------

    def save_claim(
        self, *, claim_id: str, namespace: str, payload: dict[str, Any],
        recorded_at: datetime | None = None,
    ) -> str:
        """写入论断：research_claims 为「当前指针」（INSERT OR REPLACE），
        research_claim_versions 追加版本行（review R3：核验/降级等修订是时态修订，
        不原位改写历史——claims_as_of 按 recorded_at ≤ T 的版本链重建过去状态）。

        recorded_at = 系统获知的逻辑时刻（缺省真实当前时间；核验等带逻辑时钟的
        调用方应显式传入，保证 as_of 重建与事件时序一致）。
        """
        c = payload
        recorded = (recorded_at or datetime.now(UTC)).isoformat()
        dumped = json.dumps(c, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO research_claims (claim_id, namespace, entity_kind,"
                " entity_id, kind, question_id, status, statement, created_at, evidence_cutoff,"
                " run_id, superseded_by, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    claim_id, namespace, c["entity_kind"], c["entity_id"], c["kind"],
                    c.get("question_id"), c.get("status", "draft"), c["statement"],
                    c["created_at"], c.get("evidence_cutoff"), c.get("run_id"),
                    c.get("superseded_by"), dumped,
                ),
            )
            self._conn.execute(
                "INSERT INTO research_claim_versions (claim_id, namespace, entity_kind,"
                " entity_id, recorded_at, payload_json) VALUES (?,?,?,?,?,?)",
                (claim_id, namespace, c["entity_kind"], c["entity_id"], recorded, dumped),
            )
            self._conn.commit()
        return claim_id

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload_json FROM research_claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _claim_payload_as_of(
        self, claim_id: str, namespace: str, t: datetime, *, fallback: str
    ) -> dict[str, Any] | None:
        """claim 在 T 时点的 payload：版本链中 recorded_at ≤ T 的最新版本；
        无版本行（版本表建立前的旧数据，迁移已按 created_at 补基线）回退当前 payload。"""
        row = self._conn.execute(
            "SELECT payload_json FROM research_claim_versions"
            " WHERE namespace = ? AND claim_id = ? AND recorded_at <= ?"
            " ORDER BY seq DESC LIMIT 1",
            (namespace, claim_id, t.isoformat()),
        ).fetchone()
        try:
            return json.loads(row[0] if row else fallback)
        except Exception:  # noqa: BLE001 - 损坏行不拖死整表投影
            return None

    def claims_as_of(
        self,
        entity_kind: str,
        entity_id: str,
        t: datetime,
        *,
        namespace: str = "prod",
        statuses: tuple[str, ...] = ("draft", "validated"),
        include_invalidated: bool = False,
    ) -> list[dict[str, Any]]:
        """as_of(T) 论断投影（review R3/R4）：

        - 时态：按版本链重建 T 时点的 status/verification——今天核验降级一条
          论断，不改写过去时点的查询结果（历史投影不漂）；
        - 失效消费：claim_invalidations（invalidated_at ≤ T）生效的论断默认
          不再出现于投影（失效语义真正影响当前读取）；include_invalidated=True
          时带 invalidated/invalidation 标记返回（审计与整合预览用）。
        """
        rows = self._conn.execute(
            "SELECT claim_id, payload_json FROM research_claims WHERE namespace = ?"
            " AND entity_kind = ? AND entity_id = ? AND created_at <= ?",
            (namespace, entity_kind, entity_id, t.isoformat()),
        ).fetchall()
        invalidated = {
            str(i.get("claim_id")): i
            for i in self.invalidations_as_of(entity_kind, entity_id, t, namespace=namespace)
        }
        out: list[dict[str, Any]] = []
        for claim_id, current_json in rows:
            payload = self._claim_payload_as_of(claim_id, namespace, t, fallback=current_json)
            if payload is None:
                continue
            if str(payload.get("status", "draft")) not in statuses:
                continue
            inv = invalidated.get(claim_id)
            if inv is not None:
                if not include_invalidated:
                    continue
                payload = dict(payload)
                payload["invalidated"] = True
                payload["invalidation"] = {
                    "reason": inv.get("reason"),
                    "invalidated_at": inv.get("invalidated_at"),
                    "source_refs": inv.get("source_refs") or [],
                }
            out.append(payload)
        out.sort(key=lambda c: (str(c.get("created_at") or ""), str(c.get("claim_id") or "")))
        return out

    def claim_invalidation(
        self, claim_id: str, *, namespace: str = "prod", as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        """claim 在 as_of（缺省=当前）已生效的最新失效记录；无 → None。"""
        sql = ("SELECT payload_json FROM claim_invalidations"
               " WHERE namespace = ? AND claim_id = ?")
        args: list[Any] = [namespace, claim_id]
        if as_of is not None:
            sql += " AND invalidated_at <= ?"
            args.append(as_of.isoformat())
        row = self._conn.execute(
            sql + " ORDER BY invalidated_at DESC LIMIT 1", args).fetchone()
        return json.loads(row[0]) if row else None

    def is_claim_invalidated(
        self, claim_id: str, *, namespace: str = "prod", as_of: datetime | None = None,
    ) -> bool:
        """失效语义判定（发布门禁/引用回读用，review R4）。"""
        return self.claim_invalidation(claim_id, namespace=namespace, as_of=as_of) is not None

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
