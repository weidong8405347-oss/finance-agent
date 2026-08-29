"""回放断点恢复：中断后从上次完成的决策点续跑。"""

from datetime import UTC, date, datetime

import pytest

from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.evaluation.config import EvalConfig
from finance_agent.evaluation.costs import CostModel
from finance_agent.evaluation.prices import PriceBook
from finance_agent.evaluation.replay import ReplayEngine
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM

POINTS = [date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31)]


def make_prices():
    closes = {}
    months = [(2022, 12)] + [(2023, m) for m in range(1, 13)] + [(2024, m) for m in range(1, 7)]
    for i, (y, m) in enumerate(months):
        closes[date(y, m, 28)] = 100.0 + i * 10.0
    return PriceBook({"AAA": closes})


def seed_kb(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    kb.add_evidence(
        Evidence(
            evidence_id="ev-1",
            source_id="edgar",
            verbatim_quote="Total revenue 100",
            retrieved_at=datetime(2022, 3, 2, tzinfo=UTC),
            available_at=datetime(2022, 3, 1, tzinfo=UTC),
            pit_grade=PitGrade.A,
        )
    )
    kb.assert_fact(
        Fact(
            entity_kind="stock", entity_id="AAA", field="revenue_fy", value=100,
            knowledge_time=datetime(2022, 3, 1, tzinfo=UTC), evidence_ids=["ev-1"],
        )
    )
    return kb


def agent_replies(n_points: int):
    replies = []
    for i in range(n_points):
        replies += [
            AssistantReply(
                content="",
                tool_calls=[
                    ToolCall(
                        call_id=f"p{i}",
                        name="propose_decision",
                        arguments={
                            "action": "buy", "conviction": 4, "horizon": "3m",
                            "rationale": ["ev-1"], "thesis_points": ["revenue_fy"],
                            "invalidation": ["x"],
                            "position": {"sizing_pct": 0.1, "max_loss_pct": 0.05},
                        },
                    )
                ],
            ),
            AssistantReply(content="buy"),
        ]
    return replies


def baseline_replies(n_points: int):
    replies = []
    for i in range(n_points):
        replies += [
            AssistantReply(
                content="",
                tool_calls=[
                    ToolCall(
                        call_id=f"b{i}", name="cast_vote", arguments={"action": "hold", "conviction": 3}
                    )
                ],
            ),
            AssistantReply(content="ok"),
        ]
    return replies


def make_config():
    return EvalConfig(
        name="resume-test",
        tickers=["AAA"],
        decision_points=POINTS,
        horizon_months=3,
        backbone_cutoff=date(2023, 6, 30),
        cost=CostModel(),
        incremental_research=False,
    )


def test_resume_after_interruption(tmp_path):
    """第一次跑到第 3 个点时脚本耗尽（崩溃）；续跑只补剩余点，报告仍含全部 4 点。"""
    events = EventStore(tmp_path / "e.db")
    kb = seed_kb(tmp_path)
    decisions = DecisionStore(tmp_path / "d.db")
    svc = DecisionService(kb=kb, decisions=decisions, events=events)

    # 第一次：脚本只够 2 个点 → 第 3 个点崩溃
    engine1 = ReplayEngine(
        kb=kb, events=events, decision_service=svc,
        llm_agent=MockLLM(agent_replies(2)), llm_baseline=MockLLM(baseline_replies(2)),
        price_book=make_prices(), artifacts_dir=tmp_path / "evals",
    )
    with pytest.raises(RuntimeError):
        engine1.run(make_config(), eval_run_id="eval-resume")

    # 续跑：同一 eval_run_id，新脚本只需覆盖剩余 2 个点
    engine2 = ReplayEngine(
        kb=kb, events=events, decision_service=svc,
        llm_agent=MockLLM(agent_replies(2)),  # 点 3、4 各 2 条
        llm_baseline=MockLLM(baseline_replies(2)),
        price_book=make_prices(), artifacts_dir=tmp_path / "evals",
    )
    report = engine2.run(make_config(), eval_run_id="eval-resume")

    assert len(report.outcomes) == 4
    assert [o.point for o in report.outcomes] == POINTS
    assert report.verdict == "clean"
