"""ResearchLoop 验收：轮次制迭代研究（DESIGN.md §5.1，P1 验收标准）。

脚本化 3 轮：完整度 0 → 0.4 → 0.8 → 1.0 单调提升后收敛；
所有落库事实绑证据；每轮落 IterationReport 事件。
"""

from datetime import UTC, datetime

from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.gateway import DataGateway
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.schema import STOCK_SCHEMA
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.research.loop import ResearchLoop

NOW = datetime(2024, 6, 1, tzinfo=UTC)
FIELDS = list(STOCK_SCHEMA.required)  # 测试驱动 schema 的五个必填字段


def tc(i: int, name: str, args: dict) -> ToolCall:
    return ToolCall(call_id=f"c{i}", name=name, arguments=args)


def scripted_llm() -> MockLLM:
    """3 轮研究脚本（verified binding 纪律：read 正文 → register 摘录 → propose 落库）。

    chunk 序：chk-0001 = query_edgar 的 filing 记录；之后每次 read_edgar_filing
    产生一个正文窗口 chunk（chk-0002 起递增）。evidence_id 显式指定（脚本静态可预测）。
    schema 必填 8 字段全写（target=1.0 收敛）。
    """
    quotes = {
        "revenue_fy": ("Total revenue 100", 100),
        "net_income_fy": ("Net income 25", 25),
        "cash_flow": ("Operating cash flow 30", 30),
        "valuation": ("market cap 500", 500),
        "business_model": ("sells phones and services", "硬件+服务"),
        "moat": ("ecosystem lock-in", "生态锁定"),
        "risks": ("competition may intensify", "竞争加剧"),
        "peers": ("peers include Pear Corp", " Pear Corp"),
    }
    facts = [(f, *quotes[f]) for f in FIELDS]
    replies = [
        # round 1 开头：先拿 filing 记录（→ chk-0001）
        AssistantReply(content="", tool_calls=[tc(0, "query_edgar", {"ticker": "AAPL"})]),
    ]
    i = 1
    for n, (field, quote, value) in enumerate(facts):
        chk = f"chk-{n + 2:04d}"
        ev = f"ev-{n}"
        replies += [
            AssistantReply(content="", tool_calls=[
                tc(i, "read_edgar_filing", {"chunk_id": "chk-0001", "query": quote.split()[0]}),
            ]),
            AssistantReply(content="", tool_calls=[
                tc(i + 1, "register_evidence",
                   {"chunk_id": chk, "verbatim_quote": quote, "evidence_id": ev}),
            ]),
            AssistantReply(content="", tool_calls=[
                tc(i + 2, "propose_fact", {"field": field, "value": value, "evidence_ids": [ev]}),
            ]),
        ]
        i += 3
        if n in (2, 5):  # round1 写 3 字段、round2 写 3 字段、round3 写 2 字段
            replies.append(AssistantReply(content="round done"))
    replies.append(AssistantReply(content="round3 done"))
    return MockLLM(replies)


def make_loop(tmp_path, llm, *, max_rounds=5):
    from finance_agent.gateway.adapters.fixture import FixtureAdapter
    from finance_agent.gateway.models import DataRecord, SourceCapability
    from finance_agent.knowledge.models import PitGrade

    kb = BitemporalStore(tmp_path / "kb.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-1")
    gateway.register(FixtureAdapter(
        SourceCapability(source_id="edgar", pit_grade=PitGrade.A, description="夹具 filing 源"),
        records=[DataRecord(
            source_id="edgar",
            payload={"form": "10-K", "accession": "000-1"},
            available_at=datetime(2024, 3, 1, tzinfo=UTC),
            url="demo://10k",
        )],
    ))
    manifest = RunManifest(run_id="live-1", mode=RunMode.LIVE)
    fake_fetch = lambda url: (  # noqa: E731 - 夹具正文：含全部脚本 quote
        "Total revenue 100. Net income 25. Operating cash flow 30. market cap 500. "
        "sells phones and services. ecosystem lock-in. competition may intensify. "
        "peers include Pear Corp."
    )
    loop = ResearchLoop(
        store=kb,
        events=events,
        writer=writer,
        gateway=gateway,
        llm=llm,
        manifest=manifest,
        max_rounds=max_rounds,
        completeness_target=1.0,
        gateway_sources=["edgar"],
        fetch_document=fake_fetch,
    )
    return loop, kb, events


def test_three_rounds_monotonic_completeness_then_converged(tmp_path):
    loop, kb, events = make_loop(tmp_path, scripted_llm())
    reports = loop.run("stock", "AAPL", objective="投资研究", now=NOW)

    assert loop.stop_reason == "converged"
    assert len(reports) == 3

    before = [r.completeness_before for r in reports]
    after = [r.completeness_after for r in reports]
    assert before[0] == 0.0
    assert before == sorted(before) and after == sorted(after)  # 单调提升
    assert after[-1] == 1.0

    # 档案终态：schema 必填字段全部就位
    profile = kb.as_of("stock", "AAPL", NOW)
    assert set(profile) == set(FIELDS)

    # 全部事实绑证据且 knowledge_time 由证据推导（= evidence.available_at）
    for rec in profile.values():
        assert len(rec.evidence_ids) >= 1
        assert rec.knowledge_time == datetime(2024, 3, 1, tzinfo=UTC) or rec.knowledge_time == datetime(
            2024, 2, 1, tzinfo=UTC
        )

    # 每轮都有迭代报告事件
    assert len(events.read("live-1", types={"research/round_end"})) == 3


