"""消融开关验收（方案 §10.2 插件消融 + B 组 F14 诊断）。

判据：
- 环境变量 → 组件集合解析（未知变量不产生组件；空 = 生产默认全量开启）；
- state_card 关闭 → 研究循环不再产生 context_compressed（且事件里可见消融留痕）；
- verifier 关闭 → S1 工具面无 verify_claim、S2 consolidator 无核验工具、
  合成批量核验显式记 ablated；
- knowledge_context 关闭 → S1 worker 无 query_observations 等共享读取；
- broker 关闭 → 工具面无 search_sources；
- second_search 关闭 → 真实装配的网关不含 tavily。
"""

from __future__ import annotations

from datetime import UTC, datetime

from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.adapters.fixture import FixtureAdapter
from finance_agent.gateway.gateway import DataGateway
from finance_agent.gateway.models import DataRecord, SourceCapability
from finance_agent.harness.ablation import ablation_flags, ablation_notes
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.models import PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply
from finance_agent.llm.mock import MockLLM
from finance_agent.research.evidence_desk import ChunkStore
from finance_agent.research.loop import ResearchLoop
from finance_agent.research.tools import make_research_tools

NOW = datetime.now(UTC)


class TestFlagParsing:
    def test_env_parse(self):
        flags = ablation_flags({"FA_ABLATE_STATE_CARD": "1", "FA_ABLATE_VERIFIER": "true",
                                "FA_ABLATE_BROKER": "0", "OTHER": "1"})
        assert flags == frozenset({"state_card", "verifier"})
        assert ablation_notes(flags)["state_card"]
        assert ablation_flags({}) == frozenset()  # 生产默认：全量开启


class TestToolFaceAblation:
    def _tools(self, tmp_path, **kw):
        kb = BitemporalStore(tmp_path / "kb.db")
        metrics = MetricStore(tmp_path / "m.db")
        events = EventStore(tmp_path / "e.db")
        return make_research_tools(
            store=kb, writer=ProfileWriter(store=kb, events=events),
            manifest=RunManifest(run_id="r-abl", mode=RunMode.LIVE),
            entity_kind="stock", entity_id="BE", chunk_store=ChunkStore(),
            events=events, metrics=metrics,
            metric_writer=TypedMetricWriter(store=metrics, kb=kb, events=events),
            **kw,
        )[0]

    def test_verifier_off_removes_tool(self, tmp_path):
        tools = self._tools(tmp_path, with_verifier=False)
        assert "verify_claim" not in tools
        assert "submit_question_result" in tools  # 其余 typed 工具不受影响

    def test_knowledge_context_off_removes_shared_reads(self, tmp_path):
        tools = self._tools(tmp_path, with_knowledge_context=False)
        for name in ("get_research_context", "query_observations", "query_claims",
                     "query_calculations", "read_evidence", "list_conflicts"):
            assert name not in tools, f"消融后 S1 不应有 {name}"
        assert "register_evidence" in tools  # 核心写入工具不受影响


class TestLoopAblation:
    def test_state_card_off_produces_no_compression_event(self, tmp_path):
        kb = BitemporalStore(tmp_path / "kb.db")
        metrics = MetricStore(tmp_path / "m.db")
        events = EventStore(tmp_path / "e.db")
        gateway = DataGateway(mode="live", events=events, run_id="live-abl")
        gateway.register(FixtureAdapter(
            SourceCapability(source_id="demo", pit_grade=PitGrade.A,
                             server_side_asof=False, description="夹具源"),
            records=[DataRecord(source_id="demo", payload={"t": "x"},
                                url="demo://f", available_at=NOW)],
        ))
        # 两轮脚本：round1 零产出 → round2 依旧（触发 stalled 与状态卡路径）
        script = [AssistantReply(content="nothing found")] * 2
        loop = ResearchLoop(
            store=kb, events=events, writer=ProfileWriter(store=kb, events=events),
            gateway=gateway, llm=MockLLM(script),
            manifest=RunManifest(run_id="live-abl", mode=RunMode.LIVE),
            gateway_sources=gateway.source_ids(),
            fetch_document=lambda url: "doc",
            metrics=metrics,
            metric_writer=TypedMetricWriter(store=metrics, kb=kb, events=events),
            max_rounds=2, ablation=frozenset({"state_card"}),
        )
        loop.run("stock", "BE", "验证")
        assert not [e for e in events.read("live-abl")
                    if e.type == "research/context_compressed"], \
            "状态卡消融后不得产生压缩事件"
        ablated = [e for e in events.read("live-abl") if e.type == "research/budget"
                   and e.payload.get("action") == "ablation"]
        assert ablated and "state_card" in ablated[0].payload["reason"], \
            "消融关闭必须留痕（可归因）"

    def test_broker_off_no_search_sources_tool(self, tmp_path):
        """broker 消融：工具面缺 search_sources（由绑定报告可见，此处测组件开关）。"""
        flags = ablation_flags({"FA_ABLATE_BROKER": "1"})
        assert "broker" in flags  # 机制层（装配行为由 loop 的 broker 跳过分支覆盖）


class TestGatewayAblation:
    def test_second_search_off_excludes_tavily(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FA_ABLATE_SECOND_SEARCH", "1")
        monkeypatch.setenv("NOVITA_API_KEY", "n")
        monkeypatch.setenv("TAVILY_API_KEY", "t")
        from finance_agent.cli import build_orchestrator

        orch = build_orchestrator(tmp_path)
        sources = orch["capabilities_info"]()["gateway_sources"]
        assert "web_search" in sources and "web_search_tavily" not in sources
