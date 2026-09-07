"""ResearchAssessment：研究充分度评估（设计 §7.6）。

字段完整度（旧 completeness）只回答「基础字段覆盖」；本模块回答「研究是否充分」：
- Integrity（硬门禁）：引用可解析、数字可重算、无时态/命名空间越界、无伪造来源；
- Question coverage：已通过验收的适用关键问题 / 冻结计划中的适用关键问题
  （unavailable 不计已回答；disputed/unavailable 必须有原因与尝试记录）；
- Evidence quality / Analytical depth / Model reproducibility：披露与结构检查；
- verdict = sufficient / partial / blocked；不用加权总分覆盖完整性失败。

LLM judge 只负责解释质量与潜在 gaps（rubric.py，advisory）；硬门禁全部由本代码运行，
rubric 满分不能覆盖引用失败或未来信息。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .plan import ResearchPlan

logger = logging.getLogger("finance_agent.research.assessment")


class IntegrityCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    detail: str = ""


class CoverageStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applicable: int = 0  # 适用问题（非 not_applicable）
    key_applicable: int = 0  # 适用关键问题（high 优先级）
    answered: int = 0
    disputed: int = 0
    unavailable: int = 0
    unanswered: int = 0
    gathering: int = 0
    coverage: float = 0.0  # answered / applicable
    key_coverage: float = 0.0  # answered(key) / key_applicable
    violations: list[str] = Field(default_factory=list)  # high 优先级问题的门禁违例


class ResearchAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessment_id: str = Field(default_factory=lambda: f"assess-{uuid.uuid4().hex[:10]}")
    plan_id: str
    entity: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    integrity_checks: list[IntegrityCheck] = Field(default_factory=list)
    question_coverage: CoverageStats = Field(default_factory=CoverageStats)
    evidence_quality: dict[str, Any] = Field(default_factory=dict)
    analytical_depth: dict[str, Any] = Field(default_factory=dict)
    model_reproducibility: dict[str, Any] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    stop_reason: str = ""  # converged/budget/stalled/cancelled/coverage_met/integrity_failed
    verdict: str = "partial"  # sufficient / partial / blocked
    hard_gate_passed: bool = False
    notes: list[str] = Field(default_factory=list)


def coverage_of(plan: ResearchPlan) -> CoverageStats:
    """按冻结计划计算问题覆盖（unavailable 不计已回答；disputed/unavailable 需原因与尝试）。"""
    applicable = [q for q in plan.questions if q.status != "not_applicable"]
    key = [q for q in applicable if q.priority == "high"]
    answered = [q for q in applicable if q.status == "answered"]
    key_answered = [q for q in key if q.status == "answered"]
    violations: list[str] = []
    for q in key:
        if q.status in ("disputed", "unavailable"):
            if not q.unresolved and not q.attempts and not q.conclusion:
                violations.append(
                    f"{q.question_id}: {q.status} 但无原因/尝试记录（门禁要求显式留痕）"
                )
        elif q.status in ("unanswered", "gathering"):
            violations.append(f"{q.question_id}: 关键问题仍为 {q.status}")
    return CoverageStats(
        applicable=len(applicable),
        key_applicable=len(key),
        answered=len(answered),
        disputed=sum(1 for q in applicable if q.status == "disputed"),
        unavailable=sum(1 for q in applicable if q.status == "unavailable"),
        unanswered=sum(1 for q in applicable if q.status == "unanswered"),
        gathering=sum(1 for q in applicable if q.status == "gathering"),
        coverage=(len(answered) / len(applicable)) if applicable else 0.0,
        key_coverage=(len(key_answered) / len(key)) if key else 0.0,
        violations=violations,
    )


def assess(
    plan: ResearchPlan,
    *,
    claims: list[dict[str, Any]],
    observations: list[Any],  # list[MetricObservation]
    calculations: list[dict[str, Any]],
    validation_issues: list[Any] | None = None,  # list[ValidationIssue]
    open_conflicts: int = 0,
    stale_fields: list[str] | None = None,
    stop_reason: str = "",
    now: datetime | None = None,
) -> ResearchAssessment:
    """确定性评估（硬门禁由代码运行）。LLM rubric 结果不进本函数的门禁判断。"""
    issues = list(validation_issues or [])
    hard_issues = [i for i in issues if getattr(i, "hard", True)]
    checks: list[IntegrityCheck] = []

    # 1) 引用可解析（产物验证的硬 issue = 引用失败）
    unresolved = [i for i in hard_issues if str(getattr(i, "code", "")).startswith("unresolved")]
    checks.append(IntegrityCheck(
        name="citations_resolvable",
        passed=not unresolved,
        detail=f"{len(unresolved)} 个引用不可解析" if unresolved else "全部引用可解析",
    ))

    # 2) 数字可重算：reported 观测带 raw 的必须能重算血缘
    from ..knowledge.normalization import recompute_lineage

    recompute_failures: list[str] = []
    recomputable = 0
    for obs in observations:
        if obs.raw is None or obs.value is None:
            continue
        try:
            recompute_lineage(obs)
            recomputable += 1
        except Exception as e:
            recompute_failures.append(f"{obs.metric_key}: {e}")
    checks.append(IntegrityCheck(
        name="numbers_recomputable",
        passed=not recompute_failures,
        detail=(
            f"{recomputable} 条观测血缘可重算"
            + (f"；{len(recompute_failures)} 条失败: {recompute_failures[:3]}" if recompute_failures else "")
        ),
    ))

    # 3) 时态越界：观测 knowledge_time 早于其证据 available_at（写侧已拦，这里复核）
    temporal_failures = [
        i for i in hard_issues if "temporal" in str(getattr(i, "code", ""))
    ]
    checks.append(IntegrityCheck(
        name="no_temporal_violation",
        passed=not temporal_failures,
        detail=f"{len(temporal_failures)} 处时态越界" if temporal_failures else "无时态越界",
    ))

    # 4) validated 论断必须有支持引用
    bad_claims = [
        c.get("claim_id", "?") for c in claims
        if c.get("status") == "validated" and not c.get("support_refs")
    ]
    checks.append(IntegrityCheck(
        name="claims_supported",
        passed=not bad_claims,
        detail=f"validated 但无支持引用: {bad_claims}" if bad_claims else "validated 论断均有支持引用",
    ))

    hard_gate_passed = all(c.passed for c in checks)

    cov = coverage_of(plan)

    # Evidence quality（披露，不打分）
    first_party = sum(1 for o in observations if getattr(o.pit_grade, "value", o.pit_grade) == "A")
    evidence_quality = {
        "observations": len(observations),
        "first_party_observations": first_party,
        "validated_claims": sum(1 for c in claims if c.get("status") == "validated"),
        "draft_claims": sum(1 for c in claims if c.get("status") == "draft"),
        "open_conflicts": open_conflicts,
        "stale_fields": list(stale_fields or []),
    }

    # Analytical depth（确定性结构检查）
    counter_claims = [c for c in claims if c.get("counter_refs")]
    hypothesis_claims = [c for c in claims if c.get("kind") in ("hypothesis", "inference")]
    answered_with_refs = [
        q for q in plan.questions
        if q.status == "answered" and (q.support_refs or q.conclusion)
    ]
    analytical_depth = {
        "counter_evidence_claims": len(counter_claims),
        "hypothesis_or_inference_claims": len(hypothesis_claims),
        "answered_questions_with_support": len(answered_with_refs),
        "questions_with_computable_checks": sum(
            1 for q in plan.questions if q.computable_checks
        ),
        "has_counter_evidence": bool(counter_claims) or any(
            q.counter_refs for q in plan.questions
        ),
    }

    # Model reproducibility
    calc_with_refs = sum(
        1 for c in calculations
        if all(
            r.get("ref_id")
            for r in c.get("input_refs", [])
            if r.get("kind") in ("observation", "calculation")
        )
    )
    failed_calcs = [c for c in calculations if c.get("status") == "failed"]
    model_reproducibility = {
        "calculations": len(calculations),
        "calculations_with_input_refs": calc_with_refs,
        "failed_calculations": len(failed_calcs),
    }

    # verdict（§7.6：覆盖不足但有有效成果 → partial，不叫「充分完成」）
    gaps: list[str] = []
    notes: list[str] = []
    target = plan.budgets.question_coverage_target
    if not hard_gate_passed:
        verdict = "blocked"
        notes.append("integrity 硬门禁未过——产物不能 validated（rubric 满分不能覆盖）")
        gaps.extend(c.detail for c in checks if not c.passed)
    elif cov.violations:
        gaps.extend(cov.violations)
        # 覆盖不足但有有效成果 → partial；一无所获 → blocked（不叫「充分完成」也不假装有部分成果）
        if cov.answered or claims or observations or calculations:
            verdict = "partial"
        else:
            verdict = "blocked"
            gaps.append("无任何已回答问题/论断/观测")
    elif cov.applicable and cov.coverage >= target:
        verdict = "sufficient"
    elif cov.answered or claims or observations:
        verdict = "partial"
        gaps.append(
            f"适用问题覆盖 {cov.coverage:.0%} < 目标 {target:.0%}"
            f"（关键问题覆盖 {cov.key_coverage:.0%}）"
        )
    else:
        verdict = "blocked"
        gaps.append("无任何已回答问题/论断/观测")
    if open_conflicts:
        notes.append(f"{open_conflicts} 项开放冲突未裁决——不输出无条件结论")
        if verdict == "sufficient":
            verdict = "partial"
    if stop_reason in ("budget", "stalled", "cancelled"):
        notes.append(f"stop_reason={stop_reason} 属于预算/执行边界，不是公司基本面结论")

    return ResearchAssessment(
        plan_id=plan.plan_id,
        entity=f"{plan.entity_kind}:{plan.entity_id}",
        created_at=now or datetime.now(UTC),
        integrity_checks=checks,
        question_coverage=cov,
        evidence_quality=evidence_quality,
        analytical_depth=analytical_depth,
        model_reproducibility=model_reproducibility,
        gaps=gaps,
        stop_reason=stop_reason,
        verdict=verdict,
        hard_gate_passed=hard_gate_passed,
        notes=notes,
    )
