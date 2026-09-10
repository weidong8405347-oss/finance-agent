"""ResearchLoop：轮次制迭代研究（S1，DESIGN.md §5.1）。

一轮 = gap 分析 → LLM turn（工具：登记证据/写事实/查档案/查数据）
     → 过程评估（coverage 软反馈 + writer 硬门禁）→ IterationReport。
收敛：完整度达标；停滞：一轮无成功写入；预算：max_rounds。
取消：should_stop 在轮次边界被检查（stop_command 的落点），已落库事实保留。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime

from ..eventstore.events import (
    CONTEXT_INJECT,
    RESEARCH_ASSESSMENT,
    RESEARCH_BUDGET,
    RESEARCH_PARTIAL_PUBLISHED,
    RESEARCH_PLAN_CREATED,
    RESEARCH_QUESTION_STALL,
    RESEARCH_ROUND_END,
    RESEARCH_ROUND_START,
    RESEARCH_RUBRIC,
    RESEARCH_SCHEDULE,
    RESEARCH_STALL_DIAGNOSTIC,
    RUN_CREATED,
    Event,
)
from ..eventstore.store import (
    DEFAULT_KEEP_RECENT_TOOLS,
    DEFAULT_MAX_TOOL_CHARS,
    EventStore,
)
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
from .scheduling import (
    DIMENSION_GROUPS_INDUSTRY,
    DIMENSION_GROUPS_STOCK,
    DispatchError,
    WorkItem,
    assert_dispatch_complete,
    build_schedule,
    dimension_groups,
    pending_questions,
    plan_view,
    worker_group,
)
from .tools import make_research_tools

#: 向后兼容别名（组口径唯一真相源已迁至 scheduling.py，audit §3.1）
_dimension_groups = dimension_groups
_DIMENSION_GROUPS_STOCK = DIMENSION_GROUPS_STOCK
_DIMENSION_GROUPS_INDUSTRY = DIMENSION_GROUPS_INDUSTRY


def _plan_coverage(plan_payload: dict | None) -> tuple[float | None, list[str]]:
    """冻结计划的问题覆盖（§7.6）：返回 (coverage, violations)；无计划 = (None, [])。"""
    if not plan_payload:
        return None, []
    from .assessment import coverage_of
    from .plan import ResearchPlan

    plan = ResearchPlan.model_validate(plan_payload)
    cov = coverage_of(plan)
    return cov.coverage, cov.violations


def _question_groups(plan_payload: dict) -> list[tuple[str, list[str]]]:
    """计划问题 → worker 组（§7.2；audit §3.1 修复后与下发口径同源）。

    组名由 `worker_group(entity_kind, module)` 给出——与 `_group_plan_view` 过滤问题
    用的是同一张表；旧实现「建组用 module 原名、过滤用维度名」导致 20 个 worker
    全部拿到空计划视图（live-a2cce641 事故根因）。
    """
    groups: dict[str, list[str]] = {}
    for q in pending_questions(plan_payload):
        g = worker_group(str(plan_payload.get("entity_kind") or "stock"), str(q.get("module") or ""))
        groups.setdefault(g, [])
    return [(g, []) for g in groups]


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
        #: 分页抓取（Document Read v2）：f(url) -> FetchedDocument；缺省退回旧纯文本抓取
        fetch_document_paged: Callable[[str], object] | None = None,
        worker_llms: list[LLM] | None = None,  # 维度并行池（P3 §4.2）；None/单元素 → 串行兼容
        # ---- 问题驱动研究（knowledge-dossier-research-redesign §7）----
        plan_id: str | None = None,  # 冻结的 ResearchPlan（metrics 存储中）
        metrics: object | None = None,  # MetricStore
        metric_writer: object | None = None,  # TypedMetricWriter
        calculations: object | None = None,  # CalculationService
        # ---- 真实预算闸（audit §3.3）----
        budget: object | None = None,  # RunBudget；None 且有计划 → 按模式预算表自建
        max_record_chars: int | None = None,  # 单条检索记录正文上限（超出走 read_chunk）
        # ---- 上下文裁剪（audit §3.3 余项：存储全量、消费剪裁）----
        #: 只保留最近 N 条工具结果全文，更早的裁到 max_tool_chars（只动投影不动日志）
        max_tool_chars: int | None = DEFAULT_MAX_TOOL_CHARS,
        keep_recent_tools: int | None = DEFAULT_KEEP_RECENT_TOOLS,
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
        self._fetch_paged = fetch_document_paged
        self._worker_llms = worker_llms or []
        self._plan_id = plan_id
        self._metrics = metrics
        self._metric_writer = metric_writer
        self._calculations = calculations
        self._run_budget = budget
        self._max_record_chars = max_record_chars
        self._max_tool_chars = max_tool_chars
        self._keep_recent_tools = keep_recent_tools
        self.stop_reason: str | None = None
        #: 预算终止的具体维度（wall_clock/tokens/retrieval_calls/...）——可归因，不笼统
        self.budget_exhausted: list[str] = []
        #: 末轮预算快照（摘要/UI 显示投入与等待原因）
        self.budget_snapshot: dict | None = None
        #: 本轮因墙钟超时未完成的组（慢组不拖死整轮，audit §3.3）
        self.timed_out_groups: list[str] = []
        #: 同 run 检索去重缓存（run() 内创建）
        self._retrieval_cache: dict = {}
        #: stalled 时填充缺口诊断卡（research-capability-upgrade §4.3 L3）
        self.stall_diagnostic: dict | None = None
        #: 问题零推进诊断（audit §3.1）：与 stall_diagnostic 分开，指向调度/提交/来源
        self.question_stall_diagnostic: dict | None = None
        #: 最近一次部分成果检查点（audit §3.9）：取消/超时路径据此冻结 partial 产物
        self.partial_checkpoint: dict | None = None
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
        # 文档库（Document Read v2）：同一财报只抓取解析一次，各 worker 共享只读文档引用
        from ..gateway.documents import DocumentStore

        doc_store = DocumentStore()
        reports: list[IterationReport] = []
        # 评估时刻：显式传入（评估回放）则固定；否则每次 gap 分析取当前真实时间，
        # 避免 run 内新写入的事实因 knowledge_time 晚于「起跑线时刻」而不可见。
        fixed_now = now
        judge_feedback: str | None = None  # 上一轮 rubric 的软反馈
        all_rejected: list[dict] = []  # 跨轮累计被拒提案（诊断卡素材）
        round_budget: int | None = None  # 首轮 gap 分析后定（动态轮数预算）
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
        # 真实预算闸（audit §3.3）：设计给了 wall-clock/检索上限，旧实现只消费轮数。
        # 未显式注入且有冻结计划 → 按模式预算表自建（deep=40min/80 次检索）。
        run_budget = self._run_budget
        if run_budget is None and self.plan_payload:
            from .budget import budget_for_mode

            run_budget = budget_for_mode(
                str(self.plan_payload.get("mode") or "standard"), self.plan_payload
            )
            self._run_budget = run_budget
        if run_budget is not None:
            run_budget.start()
            self._emit(RESEARCH_BUDGET, {"action": "start", "run_id": self._manifest.run_id,
                                         "budget": run_budget.snapshot().as_payload()})
        #: 同 run 检索去重（重复资料既是成本也是上下文膨胀的主因）
        retrieval_cache: dict = self._retrieval_cache
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
            if round_budget is None:
                round_budget = self._effective_budget(gaps_before)
            if round_no > round_budget:
                self.stop_reason = "budget"
                self.budget_exhausted = ["max_rounds"]
                break
            if run_budget is not None and run_budget.is_exhausted():
                # wall-clock/token/检索/工具 任一耗尽即停（不再只数轮数）；
                # 已落库成果保留，末段预留给部分成果合成。
                self.stop_reason = "budget"
                self.budget_exhausted = run_budget.exhausted()
                self._emit(RESEARCH_BUDGET, {
                    "action": "research_stopped", "reason": ",".join(self.budget_exhausted),
                    "round": round_no, "budget": run_budget.snapshot().as_payload(),
                })
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
            dispatched_qids: list[str] = [str(q.get("question_id") or "")
                                          for q in pending_questions(self.plan_payload)]
            if len(self._worker_llms) > 1:
                # 调度器产出显式 WorkItem（audit §3.1）：字段缺口 × 计划问题 → worker，
                # 建组与问题下发同源（同一张 entity_kind+module → group 表）。
                field_groups = dimension_groups(
                    gaps_before.missing, gaps_before.stale, gaps_before.optional_missing,
                    entity_kind=entity_kind, weak=list(gaps_before.weak),
                )
                schedule = build_schedule(
                    plan_payload=self.plan_payload,
                    entity_kind=entity_kind,
                    field_groups=field_groups,
                    max_workers=(self.plan_payload or {}).get("budgets", {}).get(
                        "max_parallel_workers"
                    ) if self.plan_payload else None,
                )
                if schedule.unassigned:
                    # 问题未完整分发 = 装配缺陷：fail-loud，不带空计划烧预算
                    raise DispatchError(
                        f"第 {round_no} 轮调度失败：问题未完整分发 {schedule.unassigned}"
                    )
                if not schedule.items:
                    schedule.items = [WorkItem(group="misc", fields=())]
                assert_dispatch_complete(schedule, self.plan_payload)
                dispatched_qids = schedule.dispatched_question_ids
                self._emit(RESEARCH_SCHEDULE, {
                    "round": round_no, "entity": f"{entity_kind}:{entity_id}",
                    **schedule.as_payload(),
                })
                workers = self._worker_llms
                from concurrent.futures import ThreadPoolExecutor, wait

                pool = ThreadPoolExecutor(
                    max_workers=max(1, len(schedule.items)),
                    thread_name_prefix=f"research-r{round_no}",
                )
                futures = {
                    item.group: pool.submit(
                        self._run_group, entity_kind, entity_id, objective,
                        gaps_before, round_no, item,
                        workers[i % len(workers)], judge_feedback,
                        playbook_text, chunk_store, doc_store,
                    )
                    for i, item in enumerate(schedule.items)
                }
                # 按剩余墙钟等待，不做整轮屏障（audit §3.3）：慢组超时只影响所属问题，
                # 其余组已完成的成果立即进本轮报告。
                remaining = run_budget.remaining_seconds() if run_budget is not None else None
                done, not_done = wait(list(futures.values()), timeout=remaining)
                group_results = [
                    f.result() for f in done if not f.cancelled() and f.exception() is None
                ]
                timed_out = sorted(g for g, f in futures.items() if f in not_done)
                if not_done:
                    # 不等待未完成组（shutdown(wait=False)）：研究阶段必须在 deadline 内返回。
                    # 未完成线程自行跑完并落库，已写入的成果不丢。
                    pool.shutdown(wait=False, cancel_futures=True)
                    self._emit(RESEARCH_BUDGET, {
                        "action": "group_timeout", "round": round_no,
                        "groups": timed_out,
                        "reason": "慢组超时：只影响所属问题，本轮不等待",
                        "budget": run_budget.snapshot().as_payload() if run_budget else {},
                    })
                    logger.warning(
                        "第 %d 轮有 %d 个组未在剩余墙钟内完成（%s）：不阻塞本轮验收",
                        round_no, len(not_done), timed_out,
                    )
                else:
                    pool.shutdown(wait=True)
                trackers = [tr for _, tr in group_results]
                self.timed_out_groups = timed_out
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
                    doc_store=doc_store,
                    fetch_paged=self._fetch_paged,
                )
                for source_id in self._gateway_sources:
                    tools[f"query_{source_id}"] = make_gateway_tool(
                        self._gateway, source_id, chunk_store,
                        budget=run_budget, cache=retrieval_cache,
                        max_record_chars=self._max_record_chars,
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
                    budget=run_budget,
                    max_tool_chars=self._max_tool_chars,
                    keep_recent_tools=self._keep_recent_tools,
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
            self._emit_question_stall(
                entity_kind, entity_id, round_no, report, trackers, dispatched_qids
            )
            self._emit_partial(entity_kind, entity_id, round_no, report)

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
        if self._run_budget is not None:
            self.budget_snapshot = self._run_budget.snapshot().as_payload()
            # 台账级重复（同正文不同请求）与检索级重复（同请求）分开计数
            self.budget_snapshot["duplicate_chunks"] = chunk_store.duplicates
            # 文档级重复（同内容哈希/同来源 URL 被短路复用）：重复资料率可见
            self.budget_snapshot["duplicate_documents"] = doc_store.duplicates
            self.budget_snapshot["documents_stored"] = len(doc_store)
            self.budget_snapshot["timed_out_groups"] = list(self.timed_out_groups)
            self._emit(RESEARCH_BUDGET, {
                "action": "research_end", "stop_reason": self.stop_reason,
                "exhausted": list(self.budget_exhausted),
                "budget": self.budget_snapshot,
            })
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
        # 来源角色归类需要 evidence_id → source_id/url（PIT 与一手分离，方案 §2 P0；
        # 披露域细化 F7）；解析不了的引用归 unknown 档（诚实缺省，不默认一手）
        evidence_sources: dict[str, str] = {}
        evidence_urls: dict[str, str] = {}
        for obs in observations:
            for ref in getattr(obs, "evidence_refs", None) or []:
                if ref in evidence_sources:
                    continue
                try:
                    ev = self._store.get_evidence(ref)
                except Exception:  # noqa: BLE001 - 不可解析本身由 assessment 计入 unknown 档
                    continue
                evidence_sources[ref] = ev.source_id
                if ev.url:
                    evidence_urls[ref] = ev.url
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
            evidence_sources=evidence_sources,
            evidence_urls=evidence_urls,
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
        item: WorkItem,
        llm: LLM,
        judge_feedback: str | None,
        playbook_text: str,
        chunk_store,
        doc_store=None,
    ) -> tuple[str, object]:
        """单个 WorkItem 的一轮研究：独立 child run（context 隔离）+ 独立步数预算。

        worker 拿到的是调度器显式下发的 question_ids（audit §3.1），不再自行推导；
        answer_question 也只接受本 worker 被分配的问题（工具层硬门禁）。
        返回 (group, tracker)——tracker 携带字段/观测/论断/计算/问题全部进展。"""
        group, fields = item.group, list(item.fields)
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
            allowed_question_ids=list(item.question_ids) or None,
            doc_store=doc_store,
            fetch_paged=self._fetch_paged,
        )
        for source_id in self._gateway_sources:
            tools[f"query_{source_id}"] = make_gateway_tool(
                self._gateway, source_id, chunk_store,
                budget=self._run_budget, cache=self._retrieval_cache,
                max_record_chars=self._max_record_chars,
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
                    "question_ids": list(item.question_ids),
                    "fields": fields,
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
                plan_payload=self._group_plan_view(group, fields, item.question_ids),
                typed_tools="propose_metric" in tools,
                assigned_question_ids=list(item.question_ids) or None,
            )
            + f"\n\n你是「{group}」维度组的专职研究员。\n{playbook_text}"
            + _worker_discipline(
                question_driven=bool(item.question_ids),
                has_document_reader=(
                    "document" if "read_document" in tools
                    else "edgar" if "read_edgar_filing" in tools
                    else ""
                ),
            )
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
                # 维度组独立步数预算（§4.2）：问题驱动组 16 步（精读→证据→观测/论断→
                # 交题的完整链需要余量；哨兵基线试跑：12 步下证据全部登记成功却零提交），
                # legacy 补字段组保持 12 步（防囤证据空转的旧约束不变）
                max_steps=16 if item.question_ids else 12,
                budget=self._run_budget,  # 全局墙钟/token/检索预算共享扣减（audit §3.3）
                max_tool_chars=self._max_tool_chars,
                keep_recent_tools=self._keep_recent_tools,
            )
            kernel.run_turn(brief)
            if kernel.budget_stop:
                tracker.rejected.append(
                    {"field": f"group:{group}", "reason": f"预算终止：{kernel.budget_stop}"}
                )
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
                    "question_ids": list(item.question_ids),
                    "written": list(tracker.written),
                    "rejected": list(tracker.rejected),
                    "evidence_registered": len(tracker.registered),  # 囤证据检测
                    "observations": list(tracker.observations),
                    "claims": list(tracker.claims),
                    "questions_advanced": list(tracker.questions_advanced),
                    "answer_rejections": list(tracker.answer_rejections),
                },
            )
        )
        return group, tracker

    def _group_plan_view(
        self, group: str, fields: list[str], question_ids: list[str] | tuple[str, ...] = (),
    ) -> dict | None:
        """维度组看到的计划子集。

        audit §3.1 修复：question_ids 给定时（调度器路径）严格按它投影，不再用
        module→组名映射二次推导——建组与下发同源，不可能再失配。
        无 question_ids 时（旧调用/串行路径）回退到映射口径：未映射/无模块的问题
        （如 targeted 自定义问题）对全部组可见（回答幂等，重复推进无害）。
        """
        if not self.plan_payload:
            return None
        if question_ids:
            return plan_view(self.plan_payload, list(question_ids))
        questions = []
        for q in pending_questions(self.plan_payload):
            module = str(q.get("module", ""))
            mapped = worker_group(str(self.plan_payload.get("entity_kind") or "stock"), module)
            if (
                mapped == group
                or q.get("question_id", "") in fields
                or group == "misc"
                or not module
            ):
                questions.append(q)
        if not questions:
            return None
        return {**self.plan_payload, "questions": questions}

    def _emit_partial(
        self, entity_kind: str, entity_id: str, round_no: int, report: IterationReport,
    ) -> None:
        """部分成果检查点（audit §3.9）：每完成一个问题就落一次可发布的已验证成果。

        事件携带输入指纹（plan_id + 已答问题 + 引用集），使取消/超时路径能把
        已验证成果冻结成 partial 产物，而不是等最终合成才有可读交付。
        """
        if not (report.questions_advanced or report.claims_written
                or report.observations_written or report.calculations_done):
            return
        answered = [
            {"question_id": q.get("question_id"), "status": q.get("status"),
             "conclusion": q.get("conclusion"), "support_refs": q.get("support_refs") or [],
             "counter_refs": q.get("counter_refs") or [], "unresolved": q.get("unresolved") or []}
            for q in (self.plan_payload or {}).get("questions", [])
            if q.get("status") in ("answered", "disputed", "unavailable")
        ]
        checkpoint = {
            "entity": f"{entity_kind}:{entity_id}",
            "round": round_no,
            "plan_id": self._plan_id,
            "answered": answered,
            "claims": list(report.claims_written),
            "observations": list(report.observations_written),
            "calculations": list(report.calculations_done),
            "facts": list(report.facts_written),
            "budget": self._run_budget.snapshot().as_payload() if self._run_budget else {},
            "input_hash": hashlib.sha256(json.dumps(
                {"plan": self._plan_id, "answered": [a["question_id"] for a in answered],
                 "claims": sorted(report.claims_written),
                 "observations": sorted(report.observations_written)},
                ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16],
        }
        self.partial_checkpoint = checkpoint
        self._emit(RESEARCH_PARTIAL_PUBLISHED, checkpoint)

    def _emit_question_stall(
        self, entity_kind: str, entity_id: str, round_no: int,
        report: IterationReport, trackers: list, dispatched_qids: list[str],
    ) -> None:
        """问题零推进的具体诊断（audit §3.1）。

        一个调度周期没有任何问题推进就不能当作「模型不够努力」笼统处理，必须区分：
        - not_dispatched：问题根本没进 worker 上下文（装配缺陷，本次事故形态）；
        - submit_rejected：answer_question 被服务端门禁拒（引用/结论不合格）；
        - source_unavailable：本轮一条证据都没登记成（源/网络/权限）；
        - analysis_incomplete：有证据但没形成观测/论断（分析未完成）。
        """
        pending = pending_questions(self.plan_payload)
        if not pending or report.questions_advanced:
            return
        pending_ids = [str(q.get("question_id") or "") for q in pending]
        answer_rejections = [r for tr in trackers for r in getattr(tr, "answer_rejections", [])]
        registered = sum(len(getattr(tr, "registered", [])) for tr in trackers)
        produced = bool(report.observations_written or report.claims_written
                        or report.calculations_done or report.facts_written)
        causes: list[str] = []
        missing_dispatch = [q for q in pending_ids if q not in set(dispatched_qids)]
        if missing_dispatch:
            causes.append("not_dispatched")
        if answer_rejections:
            causes.append("submit_rejected")
        if registered == 0:
            causes.append("source_unavailable")
        if registered and not produced:
            causes.append("hoarding_evidence")
        if produced and not answer_rejections and not missing_dispatch:
            causes.append("analysis_incomplete")
        diag = {
            "entity": f"{entity_kind}:{entity_id}",
            "round": round_no,
            "causes": causes or ["analysis_incomplete"],
            "pending_question_ids": pending_ids,
            "dispatched_question_ids": list(dispatched_qids),
            "not_dispatched": missing_dispatch,
            "answer_rejections": answer_rejections,
            "evidence_registered": registered,
            "produced": {
                "facts": list(report.facts_written),
                "observations": list(report.observations_written),
                "claims": list(report.claims_written),
                "calculations": list(report.calculations_done),
            },
            "suggestions": _question_stall_suggestions(causes, missing_dispatch, answer_rejections),
        }
        self.question_stall_diagnostic = diag
        logger.warning(
            "research 问题零推进 %s:%s 第 %d 轮：%s（待答 %d 题）",
            entity_kind, entity_id, round_no, diag["causes"], len(pending_ids),
        )
        self._emit(RESEARCH_QUESTION_STALL, diag)

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

        digest = self._build_judge_digest(report)
        score = RubricJudge(self._judge_llm).judge(digest)
        if score is None:
            self._emit(RESEARCH_RUBRIC, {"round": report.round, "parse_error": True})
            return None
        self._emit(
            RESEARCH_RUBRIC,
            {"round": report.round, "scores": score.model_dump(), "gaps": score.gaps},
        )
        return "；".join(score.gaps) if score.gaps else None

    #: rubric digest 上限：够放 ~12 条论断 + 摘录，不致于把 judge 上下文打爆
    _JUDGE_DIGEST_MAX_CHARS = 12000

    def _build_judge_digest(self, report: IterationReport) -> str:
        """rubric 输入升级（tools-plugins 方案 §2「研究评审」P0）。

        旧缺陷：judge 只收到 IterationReport 的 ID/字段/计数，无法判断一手证据、
        反证质量、推理跳跃和关键遗漏。现在附带：本轮写入论断的原文 + 支持摘录、
        计划问题的结论/未解决项（均有界），让评分基于内容而非仅基于结构。
        """
        parts: list[str] = [report.model_dump_json()]
        if self._metrics is not None and report.claims_written:
            claim_entries = []
            for cid in report.claims_written[:12]:
                payload = self._metrics.get_claim(cid)
                if payload is None:
                    continue
                quotes = []
                for ref in (payload.get("support_refs") or [])[:3]:
                    if str(ref).startswith("ev-"):
                        try:
                            quotes.append(self._store.get_evidence(str(ref)).verbatim_quote[:400])
                        except Exception:  # noqa: BLE001 - 未登记引用本身就是评审信号
                            quotes.append(f"{ref}（不可解析）")
                claim_entries.append({
                    "claim_id": cid,
                    "statement": str(payload.get("statement") or "")[:400],
                    "kind": payload.get("kind"),
                    "status": payload.get("status"),
                    "question_id": payload.get("question_id"),
                    "limitations": (payload.get("limitations") or [])[:3],
                    "support_quotes": quotes,
                })
            if claim_entries:
                parts.append(
                    "本轮写入的论断（含支持证据原文摘录，评审其是否真正支持结论）："
                    + json.dumps(claim_entries, ensure_ascii=False)
                )
        if self.plan_payload:
            answered = [
                {"question_id": q.get("question_id"), "status": q.get("status"),
                 "conclusion": str(q.get("conclusion") or "")[:300],
                 "unresolved": (q.get("unresolved") or [])[:3]}
                for q in self.plan_payload.get("questions", [])
                if q.get("status") not in (None, "", "unanswered")
            ]
            if answered:
                parts.append("计划问题状态与结论：" + json.dumps(answered, ensure_ascii=False))
        digest = "\n".join(parts)
        if len(digest) > self._JUDGE_DIGEST_MAX_CHARS:
            digest = digest[: self._JUDGE_DIGEST_MAX_CHARS] + "\n…（digest 截断）"
        return digest

    def _emit(self, type_: str, payload: dict) -> None:
        self._events.append(Event(run_id=self._manifest.run_id, type=type_, payload=payload))


def _worker_discipline(*, question_driven: bool, has_document_reader: str | bool) -> str:
    """worker 尾部生产纪律（tools-plugins 方案 §2「研究策略」P0）。

    旧缺陷：plan 模式下 worker 尾部仍附「先逐字段写入」纪律，会把开放问题
    压回快速填字段，影响深度。现在：分配到问题的 worker 用问题驱动纪律
    （搜索发现 → 重要资料精读 → 证据 → 观测/论断 → 立即交题），
    legacy 补字段模式（无计划/无分配问题）才保留逐字段纪律。

    has_document_reader："document" = read_document 已装配；"edgar" = 仅
    read_edgar_filing；其他/False = 无文档读取工具（提示只引用真实存在的工具）。
    """
    if question_driven:
        # 精读提示只引用实际装配的工具（能力与提示同源，不引导模型调不存在的工具）
        if has_document_reader == "document":
            read_hint = "用 read_document/search_document 按页精读原文"
        elif has_document_reader == "edgar":
            read_hint = "用 read_edgar_filing 抓正文并定位原文窗口"
        else:
            read_hint = "用 read_chunk 取回完整正文"
        return (
            "\n\n生产纪律（问题驱动，防囤证据空转）：按问题逐个推进——每题：搜索发现 →"
            f" 重要资料精读（{read_hint}，搜索摘录不足以支撑结论）→ 登记证据 →"
            " propose_metric/propose_claim → 完成一题立即 answer_question；"
            "禁止连续登记超过 3 条证据而不产出观测/论断/答案；"
            "查不到就标 unavailable 并记录 attempts，不烧预算空转。"
            "answer_question 只需 conclusion + 已登记证据 refs：能答就先交答案，"
            "结构化观测可随后补；propose_metric 被拒时按拒绝提示的修法示例重试一次，"
            "仍失败就先交答案再补观测，不要放弃提交。"
            "旧字段（propose_fact）只在回答问题的顺带产出时写，不为刷字段完整度消耗预算。"
        )
    # legacy 补字段模式（2026-09-01 实测 flash worker 囤证据空转：124 次登记 0 次写入）
    return (
        "\n\n生产纪律（防囤证据空转）：按字段逐个推进——每字段：搜索 → 登记 1-2 条关键证据"
        " → 立即 propose_fact；禁止连续登记超过 3 条证据而不写事实；"
        "本组字段写完才准碰可选维度；写不出就留白，换下一个字段。"
    )


def _question_stall_suggestions(
    causes: list[str], not_dispatched: list[str], answer_rejections: list[dict],
) -> list[str]:
    """问题零推进的可执行建议（规则化，不依赖 LLM）。"""
    out: list[str] = []
    if "not_dispatched" in causes:
        out.append(
            f"调度装配缺陷：问题 {not_dispatched} 未下发给任何 worker"
            "（检查 scheduling.worker_group 映射与计划 module 字段）"
        )
    if "submit_rejected" in causes:
        reasons = sorted({str(r.get("reason", ""))[:80] for r in answer_rejections})
        out.append(
            "answer_question 被门禁拒：" + "；".join(reasons[:3])
            + "（answered 需 conclusion + 可解析 support_refs；查不到就标 unavailable 并记 attempts）"
        )
    if "source_unavailable" in causes:
        out.append(
            "本轮零证据登记：检查数据源可用性与检索预算（gateway 预检/源降级/预算耗尽）"
        )
    if "hoarding_evidence" in causes:
        out.append(
            "囤证据空转：证据已登记但未形成观测/论断——按题推进，每题至少一条 propose_claim"
        )
    if not out or causes == ["analysis_incomplete"]:
        out.append(
            "分析未完成：有产出但未提交答案——完成一题立即 answer_question，不要攒到轮末"
        )
    return out


#: 定性维度字段（当前数据源覆盖薄弱的那批——EDGAR/行情撑不起）
_QUALITATIVE_FIELDS = frozenset({"moat", "risks", "management", "catalysts", "counter_evidence", "peers"})


def _stall_suggestions(entity_id: str, missing: list[str], sources: list[str]) -> list[str]:
    """停滞建议（规则化，不依赖 LLM）：按缺口形态指出最可能的数据/工具边界。

    2026-09-10 按当前工具面更新（哨兵基线实测：旧文案指向「待接入 HKEXnews/Exa」，
    两者早已装配——过时建议会把排查引向不存在的缺口）。
    """
    out: list[str] = []
    if re.fullmatch(r"\d{4,5}(\.HK)?", entity_id) or entity_id.upper().endswith(".HK"):
        if "hkex_news" in sources:
            out.append(
                "港股披露用 hkex_news（A 级，全 PDF）：中文 PDF 乱码时（quality=garbled/"
                "needs_ocr）换英文版/HTML 公告或标 unavailable 并记录 attempts，"
                "不要硬引乱码正文"
            )
        else:
            out.append(
                "港股披露不在 SEC EDGAR 覆盖内，且 hkex_news 源未装配"
                "（检查启动时的源注册/预检提示）"
            )
    if set(missing) & _QUALITATIVE_FIELDS:
        if "web_search" in sources or "web_search_tavily" in sources:
            out.append(
                "定性维度（护城河/风险/管理层等）靠 web_search + 文档精读"
                "（fetch_document/read_document/search_document）；检查检索预算与"
                "查询改写，连续低收益就发布 partial 并标明缺口"
            )
        else:
            out.append(
                "定性维度靠 EDGAR/行情源覆盖薄弱，且 web 搜索源未装配"
                "（检查 NOVITA_API_KEY/EXA_API_KEY/TAVILY_API_KEY 与启动提示）"
            )
    if not out:
        out.append("可换查询策略重试，或缩小研究目标范围（objective 指定更具体的缺口字段）")
    return out
