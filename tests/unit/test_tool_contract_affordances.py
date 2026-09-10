"""工具契约可操作性（方案 §3「工具应支持可处理的错误」；基线试跑整改 2026-09-10）。

哨兵基线试跑（NVDA guidance 题）暴露的失败形态：证据全部找对，但 worker 在
① guidance.target_period 传字符串（schema 未描述形状）② period 缺 start
③ 多数字摘录缺 value_span 三道门禁上烧光 12 步预算，零观测零答案。

整改判据：
- schema 完整描述 guidance/consensus/period 的对象形状（不再 {"type":"object"} 空壳）；
- 每类拒绝都带**可照抄的修法示例**（模型第二次调用就能改对）；
- 问题驱动纪律明示 answer_question 的低门槛（能答先交，观测被拒不等于放弃答案）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.models import Evidence, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.research.evidence_desk import ChunkStore
from finance_agent.research.tools import TOOL_SCHEMAS, make_research_tools

NOW = datetime.now(UTC)
T0 = datetime(2026, 2, 25, tzinfo=UTC)

GUIDANCE_QUOTE = (
    "NVIDIA's outlook for the first quarter of fiscal 2027 is as follows: "
    "Revenue is expected to be $78.0 billion, plus or minus 2%."
)
TABLE_QUOTE = "Revenue $81,615 $68,127 $44,062 20 % 85 %"


@pytest.fixture()
def env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    kb.add_evidence(Evidence(
        evidence_id="ev-guidance", source_id="web_fetch",
        verbatim_quote=GUIDANCE_QUOTE, retrieved_at=NOW, available_at=T0,
        pit_grade=PitGrade.B,
    ))
    kb.add_evidence(Evidence(
        evidence_id="ev-table", source_id="web_fetch",
        verbatim_quote=TABLE_QUOTE, retrieved_at=NOW, available_at=T0,
        pit_grade=PitGrade.B,
    ))
    tools, tracker = make_research_tools(
        store=kb, writer=writer,
        manifest=RunManifest(run_id="live-aff", mode=RunMode.LIVE),
        entity_kind="stock", entity_id="NVDA", chunk_store=ChunkStore(),
        events=events, metrics=metrics, metric_writer=mw,
    )
    return tools, tracker, metrics


def content(out: dict) -> str:
    return out["content"]


# ---------------- schema 形状完整（不再空壳 object） ----------------


class TestProposeMetricSchema:
    def test_guidance_fully_specified(self):
        props = TOOL_SCHEMAS["propose_metric"]["parameters"]["properties"]
        g = props["guidance"]
        assert g.get("required") == ["issuer", "published_at", "target_period"]
        tp = g["properties"]["target_period"]
        assert tp["type"] == "object", "target_period 必须声明为对象（字符串标签是事故形态）"
        assert tp.get("required") == ["end", "frequency"]
        assert "不是字符串" in (g.get("description") + tp.get("description", ""))

    def test_consensus_fully_specified(self):
        c = TOOL_SCHEMAS["propose_metric"]["parameters"]["properties"]["consensus"]
        assert c.get("required") == ["vendor", "snapshot_at"]

    def test_period_description_carries_example(self):
        p = TOOL_SCHEMAS["propose_metric"]["parameters"]["properties"]["period"]
        assert "start" in p["description"] and "2026-01-26" in p["description"]


# ---------------- 拒绝必须带可照抄的修法示例 ----------------


class TestRejectionHints:
    def test_period_missing_start_hint(self, env):
        tools, tracker, _ = env
        out = content(tools["propose_metric"]({
            "metric_key": "revenue", "value_text": "$78.0 billion",
            "period": {"end": "2026-04-26", "frequency": "Q", "fiscal_label": "FY2027Q1"},
            "nature": "reported", "evidence_ids": ["ev-guidance"],
            "value_span": "Revenue is expected to be $78.0 billion",
        }))
        assert out.startswith("rejected: period 非法")
        assert '"start":"2026-01-26"' in out.replace(" ", "") or "start" in out
        assert "instant" in out, "必须说明只有 instant 可省 start"
        assert tracker.rejected

    def test_guidance_string_target_period_hint(self, env):
        """基线事故复现：target_period 传字符串 → 拒绝必须给出对象示例。"""
        tools, _, _ = env
        out = content(tools["propose_metric"]({
            "metric_key": "revenue", "value_text": "$78.0 billion",
            "period": {"start": "2026-01-26", "end": "2026-04-26",
                       "frequency": "Q", "fiscal_label": "FY2027Q1"},
            "nature": "guidance", "evidence_ids": ["ev-guidance"],
            "value_span": "Revenue is expected to be $78.0 billion",
            "guidance": {"issuer": "NVIDIA Corporation", "published_at": "2026-02-25",
                         "target_period": "FY2027Q1"},
            "unit": "USD", "currency": "USD",
        }))
        assert out.startswith("rejected:")
        assert "target_period 是对象" in out
        assert '"fiscal_label":"FY2027Q1"' in out.replace(" ", ""), "示例可直接照抄"

    def test_value_span_multi_number_hint(self, env):
        """表格摘录多数字 → 拒绝带 value_span 逐字示例（模型照抄即可通过）。"""
        tools, _, metrics = env
        out = content(tools["propose_metric"]({
            "metric_key": "revenue", "value_text": "$81,615",
            "period": {"start": "2026-01-26", "end": "2026-04-26",
                       "frequency": "Q", "fiscal_label": "FY2027Q1"},
            "nature": "reported", "evidence_ids": ["ev-table"],
            "unit": "USD", "currency": "USD",
            "locator": {"document": "doc-x", "table": "Q1 Summary", "row": "Revenue"},
        }))
        assert out.startswith("rejected:")
        assert "value_span" in out
        assert '"$81,615"' in out or "$81,615" in out, "示例必须含目标数字的逐字片段"

        # 照抄修法后重提 → 通过（提示可操作性的闭环证明）
        out2 = content(tools["propose_metric"]({
            "metric_key": "revenue", "value_text": "$81,615",
            "value_span": "$81,615",
            "period": {"start": "2026-01-26", "end": "2026-04-26",
                       "frequency": "Q", "fiscal_label": "FY2027Q1"},
            "nature": "reported", "evidence_ids": ["ev-table"],
            "unit": "USD", "currency": "USD",
            "locator": {"document": "doc-x", "table": "Q1 Summary", "row": "Revenue"},
        }))
        assert "observation_id" in out2, f"按提示修复后应写入成功：{out2[:300]}"
        obs = metrics.observations_as_of("stock", "NVDA", datetime.now(UTC),
                                         metric_key="revenue")
        assert obs and obs[0].value == "81615"


# ---------------- 纪律提示：answer_question 低门槛 ----------------


class TestDisciplineAffordance:
    def test_question_driven_discipline_mentions_answer_first(self):
        from finance_agent.research.loop import _worker_discipline

        qd = _worker_discipline(question_driven=True, has_document_reader="document")
        assert "能答就先交答案" in qd
        assert "不要放弃提交" in qd


# ---------------- naive datetime 归一（基线 r3 试跑暴露的 TypeError） ----------------


class TestNaiveDatetimeNormalization:
    def test_guidance_date_only_string_writes_end_to_end(self, env):
        """基线 r3 事故：published_at="2026-02-25"（日期串→naive）与 aware
        knowledge_time 比较 TypeError 炸门禁。整改后：无时区按 UTC，端到端写入成功。"""
        tools, _, metrics = env
        out = content(tools["propose_metric"]({
            "metric_key": "revenue", "value_text": "$78.0 billion",
            "value_span": "Revenue is expected to be $78.0 billion",
            "period": {"start": "2026-01-26", "end": "2026-04-26",
                       "frequency": "Q", "fiscal_label": "FY2027Q1"},
            "nature": "guidance", "evidence_ids": ["ev-guidance"],
            "guidance": {"issuer": "NVIDIA Corporation", "published_at": "2026-02-25",
                         "target_period": {"start": "2026-01-26", "end": "2026-04-26",
                                           "frequency": "Q", "fiscal_label": "FY2027Q1"}},
            "unit": "USD", "currency": "USD",
            "locator": {"document": "doc-x", "section": "Outlook"},
        }))
        assert "observation_id" in out, f"日期串 published_at 必须能写入：{out[:400]}"
        obs = json.loads(out)
        stored = metrics.get_observation(obs["observation_id"])
        assert stored.nature == "guidance"
        assert stored.guidance_published_at.tzinfo is not None, "落库必须带时区（UTC 归一）"

    def test_model_normalizes_naive_fields(self):

        from finance_agent.knowledge.metrics import (
            ConsensusObservation,
            GuidanceObservation,
            MetricPeriod,
            RawValue,
        )

        common = {
            "entity_kind": "stock", "entity_id": "NVDA", "metric_key": "revenue",
            "period": MetricPeriod(start=datetime(2026, 1, 26, tzinfo=UTC).date(),
                                   end=datetime(2026, 4, 26, tzinfo=UTC).date(),
                                   frequency="Q", fiscal_label="FY2027Q1"),
            "value": "78000000000", "unit": "USD", "currency": "USD",
            "raw": RawValue(value_text="$78.0 billion", quote_ref="ev-x"),
            "evidence_refs": ["ev-x"],
            "knowledge_time": datetime(2026, 2, 25, 21, 0, tzinfo=UTC),
            "retrieved_at": datetime(2026, 9, 10, tzinfo=UTC),
            "created_at": datetime(2026, 9, 10, tzinfo=UTC),
        }
        g = GuidanceObservation(
            **common, issuer="NVIDIA",
            guidance_published_at=datetime(2026, 2, 25),  # naive
            target_period=MetricPeriod(start=datetime(2026, 1, 26, tzinfo=UTC).date(),
                                       end=datetime(2026, 4, 26, tzinfo=UTC).date(),
                                       frequency="Q"),
        )
        assert g.guidance_published_at.tzinfo == UTC
        c = ConsensusObservation(
            **common, vendor="vendor-x",
            consensus_snapshot_at=datetime(2026, 8, 1),  # naive
        )
        assert c.consensus_snapshot_at.tzinfo == UTC
