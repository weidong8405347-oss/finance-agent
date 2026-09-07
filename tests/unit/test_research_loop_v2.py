"""问题驱动研究循环验收（设计 §7/§13.1「研究」组）。

关键场景：档案 100% + 新目标仍研究、问题覆盖决定终止、typed 产出防 stalled 误判、
answer_question/propose_metric 服务端门禁、终局评估事件（硬门禁代码运行）。
"""

from datetime import UTC, datetime

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.adapters.fixture import FixtureAdapter
from finance_agent.gateway.gateway import DataGateway
from finance_agent.gateway.models import DataRecord, SourceCapability
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.schema import STOCK_SCHEMA
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.research.calculations import CalculationService
from finance_agent.research.loop import ResearchLoop
from finance_agent.research.plan import Budgets, ResearchPlan, ResearchQuestion

NOW = datetime.now(UTC)
DOC_TEXT = "Total revenue 1500 million for fiscal 2024. Firm backlog of 300 million announced."


def tc(i: int, name: str, args: dict) -> ToolCall:
    return ToolCall(call_id=f"c{i}", name=name, arguments=args)


@pytest.fixture()
def env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    calcs = CalculationService(metrics, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-loop")
    gateway.register(FixtureAdapter(
        SourceCapability(source_id="demo", pit_grade=PitGrade.A,
                         server_side_asof=False, description="夹具源"),
        records=[DataRecord(source_id="demo", payload={"form": "10-K", "title": "FY2024"},
                            url="demo://filing", available_at=NOW)],
    ))
    return kb, metrics, events, writer, mw, calcs, gateway


def make_loop(env, llm, *, plan_id=None, completeness_target=0.8, max_rounds=3):
    kb, metrics, events, writer, mw, calcs, gateway = env
    return ResearchLoop(
        store=kb, events=events, writer=writer, gateway=gateway, llm=llm,
        manifest=RunManifest(run_id="live-loop", mode=RunMode.LIVE),
        completeness_target=completeness_target,
        gateway_sources=gateway.source_ids(),
        fetch_document=lambda url: DOC_TEXT,
        max_rounds=max_rounds,
        plan_id=plan_id, metrics=metrics, metric_writer=mw, calculations=calcs,
    )


def save_plan(metrics, questions, *, plan_id="plan-t1", mode="targeted", target=0.8,
              max_rounds=3):
    plan = ResearchPlan(
        plan_id=plan_id, entity_kind="stock", entity_id="BE", objective="订单与收入验证",
        mode=mode, questions=questions,
        budgets=Budgets(max_rounds=max_rounds, question_coverage_target=target),
        created_at=NOW,
    )
    metrics.save_plan(plan_id=plan_id, namespace="prod", payload=plan.model_dump(mode="json"))
    return plan


def seed_full_profile(kb):
    kb.add_evidence(Evidence(
        evidence_id="ev-f", source_id="edgar", verbatim_quote="complete profile evidence",
        retrieved_at=NOW, available_at=NOW, pit_grade=PitGrade.A,
    ))
    for field in STOCK_SCHEMA.required:
        kb.assert_fact(Fact(
            entity_kind="stock", entity_id="BE", field=field,
            value=f"{field}：有据可查的描述内容", knowledge_time=NOW, evidence_ids=["ev-f"],
        ))


#: 完整 typed 流程脚本：检索 → 读正文 → 登记证据 → typed 观测 → 论断 → 回答问题
TYPED_FLOW = [
    AssistantReply(content="", tool_calls=[tc(0, "query_demo", {})]),
    AssistantReply(content="", tool_calls=[tc(1, "read_edgar_filing",
                                              {"chunk_id": "chk-0001", "query": "revenue"})]),
    AssistantReply(content="", tool_calls=[tc(2, "register_evidence", {
        "chunk_id": "chk-0002", "evidence_id": "ev-t1",
        "verbatim_quote": "Total revenue 1500 million for fiscal 2024"})]),
    AssistantReply(content="", tool_calls=[tc(3, "propose_metric", {
        "metric_key": "revenue", "value_text": "1500 million", "unit": "USD",
        "unit_text": "USD", "currency": "USD",
        "period": {"start": "2024-01-01", "end": "2024-12-31",
                   "frequency": "FY", "fiscal_label": "FY2024"},
        "evidence_ids": ["ev-t1"],
    })]),
    AssistantReply(content="", tool_calls=[tc(4, "propose_claim", {
        "statement": "FY2024 收入 15 亿美元，增长由订单交付驱动",
        "kind": "inference", "question_id": "q-backlog", "support_refs": ["ev-t1"],
    })]),
    AssistantReply(content="", tool_calls=[tc(5, "answer_question", {
        "question_id": "q-backlog", "status": "answered",
        "conclusion": "订单按计划转化，FY2024 收入 1500 million 有披露支持",
        "support_refs": ["ev-t1"],
    })]),
    AssistantReply(content="round done"),
]


class TestPlanDrivenTermination:
    def test_full_profile_without_plan_skips(self, env):
        """旧行为兼容：无计划时满档案直接 converged（0 轮）。"""
        kb, metrics, *_ = env
        seed_full_profile(kb)
        loop = make_loop(env, MockLLM([]))
        reports = loop.run("stock", "BE", "研究")
        assert reports == [] and loop.stop_reason == "converged"

    def test_full_profile_with_new_objective_still_researches(self, env):
        """已有 100% 档案遇到新目标仍创建计划研究（§7.1，不宣告「无需研究」）。"""
        kb, metrics, *_ = env
        seed_full_profile(kb)
        save_plan(metrics, [ResearchQuestion(
            question_id="q-new", text="数据中心订单的交付确定性如何？", priority="high",
            acceptance="结论有直接证据",
        )])
        script = [
            AssistantReply(content="", tool_calls=[tc(0, "answer_question", {
                "question_id": "q-new", "status": "answered",
                "conclusion": "交付计划有 backlog 披露支持", "support_refs": ["ev-f"],
            })]),
            AssistantReply(content="done"),
        ]
        loop = make_loop(env, MockLLM(script), plan_id="plan-t1")
        reports = loop.run("stock", "BE", "评估数据中心订单转化")
        assert len(reports) == 1  # 满档案仍跑了一轮
        assert loop.stop_reason == "converged"
        plan = metrics.get_plan("plan-t1")
        q = plan["questions"][0]
        assert q["status"] == "answered" and q["conclusion"]

    def test_coverage_target_terminates_loop(self, env):
        kb, metrics, events, *_ = env
        save_plan(metrics, [ResearchQuestion(
            question_id="q-backlog", text="订单能否按计划转为收入和现金？", priority="high",
            acceptance="结论有直接证据", computable_checks=["yoy_growth"],
        )])
        loop = make_loop(env, MockLLM(list(TYPED_FLOW)), plan_id="plan-t1",
                         completeness_target=0.0)
        reports = loop.run("stock", "BE", "验证订单转化")
        assert loop.stop_reason == "converged"  # 问题覆盖达标即终止（字段完整度不再独裁）
        assert len(reports) == 1
        assert reports[0].question_coverage == pytest.approx(1.0)
        # 计划创建/问题进展/评估事件齐备（Sessions 投影数据源）
        types = [e.type for e in events.read("live-loop")]
        assert "research/plan_created" in types
        assert "research/question_updated" in types
        assert "research/assessment" in types

    def test_unanswered_question_keeps_loop_running(self, env):
        """覆盖不足 → 不因字段达标而提前收敛；预算耗尽 stop_reason=budget。"""
        kb, metrics, events, *_ = env
        seed_full_profile(kb)
        save_plan(metrics, [
            ResearchQuestion(question_id="q-a", text="问题A", priority="high"),
            ResearchQuestion(question_id="q-b", text="问题B", priority="high"),
        ], target=1.0, max_rounds=2)
        script = [
            AssistantReply(content="", tool_calls=[tc(0, "answer_question", {
                "question_id": "q-a", "status": "answered",
                "conclusion": "A 有结论", "support_refs": ["ev-f"]})]),
            AssistantReply(content="r1 done"),
            AssistantReply(content="", tool_calls=[tc(1, "answer_question", {
                "question_id": "q-b", "status": "gathering"})]),
            AssistantReply(content="r2 done"),
        ]
        loop = make_loop(env, MockLLM(script), plan_id="plan-t1", max_rounds=2)
        reports = loop.run("stock", "BE", "两问题研究")
        assert len(reports) == 2 and loop.stop_reason == "budget"
        assessment = loop.assessment
        assert assessment is not None and assessment.verdict == "partial"
        # 关键问题仍 gathering → violations 可见（不悄悄遗漏）
        assert any("q-b" in v for v in assessment.question_coverage.violations)


class TestTypedProgress:
    def test_typed_only_output_is_progress_not_stalled(self, env):
        """有价值分析但没写字段 ≠ stalled（§7.7 进展定义扩展）。"""
        kb, metrics, events, *_ = env
        save_plan(metrics, [ResearchQuestion(
            question_id="q-backlog", text="订单转化？", priority="high")])
        # round1 只写观测/论断/问题（不 propose_fact）；round2 无产出 → stalled 在 round2
        script = [*TYPED_FLOW, AssistantReply(content="no new findings")]
        loop = make_loop(env, MockLLM(script), plan_id="plan-t1",
                         completeness_target=1.1)  # 字段完整度永不达标 → 只看进展/覆盖
        reports = loop.run("stock", "BE", "验证")
        # round1 已回答问题 → 覆盖 1.0 ≥ 0.8，但 completeness 0 < 1.1 → 继续 round2
        assert reports[0].progress is True
        assert reports[0].observations_written == ["revenue"]
        assert reports[0].claims_written and reports[0].questions_advanced == ["q-backlog"]
        assert len(reports) == 2 and loop.stop_reason == "stalled"

    def test_metric_written_and_event_has_full_payload(self, env):
        kb, metrics, events, *_ = env
        save_plan(metrics, [ResearchQuestion(question_id="q-backlog", text="?", priority="high")])
        loop = make_loop(env, MockLLM(list(TYPED_FLOW)), plan_id="plan-t1",
                         completeness_target=0.0)
        loop.run("stock", "BE", "验证")
        obs = metrics.observations_as_of("stock", "BE", datetime.now(UTC), metric_key="revenue")
        assert len(obs) == 1 and obs[0].value == "1500000000"
        assert obs[0].raw is not None and obs[0].raw.value_text == "1500 million"
        asserted = [e for e in events.read("live-loop") if e.type == "metric/asserted"]
        assert asserted
        payload = asserted[0].payload["observation"]
        # 事件携带完整不可变 payload（重建依据，§6.5）
        assert payload["value"] == "1500000000" and payload["normalization"]

    def test_claim_validated_event(self, env):
        kb, metrics, events, *_ = env
        save_plan(metrics, [ResearchQuestion(question_id="q-backlog", text="?", priority="high")])
        loop = make_loop(env, MockLLM(list(TYPED_FLOW)), plan_id="plan-t1",
                         completeness_target=0.0)
        loop.run("stock", "BE", "验证")
        validated = [e for e in events.read("live-loop") if e.type == "research/claim_validated"]
        assert validated  # support_refs 全部可解析 → validated
        claims = metrics.claims_as_of("stock", "BE", datetime.now(UTC))
        assert claims and claims[0]["status"] == "validated"


class TestToolGates:
    def test_answer_question_gate_requires_evidence(self, env):
        """answered 无结论/引用 → 拒；unavailable 无尝试记录 → 拒（§7.6 硬规则）。"""
        kb, metrics, events, *_ = env
        save_plan(metrics, [ResearchQuestion(question_id="q-g", text="?", priority="high")])
        script = [
            AssistantReply(content="", tool_calls=[tc(0, "answer_question", {
                "question_id": "q-g", "status": "answered", "conclusion": "拍脑袋结论"})]),
            AssistantReply(content="", tool_calls=[tc(1, "answer_question", {
                "question_id": "q-g", "status": "unavailable"})]),
            AssistantReply(content="", tool_calls=[tc(2, "answer_question", {
                "question_id": "q-g", "status": "unavailable",
                "unresolved": ["backlog 未披露"], "attempts": ["查了 FY2024 10-K 与电话会"]})]),
            AssistantReply(content="done"),
            AssistantReply(content="nothing more"),  # round2 无进展 → stalled 终止
        ]
        loop = make_loop(env, MockLLM(script), plan_id="plan-t1", completeness_target=0.0)
        loop.run("stock", "BE", "门禁验证")
        q = metrics.get_plan("plan-t1")["questions"][0]
        assert q["status"] == "unavailable" and q["attempts"]  # 前两次被拒，第三次合规落地
        updated = [e for e in events.read("live-loop") if e.type == "research/question_updated"]
        assert len(updated) == 1  # 被拒调用不产生事件

    def test_propose_metric_rejects_unquoted_number(self, env):
        """自编数字防线：value_text 数字未在摘录逐字出现 → 拒。"""
        kb, metrics, events, *_ = env
        save_plan(metrics, [ResearchQuestion(question_id="q-m", text="?", priority="high")])
        script = [
            AssistantReply(content="", tool_calls=[tc(0, "query_demo", {})]),
            AssistantReply(content="", tool_calls=[tc(1, "read_edgar_filing", {
                "chunk_id": "chk-0001", "query": "revenue"})]),
            AssistantReply(content="", tool_calls=[tc(2, "register_evidence", {
                "chunk_id": "chk-0002", "evidence_id": "ev-t1",
                "verbatim_quote": "Total revenue 1500 million for fiscal 2024"})]),
            AssistantReply(content="", tool_calls=[tc(3, "propose_metric", {
                "metric_key": "revenue", "value_text": "9999 million",  # 凭记忆编数
                "period": {"start": "2024-01-01", "end": "2024-12-31", "frequency": "FY"},
                "evidence_ids": ["ev-t1"]})]),
            AssistantReply(content="done"),
        ]
        loop = make_loop(env, MockLLM(script), plan_id="plan-t1", completeness_target=0.0,
                         max_rounds=1)
        reports = loop.run("stock", "BE", "编数防线")
        assert metrics.observations_as_of("stock", "BE", datetime.now(UTC)) == []
        assert any("9999" in r.get("reason", "") for r in reports[0].rejected)

    def test_calculate_metric_with_observation_ref(self, env):
        """计算输入引用已登记观测（可重算契约）；结果落 calculation/completed。"""
        kb, metrics, events, *_ = env
        save_plan(metrics, [ResearchQuestion(question_id="q-backlog", text="?", priority="high")])
        flow = list(TYPED_FLOW)
        # 在 claim 之后插入一个计算调用（用 assumption 输入——obs id 脚本不可知）
        flow.insert(5, AssistantReply(content="", tool_calls=[tc(9, "calculate_metric", {
            "formula_id": "yoy_growth",
            "inputs": [{"kind": "assumption", "label": "current", "value": "1500"},
                       {"kind": "assumption", "label": "prior", "value": "1000"}],
        })]))
        loop = make_loop(env, MockLLM(flow), plan_id="plan-t1", completeness_target=0.0)
        reports = loop.run("stock", "BE", "计算链路")
        assert reports[0].calculations_done
        calc_events = [e for e in events.read("live-loop") if e.type == "calculation/completed"]
        assert calc_events and calc_events[0].payload["result"] == "0.5"
        stored = metrics.get_calculation(reports[0].calculations_done[0])
        assert stored is not None and stored.payload["formula_id"] == "yoy_growth"


class TestAssessmentEmission:
    def test_assessment_event_and_verdict(self, env):
        kb, metrics, events, *_ = env
        save_plan(metrics, [ResearchQuestion(
            question_id="q-backlog", text="订单转化？", priority="high")])
        loop = make_loop(env, MockLLM(list(TYPED_FLOW)), plan_id="plan-t1",
                         completeness_target=0.0)
        loop.run("stock", "BE", "评估验证")
        assessments = [e for e in events.read("live-loop") if e.type == "research/assessment"]
        assert assessments
        payload = assessments[0].payload
        assert payload["verdict"] == "sufficient"
        assert payload["hard_gate_passed"] is True
        assert payload["question_coverage"]["answered"] == 1
        assert loop.assessment is not None
        # 计划状态收尾
        assert metrics.get_plan("plan-t1")["status"] == "completed"

    def test_no_assessment_without_plan(self, env):
        """旧管线兼容：无计划 → 不产评估（行为与升级前一致）。"""
        kb, metrics, events, *_ = env
        seed_full_profile(kb)
        loop = make_loop(env, MockLLM([]))
        loop.run("stock", "BE", "研究")
        assert loop.assessment is None
        assert not [e for e in events.read("live-loop") if e.type == "research/assessment"]
