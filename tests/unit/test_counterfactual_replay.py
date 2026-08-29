"""反事实探针接入回放报告：决策点的决策被扰动重放，PC/CI/IDS 进报告。"""

from datetime import UTC, date, datetime

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

POINT = date(2023, 3, 31)


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


def make_prices():
    return PriceBook({"AAA": {date(2023, m, 28): 100.0 + m for m in range(1, 10)}})


def test_counterfactual_probe_wired_into_report(tmp_path):
    # 主决策 1 次 + 探针 base 1 次 + 2 个扰动 trial，每次决策 turn = 2 条回复
    def decision_replies():
        return [
            AssistantReply(
                content="",
                tool_calls=[
                    ToolCall(
                        call_id="x",
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

    agent = MockLLM(decision_replies() + decision_replies() + decision_replies() + decision_replies())
    baseline = MockLLM(
        [
            AssistantReply(
                content="",
                tool_calls=[
                    ToolCall(call_id="b", name="cast_vote", arguments={"action": "hold", "conviction": 3})
                ],
            ),
            AssistantReply(content="ok"),
        ]
    )

    events = EventStore(tmp_path / "e.db")
    kb = seed_kb(tmp_path)
    svc = DecisionService(kb=kb, decisions=DecisionStore(tmp_path / "d.db"), events=events)
    engine = ReplayEngine(
        kb=kb,
        events=events,
        decision_service=svc,
        llm_agent=agent,
        llm_baseline=baseline,
        price_book=make_prices(),
        artifacts_dir=tmp_path / "evals",
    )
    config = EvalConfig(
        name="cf-test",
        tickers=["AAA"],
        decision_points=[POINT],
        horizon_months=3,
        backbone_cutoff=date(2023, 6, 30),
        cost=CostModel(),
        counterfactual=True,
    )
    report = engine.run(config)

    assert report.verdict == "clean"
    assert report.counterfactual is not None
    # thesis 字段 revenue_fy（数值）→ ScaleField ×0.1 一种扰动 = 1 trial；
    # 脚本化 LLM 永远 buy 4 → 决策不变（PC=1）——探针正确识别「不随输入变化」
    assert report.counterfactual.trials == 1
    assert report.counterfactual.pc == 1.0
