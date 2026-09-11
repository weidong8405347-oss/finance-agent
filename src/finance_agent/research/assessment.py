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
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from .plan import ResearchPlan

logger = logging.getLogger("finance_agent.research.assessment")


# ---------------- 来源角色（与 PIT 等级分离，tools-plugins 方案 §2/§4.1） ----------------
# PIT 等级回答「时间可追溯性」，来源角色回答「一手/二手/供应商」——两者不得混用。
# 旧缺陷：first_party_observations 实际统计的是 PIT A。本表是角色归类的唯一真相源；
# 插件化（P1-C）后随 manifest 的 source_type 迁移，未知 source 一律 unknown（不偺一手）。
SOURCE_ROLES: dict[str, str] = {
    "edgar": "issuer_filing",        # 发行人向 SEC 提交的披露原文
    "edgar_facts": "issuer_filing",  # 同上（XBRL 结构化通道）
    "hkex_news": "issuer_filing",    # 港交所披露易发行人公告
    "web_search": "media_secondary",       # Exa：媒体/转载为主
    "web_search_tavily": "media_secondary",
    "news_gdelt": "media_secondary",
    "fundamentals": "vendor_snapshot",     # yfinance 供应商快照
    "fundamentals_hk": "vendor_snapshot",  # akshare/东财快照
    "prices": "market_data",
    "prices_stooq": "market_data",
    "canary_news": "eval_decoy",     # 评估诱饵源，不计入任何真实来源档
}

#: 算作「一手」的角色：发行人/监管披露（权威原文）
_FIRST_PARTY_ROLES = frozenset({"issuer_filing", "regulator"})

#: 发行人/监管披露域（基线发现 F7）：直接 URL 抓取（web_fetch，无源级角色）但
#: 真实主机名属于官方披露库的原文，按主机名归入 issuer_filing——NVDA 新闻稿从
#: sec.gov 直拉却被归 secondary 的事故形态。匹配走 urlparse hostname（review R12），
#: 只细化 unknown 档，媒体源不因转载 URL 升档（转载族归并属 SearchBroker，P1-A 后续）。
ISSUER_DISCLOSURE_DOMAINS = (
    "sec.gov", "hkexnews.hk", "cninfo.com.cn", "sse.com.cn", "szse.cn",
)


def _url_hostname(url: str) -> str:
    """URL 的真实主机名（小写、去尾点；scheme-less 也能解析）；解析失败 → ""。"""
    text = url.strip().lower()
    if not text:
        return ""
    if "://" not in text:
        text = "//" + text  # 让 urlparse 把首段当 netloc
    try:
        return (urlparse(text).hostname or "").rstrip(".")
    except ValueError:
        return ""


def is_issuer_disclosure_url(url: str) -> bool:
    """主机名等于官方披露域或其合法子域（review R12：按真实 hostname 匹配——
    查询参数/路径/userinfo 里出现 'sec.gov' 的媒体网页不得归为发行人披露）。"""
    host = _url_hostname(url)
    return any(host == d or host.endswith("." + d) for d in ISSUER_DISCLOSURE_DOMAINS)


def source_role(source_id: str, url: str | None = None) -> str:
    """source_id（+可选 URL 域名）→ 来源角色；未知源 = unknown（诚实缺省）。"""
    role = SOURCE_ROLES.get(source_id, "unknown")
    if role == "unknown" and url and is_issuer_disclosure_url(url):
        return "issuer_filing"
    return role


