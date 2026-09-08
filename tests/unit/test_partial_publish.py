"""部分发布验收（audit §3.9 P1）：终止路径保留 stop_reason 与已验证成果。

事故形态：只有最终 `_finalize_artifact_v2()` 发布 Dossier；取消路径可能直接结束，
已有有效成果没有统一 partial finalize——用户等了 59 分 49 秒，中途什么都没有。

判据：每完成一个问题就落检查点；取消/预算/停滞路径都冻结 partial 产物并发布快照；
产物诚实标注 draft/partial 与未完成题目（不拿草稿冒充最终报告）。
"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from finance_agent.commands.steps import StepContext, StepDeps, _publish_partial_artifact
from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.dossier.projector import DossierProjector
from finance_agent.dossier.service import DossierService
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.gateway import DataGateway
from finance_agent.harness.approvals import ApprovalService
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.models import Evidence, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.research.calculations import CalculationService

NOW = datetime(2024, 6, 1, tzinfo=UTC)


@pytest.fixture()
def env(tmp_path: Path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    decisions = DecisionStore(tmp_path / "d.db")
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    calcs = CalculationService(metrics, events=events)
    projector = DossierProjector(kb=kb, metrics=metrics, decisions=decisions)
    dossier = DossierService(kb=kb, metrics=metrics, projector=projector, events=events,
                             decisions=decisions)
    kb.add_evidence(Evidence(
        evidence_id="ev-1", source_id="web_search", url="https://x.com/a",
        verbatim_quote="平台收入 1500 million（FY2024）", retrieved_at=NOW,
        available_at=NOW, pit_grade=PitGrade.B,
    ))
    deps = StepDeps(
        events=events, kb=kb, writer=ProfileWriter(store=kb, events=events),
        gateway=DataGateway(mode="live", events=events, run_id="live-partial"),
        decisions=DecisionService(kb=kb, decisions=decisions, events=events),
        llm_for=lambda role: None, approvals=ApprovalService(events),
        evals_dir=tmp_path / "evals", reports_dir=tmp_path / "reports",
        knowledge_dir=tmp_path / "knowledge",
        metrics=metrics, metric_writer=mw, calculations=calcs, dossier_service=dossier,
    )
    ctx = StepContext(
        command_id="cmd-1", session_run_id="sess-1", child_run_id="child-research",
        ticker="ai-for-science", objective="哪些公司在形成护城河", config="",
        should_cancel=lambda: False, entity_kind="industry", depth="deep",
    )
    return deps, ctx, kb, metrics, events, dossier, tmp_path


def fake_loop(**kw):
    """终止路径的 loop 状态替身（只暴露 _publish_partial_artifact 用到的字段）。"""
    base = dict(
        stop_reason="cancelled", budget_exhausted=[], partial_checkpoint=None,
        plan_payload=None, assessment=None, budget_snapshot=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


CHECKPOINT = {
    "answered": [
        {"question_id": "objective-technology_moat-abc123", "status": "answered",
         "conclusion": "Schrödinger 的物理仿真平台有第三方客户验证，复制需 3-5 年。",
         "support_refs": ["ev-1", "claim-1"], "counter_refs": [], "unresolved": []},
        {"question_id": "demand-supply", "status": "unavailable",
         "conclusion": "", "support_refs": [], "counter_refs": [],
         "unresolved": ["缺行业供需公开数据"], },
    ],
    "claims": ["claim-1"], "observations": [], "calculations": ["calc-1"], "facts": [],
}

PLAN = {
    "plan_id": "plan-1", "entity_kind": "industry", "entity_id": "ai-for-science",
    "objective": "哪些公司在形成护城河", "mode": "deep", "status": "active",
    "questions": [
        {"question_id": "objective-technology_moat-abc123", "status": "answered",
         "module": "candidate_pool"},
        {"question_id": "demand-supply", "status": "unavailable", "module": "key_kpi"},
        {"question_id": "candidate-pool", "status": "unanswered", "module": "candidate_pool"},
        {"question_id": "counter-evidence", "status": "unanswered", "module": "catalysts_risks"},
    ],
}


class TestPartialPublish:
    def test_cancelled_path_freezes_verified_results(self, env):
        deps, ctx, kb, metrics, events, dossier, tmp_path = env
        loop = fake_loop(stop_reason="cancelled", partial_checkpoint=CHECKPOINT,
                         plan_payload=PLAN)
        artifact_id = _publish_partial_artifact(deps, ctx, loop, NOW)
        assert artifact_id
        art = metrics.get_artifact(artifact_id)
        assert art["status"] == "draft" and art["sufficiency"] == "partial"
        assert art["calculation_ids"] == ["calc-1"]
        assert art["plan_id"] == "plan-1"
        # 正文含已完成题目的完整结论 + 未完成题目的缺口说明
        md = art["markdown"]
        assert "Schrödinger" in md and "部分成果" in md
        assert "candidate-pool" in md and "counter-evidence" in md
        # 落盘同源（partial-report.md 由冻结产物渲染）
        files = list((tmp_path / "reports").rglob("partial-report.md"))
        assert files and "部分成果" in files[0].read_text(encoding="utf-8")

    def test_stop_reason_and_exhausted_dimension_are_visible(self, env):
        deps, ctx, kb, metrics, events, dossier, tmp_path = env
        loop = fake_loop(stop_reason="budget", budget_exhausted=["wall_clock"],
                         partial_checkpoint=CHECKPOINT, plan_payload=PLAN,
                         budget_snapshot={"exhausted": ["wall_clock"], "seconds_used": 2400})
        artifact_id = _publish_partial_artifact(deps, ctx, loop, NOW)
        created = [e for e in events.read("child-research")
                   if e.type == "research/artifact_created"]
        assert created and created[-1].payload["partial"] is True
        assert created[-1].payload["stop_reason"] == "budget"
        assert created[-1].payload["open_questions"] == ["candidate-pool", "counter-evidence"]
        art = metrics.get_artifact(artifact_id)
        assert any("budget" in x for x in art["report_document"]["limitations"])

    def test_snapshot_published_so_page_can_read_partial(self, env):
        deps, ctx, kb, metrics, events, dossier, tmp_path = env
        loop = fake_loop(stop_reason="cancelled", partial_checkpoint=CHECKPOINT,
                         plan_payload=PLAN)
        _publish_partial_artifact(deps, ctx, loop, NOW)
        published = [e for e in events.read("sess-1") if e.type == "dossier/published"] \
            or [e for e in events.read("child-research") if e.type == "dossier/published"]
        assert published, "部分成果未发布快照（页面读不到）"
        snap = dossier.get(published[0].payload["snapshot_id"])
        assert snap["modules"]["research_sources"]["status"] in ("ready", "partial")

    def test_no_results_means_no_artifact(self, env):
        """一无所获时不发空产物（不拿 draft 冒充成果）。"""
        deps, ctx, kb, metrics, events, dossier, tmp_path = env
        loop = fake_loop(stop_reason="cancelled", partial_checkpoint=None, plan_payload=PLAN)
        assert _publish_partial_artifact(deps, ctx, loop, NOW) is None
        assert metrics.artifacts_as_of("industry", "ai-for-science", NOW) == []

    def test_report_card_marks_partial(self, env):
        deps, ctx, kb, metrics, events, dossier, tmp_path = env
        loop = fake_loop(stop_reason="stalled", partial_checkpoint=CHECKPOINT,
                         plan_payload=PLAN)
        _publish_partial_artifact(deps, ctx, loop, NOW)
        cards = [e for e in events.read("sess-1") if e.type == "report/published"]
        assert cards and cards[-1].payload["partial"] is True
        assert "部分成果" in cards[-1].payload["summary"]
        assert "2 题已完成" in cards[-1].payload["summary"]


class TestPartialCheckpoint:
    def test_checkpoint_emitted_when_questions_advance(self, tmp_path):
        """每完成一个问题就落检查点（不等最终合成）。"""
        import hashlib

        from test_research_loop_v2 import TYPED_FLOW, make_loop, save_plan, seed_full_profile

        from finance_agent.llm.mock import MockLLM
        from finance_agent.research.plan import ResearchQuestion

        kb_path = tmp_path
        env = _loop_env(kb_path)
        kb, metrics, events, writer, mw, calcs, gateway = env
        seed_full_profile(kb)
        plan = save_plan(metrics, [
            ResearchQuestion(question_id="q-backlog", text="订单与收入验证？",
                             priority="high", status="unanswered"),
            ResearchQuestion(question_id="q-other", text="其他？", priority="medium",
                             status="unanswered"),
        ], plan_id="plan-partial", mode="targeted")
        loop = make_loop(env, MockLLM(list(TYPED_FLOW)), plan_id=plan.plan_id, max_rounds=1)
        loop.run("stock", "BE", "订单与收入验证")
        checkpoints = [e for e in events.read("live-loop")
                       if e.type == "research/partial_published"]
        assert checkpoints, "问题推进却没有部分成果检查点"
        payload = checkpoints[0].payload
        assert payload["plan_id"] == plan.plan_id
        assert any(a["question_id"] == "q-backlog" for a in payload["answered"])
        assert payload["input_hash"] == hashlib.sha256(
            __import__("json").dumps(
                {"plan": plan.plan_id, "answered": ["q-backlog"],
                 "claims": sorted(payload["claims"]),
                 "observations": sorted(payload["observations"])},
                ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        assert loop.partial_checkpoint is not None


def _loop_env(tmp_path):
    from finance_agent.gateway.adapters.fixture import FixtureAdapter
    from finance_agent.gateway.models import DataRecord, SourceCapability

    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    calcs = CalculationService(metrics, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-loop")
    gateway.register(FixtureAdapter(
        SourceCapability(source_id="demo", pit_grade=PitGrade.A, server_side_asof=False,
                         description="夹具源"),
        records=[DataRecord(source_id="demo", payload={"form": "10-K", "title": "FY2024"},
                            url="demo://filing", available_at=NOW)],
    ))
    return kb, metrics, events, writer, mw, calcs, gateway
