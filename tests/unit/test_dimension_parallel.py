"""P3 维度并行 loop + 动态预算 + playbook 加载器的契约测试。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_research_loop import NOW, make_loop

from finance_agent.commands.steps import theme_slug
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.research.loop import _dimension_groups
from finance_agent.research.playbooks import load_playbook


def tc(i: int, name: str, args: dict) -> ToolCall:
    return ToolCall(call_id=f"c{i}", name=name, arguments=args)


class SmartResearcher:
    """会读工具结果的 mock researcher：并行组共享 ChunkStore 时 chunk 编号交错，
    硬编码 chk-id 必然撞车——真实模型是从工具结果里读 chunk_id 的，mock 同理。
    流程：query_edgar → 从结果读 chunk_id → register_evidence → propose_fact → done。"""

    def __init__(self, field: str):
        self._field = field
        self._calls = 0
        self.model_name = f"smart-{field}"

    def complete(self, messages, tools):
        import re

        self._calls += 1
        n = self._calls
        if n == 1:
            return AssistantReply(
                content="", tool_calls=[tc(1, "query_edgar", {"ticker": "AAPL"})])
        # 从最近的 tool 结果里读 chunk_id（真实模型的做法）
        chunk_id = None
        for m in reversed(messages):
            if m.get("role") != "tool":
                continue
            found = re.findall(r'"chunk_id":\s*"(chk-\d+)"', str(m.get("content", "")))
            if found:
                chunk_id = found[-1]
                break
        if n == 2 and chunk_id:
            return AssistantReply(content="", tool_calls=[tc(2, "register_evidence", {
                "evidence_id": f"ev-{self._field}", "chunk_id": chunk_id,
                "verbatim_quote": "10-K"})])
        if n == 3:
            return AssistantReply(content="", tool_calls=[tc(3, "propose_fact", {
                "field": self._field, "value": f"{self._field} 已研",
                "evidence_ids": [f"ev-{self._field}"]})])
        return AssistantReply(content="done")


class TestDimensionGroups:
    def test_fields_route_to_groups(self):
        groups = dict(
            _dimension_groups(
                ["revenue_fy", "moat", "peers", "management"],
                ["cash_flow"],
                ["market_share", "talent_density"],
            )
        )
        assert {"revenue_fy", "cash_flow"} <= set(groups["financial"])
        assert groups["business"] == ["moat"]
        assert set(groups["industry"]) == {"peers", "market_share"}
        # talent_density 已登记进 risk_mgmt 组（与 management 同根，都是「人」的维度）
        assert set(groups["risk_mgmt"]) == {"management", "talent_density"}

    def test_weak_fields_attach_to_active_groups_only(self):
        """弱字段回流：挂进因缺口已激活的本维度组；不新建组、不无中生有研究。

        weak 是引导不是缺口——moat 虽弱但 business 组未激活就不回流；
        财务组的 net_income_fy 顺带进 financial 组交专职研究员重写。
        """
        groups = dict(
            _dimension_groups(["revenue_fy"], [], [], weak=["net_income_fy", "moat", "peers"])
        )
        assert set(groups["financial"]) == {"revenue_fy", "net_income_fy"}
        # 未激活的维度组不因 weak 而创建，并行面不扩大
        assert list(groups) == ["financial"]

        # 全无缺口时 weak 不触发任何组（收敛判据不受 weak 影响）
        assert _dimension_groups([], [], [], weak=["moat"]) == []

    def test_parallel_groups_isolated_and_both_write(self, tmp_path):
        """两 worker 并行：各自维度组写入互不干扰；group_end 事件留痕；chunk 编号不撞车。"""
        loop, kb, events = make_loop(tmp_path, MockLLM([]), max_rounds=1)
        # 并行池：两个 SmartResearcher（各自从工具结果读 chunk_id，抗交错）
        loop._worker_llms = [SmartResearcher("revenue_fy"), SmartResearcher("moat")]  # noqa: SLF001
        # 只留 financial + business 两组缺口：把其余字段先写满（种子事实绑种子证据）
        from finance_agent.harness.manifest import RunManifest, RunMode
        from finance_agent.knowledge.models import Evidence, Fact, PitGrade

        old = datetime(2024, 3, 1, tzinfo=UTC)
        kb.add_evidence(Evidence(
            evidence_id="ev-old", source_id="edgar", url="demo://x",
            verbatim_quote="x", retrieved_at=old, available_at=old, pit_grade=PitGrade.A,
        ))
        writer = loop._writer  # noqa: SLF001
        for f in ("net_income_fy", "cash_flow", "valuation", "risks", "peers",
                  "business_model", "management", "catalysts", "counter_evidence",
                  "future_space", "market_share", "talent_density"):
            writer.write_fact(
                Fact(entity_kind="stock", entity_id="AAPL", field=f, value="x",
                     knowledge_time=old, evidence_ids=["ev-old"], run_id="seed"),
                run=RunManifest(run_id="seed", mode=RunMode.LIVE),
            )
        reports = loop.run("stock", "AAPL", objective="研究", now=NOW)

        # 两组并行各写一字段
        assert set(reports[0].facts_written) >= {"revenue_fy", "moat"}
        group_events = [e for e in events.read("live-1") if e.type == "research/group_end"]
        assert {g.payload["group"] for g in group_events} >= {"financial", "business"}
        # 组间 chunk 编号不撞车（共享台账锁）：两个 chk 都被正确解析（无串台拒绝）
        rejs = [r for r in reports[0].rejected if "chunk" in str(r.get("reason", ""))]
        assert not rejs

    def test_worker_failure_isolated(self, tmp_path):
        """单组 worker 抛错不拖死整轮：其余组照常写入，失败组进 rejected 可见。"""
        biz = [
            AssistantReply(content="", tool_calls=[tc(0, "query_edgar", {"ticker": "AAPL"})]),
            AssistantReply(content="", tool_calls=[tc(1, "read_edgar_filing",
                                                      {"chunk_id": "chk-0001", "query": "ecosystem"})]),
            AssistantReply(content="", tool_calls=[tc(2, "register_evidence", {
                "evidence_id": "ev-biz", "chunk_id": "chk-0002",
                "verbatim_quote": "ecosystem lock-in"})]),
            AssistantReply(content="", tool_calls=[tc(3, "propose_fact", {
                "field": "moat", "value": "生态锁定", "evidence_ids": ["ev-biz"]})]),
            AssistantReply(content="biz done"),
        ]

        class BoomLLM:
            def complete(self, messages, tools):
                raise ConnectionError("worker 挂了")

            model_name = "boom"

        loop, kb, _ = make_loop(tmp_path, MockLLM([]), max_rounds=1)
        loop._worker_llms = [BoomLLM(), MockLLM(biz)]  # noqa: SLF001
        reports = loop.run("stock", "AAPL", objective="研究", now=NOW)
        assert "moat" in reports[0].facts_written
        assert any("group:" in str(r.get("field", "")) for r in reports[0].rejected)


class TestDynamicBudget:
    def test_zero_completeness_gets_five_rounds(self, tmp_path):
        """0% 档案 → 5 轮预算（每轮有微小进展但永不收敛时烧满预算才停）。"""
        # 每轮写一个非 schema 字段（有进展但完整度不动 → 预算耗尽停）
        replies = []
        for i in range(5):
            replies += [
                AssistantReply(content="", tool_calls=[tc(i * 2, "query_edgar", {"ticker": "AAPL"})]),
                AssistantReply(content="", tool_calls=[tc(i * 2 + 1, "register_evidence", {
                    "evidence_id": f"ev-b{i}", "chunk_id": "chk-0001", "verbatim_quote": "10-K"})]),
                # 注：propose 一个非必填字段（capacity v{i}）保证 progress=True
                AssistantReply(content="", tool_calls=[tc(100 + i, "propose_fact", {
                    "field": f"note_{i}", "value": f"笔记{i}", "evidence_ids": [f"ev-b{i}"]})]),
                AssistantReply(content=f"round {i} done"),
            ]
        loop, _, events = make_loop(tmp_path, MockLLM(replies), max_rounds=None)  # 动态预算
        loop.run("stock", "AAPL", objective="研究", now=NOW)
        assert loop.stop_reason == "budget"
        assert len(events.read("live-1", types={"research/round_end"})) == 5

    def test_explicit_max_rounds_overrides_dynamic(self, tmp_path):
        """显式 max_rounds 优先（测试/装配的可控接缝）。"""
        llm = MockLLM([AssistantReply(content="没有新发现")])
        loop, _, _ = make_loop(tmp_path, llm, max_rounds=2)
        loop.run("stock", "AAPL", objective="研究", now=NOW)
        assert loop.stop_reason == "stalled"  # 一轮零写入即停滞（预算未到也用不上）


class TestPlaybooks:
    def test_loads_repo_file_with_hash(self):
        text, ver = load_playbook("industry_map")
        assert "赛道" in text and len(ver) == 8

    def test_missing_file_falls_back_to_builtin(self, monkeypatch):
        from finance_agent.research import playbooks

        monkeypatch.setattr(playbooks, "_PLAYBOOK_DIR", Path("/nonexistent"))
        text, ver = load_playbook("screen")
        assert text == playbooks._BUILTIN["screen"] and len(ver) == 8

    def test_unknown_name_fail_loud(self):
        with pytest.raises(ValueError, match="未知 playbook"):
            load_playbook("nope")


def test_theme_slug():
    assert theme_slug("AI for Science") == "ai-for-science"
    assert theme_slug("AI for Science 赛道") == "ai-for-science-赛道"
    assert theme_slug("  ") == "industry"