def classify_observation_source(
    obs: Any, evidence_sources: dict[str, str] | None,
    evidence_urls: dict[str, str] | None = None,
) -> str:
    """一条观测的来源档：first_party / secondary / vendor / derived / internal /
    market_data / unknown（方案 §2「时间可追溯与一手/权威来源被混用」的拆分）。

    - guidance：发行人自己的指引 → first_party（发布者义务已在 schema 层强制）；
    - consensus：供应商快照 → vendor；model_estimate → internal；calculated → derived；
    - reported：看所绑证据的来源角色——全部一手才算一手，混入媒体即 secondary，
      证据源不可解析 → unknown（不能因为拿不到来源就默认一手）。
    """
    nature = getattr(obs, "nature", "reported")
    if nature == "guidance":
        return "first_party"
    if nature == "consensus":
        return "vendor"
    if nature == "model_estimate":
        return "internal"
    if nature == "calculated":
        return "derived"
    refs = list(getattr(obs, "evidence_refs", []) or [])
    if not refs or evidence_sources is None:
        return "unknown"
    roles = {
        source_role(evidence_sources[r], (evidence_urls or {}).get(r))
        for r in refs if r in evidence_sources
    }
    if not roles:
        return "unknown"
    if roles <= _FIRST_PARTY_ROLES:
        return "first_party"
    if "market_data" in roles and roles <= {"market_data", *_FIRST_PARTY_ROLES}:
        return "market_data"
    if roles <= {"vendor_snapshot"}:
        return "vendor"
    return "secondary"


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
    namespace: str = "prod"  # 评估查询的上下文过滤键（review #11：eval 评估不得泄漏进生产档案）
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    integrity_checks: list[IntegrityCheck] = Field(default_factory=list)
    question_coverage: CoverageStats = Field(default_factory=CoverageStats)
    evidence_quality: dict[str, Any] = Field(default_factory=dict)
    analytical_depth: dict[str, Any] = Field(default_factory=dict)
    model_reproducibility: dict[str, Any] = Field(default_factory=dict)
    #: 量表一致性扫描（基线发现 F10）：同语义组 ~1000/10^6 倍离群对与
    #: 同值不同维度（语义键漂移候选）；披露不拦门（写侧硬闸在 metric_writer 2c）
    numeric_consistency: dict[str, Any] = Field(default_factory=dict)
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