def test_stall_when_round_writes_nothing(tmp_path):
    llm = MockLLM([AssistantReply(content="没有新发现"), AssistantReply(content="还是没有")])
    loop, _, events = make_loop(tmp_path, llm, max_rounds=5)
    loop.run("stock", "AAPL", objective="研究", now=NOW)
    assert loop.stop_reason == "stalled"
    assert len(events.read("live-1", types={"research/round_end"})) == 1


def test_rejected_fact_does_not_block_loop(tmp_path):
    """numeric-guard 拒绝的事实不落库、记入报告，循环继续。"""
    f1 = FIELDS[0]
    llm = MockLLM(
        [
            AssistantReply(content="", tool_calls=[tc(0, "query_edgar", {"ticker": "AAPL"})]),
            AssistantReply(content="", tool_calls=[
                tc(1, "read_edgar_filing", {"chunk_id": "chk-0001", "query": "Net"}),
            ]),
            # 登记「Net income 25」作为证据（合法：quote 是 chunk 逐珠子串）
            AssistantReply(content="", tool_calls=[
                tc(2, "register_evidence", {
                    "chunk_id": "chk-0002", "verbatim_quote": "Net income 25",
                    "evidence_id": "ev-bad",
                }),
            ]),
            # 但拿它去支撑「revenue=100」→ 数字不在摘录里 → numeric-guard 拒绝
            AssistantReply(
                content="",
                tool_calls=[tc(3, "propose_fact", {"field": f1, "value": 100, "evidence_ids": ["ev-bad"]})],
            ),
            AssistantReply(content="done"),
        ]
    )
    loop, kb, _ = make_loop(tmp_path, llm, max_rounds=1)
    reports = loop.run("stock", "AAPL", objective="研究", now=NOW)
    assert loop.stop_reason == "stalled"  # 无成功写入 → 停滞
    assert reports[0].facts_written == [] and len(reports[0].rejected) == 1
    assert kb.as_of("stock", "AAPL", NOW) == {}


def test_fabricated_quote_is_rejected(tmp_path):
    """自编自引防线（2026-08-30 验收事故回归）：模型凭记忆编的摘录不是 chunk
    逐珠子串 → register_evidence 拒绝，证据不落库。"""
    llm = MockLLM(
        [
            AssistantReply(content="", tool_calls=[tc(0, "query_edgar", {"ticker": "AAPL"})]),
            AssistantReply(content="", tool_calls=[
                tc(1, "read_edgar_filing", {"chunk_id": "chk-0001", "query": "revenue"}),
            ]),
            # 模型编造：正文里根本没有「1.47 billion」
            AssistantReply(content="", tool_calls=[
                tc(2, "register_evidence", {
                    "chunk_id": "chk-0002",
                    "verbatim_quote": "Total revenue was $1.47 billion",
                }),
            ]),
            AssistantReply(content="done"),
        ]
    )
    loop, kb, _ = make_loop(tmp_path, llm, max_rounds=1)
    reports = loop.run("stock", "AAPL", objective="研究", now=NOW)
    assert reports[0].facts_written == []
    assert any("逐珠子串" in r["reason"] for r in reports[0].rejected)
    # 未登记任何证据
    import pytest as _pytest

    from finance_agent.knowledge.errors import MissingEvidenceError
    with _pytest.raises(MissingEvidenceError):
        kb.get_evidence("ev-0")


def test_stale_fields_force_refresh_even_when_complete(tmp_path):
    """档案「完整但陈旧」→ 必须触发研究刷新（不得「无需研究」）。

    回归：2026-08-30 BNTX 实测——stale 字段占权重分让完整度达标，研究假收敛。
    """
    from finance_agent.knowledge.models import Evidence, Fact, PitGrade

    llm = MockLLM([AssistantReply(content="没有新发现")])
    loop, kb, _ = make_loop(tmp_path, llm, max_rounds=1)
    # 预置：五个必填字段全部填满，但 knowledge_time 很旧（stale）

    old = datetime(2020, 1, 1, tzinfo=UTC)
    kb.add_evidence(Evidence(
        evidence_id="ev-old", source_id="demo", verbatim_quote="old data",
        retrieved_at=old, available_at=old, pit_grade=PitGrade.A))
    from finance_agent.harness.manifest import RunManifest, RunMode
    from finance_agent.knowledge.writer import ProfileWriter
    writer = ProfileWriter(store=kb, events=None)
    for f in FIELDS:
        writer.write_fact(
            Fact(entity_kind="stock", entity_id="AAPL", field=f, value="old",
                 knowledge_time=old, evidence_ids=["ev-old"]),
            run=RunManifest(run_id="seed", mode=RunMode.LIVE),
        )
    reports = loop.run("stock", "AAPL", objective="刷新", now=NOW)
    assert reports, "有 stale 字段就必须跑研究轮（不得零轮收敛）"
    assert loop.stop_reason != "converged" or reports
