"""基线 A' 新发现整改（F10/F11/F12，docs/sentinel-baseline-A-2026-09-10.md §8）。

F10 量表归一 + 量级离群：千元/'000 原样入库是同库 1000 倍漂移的命门——
    规模词识别扩展（'000 缩写）、写侧硬闸（规模词未换算即拒）、
    离群/同值异键扫描（assessment 披露 + propose_metric 响应预警）。
F11 stalled 提前终止：answer_question 被拒后的修复尝试与高价值精读是
    「可验证探索」，给门禁修复回留有界轮次（不重置预算、不算收敛）。
F12 开放冲突未在预算内裁决：交题纪律前置——数值结论交题前查 list_conflicts。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.errors import KnowledgeInvariantError
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation
from finance_agent.knowledge.models import Evidence, PitGrade
from finance_agent.knowledge.normalization import detect_scale_word, normalize_raw
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.research.assessment import numeric_consistency_scan
from finance_agent.research.prompts import PLAN_MODE_CONTRACT

T0 = datetime(2024, 3, 1, tzinfo=UTC)
T1 = datetime(2024, 6, 1, tzinfo=UTC)
FY2023 = MetricPeriod(start=date(2023, 1, 1), end=date(2023, 12, 31),
                      frequency="FY", fiscal_label="FY2023")


# ---------------- F10：规模词识别与归一 ----------------


class TestScaleWordDetection:
    def test_apostrophe_thousands_header(self):
        assert detect_scale_word("802,623", "RMB'000") == "thousand"
        assert detect_scale_word("802,623", "US$’000") == "thousand"
        assert detect_scale_word("1,204", "'000s") == "thousand"

    def test_chinese_thousand_unit(self):
        assert detect_scale_word("802,623", "人民币千元") == "千"
        assert detect_scale_word("802,623", "千元") == "千"

    def test_plain_numbers_no_false_positive(self):
        assert detect_scale_word("3,000", "units") is None
        assert detect_scale_word("802,623", "CNY") is None

    def test_normalize_apostrophe_thousands_to_base(self):
        value, steps = normalize_raw("802,623", "RMB'000")
        assert value == "802623000"
        assert steps[0].formula_id == "unit_word_scale" and steps[0].params["word"] == "thousand"


# ---------------- F10：写侧硬闸 + 离群披露 ----------------


def make_env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    kb.add_evidence(Evidence(
        evidence_id="ev-th", source_id="hkex_news", url="https://hkex/f.pdf",
        verbatim_quote="收入 802,623", retrieved_at=T1, available_at=T0, pit_grade=PitGrade.A,
    ))
    return kb, metrics, events, mw


def thousands_obs(*, normalized: bool, dims: dict | None = None,
                  unit_text: str = "", header: str = "人民币千元") -> ReportedObservation:
    """2228 事故形态：表头声明千元、单元格 802,623。normalized=False 复现漏乘。"""
    if normalized:
        value, steps = normalize_raw("802,623", header or unit_text)
    else:
        value, steps = "802623", []
    return ReportedObservation(
        entity_kind="stock", entity_id="2228.HK", metric_key="revenue", period=FY2023,
        value=value, unit="CNY", currency="CNY", basis="IFRS",
        dimensions=dims or {},
        raw=RawValue(value_text="802,623", unit_text=unit_text, quote_ref="ev-th"),
        locator={"header": header, "page": "242", "table": "综合损益表"},
        normalization=[s.model_dump(mode="json") for s in steps],
        evidence_refs=["ev-th"], knowledge_time=T0, source_available_at=T0,
        retrieved_at=T1, created_at=T1, pit_grade=PitGrade.A,
    )


class TestScaleGate:
    def test_unconverted_scale_word_rejected(self, tmp_path, caplog):
        """规模词在原文/表头出现而换算链没有对应步骤 → 拒写并给修法（F10 硬闸）。"""
        kb, metrics, events, mw = make_env(tmp_path)
        obs = thousands_obs(normalized=False)
        with (
            caplog.at_level(logging.WARNING, logger="finance_agent.knowledge.metrics"),
            pytest.raises(KnowledgeInvariantError, match="规模词"),
        ):
            mw.write_observation(obs, run=RunManifest(run_id="r1", mode=RunMode.LIVE))
        # 失败三通道：事件 + 日志 + 不落库
        verdicts = [e for e in events.read("r1") if e.type == "hook/verdict"]
        assert verdicts and verdicts[0].payload["hook"] == "typed-metric-gate"
        assert "typed 观测拒写" in caplog.text
        assert metrics.observations_as_of("stock", "2228.HK", T1) == []

    def test_converted_same_observation_passes(self, tmp_path):
        kb, metrics, events, mw = make_env(tmp_path)
        obs = thousands_obs(normalized=True)
        assert obs.value == "802623000"
        oid, created = mw.write_observation(
            obs, run=RunManifest(run_id="r1", mode=RunMode.LIVE))
        assert created and oid.startswith("obs-")

    def test_apostrophe_unit_text_also_gated(self, tmp_path):
        """unit_text=RMB'000 而值未换算：同样拒写（不是只有表头路径）。"""
        kb, metrics, events, mw = make_env(tmp_path)
        obs = thousands_obs(normalized=False, unit_text="RMB'000", header="")
        with pytest.raises(KnowledgeInvariantError, match="规模词"):
            mw.write_observation(obs, run=RunManifest(run_id="r1", mode=RunMode.LIVE))


