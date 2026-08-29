"""ReplayEngine 验收：walk-forward 时点回放全链路。

场景：4 个季度决策点；agent 每次基于 ≤T 档案出卡（buy, sizing 0.1）；
LLM-only 对照组无档案裸投（hold）；B&H 基线按同一成本模型。
断言：对账数字手工可核；截止日分区正确；leakage=0 → clean；被污染 → 整批作废。
"""

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

POINTS = [date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31)]


def make_prices() -> PriceBook:
    # 月末收盘：2022-12 ~ 2024-06，每月 +10（稳定单边行情，便于手算）
    closes = {}
    months = [(2022, 12)] + [(2023, m) for m in range(1, 13)] + [(2024, m) for m in range(1, 7)]
    for i, (y, m) in enumerate(months):
        closes[date(y, m, 28)] = 100.0 + i * 10.0
    return PriceBook({"AAA": closes})


def seed_kb(tmp_path) -> BitemporalStore:
    kb = BitemporalStore(tmp_path / "kb.db")
    kb.add_evidence(
        Evidence(
            evidence_id="ev-1",
            source_id="edgar",
            verbatim_quote="Total revenue 100",
            retrieved_at=datetime(2022, 3, 2, tzinfo=UTC),
            available_at=datetime(2022, 3, 1, tzinfo=UTC),  # 远早于第一个决策点
            pit_grade=PitGrade.A,
        )
    )
    kb.assert_fact(
        Fact(
            entity_kind="stock",
            entity_id="AAA",
            field="revenue_fy",
            value=100,
            knowledge_time=datetime(2022, 3, 1, tzinfo=UTC),
            evidence_ids=["ev-1"],
        )
    )
    return kb


def agent_llm():
    from finance_agent.llm.base import AssistantReply, ToolCall
    from finance_agent.llm.mock import MockLLM

    replies = []
    for i in range(len(POINTS)):
        replies += [
            AssistantReply(
                content="",
                tool_calls=[
                    ToolCall(
                        call_id=f"p{i}",
                        name="propose_decision",
                        arguments={
                            "action": "buy",
                            "conviction": 4,
                            "horizon": "3m",
                            "rationale": ["ev-1"],
                            "thesis_points": ["revenue_fy"],
                            "invalidation": ["营收跌破 90 则失效"],
                            "position": {"sizing_pct": 0.1, "max_loss_pct": 0.05},
                        },
                    )
                ],
            ),
            AssistantReply(content="buy"),
        ]
    return MockLLM(replies)


def baseline_llm():
    from finance_agent.llm.base import AssistantReply, ToolCall
    from finance_agent.llm.mock import MockLLM

    replies = []
    for i in range(len(POINTS)):
        replies += [
            AssistantReply(
                content="",
                tool_calls=[
                    ToolCall(
                        call_id=f"b{i}",
                        name="cast_vote",
                        arguments={"action": "hold", "conviction": 3},
                    )
                ],
            ),
            AssistantReply(content="hold"),
        ]
    return MockLLM(replies)


def make_engine(tmp_path, *, kb=None, agent=None, baseline=None):
    events = EventStore(tmp_path / "events.db")
    kb = kb or seed_kb(tmp_path)
    decisions = DecisionStore(tmp_path / "decisions.db")
    svc = DecisionService(kb=kb, decisions=decisions, events=events)
    engine = ReplayEngine(
        kb=kb,
        events=events,
        decision_service=svc,
        llm_agent=agent or agent_llm(),
        llm_baseline=baseline or baseline_llm(),
        price_book=make_prices(),
        artifacts_dir=tmp_path / "evals",
    )
    return engine, events


def make_config() -> EvalConfig:
    return EvalConfig(
        name="p3-acceptance",
        tickers=["AAA"],
        decision_points=POINTS,
        horizon_months=3,
        backbone_model="mock-llm",
        backbone_cutoff=date(2023, 6, 30),  # 前两个点在污染区，后两个在诚实区
        cost=CostModel(commission_bps=5.0, slippage_bps=10.0),
        incremental_research=False,
    )


