"""调度器验收（audit §3.1 P0 + §5 验收用例 1「问题分发装配回放」）。

事故形态（live-a2cce641）：行业旧档案完整度 100% + deep 计划 9 题 →
建组用 module 原名、过滤用旧维度名，两套口径不相交 → 20 个 worker 全部拿到
空计划视图 → 5 轮 576 次工具调用、answer_question 0 次、问题推进 0/9。

本组用例不只分别单测两个映射函数：第 3 节用真实 ResearchLoop + 真实分组逻辑 +
真实行业配方贯穿装配，断言九个问题全部进入某个 worker 的执行上下文。
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
from finance_agent.knowledge.schema import INDUSTRY_SCHEMA
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply
from finance_agent.research.calculations import CalculationService
from finance_agent.research.loop import ResearchLoop
from finance_agent.research.plan import build_plan, load_recipe
from finance_agent.research.scheduling import (
    DispatchError,
    assert_dispatch_complete,
    build_schedule,
    pending_questions,
    plan_view,
    worker_group,
)

NOW = datetime.now(UTC)


# ---------------- 1. 单一映射表：建组与下发同源 ----------------


class TestWorkerGroupMapping:
    def test_industry_modules_map_to_industry_groups(self):
        """行业 module 不许落进股票组（audit：key_kpi → financial 的错配）。"""
        assert worker_group("industry", "industry_chain") == "landscape"
        assert worker_group("industry", "key_kpi") == "market"
        assert worker_group("industry", "candidate_pool") == "candidate_pool"
        assert worker_group("industry", "catalysts_risks") == "policy_players"
        assert worker_group("industry", "investment_snapshot") == "landscape"

    def test_stock_modules_map_to_stock_groups(self):
        assert worker_group("stock", "financial_quality") == "financial"
        assert worker_group("stock", "key_kpi") == "financial"
        assert worker_group("stock", "business_engine") == "business"
        assert worker_group("stock", "risks") == "risk_mgmt"

    def test_unknown_module_is_deterministic_not_dropped(self):
        """未登记模块 → 组名 = 模块本身（确定性；不静默塞进 misc 丢并行度）。"""
        assert worker_group("industry", "brand_new_module") == "brand_new_module"
        assert worker_group("stock", "") == "misc"


# ---------------- 2. 调度产物：完整分配 + 无空问题 worker ----------------


def _plan(questions, *, entity_kind="industry", **kw):
    return {
        "plan_id": "plan-s1", "entity_kind": entity_kind, "entity_id": "ai-for-science",
        "objective": "谁在形成护城河", "mode": "deep", "recipe_id": "industry",
        "recipe_version": "1", "created_at": NOW.isoformat(), "status": "active",
        "questions": questions, "acceptance": "", "budgets": {}, "scope": {}, **kw,
    }


def _q(qid, module, *, status="unanswered", priority="high", acceptance="要有证据"):
    return {"question_id": qid, "text": f"{qid}?", "priority": priority, "status": status,
            "module": module, "acceptance": acceptance}


class TestBuildSchedule:
    def test_all_pending_questions_dispatched(self):
        sched = build_schedule(
            plan_payload=_plan([_q("a", "industry_chain"), _q("b", "key_kpi"),
                                _q("c", "candidate_pool"), _q("d", "catalysts_risks")]),
            entity_kind="industry", field_groups=[],
        )
        assert sorted(sched.dispatched_question_ids) == ["a", "b", "c", "d"]
        assert sched.unassigned == []
        assert_dispatch_complete(sched, _plan([_q("a", "industry_chain")]))

    def test_closed_questions_not_dispatched(self):
        sched = build_schedule(
            plan_payload=_plan([_q("a", "key_kpi"), _q("done", "key_kpi", status="answered"),
                                _q("na", "key_kpi", status="not_applicable")]),
            entity_kind="industry", field_groups=[],
        )
        assert sched.dispatched_question_ids == ["a"]

    def test_field_only_group_folded_when_questions_pending(self):
        """有待执行问题时不允许空问题 worker：字段组折叠进问题组（可见，不静默）。"""
        sched = build_schedule(
            plan_payload=_plan([_q("a", "industry_chain")]),
            entity_kind="industry",
            field_groups=[("policy_players", ["policy"]), ("landscape", ["value_chain"])],
        )
        groups = {it.group for it in sched.items}
        assert "policy_players" not in groups  # 只带字段的组被折叠
        assert all(it.question_ids for it in sched.items)
        policy_item = next(it for it in sched.items if "policy" in it.fields)
        assert policy_item.group == "landscape"
        assert sched.folded and sched.folded[0]["from_group"] == "policy_players"

    def test_field_groups_kept_when_no_pending_questions(self):
        sched = build_schedule(
            plan_payload=_plan([_q("a", "industry_chain", status="answered")]),
            entity_kind="industry", field_groups=[("market", ["market_size"])],
        )
        assert [it.group for it in sched.items] == ["market"]
        assert sched.items[0].fields == ("market_size",)
        assert sched.items[0].question_ids == ()

    def test_global_questions_visible_to_every_worker(self):
        """无模块问题（targeted 自定义目标）对每个 worker 可见，回答幂等。"""
        sched = build_schedule(
            plan_payload=_plan([_q("targeted-1", ""), _q("a", "key_kpi"),
                                _q("b", "industry_chain")]),
            entity_kind="industry", field_groups=[],
        )
        assert sched.global_question_ids == ["targeted-1"]
        assert all("targeted-1" in it.question_ids for it in sched.items)

    def test_worker_cap_never_drops_questions(self):
        sched = build_schedule(
            plan_payload=_plan([_q(f"q{i}", m) for i, m in enumerate(
                ["industry_chain", "key_kpi", "candidate_pool", "catalysts_risks",
                 "research_sources", "investment_snapshot"])]),
            entity_kind="industry", field_groups=[], max_workers=2,
        )
        assert len(sched.items) == 2
        assert sorted(sched.dispatched_question_ids) == [
            "q0", "q1", "q2", "q3", "q4", "q5"]
        assert sched.unassigned == []
        assert sched.merged_over_cap

    def test_acceptance_conditions_travel_with_question_ids(self):
        sched = build_schedule(
            plan_payload=_plan([_q("a", "key_kpi", acceptance="供需各有证据")]),
            entity_kind="industry", field_groups=[],
        )
        assert sched.items[0].acceptance["a"] == "供需各有证据"
        assert sched.items[0].modules["a"] == "key_kpi"

    def test_unassigned_question_fails_loud(self):
        """装配缺陷必须在开跑前炸出来，而不是烧 53 分钟空转。"""
        from finance_agent.research.scheduling import Schedule, WorkItem

        bad = Schedule(items=[WorkItem(group="market", question_ids=("a",))],
                       unassigned=["b"])
        with pytest.raises(DispatchError, match="问题未完整分发"):
            assert_dispatch_complete(bad, _plan([_q("a", "key_kpi"), _q("b", "key_kpi")]))

    def test_empty_question_worker_fails_loud(self):
        from finance_agent.research.scheduling import Schedule, WorkItem

        bad = Schedule(items=[WorkItem(group="market", question_ids=("a",)),
                              WorkItem(group="landscape", fields=("value_chain",))])
        with pytest.raises(DispatchError, match="空问题 worker"):
            assert_dispatch_complete(bad, _plan([_q("a", "key_kpi")]))

    def test_plan_view_projects_only_assigned_questions(self):
        payload = _plan([_q("a", "key_kpi"), _q("b", "industry_chain"),
                         _q("c", "key_kpi", status="answered")])
        view = plan_view(payload, ["a", "c"])
        assert [q["question_id"] for q in view["questions"]] == ["a"]  # answered 不下发
        assert plan_view(payload, []) is None
        assert plan_view(None, ["a"]) is None


# ---------------- 3. 验收用例 1：问题分发装配回放（真实 loop + 真实配方） ----------------


class RecordingLLM:
    """替身：只记录每次实际收到的消息，不做任何工具调用（边界替换在 LLM 层）。"""

    model_name = "recording"

    def __init__(self, name: str):
        self.name = name
        self.received: list[list[dict]] = []

    def complete(self, messages, tools):
        self.received.append([dict(m) for m in messages])
        return AssistantReply(content="（本轮不产出）", tool_calls=[],
                              usage={"prompt_tokens": 1, "completion_tokens": 1,
                                     "total_tokens": 2})


@pytest.fixture()
def industry_env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    calcs = CalculationService(metrics, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-industry")
    gateway.register(FixtureAdapter(
        SourceCapability(source_id="demo", pit_grade=PitGrade.A, server_side_asof=False,
                         description="夹具源"),
        records=[DataRecord(source_id="demo", payload={"title": "行业报告"},
                            url="demo://report", available_at=NOW)],
    ))
    # 旧档案完整度 100%（事故现场：行业字段全满，研究却仍在继续）
    kb.add_evidence(Evidence(
        evidence_id="ev-full", source_id="demo",
        verbatim_quote="industry profile complete evidence 1234 million",
        retrieved_at=NOW, available_at=NOW, pit_grade=PitGrade.A,
    ))
    for field in [*INDUSTRY_SCHEMA.required, *INDUSTRY_SCHEMA.optional]:
        kb.assert_fact(Fact(
            entity_kind="industry", entity_id="ai-for-science", field=field,
            value=[{"name": "环节", "note": "已满档案"}] if field in (
                "sub_sectors", "player_landscape") else f"{field}：有据可查的行业描述",
            knowledge_time=NOW, evidence_ids=["ev-full"],
        ))
    return kb, metrics, events, writer, mw, calcs, gateway


class TestDispatchAssemblyReplay:
    def _run(self, industry_env, *, mode="deep"):
        kb, metrics, events, writer, mw, calcs, gateway = industry_env
        recipe = load_recipe("industry")
        plan = build_plan(
            entity_kind="industry", entity_id="ai-for-science",
            objective="究竟哪些公司是真的在形成技术护城河以及有比较大可能能够取得商业爆发",
            mode=mode, recipe=recipe, missing_fields=[], stale_fields=[],
            run_id="live-industry", now=NOW,
        )
        metrics.save_plan(plan_id=plan.plan_id, namespace="prod",
                          payload=plan.model_dump(mode="json"))
        workers = [RecordingLLM(f"w{i}") for i in range(4)]
        loop = ResearchLoop(
            store=kb, events=events, writer=writer, gateway=gateway,
            llm=RecordingLLM("main"), manifest=RunManifest(run_id="live-industry",
                                                           mode=RunMode.LIVE),
            completeness_target=0.8, gateway_sources=gateway.source_ids(),
            fetch_document=lambda url: "行业报告正文 1234 million",
            worker_llms=workers, plan_id=plan.plan_id, metrics=metrics,
            metric_writer=mw, calculations=calcs,
        )
        loop.run("industry", "ai-for-science", plan.objective)
        return loop, plan, workers, events

    def test_every_question_reaches_a_worker_context(self, industry_env):
        """九个问题必须全部进入执行上下文（事故现场：0 个进入）。"""
        loop, plan, workers, events = self._run(industry_env)
        pending = [q["question_id"] for q in pending_questions(plan.model_dump(mode="json"))]
        assert len(pending) >= 7, f"deep 行业配方问题过少：{pending}"

        briefs = "\n".join(
            m["content"] for w in workers for call in w.received for m in call
            if m.get("role") == "user"
        )
        missing = [q for q in pending if f"[{q}]" not in briefs]
        assert not missing, f"问题未进入任何 worker 上下文：{missing}"

        sched_events = [e for e in events.read("live-industry")
                        if e.type == "research/schedule"]
        assert sched_events, "调度装配未落事件（不可回放）"
        dispatched = sched_events[0].payload["dispatched_question_ids"]
        assert sorted(dispatched) == sorted(pending)
        assert sched_events[0].payload["unassigned"] == []
        # 每个 worker 都拿到问题（不许有空问题 worker）
        assert all(it["question_ids"] for it in sched_events[0].payload["items"])

    def test_plan_mode_contract_in_worker_brief(self, industry_env):
        """plan 模式专门提示词：证据/分析/反证/提交答案（旧字段补全只是副产物）。"""
        loop, plan, workers, events = self._run(industry_env)
        briefs = "\n".join(
            m["content"] for w in workers for call in w.received for m in call
            if m.get("role") == "user"
        )
        assert "问题驱动研究" in briefs
        assert "answer_question" in briefs
        assert "反证" in briefs
        assert "字段 100% 不等于研究充分" in briefs

    def test_worker_answer_gate_rejects_unassigned_question(self, industry_env):
        """worker 只能回答被分配的问题（越位回答让验收无法归因）。"""
        from finance_agent.research.tools import make_research_tools

        kb, metrics, events, writer, mw, calcs, gateway = industry_env
        recipe = load_recipe("industry")
        plan = build_plan(
            entity_kind="industry", entity_id="ai-for-science", objective="目标",
            mode="targeted", recipe=recipe, focus="护城河", run_id="live-industry", now=NOW,
        )
        metrics.save_plan(plan_id=plan.plan_id, namespace="prod",
                          payload=plan.model_dump(mode="json"))
        from finance_agent.research.evidence_desk import ChunkStore

        tools, tracker = make_research_tools(
            store=kb, writer=writer,
            manifest=RunManifest(run_id="live-industry", mode=RunMode.LIVE),
            entity_kind="industry", entity_id="ai-for-science", chunk_store=ChunkStore(),
            events=events, metrics=metrics, metric_writer=mw, calculations=calcs,
            plan_id=plan.plan_id, allowed_question_ids=["other-q"],
        )
        out = tools["answer_question"]({
            "question_id": plan.questions[0].question_id, "status": "gathering",
        })
        assert out["content"].startswith("rejected:")
        assert "未分配给本 worker" in out["content"]
        assert tracker.answer_rejections, "拒绝未计入 tracker（诊断无法区分未分发/提交失败）"

    def test_question_stall_diagnostic_classifies_cause(self, industry_env):
        """一轮零问题推进 → 具体诊断（区分未分发/提交被拒/来源不可得/分析未完成）。"""
        loop, plan, workers, events = self._run(industry_env)
        stalls = [e for e in events.read("live-industry")
                  if e.type == "research/question_stall"]
        assert stalls, "问题零推进却没有诊断事件"
        payload = stalls[0].payload
        assert payload["pending_question_ids"]
        # 问题已分发、worker 没登记证据也没提交 → 来源不可得（替身不产出）
        assert "not_dispatched" not in payload["causes"]
        assert "source_unavailable" in payload["causes"]
        assert payload["suggestions"]
        assert loop.question_stall_diagnostic is not None