class TestNumericConsistencyScan:
    def _obs(self, *, oid: str, value: str, dims: dict, metric_key: str = "revenue",
             currency: str = "CNY"):
        return SimpleNamespace(
            observation_id=oid, metric_key=metric_key, value=value, unit=currency,
            currency=currency, basis="IFRS", nature="reported", dimensions=dims,
            pit_grade="A", evidence_refs=[], status="ok",
            period=SimpleNamespace(end=date(2023, 12, 31), frequency="FY"),
            raw=SimpleNamespace(unit_text=""),
        )

    def test_scale_suspect_pair_detected(self):
        obs = [
            self._obs(oid="obs-a", value="802623000", dims={}),
            self._obs(oid="obs-b", value="802623", dims={"segment": "platform"}),
        ]
        scan = numeric_consistency_scan(obs)
        assert scan["scale_suspect_total"] == 1
        pair = scan["scale_suspect_pairs"][0]
        assert {pair["a"]["observation_id"], pair["b"]["observation_id"]} == {"obs-a", "obs-b"}

    def test_same_value_different_dims_flagged(self):
        obs = [
            self._obs(oid="obs-a", value="802623000", dims={}),
            self._obs(oid="obs-b", value="802623000", dims={"segment": "dup"}),
        ]
        scan = numeric_consistency_scan(obs)
        assert scan["scale_suspect_total"] == 0
        assert len(scan["same_value_different_dims"]) == 1

    def test_legit_different_values_not_flagged(self):
        obs = [
            self._obs(oid="obs-a", value="802623000", dims={}),
            self._obs(oid="obs-b", value="120000000", dims={"segment": "us"}),
        ]
        scan = numeric_consistency_scan(obs)
        assert scan["scale_suspect_total"] == 0
        assert scan["same_value_different_dims"] == []

    def test_assessment_discloses_scale_suspects(self):
        from finance_agent.research.assessment import assess
        from finance_agent.research.plan import Budgets, ResearchPlan

        plan = ResearchPlan(
            plan_id="plan-f10", entity_kind="stock", entity_id="2228.HK", objective="o",
            mode="targeted", questions=[], budgets=Budgets(), created_at=T0,
        )
        obs = [
            self._obs(oid="obs-a", value="802623000", dims={}),
            self._obs(oid="obs-b", value="802623", dims={"segment": "platform"}),
        ]
        a = assess(plan, claims=[], observations=obs, calculations=[])
        assert a.numeric_consistency["scale_suspect_total"] == 1
        assert any("量表离群" in n for n in a.notes)


# ---------------- F11：可验证探索的有界修复回环 ----------------


