"""DecisionLoop（S3）：基于档案生成决策卡，risk-review 必达。"""

from datetime import UTC, datetime

from finance_agent.decision.card import Action
from finance_agent.decision.loop import DecisionLoop
from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM

NOW = datetime(2024, 6, 1, tzinfo=UTC)


def env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    kb.add_evidence(
        Evidence(
            evidence_id="ev-1",
            source_id="edgar",
            verbatim_quote="Total revenue 100",
            retrieved_at=NOW,
            available_at=datetime(2023, 3, 1, tzinfo=UTC),
            pit_grade=PitGrade.A,
        )
    )
    kb.assert_fact(
        Fact(
            entity_kind="stock",
            entity_id="AAPL",
            field="revenue_fy",
            value=100,
            knowledge_time=datetime(2023, 3, 1, tzinfo=UTC),
            evidence_ids=["ev-1"],
        )
    )
    decisions = DecisionStore(tmp_path / "decisions.db")
    events = EventStore(tmp_path / "e.db")
    svc = DecisionService(kb=kb, decisions=decisions, events=events)
    manifest = RunManifest(run_id="run-1", mode=RunMode.LIVE)
    return kb, decisions, events, svc, manifest


def propose_call(sizing=0.1) -> AssistantReply:
    return AssistantReply(
        content="",
        tool_calls=[
            ToolCall(
                call_id="c1",
                name="propose_decision",
                arguments={
                    "action": "buy",
                    "conviction": 4,
                    "horizon": "6m",
                    "rationale": ["ev-1"],
                    "thesis_points": ["revenue_fy"],
                    "invalidation": ["营收增速连续两季 < 5% 则失效"],
                    "position": {"sizing_pct": sizing, "max_loss_pct": 0.05},
                },
            )
        ],
    )


def test_decision_issued_end_to_end(tmp_path):
    kb, decisions, events, svc, manifest = env(tmp_path)
    llm = MockLLM([propose_call(), AssistantReply(content="建议买入")])
    loop = DecisionLoop(kb=kb, events=events, decision_service=svc, llm=llm, manifest=manifest)

    card_id = loop.run("stock", "AAPL", now=NOW)

    assert card_id is not None
    card = decisions.get(card_id)
    assert card.action is Action.BUY
    # 决策绑定了当时的档案快照，可复现
    from finance_agent.knowledge.snapshot import kb_snapshot_id

    assert card.kb_snapshot_id == kb_snapshot_id(kb, [("stock", "AAPL")], NOW)
    assert events.read("run-1", types={"decision/card_issued"})


def test_risk_review_rejection_returns_none(tmp_path):
    kb, decisions, events, svc, manifest = env(tmp_path)
    llm = MockLLM([propose_call(sizing=0.9), AssistantReply(content="被拒")])
    loop = DecisionLoop(kb=kb, events=events, decision_service=svc, llm=llm, manifest=manifest)

    assert loop.run("stock", "AAPL", now=NOW) is None
    assert decisions.list() == []
    assert events.read("run-1", types={"decision/card_issued"}) == []


def test_llm_choosing_no_action_is_respected(tmp_path):
    """fail-closed 精神：模型可以不出卡（watch/avoid/不行动是合法决策）。"""
    kb, decisions, events, svc, manifest = env(tmp_path)
    llm = MockLLM([AssistantReply(content="证据不足，暂不出卡")])
    loop = DecisionLoop(kb=kb, events=events, decision_service=svc, llm=llm, manifest=manifest)
    assert loop.run("stock", "AAPL", now=NOW) is None
    assert decisions.list() == []
