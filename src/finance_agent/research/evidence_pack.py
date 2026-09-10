"""EvidencePack：问题证据包（tools-plugins 方案 §4.2/§5.4）。

它是给 worker、verifier、S2 和合成器的**共同输入**，不是新的事实库：
全部素材来自既有 typed 数据（Evidence/Observation/Calculation/Claim/问题状态），
按问题/论断组织成可核验的一包——原文、数值、反证、缺口。

纪律：
- 摘要、供应商生成答案、其他 agent 的总结只作为分析材料，不能替代原始证据登记；
- input_hash 冻结输入身份（同引用集同 as_of → 同包，核验结果可复现可归因）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..knowledge.store import BitemporalStore
from .assessment import source_role


class EvidenceSpan(BaseModel):
    """一条可核验的原文/数值素材（ref 可回读，定位随包携带）。"""

    model_config = ConfigDict(extra="forbid")

    ref: str                      # ev-/obs-/calc-/claim-
    kind: str = ""                # evidence/observation/calculation/claim/unresolved
    text: str = ""                # 逐字摘录（evidence）或值摘要（obs/calc）
    source_id: str = ""
    source_role: str = ""         # 一手/二手/供应商（与 PIT 分离）
    url: str | None = None
    available_at: datetime | None = None
    pit_grade: str = ""
    quality: str = "ok"
    locator: dict[str, str] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)  # 期间/单位/币种/维度等数值上下文


class EvidencePack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack_id: str = ""
    entity: str
    namespace: str = "prod"
    question_id: str | None = None
    claim_id: str | None = None
    as_of: datetime
    input_hash: str = ""
    candidate_answer: str = ""
    supporting_spans: list[EvidenceSpan] = Field(default_factory=list)
    counter_spans: list[EvidenceSpan] = Field(default_factory=list)
    contextual_spans: list[EvidenceSpan] = Field(default_factory=list)
    observation_refs: list[str] = Field(default_factory=list)
    calculation_refs: list[str] = Field(default_factory=list)
    source_families: dict[str, int] = Field(default_factory=dict)
    unresolved_refs: list[str] = Field(default_factory=list)
    unresolved_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)

    def compact(self) -> dict[str, Any]:
        """verifier prompt 用的有界投影（原文优先，元数据从简）。"""
        def spans(items: list[EvidenceSpan], cap: int, chars: int) -> list[dict]:
            return [
                {"ref": s.ref, "role": s.source_role or s.source_id,
                 "text": s.text[:chars], "locator": s.locator,
                 **({"meta": s.meta} if s.meta else {})}
                for s in items[:cap]
            ]

        return {
            "entity": self.entity,
            "question_id": self.question_id,
            "candidate_answer": self.candidate_answer[:600],
            "supporting": spans(self.supporting_spans, 12, 700),
            "counter": spans(self.counter_spans, 6, 700),
            "contextual": spans(self.contextual_spans, 6, 300),
            "source_families": self.source_families,
            "unresolved_refs": self.unresolved_refs,
            "unresolved_conflicts": self.unresolved_conflicts[:6],
            "missing_evidence": self.missing_evidence[:8],
        }


def resolve_span(kb: BitemporalStore, metrics: Any, ref: str, *, namespace: str) -> EvidenceSpan:
    """单个引用 → EvidenceSpan（不可解析也返回 span，kind=unresolved，不静默丢）。"""
    try:
        if ref.startswith("ev-"):
            ev = kb.get_evidence(ref)
            return EvidenceSpan(
                ref=ref, kind="evidence", text=ev.verbatim_quote,
                source_id=ev.source_id, source_role=source_role(ev.source_id),
                url=ev.url, available_at=ev.available_at,
                pit_grade=getattr(ev.pit_grade, "value", str(ev.pit_grade)),
                quality=getattr(ev, "quality", "ok"),
                locator={str(k): str(v) for k, v in (ev.locator or {}).items()},
            )
        if ref.startswith("obs-") and metrics is not None:
            obs = metrics.get_observation(ref)
            if obs is None:
                return EvidenceSpan(ref=ref, kind="unresolved")
            text = f"{obs.metric_key}={obs.value} {obs.unit or ''}".strip()
            if obs.raw is not None:
                text += f"（原文 {obs.raw.value_text} {obs.raw.unit_text}）".rstrip()
            return EvidenceSpan(
                ref=ref, kind="observation", text=text,
                source_id=",".join(sorted({source_role_ref(kb, r) for r in obs.evidence_refs})),
                available_at=obs.source_available_at,
                pit_grade=getattr(obs.pit_grade, "value", str(obs.pit_grade)),
                locator={str(k): str(v) for k, v in (obs.locator or {}).items()},
                meta={
                    "metric_key": obs.metric_key, "nature": obs.nature,
                    "basis": obs.basis, "currency": obs.currency,
                    "period": obs.period.fiscal_label or obs.period.end.isoformat(),
                    "frequency": obs.period.frequency, "dimensions": obs.dimensions,
                    "subject": (f"{obs.subject_kind}:{obs.subject_id}"
                                if obs.is_cross_subject else ""),
                },
            )
        if ref.startswith("calc-") and metrics is not None:
            stored = metrics.get_calculation(ref)
            if stored is None:
                return EvidenceSpan(ref=ref, kind="unresolved")
            p = stored.payload
            return EvidenceSpan(
                ref=ref, kind="calculation",
                text=f"{p.get('formula_id')} → {p.get('result')} {p.get('unit') or ''}".strip(),
                meta={"status": p.get("status"), "input_refs": p.get("input_refs"),
                      "warnings": p.get("warnings")},
            )
        if ref.startswith("claim-") and metrics is not None:
            payload = metrics.get_claim(ref)
            if payload is None:
                return EvidenceSpan(ref=ref, kind="unresolved")
            return EvidenceSpan(
                ref=ref, kind="claim", text=str(payload.get("statement") or "")[:700],
                meta={"claim_status": payload.get("status"), "kind": payload.get("kind")},
            )
    except Exception as e:  # noqa: BLE001 - 不可解析显式标记，不拖死整包
        return EvidenceSpan(ref=ref, kind="unresolved", meta={"error": f"{type(e).__name__}: {e}"})
    return EvidenceSpan(ref=ref, kind="unresolved")


def source_role_ref(kb: BitemporalStore, evidence_ref: str) -> str:
    """证据引用的来源角色（观测的证据族归类用；解析不了 = unknown）。"""
    if not evidence_ref.startswith("ev-"):
        return "non_evidence"
    try:
        return source_role(kb.get_evidence(evidence_ref).source_id)
    except Exception:  # noqa: BLE001 - 缺失来源如实归 unknown（不默认一手）
        return "unknown"


def build_evidence_pack(
    kb: BitemporalStore,
    metrics: Any | None,
    *,
    entity_kind: str,
    entity_id: str,
    namespace: str = "prod",
    as_of: datetime | None = None,
    claim_payload: dict[str, Any] | None = None,
    question: dict[str, Any] | None = None,
) -> EvidencePack:
    """从既有 typed 数据组装证据包（不做新检索；缺口如实列出）。"""
    now = as_of or datetime.now(UTC)
    supporting: list[EvidenceSpan] = []
    counter: list[EvidenceSpan] = []
    contextual: list[EvidenceSpan] = []
    unresolved: list[str] = []
    obs_refs: list[str] = []
    calc_refs: list[str] = []

    def collect(refs: list[str], bucket: list[EvidenceSpan]) -> None:
        for ref in refs:
            span = resolve_span(kb, metrics, str(ref), namespace=namespace)
            if span.kind == "unresolved":
                unresolved.append(str(ref))
                continue
            bucket.append(span)
            if span.kind == "observation":
                obs_refs.append(str(ref))
            elif span.kind == "calculation":
                calc_refs.append(str(ref))
            # 观测背后的证据原文也进包（内容核验必须看原文，不看二手摘要）
            if span.kind == "observation" and metrics is not None:
                obs = metrics.get_observation(str(ref))
                for ev_ref in (obs.evidence_refs if obs else []) or []:
                    ev_span = resolve_span(kb, metrics, str(ev_ref), namespace=namespace)
                    if ev_span.kind == "evidence" and not any(
                        s.ref == ev_span.ref for s in bucket
                    ):
                        bucket.append(ev_span)

    candidate_answer = ""
    question_id: str | None = None
    claim_id: str | None = None
    if claim_payload is not None:
        claim_id = str(claim_payload.get("claim_id") or "")
        question_id = claim_payload.get("question_id")
        collect([str(r) for r in (claim_payload.get("support_refs") or [])], supporting)
        collect([str(r) for r in (claim_payload.get("counter_refs") or [])], counter)
        candidate_answer = str(claim_payload.get("statement") or "")
    if question is not None:
        question_id = str(question.get("question_id") or question_id or "")
        collect([str(r) for r in (question.get("support_refs") or [])], contextual)
        collect([str(r) for r in (question.get("counter_refs") or [])], counter)
        if question.get("conclusion"):
            candidate_answer = candidate_answer or str(question.get("conclusion"))

    # 来源族（一手/二手/供应商分离）
    families: dict[str, int] = {}
    for span in [*supporting, *counter, *contextual]:
        if span.kind != "evidence":
            continue
        role = span.source_role or "unknown"
        families[role] = families.get(role, 0) + 1

    # 相关开放冲突（typed：引用观测的语义键；legacy：本实体全部）
    conflicts: list[dict[str, Any]] = []
    if metrics is not None and obs_refs:
        try:
            conflicted = set(metrics.conflicted_semantic_hashes(
                entity_kind, entity_id, namespace=namespace, as_of=now,
                exclude_resolved=True,
            ))
            for ref in obs_refs:
                obs = metrics.get_observation(ref)
                if obs is not None and obs.semantic_hash() in conflicted:
                    conflicts.append({"observation_ref": ref,
                                      "semantic_hash": obs.semantic_hash(),
                                      "metric_key": obs.metric_key})
        except Exception:  # noqa: BLE001 - 冲突查询失败不拖死证据包（缺口如实记）
            conflicts.append({"error": "conflicted_semantic_hashes 查询失败"})
    try:
        for rec in kb.open_conflicts(entity_kind, entity_id, namespace=namespace):
            conflicts.append({"field": rec.field, "fact_id": rec.fact_id})
    except Exception:  # noqa: BLE001 - 同上
        pass

    missing: list[str] = []
    if not supporting and candidate_answer:
        missing.append("结论无支持证据（supporting_spans 为空）")
    if unresolved:
        missing.append(f"引用不可解析：{unresolved}")
    low_quality = [s.ref for s in supporting
                   if s.quality in ("garbled", "needs_ocr")]
    if low_quality:
        missing.append(f"支持证据抽取质量不可用（乱码/需 OCR）：{low_quality}")

    material = {
        "entity": f"{entity_kind}:{entity_id}",
        "namespace": namespace,
        "question_id": question_id, "claim_id": claim_id,
        "as_of": now.isoformat(),
        "support": sorted(s.ref for s in supporting),
        "counter": sorted(s.ref for s in counter),
        "context": sorted(s.ref for s in contextual),
        "candidate_answer": candidate_answer,
    }
    pack = EvidencePack(
        pack_id=f"pack-{hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:12]}",
        entity=f"{entity_kind}:{entity_id}", namespace=namespace,
        question_id=question_id, claim_id=claim_id, as_of=now,
        input_hash=hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:16],
        candidate_answer=candidate_answer,
        supporting_spans=supporting, counter_spans=counter, contextual_spans=contextual,
        observation_refs=sorted(set(obs_refs)), calculation_refs=sorted(set(calc_refs)),
        source_families=families, unresolved_refs=unresolved,
        unresolved_conflicts=conflicts, missing_evidence=missing,
    )
    return pack


__all__ = ["EvidencePack", "EvidenceSpan", "build_evidence_pack", "resolve_span"]