class TestExplorationRounds:
    def test_rejected_answers_grant_bounded_rounds(self, tmp_path):
        """交题被拒的轮次不立即 stalled：给 2 轮修复回环，仍无产出才停。"""
        from finance_agent.gateway.adapters.fixture import FixtureAdapter
        from finance_agent.gateway.gateway import DataGateway
        from finance_agent.gateway.models import SourceCapability
        from finance_agent.knowledge.writer import ProfileWriter
        from finance_agent.llm.base import AssistantReply, ToolCall
        from finance_agent.llm.mock import MockLLM
        from finance_agent.research.loop import ResearchLoop
        from finance_agent.research.plan import Budgets, ResearchPlan, ResearchQuestion

        kb = BitemporalStore(tmp_path / "kb.db")
        metrics = MetricStore(tmp_path / "m.db")
        events = EventStore(tmp_path / "e.db")
        writer = ProfileWriter(store=kb, events=events)
        mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
        gateway = DataGateway(mode="live", events=events, run_id="live-f11")
        gateway.register(FixtureAdapter(
            SourceCapability(source_id="demo", pit_grade=PitGrade.A,
                             server_side_asof=False, description="夹具源"),
            records=[],
        ))
        plan = ResearchPlan(
            plan_id="plan-f11", entity_kind="stock", entity_id="BE", objective="o",
            mode="targeted",
            questions=[ResearchQuestion(
                question_id="q1", text="订单口径？", priority="high", module="business",
            )],
            budgets=Budgets(max_rounds=6, question_coverage_target=0.8),
            created_at=T0,
        )
        metrics.save_plan(plan_id=plan.plan_id, namespace="prod",
                          payload=plan.model_dump(mode="json"))

        def rejected_round(i: int) -> AssistantReply:
            # 未知 question_id → answer_question 服务端拒绝（修复尝试 = 可验证探索）
            return AssistantReply(content="", tool_calls=[ToolCall(
                call_id=f"c{i}", name="answer_question",
                arguments={"question_id": f"q-ghost-{i}", "status": "gathering"},
            )])

        llm = MockLLM([rejected_round(0), AssistantReply(content="r1"),
                       rejected_round(1), AssistantReply(content="r2"),
                       rejected_round(2), AssistantReply(content="r3")])
        loop = ResearchLoop(
            store=kb, events=events, writer=writer, gateway=gateway, llm=llm,
            manifest=RunManifest(run_id="live-f11", mode=RunMode.LIVE),
            completeness_target=0.8, gateway_sources=gateway.source_ids(),
            max_rounds=6, plan_id=plan.plan_id, metrics=metrics, metric_writer=mw,
        )
        reports = loop.run("stock", "BE", "o")
        assert loop.stop_reason == "stalled"
        # 2 轮修复回环 + 第 3 轮 stalled：不再是「首轮被拒即停」
        assert len(reports) == 3
        assert reports[0].exploration_only is True
        assert reports[0].answer_rejections == 1 and reports[0].progress is False
        # stalled 诊断指向修复回环耗尽与具体拒绝原因
        diag = loop.stall_diagnostic or {}
        assert diag.get("exploration_exhausted", {}).get("rounds_granted") == 2
        assert any("门禁修复回环已耗尽" in s for s in diag["suggestions"])

    def test_gated_metric_submissions_grant_rounds(self, tmp_path):
        """基线实际形态：propose_metric 被门禁拦下（带修法提示）的轮次 =
        可验证探索，给修复回环轮次；不再是「2 轮零写入即 stalled」。"""
        from finance_agent.gateway.adapters.fixture import FixtureAdapter
        from finance_agent.gateway.gateway import DataGateway
        from finance_agent.gateway.models import DataRecord, SourceCapability
        from finance_agent.knowledge.writer import ProfileWriter
        from finance_agent.llm.base import AssistantReply, ToolCall
        from finance_agent.llm.mock import MockLLM
        from finance_agent.research.loop import ResearchLoop

        kb = BitemporalStore(tmp_path / "kb.db")
        metrics = MetricStore(tmp_path / "m.db")
        events = EventStore(tmp_path / "e.db")
        writer = ProfileWriter(store=kb, events=events)
        mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
        gateway = DataGateway(mode="live", events=events, run_id="live-f11c")
        gateway.register(FixtureAdapter(
            SourceCapability(source_id="demo", pit_grade=PitGrade.A,
                             server_side_asof=False, description="夹具源"),
            records=[DataRecord(source_id="demo", payload={"title": "backlog 300 million"},
                                url="demo://x", available_at=T0)],
        ))

        def gated_round(i: int) -> list[AssistantReply]:
            return [
                AssistantReply(content="", tool_calls=[
                    ToolCall(call_id=f"q{i}", name="query_demo", arguments={}),
                    ToolCall(call_id=f"m{i}", name="propose_metric", arguments={
                        "metric_key": "revenue", "value_text": "999",
                        "unit": "USD", "currency": "USD",
                        "period": {"start": "2023-01-01", "end": "2023-12-31",
                                   "frequency": "FY"},
                        "evidence_ids": ["ev-not-registered"],
                    }),
                ]),
                AssistantReply(content=f"round {i} no luck"),
            ]

        llm = MockLLM([*gated_round(1), *gated_round(2), *gated_round(3)])
        # 探索回环仅限 plan 模式（F11）：装配冻结计划
        from finance_agent.research.plan import Budgets, ResearchPlan, ResearchQuestion

        plan = ResearchPlan(
            plan_id="plan-f11c", entity_kind="stock", entity_id="BE", objective="o",
            mode="targeted",
            questions=[ResearchQuestion(
                question_id="q1", text="收入？", priority="high", module="financial_quality",
            )],
            budgets=Budgets(max_rounds=6, question_coverage_target=0.8),
            created_at=T0,
        )
        metrics.save_plan(plan_id=plan.plan_id, namespace="prod",
                          payload=plan.model_dump(mode="json"))
        loop = ResearchLoop(
            store=kb, events=events, writer=writer, gateway=gateway, llm=llm,
            manifest=RunManifest(run_id="live-f11c", mode=RunMode.LIVE),
            completeness_target=0.8, gateway_sources=gateway.source_ids(), max_rounds=6,
            plan_id=plan.plan_id, metrics=metrics, metric_writer=mw,
        )
        reports = loop.run("stock", "BE", "o")
        assert loop.stop_reason == "stalled"
        assert len(reports) == 3, "提交被拒的前两轮计入修复回环，第三轮才 stalled"
        assert reports[0].exploration_only is True and reports[0].progress is False
        # 拒绝必须是提交类（metric）而非囤证据类
        assert any("metric" in r for r in reports[0].rejected)

    def test_pure_hoarding_still_stalls_immediately(self, tmp_path):
        """囤证据（无交题尝试、精读 <3）不算探索：旧 stalled 行为不变。"""
        from finance_agent.gateway.adapters.fixture import FixtureAdapter
        from finance_agent.gateway.gateway import DataGateway
        from finance_agent.gateway.models import DataRecord, SourceCapability
        from finance_agent.knowledge.writer import ProfileWriter
        from finance_agent.llm.base import AssistantReply, ToolCall
        from finance_agent.llm.mock import MockLLM
        from finance_agent.research.loop import ResearchLoop

        kb = BitemporalStore(tmp_path / "kb.db")
        events = EventStore(tmp_path / "e.db")
        writer = ProfileWriter(store=kb, events=events)
        gateway = DataGateway(mode="live", events=events, run_id="live-f11b")
        gateway.register(FixtureAdapter(
            SourceCapability(source_id="demo", pit_grade=PitGrade.A,
                             server_side_asof=False, description="夹具源"),
            records=[DataRecord(source_id="demo", payload={"title": "x"},
                                url="demo://x", available_at=T0)],
        ))
        llm = MockLLM([
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c0", name="query_demo", arguments={})]),
            AssistantReply(content="nothing written"),
        ])
        loop = ResearchLoop(
            store=kb, events=events, writer=writer, gateway=gateway, llm=llm,
            manifest=RunManifest(run_id="live-f11b", mode=RunMode.LIVE),
            completeness_target=0.8, gateway_sources=gateway.source_ids(), max_rounds=4,
        )
        reports = loop.run("stock", "BE", "o")
        assert loop.stop_reason == "stalled"
        assert len(reports) == 1, "仅检索登记、无交题尝试/精读 → 首轮即 stalled（防囤证据）"
        assert reports[0].exploration_only is False

    def test_close_reads_count_as_exploration(self, tmp_path):
        """文档精读 ≥3 次也是可验证探索（读原文不是空转）。"""
        from finance_agent.research.tools import make_research_tools

        kb = BitemporalStore(tmp_path / "kb.db")
        from finance_agent.research.evidence_desk import ChunkStore

        pages = {1: "page one text", 2: "page two text", 3: "page three text"}

        def fake_paged(url: str):
            from finance_agent.gateway.fetch import FetchedDocument
            from finance_agent.gateway.text_quality import TextQuality

            return FetchedDocument(kind="text", url=url,
                                   page_texts=tuple(pages.items()), total_pages=3,
                                   quality=TextQuality("ok"))

        tools, tracker = make_research_tools(
            store=kb, writer=None,  # type: ignore[arg-type]
            manifest=RunManifest(run_id="live-cr", mode=RunMode.LIVE),
            entity_kind="stock", entity_id="BE", chunk_store=ChunkStore(),
            fetch_paged=fake_paged,
        )
        doc = json.loads(tools["fetch_document"]({"url": "https://x.example/a"})["content"])
        doc_id = doc["document_id"]
        assert tracker.close_reads == [f"{doc_id}#fetch"]
        tools["read_document"]({"document_id": doc_id, "page": 2})
        tools["search_document"]({"document_id": doc_id, "query": "three"})
        assert len(tracker.close_reads) == 3


# ---------------- F12：交题前查冲突的纪律进契约 ----------------


class TestConflictDisciplineInContract:
    def test_plan_contract_requires_conflict_check_before_numeric_answer(self):
        assert "list_conflicts" in PLAN_MODE_CONTRACT
        assert "数值类结论交题前" in PLAN_MODE_CONTRACT
        assert "adjudicate_conflict" in PLAN_MODE_CONTRACT
        assert "不得留着竞争值交无条件答案" in PLAN_MODE_CONTRACT
