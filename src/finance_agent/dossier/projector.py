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
from . import registry as module_registry
from .models import (
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
    SnapshotInputs,
)

#: 行业实体的模块标题覆写（§7.5/review #24）——已迁至 registry（audit §3.6），
#: 保留常量只为旧测试/旧调用兼容
_INDUSTRY_MODULE_TITLES = {
    m.module_id: m.title for m in module_registry.INDUSTRY_MODULES
}


def _structures_from(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    """从冻结产物取结构化产物（audit §3.7）：最新 report 产物优先。

    只读已冻结的 artifact.structures——不在投影时调 LLM 临时生成图。
    """
    out: dict[str, Any] = {}
    for art in artifacts:  # artifacts_as_of 已按 created_at 倒序
        if art.get("purpose", "report") != "report":
            continue
        structures = art.get("structures") or {}
        for kind, payload in structures.items():
            out.setdefault(kind, payload)
    return out


def _mask_plan_question_status(plan: dict) -> dict:
    """历史投影下计划被后续更新过：问题范围保留，状态/结论置为不可分辨
    （review #10：不用今日状态冒充当时进展，也不伪造当时状态）。"""
    masked = dict(plan)
    masked["questions"] = [
        {**q, "status": "historical_unknown", "conclusion": None,
         "support_refs": [], "counter_refs": [], "unresolved": [], "attempts": []}
        for q in plan.get("questions", [])
    ]
    masked["status_reliable"] = False
    return masked


def _plan_digest(plan: dict | None) -> str:
    """计划指纹（进 data_hash，review #12）：范围 + 逐问题状态/结论。"""
    if not plan:
        return ""
    canon = json.dumps(
        {
            "plan_id": plan.get("plan_id"),
            "status": plan.get("status"),
            "questions": sorted(
                (q.get("question_id"), q.get("status"), q.get("conclusion"))
                for q in plan.get("questions", [])
            ),
        },
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(canon.encode()).hexdigest()[:16]

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
        # 历史纪律（review #10）：只用 as_of 前已创建的计划；问题状态被后续更新过则置不可分辨
        plans = self._metrics.plans_for(entity_kind, entity_id, namespace=ns, limit=1, as_of=t)
        plan = plans[0] if plans else None
        plan_status_reliable = True
        if plan is not None:
            updated = plan.get("updated_at")
            if updated and updated > t.isoformat():
                plan = _mask_plan_question_status(plan)
                plan_status_reliable = False
        resolutions = self._metrics.resolutions_as_of(entity_kind, entity_id, t, namespace=ns)
        # 冲突集合按截止时点过滤（review #3）：未来才可知的竞争值不进历史快照
        open_conflict_sems = set(self._metrics.conflicted_semantic_hashes(
            entity_kind, entity_id, namespace=ns, as_of=t, exclude_resolved=True,
        ))
        calculation_ids = self._metrics.list_calculation_ids(entity_kind, entity_id, t, namespace=ns)
        recipe = self._recipe(context.recipe_id)
        #: 结构化产物（来自冻结产物，确定性投影）+ 模块注册表（前后端同源）
        structures = _structures_from(artifacts)

        snapshot = DossierSnapshot(
            schema_version=SCHEMA_VERSION,
            entity=EntityRef(kind=entity_kind, id=entity_id, name=name or entity_id),  # type: ignore[arg-type]
            context=context,
            recipe={"id": recipe.id, "version": recipe.version},
            structures=structures,
            module_registry=module_registry.as_payload(entity_kind),
        )
        snapshot.summary = self._build_summary(
            facts, observations, claims, artifacts, recipe, t,
            entity_kind=entity_kind, entity_id=entity_id, plan=plan, structures=structures,
        )
        snapshot.modules = self._build_modules(
            entity_kind, entity_id, facts, observations, claims, artifacts,
            recipe, t, open_conflict_sems, ns, mode=context.mode, structures=structures,
            plan=plan,
        )
        snapshot.research = self._build_research_coverage(plan, claims, artifacts, t, ns)
        # 来源目录含 claim 引用（review #22）：仅由论断引用的证据不再被抽屉接口 404
        snapshot.evidence_refs = sorted(
            {eid for f in facts.values() for eid in f.evidence_ids}
            | {ref for o in observations for ref in o.evidence_refs}
            | {
                ref for c in claims
                for ref in [*c.get("support_refs", []), *c.get("counter_refs", [])]
                if str(ref).startswith("ev-")
            }
        )
        snapshot.document_refs = sorted({d for o in observations for d in o.document_refs})
        snapshot.decision_refs = self._decision_refs(entity_kind, entity_id, t, ns)
        snapshot.limitations = [
            "本档案是投影：结论的可信度以各模块状态与来源抽屉为准",
        ]
        # 冻结输入版本集（review #2）：模块/序列/来源请求按这些 id 读取，
        # 补录历史观测不改变已冻结快照；计划/问题状态进哈希（review #12）
        snapshot.inputs = SnapshotInputs(
            fact_ids={f: r.fact_id for f, r in sorted(facts.items())},
            observation_ids=sorted(o.observation_id for o in observations),
            claims=claims,
            artifacts=[
                {k: a.get(k) for k in ("artifact_id", "title", "status", "sufficiency",
                                       "created_at", "plan_id", "run_id", "purpose")}
                for a in artifacts
            ],
            resolution_ids=sorted(r.resolution_id for r in resolutions),
            calculation_ids=calculation_ids,
            conflicted_semantic_hashes=sorted(open_conflict_sems),
            plan=plan,
            plan_status_reliable=plan_status_reliable,
        )
        if context.mode == "rebuilt":
            snapshot.limitations.append(
                "基于历史证据重建：本页分析生成于今天，只使用 as_of 前可知的资料"
            )
        if context.mode == "historical":
            snapshot.limitations.append(
                f"历史视图（as_of {t.isoformat(timespec='seconds')}）：正文、图表、来源与质量同一截止时点"
            )
        # data_hash：冻结输入版本集（含计划问题状态与计算 id，review #12）
        inputs = {
            "facts": {f: (r.fact_id, r.version) for f, r in sorted(facts.items())},
            "observations": sorted(
                (o.observation_id, o.semantic_hash()) for o in observations
            ),
            "claims": sorted((c["claim_id"], c.get("status", "")) for c in claims),
            "artifacts": sorted(a["artifact_id"] for a in artifacts),
            "resolutions": sorted(r.resolution_id for r in resolutions),
            "calculations": sorted(calculation_ids),
            "plan": _plan_digest(plan),
            "structures": sorted(structures.keys()),
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
        *,
        entity_kind: str = "stock",
        entity_id: str = "",
        plan: dict[str, Any] | None = None,
        structures: dict[str, Any] | None = None,
    ) -> DossierSummary:
        summary = DossierSummary()
        structures = structures or {}
        exec_sum = structures.get("executive_summary") or {}
        candidates = structures.get("candidate_assessment") or {}
        # 研究结论（audit §3.8）：优先用回答用户目标的结构化摘要，而不是「最后创建的
        # 一条 validated claim」当总论
        if exec_sum.get("answer"):
            summary.thesis = str(exec_sum["answer"])
            summary.thesis_refs = list(exec_sum.get("refs") or [])
            summary.thesis_kind = "claim"
        validated = [
            c for c in claims
            if c.get("status") == "validated" and c.get("kind") in ("analysis", "fact_summary", "inference")
        ]
        validated.sort(key=lambda c: c.get("created_at", ""), reverse=True)
        # 用户情景 artifact 不参与默认结论投影（review #26：不自动成为发布版）
        report_artifacts = [a for a in artifacts if a.get("purpose", "report") == "report"]
        if summary.thesis is None:
            if validated:
                summary.thesis = validated[0]["statement"]
                summary.thesis_refs = [validated[0]["claim_id"], *validated[0].get("support_refs", [])]
                summary.thesis_kind = "claim"
            elif report_artifacts:
                latest = report_artifacts[0]
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
        # 目标与候选分层（audit §3.4/§3.8）：首屏回答「哪些公司、依据是什么」
        summary.objective = str((plan or {}).get("objective") or exec_sum.get("objective") or "")
        summary.tiers = {str(k): [str(x) for x in v] for k, v in (exec_sum.get("tiers") or {}).items()}
        if not summary.tiers and candidates.get("candidates"):
            for item in candidates["candidates"]:
                tier = str(item.get("tier") or "needs_review")
                label = str(item.get("name") or item.get("entity_id") or "")
                summary.tiers.setdefault(tier, []).append(label)
        summary.biggest_disagreement = str(exec_sum.get("biggest_disagreement") or "")
        # 最近变化（audit §3.8）：不再拿「最后三条 claim 各截 120 字」冒充 diff——
        # 改为本次研究的可分辨进展（问题结论/新验证论断），保留完整句
        changes: list[str] = []
        for q in (plan or {}).get("questions", []) or []:
            if q.get("status") in ("answered", "disputed", "unavailable") and q.get("conclusion"):
                changes.append(f"[{q.get('question_id')}] {q['conclusion']}")
        for c in validated[:3]:
            if c.get("statement") and c["statement"] not in changes:
                changes.append(c["statement"])
        summary.key_changes = changes[:5]
        # 关键驱动：目标维度/商业引擎类问题的结论（按注册表归位，不再只认三个股票问题 id）
        driver_modules = {"objective", "business_engine", "key_kpi", "candidate_pool",
                          "financial_quality", "revenue_segments"}
        for c in claims:
            qid = c.get("question_id") or ""
            q_module = next(
                (str(q.get("module") or "") for q in (plan or {}).get("questions", []) or []
                 if q.get("question_id") == qid), ""
            )
            if (q_module in driver_modules or qid.startswith("objective-")) \
                    and c.get("statement") and c["statement"] not in summary.drivers:
                summary.drivers.append(c["statement"])
        summary.drivers = summary.drivers[:5]
        if not summary.drivers and exec_sum.get("main_basis"):
            summary.drivers = [str(x) for x in exec_sum["main_basis"]][:5]
        # 最大反证：不硬截断（audit §3.8：反证在句中截断会失真）
        counter = [c for c in claims if c.get("counter_refs") or c.get("question_id") == "counter-evidence"
                   or str(c.get("question_id") or "").startswith("objective-counter_evidence")]
        if counter:
            counter.sort(key=lambda c: c.get("created_at", ""), reverse=True)
            summary.counter_evidence = counter[0]["statement"]
            summary.counter_refs = [counter[0]["claim_id"], *counter[0].get("counter_refs", [])]
        elif "counter_evidence" in facts:
            value = facts["counter_evidence"].value
            summary.counter_evidence = value if isinstance(value, str) else str(value)
        # 限制与未核验部分：结构产物 + 计划未解决项（全文保留，不截断）
        limits: list[str] = [str(x) for x in (exec_sum.get("limitations") or [])]
        limits += [str(x) for x in (candidates.get("limitations") or [])]
        for q in (plan or {}).get("questions", []) or []:
            for u in q.get("unresolved") or []:
                limits.append(f"[{q.get('question_id')}] {u}")
        for c in claims[:10]:
            limits += [str(x) for x in (c.get("limitations") or [])]
        summary.limitations = list(dict.fromkeys(limits))[:12]
        # 问题进展：无论是否已有 assessment 都显示（audit §3.8：0/9 不得隐藏）
        questions = (plan or {}).get("questions", []) or []
        applicable = [q for q in questions if q.get("priority") == "high"
                      and q.get("status") != "not_applicable"]
        answered = [q for q in applicable if q.get("status") == "answered"]
        if questions:
            summary.question_progress = (
                f"关键问题 {len(answered)}/{len(applicable)} 已回答"
                f"（全部问题 {sum(1 for q in questions if q.get('status') == 'answered')}"
                f"/{len(questions)}）"
            )
            if (plan or {}).get("status") == "active":
                summary.question_progress += "；研究进行中"
        # 可信度分层（audit §3.8）：沿用现有能力时只写「引用通过基础校验」，
        # 不把 validated 谎称为「事实已核对」
        validated_n = len(validated)
        bound_obs = sum(1 for o in observations if o.status == "ok" and (o.locator or o.raw))
        sufficiency = next(
            (str(a.get("sufficiency")) for a in report_artifacts if a.get("sufficiency")), ""
        )
        summary.credibility = {
            "refs_resolvable": f"{validated_n} 条论断引用通过基础校验（存在性+命名空间+实体上下文）",
            "facts_checked": (
                f"{bound_obs} 项 typed 观测带原文锚点/定位并过换算链重算"
                if bound_obs else "无带定位的 typed 观测（关键数字未核对）"
            ),
            "analysis_reviewed": (
                "尚无人工/二次复核：validated 仅表示引用可解析，不表示证据充分支持整句话"
            ),
            "sufficiency": sufficiency or "尚无充分度评估（未冻结研究产物）",
        }
        if exec_sum.get("credibility"):
            summary.credibility.update(
                {str(k): str(v) for k, v in exec_sum["credibility"].items()}
            )
        # 关键指标：行业模板 KPI → 最新 typed 观测（缺 = 缺口，不补零）
        summary.key_metrics = self._key_metrics(
            observations, recipe, t, entity_kind=entity_kind,
            entity_id=entity_id or str((plan or {}).get("entity_id") or ""),
        )
        if claims:
            summary.updated_at = max(
                datetime.fromisoformat(c["created_at"]) for c in claims if c.get("created_at")
            ) if any(c.get("created_at") for c in claims) else None
        return summary

    def _key_metrics(
        self, observations: list[MetricObservation], recipe: Recipe, t: datetime,
        *, entity_kind: str = "stock", entity_id: str = "",
    ) -> list[KeyMetric]:
        by_key: dict[str, list[MetricObservation]] = {}
        for o in observations:
            # KPI 卡只读本实体自己的指标（audit §3.2）：跨主体观测（行业里写公司数）
            # 属于候选矩阵，不得当作行业 KPI 投影
            if entity_id and o.subject_id != entity_id:
                continue
            by_key.setdefault(o.metric_key, []).append(o)
        #: 公司 KPI 名与行业 KPI 名不同（audit §3.6）：同一经济含义的别名归一
        aliases = _KPI_ALIASES.get(entity_kind, {})
        out: list[KeyMetric] = []
        for spec in recipe.kpis[:6]:  # 首屏约 5–6 个指标（§4.3）
            keys = [spec.key, *aliases.get(spec.key, [])]
            pool = [o for k in keys for o in by_key.get(k, []) if o.status == "ok"
                    and o.value is not None]
            plain = [o for o in pool if not o.dimensions]
            # 带维度的观测不再被直接过滤掉（audit §3.6）：无总量时用分部值并标注维度
            cands = plain or pool
            if not cands:
                out.append(KeyMetric(
                    metric_key=spec.key, label=spec.label, status="missing",
                    as_of_note="无 typed 观测（旧文本字段不进指标栏，不猜数）",
                ))
                continue
            # 期间最新（period_end 最大）；同期间取 knowledge_time 最新
            best = max(cands, key=lambda o: (o.period.end, o.knowledge_time))
            stale = self._is_stale(best.knowledge_time, t, recipe, spec.key)
            dim_note = "；".join(f"{k}={v}" for k, v in sorted(best.dimensions.items()))
            out.append(KeyMetric(
                metric_key=best.metric_key, label=spec.label, value=best.value,
                unit=best.unit, currency=best.currency,
                period_label=best.period.fiscal_label or best.period.end.isoformat(),
                nature=best.nature, observation_id=best.observation_id,
                evidence_refs=list(best.evidence_refs),  # review #21：点击指标直达自己的来源
                status="stale" if stale else ("conflicted" if best.status == "conflicted" else "ok"),
                as_of_note=(
                    f"{best.period.frequency} · 可知 {best.knowledge_time.date().isoformat()}"
                    + (f" · {dim_note}" if dim_note else "")
                ),
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
        structures: dict[str, Any] | None = None,
        plan: dict[str, Any] | None = None,
    ) -> dict[str, ModuleState]:
        structures = structures or {}
        #: 模块 → 未完成问题 id（audit §4：点击缺口可直接补研相应 question_id）
        open_questions_by_module = _open_questions_by_module(entity_kind, plan)
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
            # 注册表优先（audit §3.6：行业指标不得被配方口径塞进股票 financial 组），
            # 配方口径兜底；带 segment 维度的收入归分部模块
            mod = module_registry.module_of_metric(entity_kind, o.metric_key) \
                or _module_of_metric(o.metric_key, recipe)
            if o.dimensions.get("segment") and o.metric_key == "revenue":
                mod = "revenue_segments" if entity_kind == "stock" else "key_kpi"
            obs_by_module.setdefault(mod, []).append(o)
        legacy_by_module: dict[str, list[Any]] = {}
        for field, rec in facts.items():
            spec = next(
                (m for m in module_registry.modules_for(entity_kind)
                 if field in m.legacy_fields), None
            )
            mod = spec.module_id if spec else _LEGACY_FIELD_MODULE.get(field)
            if mod:
                legacy_by_module.setdefault(mod, []).append(rec)
        claims_by_module: dict[str, list[dict]] = {}
        for c in claims:
            mod = _claim_module(entity_kind, c, recipe)
            if mod:
                claims_by_module.setdefault(mod, []).append(c)
        conflict_obs = {o.semantic_hash() for o in observations} & open_conflict_sems

        modules: dict[str, ModuleState] = {}
        # 模块清单由注册表决定（audit §3.6）：行业不再遍历固定十个股票模块，
        # 不适用项标 not_applicable（不当作研究缺失，也不占默认导航）
        for spec in module_registry.modules_for(entity_kind):
            mod = spec.module_id
            if not module_registry._applicable(spec, entity_kind):  # noqa: SLF001
                modules[mod] = ModuleState(
                    status="not_applicable", title=spec.title,
                    reasons=[f"该模块不适用于 {entity_kind} 实体（信息架构由配方/注册表决定）"],
                )
                continue
            title = spec.title
            obs = obs_by_module.get(mod, [])
            legacy = legacy_by_module.get(mod, [])
            mod_claims = claims_by_module.get(mod, [])
            #: 本模块的结构产物（§3.7）：有结构 = 有可渲染交付，不只靠旧字段
            mod_structures = {
                kind: structures[kind] for kind in spec.structures if kind in structures
            }
            latest_kt = max(
                [o.knowledge_time for o in obs] + [r.knowledge_time for r in legacy],
                default=None,
            )
            # 模块内容指纹（data_ref）：changed_modules 据此发现「状态未变但数据已变」
            digest_src: dict[str, Any] = {
                "obs": sorted(o.observation_id for o in obs),
                "facts": sorted((r.fact_id, r.version) for r in legacy),
                "claims": sorted(c["claim_id"] for c in mod_claims),
                "structures": sorted(mod_structures.keys()),
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
                report_arts = [a for a in artifacts if a.get("purpose", "report") == "report"]
                status = "ready" if (report_arts or claims) else ("partial" if legacy else "missing")
                if not report_arts:
                    reasons.append("尚无冻结研究产物（旧档案内容在数据与审计区；用户情景不算研究产物）")
            elif mod == "investment_snapshot":
                report_arts = [a for a in artifacts if a.get("purpose", "report") == "report"]
                if any(c.get("status") == "validated" for c in claims) or report_arts:
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
                if mod_structures:
                    # 结构产物就绪（产业链图/候选矩阵/时间线）= 有可渲染交付。
                    # 关系图的证据是 evidence_refs 而不是 typed 观测，不额外要求数值。
                    if _structure_has_content(mod_structures):
                        status = "ready"
                    else:
                        status = "partial"
                        reasons.append("结构产物为空壳（无节点/候选/时间线项）——不作就绪宣称")
                elif obs:
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
                # 缺口可点击补研：只给未完成（非 answered/not_applicable）的问题 id
                gap_refs=list(open_questions_by_module.get(mod, [])),
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
        # 用户情景不是研究产物（review #26）：不进覆盖/首屏结论投影
        reports = [a for a in artifacts if a.get("purpose", "report") == "report"]
        cov = ResearchCoverage(
            artifact_refs=[a["artifact_id"] for a in reports],
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

        优先沿用 as_of 前最新研究计划的配方（历史快照不得采用未来计划的选择，
        review #10）；否则按档案文本提示匹配。
        """
        plans = self._metrics.plans_for(entity_kind, entity_id, namespace=namespace,
                                        limit=1, as_of=t)
        if plans:
            return plans[0].get("recipe_id", "general"), "沿用当时最新研究计划的配方选择"
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


#: 同一经济含义的 KPI 别名（audit §3.6：公司 KPI 因名字不同在行业页不可见）
_KPI_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "industry": {
        "market_size": ("tam", "market_size_total", "industry_revenue"),
        "growth_rate": ("market_growth", "industry_growth", "cagr"),
        "capacity_supply": ("capacity", "supply", "contracted_mw"),
    },
    "stock": {
        "revenue": ("total_revenue", "sales"),
        "gross_margin": ("gm", "gross_margin_pct"),
        "firm_backlog": ("backlog", "orders"),
    },
}


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


#: 行业实体：股票模块名 → 行业模块（claim/问题归位用，audit §3.6）
_STOCK_TO_INDUSTRY_MODULE = {
    "business_engine": "industry_chain",
    "revenue_segments": "industry_chain",
    "peers": "candidate_pool",
    "key_kpi": "key_kpi",
    "financial_quality": "key_kpi",
    "catalysts_risks": "catalysts_risks",
    "expectations": "key_kpi",
    "valuation_lab": "candidate_pool",
}

#: 目标维度（objective.py）→ 模块：按实体类型分开
_OBJECTIVE_DIMENSION_MODULE = {
    "industry": {
        "technology_moat": "candidate_pool",
        "commercial_proof": "key_kpi",
        "sustainability": "candidate_pool",
        "counter_evidence": "catalysts_risks",
        "investability": "candidate_pool",
        "commercial_breakout": "candidate_pool",
    },
    "stock": {
        "technology_moat": "business_engine",
        "commercial_proof": "financial_quality",
        "sustainability": "financial_quality",
        "counter_evidence": "catalysts_risks",
        "investability": "peers",
        "commercial_breakout": "business_engine",
    },
}


#: 结构产物的「有内容」判据：任一键非空即算就绪（空壳不宣称 ready）
_STRUCTURE_CONTENT_KEYS = ("nodes", "candidates", "items", "rows", "answer", "tiers")


def _structure_has_content(structures: dict[str, Any]) -> bool:
    for payload in structures.values():
        if not isinstance(payload, dict):
            continue
        for key in _STRUCTURE_CONTENT_KEYS:
            if payload.get(key):
                return True
    return False


def _open_questions_by_module(entity_kind: str, plan: dict[str, Any] | None) -> dict[str, list[str]]:
    """冻结计划里未完成的问题 → 所属模块（audit §4：缺口可点击直接补研）。

    归属口径与 claim 一致（`_claim_module`）：模块名/配方问题 id/目标编译题都能归位；
    归不到模块的问题不硬塞（宁可不显示，也不错放）。
    """
    out: dict[str, list[str]] = {}
    for q in (plan or {}).get("questions", []) or []:
        if q.get("status") in ("answered", "not_applicable"):
            continue
        qid = str(q.get("question_id") or "")
        if not qid:
            continue
        mod = _claim_module(entity_kind, {"question_id": str(q.get("module") or qid)}, None) \
            or _claim_module(entity_kind, {"question_id": qid}, None)
        if mod:
            out.setdefault(mod, []).append(qid)
    return out


def _claim_module(entity_kind: str, claim: dict[str, Any], recipe: Recipe) -> str | None:
    """claim → 档案模块（audit §3.6）：统一校验，不再让 claim 漂在模块外。

    三种形态都能归位：
    - question_id 是注册表里的模块名（本次事故：三条 claim 误用 key_kpi）；
    - question_id 是配方问题 id（value-chain/candidate-pool/...）；
    - question_id 是目标编译题（objective-<dimension>-<hash>）。
    """
    qid = str(claim.get("question_id") or "")
    if not qid:
        return None
    known = {m.module_id for m in module_registry.modules_for(entity_kind)}
    if qid in known:
        return qid
    if qid.startswith("objective-"):
        parts = qid.split("-")
        dim = parts[1] if len(parts) > 2 else ""
        table = _OBJECTIVE_DIMENSION_MODULE.get(entity_kind, _OBJECTIVE_DIMENSION_MODULE["stock"])
        return table.get(dim)
    mod = _module_of_question(qid)
    if mod is None:
        return None
    if entity_kind == "industry":
        return _STOCK_TO_INDUSTRY_MODULE.get(mod, mod)
    return mod


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
    natures: tuple[str, ...] | None = None,
) -> MetricSeriesSet:
    """typed 观测 → 图表序列（review #15：按完整语义键拆分）。

    分组键 = metric_key + frequency + dimensions + basis + currency + nature：
    分部/合并、GAAP/非 GAAP、披露/指引/预期各自成序列，不混合投影——
    前端每期一点，混合分组会静默丢点并把分部/指引画成公司实际值。
    缺期保留断点；重述/冲突在点上标记。"""
    labels = labels or {}
    conflicted = conflicted_sems or set()
    out: list[MetricSeries] = []
    for key in metric_keys:
        obs = [o for o in observations if o.metric_key == key]
        if frequency:
            obs = [o for o in obs if o.period.frequency == frequency]
        if natures:
            obs = [o for o in obs if o.nature in natures]
        if not obs:
            continue
        groups: dict[tuple, list[MetricObservation]] = {}
        for o in obs:
            gk = (
                o.period.frequency,
                tuple(sorted(o.dimensions.items())),
                o.basis,
                o.currency,
                o.nature,
            )
            groups.setdefault(gk, []).append(o)
        for (freq, dims, basis, currency, nature), group in sorted(
            groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]), kv[0][2])
        ):
            group.sort(key=lambda o: o.period.end)
            first = group[0]
            dim_label = "·".join(v for _k, v in dims) if dims else ""
            label_parts = [labels.get(key, key)]
            if dim_label:
                label_parts.append(dim_label)
            if basis not in ("GAAP", "IFRS"):
                label_parts.append(basis)
            if nature not in ("reported", "calculated"):
                label_parts.append({"guidance": "指引", "consensus": "一致预期",
                                    "model_estimate": "模型"}.get(nature, nature))
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
                for o in group
            ]
            out.append(MetricSeries(
                metric_key=key, label=" · ".join(label_parts), unit=first.unit,
                currency=currency, frequency=freq,
                dimensions=dict(dims), basis=basis, nature=nature,
                points=points, status="ready",
            ))
    return MetricSeriesSet(series=out)


def business_graph(
    facts: dict[str, Any], claims: list[dict[str, Any]], *, entity_kind: str = "stock",
    structures: dict[str, Any] | None = None,
) -> BusinessGraph:
    """商业引擎/产业链投影（review #24 + audit §3.7）。

    结构优先：有冻结的 IndustryMap 结构产物就填 nodes/edges/layers（带证据、
    可点击）；无流量数据时 flow_known=False，前端画等宽边而不是假 Sankey。
    旧文本字段（value_chain/competition/sub_sectors 或 business_model）只作叙述兼容。
    """
    from .models import BusinessGraphEdge, BusinessGraphNode

    graph = BusinessGraph()
    imap = (structures or {}).get("industry_map") or {}
    for node in imap.get("nodes") or []:
        graph.nodes.append(BusinessGraphNode(
            node_id=str(node.get("node_id") or ""),
            label=str(node.get("label") or node.get("node_id") or ""),
            kind="input" if str(node.get("layer")) == "upstream" else "other",
            note=str(node.get("note") or ""),
            layer=str(node.get("layer") or ""),
            company_refs=[str(x) for x in (node.get("company_refs") or [])],
            bottleneck=bool(node.get("bottleneck")),
            evidence_refs=[str(x) for x in (node.get("evidence_refs") or [])],
        ))
    for edge in imap.get("edges") or []:
        graph.edges.append(BusinessGraphEdge(
            source=str(edge.get("source") or ""),
            target=str(edge.get("target") or ""),
            label=str(edge.get("note") or edge.get("relation") or ""),
            relation=str(edge.get("relation") or "supplies"),
            flow_known=bool(edge.get("flow_known")),
            value_ref=(str(edge["flow_value"]) if edge.get("flow_value") else None),
            evidence_refs=[str(x) for x in (edge.get("evidence_refs") or [])],
        ))
    graph.layers = [str(x) for x in (imap.get("layers") or [])]
    graph.routes = [dict(x) for x in (imap.get("routes") or []) if isinstance(x, dict)]
    graph.bottlenecks = [str(x) for x in (imap.get("bottlenecks") or [])]

    narrative_fields = (
        ("value_chain", "competition", "sub_sectors")
        if entity_kind == "industry"
        else ("business_model", "future_space")
    )
    parts: list[str] = []
    refs: list[str] = []
    for field in narrative_fields:
        rec = facts.get(field)
        if rec is None:
            continue
        text = rec.value if isinstance(rec.value, str) else json.dumps(rec.value, ensure_ascii=False)
        parts.append(text if len(parts) == 0 else f"【{field}】{text}")
        refs.extend([rec.fact_id, *rec.evidence_ids])
    if parts:
        graph.narrative = "\n\n".join(parts)
        graph.narrative_refs = refs
    for c in claims:
        if (
            not graph.narrative
            and c.get("question_id") in ("business-model", "value-chain", "bottleneck", "clinical-evidence")
            and c.get("status") in ("validated", "draft")
        ):
            graph.narrative = c["statement"]
            graph.narrative_refs = [c["claim_id"]]
    return graph
