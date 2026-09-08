"""真实预算闸验收（audit §3.3 P0 + §5 验收用例 4「预算/慢组故障注入」）。

事故形态（live-a2cce641）：设计给了 deep 40 分钟 / 80 次检索 / 4 worker / 5 轮，
代码只消费轮数——研究阶段跑了 53 分 31 秒、外部检索 128 次（Exa 84 次）、
236 次模型调用无人扣减，慢 worker（单步最长 355 秒）拖住整轮屏障。

本组断言：预算在 LLM 与网关入口真实扣减、结束时间不超过 deadline + 取消容差、
慢组超时只影响所属问题、缺 usage 不按零计费、停止原因可归因到具体维度。
"""

import time
from datetime import UTC, datetime

import pytest
from test_research_loop import make_loop as make_stock_loop

from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.adapters.fixture import FixtureAdapter
from finance_agent.gateway.gateway import DataGateway
from finance_agent.gateway.models import DataRecord, SourceCapability
from finance_agent.gateway.tools import make_gateway_tool
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.models import Evidence, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.loop.kernel import AgentKernel
from finance_agent.research.budget import RunBudget, budget_for_mode
from finance_agent.research.evidence_desk import ChunkStore

NOW = datetime(2024, 6, 1, tzinfo=UTC)


def tc(i: int, name: str, args: dict) -> ToolCall:
    return ToolCall(call_id=f"c{i}", name=name, arguments=args)


