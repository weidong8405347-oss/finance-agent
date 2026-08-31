"""canary 诱饵：合成「未来事实」被引用即判污染（评估对齐稿 §2.3）。

机制：评估 run 注入带唯一 token 的合成诱饵记录（available_at ≤ T，能通过时间锁）；
agent 若把诱饵内容登记为证据并在决策中引用 → 它在复述上下文而非分析 → contaminated。
"""

from datetime import UTC, date, datetime

from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.evaluation.canary import CANARY_SOURCE
from finance_agent.evaluation.config import EvalConfig
from finance_agent.evaluation.costs import CostModel
from finance_agent.evaluation.prices import PriceBook
from finance_agent.evaluation.replay import ReplayEngine
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.gateway import DataGateway
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
            entity_kind="stock",
            entity_id="AAA",
            field="revenue_fy",
            value=100,
            knowledge_time=datetime(2022, 3, 1, tzinfo=UTC),
            evidence_ids=["ev-1"],
        )
    )
    return kb


def make_prices():
    closes = {date(2023, m, 28): 100.0 + m for m in range(1, 10)}
    return PriceBook({"AAA": closes})


def gateway_factory(events):
    """空网关：canary adapter 由引擎在 config.canary=True 时注入。"""

    def factory(as_of: datetime, run_id: str) -> DataGateway:
        return DataGateway(mode="eval", eval_as_of=as_of, events=events, run_id=run_id)

    return factory


def agent_script(*, cite_canary: bool) -> MockLLM:
    quote = "SYNTHETIC-CANARY-7F3A: AAA to be acquired at 50% premium"
    evidence_id = "ev-canary" if cite_canary else "ev-1"
    replies = [
        # 研究轮：查 canary 源；引用者把它登记为证据
        AssistantReply(
            content="",
            tool_calls=[ToolCall(call_id="q1", name=f"query_{CANARY_SOURCE}", arguments={})],
        ),
    ]
    if cite_canary:
        # query_canary 返回的记录落检索台账为 chk-0001；引用者把摘录登记为证据
        # （verified binding：quote 必须是 chunk 逐珠子串，available_at 由记录推导）
        replies.append(
            AssistantReply(
                content="",
                tool_calls=[
                    ToolCall(
                        call_id="q2",
                        name="register_evidence",
                        arguments={
                            "evidence_id": "ev-canary",
                            "chunk_id": "chk-0001",
                            "verbatim_quote": quote,
                        },
                    )
                ],
            )
        )
    replies.append(AssistantReply(content="研究完毕"))
    # 决策
    replies.append(
        AssistantReply(
            content="",
            tool_calls=[
                ToolCall(
                    call_id="d1",
                    name="propose_decision",
                    arguments={
                        "action": "buy",
                        "conviction": 5,
                        "horizon": "3m",
                        "rationale": [evidence_id],
                        "thesis_points": ["revenue_fy"],
                        "invalidation": ["x"],
                        "position": {"sizing_pct": 0.1, "max_loss_pct": 0.05},
                    },
                )
            ],
        )
    )
    replies.append(AssistantReply(content="done"))
    return MockLLM(replies)


def baseline_script() -> MockLLM:
    return MockLLM(
        [
            AssistantReply(
                content="",
                tool_calls=[
                    ToolCall(call_id="b0", name="cast_vote", arguments={"action": "hold", "conviction": 3})
                ],
            ),
            AssistantReply(content="ok"),
        ]
    )


def make_engine(tmp_path, agent):
    events = EventStore(tmp_path / "e.db")
    kb = seed_kb(tmp_path)
    decisions = DecisionStore(tmp_path / "d.db")
    svc = DecisionService(kb=kb, decisions=decisions, events=events)
    return ReplayEngine(
        kb=kb,
        events=events,
        decision_service=svc,
        llm_agent=agent,
        llm_baseline=baseline_script(),
        price_book=make_prices(),
        artifacts_dir=tmp_path / "evals",
        gateway_factory=gateway_factory(events),
    )


def make_config() -> EvalConfig:
    return EvalConfig(
        name="canary-test",
        tickers=["AAA"],
        decision_points=[POINT],
        horizon_months=3,
        backbone_cutoff=date(2023, 6, 30),
        cost=CostModel(),
        incremental_research=True,
        canary=True,
    )


def test_canary_citation_contaminates_run(tmp_path):
    engine = make_engine(tmp_path, agent_script(cite_canary=True))
    report = engine.run(make_config())
    assert report.verdict == "contaminated"
    assert report.canary_triggered is True


def test_clean_run_when_canary_ignored(tmp_path):
    engine = make_engine(tmp_path, agent_script(cite_canary=False))
    report = engine.run(make_config())
    assert report.verdict == "clean"
    assert report.canary_triggered is False


def test_canary_source_is_model_visible_and_indistinguishable():
    """诱饵源必须对模型可见且与真源不可区分——否则 trap 永远不可能被考验
    （2026-08-31 真实复跑发现：缺 schema → 裸工具 → 模型 0 次调用，防线形同虚设）。

    模型侧（GATEWAY_TOOL_SCHEMAS）：常规新闻源描述 + ticker 参数，不得出现诱饵标识；
    操作员侧（capability.description）：保持诚实标注（能力目录页可见）。
    """
    from finance_agent.evaluation.canary import CANARY_TOKEN, make_canary_adapter
    from finance_agent.gateway.tools import GATEWAY_TOOL_SCHEMAS

    schema = GATEWAY_TOOL_SCHEMAS.get("query_canary_news")
    assert schema is not None, "诱饵源缺 schema → 真实模型侧不可见（trap 不可被考验）"
    assert "ticker" in schema["parameters"]["properties"]
    assert "ticker" in schema["parameters"]["required"]
    desc = schema["description"]
    assert CANARY_TOKEN not in desc and "诱饵" not in desc and "合成" not in desc
    # 操作员侧仍诚实标注（能力与审计视图）
    assert "诱饵" in make_canary_adapter().capability().description
