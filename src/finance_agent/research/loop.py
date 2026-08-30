"""ResearchLoop：轮次制迭代研究（S1，DESIGN.md §5.1）。

一轮 = gap 分析 → LLM turn（工具：登记证据/写事实/查档案/查数据）
     → 过程评估（coverage 软反馈 + writer 硬门禁）→ IterationReport。
收敛：完整度达标；停滞：一轮无成功写入；预算：max_rounds。
取消：should_stop 在轮次边界被检查（stop_command 的落点），已落库事实保留。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from ..eventstore.events import (
    CONTEXT_INJECT,
    RESEARCH_ROUND_END,
    RESEARCH_ROUND_START,
    RESEARCH_RUBRIC,
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
from .prompts import GROUNDING_CONTRACT, build_round_brief
from .report import IterationReport
from .tools import make_research_tools


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
        max_rounds: int = 3,
        completeness_target: float = 0.8,
        namespace: str = "prod",
        gateway_sources: list[str] | None = None,
        max_steps_per_round: int = 16,
        hooks: list[Hook] | None = None,
        judge_llm: LLM | None = None,  # research-rubric 软反馈（advisory，D4）
        should_stop: Callable[[], bool] | None = None,  # 取消闸（轮次边界检查）
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
        self.stop_reason: str | None = None

    def run(
        self,
        entity_kind: str,
        entity_id: str,
        objective: str,
        *,
        now: datetime | None = None,
    ) -> list[IterationReport]:
        analyzer = GapAnalyzer(self._store)
        reports: list[IterationReport] = []
        # 评估时刻：显式传入（评估回放）则固定；否则每次 gap 分析取当前真实时间，
        # 避免 run 内新写入的事实因 knowledge_time 晚于「起跑线时刻」而不可见。
        fixed_now = now
        judge_feedback: str | None = None  # 上一轮 rubric 的软反馈

        def _now() -> datetime:
            return fixed_now or datetime.now(UTC)

        for round_no in range(1, self._max_rounds + 1):
            if self._should_stop is not None and self._should_stop():
                self.stop_reason = "cancelled"
                break
            gaps_before = analyzer.analyze(entity_kind, entity_id, _now(), namespace=self._namespace)
            if gaps_before.completeness >= self._target:
                self.stop_reason = "converged"
                break
            self._emit(RESEARCH_ROUND_START, {"round": round_no, "gaps": gaps_before.model_dump(mode="json")})

            tools, tracker = make_research_tools(
                store=self._store,
                writer=self._writer,
                manifest=self._manifest,
                entity_kind=entity_kind,
                entity_id=entity_id,
                namespace=self._namespace,
            )
            for source_id in self._gateway_sources:
                tools[f"query_{source_id}"] = make_gateway_tool(self._gateway, source_id)

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

            gaps_after = analyzer.analyze(entity_kind, entity_id, _now(), namespace=self._namespace)
            report = IterationReport(
                run_id=self._manifest.run_id,
                round=round_no,
                entity=f"{entity_kind}:{entity_id}",
                completeness_before=gaps_before.completeness,
                completeness_after=gaps_after.completeness,
                facts_written=list(tracker.written),
                rejected=list(tracker.rejected),
                missing_after=list(gaps_after.missing),
                progress=bool(tracker.written),
            )
            self._emit(RESEARCH_ROUND_END, report.model_dump(mode="json"))
            reports.append(report)

            judge_feedback = self._judge_round(report)

            if gaps_after.completeness >= self._target:
                self.stop_reason = "converged"
                break
            if not report.progress:
                self.stop_reason = "stalled"
                break
        else:
            self.stop_reason = "budget"

        return reports

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
