"""profile.consolidator（tools-plugins 方案 §9.1/§9.3，P2-B）：档案整合与增量刷新。

S2 从「重写 thesis」扩展为：读取冻结基线 → 汇总本轮候选 → 语义去重 →
冲突清单 → 依赖失效 → 预期 diff → 幂等提交（留变化说明）。

纪律：
- prepare 是确定性只读预览（不写任何状态）；commit 才落记录，且必须带
  expected_base_hash——基线过期拒绝提交（不拿旧基线盖新数据）；
- 依赖失效走**追加记录**（claim_invalidations，按 invalidated_at 时态合并）：
  旧快照保持原样，历史投影不泄露「今天才作废」的状态；
- change_set_id 幂等：重跑 commit 不重复落失效记录；
- 事实与分析分层：整合结论以 Claim/失效记录表达，不直接改冻结产物。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from ..eventstore.events import Event
from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest
from ..knowledge.metric_store import MetricStore
from ..knowledge.store import BitemporalStore
from ..research.assessment import numeric_consistency_scan

logger = logging.getLogger("finance_agent.dossier.consolidator")

#: 整合提交事件（变化说明的审计锚点）
PROFILE_UPDATE_COMMITTED = "profile/update_committed"
#: 论断失效事件（依赖变更触发；旧快照不变）
CLAIM_INVALIDATED = "profile/claim_invalidated"


class ConsolidationError(Exception):
    """整合提交被拒（基线过期/引用非法/幂等冲突）——fail-loud，不静默部分提交。"""


def _sha16(material: Any) -> str:
    canon = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


class ProfileConsolidator:
    def __init__(
        self, *, kb: BitemporalStore, metrics: MetricStore,
        events: EventStore | None = None,
    ):
        self._kb = kb
        self._metrics = metrics
        self._events = events

    # ---------------- 依赖图（§9.3：document → observation → calculation → claim → module）

    def dependency_graph(
        self, entity_kind: str, entity_id: str, *, namespace: str = "prod",
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        now = as_of or datetime.now(UTC)
        edges: list[dict[str, str]] = []
        observations = self._metrics.observations_as_of(
            entity_kind, entity_id, now, namespace=namespace)
        for o in observations:
            for ev in o.evidence_refs or []:
                edges.append({"from": str(ev), "to": o.observation_id,
                              "kind": "evidence→observation"})
        calc_ids = self._metrics.list_calculation_ids(
            entity_kind, entity_id, now, namespace=namespace)
        for cid in calc_ids:
            stored = self._metrics.get_calculation(cid)
            if stored is None:
                continue
            for ref in stored.payload.get("input_refs") or []:
                if ref.get("ref_id"):
                    edges.append({"from": str(ref["ref_id"]), "to": cid,
                                  "kind": "input→calculation"})
        claims = self._metrics.claims_as_of(
            entity_kind, entity_id, now, namespace=namespace,
            statuses=("draft", "validated"))
        module_of = self._question_module_map(entity_kind, entity_id, namespace)
        for c in claims:
            for ref in [*(c.get("support_refs") or []), *(c.get("counter_refs") or [])]:
                edges.append({"from": str(ref), "to": str(c.get("claim_id")),
                              "kind": "ref→claim"})
            module = module_of.get(str(c.get("question_id") or "")) or (
                f"legacy:{c['legacy_field']}" if c.get("legacy_field") else "general"
            )
            edges.append({"from": str(c.get("claim_id")), "to": f"module:{module}",
                          "kind": "claim→module"})
        invalidated = sorted(
            self._metrics.invalidated_observation_ids(namespace=namespace, as_of=now)
        )
        return {
            "entity": f"{entity_kind}:{entity_id}",
            "as_of": now.isoformat(),
            "nodes": {
                "observations": [o.observation_id for o in observations],
                "calculations": list(calc_ids),
                "claims": [str(c.get("claim_id")) for c in claims],
                "invalidated_observations": invalidated,
            },
            "edges": edges,
        }

    def _question_module_map(
        self, entity_kind: str, entity_id: str, namespace: str,
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            plans = self._metrics.plans_for(
                entity_kind, entity_id, namespace=namespace, limit=5)
        except Exception:  # noqa: BLE001 - 计划缺失不拖死整合（module 归 general）
            return out
        for plan in plans:
            for q in plan.get("questions") or []:
                if q.get("question_id") and q.get("module"):
                    out.setdefault(str(q["question_id"]), str(q["module"]))
        return out

    # ---------------- prepare（确定性只读预览） ----------------

    def state_hash(
        self, entity_kind: str, entity_id: str, *, namespace: str = "prod",
        as_of: datetime | None = None, base_snapshot_id: str | None = None,
    ) -> str:
        """研究数据基线指纹（expected_base_hash 口径）。

        只覆盖 S2 整合自身不会写的输入：观测投影（id+值+状态）、计算 id、
        基线快照。thesis Fact/Claim、裁决、失效记录都是整合动作本身的产出，
        不进指纹（否则 prepare→thesis→commit 的正常工作流会被自己触发
        「基线过期」）；并发研究写入的新观测/修订会改变指纹 → 拒绝提交。
        """
        now = as_of or datetime.now(UTC)
        obs = self._metrics.observations_as_of(
            entity_kind, entity_id, now, namespace=namespace)
        calcs = self._metrics.list_calculation_ids(
            entity_kind, entity_id, now, namespace=namespace)
        base = None
        if base_snapshot_id:
            snap = self._metrics.get_snapshot(base_snapshot_id)
            base = (snap or {}).get("data_hash")
        return _sha16({
            "entity": f"{entity_kind}:{entity_id}", "namespace": namespace,
            "base_snapshot": base_snapshot_id, "base_data_hash": base,
            "observations": sorted(
                (o.observation_id, str(o.value), str(o.status)) for o in obs),
            "calculations": sorted(calcs),
        })

    def prepare_update(
        self, entity_kind: str, entity_id: str, *,
        base_snapshot_id: str | None = None,
        candidate_refs: list[str] | None = None,
        namespace: str = "prod", as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """整合预览：待合并、重复、冲突、失效依赖、预期 diff（全部只读）。"""
        now = as_of or datetime.now(UTC)
        base = None
        if base_snapshot_id:
            base = self._metrics.get_snapshot(base_snapshot_id)
            if base is None:
                raise ConsolidationError(f"基线快照不存在: {base_snapshot_id}")
        else:
            base = self._metrics.latest_snapshot(
                entity_kind, entity_id, namespace=namespace)
        base_inputs = (base or {}).get("inputs") or {}
        base_ctx = (base or {}).get("context") or {}

        def _base_ids(key: str) -> set[str]:
            return {str(x) for x in (base_inputs.get(key) or [])}

        # 快照的 claims 是冻结 payload 列表（状态不漂移），id 从中取
        base_claim_ids = {
            str(c.get("claim_id")) for c in (base_inputs.get("claims") or [])
            if isinstance(c, dict)
        }

        obs = self._metrics.observations_as_of(
            entity_kind, entity_id, now, namespace=namespace)
        claims = self._metrics.claims_as_of(
            entity_kind, entity_id, now, namespace=namespace,
            statuses=("draft", "validated"))
        calcs = self._metrics.list_calculation_ids(
            entity_kind, entity_id, now, namespace=namespace)
        view = self._kb.view(entity_kind, entity_id, now, namespace=namespace)

        new_obs = [o.observation_id for o in obs
                   if o.observation_id not in _base_ids("observation_ids")]
        new_claims = [str(c.get("claim_id")) for c in claims
                      if str(c.get("claim_id")) not in base_claim_ids]
        new_calcs = [c for c in calcs if c not in _base_ids("calculation_ids")]

        # 冲突（typed 竞争语义键 + legacy 字段）
        typed_conflicts = []
        for sem in self._metrics.conflicted_semantic_hashes(
            entity_kind, entity_id, namespace=namespace, as_of=now, exclude_resolved=True,
        ):
            versions = self._metrics.observation_history(sem, namespace=namespace, as_of=now)
            typed_conflicts.append({
                "semantic_hash": sem,
                "metric_key": versions[0].metric_key if versions else "",
                "versions": [{"observation_id": v.observation_id, "value": v.value}
                             for v in versions],
            })
        legacy_conflicts = sorted({r.field for r in self._kb.open_conflicts(
            entity_kind, entity_id, namespace=namespace)})

        # 依赖失效（§9.3）：已失效观测的下游计算/论断/产物 = 待重审
        invalidated_obs = sorted(
            self._metrics.invalidated_observation_ids(namespace=namespace, as_of=now))
        stale_dependents: list[dict[str, Any]] = []
        already = {
            str(i.get("claim_id"))
            for i in self._metrics.invalidations_as_of(
                entity_kind, entity_id, now, namespace=namespace)
        }
        for oid in invalidated_obs:
            refs = self._metrics.refs_to(oid, namespace=namespace)
            for kind in ("calculations", "claims", "artifacts"):
                for rid in refs.get(kind, []):
                    stale_dependents.append({
                        "ref": rid, "kind": kind[:-1], "via_observation": oid,
                        "already_invalidated": (kind == "claims" and rid in already),
                    })

        # 语义去重线索：同值异维度（语义键漂移候选，F10 扫描复用）
        scan = numeric_consistency_scan(list(obs))

        # 候选引用分类（模型圈定的本轮整合素材；不可解析显式列出）
        candidates: dict[str, list[str]] = {"resolved": [], "unresolved": []}
        for ref in candidate_refs or []:
            if self._ref_exists(str(ref), namespace):
                candidates["resolved"].append(str(ref))
            else:
                candidates["unresolved"].append(str(ref))

        module_of = self._question_module_map(entity_kind, entity_id, namespace)
        touched = sorted({
            module_of.get(str(c.get("question_id") or ""), "general")
            for c in claims if str(c.get("claim_id")) in set(new_claims)
        })
        expected_diff = {
            "base_snapshot_id": base_ctx.get("snapshot_id"),
            "base_data_hash": (base or {}).get("data_hash"),
            "new_observations": len(new_obs), "new_claims": len(new_claims),
            "new_calculations": len(new_calcs),
            "modules_touched": touched,
            "open_conflicts": len(typed_conflicts) + len(legacy_conflicts),
            "stale_dependents": len(stale_dependents),
        }
        change_set_id = "chg-" + _sha16({
            "entity": f"{entity_kind}:{entity_id}", "namespace": namespace,
            "base": base_ctx.get("snapshot_id"),
            "new_obs": sorted(new_obs), "new_claims": sorted(new_claims),
            "new_calcs": sorted(new_calcs),
            "stale": sorted(d["ref"] for d in stale_dependents),
            "conflicts": sorted(c["semantic_hash"] for c in typed_conflicts)
            + legacy_conflicts,
        })[:12]
        return {
            "change_set_id": change_set_id,
            "expected_base_hash": self.state_hash(
                entity_kind, entity_id, namespace=namespace, as_of=now,
                base_snapshot_id=base_ctx.get("snapshot_id"),
            ),
            "entity": f"{entity_kind}:{entity_id}",
            "as_of": now.isoformat(),
            "base_snapshot": {
                "snapshot_id": base_ctx.get("snapshot_id"),
                "data_hash": (base or {}).get("data_hash"),
                "as_of": base_ctx.get("as_of"),
            } if base else None,
            "merges": {
                "new_observations": new_obs[:100], "new_claims": new_claims[:100],
                "new_calculations": new_calcs[:50],
                "fact_fields": sorted(view)[:50],
            },
            "duplicates": scan["same_value_different_dims"],
            "scale_suspects": scan["scale_suspect_pairs"],
            "conflicts": {"typed": typed_conflicts, "legacy_fields": legacy_conflicts},
            "invalidated_dependencies": {
                "observations": invalidated_obs,
                "stale_dependents": stale_dependents[:50],
            },
            "candidates": candidates,
            "expected_diff": expected_diff,
            "hint": ("整合后 commit_profile_update(change_set_id, expected_base_hash) 幂等提交；"
                     "冲突先 adjudicate_conflict；需要作废的依赖论断在 commit 的 "
                     "invalidate_claims 里给 claim_id+reason（追加记录，旧快照不变）"),
        }

    def _ref_exists(self, ref: str, namespace: str) -> bool:
        from ..research.artifacts import ref_resolvable

        return ref_resolvable(self._kb, self._metrics, ref, namespace=namespace)

    # ---------------- commit（幂等提交） ----------------

    def commit_update(
        self, entity_kind: str, entity_id: str, *,
        change_set_id: str, expected_base_hash: str,
        note: str = "", invalidate_claims: list[dict[str, Any]] | None = None,
        base_snapshot_id: str | None = None,
        manifest: RunManifest | None = None, namespace: str = "prod",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """幂等提交整合：校验基线哈希 → 追加失效记录 → 落提交台账与事件。

        - 同 change_set_id 重放：返回首次结果（不重复落失效记录）；
        - expected_base_hash 与当前状态不符：拒绝（基线过期，重新 prepare）；
        - invalidate_claims: [{claim_id, reason, source_refs?}]——逐条校验归属后
          追加 claim_invalidations（时态记录，旧快照不变）。
        """
        ts = now or datetime.now(UTC)
        if not str(change_set_id or "").strip():
            raise ConsolidationError(
                "commit 必须带 prepare_profile_update 返回的 change_set_id"
            )
        existing = self._metrics.get_profile_update_commit(
            change_set_id, namespace=namespace)
        if existing is not None:
            return {"idempotent_replay": True, **existing}
        if not str(note or "").strip():
            raise ConsolidationError("commit 必须给 note（变化说明进审计与档案历史）")
        current = self.state_hash(
            entity_kind, entity_id, namespace=namespace, as_of=ts,
            base_snapshot_id=base_snapshot_id,
        )
        if current != expected_base_hash:
            raise ConsolidationError(
                f"基线已变化（expected {expected_base_hash}，当前 {current}）——"
                "有并发写入或数据更新；重新 prepare_profile_update 后再提交"
                "（不允许拿过期基线盖新数据）"
            )
        invalidations: list[dict[str, Any]] = []
        for item in invalidate_claims or []:
            claim_id = str(item.get("claim_id") or "")
            reason = str(item.get("reason") or "").strip()
            if not claim_id or not reason:
                raise ConsolidationError(
                    f"invalidate_claims 条目必须含 claim_id 与 reason（收到 {item!r}）")
            claim = self._metrics.get_claim(claim_id)
            if claim is None or claim.get("namespace", "prod") != namespace or (
                claim.get("entity_kind"), claim.get("entity_id"),
            ) != (entity_kind, entity_id):
                raise ConsolidationError(
                    f"论断 {claim_id} 不存在或不属于 {entity_kind}:{entity_id}"
                    "（跨上下文失效拒绝）")
            invalidation_id = f"inval-{uuid.uuid4().hex[:10]}"
            self._metrics.save_claim_invalidation(
                invalidation_id=invalidation_id, namespace=namespace,
                entity_kind=entity_kind, entity_id=entity_id,
                claim_id=claim_id, reason=reason,
                source_refs=[str(r) for r in (item.get("source_refs") or [])],
                invalidated_at=ts,
                run_id=manifest.run_id if manifest else None,
            )
            invalidations.append({"invalidation_id": invalidation_id,
                                  "claim_id": claim_id, "reason": reason})
            if self._events is not None:
                self._events.append(Event(
                    run_id=manifest.run_id if manifest else "consolidator",
                    type=CLAIM_INVALIDATED,
                    payload={
                        "invalidation_id": invalidation_id,
                        "entity": f"{entity_kind}:{entity_id}",
                        "claim_id": claim_id, "reason": reason,
                        "source_refs": [str(r) for r in (item.get("source_refs") or [])],
                        "namespace": namespace,
                    },
                ))
        payload = {
            "change_set_id": change_set_id,
            "entity": f"{entity_kind}:{entity_id}",
            "namespace": namespace,
            "expected_base_hash": expected_base_hash,
            "committed_at": ts.isoformat(),
            "note": note,
            "invalidations": invalidations,
            "run_id": manifest.run_id if manifest else None,
        }
        created = self._metrics.save_profile_update_commit(
            change_set_id=change_set_id, namespace=namespace,
            entity_kind=entity_kind, entity_id=entity_id,
            expected_base_hash=expected_base_hash, committed_at=ts,
            run_id=manifest.run_id if manifest else None, payload=payload,
        )
        if not created:
            # 并发窗口：另一提交先落——读回首次结果（幂等语义不变）
            first = self._metrics.get_profile_update_commit(
                change_set_id, namespace=namespace)
            return {"idempotent_replay": True, **(first or payload)}
        if self._events is not None:
            self._events.append(Event(
                run_id=manifest.run_id if manifest else "consolidator",
                type=PROFILE_UPDATE_COMMITTED,
                payload=payload,
            ))
        logger.info(
            "档案整合提交 %s:%s change_set=%s（失效 %d 条论断）",
            entity_kind, entity_id, change_set_id, len(invalidations),
        )
        return {
            "committed": True, **payload,
            "snapshot_note": ("新快照由下一次投影/发布按 data_hash 幂等生成；"
                              "旧快照与历史投影保持不变（失效按 invalidated_at 时态合并）"),
        }


__all__ = [
    "ProfileConsolidator", "ConsolidationError",
    "PROFILE_UPDATE_COMMITTED", "CLAIM_INVALIDATED",
]