def test_walkforward_replay_end_to_end(tmp_path):
    engine, events = make_engine(tmp_path)
    report = engine.run(make_config())

    # 4 个决策点全部出卡，created_at == T，全部 buy
    assert report.verdict == "clean"
    assert len(report.outcomes) == 4
    assert all(o.action == "buy" and not o.incomplete for o in report.outcomes)
    assert [o.point for o in report.outcomes] == POINTS

    # 手算核对第一个点：T=03-31 → 入场 04-28@140；T+3m=06-30 → 出场 07-28@170
    first = report.outcomes[0]
    expected_gross = 170.0 / 140.0 - 1
    assert abs(first.gross_return - expected_gross) < 1e-9
    # 净收益 = (gross - 双边成本 0.003) * sizing 0.1
    assert abs(first.net_return - (expected_gross - 0.003) * 0.1) < 1e-9
    # B&H 基线同窗口同成本（sizing=1.0）
    assert abs(first.baseline_bh_net_return - (expected_gross - 0.003) * 1.0) < 1e-9

    # 截止日分区：前两点污染区、后两点诚实区
    assert [o.zone for o in report.outcomes] == [
        "contaminated",
        "contaminated",
        "honest",
        "honest",
    ]

    # LLM-only 对照全 hold（不持仓）→ 收益 0 → 知识库增量 = agent − baseline > 0
    assert report.aggregate.llm_only_mean_net_return == 0.0
    assert report.aggregate.mean_net_return > 0
    assert report.aggregate.kb_delta > 0

    # 统计纪律字段齐全
    assert 0.0 <= report.aggregate.dsr <= 1.0
    assert 0.0 <= report.aggregate.psr <= 1.0
    assert report.aggregate.max_drawdown >= 0.0

    # 报告落盘 + 事件
    assert (tmp_path / "evals" / report.eval_run_id / "report.json").exists()
    assert events.read(report.eval_run_id, types={"eval/report"})


def test_leakage_marks_run_contaminated(tmp_path):
    """硬门禁：回放过程中出现任何一次穿越尝试（被网关拦下也算），整批作废。"""
    from finance_agent.gateway.adapters.fixture import FixtureAdapter
    from finance_agent.gateway.gateway import DataGateway
    from finance_agent.gateway.models import DataRecord, SourceCapability
    from finance_agent.llm.base import AssistantReply, ToolCall
    from finance_agent.llm.mock import MockLLM

    events = EventStore(tmp_path / "e.db")
    kb = seed_kb(tmp_path)
    decisions = DecisionStore(tmp_path / "d.db")
    svc = DecisionService(kb=kb, decisions=decisions, events=events)

    def poisoned_gateway(as_of: datetime, run_id: str) -> DataGateway:
        g = DataGateway(mode="eval", eval_as_of=as_of, events=events, run_id=run_id)
        g.register(
            FixtureAdapter(
                SourceCapability(source_id="edgar", pit_grade=PitGrade.A),
                # 一条「未来」记录：网关应丢弃并记 leakage/attempt
                [DataRecord(
                    source_id="edgar", payload={"form": "10-K"},
                    available_at=datetime(2024, 1, 1, tzinfo=UTC),
                )],
            )
        )
        return g

    agent = MockLLM(
        [
            # 研究轮：查数据源（触发网关拦截）→ 无新发现停滞
            AssistantReply(content="", tool_calls=[ToolCall(call_id="q1", name="query_edgar", arguments={})]),
            AssistantReply(content="研究完毕"),
            # 决策：引用干净证据 ev-1
            AssistantReply(
                content="",
                tool_calls=[
                    ToolCall(
                        call_id="d1",
                        name="propose_decision",
                        arguments={
                            "action": "buy",
                            "conviction": 4,
                            "horizon": "3m",
                            "rationale": ["ev-1"],
                            "thesis_points": ["revenue_fy"],
                            "invalidation": ["x"],
                            "position": {"sizing_pct": 0.1, "max_loss_pct": 0.05},
                        },
                    )
                ],
            ),
            AssistantReply(content="buy"),
        ]
    )
    baseline = MockLLM(
        [
            AssistantReply(
                content="",
                tool_calls=[
                    ToolCall(call_id="b0", name="cast_vote", arguments={"action": "hold", "conviction": 3})
                ],
            ),
            AssistantReply(content="hold"),
        ]
    )
    engine = ReplayEngine(
        kb=kb,
        events=events,
        decision_service=svc,
        llm_agent=agent,
        llm_baseline=baseline,
        price_book=make_prices(),
        artifacts_dir=tmp_path / "evals",
        gateway_factory=poisoned_gateway,
    )
    config = EvalConfig(
        name="contaminated",
        tickers=["AAA"],
        decision_points=[date(2023, 3, 31)],
        horizon_months=3,
        backbone_model="mock-llm",
        backbone_cutoff=date(2023, 6, 30),
        cost=CostModel(commission_bps=5.0, slippage_bps=10.0),
        incremental_research=True,
    )
    report = engine.run(config)
    assert report.verdict == "contaminated"
    assert report.leakage_events >= 1
