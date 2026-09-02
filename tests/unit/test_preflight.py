"""数据源预检（gateway/preflight，research-capability-upgrade §4.9）的契约测试。

覆盖：
- DataGateway.preflight 聚合各 adapter 探活结果 + 落事件；未实现 healthcheck 的结构宽容
- CommandRunner 启动前预检：关键源挂 → blocked 拒启动并明示（三通道）；
  回退组内单源挂（stooq 反爬常态）→ 降级继续但留痕；全灭 → blocked
"""

import logging
from pathlib import Path

import pytest
from test_commands import wait_for

from finance_agent.commands.registry import parse_command
from finance_agent.commands.runner import CommandRequest, CommandRunner
from finance_agent.commands.steps import StepDeps
from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.adapters.fixture import FixtureAdapter
from finance_agent.gateway.gateway import DataGateway
from finance_agent.gateway.models import DataRecord, SourceCapability
from finance_agent.harness.approvals import ApprovalService
from finance_agent.knowledge.models import PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM


def _progress_script(source_id: str):
    """一轮有进展的研究脚本（quote 直接取自查寻记录 chunk 的 JSON 文本）。
    零产出 stalled = blocked（§4.3），要验证「预检放行后管道走通」必须有真实写入。"""
    return [
        AssistantReply(content="", tool_calls=[ToolCall(
            call_id="q1", name=f"query_{source_id}", arguments={"ticker": "BE"})]),
        AssistantReply(content="", tool_calls=[ToolCall(call_id="r1", name="register_evidence",
            arguments={"evidence_id": "ev-1", "chunk_id": "chk-0001", "verbatim_quote": "10-K"})]),
        AssistantReply(content="", tool_calls=[ToolCall(call_id="p1", name="propose_fact",
            arguments={"field": "business_model", "value": "申报驱动业务", "evidence_ids": ["ev-1"]})]),
        AssistantReply(content="done"),
    ]


def _fixture(source_id: str, *, health_error: str | None = None) -> FixtureAdapter:
    # C 级：预检测试不关 PIT 语义；C 级允许 available_at=None（A/B 级会被证据校验拒）
    adapter = FixtureAdapter(
        SourceCapability(
            source_id=source_id, pit_grade=PitGrade.C,
            server_side_asof=False, description=f"夹具 {source_id}",
        ),
        records=[DataRecord(source_id=source_id, payload={"form": "10-K"}, url="demo://f")],
    )
    adapter.health_error = health_error
    return adapter


