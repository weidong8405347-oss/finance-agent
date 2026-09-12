"""Investment Objects 推导（升级方案 §12-§15/§28/§34）：从冻结输入推导确定性版本。

输入：as_of 投影的 claims（含 verification 分项状态）、冻结计划、结构化产物
（validation_timeline / candidate_assessment）。输出：ThesisObject 列表 +
MoatAssessment 列表（进 DossierSnapshot.investment_objects）。

硬纪律：
- 不从文本猜置信度——confidence 只来自「claim 状态 × 内容核验状态」的映射
  （CONFIDENCE_FROM_STATUS），basis 字段如实写明推导链；
- 不从文本猜多空方向——direction 恒为 None（直到有确定性来源）；
- monitor 链接只做确定性关联：时间线条目与论点的证据引用相交，或公司引用相交；
- 反证义务（§14）：counter_refs 为空 → bear_case_status=unmet，
  前端标「反证义务未履行」，不假装无反证；
- 护城河十维评分无逐维度评级证据时留空（score=None），不编造。
"""

from __future__ import annotations

import re
from typing import Any

from .structures import MOAT_DIMENSIONS, MoatAssessment, MoatDimension, ThesisObject

#: claim 状态 × 内容核验状态 → confidence（状态映射，不是统计置信度；
#: 映射值的选择约定见各 basis 文案，前端展示时必须与 basis 一起呈现或仅作排序用）
_CONFIDENCE_VALIDATED = {
    "supported": 0.8,
    "partially_supported": 0.6,
    "unchecked": 0.5,
    "insufficient": 0.35,
    "contradicted": 0.15,
}
_CONFIDENCE_DRAFT = 0.3

#: 计划问题优先级 → importance（确定性映射；无计划归属的论点 importance=None）
_IMPORTANCE_FROM_PRIORITY = {"high": 0.9, "medium": 0.6, "low": 0.3}

#: 进入 ThesisObject 的 claim 类型（判断层；fact_summary 是事实层 §15，不进论点对象）
_THESIS_KINDS = ("analysis", "inference", "hypothesis")

#: theses 数量上限（§9 密度纪律；超出按 importance/support 排序截断）
THESIS_CAP = 12

_SENTENCE_SPLIT = re.compile(r"[。；;\n]|(?<=[.!?])\s")


def _confidence_of(claim: dict[str, Any]) -> tuple[float | None, str]:
    """confidence = f(status, verification.evidence_support)；返回 (值, 推导说明)。"""
    status = str(claim.get("status") or "")
    support = str((claim.get("verification") or {}).get("evidence_support") or "unchecked")
    if status == "validated":
        value = _CONFIDENCE_VALIDATED.get(support)
        if value is None:
            return None, ""
        basis = {
            "supported": "引用校验通过 + 内容核验支持",
            "partially_supported": "引用校验通过 + 内容核验部分支持",
            "unchecked": "引用校验通过（内容未核验）",
            "insufficient": "引用校验通过但内容核验证据不足",
            "contradicted": "引用校验通过但内容核验矛盾",
        }[support]
        return value, f"{basis} → {value}（状态映射，非统计置信度）"
    if status == "draft":
        return _CONFIDENCE_DRAFT, f"草稿（未经校验）→ {_CONFIDENCE_DRAFT}（状态映射）"
    return None, ""


def _title_of(claim: dict[str, Any], question: dict[str, Any]) -> str:
    """标题：配方问题原文优先；否则论点首句（在句读处截断，≤60 字，不中途断词改写）。

    targeted 自定义问题不用原文（事故 2026-09-12：targeted 问题的「文本」是操作指令
    ——"这是结构补齐题…"被当成论点标题泄进投资者视图，违反 §10 研究过程信息
    不进投资者视图）。
    """
    qid = str(question.get("question_id") or "")
    text = str(question.get("text") or "")
    if text and not qid.startswith("targeted-"):
        return text
    statement = str(claim.get("statement") or "")
    first = _SENTENCE_SPLIT.split(statement, maxsplit=1)[0].strip()
    if len(first) > 60:
        first = first[:60].rstrip() + "…"
    return first


