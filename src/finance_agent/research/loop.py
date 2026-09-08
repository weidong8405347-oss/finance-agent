"""ResearchLoop：轮次制迭代研究（S1，DESIGN.md §5.1）。

一轮 = gap 分析 → LLM turn（工具：登记证据/写事实/查档案/查数据）
     → 过程评估（coverage 软反馈 + writer 硬门禁）→ IterationReport。
收敛：完整度达标；停滞：一轮无成功写入；预算：max_rounds。
取消：should_stop 在轮次边界被检查（stop_command 的落点），已落库事实保留。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime

from ..eventstore.events import (
    CONTEXT_INJECT,
    RESEARCH_ASSESSMENT,
    RESEARCH_PLAN_CREATED,
    RESEARCH_ROUND_END,
    RESEARCH_ROUND_START,
    RESEARCH_RUBRIC,
    RESEARCH_STALL_DIAGNOSTIC,
    RUN_CREATED,
    Event,
)
from ..eventstore.store import EventStore
from ..gateway.gateway import DataGateway
from ..gateway.tools import make_gateway_tool
from ..harness.manifest import RunManifest
from ..knowledge.gaps import GapAnalyzer
from ..knowledge.store import BitemporalStore
from ..knowledge.writer import ProfileWriter
from ..llm.base import LLM
from ..loop.hooks import Hook
from ..loop.kernel import AgentKernel
from .playbooks import load_playbook
from .prompts import GROUNDING_CONTRACT, build_round_brief
from .report import IterationReport
from .tools import make_research_tools


def _plan_coverage(plan_payload: dict | None) -> tuple[float | None, list[str]]:
    """冻结计划的问题覆盖（§7.6）：返回 (coverage, violations)；无计划 = (None, [])。"""
    if not plan_payload:
        return None, []
    from .assessment import coverage_of
    from .plan import ResearchPlan

    plan = ResearchPlan.model_validate(plan_payload)
    cov = coverage_of(plan)
    return cov.coverage, cov.violations

#: 维度组定义（P3 §4.2）：字段 → 专职 researcher 组。按实体类型分表——
#: 行业字段与股票字段不同集（2026-09-01 实测：行业字段全落 misc 单组，丢失并行性）
_DIMENSION_GROUPS_STOCK: dict[str, tuple[str, ...]] = {
    "financial": ("revenue_fy", "net_income_fy", "cash_flow", "valuation"),
    "business": ("business_model", "moat"),
    "industry": ("peers", "market_share", "future_space"),
    # talent_density 归 risk_mgmt 组（与 management 同根：都是「人」的维度，
    # 2026-09-02 前未登记 → 轮转进随机组，挖不到专业指引 → 维度全空）
    "risk_mgmt": ("risks", "management", "talent_density", "catalysts", "counter_evidence"),
}
_DIMENSION_GROUPS_INDUSTRY: dict[str, tuple[str, ...]] = {
    "market": ("market_size", "growth_rate", "future_space"),
    "landscape": ("value_chain", "competition", "sub_sectors"),
    # player_landscape 不在 F1：它是 F2 标的池挖掘的专属产出（专用工具校验+并集纪律）
    "policy_players": ("policy",),
}


def _dimension_groups(
    missing: list[str], stale: list[str], optional_missing: list[str],
    *, entity_kind: str = "stock", weak: list[str] | None = None,
) -> list[tuple[str, list[str]]]:
    """缺口字段（缺失+陈旧+可选）按维度分组；未登记进组的字段轮转分配。
    注：player_landscape 永不分组（F2 专属产出，见 _DIMENSION_GROUPS_INDUSTRY 注释）。
    弱字段回流（2026-09-03 整改收尾）：weak 只挂进「因缺口已激活」的本维度组，
    让专职组顺带重写；不新建组、不轮转（weak 是引导不是缺口，不许扩大并行面）。
    无对应激活组 → 本轮不回流（serial 路径的 brief 全量投影仍可见）。"""
    table = _DIMENSION_GROUPS_INDUSTRY if entity_kind == "industry" else _DIMENSION_GROUPS_STOCK
    pending = [f for f in dict.fromkeys([*missing, *stale, *optional_missing])
               if f != "player_landscape"]
    groups: list[list] = []
    assigned: set[str] = set()
    for gname, gfields in table.items():
        hit = [f for f in pending if f in gfields]
        if hit:
            groups.append([gname, hit])
            assigned.update(hit)
    rest = [f for f in pending if f not in assigned]
    for i, f in enumerate(rest):
        if groups:
            groups[i % len(groups)][1].append(f)
        else:
            groups.append(["misc", [f]])
    for f in weak or []:
        if f in assigned or f == "player_landscape":
            continue
        for g in groups:
            if f in table.get(g[0], ()):
                g[1].append(f)
                assigned.add(f)
                break
    return [(g, fs) for g, fs in groups]

#: 计划问题 module → 维度组映射（review #7）：配方用模块名（financial_quality/
#: business_engine/risks…），维度组用旧分组名（financial/business/industry/risk_mgmt），
#: 不映射则四组全部拿到空计划视图，并行 worker 丢失问题与验收条件
_QUESTION_MODULE_TO_GROUP: dict[str, str] = {
    # 股票维度组
    "financial_quality": "financial",
    "financials": "financial",
    "valuation": "financial",
    "valuation_lab": "financial",
    "expectations": "financial",
    "key_kpi": "financial",
    "business_engine": "business",
    "revenue_segments": "business",
    "peers": "industry",
    "management": "industry",
    "risks": "risk_mgmt",
    "catalysts": "risk_mgmt",
    "catalysts_risks": "risk_mgmt",
    # 行业维度组（F1）
    "industry_chain": "landscape",
    "candidate_pool": "landscape",
    "key_kpi_industry": "market",
    "market": "market",
    "policy": "policy_players",
}


def _question_groups(plan_payload: dict) -> list[tuple[str, list[str]]]:
    """计划问题 → 维度组（§7.2）：档案已满但计划未完时，按问题 module 分组保持并行。

    fields 置空（问题组不绑字段缺口），组名 = module；问题子集由 _group_plan_view 投影。"""
    modules: dict[str, int] = {}
    for q in plan_payload.get("questions", []):
        if q.get("status") in ("answered", "not_applicable"):
            continue
        mod = q.get("module") or "misc"
        modules[mod] = modules.get(mod, 0) + 1
    if not modules:
        return []
    return [(mod, []) for mod in modules]


logger = logging.getLogger("finance_agent.research")


class ResearchLoop:
    def __init__(
        self,
        *,
        store: BitemporalStore,
        events: EventStore,
        writer: ProfileWriter,
        gateway: DataGateway,
        llm: LLM,
        manifest: RunManifest,
        max_rounds: int | None = None,  # None → 动态预算（§4.2）；显式传入 = 覆盖
        completeness_target: float = 0.8,
        namespace: str = "prod",
        gateway_sources: list[str] | None = None,
        max_steps_per_round: int = 16,
        hooks: list[Hook] | None = None,
        judge_llm: LLM | None = None,  # research-rubric 软反馈（advisory，D4）
        should_stop: Callable[[], bool] | None = None,  # 取消闸（轮次边界检查）
        fetch_document: Callable[[str], str] | None = None,  # 文档正文抓取（live 才有）
        worker_llms: list[LLM] | None = None,  # 维度并行池（P3 §4.2）；None/单元素 → 串行兼容
        # ---- 问题驱动研究（knowledge-dossier-research-redesign §7）----
        plan_id: str | None = None,  # 冻结的 ResearchPlan（metrics 存储中）
        metrics: object | None = None,  # MetricStore
        metric_writer: object | None = None,  # TypedMetricWriter
        calculations: object | None = None,  # CalculationService
    ):
        self._store = store
        self._events = events
        self._writer = writer
        self._gateway = gateway
        self._llm = llm
        self._manifest = manifest
        self._max_rounds = max_rounds
        self._target = completeness_target
        self._namespace = namespace
        self._gateway_sources = gateway_sources or []
        self._max_steps = max_steps_per_round
        self._hooks = hooks or []
        self._judge_llm = judge_llm
        self._should_stop = should_stop
        self._fetch_document = fetch_document
        self._worker_llms = worker_llms or []
        self._plan_id = plan_id
        self._metrics = metrics
        self._metric_writer = metric_writer
        self._calculations = calculations
        self.stop_reason: str | None = None
        #: stalled 时填充缺口诊断卡（research-capability-upgrade §4.3 L3）
        self.stall_diagnostic: dict | None = None
        #: 研究充分度评估（§7.6；有计划+新存储时填充）
        self.assessment: object | None = None
        self.plan_payload: dict | None = None

    def run(
        self,
        entity_kind: str,
        entity_id: str,
        objective: str,
        *,
        now: datetime | None = None,
    ) -> list[IterationReport]:
        from .evidence_desk import ChunkStore

        analyzer = GapAnalyzer(self._store)
        chunk_store = ChunkStore()  # 检索台账：本 run 的证据验证基准（跨轮共享）
        reports: list[IterationReport] = []
        # 评估时刻：显式传入（评估回放）则固定；否则每次 gap 分析取当前真实时间，
        # 避免 run 内新写入的事实因 knowledge_time 晚于「起跑线时刻」而不可见。
        fixed_now = now
        judge_feedback: str | None = None  # 上一轮 rubric 的软反馈
        all_rejected: list[dict] = []  # 跨轮累计被拒提案（诊断卡素材）
        budget: int | None = None  # 首轮 gap 分析后定（动态预算）
        # 维度 researcher 方法论 playbook（每 run 记版本哈希，可复现）
        playbook_text, playbook_ver = load_playbook("dimension_researcher")
        self._emit("research/playbook", {"name": "dimension_researcher", "version": playbook_ver})

        def _now() -> datetime:
            return fixed_now or datetime.now(UTC)

        # 冻结计划（§7.1）：有计划时研究目标由问题队列定义——
        # 已有 100% 档案遇到新目标仍继续研究，只复用有效证据，不宣告「无需研究」
        typed = self._metrics is not None and self._metric_writer is not None
        if self._plan_id and self._metrics is not None:
            self.plan_payload = self._metrics.get_plan(self._plan_id)
            if self.plan_payload is not None:
                self._emit(RESEARCH_PLAN_CREATED, {
                    "plan_id": self._plan_id,
                    "entity": f"{entity_kind}:{entity_id}",
                    "mode": self.plan_payload.get("mode"),
                    "objective": self.plan_payload.get("objective"),
                    "recipe_id": self.plan_payload.get("recipe_id"),
                    "recipe_version": self.plan_payload.get("recipe_version"),
                    "question_count": len(self.plan_payload.get("questions", [])),
                    "budgets": self.plan_payload.get("budgets"),
                    "acceptance": self.plan_payload.get("acceptance"),
                })

        round_no = 0
        while True:
            if self._should_stop is not None and self._should_stop():
                self.stop_reason = "cancelled"
                break
            round_no += 1
            gaps_before = analyzer.analyze(entity_kind, entity_id, _now(), namespace=self._namespace)
            # 每轮重读计划（问题状态由 answer_question 在存储中更新）
            if self._plan_id and self._metrics is not None:
                self.plan_payload = self._metrics.get_plan(self._plan_id)
            coverage, violations = _plan_coverage(self.plan_payload)
            target_coverage = (
                (self.plan_payload or {}).get("budgets", {}).get("question_coverage_target", 0.8)
                if self.plan_payload else 0.8
            )
            coverage_ok = coverage is None or (coverage >= target_coverage and not violations)
            # 收敛 = 基础字段覆盖 且 无陈旧字段 且（有计划时）问题覆盖达标——
            # 字段完整 ≠ 研究充分（§2.2）；无计划时保持旧判据（兼容）
            if (
                gaps_before.completeness >= self._target
                and not gaps_before.stale
                and coverage_ok
            ):
                self.stop_reason = "converged"
                break
            if budget is None:
                budget = self._effective_budget(gaps_before)
            if round_no > budget:
                self.stop_reason = "budget"
                break
            self._emit(RESEARCH_ROUND_START, {
                "round": round_no,
                "gaps": gaps_before.model_dump(mode="json"),
                "question_coverage": coverage,
            })

            # 维度分组并行（P3 §4.2）：配置了 worker 池（>1）才启用——
            # 每组独立 child run / 独立 context / 独立步数预算；共享 ChunkStore（锁保护）。
            # 无池 → 旧式单 kernel 路径（单 LLM 被多组共享会互相抽干脚本，且没有必要隔离）。
            trackers: list = []
            if len(self._worker_llms) > 1:
                # 有计划时维度分组叠加问题归属（问题.module → 字段组）；无计划保持旧分组
                groups = _dimension_groups(
                    gaps_before.missing, gaps_before.stale, gaps_before.optional_missing,
                    entity_kind=entity_kind, weak=list(gaps_before.weak),
                )
                if self.plan_payload and not any(g[1] for g in groups):
                    # 档案已满但计划未完：按问题模块分组（§7.2 问题 → worker 组）
                    groups = _question_groups(self.plan_payload)
                workers = self._worker_llms
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=max(1, len(groups))) as pool:
                    futures = [
                        pool.submit(
                            self._run_group, entity_kind, entity_id, objective,
                            gaps_before, round_no, gname, gfields,
                            workers[i % len(workers)], judge_feedback,
                            playbook_text, chunk_store,
                        )
                        for i, (gname, gfields) in enumerate(groups)
                    ]
                    group_results = [f.result() for f in futures]
                trackers = [tr for _, tr in group_results]
                written = [f for tr in trackers for f in tr.written]
                rejected = [r for tr in trackers for r in tr.rejected]
            else:
                tools, tracker = make_research_tools(
                    store=self._store,
                    writer=self._writer,
                    manifest=self._manifest,
                    entity_kind=entity_kind,
                    entity_id=entity_id,
                    namespace=self._namespace,
                    chunk_store=chunk_store,
                    fetch_document=self._fetch_document,
                    events=self._events,
                    metrics=self._metrics,
                    metric_writer=self._metric_writer,
                    calculations=self._calculations,
                    plan_id=self._plan_id,
                )
                for source_id in self._gateway_sources:
                    tools[f"query_{source_id}"] = make_gateway_tool(
                        self._gateway, source_id, chunk_store
                    )

                if round_no == 1:  # system 契约只注入一次（稳定前缀）
                    self._emit(CONTEXT_INJECT, {"role": "system", "content": GROUNDING_CONTRACT})

                kernel = AgentKernel(
                    store=self._events,
                    llm=self._llm,
                    manifest=self._manifest,
                    tools=tools,
                    hooks=self._hooks,
                    max_steps=self._max_steps,
                )
                kernel.run_turn(
                    build_round_brief(
                        entity_kind, entity_id, objective, gaps_before, round_no,
                        judge_feedback=judge_feedback,
                        plan_payload=self.plan_payload,
                        typed_tools=typed and set(tools) >= {"propose_metric", "answer_question"},
                    )
                )
                trackers = [tracker]
                written, rejected = tracker.written, tracker.rejected

            gaps_after = analyzer.analyze(entity_kind, entity_id, _now(), namespace=self._namespace)
            if self._plan_id and self._metrics is not None:
                self.plan_payload = self._metrics.get_plan(self._plan_id)
            coverage_after, violations_after = _plan_coverage(self.plan_payload)
            target_coverage = (
                (self.plan_payload or {}).get("budgets", {}).get("question_coverage_target", 0.8)
                if self.plan_payload else 0.8
            )
            report = IterationReport(
                run_id=self._manifest.run_id,
                round=round_no,
                entity=f"{entity_kind}:{entity_id}",
                completeness_before=gaps_before.completeness,
                completeness_after=gaps_after.completeness,
                facts_written=written,
                rejected=rejected,
                missing_after=list(gaps_after.missing),
                # 进展 = 字段/观测/论断/计算/问题任一有成功产出（§7.7 防 stalled 误判）
                progress=any(tr.any_progress for tr in trackers),
                observations_written=[m for tr in trackers for m in tr.observations],
                claims_written=[c for tr in trackers for c in tr.claims],
                calculations_done=[c for tr in trackers for c in tr.calculations],
                questions_advanced=[q for tr in trackers for q in tr.questions_advanced],
                question_coverage=coverage_after,
            )
            self._emit(RESEARCH_ROUND_END, report.model_dump(mode="json"))
            reports.append(report)
            all_rejected.extend(rejected)

            judge_feedback = self._judge_round(report)

            coverage_ok_after = coverage_after is None or (
                coverage_after >= target_coverage and not violations_after
            )
            if (
                gaps_after.completeness >= self._target
                and not gaps_after.stale
                and coverage_ok_after
            ):
                self.stop_reason = "converged"
                break
            if not report.progress:
                self.stop_reason = "stalled"
                self._emit_stall_diagnostic(
                    entity_kind, entity_id, gaps_after, all_rejected, reports
                )
                break

        self._finalize_assessment(entity_kind, entity_id, _now(), reports)
        return reports

    # ---------------- 研究充分度评估（§7.6） ----------------

    def _finalize_assessment(
        self, entity_kind: str, entity_id: str, now: datetime, reports: list[IterationReport],
    ) -> None:
        """终局评估：硬门禁由代码运行（rubric 满分不能覆盖引用失败）。"""
        if self._metrics is None or self.plan_payload is None:
            return
        from .assessment import assess
        from .plan import ResearchPlan

        try:
            plan = ResearchPlan.model_validate(self.plan_payload)
        except Exception:
            logger.warning("评估跳过：计划 payload 不可解析 %s", self._plan_id, exc_info=True)
            return
        claims = self._metrics.claims_as_of(
            entity_kind, entity_id, now, namespace=self._namespace,
            statuses=("draft", "validated"),
        )
        observations = self._metrics.observations_as_of(
            entity_kind, entity_id, now, namespace=self._namespace
        )
        calculations = []
        for tr_reports in reports:
            for cid in tr_reports.calculations_done:
                stored = self._metrics.get_calculation(cid)
                if stored is not None:
                    calculations.append(stored.payload)
        analyzer = GapAnalyzer(self._store)
        gaps = analyzer.analyze(entity_kind, entity_id, now, namespace=self._namespace)
        open_conflicts = len(gaps.conflicts) + len(
            self._metrics.conflicted_semantic_hashes(
                entity_kind, entity_id, namespace=self._namespace,
                as_of=now, exclude_resolved=True,
            )
        )
        assessment = assess(
            plan,
            claims=claims,
            observations=observations,
            calculations=calculations,
            open_conflicts=open_conflicts,
            stale_fields=list(gaps.stale),
            stop_reason=self.stop_reason or "",
            namespace=self._namespace,
            now=now,
        )
        self.assessment = assessment
        self._emit(RESEARCH_ASSESSMENT, assessment.model_dump(mode="json"))
        # 收尾状态走锁内原子入口（不与并行问题更新互踩，review #8）
        self._metrics.set_plan_status(plan.plan_id, "completed", namespace=self._namespace)
        self.plan_payload = self._metrics.get_plan(self._plan_id) or self.plan_payload

    def _effective_budget(self, gaps) -> int:
        """预算动态化（§4.2）：显式 max_rounds 优先；其次冻结计划的模式预算（§7.7）；
        否则按初始完整度定预算（旧行为兼容）。"""
        if self._max_rounds is not None:
            return self._max_rounds
        if self.plan_payload:
            plan_rounds = (self.plan_payload.get("budgets") or {}).get("max_rounds")
            if plan_rounds:
                return int(plan_rounds)
        if not gaps.missing and gaps.stale:
            return 1  # 仅刷新陈旧字段
        if gaps.completeness == 0:
            return 5
        if gaps.completeness > 0.5:
            return 3
        return 4

    def _run_group(
        self,
        entity_kind: str,
        entity_id: str,
        objective: str,
        gaps_before,
        round_no: int,
        group: str,
        fields: list[str],
        llm: LLM,
        judge_feedback: str | None,
        playbook_text: str,
        chunk_store,
    ) -> tuple[str, object]:
        """单个维度组的一轮研究：独立 child run（context 隔离）+ 独立步数预算。

        返回 (group, tracker)——tracker 携带字段/观测/论断/计算/问题全部进展。"""
        tools, tracker = make_research_tools(
            store=self._store,
            writer=self._writer,
            manifest=self._manifest,
            entity_kind=entity_kind,
            entity_id=entity_id,
            namespace=self._namespace,
            chunk_store=chunk_store,
            fetch_document=self._fetch_document,
            events=self._events,
            metrics=self._metrics,
            metric_writer=self._metric_writer,
            calculations=self._calculations,
            plan_id=self._plan_id,
        )
        for source_id in self._gateway_sources:
            tools[f"query_{source_id}"] = make_gateway_tool(
                self._gateway, source_id, chunk_store
            )
        group_run_id = f"{self._manifest.run_id}--r{round_no}-{group}"
        self._events.append(
            Event(
                run_id=group_run_id,
                type=RUN_CREATED,
                payload={
                    "parent_run_id": self._manifest.run_id,
                    "kind": "dimension_group",
                    "group": group,
                    "round": round_no,
                },
            )
        )
        self._events.append(
            Event(run_id=group_run_id, type=CONTEXT_INJECT,
                  payload={"role": "system", "content": GROUNDING_CONTRACT})
        )
        # 组内弱字段单独挂（不混进「缺失」谎报）：专职组看到的 brief 区分
        # 「缺失字段」与「待改进字段（已有值但未过质检）」，避免跨组重写同一弱字段。
        weak_scoped = {f: list(v) for f, v in gaps_before.weak.items() if f in set(fields)}
        group_gaps = gaps_before.model_copy(
            update={
                "missing": [f for f in fields if f not in weak_scoped],
                "stale": [],
                "optional_missing": [],
                "weak": weak_scoped,
            }
        )
        brief = (
            build_round_brief(
                entity_kind, entity_id, objective, group_gaps, round_no,
                judge_feedback=judge_feedback,
                plan_payload=self._group_plan_view(group, fields),
                typed_tools="propose_metric" in tools,
            )
            + f"\n\n你是「{group}」维度组的专职研究员。\n{playbook_text}"
            # 生产纪律（2026-09-01 实测 flash worker 囤证据空转：124 次登记 0 次写入）
            + "\n\n生产纪律（防囤证据空转）：按字段逐个推进——每字段：搜索 → 登记 1-2 条关键证据"
              " → 立即 propose_fact；禁止连续登记超过 3 条证据而不写事实；"
              "本组字段写完才准碰可选维度；写不出就留白，换下一个字段。"
        )
        try:
            kernel = AgentKernel(
                store=self._events,
                llm=llm,
                manifest=RunManifest(
                    run_id=group_run_id,
                    mode=self._manifest.mode,
                    eval_as_of=self._manifest.eval_as_of,
                ),
                tools=tools,
                hooks=self._hooks,
                max_steps=12,  # 维度组独立步数预算（§4.2）
            )
            kernel.run_turn(brief)
        except Exception as e:  # 单组失败不拖死整轮——记入被拒清单（可见），其余组继续
            logger.warning("维度组 %s 第 %d 轮失败：%s", group, round_no, e)
            tracker.rejected.append({"field": f"group:{group}", "reason": f"{type(e).__name__}: {e}"})
        self._events.append(
            Event(
                run_id=self._manifest.run_id,
                type="research/group_end",
                payload={
                    "round": round_no,
                    "group": group,
                    "fields": fields,
                    "written": list(tracker.written),
                    "rejected": list(tracker.rejected),
                    "evidence_registered": len(tracker.registered),  # 囤证据检测
                    "observations": list(tracker.observations),
                    "claims": list(tracker.claims),
                    "questions_advanced": list(tracker.questions_advanced),
                },
            )
        )
        return group, tracker

    def _group_plan_view(self, group: str, fields: list[str]) -> dict | None:
        """维度组看到的计划子集（review #7）：问题按 module→维度组映射分配，
        未映射/无模块的问题（如 targeted 自定义问题）对全部组可见（回答幂等，
        重复推进无害）；已 answered/not_applicable 的问题不再下发。"""
        if not self.plan_payload:
            return None
        questions = []
        for q in self.plan_payload.get("questions", []):
            if q.get("status") in ("answered", "not_applicable"):
                continue
            module = q.get("module", "")
            mapped = _QUESTION_MODULE_TO_GROUP.get(module, module)  # 问题组路径：组名即 module
            if mapped == group or q.get("question_id", "") in fields or group == "misc" or not module:
                questions.append(q)
        if not questions:
            return None
        return {**self.plan_payload, "questions": questions}

    def _emit_stall_diagnostic(
        self,
        entity_kind: str,
        entity_id: str,
        gaps,
        rejected: list[dict],
        reports: list[IterationReport],
    ) -> None:
        """stalled 三通道：事件落库（本方法）+ 日志 + 摘要由 step 层上浮给用户。

        诊断卡回答三个问题：缺什么、试过什么（各源查询与提案被拒记录）、建议怎么办。
        """
        diag = {
            "entity": f"{entity_kind}:{entity_id}",
            "rounds_attempted": len(reports),
            "missing_fields": list(gaps.missing),
            "stale_fields": list(gaps.stale),
            "rejected": list(rejected),
            "sources_available": list(self._gateway_sources),
            "suggestions": _stall_suggestions(entity_id, gaps.missing, self._gateway_sources),
        }
        self.stall_diagnostic = diag
        logger.warning(
            "research stalled %s:%s：%d 轮零写入，缺口 %s；建议：%s",
            entity_kind, entity_id, len(reports), gaps.missing, diag["suggestions"],
        )
        self._emit(RESEARCH_STALL_DIAGNOSTIC, diag)

    def _judge_round(self, report: IterationReport) -> str | None:
        """rubric 软反馈：评分落事件，gaps 进下一轮 brief；解析失败无害。"""
        if self._judge_llm is None:
            return None
        from .rubric import RubricJudge

        digest = report.model_dump_json()
        score = RubricJudge(self._judge_llm).judge(digest)
        if score is None:
            self._emit(RESEARCH_RUBRIC, {"round": report.round, "parse_error": True})
            return None
        self._emit(
            RESEARCH_RUBRIC,
            {"round": report.round, "scores": score.model_dump(), "gaps": score.gaps},
        )
        return "；".join(score.gaps) if score.gaps else None

    def _emit(self, type_: str, payload: dict) -> None:
        self._events.append(Event(run_id=self._manifest.run_id, type=type_, payload=payload))


#: 定性维度字段（当前数据源覆盖薄弱的那批——EDGAR/行情撑不起）
_QUALITATIVE_FIELDS = frozenset({"moat", "risks", "management", "catalysts", "counter_evidence", "peers"})


def _stall_suggestions(entity_id: str, missing: list[str], sources: list[str]) -> list[str]:
    """停滞建议（规则化，不依赖 LLM）：按缺口形态指出最可能的数据边界。"""
    out: list[str] = []
    if re.fullmatch(r"\d{4,5}(\.HK)?", entity_id) or entity_id.upper().endswith(".HK"):
        out.append(
            "港股披露不在 SEC EDGAR 覆盖内；待接入 HKEXnews 源后重试"
            "（docs/research-capability-upgrade.md P2）"
        )
    if set(missing) & _QUALITATIVE_FIELDS:
        out.append(
            "定性维度（护城河/风险/管理层等）靠现有 EDGAR+行情源覆盖薄弱；"
            "待接入 web 搜索源（Exa）后重试（docs/research-capability-upgrade.md P2）"
        )
    if not out:
        out.append("可换查询策略重试，或缩小研究目标范围（objective 指定更具体的缺口字段）")
    return out
