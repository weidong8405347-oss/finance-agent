"""内容级核验（tools-plugins 方案 §5.4，P2-A）：检查原文是否支持结论。

分工（方案的「检查分工」四条）：
1. 程序硬检查（本模块 run_hard_checks，确定性代码）：引用可解析、数字与原文
   一致、主体/期间错配、开放冲突；
2. 内容检查（content_review，LLM 产出**可审计核验意见**，不是绝对真值）：
   原子论断逐条 supported/partially_supported/contradicted/insufficient、
   遗漏限定条件、推理前提与边界、替代解释；
3. 反证闭环：counter_search 记录查过的来源与范围——没有找到反证时保存检索
   记录，不制造反对意见凑数；
4. 结果落 claim.verification（分项状态）+ research/claim_verified 事件；
   contradicted/insufficient 的 validated 论断降级 draft（发布规则在
   ArtifactValidator：contradicted 硬失败，insufficient 软问题）。

诚实边界：LLM 不可用/解析失败 → content_review_available=False，只做硬检查，
evidence_support 保持 unchecked（不假装核验过）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..eventstore.events import RESEARCH_CLAIM_VERIFIED, Event
from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest
from ..knowledge.store import BitemporalStore
from ..llm.base import LLM
from .artifacts import ClaimVerification
from .evidence_pack import EvidencePack, build_evidence_pack

logger = logging.getLogger("finance_agent.research.verifier")

VERIFY_PROMPT = """\
你是研究论断的内容级核验员（独立于论断作者）。给定论断与证据包，判断**原文是否
真正支持结论**——引用存在不等于支持。纪律：
- 只依据给定原文判断；不用你的记忆补充事实；原文不足以判断就标 insufficient；
- 把论断拆成原子论断（每条一个可独立核验的陈述），逐条给：
  verdict ∈ supported / partially_supported / contradicted / insufficient，
  supporting_refs（证据包里的 ref）、missing_conditions（遗漏的期间/主体/范围/
  口径限定）、mismatches（主体/期间/单位/币种错配说明）；
- 推论类论断额外检查：前提是否明示（premises_explicit）、推理边界是否成立
  （boundary_ok）、有无替代解释（alternative_explanations）；
