"""DossierProjector：在给定时间与快照上下文拼装读模型（设计 §5/§6.6/§10.1）。

职责边界：
- 只投影，不在请求时调用 LLM 改写结论；
- 所有模块共用同一 DossierContext（as_of/namespace/mode）——表格、图表、来源、
  质量与研究结论来自同一可见世界；
- 历史投影纪律：默认只显示 created_at ≤ T 且输入可知时间 ≤ T 的产物；
  「今天基于 T 前资料重建」的分析必须显式 mode=rebuilt 标注；
- 图表数值只来自 typed 观测/计算（NumericObservation 契约），旧文本字段
  只进 legacy 区（数据与审计），带 needs_normalization 标记，不猜数。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from ..knowledge.metric_store import MetricStore
from ..knowledge.metrics import MetricObservation
from ..knowledge.snapshot import kb_snapshot_id
from ..knowledge.store import BitemporalStore
from ..research.plan import Recipe, load_recipe, select_recipe
from .models import (
    MODULE_IDS,
    MODULE_TITLES,
    SCHEMA_VERSION,
    BusinessGraph,
    DossierContext,
    DossierSnapshot,
    DossierSummary,
    EntityRef,
    EvidenceItem,
    KeyMetric,
    LegacyFactItem,
    MetricPoint,
    MetricSeries,
    MetricSeriesSet,
    ModuleState,
    ResearchCoverage,
)

#: 数值锚点字段（旧 schema）：文本里含数字但无单位/期间 → needs_normalization
_LEGACY_NUMERIC_FIELDS = frozenset(
    {"revenue_fy", "net_income_fy", "cash_flow", "valuation", "market_size", "growth_rate", "market_share"}
)
_HAS_NUMBER = re.compile(r"\d")

#: recipe freshness 键 → 模块映射（新鲜度按模块定义，§6.4）
_FRESHNESS_TO_MODULE = {
    "financials": "financial_quality",
    "kpi": "key_kpi",
    "orders": "key_kpi",
    "backlog": "key_kpi",
    "capacity": "key_kpi",
    "pipeline": "key_kpi",
    "runway": "financial_quality",
    "risks": "catalysts_risks",
    "catalysts": "catalysts_risks",
    "management": "peers",
    "market_size": "key_kpi",
    "growth": "key_kpi",
    "policy": "catalysts_risks",
}

#: 旧字段 → 模块归属（legacy 投影 + 模块状态计算）
_LEGACY_FIELD_MODULE = {
    "business_model": "business_engine",
    "moat": "peers",
    "peers": "peers",
    "market_share": "peers",
    "revenue_fy": "financial_quality",
    "net_income_fy": "financial_quality",
    "cash_flow": "financial_quality",
    "valuation": "valuation_lab",
    "risks": "catalysts_risks",
    "catalysts": "catalysts_risks",
    "counter_evidence": "catalysts_risks",
    "management": "peers",
    "talent_density": "peers",
    "future_space": "business_engine",
    # 行业字段
    "market_size": "key_kpi",
    "growth_rate": "key_kpi",
    "value_chain": "business_engine",
    "competition": "peers",
    "policy": "catalysts_risks",
    "sub_sectors": "business_engine",
    "player_landscape": "peers",
}


class DossierProjector:
    def __init__(
        self,
        *,
        kb: BitemporalStore,
        metrics: MetricStore,
        decisions: Any | None = None,  # DecisionStore（可选：decision_refs）
        recipes_dir: str | None = None,
    ):
        self._kb = kb
        self._metrics = metrics
        self._decisions = decisions
        self._recipes_dir = recipes_dir

    # ---------------- 上下文与快照 ----------------

    def build_context(
        self,
        entity_kind: str,
        entity_id: str,
        *,
        as_of: datetime | None = None,
        namespace: str = "prod",
        mode: str = "live",
    ) -> DossierContext:
        """live 模式把「现在」固定为服务端时刻（§6.6）；historical 必须显式给 as_of。"""
        now = datetime.now(UTC)
        if mode == "historical":
            if as_of is None:
                raise ValueError("historical 模式必须显式给出 as_of")
            if as_of >= now:
                raise ValueError("historical 模式的 as_of 必须是过去时刻")
        t = as_of or now
        recipe_id, _basis = self._detect_recipe(entity_kind, entity_id, t, namespace)
        recipe = self._recipe(recipe_id)
        return DossierContext(
            mode=mode,  # type: ignore[arg-type]
            namespace=namespace,
            as_of=t,
            recipe_id=recipe.id,
            recipe_version=recipe.version,
        )

    def project(
        self,
        entity_kind: str,
        entity_id: str,
        context: DossierContext,
        *,
        name: str = "",
    ) -> DossierSnapshot:
        t = context.as_of
        ns = context.namespace
        facts = self._kb.view(entity_kind, entity_id, t, namespace=ns)
        observations = self._metrics.observations_as_of(entity_kind, entity_id, t, namespace=ns)
        claims = self._metrics.claims_as_of(entity_kind, entity_id, t, namespace=ns)
        artifacts = self._metrics.artifacts_as_of(entity_kind, entity_id, t, namespace=ns)
        plans = self._metrics.plans_for(entity_kind, entity_id, namespace=ns, limit=1)
        plan = plans[0] if plans else None
        resolutions = self._metrics.resolutions_as_of(entity_kind, entity_id, t, namespace=ns)
        conflicted_sems = set(self._metrics.conflicted_semantic_hashes(entity_kind, entity_id, namespace=ns))
        resolved_sems = {r.semantic_hash for r in resolutions}
        open_conflict_sems = conflicted_sems - resolved_sems
        recipe = self._recipe(context.recipe_id)

        snapshot = DossierSnapshot(
            schema_version=SCHEMA_VERSION,
            entity=EntityRef(kind=entity_kind, id=entity_id, name=name or entity_id),  # type: ignore[arg-type]
            context=context,
            recipe={"id": recipe.id, "version": recipe.version},
        )
        snapshot.summary = self._build_summary(
            facts, observations, claims, artifacts, recipe, t
        )
        snapshot.modules = self._build_modules(
            entity_kind, entity_id, facts, observations, claims, artifacts,
            recipe, t, open_conflict_sems, ns, mode=context.mode,
        )
        snapshot.research = self._build_research_coverage(plan, claims, artifacts, t, ns)
        snapshot.evidence_refs = sorted(
            {eid for f in facts.values() for eid in f.evidence_ids}
            | {ref for o in observations for ref in o.evidence_refs}
        )
        snapshot.document_refs = sorted({d for o in observations for d in o.document_refs})
        snapshot.decision_refs = self._decision_refs(entity_kind, entity_id, t, ns)
        snapshot.limitations = [
            "本档案是投影：结论的可信度以各模块状态与来源抽屉为准",
        ]
        if context.mode == "rebuilt":
            snapshot.limitations.append(
                "基于历史证据重建：本页分析生成于今天，只使用 as_of 前可知的资料"
            )
        if context.mode == "historical":
            snapshot.limitations.append(
                f"历史视图（as_of {t.isoformat(timespec='seconds')}）：正文、图表、来源与质量同一截止时点"
            )
        # data_hash：投影输入版本集（fact/observation/claim/artifact/resolution）
        inputs = {
            "facts": {f: (r.fact_id, r.version) for f, r in sorted(facts.items())},
            "observations": sorted(
                (o.observation_id, o.semantic_hash()) for o in observations
            ),
            "claims": sorted((c["claim_id"], c.get("status", "")) for c in claims),
            "artifacts": sorted(a["artifact_id"] for a in artifacts),
            "resolutions": sorted(r.resolution_id for r in resolutions),
        }
        snapshot.data_hash = snapshot.compute_data_hash(inputs)
        with contextlib.suppress(Exception):
            snapshot.context.kb_snapshot_id = kb_snapshot_id(
                self._kb, [(entity_kind, entity_id)], t, namespace=ns
            )
        return snapshot

    # ---------------- summary ----------------

    def _build_summary(
        self,
        facts: dict[str, Any],
        observations: list[MetricObservation],
        claims: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
        recipe: Recipe,
        t: datetime,
    ) -> DossierSummary:
        summary = DossierSummary()
        # 研究结论：validated claim（analysis/fact_summary 优先）> artifact 摘要 >
        # legacy thesis（标 legacy_analysis——旧 thesis 不是 reported fact，§2.2）
        validated = [
            c for c in claims
            if c.get("status") == "validated" and c.get("kind") in ("analysis", "fact_summary", "inference")
        ]
        validated.sort(key=lambda c: c.get("created_at", ""), reverse=True)
        if validated:
            summary.thesis = validated[0]["statement"]
            summary.thesis_refs = [validated[0]["claim_id"], *validated[0].get("support_refs", [])]
            summary.thesis_kind = "claim"
        elif artifacts:
            latest = artifacts[0]
            text = _artifact_headline(latest)
            if text:
                summary.thesis = text
                summary.thesis_refs = [latest["artifact_id"]]
                summary.thesis_kind = "draft" if latest.get("status") == "draft" else "claim"
        else:
            draft = [c for c in claims if c.get("status") == "draft"]
            thesis_fact = facts.get("thesis")
            if draft:
                draft.sort(key=lambda c: c.get("created_at", ""), reverse=True)
                summary.thesis = draft[0]["statement"]
                summary.thesis_refs = [draft[0]["claim_id"]]
                summary.thesis_kind = "draft"
            elif thesis_fact is not None and isinstance(thesis_fact.value, str):
                summary.thesis = thesis_fact.value.strip()[:300]
                summary.thesis_refs = [thesis_fact.fact_id]
                summary.thesis_kind = "legacy_analysis"
        # 最近变化：最新 3 条 validated/draft claim（按创建时间）
        recent = sorted(claims, key=lambda c: c.get("created_at", ""), reverse=True)[:3]
        summary.key_changes = [c["statement"][:120] for c in recent if c.get("statement")]
        # 关键驱动：business-model/revenue-engine 问题的结论（计划问题投影）
        for c in claims:
            if c.get("question_id") in ("revenue-engine", "business-model", "order-to-revenue"):
                summary.drivers.append(c["statement"][:120])
        # 最大反证：counter_refs 的 claim 或 counter-evidence 问题结论
        counter = [c for c in claims if c.get("counter_refs") or c.get("question_id") == "counter-evidence"]
        if counter:
            counter.sort(key=lambda c: c.get("created_at", ""), reverse=True)
            summary.counter_evidence = counter[0]["statement"][:200]
            summary.counter_refs = [counter[0]["claim_id"]]
        elif "counter_evidence" in facts:
            value = facts["counter_evidence"].value
            summary.counter_evidence = (value if isinstance(value, str) else str(value))[:200]
        # 关键指标：行业模板 KPI → 最新 typed 观测（缺 = 缺口，不补零）
        summary.key_metrics = self._key_metrics(observations, recipe, t)
        if claims:
            summary.updated_at = max(
                datetime.fromisoformat(c["created_at"]) for c in claims if c.get("created_at")
            ) if any(c.get("created_at") for c in claims) else None
        return summary

    def _key_metrics(
        self, observations: list[MetricObservation], recipe: Recipe, t: datetime
    ) -> list[KeyMetric]:
        by_key: dict[str, list[MetricObservation]] = {}
        for o in observations:
            by_key.setdefault(o.metric_key, []).append(o)
        out: list[KeyMetric] = []
        for spec in recipe.kpis[:6]:  # 首屏约 5–6 个指标（§4.3）
            cands = [
                o for o in by_key.get(spec.key, [])
                if o.status == "ok" and o.value is not None and not o.dimensions
            ]
            if not cands:
                out.append(KeyMetric(
                    metric_key=spec.key, label=spec.label, status="missing",
                    as_of_note="无 typed 观测（旧文本字段不进指标栏，不猜数）",
                ))
                continue
            # 期间最新（period_end 最大）；同期间取 knowledge_time 最新
            best = max(cands, key=lambda o: (o.period.end, o.knowledge_time))
            stale = self._is_stale(best.knowledge_time, t, recipe, spec.key)
            out.append(KeyMetric(
                metric_key=spec.key, label=spec.label, value=best.value,
                unit=best.unit, currency=best.currency,
                period_label=best.period.fiscal_label or best.period.end.isoformat(),
                nature=best.nature, observation_id=best.observation_id,
                status="stale" if stale else ("conflicted" if best.status == "conflicted" else "ok"),
                as_of_note=f"{best.period.frequency} · 可知 {best.knowledge_time.date().isoformat()}",
            ))
        return out

    # ---------------- 模块状态 ----------------

    def _build_modules(
        self,
        entity_kind: str,
        entity_id: str,
        facts: dict[str, Any],
        observations: list[MetricObservation],
        claims: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
        recipe: Recipe,
        t: datetime,
        open_conflict_sems: set[str],
        namespace: str,
        mode: str = "live",
    ) -> dict[str, ModuleState]:
        now = datetime.now(UTC)
        has_current_data = None
        if mode == "historical":
            # 仅历史模式：判断「现在有数据但 as_of 不可知」→ unavailable_at_as_of
            # （live 模式的 as_of 就是服务端当前时刻，不存在此状态）
            with contextlib.suppress(Exception):
                cur_facts = self._kb.view(entity_kind, entity_id, now, namespace=namespace)
                cur_obs = self._metrics.observations_as_of(entity_kind, entity_id, now, namespace=namespace)
                has_current_data = bool(cur_facts) or bool(cur_obs)

        obs_by_module: dict[str, list[MetricObservation]] = {}
        for o in observations:
            obs_by_module.setdefault(_module_of_metric(o.metric_key, recipe), []).append(o)
        legacy_by_module: dict[str, list[Any]] = {}
        for field, rec in facts.items():
            mod = _LEGACY_FIELD_MODULE.get(field)
            if mod:
                legacy_by_module.setdefault(mod, []).append(rec)
        claims_by_module: dict[str, list[dict]] = {}
        for c in claims:
            mod = _module_of_question(c.get("question_id"))
            if mod:
                claims_by_module.setdefault(mod, []).append(c)
        conflict_obs = {o.semantic_hash() for o in observations} & open_conflict_sems

        modules: dict[str, ModuleState] = {}
        for mod in MODULE_IDS:
            title = MODULE_TITLES.get(mod, mod)
            obs = obs_by_module.get(mod, [])
            legacy = legacy_by_module.get(mod, [])
            mod_claims = claims_by_module.get(mod, [])
            latest_kt = max(
                [o.knowledge_time for o in obs] + [r.knowledge_time for r in legacy],
                default=None,
            )
            # 模块内容指纹（data_ref）：changed_modules 据此发现「状态未变但数据已变」
            digest_src: dict[str, Any] = {
                "obs": sorted(o.observation_id for o in obs),
                "facts": sorted((r.fact_id, r.version) for r in legacy),
                "claims": sorted(c["claim_id"] for c in mod_claims),
            }
            if mod == "research_sources":
                digest_src["artifacts"] = sorted(a["artifact_id"] for a in artifacts)
            data_ref = "md-" + hashlib.sha256(
                json.dumps(digest_src, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:12]
            has_conflict = any(o.semantic_hash() in conflict_obs for o in obs) or any(
                r.conflict_flag for r in legacy
            )
            reasons: list[str] = []
            if mod == "research_sources":
                status = "ready" if (artifacts or claims) else ("partial" if legacy else "missing")
                if not artifacts:
                    reasons.append("尚无冻结研究产物（旧档案内容在数据与审计区）")
            elif mod == "investment_snapshot":
                if any(c.get("status") == "validated" for c in claims) or artifacts:
                    status = "ready"
                elif claims or "thesis" in facts:
                    status = "partial"
                    reasons.append("研究结论尚为草稿/legacy（未经本轮验证）")
                else:
                    status = "missing"
                    reasons.append("无已验证 claim——先补研")
            elif mod == "expectations":
                g = [o for o in observations if o.nature in ("guidance", "consensus")]
                if g and any(o.nature == "consensus" for o in g):
                    status = "ready"
                elif g:
                    status = "partial"
                    reasons.append("无 consensus 快照——只比较公司指引（不虚构一致预期）")
                else:
                    status = "missing"
                    reasons.append("无指引/一致预期观测（数据能力缺口，不以空图宣称完成）")
            elif mod == "valuation_lab":
                applic = recipe.models.get("reverse_dcf", {}).get("applicable_when", "")
                calcs_ok = bool(obs_by_module.get("valuation_lab"))
                if calcs_ok:
                    status = "ready"
                elif applic in ("disabled_for_pre_revenue",):
                    status = "not_applicable"
                    reasons.append("行业配方禁用通用 EV/FCFF 模型（未盈利/现金流不可建模）")
                elif "valuation" in facts:
                    status = "partial"
                    reasons.append("仅有旧估值字段（legacy）；typed 估值计算待补研")
                else:
                    status = "missing"
                    reasons.append("无估值输入（市场数据/财务观测）")
            else:
                if obs:
                    status = "ready"
                    if mod == "key_kpi" or mod == "financial_quality":
                        required = [k.key for k in recipe.kpis if k.required]
                        covered = {o.metric_key for o in obs}
                        missing = [k for k in required if k not in covered]
                        if missing:
                            status = "partial"
                            reasons.append(f"必需 KPI 缺口: {', '.join(missing)}")
                elif mod_claims:
                    status = "partial"
                    reasons.append("有研究论断但无 typed 观测（图表降级为结论+来源）")
                elif legacy:
                    status = "partial"
                    reasons.append("仅旧字段文本（needs_normalization，不猜数绘图）")
                else:
                    status = "missing"
                    reasons.append("as_of 时点无该模块数据")
            # 横切状态覆盖：冲突 > 陈旧 > 历史不可知
            if has_conflict and status in ("ready", "partial"):
                status = "conflicted"
                reasons.append("存在开放冲突（同语义键竞争值未裁决）")
            if status == "ready" and latest_kt and self._module_stale(mod, latest_kt, t, recipe):
                status = "stale"
                reasons.append("超过配方新鲜度目标（今天重抓旧数字不使其变新鲜）")
            if status == "missing" and mode == "historical" and has_current_data:
                status = "unavailable_at_as_of"
                reasons.append(f"as_of（{t.date().isoformat()}）时点该模块数据尚不可知；当前视图有数据")
            modules[mod] = ModuleState(
                status=status, title=title, reasons=reasons,
                last_knowledge_time=latest_kt,
                data_ref=data_ref,
            )
        return modules

    def _module_stale(self, module: str, latest: datetime, t: datetime, recipe: Recipe) -> bool:
        for key, days in recipe.freshness.items():
            stem = key.removesuffix("_days")
            if _FRESHNESS_TO_MODULE.get(stem) == module:
                return (t - latest).days > days
        return False

    def _is_stale(self, kt: datetime, t: datetime, recipe: Recipe, metric_key: str) -> bool:
        days = recipe.freshness.get("financials_days") or recipe.freshness.get("kpi_days")
        if days is None:
            return False
        return (t - kt).days > days

    # ---------------- research coverage ----------------

    def _build_research_coverage(
        self, plan: dict | None, claims: list[dict], artifacts: list[dict], t: datetime, ns: str
    ) -> ResearchCoverage:
        cov = ResearchCoverage(
            artifact_refs=[a["artifact_id"] for a in artifacts],
        )
        if plan:
            cov.plan_id = plan.get("plan_id")
            qs = plan.get("questions", [])
            applicable = [q for q in qs if q.get("status") != "not_applicable"]
            cov.required = len(applicable)
            cov.answered = sum(1 for q in applicable if q.get("status") == "answered")
        return cov

    def _decision_refs(self, entity_kind: str, entity_id: str, t: datetime, ns: str) -> list[str]:
        if self._decisions is None:
            return []
        with contextlib.suppress(Exception):
            cards = self._decisions.list(namespace=ns, entity_id=entity_id)
            return [
                c.card_id for c in cards
                if c.subject.kind == entity_kind and c.created_at <= t
            ]
        return []

    # ---------------- 配方识别 ----------------

    def _detect_recipe(
        self, entity_kind: str, entity_id: str, t: datetime, namespace: str
    ) -> tuple[str, str]:
        """行业识别给出来源与可更改选择；不确定用通用（§7.5）。

        优先沿用最新研究计划的配方（研究时的显式选择）；否则按档案文本提示匹配。
        """
        plans = self._metrics.plans_for(entity_kind, entity_id, namespace=namespace, limit=1)
        if plans:
            return plans[0].get("recipe_id", "general"), "沿用最新研究计划的配方选择"
        hints = ""
        with contextlib.suppress(Exception):
            facts = self._kb.view(entity_kind, entity_id, t, namespace=namespace)
            for field in ("business_model", "moat", "peers"):
                rec = facts.get(field)
                if rec is not None and isinstance(rec.value, str):
                    hints += " " + rec.value
        return select_recipe(
            entity_kind, hint_text=hints[:2000], recipes_dir=self._recipes_dir
        )

    def _recipe(self, recipe_id: str) -> Recipe:
        try:
            return load_recipe(recipe_id, recipes_dir=self._recipes_dir)
        except FileNotFoundError:
            return load_recipe("general", recipes_dir=self._recipes_dir)


# ---------------- 辅助 ----------------


def _module_of_metric(metric_key: str, recipe: Recipe) -> str:
    kpi_keys = {k.key for k in recipe.kpis}
    financial = {"revenue", "net_income", "cfo", "capex", "fcf", "gross_margin", "operating_margin",
                 "net_margin", "net_debt", "cash", "total_debt", "quarterly_burn", "r_and_d",
                 "cash_runway", "ebitda", "gross_profit", "operating_income"}
    valuation = {"market_cap", "ev", "enterprise_value", "pe", "ps", "ev_sales", "ev_ebitda",
                 "implied_growth", "fcf_yield"}
    expectations = {"guidance", "consensus", "eps_actual", "eps_guidance", "eps_consensus"}
    if metric_key in valuation:
        return "valuation_lab"
    if metric_key in expectations or metric_key.startswith(("guidance_", "consensus_")):
        return "expectations"
    if metric_key in financial:
        return "financial_quality"
    if metric_key in kpi_keys:
        return "key_kpi"
    return "key_kpi"


def _module_of_question(question_id: str | None) -> str | None:
    mapping = {
        "business-model": "business_engine",
        "revenue-engine": "revenue_segments",
        "order-to-revenue": "key_kpi",
        "capacity-utilization": "key_kpi",
        "financial-quality": "financial_quality",
        "cash-runway": "financial_quality",
        "demand-space": "business_engine",
        "demand-drivers": "business_engine",
        "moat-competition": "peers",
        "customer-concentration": "catalysts_risks",
        "management-delivery": "peers",
        "catalysts-risks": "catalysts_risks",
        "counter-evidence": "catalysts_risks",
        "expectations-gap": "expectations",
        "valuation-assumptions": "valuation_lab",
        "unit-economics": "key_kpi",
        "service-revenue": "revenue_segments",
        "segment-depth": "revenue_segments",
        "pipeline-status": "key_kpi",
        "clinical-evidence": "business_engine",
        "regulatory-path": "catalysts_risks",
        "partnership-economics": "business_engine",
        "value-chain": "business_engine",
        "bottleneck": "business_engine",
        "demand-supply": "key_kpi",
        "technology-routes": "business_engine",
        "policy": "catalysts_risks",
        "candidate-pool": "peers",
    }
    return mapping.get(question_id or "")


def _artifact_headline(artifact: dict[str, Any]) -> str | None:
    """artifact 报告文档的第一段（执行摘要首句）作为结论摘要。"""
    doc = artifact.get("report_document") or {}
    for block in doc.get("blocks", []):
        if block.get("type") == "paragraph" and block.get("text"):
            return block["text"][:200]
    return None


def legacy_fact_items(
    kb: BitemporalStore,
    facts: dict[str, Any],
) -> list[LegacyFactItem]:
    """旧字段 → 「数据与审计」区条目：数值语义不明的标 needs_normalization。"""
    from ..knowledge.verify import field_issues

    out: list[LegacyFactItem] = []
    for field, rec in sorted(facts.items()):
        evidences = []
        for eid in rec.evidence_ids:
            with contextlib.suppress(Exception):
                evidences.append(kb.get_evidence(eid))
        issues = field_issues(field, rec, evidences)
        needs_norm = False
        if field in _LEGACY_NUMERIC_FIELDS and isinstance(rec.value, str) and _HAS_NUMBER.search(rec.value):
            needs_norm = True  # 文本含数字但单位/期间不明——不猜数（§11.1）
        out.append(LegacyFactItem(
            field=field, fact_id=rec.fact_id, value=rec.value,
            event_time=rec.event_time.isoformat() if rec.event_time else None,
            knowledge_time=rec.knowledge_time.isoformat(),
            version=rec.version, conflict=rec.conflict_flag,
            issues=issues, evidence_ids=list(rec.evidence_ids),
            needs_normalization=needs_norm,
        ))
    return out


def evidence_items(
    kb: BitemporalStore,
    facts: dict[str, Any],
    observations: list[MetricObservation],
    claims: list[dict[str, Any]],
) -> list[EvidenceItem]:
    """来源目录（同一截止时点过滤后的引用集合 → 抽屉数据源）。"""
    used_by: dict[str, list[str]] = {}
    quotes: dict[str, EvidenceItem] = {}
    for field, rec in facts.items():
        for eid in rec.evidence_ids:
            used_by.setdefault(eid, []).append(f"fact:{field}")
    for o in observations:
        for eid in o.evidence_refs:
            used_by.setdefault(eid, []).append(f"metric:{o.metric_key}")
    for c in claims:
        for ref in [*c.get("support_refs", []), *c.get("counter_refs", [])]:
            if ref.startswith("ev-"):
                used_by.setdefault(ref, []).append(f"claim:{c.get('claim_id')}")
    for eid in sorted(used_by):
        with contextlib.suppress(Exception):
            ev = kb.get_evidence(eid)
            quotes[eid] = EvidenceItem(
                evidence_id=ev.evidence_id,
                source_id=ev.source_id,
                provider_id=ev.source_id,  # 兼容映射：旧 source_id = provider 层（§6.1）
                url=ev.url,
                verbatim_quote=ev.verbatim_quote,
                available_at=ev.available_at.isoformat() if ev.available_at else None,
                retrieved_at=ev.retrieved_at.isoformat(),
                pit_grade=ev.pit_grade.value,
                used_by=used_by[eid],
            )
    return list(quotes.values())


def series_set(
    observations: list[MetricObservation],
    metric_keys: list[str],
    labels: dict[str, str] | None = None,
    *,
    frequency: str | None = None,
    conflicted_sems: set[str] | None = None,
) -> MetricSeriesSet:
    """typed 观测 → 图表序列（缺期保留断点；重述/冲突在点上标记）。"""
    labels = labels or {}
    conflicted = conflicted_sems or set()
    out: list[MetricSeries] = []
    for key in metric_keys:
        obs = [o for o in observations if o.metric_key == key]
        if frequency:
            obs = [o for o in obs if o.period.frequency == frequency]
        if not obs:
            continue
        obs.sort(key=lambda o: o.period.end)
        first = obs[0]
        points = [
            MetricPoint(
                period_label=o.period.fiscal_label or o.period.end.isoformat(),
                period_end=o.period.end.isoformat(),
                period_start=o.period.start.isoformat() if o.period.start else None,
                value=o.value, nature=o.nature, basis=o.basis, unit=o.unit,
                currency=o.currency, observation_id=o.observation_id,
                status=o.status, knowledge_time=o.knowledge_time.isoformat(),
                conflict=o.semantic_hash() in conflicted,
            )
            for o in obs
        ]
        out.append(MetricSeries(
            metric_key=key, label=labels.get(key, key), unit=first.unit,
            currency=first.currency, frequency=first.period.frequency,
            points=points, status="ready",
        ))
    return MetricSeriesSet(series=out)


def business_graph(
    facts: dict[str, Any], claims: list[dict[str, Any]]
) -> BusinessGraph:
    """商业引擎投影：legacy business_model 文本作 narrative；图结构待 typed 数据。

    无流量数据用流程图（narrative），不能编造 Sankey 宽度（§4.4）。
    """
    graph = BusinessGraph()
    bm = facts.get("business_model")
    if bm is not None:
        graph.narrative = bm.value if isinstance(bm.value, str) else str(bm.value)
        graph.narrative_refs = [bm.fact_id, *bm.evidence_ids]
    for c in claims:
        if (
            not graph.narrative
            and c.get("question_id") == "business-model"
            and c.get("status") in ("validated", "draft")
        ):
            graph.narrative = c["statement"]
            graph.narrative_refs = [c["claim_id"]]
    return graph