class FakeClock:
    """可推进的单调时钟（预算墙钟注入点）。"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ---------------- 1. RunBudget 单元：扣减、耗尽、timeout 钳制 ----------------


class TestRunBudgetUnit:
    def test_wall_clock_exhaustion_is_attributable(self):
        clock = FakeClock()
        b = RunBudget(wall_clock_minutes=1, monotonic=clock)
        b.start()
        assert b.exhausted() == []
        clock.advance(61)
        assert b.exhausted() == ["wall_clock"]
        assert b.remaining_seconds() == 0.0

    def test_synthesis_reserve_shortens_research_window(self):
        """预算末段预留给部分成果合成（deep：40 分钟里留 6 分钟）。"""
        b = RunBudget(wall_clock_minutes=40, synthesis_reserve_minutes=6)
        assert b.seconds_limit() == pytest.approx(34 * 60)
        assert b.reserve_seconds == pytest.approx(360)

    def test_llm_timeout_never_exceeds_remaining(self):
        clock = FakeClock()
        b = RunBudget(wall_clock_minutes=1, llm_timeout_cap=300.0, monotonic=clock)
        b.start()
        # 剩 60 秒 → 单请求 timeout 钳到 60（不超剩余墙钟）
        assert b.llm_timeout(300.0) == pytest.approx(60.0)
        clock.advance(59)  # 剩 1 秒
        assert b.llm_timeout(300.0) == pytest.approx(5.0)  # 地板：不发必然超时的请求

    def test_retrieval_budget_denies_with_readable_reason(self):
        b = RunBudget(retrieval_calls=2)
        assert b.admit_retrieval()[0]
        assert b.admit_retrieval()[0]
        ok, reason = b.admit_retrieval()
        assert not ok
        assert "检索预算耗尽 retrieval_calls（2/2）" in reason
        assert "retrieval_calls" in b.exhausted()

    def test_missing_usage_is_estimated_not_zero(self):
        """缺 usage 不能按零计费（audit §3.3）：按上下文长度估算并单独计数。"""
        b = RunBudget(tokens=10_000)
        assert b.record_usage(None, context_chars=4000) == 1000
        snap = b.snapshot()
        assert snap.tokens_used == 1000
        assert snap.tokens_estimated == 1000
        assert b.record_usage({"prompt_tokens": 10, "completion_tokens": 5},
                              context_chars=9999) == 15
        assert b.snapshot().tokens_estimated == 1000  # 真实 usage 不进估算计数

    def test_token_budget_exhausts_and_blocks_next_call(self):
        b = RunBudget(tokens=100)
        b.record_usage({"total_tokens": 120})
        assert "tokens" in b.exhausted()
        assert not b.admit_llm_call()[0]

    def test_retry_gate_denies_when_budget_gone(self):
        clock = FakeClock()
        b = RunBudget(retries=1, wall_clock_minutes=1, monotonic=clock)
        assert b.admit_retry()
        assert not b.admit_retry()
        clock.advance(120)
        b2 = RunBudget(retries=10, wall_clock_minutes=1, monotonic=clock)
        b2.start()
        clock.advance(120)
        assert not b2.admit_retry()  # 墙钟耗尽 → 不再退避重试

    def test_deep_mode_budgets_come_from_frozen_plan(self):
        plan = {"mode": "deep", "budgets": {"wall_clock_minutes": 40, "retrieval_calls": 80,
                                            "max_parallel_workers": 4, "max_rounds": 5}}
        b = budget_for_mode("deep", plan)
        assert b.wall_clock_minutes == 40
        assert b.retrieval_calls == 80
        assert b.seconds_limit() == pytest.approx((40 - 6) * 60)
        assert b.llm_timeout_cap == 240.0

    def test_describe_is_human_readable(self):
        b = RunBudget(wall_clock_minutes=1, retrieval_calls=80)
        b.start()
        b.admit_retrieval()
        text = b.describe()
        assert "墙钟" in text and "检索 1/80" in text


# ---------------- 2. kernel：入口扣减 + 停止可见 ----------------


def _kernel_env(tmp_path, llm, *, budget=None, tools=None):
    events = EventStore(tmp_path / "e.db")
    kernel = AgentKernel(
        store=events, llm=llm, manifest=RunManifest(run_id="k1", mode=RunMode.LIVE),
        tools=tools or {}, budget=budget, max_steps=4,
    )
    return kernel, events


class TestKernelBudget:
    def test_exhausted_budget_denies_llm_call_and_records_reason(self, tmp_path):
        clock = FakeClock()
        budget = RunBudget(wall_clock_minutes=1, monotonic=clock)
        budget.start()
        clock.advance(120)
        llm = MockLLM([AssistantReply(content="不该被调用")])
        kernel, events = _kernel_env(tmp_path, llm, budget=budget)
        kernel.run_turn("研究")
        assert kernel.budget_stop and "wall_clock" in kernel.budget_stop
        assert not llm.received, "预算耗尽仍发出了模型请求"
        denied = [e for e in events.read("k1") if e.type == "research/budget"]
        assert denied and denied[0].payload["action"] == "llm_call_denied"
        assert denied[0].payload["budget"]["exhausted"] == ["wall_clock"]

    def test_request_timeout_is_clamped_to_remaining(self, tmp_path):
        clock = FakeClock()
        budget = RunBudget(wall_clock_minutes=1, llm_timeout_cap=300.0, monotonic=clock)
        budget.start()
        clock.advance(50)  # 剩 10 秒

        class TimeoutLLM(MockLLM):
            timeout = 300.0
            applied: list[float] = []

            def set_request_timeout(self, cap):
                self.applied.append(cap)
                self.timeout = min(self.timeout, cap)

        llm = TimeoutLLM([AssistantReply(content="ok")])
        kernel, _ = _kernel_env(tmp_path, llm, budget=budget)
        kernel.run_turn("研究")
        assert llm.applied and llm.applied[0] == pytest.approx(10.0)

    def test_retry_gate_is_pushed_into_llm(self, tmp_path):
        budget = RunBudget(retries=3)
        gates: list = []

        class GateLLM(MockLLM):
            def set_retry_gate(self, gate):
                gates.append(gate)

        kernel, _ = _kernel_env(tmp_path, GateLLM([AssistantReply(content="ok")]), budget=budget)
        kernel.run_turn("研究")
        assert gates and gates[0]() is True
        assert budget.snapshot().retries_used == 1

    def test_tool_budget_denies_execution_with_error_content(self, tmp_path):
        called: list[str] = []
        budget = RunBudget(tool_calls=1)
        tools = {"noop": lambda args: called.append("x") or {"content": "ok", "provenance": []}}
        llm = MockLLM([
            AssistantReply(content="", tool_calls=[tc(1, "noop", {}), tc(2, "noop", {})]),
            AssistantReply(content="done"),
        ])
        kernel, events = _kernel_env(tmp_path, llm, budget=budget, tools=tools)
        kernel.run_turn("研究")
        assert len(called) == 1, "工具预算耗尽后仍执行了工具"
        results = [e for e in events.read("k1") if e.type == "tool/result"]
        assert any("工具调用预算耗尽" in r.payload["content"] for r in results)
        assert kernel.budget_stop and "tool_calls" in kernel.budget_stop

    def test_turn_end_carries_budget_snapshot(self, tmp_path):
        budget = RunBudget(wall_clock_minutes=5, retrieval_calls=10)
        kernel, events = _kernel_env(
            tmp_path, MockLLM([AssistantReply(content="ok", usage={"total_tokens": 42})]),
            budget=budget,
        )
        kernel.run_turn("研究")
        end = [e for e in events.read("k1") if e.type == "turn/end"][0]
        assert end.payload["budget"]["tokens_used"] == 42
        assert end.payload["budget"]["llm_calls"] == 1


# ---------------- 3. 网关入口：检索预算 + 去重 + 按需正文 ----------------


def _gateway(tmp_path):
    events = EventStore(tmp_path / "e.db")
    gw = DataGateway(mode="live", events=events, run_id="g1")
    adapter = FixtureAdapter(
        SourceCapability(source_id="demo", pit_grade=PitGrade.A, server_side_asof=False,
                         description="夹具源"),
        records=[DataRecord(source_id="demo",
                            payload={"title": "报告", "text": "营收 1234 million 元。" * 40},
                            url="demo://r", available_at=NOW)],
    )
    gw.register(adapter)
    return gw, adapter, events


class TestGatewayBudget:
    def test_retrieval_budget_blocks_at_gateway_entry(self, tmp_path):
        gw, adapter, _ = _gateway(tmp_path)
        budget = RunBudget(retrieval_calls=1)
        tool = make_gateway_tool(gw, "demo", ChunkStore(), budget=budget)
        assert "报告" in tool({"q": 1})["content"]
        out = tool({"q": 2})
        assert out["content"].startswith("rejected:")
        assert out.get("budget_denied") is True
        assert len(adapter.seen_as_of) == 1, "预算耗尽后仍打了外部源"

    def test_duplicate_request_served_from_cache(self, tmp_path):
        """同 run 重复请求复用结果：不重复扣预算、不重复撑大上下文。"""
        gw, adapter, _ = _gateway(tmp_path)
        budget = RunBudget(retrieval_calls=5)
        cache: dict = {}
        tool = make_gateway_tool(gw, "demo", ChunkStore(), budget=budget, cache=cache)
        first = tool({"q": "same"})
        second = tool({"q": "same"})
        assert second.get("cached") is True
        assert second["content"] == first["content"]
        assert len(adapter.seen_as_of) == 1
        assert budget.snapshot().retrieval_calls == 1
        assert budget.snapshot().duplicate_retrievals == 1

    def test_long_record_text_is_clamped_with_bundle_pointer(self, tmp_path):
        """按需 evidence bundle：长正文截断 + 明确告知用 read_chunk 取全文。"""
        gw, adapter, _ = _gateway(tmp_path)
        store = ChunkStore()
        tool = make_gateway_tool(gw, "demo", store, max_record_chars=200)
        content = tool({})["content"]
        assert '"text_truncated": true' in content
        assert "read_chunk(chunk_id=chk-0001)" in content
        # 台账里仍是完整规范正文（截断只发生在传输层，不影响证据校验）
        chunk = store.get("chk-0001")
        assert chunk is not None and len(chunk.text) > 200
        assert chunk.spans, "chunk 必须切出稳定 span 供引用"

    def test_chunk_store_dedups_identical_content(self, tmp_path):
        store = ChunkStore()
        a = store.add(source_id="s", text="同一段正文 1234 million", url=None,
                      available_at=NOW, pit_grade=PitGrade.A)
        b = store.add(source_id="s", text="同一段正文 1234 million", url=None,
                      available_at=NOW, pit_grade=PitGrade.A)
        assert a == b
        assert store.duplicates == 1
        c = store.add(source_id="s", text="另一段正文", url=None, available_at=NOW,
                      pit_grade=PitGrade.A)
        assert c != a


# ---------------- 4. 端到端：故障注入（超时/慢组/取消/partial 保留） ----------------


class SlowLLM:
    """慢 worker：单步长时间等待（复现 key_kpi 组拖住整轮的尾部等待）。"""

    model_name = "slow"

    def __init__(self, seconds: float):
        self._seconds = seconds

    def complete(self, messages, tools):
        time.sleep(self._seconds)
        return AssistantReply(content="slow done", tool_calls=[],
                              usage={"prompt_tokens": 1, "completion_tokens": 1,
                                     "total_tokens": 2})


class TestLoopBudgetInjection:
    def test_wall_clock_deadline_stops_research_and_keeps_partial(self, tmp_path):
        """墙钟耗尽即停：不进入新一轮，已落库成果保留（partial），原因可归因。"""
        clock = FakeClock()
        loop, kb, events = make_stock_loop(tmp_path, MockLLM([]), max_rounds=5)
        budget = RunBudget(wall_clock_minutes=1, monotonic=clock, now=lambda: NOW)
        budget.start()
        # 先写入一项部分成果（模拟上一轮已有产出）
        kb.add_evidence(Evidence(
            evidence_id="ev-partial", source_id="demo", verbatim_quote="partial 100 million",
            retrieved_at=NOW, available_at=NOW, pit_grade=PitGrade.A,
        ))
        from finance_agent.knowledge.models import Fact

        loop._writer.write_fact(  # noqa: SLF001
            Fact(entity_kind="stock", entity_id="AAPL", field="revenue_fy",
                 value="100 million", knowledge_time=NOW, evidence_ids=["ev-partial"]),
            run=RunManifest(run_id="live-1", mode=RunMode.LIVE),
        )
        loop._run_budget = budget  # noqa: SLF001 - 注入预算（装配层走同一参数）
        clock.advance(120)  # 墙钟耗尽

        reports = loop.run("stock", "AAPL", objective="研究", now=NOW)
        assert reports == [], "预算耗尽后不应再跑一轮"
        assert loop.stop_reason == "budget"
        assert loop.budget_exhausted == ["wall_clock"]
        stopped = [e for e in events.read("live-1") if e.type == "research/budget"
                   and e.payload.get("action") == "research_stopped"]
        assert stopped and stopped[0].payload["reason"] == "wall_clock"
        assert stopped[0].payload["budget"]["exhausted"] == ["wall_clock"]
        # partial 保留：已写入的事实不被预算终止清掉
        assert kb.as_of("stock", "AAPL", NOW)["revenue_fy"].value == "100 million"
        assert loop.budget_snapshot["exhausted"] == ["wall_clock"]

    def test_slow_group_does_not_block_the_round(self, tmp_path):
        """慢组超时只影响所属问题：本轮在 deadline + 容差内返回，其余组成果保留。"""
        from finance_agent.llm.base import AssistantReply as R

        fast = MockLLM([
            R(content="", tool_calls=[tc(1, "query_edgar", {"ticker": "AAPL"})]),
            R(content="", tool_calls=[tc(2, "register_evidence", {
                "evidence_id": "ev-fast", "chunk_id": "chk-0001",
                "verbatim_quote": "10-K"})]),
            R(content="", tool_calls=[tc(3, "propose_fact", {
                "field": "moat", "value": "快组成果", "evidence_ids": ["ev-fast"]})]),
            R(content="done"),
        ] * 4)
        loop, kb, events = make_stock_loop(tmp_path, MockLLM([]), max_rounds=1)
        loop._worker_llms = [SlowLLM(0.8), fast]  # noqa: SLF001
        loop._run_budget = RunBudget(  # noqa: SLF001
            wall_clock_minutes=0.005, synthesis_reserve_minutes=0,  # 0.3 秒
            monotonic=time.monotonic,
        )
        started = time.monotonic()
        reports = loop.run("stock", "AAPL", objective="研究", now=NOW)
        elapsed = time.monotonic() - started
        assert elapsed < 0.75, f"整轮屏障未被打破：耗时 {elapsed:.2f}s"
        assert reports, "超时不应丢掉本轮报告"
        timeouts = [e for e in events.read("live-1") if e.type == "research/budget"
                    and e.payload.get("action") == "group_timeout"]
        assert timeouts, "慢组超时未落事件（不可见）"
        assert timeouts[0].payload["groups"]

    def test_retrieval_budget_is_enforced_end_to_end(self, tmp_path):
        """80 次检索上限真实生效：耗尽后工具返回可读拒绝，不再打外部源。"""
        replies = []
        for i in range(4):
            replies.append(AssistantReply(
                content="", tool_calls=[tc(i, "query_edgar", {"ticker": f"AAPL-{i}"})]))
        replies.append(AssistantReply(content="done"))
        loop, kb, events = make_stock_loop(tmp_path, MockLLM(replies), max_rounds=1)
        loop._run_budget = RunBudget(retrieval_calls=2)  # noqa: SLF001
        loop.run("stock", "AAPL", objective="研究", now=NOW)
        results = [e for e in events.read("live-1") if e.type == "tool/result"]
        denied = [r for r in results if "检索预算耗尽" in str(r.payload.get("content", ""))]
        assert denied, "检索预算未在网关入口生效"
        assert loop.budget_snapshot["retrieval_calls"] == 2

    def test_repeated_identical_query_does_not_consume_budget(self, tmp_path):
        """重复请求走去重：既不扣检索预算也不重复撑大上下文（audit §3.3）。"""
        replies = [
            AssistantReply(content="", tool_calls=[
                tc(i, "query_edgar", {"ticker": "AAPL"})]) for i in range(4)
        ]
        replies.append(AssistantReply(content="done"))
        loop, kb, events = make_stock_loop(tmp_path, MockLLM(replies), max_rounds=1)
        loop._run_budget = RunBudget(retrieval_calls=2)  # noqa: SLF001
        loop.run("stock", "AAPL", objective="研究", now=NOW)
        assert loop.budget_snapshot["retrieval_calls"] == 1
        assert loop.budget_snapshot["duplicate_retrievals"] == 3  # 4 次同请求：1 次真打 + 3 次命中
        results = [e for e in events.read("live-1") if e.type == "tool/result"]
        assert not [r for r in results if "检索预算耗尽" in str(r.payload.get("content", ""))]

    def test_cancel_still_wins_over_budget(self, tmp_path):
        """取消闸优先于预算：用户停止 → stop_reason=cancelled，已落库成果保留。"""
        loop, kb, events = make_stock_loop(tmp_path, MockLLM([]), max_rounds=3)
        loop._should_stop = lambda: True  # noqa: SLF001
        loop._run_budget = RunBudget(wall_clock_minutes=1)  # noqa: SLF001
        loop.run("stock", "AAPL", objective="研究", now=NOW)
        assert loop.stop_reason == "cancelled"


# ---------------- 5. 重试预算（router 层） ----------------


class TestRetryBudget:
    def test_retry_gate_stops_backoff_immediately(self):
        from finance_agent.llm.router import _with_retry

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            raise ConnectionError("boom")

        gate_calls = {"n": 0}

        def gate():
            gate_calls["n"] += 1
            return False

        with pytest.raises(ConnectionError):
            _with_retry(flaky, attempts=4, gate=gate)
        assert calls["n"] == 1, "重试预算耗尽后仍在重试"
        assert gate_calls["n"] == 1

    def test_evidence_preserved_after_budget_stop(self, tmp_path):
        """预算终止不清库：已登记证据与事实全部保留（partial 交付的物质基础）。"""
        kb = BitemporalStore(tmp_path / "kb.db")
        events = EventStore(tmp_path / "e.db")
        writer = ProfileWriter(store=kb, events=events)
        kb.add_evidence(Evidence(
            evidence_id="ev-keep", source_id="demo", verbatim_quote="kept 100 million",
            retrieved_at=NOW, available_at=NOW, pit_grade=PitGrade.A,
        ))
        from finance_agent.knowledge.models import Fact

        writer.write_fact(
            Fact(entity_kind="stock", entity_id="AAPL", field="revenue_fy",
                 value="100 million", knowledge_time=NOW, evidence_ids=["ev-keep"]),
            run=RunManifest(run_id="live-1", mode=RunMode.LIVE),
        )
        loop = make_stock_loop(tmp_path, MockLLM([]), max_rounds=1)[0]
        clock = FakeClock()
        loop._run_budget = RunBudget(wall_clock_minutes=1, monotonic=clock)  # noqa: SLF001
        loop._run_budget.start()  # noqa: SLF001
        clock.advance(120)
        loop.run("stock", "AAPL", objective="研究", now=NOW)
        assert loop.stop_reason == "budget"
        assert kb.as_of("stock", "AAPL", NOW)["revenue_fy"].value == "100 million"
