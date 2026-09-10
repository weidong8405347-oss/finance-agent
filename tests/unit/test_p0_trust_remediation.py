"""P0 可信度与策略整改（docs/research-profile-tools-plugins-plan-2026-09-09.md §2/§11 P0）。

覆盖四个代码事实及其整改：
1. 旧事实冲突工具：`keep_evidence_id` 只进说明、底层只清 conflict_flag——能显示
   「冲突已处理」却没有真正选择与保存获胜事实 → 真裁决（定位获胜方 + 同值晋升 + 拒绝可见）；
2. validated 语义：引用可解析 ≠ 原文支持结论 → 分项核验状态 verification 落库并诚实披露；
3. 来源质量：first_party_observations 不再与 PIT A 混用 → 按来源角色分档；
4. 研究策略与评审：plan 模式 worker 不再附带「先逐字段写入」纪律；rubric 输入
   携带论断原文与支持摘录（不再只有 ID 和计数）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.research.evidence_desk import ChunkStore
from finance_agent.research.tools import make_research_tools

NOW = datetime.now(UTC)
T0 = datetime(2024, 3, 1, tzinfo=UTC)
T1 = datetime(2024, 6, 1, tzinfo=UTC)


@pytest.fixture()
def env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    return kb, metrics, events, writer, mw


def make_tools(env, entity_kind="stock", entity_id="BE", **kwargs):
    kb, metrics, events, writer, mw = env
    return make_research_tools(
        store=kb, writer=writer,
        manifest=RunManifest(run_id="live-p0", mode=RunMode.LIVE),
        entity_kind=entity_kind, entity_id=entity_id,
        chunk_store=ChunkStore(), events=events,
        metrics=metrics, metric_writer=mw, **kwargs,
    )


def seed_conflict(kb: BitemporalStore) -> tuple[str, str]:
    """两个竞争版本：v1=100（edgar 原文），v2=120（web 转载）→ v2 带冲突标记。"""
    kb.add_evidence(Evidence(
        evidence_id="ev-filing", source_id="edgar", url="https://sec.gov/f1",
        verbatim_quote="Total revenue was 100 million for fiscal 2023",
        retrieved_at=NOW, available_at=T0, pit_grade=PitGrade.A,
    ))
    kb.add_evidence(Evidence(
        evidence_id="ev-web", source_id="web_search", url="https://news.example/r",
        verbatim_quote="Total revenue was 120 million for fiscal 2023",
        retrieved_at=NOW, available_at=T1, pit_grade=PitGrade.B,
    ))
    v1 = kb.assert_fact(Fact(
        entity_kind="stock", entity_id="BE", field="revenue_fy", value=100,
        event_time=T0, knowledge_time=T0, evidence_ids=["ev-filing"],
    ))
    v2 = kb.assert_fact(Fact(
        entity_kind="stock", entity_id="BE", field="revenue_fy", value=120,
        event_time=T0, knowledge_time=T1, evidence_ids=["ev-web"],
    ))
    return v1, v2


# ---------------- 1. 旧事实冲突工具：真裁决 ----------------


class TestConflictAdjudication:
    def test_winner_promoted_when_not_latest(self, env):
        """keep 旧版本 → 同值晋升为当前投影（不是只清标记）。"""
        kb, metrics, events, writer, mw = env
        v1, v2 = seed_conflict(kb)
        tools, tracker = make_tools(env)

        out = tools["resolve_conflict"]({
            "field": "revenue_fy", "keep_evidence_id": "ev-filing",
            "note": "发行人原文优先于媒体转载",
        })
        payload = json.loads(out["content"])
        assert payload["winner_fact_id"] == v1
        assert payload["promoted_fact_id"], "获胜方非最新版必须晋升落最新投影"
        assert payload["cleared"] >= 1

        # 当前投影 = 获胜值；历史 append-only 保留全部版本；标记全部清除
        current = kb.as_of("stock", "BE", datetime.now(UTC))["revenue_fy"]
        assert current.value == 100
        hist = kb.history("stock", "BE", "revenue_fy")
        assert [h.value for h in hist] == [100, 120, 100]
        assert not any(h.conflict_flag for h in hist)
        assert not tracker.rejected

        # 事件可审计：裁决事件带获胜事实 id，晋升走 fact/asserted
        resolved = [e for e in events.read("live-p0") if e.type == "fact/conflict_resolved"]
        assert resolved and resolved[0].payload["keep_fact_id"] == v1
        assert resolved[0].payload["note"] == "发行人原文优先于媒体转载"
        asserted = [e for e in events.read("live-p0") if e.type == "fact/asserted"]
        assert any(a.payload["fact_id"] == payload["promoted_fact_id"] for a in asserted)

    def test_keep_latest_needs_no_promotion(self, env):
        kb, *_ = env
        v1, v2 = seed_conflict(kb)
        tools, _ = make_tools(env)
        out = json.loads(tools["resolve_conflict"]({
            "field": "revenue_fy", "keep_evidence_id": "ev-web",
        })["content"])
        assert out["winner_fact_id"] == v2
        assert out["promoted_fact_id"] is None
        assert kb.as_of("stock", "BE", datetime.now(UTC))["revenue_fy"].value == 120

    def test_keep_fact_id_direct(self, env):
        kb, *_ = env
        v1, _ = seed_conflict(kb)
        tools, _ = make_tools(env)
        out = json.loads(tools["resolve_conflict"]({
            "field": "revenue_fy", "keep_fact_id": v1,
        })["content"])
        assert out["winner_fact_id"] == v1
        assert kb.as_of("stock", "BE", datetime.now(UTC))["revenue_fy"].value == 100

    def test_unknown_winner_rejected_and_flags_untouched(self, env):
        """定位失败 fail-loud：拒绝且不清标记（不许显示「已处理」）。"""
        kb, metrics, events, writer, mw = env
        seed_conflict(kb)
        tools, tracker = make_tools(env)
        out = tools["resolve_conflict"]({
            "field": "revenue_fy", "keep_evidence_id": "ev-nonexistent",
        })
        assert out["content"].startswith("rejected:")
        assert "未被字段" in out["content"]
        assert tracker.rejected, "拒绝必须计入 tracker（诊断可见）"
        hist = kb.history("stock", "BE", "revenue_fy")
        assert hist[-1].conflict_flag, "裁决失败不得清冲突标记"
        assert not [e for e in events.read("live-p0") if e.type == "fact/conflict_resolved"]

    def test_missing_winner_param_rejected(self, env):
        kb, *_ = env
        seed_conflict(kb)
        tools, _ = make_tools(env)
        out = tools["resolve_conflict"]({"field": "revenue_fy"})
        assert out["content"].startswith("rejected:")
        assert "keep_fact_id" in out["content"]

    def test_writer_adjudicate_requires_history(self, env):
        kb, metrics, events, writer, mw = env
        from finance_agent.knowledge.errors import KnowledgeError
        with pytest.raises(KnowledgeError):
            writer.adjudicate_conflict(
                "stock", "NOPE", "revenue_fy", keep_fact_id="fact-x",
                run=RunManifest(run_id="live-p0", mode=RunMode.LIVE),
            )


# ---------------- 2. validated 语义拆分 ----------------


class TestClaimVerificationSplit:
    def test_validated_claim_records_references_only(self, env):
        """validated = 引用校验过；内容级核验状态诚实标 unchecked。"""
        kb, metrics, events, writer, mw = env
        kb.add_evidence(Evidence(
            evidence_id="ev-q1", source_id="edgar", verbatim_quote="backlog of 300 million",
            retrieved_at=NOW, available_at=T0, pit_grade=PitGrade.A,
        ))
        tools, _ = make_tools(env)
        out = json.loads(tools["propose_claim"]({
            "statement": "在手订单 300 million 支撑 2025 收入增长",
            "kind": "inference", "support_refs": ["ev-q1"],
        })["content"])
        assert out["status"] == "validated"
        assert "内容级核验" in out["note"]

        claim = metrics.get_claim(out["claim_id"])
        ver = claim["verification"]
        assert ver["references_valid"] is True
        assert ver["evidence_support"] == "unchecked"
        assert ver["numeric_checks"] == "unchecked"
        assert ver["analysis_review"] == "unchecked"  # inference 需要推理审查
        assert ver["counter_evidence_search"] is False

        validated_events = [
            e for e in events.read("live-p0") if e.type == "research/claim_validated"
        ]
        assert validated_events
        assert validated_events[0].payload["content_checked"] is False

    def test_fact_summary_analysis_review_not_required(self, env):
        kb, metrics, events, writer, mw = env
        kb.add_evidence(Evidence(
            evidence_id="ev-q2", source_id="edgar", verbatim_quote="revenue 100 million",
            retrieved_at=NOW, available_at=T0, pit_grade=PitGrade.A,
        ))
        tools, _ = make_tools(env)
        out = json.loads(tools["propose_claim"]({
            "statement": "FY2023 收入 100 million", "kind": "fact_summary",
            "support_refs": ["ev-q2"], "counter_refs": [],
        })["content"])
        claim = metrics.get_claim(out["claim_id"])
        assert claim["verification"]["analysis_review"] == "not_required"

    def test_draft_claim_references_invalid(self, env):
        kb, metrics, events, writer, mw = env
        tools, _ = make_tools(env)
        out = json.loads(tools["propose_claim"]({
            "statement": "没有证据支撑的结论", "kind": "inference",
            "support_refs": ["ev-ghost"],
        })["content"])
        assert out["status"] == "draft"
        claim = metrics.get_claim(out["claim_id"])
        assert claim["verification"]["references_valid"] is False

    def test_legacy_claim_payload_defaults_to_unchecked(self):
        """旧数据（无 verification 字段）反序列化 = 引用校验语义，不冒充内容已核验。"""
        from finance_agent.research.artifacts import ResearchClaim

        legacy = {
            "claim_id": "claim-legacy", "entity_kind": "stock", "entity_id": "BE",
            "statement": "旧论断内容", "kind": "inference", "status": "validated",
            "support_refs": ["ev-1"],
        }
        claim = ResearchClaim.model_validate(legacy)
        assert claim.verification.references_valid is False  # 历史 validated 不自动升级
        assert claim.verification.evidence_support == "unchecked"
        assert not claim.verification.content_checked


# ---------------- 3. 来源质量与 PIT 分离 ----------------


@dataclass
class _Obs:
    """assessment 消费的最小观测面（避免全模型校验噪音，聚焦归类逻辑）。"""

    metric_key: str = "revenue"
    nature: str = "reported"
    pit_grade: str = "A"
    evidence_refs: list[str] = field(default_factory=list)
    raw: object | None = None
    value: str | None = None


class TestSourceQualitySeparation:
    def test_first_party_and_pit_counted_separately(self):
        from finance_agent.research.assessment import assess
        from finance_agent.research.plan import Budgets, ResearchPlan

        plan = ResearchPlan(
            plan_id="plan-sq", entity_kind="stock", entity_id="BE", objective="o",
            mode="targeted", questions=[], budgets=Budgets(), created_at=NOW,
        )
        obs = [
            _Obs(pit_grade="A", evidence_refs=["ev-sec"]),      # 一手 + PIT A
            _Obs(pit_grade="B", evidence_refs=["ev-news"]),     # 二手 + PIT B
            _Obs(nature="guidance", pit_grade="B", evidence_refs=["ev-pr"]),  # 发行人指引
            _Obs(nature="consensus", pit_grade="C", evidence_refs=["ev-vendor"]),
            _Obs(pit_grade="A", evidence_refs=["ev-unknown-src"]),  # 来源解析不了
        ]
        # ev-unknown-src 不在映射里 → 该观测归 unknown（PIT A 也不冒充一手）
        evidence_sources = {"ev-sec": "edgar", "ev-news": "web_search",
                            "ev-pr": "edgar", "ev-vendor": "fundamentals"}
        a = assess(plan, claims=[], observations=obs, calculations=[],
                   evidence_sources=evidence_sources)
        eq = a.evidence_quality
        assert eq["pit_a_observations"] == 2
        assert eq["first_party_observations"] == 2   # SEC 原文 + 发行人指引
        assert eq["secondary_observations"] == 1     # 媒体转载
        assert eq["vendor_observations"] == 1        # consensus 快照
        assert eq["unknown_source_observations"] == 1

    def test_validated_claims_content_unchecked_disclosed(self):
        from finance_agent.research.assessment import assess
        from finance_agent.research.plan import Budgets, ResearchPlan

        plan = ResearchPlan(
            plan_id="plan-cu", entity_kind="stock", entity_id="BE", objective="o",
            mode="targeted", questions=[], budgets=Budgets(), created_at=NOW,
        )
        claims = [
            {"claim_id": "c1", "status": "validated", "support_refs": ["ev-1"],
             "kind": "inference"},  # 无 verification = 旧数据 → unchecked
            {"claim_id": "c2", "status": "validated", "support_refs": ["ev-2"],
             "kind": "inference",
             "verification": {"references_valid": True, "evidence_support": "supported"}},
            {"claim_id": "c3", "status": "draft", "support_refs": [], "kind": "hypothesis"},
        ]
        a = assess(plan, claims=claims, observations=[], calculations=[])
        eq = a.evidence_quality
        assert eq["validated_claims"] == 2
        assert eq["validated_claims_content_unchecked"] == 1
        assert any("内容级核验" in n for n in a.notes)


# ---------------- 4. 研究策略与评审输入 ----------------


class TestPlanDisciplineAndRubricInput:
    def test_worker_discipline_question_driven(self):
        from finance_agent.research.loop import _worker_discipline

        qd = _worker_discipline(question_driven=True, has_document_reader="document")
        assert "按问题逐个推进" in qd
        assert "answer_question" in qd
        assert "read_document" in qd
        assert "按字段逐个推进" not in qd, "plan 模式不得把开放问题压回快速填字段"

        qd_edgar = _worker_discipline(question_driven=True, has_document_reader="edgar")
        assert "read_edgar_filing" in qd_edgar
        assert "read_document" not in qd_edgar, "提示只引用实际装配的工具"

        qd_no_reader = _worker_discipline(question_driven=True, has_document_reader="")
        assert "read_chunk" in qd_no_reader

        legacy = _worker_discipline(question_driven=False, has_document_reader="document")
        assert "按字段逐个推进" in legacy
        assert "propose_fact" in legacy

    def test_judge_digest_contains_claim_content_and_quotes(self, env):
        """rubric 输入升级：评审看到论断原文与支持摘录，而不是只有 ID 和计数。"""
        kb, metrics, events, writer, mw = env
        kb.add_evidence(Evidence(
            evidence_id="ev-d1", source_id="edgar",
            verbatim_quote="Firm backlog reached 300 million as of December 2023",
            retrieved_at=NOW, available_at=T0, pit_grade=PitGrade.A,
        ))
        tools, _ = make_tools(env)
        out = json.loads(tools["propose_claim"]({
            "statement": "在手订单 300 million 创历史新高", "kind": "fact_summary",
            "support_refs": ["ev-d1"],
        })["content"])
        claim_id = out["claim_id"]

        loop = _make_bare_loop(env)
        from finance_agent.research.report import IterationReport

        report = IterationReport(
            run_id="live-p0", round=1, entity="stock:BE",
            completeness_before=0.2, completeness_after=0.3,
            claims_written=[claim_id], progress=True,
        )
        digest = loop._build_judge_digest(report)
        assert "在手订单 300 million 创历史新高" in digest
        assert "Firm backlog reached 300 million" in digest

    def test_judge_digest_bounded(self, env):
        kb, metrics, events, writer, mw = env
        loop = _make_bare_loop(env)
        from finance_agent.research.report import IterationReport

        report = IterationReport(
            run_id="live-p0", round=1, entity="stock:BE",
            completeness_before=0.0, completeness_after=0.0,
            rejected=[{"field": f"f{i}", "reason": "x" * 500} for i in range(80)],
        )
        digest = loop._build_judge_digest(report)
        assert len(digest) <= loop._JUDGE_DIGEST_MAX_CHARS + 20
        assert "digest 截断" in digest

    def test_judge_digest_includes_question_conclusions(self, env):
        kb, metrics, events, writer, mw = env
        loop = _make_bare_loop(env)
        loop.plan_payload = {"questions": [
            {"question_id": "q1", "status": "answered",
             "conclusion": "订单口径为含税合同额", "unresolved": []},
            {"question_id": "q2", "status": "unanswered", "conclusion": ""},
        ]}
        from finance_agent.research.report import IterationReport

        report = IterationReport(
            run_id="live-p0", round=1, entity="stock:BE",
            completeness_before=0.5, completeness_after=0.5,
        )
        digest = loop._build_judge_digest(report)
        assert "订单口径为含税合同额" in digest
        assert "q2" not in digest  # 未推进的问题不占 digest


def _make_bare_loop(env):
    """只装配 digest 所需依赖的 ResearchLoop（不跑轮次）。"""
    from finance_agent.gateway.adapters.fixture import FixtureAdapter
    from finance_agent.gateway.gateway import DataGateway
    from finance_agent.gateway.models import SourceCapability
    from finance_agent.llm.mock import MockLLM
    from finance_agent.research.loop import ResearchLoop

    kb, metrics, events, writer, mw = env
    gateway = DataGateway(mode="live", events=events, run_id="live-p0")
    gateway.register(FixtureAdapter(
        SourceCapability(source_id="demo", pit_grade=PitGrade.A,
                         server_side_asof=False, description="夹具源"),
        records=[],
    ))
    return ResearchLoop(
        store=kb, events=events, writer=writer, gateway=gateway,
        llm=MockLLM([]), manifest=RunManifest(run_id="live-p0", mode=RunMode.LIVE),
        metrics=metrics, metric_writer=mw,
    )