def derive_theses(
    claims: list[dict[str, Any]],
    structures: dict[str, Any],
    plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """claims + 时间线 + 候选 → ThesisObject 列表（superseded 不进；按重要性排序）。"""
    questions = {
        str(q.get("question_id")): q
        for q in (plan or {}).get("questions", []) or []
        if q.get("question_id")
    }
    timeline_items = ((structures or {}).get("validation_timeline") or {}).get("items") or []
    candidates = ((structures or {}).get("candidate_assessment") or {}).get("candidates") or []

    def _related_companies(claim: dict[str, Any]) -> list[str]:
        """确定性公司关联：候选名/实体 id 在论点中逐字出现（子串匹配，不模糊推断）。"""
        statement = str(claim.get("statement") or "")
        out: list[str] = []
        for c in candidates:
            eid = str(c.get("entity_id") or "")
            name = str(c.get("name") or "")
            if (eid and eid in statement) or (name and name in statement):
                out.append(eid or name)
        return out

    out: list[ThesisObject] = []
    for claim in claims:
        if claim.get("status") == "superseded":
            continue  # 被替代的论点保留在 claims 区（审计轨迹），不进论点对象
        if str(claim.get("kind") or "") not in _THESIS_KINDS:
            continue
        supports = [str(r) for r in claim.get("support_refs") or []]
        contradicts = [str(r) for r in claim.get("counter_refs") or []]
        related = _related_companies(claim)
        ref_set = set(supports) | set(contradicts)
        # monitor：证据相交 或 公司相交（确定性；均无 → 空，不猜关联）
        monitor: list[str] = []
        for item in timeline_items:
            item_refs = {str(r) for r in item.get("evidence_refs") or []}
            item_companies = {str(x) for x in item.get("company_refs") or []}
            if (ref_set & item_refs) or (set(related) & item_companies):
                event = str(item.get("event") or "")
                if event and event not in monitor:
                    monitor.append(event)
        qid = str(claim.get("question_id") or "")
        question = questions.get(qid) or {}
        priority = str(question.get("priority") or "")
        confidence, basis = _confidence_of(claim)
        out.append(ThesisObject(
            id=str(claim.get("claim_id") or ""),
            title=_title_of(claim, question),
            summary=str(claim.get("statement") or ""),
            kind=str(claim.get("kind") or ""),
            status=str(claim.get("status") or ""),
            importance=_IMPORTANCE_FROM_PRIORITY.get(priority),
            confidence=confidence,
            confidence_basis=basis,
            direction=None,  # 不从文本猜多空
            support_count=len(supports),
            counter_count=len(contradicts),
            unresolved_count=len(claim.get("limitations") or []),
            supports=supports,
            contradicts=contradicts,
            monitor=monitor,
            related_companies=related,
            bear_case_status="met" if contradicts else "unmet",
        ))
    # 排序：importance（None 沉底）→ support_count → created 稳定性由原列表序保证
    out.sort(key=lambda t: (
        -(t.importance if t.importance is not None else -1.0),
        -t.support_count,
    ))
    return [t.model_dump(mode="json") for t in out[:THESIS_CAP]]


def derive_moat_assessments(
    claims: list[dict[str, Any]],
    structures: dict[str, Any],
) -> list[dict[str, Any]]:
    """候选 → MoatAssessment 骨架（§28）：十维 score 留空 + 证据组原文 + 相关论断。

    评分需要逐维度评级证据（当前数据结构未含）——骨架承载证据与关联，
    分数通道是后续项（不从四维证据组猜十维分数）。
    """
    candidates = ((structures or {}).get("candidate_assessment") or {}).get("candidates") or []
    if not candidates:
        return []
    out: list[MoatAssessment] = []
    for c in candidates:
        entity_id = str(c.get("entity_id") or "")
        name = str(c.get("name") or entity_id)
        claim_refs = [
            str(cl.get("claim_id"))
            for cl in claims
            if cl.get("status") != "superseded"
            and ((entity_id and entity_id in str(cl.get("statement") or ""))
                 or (name and name in str(cl.get("statement") or "")))
        ]
        out.append(MoatAssessment(
            entity_id=entity_id,
            name=name,
            tier=str(c.get("tier") or ""),
            dimensions={d: MoatDimension() for d in MOAT_DIMENSIONS},
            evidence_groups={
                "moat": [str(x) for x in c.get("moat_evidence") or []],
                "commercial": [str(x) for x in c.get("commercial_evidence") or []],
                "sustainability": [str(x) for x in c.get("sustainability_evidence") or []],
                "counter": [str(x) for x in c.get("counter_evidence") or []],
            },
            claim_refs=claim_refs,
            notes=["十维评分需逐维度评级证据（当前结构未含）——分数留空，不编造；"
                   "证据组为候选评估原文，未按维度归因。"],
        ))
    return [m.model_dump(mode="json") for m in out]


def derive_investment_objects(
    claims: list[dict[str, Any]],
    structures: dict[str, Any],
    plan: dict[str, Any] | None,
) -> dict[str, Any]:
    """快照级 Investment Objects（§34）：theses + moat_assessments。"""
    return {
        "theses": derive_theses(claims, structures, plan),
        "moat_assessments": derive_moat_assessments(claims, structures),
    }


__all__ = [
    "derive_investment_objects", "derive_theses", "derive_moat_assessments", "THESIS_CAP",
]