def make_deps(tmp_path: Path, *adapters: FixtureAdapter, script: list | None = None):
    events = EventStore(tmp_path / "e.db")
    kb = BitemporalStore(tmp_path / "kb.db")
    writer = ProfileWriter(store=kb, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-t")
    for a in adapters:
        gateway.register(a)
    deps = StepDeps(
        events=events,
        kb=kb,
        writer=writer,
        gateway=gateway,
        decisions=DecisionService(kb=kb, decisions=DecisionStore(tmp_path / "d.db"), events=events),
        llm_for=lambda role: MockLLM(script or [AssistantReply(content="done")]),
        approvals=ApprovalService(events),
        evals_dir=tmp_path / "evals",
        reports_dir=tmp_path / "reports",
        knowledge_dir=tmp_path / "knowledge",
        max_rounds=1,
    )
    return deps, events


class TestGatewayPreflight:
    def test_aggregates_results_and_emits_event(self, tmp_path):
        deps, events = make_deps(
            tmp_path, _fixture("edgar"), _fixture("prices", health_error="限流 429")
        )
        report = deps.gateway.preflight(run_id="live-s1")

        assert report["edgar"]["ok"] is True
        assert report["prices"]["ok"] is False and "限流" in report["prices"]["detail"]
        emitted = [e for e in events.read("live-s1") if e.type == "gateway/preflight"]
        assert emitted and emitted[0].payload["results"]["prices"]["ok"] is False

    def test_adapter_without_healthcheck_passes_by_default(self, tmp_path):
        class BareAdapter:  # 结构化 adapter：无 healthcheck → 默认放行（不受阻）
            def capability(self):
                return SourceCapability(
                    source_id="bare", pit_grade=PitGrade.A, description="无探活"
                )

            def query(self, request, as_of=None):
                return []

        deps, _ = make_deps(tmp_path)
        deps.gateway.register(BareAdapter())
        report = deps.gateway.preflight()
        assert report["bare"]["ok"] is True and "默认放行" in report["bare"]["detail"]

    def test_healthcheck_exception_contained(self, tmp_path):
        class BoomAdapter:
            def capability(self):
                return SourceCapability(
                    source_id="boom", pit_grade=PitGrade.A, description="抛异常的探活"
                )

            def query(self, request, as_of=None):
                return []

            def healthcheck(self):
                raise ConnectionError("boom")

        deps, _ = make_deps(tmp_path)
        deps.gateway.register(BoomAdapter())
        report = deps.gateway.preflight()
        assert report["boom"]["ok"] is False and "boom" in report["boom"]["detail"]


class TestCommandPreflight:
    def _run(self, deps, events, text="/research BE"):
        runner = CommandRunner(deps, approval_timeout_s=0.2)
        parsed = parse_command(text)
        command_id = runner.start(CommandRequest(session_run_id="live-s1", parsed=parsed))
        return wait_for(
            events, "live-s1",
            lambda e: e.type == "command/done" and e.payload["command_id"] == command_id,
        )[0]

    def test_critical_source_down_blocks_before_any_step(self, tmp_path, caplog):
        """EDGAR 挂（唯一基本面源）→ 拒启动：不烧 token、不跑 step，明示哪个源。"""
        deps, events = make_deps(
            tmp_path, _fixture("edgar", health_error="HTTP 403"), _fixture("prices")
        )
        with caplog.at_level(logging.ERROR, logger="finance_agent.commands"):
            done = self._run(deps, events)
        assert done.payload["outcome"] == "blocked"
        assert "edgar" in done.payload["summary"] and "403" in done.payload["summary"]
        assert not [e for e in events.read("live-s1") if e.type == "step_agent/start"], \
            "预检拦截必须发生在任何 step 启动之前"
        # 三通道：事件（gateway/preflight 落会话流）+ 日志（caplog）+ 用户可见（done.summary）
        assert [e for e in events.read("live-s1") if e.type == "gateway/preflight"]
        assert any("预检拦截" in r.message for r in caplog.records)

    def test_redundant_group_single_failure_degrades_not_blocks(self, tmp_path, caplog):
        """行情回退组内 stooq 挂、yfinance 活 → 继续跑，但降级留痕进完成摘要。"""
        deps, events = make_deps(
            tmp_path,
            _fixture("edgar"),
            _fixture("prices"),
            _fixture("prices_stooq", health_error="JS PoW 反爬"),
            script=_progress_script("edgar"),
        )
        with caplog.at_level(logging.WARNING, logger="finance_agent.commands"):
            done = self._run(deps, events)
        assert done.payload["outcome"] != "blocked"
        assert "降级源" in done.payload["summary"] and "prices_stooq" in done.payload["summary"]
        assert any("预检降级" in r.message for r in caplog.records)

    def test_all_sources_down_blocks(self, tmp_path):
        deps, events = make_deps(tmp_path, _fixture("edgar", health_error="down"),
                                 _fixture("prices", health_error="down"))
        done = self._run(deps, events)
        assert done.payload["outcome"] == "blocked"
        assert "edgar" in done.payload["summary"] and "prices" in done.payload["summary"]

    def test_unregistered_sources_are_not_checked(self, tmp_path):
        """装配没注册的源不参与判定（demo 夹具场景：无 edgar/prices 也不拦）。"""
        deps, events = make_deps(tmp_path, _fixture("demo"), script=_progress_script("demo"))
        done = self._run(deps, events)
        assert done.payload["outcome"] != "blocked"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


class TestIndustryPreflightPolicy:
    """行业漏斗的命门是搜索源（不是行情/EDGAR）——预检按 command 类型分流（§4.9）。"""

    def _run_industry(self, deps, events):
        runner = CommandRunner(deps, approval_timeout_s=0.2)
        parsed = parse_command("/industry AI for Science")
        command_id = runner.start(CommandRequest(session_run_id="live-s1", parsed=parsed))
        return wait_for(
            events, "live-s1",
            lambda e: e.type == "command/done" and e.payload["command_id"] == command_id,
        )[0]

    def test_search_sources_down_blocks_industry(self, tmp_path):
        deps, events = make_deps(
            tmp_path,
            _fixture("edgar"),
            _fixture("web_search", health_error="key 失效"),
            _fixture("web_search_tavily", health_error="429"),
        )
        done = self._run_industry(deps, events)
        assert done.payload["outcome"] == "blocked"
        assert "web_search" in done.payload["summary"]

    def test_prices_down_does_not_block_industry(self, tmp_path):
        """行情组全灭对行业漏斗是降级而非拦截（定性调研不需要行情）。"""
        deps, events = make_deps(
            tmp_path,
            _fixture("web_search"),
            _fixture("prices", health_error="限流"),
            _fixture("prices_stooq", health_error="反爬"),
        )
        done = self._run_industry(deps, events)
        assert "数据源预检未通过" not in done.payload["summary"]
        assert "降级源" in done.payload["summary"]  # 降级留痕可见
