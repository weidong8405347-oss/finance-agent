"""来源独立性判断验收（review P2-A 剩余项，方案 §5.4）。

判据：
- EvidencePack 把支持证据按 文档 → canonical URL → 正文哈希 归组：
  同文档/同址（跟踪参数归一）/同文不同址 = 同一来源族；
- verifier 记录 independent_sources 并进核验意见：supported 但只有一族 →
  显式标注「不构成独立佐证」（可见性纪律，不硬拦——单 filing 是合法事实源）；
- assessment 披露 single_source_supported_claims 计数。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metrics import MetricPeriod
from finance_agent.knowledge.models import Evidence, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.llm.base import AssistantReply
from finance_agent.llm.mock import MockLLM
from finance_agent.research.assessment import assess
from finance_agent.research.evidence_pack import (
    build_evidence_pack,
    compute_source_independence,
)
from finance_agent.research.plan import Budgets, ResearchPlan, ResearchQuestion
from finance_agent.research.verifier import verify_claim

NOW = datetime.now(UTC)
T0 = datetime(2024, 3, 1, tzinfo=UTC)
FY2023 = MetricPeriod(start=date(2023, 1, 1), end=date(2023, 12, 31),
                      frequency="FY", fiscal_label="FY2023")
RUN = RunManifest(run_id="r-indep", mode=RunMode.LIVE)


def make_env(tmp_path, *, evidences: list[Evidence]):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    for ev in evidences:
        kb.add_evidence(ev)
    return kb, metrics, events


def ev(eid: str, *, url: str | None, text: str, source_id: str = "web_search",
       locator: dict | None = None) -> Evidence:
    return Evidence(
        evidence_id=eid, source_id=source_id, url=url, verbatim_quote=text,
        retrieved_at=NOW, available_at=T0, pit_grade=PitGrade.B,
        locator=locator or {},
    )


def save_claim(metrics, claim_id, support, **kw):
    metrics.save_claim(claim_id=claim_id, namespace="prod", payload={
        "claim_id": claim_id, "entity_kind": "stock", "entity_id": "BE",
        "statement": kw.get("statement", "订单 300 million 创纪录"),
        "kind": "inference", "status": "validated",
        "support_refs": list(support), "counter_refs": [], "limitations": [],
        "created_at": T0.isoformat(), "namespace": "prod",
    }, recorded_at=T0)


def supported_llm():
    return MockLLM([AssistantReply(content=json.dumps({
        "atomic_claims": [{"text": "订单 300 million", "verdict": "supported",
                           "supporting_refs": []}],
        "reasoning_review": {"premises_explicit": True, "boundary_ok": True,
                             "alternative_explanations": []},
        "next_actions": [],
    }))])


class TestIndependenceGrouping:
    def test_same_url_tracking_variants_one_group(self, tmp_path):
        kb, metrics, events = make_env(tmp_path, evidences=[
            ev("ev-a", url="https://news.com/story?utm_source=feed", text="backlog 300 million"),
            ev("ev-b", url="https://www.news.com/story/", text="backlog 300 million"),
        ])
        save_claim(metrics, "claim-1", ["ev-a", "ev-b"])
        pack = build_evidence_pack(kb, metrics, entity_kind="stock", entity_id="BE",
                                   claim_payload=metrics.get_claim("claim-1"), as_of=T0)
        indep = pack.source_independence
        assert indep["independent_sources"] == 1
        assert indep["single_source"] is True
        # compact 投影透出（verifier prompt 可见）
        assert pack.compact()["source_independence"]["single_source"] is True

    def test_same_document_locator_one_group(self, tmp_path):
        kb, metrics, events = make_env(tmp_path, evidences=[
            ev("ev-a", url=None, text="page 3 quote",
               source_id="edgar", locator={"document_id": "doc-0001", "page": "3"}),
            ev("ev-b", url=None, text="page 5 quote",
               source_id="edgar", locator={"document_id": "doc-0001", "page": "5"}),
        ])
        save_claim(metrics, "claim-2", ["ev-a", "ev-b"])
        pack = build_evidence_pack(kb, metrics, entity_kind="stock", entity_id="BE",
                                   claim_payload=metrics.get_claim("claim-2"), as_of=T0)
        assert pack.source_independence["independent_sources"] == 1
        assert list(pack.source_independence["groups"]) == ["doc:doc-0001"]

    def test_distinct_sources_counted(self, tmp_path):
        kb, metrics, events = make_env(tmp_path, evidences=[
            ev("ev-a", url="https://sec.gov/f", text="filing says 300 million",
               source_id="edgar"),
            ev("ev-b", url="https://news.com/x", text="analysts cite 300 million"),
        ])
        save_claim(metrics, "claim-3", ["ev-a", "ev-b"])
        pack = build_evidence_pack(kb, metrics, entity_kind="stock", entity_id="BE",
                                   claim_payload=metrics.get_claim("claim-3"), as_of=T0)
        assert pack.source_independence["independent_sources"] == 2
        assert pack.source_independence["single_source"] is False

    def test_same_text_different_urls_is_family(self, tmp_path):
        kb, metrics, events = make_env(tmp_path, evidences=[
            ev("ev-a", url=None, text="wire copy identical text", source_id="web_search"),
            ev("ev-b", url=None, text="wire copy identical text",
               source_id="web_search_tavily"),
        ])
        spans = build_evidence_pack(
            kb, metrics, entity_kind="stock", entity_id="BE",
            claim_payload={"claim_id": "claim-x", "support_refs": ["ev-a", "ev-b"]},
            as_of=T0,
        ).supporting_spans
        indep = compute_source_independence(spans)
        assert indep["independent_sources"] == 1, "同文不同址 = 转载族一族"


class TestVerifierRecordsIndependence:
    def test_supported_single_source_flagged_not_blocked(self, tmp_path):
        kb, metrics, events = make_env(tmp_path, evidences=[
            ev("ev-a", url="https://news.com/s", text="backlog 300 million USD"),
            ev("ev-b", url="https://news.com/s?utm_campaign=x", text="backlog 300 million USD"),
        ])
        save_claim(metrics, "claim-v", ["ev-a", "ev-b"])
        result = verify_claim(kb, metrics, claim_id="claim-v", llm=supported_llm(),
                              events=events, manifest=RUN, entity_kind="stock",
                              entity_id="BE", now=NOW)
        assert result.evidence_support == "supported"
        assert result.independent_sources == 1
        notes = metrics.get_claim("claim-v")["verification"]["notes"]
        assert any("独立佐证" in n for n in notes), "单族支持必须显式标注（不假装多源佐证）"
        assert result.status_after == "validated", "单 filing/单族是合法事实源，不硬拦"
        # 事件可审计
        evs = [e for e in events.read("r-indep") if e.type == "research/claim_verified"]
        assert evs and evs[0].payload["independent_sources"] == 1

    def test_multi_source_no_flag(self, tmp_path):
        kb, metrics, events = make_env(tmp_path, evidences=[
            ev("ev-a", url="https://sec.gov/f", text="backlog 300 million USD",
               source_id="edgar"),
            ev("ev-b", url="https://news.com/y", text="company backlog 300 million per filing"),
        ])
        save_claim(metrics, "claim-w", ["ev-a", "ev-b"])
        result = verify_claim(kb, metrics, claim_id="claim-w", llm=supported_llm(),
                              events=events, manifest=RUN, entity_kind="stock",
                              entity_id="BE", now=NOW)
        assert result.independent_sources == 2
        assert not any("独立佐证" in n
                       for n in metrics.get_claim("claim-w")["verification"]["notes"])


class TestAssessmentDisclosure:
    def test_single_source_supported_counted(self, tmp_path):
        kb, metrics, events = make_env(tmp_path, evidences=[
            ev("ev-a", url="https://news.com/s", text="backlog 300 million USD"),
        ])
        save_claim(metrics, "claim-s", ["ev-a"])
        verify_claim(kb, metrics, claim_id="claim-s", llm=supported_llm(),
                     events=events, manifest=RUN, entity_kind="stock",
                     entity_id="BE", now=NOW)
        plan = ResearchPlan(
            plan_id="plan-i", entity_kind="stock", entity_id="BE", objective="o",
            mode="targeted",
            questions=[ResearchQuestion(question_id="q1", text="?", priority="high",
                                        status="answered", conclusion="c")],
            budgets=Budgets(), created_at=T0,
        )
        claims = metrics.claims_as_of("stock", "BE", NOW)
        out = assess(plan, claims=claims, observations=[], calculations=[], now=NOW)
        assert out.evidence_quality["single_source_supported_claims"] == 1