- 数字必须与原文逐字/等值核对；约估、外推、跨期拼接都要指出；
- 不制造反对意见凑数：证据包里没有反证就在 next_actions 里要求补检索。
只输出 JSON：{"atomic_claims":[{"text":str,"verdict":str,"supporting_refs":[str],
"missing_conditions":[str],"mismatches":[str],"notes":str}],
"reasoning_review":{"premises_explicit":bool,"boundary_ok":bool,
"alternative_explanations":[str]},
"next_actions":[str]}
"""


class AtomicVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    verdict: str  # supported/partially_supported/contradicted/insufficient
    supporting_refs: list[str] = Field(default_factory=list)
    missing_conditions: list[str] = Field(default_factory=list)
    mismatches: list[str] = Field(default_factory=list)
    notes: str = ""


class ReasoningReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    premises_explicit: bool = True
    boundary_ok: bool = True
    alternative_explanations: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    """一次核验的可审计产出（进事件与 claim.verification，不是绝对真值）。"""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    pack_id: str = ""
    input_hash: str = ""
    references_valid: bool = False
    evidence_support: str = "unchecked"
    numeric_checks: str = "unchecked"  # not_applicable/passed/failed/unchecked
    analysis_review: str = "unchecked"  # not_required/passed/failed/unchecked
    counter_evidence_search: bool = False
    content_review_available: bool = False
    atomic: list[AtomicVerdict] = Field(default_factory=list)
    reasoning: ReasoningReview | None = None
    hard_issues: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    reviewed_by: str = ""
    reviewed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status_before: str = ""
    status_after: str = ""


# ---------------- 1. 程序硬检查（确定性代码） ----------------


def _numbers(text: str) -> list[Decimal]:
    from ..knowledge.guard import _numbers as guard_numbers

    out: list[Decimal] = []
    for n in guard_numbers(text or ""):
        try:
            out.append(Decimal(str(n)))
        except InvalidOperation:
            continue
    return out


def _is_year_like(d: Decimal) -> bool:
    return d == d.to_integral_value() and 1900 <= int(d) <= 2100


def _matches(n: Decimal, allowed: list[Decimal]) -> bool:
    return any(abs(n - a) <= max(abs(a) * Decimal("1e-9"), Decimal("1e-12")) for a in allowed)


def run_hard_checks(claim_payload: dict[str, Any], pack: EvidencePack) -> tuple[bool, str, list[str]]:
    """引用/数值/主体/冲突四类硬检查。返回 (references_valid, numeric_state, issues)。"""
    issues: list[str] = []
    references_valid = not pack.unresolved_refs
    if pack.unresolved_refs:
        issues.append(f"引用不可解析：{pack.unresolved_refs}")

    # 数值一致性：论断中的数字（排除年份形态）必须能在支持原文/观测值中定位
    statement = str(claim_payload.get("statement") or "")
    stmt_numbers = [n for n in _numbers(statement) if not _is_year_like(n)]
    allowed: list[Decimal] = []
    for span in pack.supporting_spans:
        allowed.extend(_numbers(span.text))
        for value in (span.meta or {}).values():
            if isinstance(value, str):
                allowed.extend(_numbers(value))
    if not stmt_numbers:
        numeric_state = "not_applicable"
    elif allowed and all(_matches(n, allowed) for n in stmt_numbers):
        numeric_state = "passed"
    else:
        numeric_state = "failed"
        unmatched = [str(n) for n in stmt_numbers if not _matches(n, allowed)]
        issues.append(
            f"论断数字未在支持原文/观测值中逐字定位：{unmatched}"
            + ("（支持材料无可核对数字）" if not allowed else "")
        )

    # 主体/期间错配（跨主体观测必须在授权与表述中显式）
    for span in pack.supporting_spans:
        if span.kind == "observation":
            subject = str((span.meta or {}).get("subject") or "")
            if subject and subject != pack.entity:
                issues.append(f"支持观测 {span.ref} 属于其他主体 {subject}（论断主体 {pack.entity}）")

    # 开放冲突未裁决 → 论断不得无条件成立
    if pack.unresolved_conflicts:
        issues.append(
            f"引用涉及 {len(pack.unresolved_conflicts)} 项开放冲突（先裁决或在论断中显式限定）"
        )
    return references_valid, numeric_state, issues


# ---------------- 2. 内容检查（LLM 核验意见） ----------------


def content_review(llm: LLM | None, pack: EvidencePack, claim_payload: dict[str, Any]) -> dict | None:
    """LLM 内容核验（advisory 输入、硬规则聚合）；不可用/解析失败 → None（诚实降级）。"""
    if llm is None:
        return None
    user = json.dumps({
        "statement": claim_payload.get("statement"),
        "kind": claim_payload.get("kind"),
        "limitations": claim_payload.get("limitations") or [],
        "evidence_pack": pack.compact(),
    }, ensure_ascii=False, default=str)
    try:
        reply = llm.complete(
            [{"role": "system", "content": VERIFY_PROMPT},
             {"role": "user", "content": user}],
            tools=[],
        )
    except Exception as e:  # noqa: BLE001 - 核验模型失败可见，不伪造核验结果
        logger.warning("内容核验 LLM 调用失败（%s）：%s", claim_payload.get("claim_id"), e)
        return {"_error": f"{type(e).__name__}: {e}"}
    return _parse_review(reply.content)


def _parse_review(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("atomic_claims"), list):
        return None
    return data


# ---------------- 3. 聚合 + 落库 ----------------


def _aggregate_support(atomic: list[AtomicVerdict], numeric_state: str) -> str:
    verdicts = [a.verdict for a in atomic]
    if not verdicts:
        return "insufficient"
    if "contradicted" in verdicts:
        return "contradicted"
    if all(v == "insufficient" for v in verdicts):
        return "insufficient"
    if "insufficient" in verdicts or "partially_supported" in verdicts:
        support = "partially_supported"
    else:
        support = "supported"
    if numeric_state == "failed" and support == "supported":
        support = "partially_supported"  # 数字对不上原文：整句最多算部分支持
    return support


def verify_claim(
    kb: BitemporalStore,
    metrics: Any,
    *,
    claim_id: str,
    llm: LLM | None,
    events: EventStore | None = None,
    manifest: RunManifest | None = None,
    namespace: str = "prod",
    entity_kind: str = "stock",
    entity_id: str = "",
    counter_search: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> VerificationResult:
    """核验一条论断并把结果写回 claim.verification（幂等：可重复核验，最新为准）。

    返回 VerificationResult（工具层序列化给模型；事件层落审计）。
    失败路径 fail-loud：claim 不存在/跨上下文 → ValueError。
    """
    payload = metrics.get_claim(claim_id)
    if payload is None:
        raise ValueError(f"论断不存在: {claim_id}")
    if payload.get("namespace", "prod") != namespace:
        raise ValueError(f"论断 {claim_id} 跨命名空间（{payload.get('namespace')} ≠ {namespace}）")
    if entity_id and (payload.get("entity_kind"), payload.get("entity_id")) != (
        entity_kind, entity_id
    ):
        raise ValueError(
            f"论断 {claim_id} 属于其他实体 "
            f"{payload.get('entity_kind')}:{payload.get('entity_id')}（跨上下文拒绝）"
        )
    as_of = now or datetime.now(UTC)
    question = None
    if payload.get("question_id"):
        # 问题上下文（结论/未解决项）随包进核验
        question = {"question_id": payload.get("question_id"),
                    "conclusion": "", "support_refs": [], "counter_refs": []}
    pack = build_evidence_pack(
        kb, metrics, entity_kind=payload.get("entity_kind", entity_kind),
        entity_id=payload.get("entity_id", entity_id), namespace=namespace,
        as_of=as_of, claim_payload=payload, question=question,
    )
    references_valid, numeric_state, hard_issues = run_hard_checks(payload, pack)

    kind = str(payload.get("kind") or "inference")
    review = content_review(llm, pack, payload)
    review_error = ""
    atomic: list[AtomicVerdict] = []
    reasoning: ReasoningReview | None = None
    next_actions: list[str] = list(pack.next_actions)
    content_ok = False
    if isinstance(review, dict) and "_error" in review:
        review_error = str(review["_error"])
    elif review is not None:
        content_ok = True
        for item in review.get("atomic_claims") or []:
            try:
                atomic.append(AtomicVerdict.model_validate({
                    "text": str(item.get("text") or "")[:400],
                    "verdict": str(item.get("verdict") or "insufficient"),
                    "supporting_refs": [str(r) for r in (item.get("supporting_refs") or [])],
                    "missing_conditions": [str(x) for x in (item.get("missing_conditions") or [])],
                    "mismatches": [str(x) for x in (item.get("mismatches") or [])],
                    "notes": str(item.get("notes") or "")[:300],
                }))
            except ValidationError:
                continue  # 单条畸形不拖死整体；下面按缺失处理
        if not atomic:
            content_ok = False  # 解析出 0 条原子核验 = 内容核验不可用（诚实降级）
        rr = review.get("reasoning_review")
        if isinstance(rr, dict):
            try:
                reasoning = ReasoningReview.model_validate(rr)
            except ValidationError:
                reasoning = None
        next_actions.extend(str(a) for a in (review.get("next_actions") or [])[:6])

    # 聚合（硬规则，不由模型自报）
    if content_ok:
        support = _aggregate_support(atomic, numeric_state)
    elif not references_valid:
        support = "insufficient"
    else:
        support = "unchecked"  # 内容审查不可用：诚实标注，不冒充已核验
    if numeric_state == "failed" and support == "supported":
        support = "partially_supported"

    if kind in ("fact_summary", "hypothesis"):
        analysis_state = "not_required"
    elif content_ok and reasoning is not None:
        analysis_state = "passed" if (reasoning.premises_explicit and reasoning.boundary_ok) \
            else "failed"
    else:
        analysis_state = "unchecked"

    # 反证闭环：检索记录（找不到反证也保存查过的范围）；无记录 → 补检索行动项
    counter_recorded = bool(counter_search) or bool(payload.get("counter_refs"))
    if not counter_recorded:
        next_actions.append("补充反证检索并记录范围（queries/sources）——未找到反证也要留痕")

    status_before = str(payload.get("status") or "draft")
    status_after = status_before
    notes_extra: list[str] = []
    if hard_issues:
        notes_extra.extend(hard_issues[:4])
    if review_error:
        notes_extra.append(f"内容审查不可用：{review_error}")
    if not content_ok and not review_error and llm is not None:
        notes_extra.append("内容审查输出不可解析（仅硬检查结果有效）")
    if counter_search:
        notes_extra.append(
            "反证检索记录：queries={q} sources={s} found={f}".format(
                q=(counter_search.get("queries") or [])[:4],
                s=(counter_search.get("sources") or [])[:4],
                f=bool(counter_search.get("found")),
            )
        )
    # 发布规则联动：validated 论断被内容核验推翻 → 降级 draft（不留在正式产物里）
    if status_before == "validated" and support in ("contradicted", "insufficient"):
        status_after = "draft"
        notes_extra.append(f"内容核验 {support}：validated 降级 draft（修正后重新核验）")

    verification = ClaimVerification(
        references_valid=references_valid,
        evidence_support=support,  # type: ignore[arg-type]
        numeric_checks=numeric_state,  # type: ignore[arg-type]
        analysis_review=analysis_state,  # type: ignore[arg-type]
        counter_evidence_search=counter_recorded,
        verified_at=as_of,
        verified_by=(getattr(llm, "model_name", "") or "hard-checks-only")
        + ("+content-review" if content_ok else ""),
        notes=notes_extra,
    )
    updated = dict(payload)
    updated["verification"] = verification.model_dump(mode="json")
    updated["status"] = status_after
    metrics.save_claim(claim_id=claim_id, namespace=namespace, payload=updated)

    result = VerificationResult(
        claim_id=claim_id, pack_id=pack.pack_id, input_hash=pack.input_hash,
        references_valid=references_valid, evidence_support=support,
        numeric_checks=numeric_state, analysis_review=analysis_state,
        counter_evidence_search=counter_recorded, content_review_available=content_ok,
        atomic=atomic, reasoning=reasoning, hard_issues=hard_issues,
        next_actions=next_actions[:8],
        reviewed_by=verification.verified_by, reviewed_at=as_of,
        status_before=status_before, status_after=status_after,
    )
    if events is not None:
        events.append(Event(
            run_id=manifest.run_id if manifest else "verifier",
            type=RESEARCH_CLAIM_VERIFIED,
            payload={
                "claim_id": claim_id,
                "entity": f"{payload.get('entity_kind')}:{payload.get('entity_id')}",
                "pack_id": pack.pack_id, "input_hash": pack.input_hash,
                "references_valid": references_valid,
                "evidence_support": support, "numeric_checks": numeric_state,
                "analysis_review": analysis_state,
                "content_review_available": content_ok,
                "atomic_verdicts": [a.model_dump(mode="json") for a in atomic],
                "hard_issues": hard_issues, "next_actions": result.next_actions,
                "counter_search": counter_search or None,
                "status_before": status_before, "status_after": status_after,
                "reviewed_by": verification.verified_by,
                "namespace": namespace,
            },
        ))
    return result


__all__ = [
    "VerificationResult", "AtomicVerdict", "ReasoningReview",
    "run_hard_checks", "content_review", "verify_claim", "make_verify_claim_tool",
    "VERIFY_PROMPT",
]


def make_verify_claim_tool(
    *, kb: BitemporalStore, metrics: Any, events: EventStore | None,
    manifest: RunManifest | None, namespace: str, entity_kind: str, entity_id: str,
    llm: LLM | None,
    on_reject: Any | None = None,
) -> Any:
    """verify_claim 工具工厂（S1 worker 与 S2 整合共用同一实现）。

    on_reject(claim_id, reason)：拒绝记账回调（S1 的 tracker；可选）。
    """
    import json as _json

    from ..knowledge.normalize import normalize_entity_id

    canonical_id = normalize_entity_id(entity_kind, entity_id)

    def verify_claim_tool(args: dict[str, Any]) -> dict[str, Any]:
        claim_id = str(args.get("claim_id") or "")
        if not claim_id:
            return {"content": "rejected: claim_id 必填", "provenance": []}
        counter_search = args.get("counter_search")
        if counter_search is not None and not isinstance(counter_search, dict):
            return {"content": "rejected: counter_search 必须是对象"
                                   "{queries,sources,found,notes}", "provenance": []}
        try:
            result = verify_claim(
                kb, metrics, claim_id=claim_id, llm=llm,
                events=events, manifest=manifest, namespace=namespace,
                entity_kind=entity_kind, entity_id=canonical_id,
                counter_search=counter_search,
            )
        except ValueError as e:
            if on_reject is not None:
                on_reject(claim_id, str(e))
            return {"content": f"rejected: {e}", "provenance": []}
        return {"content": _json.dumps(
            result.model_dump(mode="json"), ensure_ascii=False, default=str
        ), "provenance": []}

    return verify_claim_tool
