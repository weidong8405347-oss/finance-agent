"""研究产物验收（设计 §7.8/§6.1）：ReportDocument 验证、claim 状态机、同源渲染。

关键场景：未知 [ev-xxx] 明确报错（不静默）、自由文本事实数字需绑定引用、
metric 插值以观测值为准、validated 硬门禁、旧 thesis 兼容为 legacy analysis。
"""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation
from finance_agent.knowledge.models import Evidence, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.research.artifacts import (
    ArtifactValidator,
    HeadingBlock,
    ParagraphBlock,
    ReportDocument,
    ResearchArtifact,
    ResearchClaim,
    SourceRefBlock,
    document_from_markdown,
    interpolate_metrics,
    render_markdown,
)

NOW = datetime(2025, 6, 1, tzinfo=UTC)
FY = MetricPeriod(start=date(2024, 1, 1), end=date(2024, 12, 31), frequency="FY", fiscal_label="FY2024")


@pytest.fixture()
def env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    store = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    kb.add_evidence(Evidence(
        evidence_id="ev-1", source_id="edgar", url="https://sec.gov/x",
        verbatim_quote="Total revenue 1500 million", retrieved_at=NOW,
        available_at=NOW, pit_grade=PitGrade.A,
    ))
    from finance_agent.harness.manifest import RunManifest, RunMode
    from finance_agent.knowledge.metric_writer import TypedMetricWriter

    w = TypedMetricWriter(store=store, kb=kb, events=events)
    from finance_agent.knowledge.normalization import normalize_raw

    value, steps = normalize_raw("1500 million", "USD")
    obs = ReportedObservation(
        entity_kind="stock", entity_id="BE", metric_key="revenue", period=FY,
        value=value, unit="USD", currency="USD",
        raw=RawValue(value_text="1500 million", unit_text="USD"),
        normalization=[s.model_dump(mode="json") for s in steps],
        evidence_refs=["ev-1"], knowledge_time=NOW, source_available_at=NOW,
        retrieved_at=NOW, created_at=NOW, pit_grade=PitGrade.A,
    )
    oid, _ = w.write_observation(obs, run=RunManifest(run_id="r", mode=RunMode.LIVE))
    claim = ResearchClaim(
        entity_kind="stock", entity_id="BE", statement="收入增长由数据中心订单驱动",
        kind="inference", support_refs=["ev-1"], status="validated",
        created_at=NOW, evidence_cutoff=NOW,
    ).with_id()
    store.save_claim(claim_id=claim.claim_id, namespace="prod",
                     payload=claim.model_dump(mode="json"))
    return kb, store, events, oid, claim


def make_artifact(blocks, **kw):
    doc = ReportDocument(title="BE 研究报告", entity_kind="stock", entity_id="BE",
                         blocks=blocks).with_id()
    return ResearchArtifact(entity_kind="stock", entity_id="BE", title="BE 研究报告",
                            report_document=doc, created_at=NOW, **kw).with_id()


class TestClaimContract:
    def test_fact_summary_requires_support(self):
        with pytest.raises(ValidationError):
            ResearchClaim(entity_kind="stock", entity_id="BE", statement="公司收入 15 亿",
                          kind="fact_summary", support_refs=[])

    def test_validated_requires_support(self):
        with pytest.raises(ValidationError):
            ResearchClaim(entity_kind="stock", entity_id="BE", statement="推断成立",
                          kind="inference", status="validated", support_refs=[])

    def test_superseded_requires_replacement(self):
        with pytest.raises(ValidationError):
            ResearchClaim(entity_kind="stock", entity_id="BE", statement="旧结论",
                          kind="analysis", status="superseded", support_refs=["ev-1"])

    def test_legacy_thesis_marked_not_reported_fact(self):
        """旧 thesis 兼容为 legacy analysis：不自动标成披露事实（§2.2）。"""
        c = ResearchClaim(
            entity_kind="stock", entity_id="BE", statement="旧投资论点全文…",
            kind="analysis", legacy_field="thesis", support_refs=["ev-1"],
        ).with_id()
        assert c.legacy_field == "thesis" and c.kind == "analysis"
        assert c.status == "draft"  # 未经验证不得 validated


