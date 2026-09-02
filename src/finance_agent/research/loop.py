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
    *, entity_kind: str = "stock",
) -> list[tuple[str, list[str]]]:
    """缺口字段（缺失+陈旧+可选）按维度分组；未登记进组的字段轮转分配。
    注：player_landscape 永不分组（F2 专属产出，见 _DIMENSION_GROUPS_INDUSTRY 注释）。"""
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
    return [(g, fs) for g, fs in groups]

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
        self.stop_reason: str | None = None
        #: stalled 时填充缺口诊断卡（research-capability-upgrade §4.3 L3）
        self.stall_diagnostic: dict | None = None

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

        round_no = 0
        while True:
            if self._should_stop is not None and self._should_stop():
                self.stop_reason = "cancelled"
                break
            round_no += 1
            gaps_before = analyzer.analyze(entity_kind, entity_id, _now(), namespace=self._namespace)
            # 收敛 = 完整度达标 且 无陈旧字段（stale 必须触发刷新研究——
            # 2026-08-30 用户实测：旧档案「完整但过时」被误判「无需研究」）
            if gaps_before.completeness >= self._target and not gaps_before.stale:
                self.stop_reason = "converged"
                break
            if budget is None:
                budget = self._effective_budget(gaps_before)
            if round_no > budget:
                self.stop_reason = "budget"
                break
            self._emit(RESEARCH_ROUND_START, {"round": round_no, "gaps": gaps_before.model_dump(mode="json")})

            # 维度分组并行（P3 §4.2）：配置了 worker 池（>1）才启用——
            # 每组独立 child run / 独立 context / 独立步数预算；共享 ChunkStore（锁保护）。
            # 无池 → 旧式单 kernel 路径（单 LLM 被多组共享会互相抽干脚本，且没有必要隔离）。
            if len(self._worker_llms) > 1:
                groups = _dimension_groups(
                    gaps_before.missing, gaps_before.stale, gaps_before.optional_missing,
                    entity_kind=entity_kind,
                )
                workers = self._worker_llms
                group_results: list[tuple[str, list[str], list[dict]]] = []
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=len(groups)) as pool:
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
                written = [f for _, ws, _ in group_results for f in ws]
                rejected = [r for _, _, rs in group_results for r in rs]
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
                    )
                )
                written, rejected = tracker.written, tracker.rejected

            gaps_after = analyzer.analyze(entity_kind, entity_id, _now(), namespace=self._namespace)
            report = IterationReport(
                run_id=self._manifest.run_id,
                round=round_no,
                entity=f"{entity_kind}:{entity_id}",
                completeness_before=gaps_before.completeness,
                completeness_after=gaps_after.completeness,
                facts_written=written,
                rejected=rejected,
                missing_after=list(gaps_after.missing),
                progress=bool(written),
            )
            self._emit(RESEARCH_ROUND_END, report.model_dump(mode="json"))
            reports.append(report)
            all_rejected.extend(rejected)

            judge_feedback = self._judge_round(report)

            if gaps_after.completeness >= self._target and not gaps_after.stale:
                self.stop_reason = "converged"
                break
            if not report.progress:
                self.stop_reason = "stalled"
                self._emit_stall_diagnostic(
                    entity_kind, entity_id, gaps_after, all_rejected, reports
                )
                break

        return reports

    def _effective_budget(self, gaps) -> int:
        """预算动态化（§4.2）：显式 max_rounds 优先；否则按初始完整度定预算。"""
        if self._max_rounds is not None:
            return self._max_rounds
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
    ) -> tuple[str, list[str], list[dict]]:
        """单个维度组的一轮研究：独立 child run（context 隔离）+ 独立步数预算。"""
        tools, tracker = make_research_tools(
            store=self._store,
            writer=self._writer,
            manifest=self._manifest,
            entity_kind=entity_kind,
            entity_id=entity_id,
            namespace=self._namespace,
            chunk_store=chunk_store,
            fetch_document=self._fetch_document,
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
        group_gaps = gaps_before.model_copy(
            update={"missing": list(fields), "stale": [], "optional_missing": []}
        )
        brief = (
            build_round_brief(
                entity_kind, entity_id, objective, group_gaps, round_no,
                judge_feedback=judge_feedback,
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
                },
            )
        )
        return group, tracker.written, tracker.rejected

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
