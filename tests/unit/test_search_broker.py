"""SearchBroker 验收（tools-plugins 方案 §5.1，P1-A 输入能力）。

判据：
- 主备策略确定性：默认只打主源；主源报错/零召回/低召回才动备源（回退原因可见）；
  mode=dual 强制双源；
- 去重：canonical URL 相同 = 同一来源（保主源版本，PIT 更高）；正文同、URL 不同
  = 转载族（同一原始来源只算一个，family_size/family_urls 可见）；
- 预算：每个实际调用的引擎真实扣减一次检索预算；预算拒绝备源 → 主源结果照样返回
  （部分可见，不装死）；
- 工具层：逐条落检索台账（chunk_id 可引证据）、broker 元数据不污染台账正文、
  缓存命中不重复扣预算、空结果与错误区分；
- 装配：无搜索源注册时 maybe_make_search_broker → None（fail-closed 不装配）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.adapters.fixture import FixtureAdapter
from finance_agent.gateway.gateway import DataGateway
from finance_agent.gateway.models import DataRecord, SourceCapability
from finance_agent.gateway.search_broker import (
    SEARCH_BROKER_TRACE,
    SearchBroker,
    canonical_url,
)
from finance_agent.gateway.tools import (
    canonical_record_text,
    make_search_broker_tool,
    maybe_make_search_broker,
)
from finance_agent.knowledge.models import PitGrade
from finance_agent.research.evidence_desk import ChunkStore, verify_and_build

NOW = datetime(2024, 6, 1, tzinfo=UTC)


def rec(source: str, url: str, text: str, *, title: str = "", at=NOW) -> DataRecord:
    return DataRecord(
        source_id=source, url=url,
        payload={"title": title or text[:20], "text": text},
        available_at=at,
    )


def make_gateway(primary_records, backup_records=None, *, with_backup=True):
    gateway = DataGateway(mode="live", events=EventStore(":memory:"), run_id="t")
    gateway.register(FixtureAdapter(
        SourceCapability(source_id="web_search", pit_grade=PitGrade.B,
                         server_side_asof=True, description="主源"),
        records=primary_records,
    ))
    if with_backup:
        gateway.register(FixtureAdapter(
            SourceCapability(source_id="web_search_tavily", pit_grade=PitGrade.C,
                             server_side_asof=False, description="备源"),
            records=backup_records or [],
        ))
    return gateway


class DenySecondBudget:
    """第二次检索准入拒绝（模拟预算只够一源）。"""

    def __init__(self):
        self.calls = 0
        self.duplicates = 0

    def admit_retrieval(self):
        self.calls += 1
        return (True, "") if self.calls == 1 else (False, "检索预算耗尽（测试）")

    def record_duplicate_retrieval(self):
        self.duplicates += 1


# ---------------- canonical URL 归一 ----------------


class TestCanonicalUrl:
    def test_www_tracking_trailing_slash_folded(self):
        a = canonical_url("https://www.Sec.gov/Archives/x/?utm_source=RSS&utm_campaign=y")
        b = canonical_url("https://sec.gov/Archives/x")
        assert a == b == "https://sec.gov/Archives/x"

    def test_query_params_kept_except_tracking(self):
        assert "id=42" in canonical_url("https://x.com/a?id=42&utm_medium=social")
        assert "utm_medium" not in canonical_url("https://x.com/a?id=42&utm_medium=social")

    def test_garbage_url_returns_lowercased(self):
        assert canonical_url("HTTP://Weird Host/x") == "http://weird host/x"
        assert canonical_url("") == ""


# ---------------- 主备策略 ----------------


class TestBrokerPolicy:
    def test_primary_only_by_default(self):
        gateway = make_gateway(
            [rec("web_search", f"https://a.com/{i}", f"alpha text {i}")
             for i in range(4)],  # 召回充足（≥ min_recall）→ 不动备源
            [rec("web_search_tavily", "https://b.com/2", "beta text")],
        )
        broker = SearchBroker(gateway)
        out = broker.search({"query": "q"})
        assert [r.url for r in out.records] == [f"https://a.com/{i}" for i in range(4)]
        assert out.trace["engines_called"] == ["web_search"]
        assert out.trace["fallback_reason"] == ""

    def test_low_recall_merges_backup(self):
        gateway = make_gateway(
            [rec("web_search", "https://a.com/1", "alpha")],
            [rec("web_search_tavily", "https://b.com/2", "beta")],
        )
        broker = SearchBroker(gateway, min_recall=3)
        out = broker.search({"query": "q"})
        assert [r.url for r in out.records] == ["https://a.com/1", "https://b.com/2"]
        assert "低召回" in out.trace["fallback_reason"]
        assert out.records[1].payload["_broker"]["origin"] == "backup"

    def test_primary_error_falls_back_visibly(self):
        gateway = make_gateway([], [rec("web_search_tavily", "https://b.com/2", "beta")])

        def boom(req, as_of=None):
            raise ConnectionError("connection refused")

        gateway._adapters["web_search"].query = boom  # noqa: SLF001 - 测试注入故障
        broker = SearchBroker(gateway)
        out = broker.search({"query": "q"})
        assert [r.url for r in out.records] == ["https://b.com/2"]
        assert "主源报错回退" in out.trace["fallback_reason"]
        assert "error" in out.trace["per_engine"]["web_search"]

    def test_dual_mode_calls_both(self):
        gateway = make_gateway(
            [rec("web_search", f"https://a.com/{i}", f"alpha {i}") for i in range(5)],
            [rec("web_search_tavily", "https://b.com/9", "beta unique")],
        )
        broker = SearchBroker(gateway)
        out = broker.search({"query": "q"}, mode="dual")
        assert out.trace["engines_called"] == ["web_search", "web_search_tavily"]
        assert len(out.records) == 6

    def test_budget_denies_second_engine_partial_visible(self):
        gateway = make_gateway(
            [rec("web_search", "https://a.com/1", "alpha")],
            [rec("web_search_tavily", "https://b.com/2", "beta")],
        )
        budget = DenySecondBudget()
        broker = SearchBroker(gateway, min_recall=3, budget=budget)
        out = broker.search({"query": "q"})
        assert [r.url for r in out.records] == ["https://a.com/1"]
        assert budget.calls == 2, "两个引擎的调用意图都必须过预算闸"
        assert out.trace["per_engine"]["web_search_tavily"]["skipped"] == "budget_denied"


# ---------------- 去重与转载族 ----------------


class TestDedup:
    def test_same_announcement_two_engines_counts_once(self):
        """方案 §5.1：两个搜索引擎返回同一公告只算一个原始来源。"""
        shared = "BloombergNEF reports record backlog 300 million for the company"
        gateway = make_gateway(
            [rec("web_search", "https://news.com/story/?utm_source=feed", shared)],
            [rec("web_search_tavily", "https://news.com/story", shared)],
        )
        broker = SearchBroker(gateway, min_recall=5)
        out = broker.search({"query": "q"})
        assert len(out.records) == 1
        assert out.records[0].source_id == "web_search", "保主源版本（PIT 更高）"
        assert out.records[0].payload["_broker"]["family_size"] == 2
        assert out.trace["url_duplicates_removed"] == 1

    def test_same_content_different_url_is_family(self):
        shared = "identical press release text republished everywhere"
        gateway = make_gateway(
            [rec("web_search", "https://a.com/x", shared)],
            [rec("web_search_tavily", "https://mirror.cn/y", shared)],
        )
        broker = SearchBroker(gateway, min_recall=5)
        out = broker.search({"query": "q"})
        assert len(out.records) == 1
        assert out.trace["family_merges"] == 1
        meta = out.records[0].payload["_broker"]
        assert meta["family_size"] == 2
        assert meta["family_urls"] == ["https://mirror.cn/y"]


# ---------------- 工具层 ----------------


class TestBrokerTool:
    def test_chunks_registered_and_broker_meta_not_in_canonical_text(self):
        gateway = make_gateway(
            [rec("web_search", "https://a.com/1", "backlog reached 300 million USD")],
            [rec("web_search_tavily", "https://b.com/2", "different outlet text")],
        )
        events = EventStore(":memory:")
        broker = SearchBroker(gateway, events=events, run_id="run-sb")
        chunks = ChunkStore()
        tool = make_search_broker_tool(broker, chunks)
        out = json.loads(tool({"query": "backlog", "mode": "dual"})["content"])
        assert len(out["items"]) == 2
        assert out["broker_trace"]["engines_called"] == ["web_search", "web_search_tavily"]
        # broker 元数据传输层可见，但不进台账正文（子串校验基准不受污染）
        item0 = out["items"][0]
        assert item0["broker"]["origin"] == "primary"
        assert "_broker" not in canonical_record_text(item0)
        # chunk 可引证（逐字子串校验通过）
        ev = verify_and_build(chunks, chunk_id=item0["chunk_id"],
                              verbatim_quote="backlog reached 300 million USD")
        assert ev.evidence_id.startswith("ev-")
        # trace 事件落库（参数/回退/去重统计可归因）
        traces = [e for e in events.read("run-sb") if e.type == SEARCH_BROKER_TRACE]
        assert traces and traces[0].payload["merged_count"] == 2

    def test_cache_hit_not_recharged_and_marked(self):
        gateway = make_gateway(
            [rec("web_search", f"https://a.com/{i}", f"alpha {i}") for i in range(4)]
        )
        budget = DenySecondBudget()
        broker = SearchBroker(gateway, budget=budget)
        tool = make_search_broker_tool(broker, ChunkStore(), cache={}, budget=budget)
        tool({"query": "q"})
        out2 = tool({"query": "q"})
        assert out2["cached"] is True
        assert budget.calls == 1, "缓存命中不得重复扣检索预算"
        assert budget.duplicates == 1

    def test_no_engines_empty_and_visible(self):
        gateway = DataGateway(mode="live", events=EventStore(":memory:"), run_id="t")
        broker = SearchBroker(gateway)
        assert broker.available() is False
        out = broker.search({"query": "q"})
        assert out.records == []
        assert "无可用搜索源" in out.trace["fallback_reason"]

    def test_maybe_make_broker_fail_closed(self):
        gateway = DataGateway(mode="live", events=EventStore(":memory:"), run_id="t")
        assert maybe_make_search_broker(gateway) is None
        gateway.register(FixtureAdapter(
            SourceCapability(source_id="web_search_tavily", pit_grade=PitGrade.C,
                             server_side_asof=False, description="仅备源"),
            records=[rec("web_search_tavily", "https://b.com/1", "beta")],
        ))
        broker = maybe_make_search_broker(gateway)
        assert broker is not None, "只有备源时 broker 仍可用（单源 + 去重）"
        out = broker.search({"query": "q"})
        assert [r.url for r in out.records] == ["https://b.com/1"]
        assert out.records[0].payload["_broker"]["origin"] == "backup"