class TestDocumentValidation:
    def test_unknown_evidence_ref_hard_fails(self, env):
        kb, store, *_ = env
        artifact = make_artifact([
            HeadingBlock(level=1, text="摘要"),
            ParagraphBlock(text="收入大幅增长 [ev-doesnotexist]"),
        ])
        issues = ArtifactValidator(kb=kb, metric_store=store).validate(artifact)
        codes = [i.code for i in issues]
        assert "unresolved_evidence" in codes
        hard = [i for i in issues if i.hard and i.code == "unresolved_evidence"]
        assert hard and hard[0].ref == "ev-doesnotexist"  # 未知 id 明确报错，不静默丢弃

    def test_known_ref_passes(self, env):
        kb, store, _, oid, claim = env
        artifact = make_artifact([
            ParagraphBlock(text="收入 15 亿美元 [ev-1]，驱动判断见结论"),
            SourceRefBlock(refs=["ev-1"]),
        ], claim_ids=[claim.claim_id])
        issues = ArtifactValidator(kb=kb, metric_store=store).validate(artifact)
        assert not [i for i in issues if i.hard and i.code.startswith("unresolved")]

    def test_bare_fact_number_flagged_soft(self, env):
        kb, store, _, oid, _ = env
        artifact = make_artifact([
            ParagraphBlock(text="公司去年收入达到 42.5 亿美元，增长很快"),
        ])
        issues = ArtifactValidator(kb=kb, metric_store=store).validate(artifact)
        soft = [i for i in issues if i.code == "bare_number_unverified"]
        assert soft and soft[0].hard is False  # 软检查：提示绑定引用，不阻断

    def test_metric_interpolation_uses_registered_value(self, env):
        kb, store, _, oid, _ = env
        artifact = make_artifact([
            ParagraphBlock(text=f"全年收入 {{{{metric:{oid}}}}}，创历史新高 [ev-1]"),
        ])
        issues = ArtifactValidator(kb=kb, metric_store=store).validate(artifact)
        assert not [i for i in issues if i.code == "unresolved_observation"]
        md = render_markdown(artifact, store=store, kb=kb)
        assert "1500000000 USD" in md  # 以登记观测值为准，LLM 不能改写数字

    def test_unresolvable_metric_ref_hard_fails(self, env):
        kb, store, *_ = env
        artifact = make_artifact([ParagraphBlock(text="收入 {{metric:obs-nope}} 增长")])
        issues = ArtifactValidator(kb=kb, metric_store=store).validate(artifact)
        assert any(i.code == "unresolved_observation" and i.hard for i in issues)

    def test_validated_status_blocked_by_hard_issues(self, env):
        _, _, _, _, claim = env
        artifact = make_artifact(
            [ParagraphBlock(text="结论 [ev-missing]")],
            claim_ids=[claim.claim_id],
        )
        from finance_agent.research.artifacts import ValidationIssue
        artifact.validation_issues = [
            ValidationIssue(code="unresolved_evidence", ref="ev-missing", message="x")
        ]
        with pytest.raises(ValidationError):
            ResearchArtifact.model_validate(
                {**artifact.model_dump(mode="json"), "status": "validated"}
            )

    def test_empty_document_flagged(self, env):
        kb, store, *_ = env
        artifact = make_artifact([])
        issues = ArtifactValidator(kb=kb, metric_store=store).validate(artifact)
        assert any(i.code == "empty_document" for i in issues)


class TestRendering:
    def test_markdown_fallback_parses_headings(self):
        doc = document_from_markdown(
            "# 标题\n\n## 摘要\n判断结论 [ev-1]\n\n## 财务\n收入 100。\n",
            title="t", entity_kind="stock", entity_id="BE",
        )
        types = [b.type for b in doc.blocks]
        assert types.count("heading") == 3 and types.count("paragraph") == 2
        assert any("降级" in x for x in doc.limitations)

    def test_render_contains_status_and_refs(self, env):
        from finance_agent.research.artifacts import ClaimBlock

        kb, store, _, oid, claim = env
        artifact = make_artifact(
            [HeadingBlock(level=1, text="摘要"),
             ParagraphBlock(text=f"收入 {{{{metric:{oid}}}}} [ev-1]"),
             ClaimBlock(claim_id=claim.claim_id)],
            claim_ids=[claim.claim_id], status="validated", sufficiency="sufficient",
        )
        md = render_markdown(artifact, store=store, kb=kb)
        assert "validated" in md and "sufficient" in md
        assert claim.statement in md  # claim block 渲染论断原文（与页面同源）

    def test_interpolate_missing_ref_left_visible(self, env):
        _, store, *_ = env
        text = interpolate_metrics("值 {{metric:obs-nope}} 保留", store)
        assert "{{metric:obs-nope}}" in text  # 不可解析保留原样（验证层已报错）