def numeric_consistency_scan(observations: list[Any]) -> dict[str, Any]:
    """量表离群与同值异键扫描（基线发现 F10，确定性规则，披露不拦门）。

    同语义组 = 同 metric_key + 期间末 + 频率 + 币种 + 口径 + 性质（维度任意）：
    - scale_suspect_pairs：值比 ≈ 10^3 / 10^6 —— 千元原样 vs 归一至元、
      million 漏乘类事故的存库信号（dims 漂移使语义键分开、冲突闸拦不住）；
    - same_value_different_dims：同值不同维度 —— 语义键漂移的重复登记候选。
    """
    from collections import defaultdict
    from decimal import Decimal, InvalidOperation

    groups: dict[tuple, list[Any]] = defaultdict(list)
    for o in observations:
        value = getattr(o, "value", None)
        if value is None:
            continue
        period = getattr(o, "period", None)
        groups[(
            getattr(o, "metric_key", ""),
            period.end.isoformat() if period is not None else "",
            period.frequency if period is not None else "",
            getattr(o, "currency", None) or "",
            getattr(o, "basis", ""),
            getattr(o, "nature", ""),
        )].append(o)

    def _brief(o: Any) -> dict[str, Any]:
        return {
            "observation_id": getattr(o, "observation_id", ""),
            "value": getattr(o, "value", None),
            "unit": getattr(o, "unit", ""),
            "dimensions": dict(getattr(o, "dimensions", {}) or {}),
            "unit_text": (getattr(o, "raw", None) or None) and o.raw.unit_text or "",
        }

    scale_suspects: list[dict[str, Any]] = []
    same_value: list[dict[str, Any]] = []
    for (metric_key, period_end, *_rest), items in groups.items():
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                try:
                    va, vb = abs(Decimal(str(a.value))), abs(Decimal(str(b.value)))
                except InvalidOperation:
                    continue
                if va == 0 or vb == 0:
                    continue
                ratio = float(va / vb) if va >= vb else float(vb / va)
                pair = {"metric_key": metric_key, "period_end": period_end,
                        "ratio": round(ratio, 3), "a": _brief(a), "b": _brief(b)}
                if 10 ** 2.7 <= ratio <= 10 ** 3.3 or 10 ** 5.7 <= ratio <= 10 ** 6.3:
                    scale_suspects.append(pair)
                elif va == vb and dict(a.dimensions or {}) != dict(b.dimensions or {}):
                    same_value.append(pair)
    return {
        "scale_suspect_pairs": scale_suspects[:10],
        "scale_suspect_total": len(scale_suspects),
        "same_value_different_dims": same_value[:10],
    }


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
    namespace: str = "prod",
    now: datetime | None = None,
    evidence_sources: dict[str, str] | None = None,  # evidence_id → source_id（来源角色归类用）
    evidence_urls: dict[str, str] | None = None,  # evidence_id → url（披露域细化，F7）
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

    # Evidence quality（披露，不打分）：PIT 与来源角色分开统计（方案 §2 P0）——
    # pit_a 回答「时间可追溯」，first_party 回答「是否发行人/监管一手披露」。
    pit_a = sum(1 for o in observations if getattr(o.pit_grade, "value", o.pit_grade) == "A")
    source_buckets: dict[str, int] = {}
    for o in observations:
        bucket = classify_observation_source(o, evidence_sources, evidence_urls)
        source_buckets[bucket] = source_buckets.get(bucket, 0) + 1
    # validated 论断的内容级核验状态（方案 §5.4）：引用校验过 ≠ 原文支持结论，
    # 未核验的数量必须可见，不得对外呈现为「事实已核验」。
    validated_claims = [c for c in claims if c.get("status") == "validated"]
    content_unchecked = sum(
        1 for c in validated_claims
        if (c.get("verification") or {}).get("evidence_support", "unchecked") == "unchecked"
    )
    # 内容级核验分布（P2-A）：unchecked/supported/partially_supported/contradicted/
    # insufficient 逐档计数——核验覆盖率可见，不得用单一 validated 计数冒充
    support_breakdown: dict[str, int] = {}
    for c in claims:
        state = str((c.get("verification") or {}).get("evidence_support", "unchecked"))
        support_breakdown[state] = support_breakdown.get(state, 0) + 1
    evidence_quality = {
        "observations": len(observations),
        "pit_a_observations": pit_a,
        "first_party_observations": source_buckets.get("first_party", 0),
        "secondary_observations": source_buckets.get("secondary", 0),
        "vendor_observations": source_buckets.get("vendor", 0),
        "derived_observations": source_buckets.get("derived", 0),
        "market_data_observations": source_buckets.get("market_data", 0),
        "unknown_source_observations": source_buckets.get("unknown", 0)
        + source_buckets.get("internal", 0) + source_buckets.get("eval_decoy", 0),
        "validated_claims": len(validated_claims),
        "validated_claims_content_unchecked": content_unchecked,
        "claims_by_evidence_support": support_breakdown,
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
    numeric_consistency = numeric_consistency_scan(list(observations))
    if numeric_consistency["scale_suspect_pairs"]:
        notes.append(
            f"{numeric_consistency['scale_suspect_total']} 对观测存在 ~10^3/10^6 倍量表离群"
            "（同指标同期间）——量表归一/维度漂移待人工复核，不得直接取均值或混用"
        )
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
    if validated_claims and content_unchecked:
        notes.append(
            f"{content_unchecked}/{len(validated_claims)} 条 validated 论断仅过引用校验，"
            "内容级核验（原文是否支持结论）尚未执行"
        )
    if stop_reason in ("budget", "stalled", "cancelled"):
        notes.append(f"stop_reason={stop_reason} 属于预算/执行边界，不是公司基本面结论")

    return ResearchAssessment(
        plan_id=plan.plan_id,
        entity=f"{plan.entity_kind}:{plan.entity_id}",
        namespace=namespace,
        created_at=now or datetime.now(UTC),
        integrity_checks=checks,
        question_coverage=cov,
        evidence_quality=evidence_quality,
        analytical_depth=analytical_depth,
        model_reproducibility=model_reproducibility,
        numeric_consistency=numeric_consistency,
        gaps=gaps,
        stop_reason=stop_reason,
        verdict=verdict,
        hard_gate_passed=hard_gate_passed,
        notes=notes,
    )
