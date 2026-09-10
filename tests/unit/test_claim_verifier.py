"""P2-A 证据核验与研究路径验收（tools-plugins 方案 §5.4/§8.1/§8.3）。

覆盖：
- EvidencePack：从既有 typed 数据组装（原文优先、观测背后的证据一并进包、
  不可解析引用显式、input_hash 幂等）；
- 硬检查（确定性代码）：引用可解析、论断数字与原文逐字、跨主体错配、开放冲突；
- 内容检查（LLM 核验意见）：原子论断聚合 supported/contradicted/insufficient、
  数字失败封顶 partially_supported、解析失败诚实降级（不冒充已核验）；
- 发布规则联动：contradicted 的 validated 论断降级 draft；ArtifactValidator
  对 contradicted 引用硬失败、insufficient 软问题；
- 反证闭环：counter_search 记录落库；无记录 → next_actions 要求补检索留痕；
- submit_question_result 批量提交（逐项门禁不放松、新引用自动并入答案）；
- track_sub_question 内部子问题（版本化追加、不扩预算/范围、幂等、越位拒绝）；
- 语义压缩状态卡：research/context_compressed 事件（来源区间+hash）+ 回流下一轮 brief。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation
from finance_agent.knowledge.models import Evidence, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply
from finance_agent.llm.mock import MockLLM
from finance_agent.research.evidence_pack import build_evidence_pack
from finance_agent.research.verifier import run_hard_checks, verify_claim

NOW = datetime.now(UTC)
T0 = datetime(2024, 3, 1, tzinfo=UTC)
T1 = datetime(2024, 6, 1, tzinfo=UTC)
FY2023 = MetricPeriod(start=date(2023, 1, 1), end=date(2023, 12, 31),
                      frequency="FY", fiscal_label="FY2023")


def make_env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    kb.add_evidence(Evidence(
        evidence_id="ev-backlog", source_id="edgar", url="https://sec.gov/f",
        verbatim_quote="Firm backlog reached 300 million USD as of December 2023",
        retrieved_at=NOW, available_at=T0, pit_grade=PitGrade.A,
    ))
    kb.add_evidence(Evidence(
        evidence_id="ev-news", source_id="web_search", url="https://news/x",
        verbatim_quote="analysts question the sustainability of the backlog",
        retrieved_at=NOW, available_at=T1, pit_grade=PitGrade.B,
    ))
    obs_id, _ = metrics.assert_observation(ReportedObservation(
        entity_kind="stock", entity_id="BE", metric_key="backlog", period=FY2023,
        value="300000000", unit="USD", currency="USD", basis="GAAP",
        raw=RawValue(value_text="300 million", unit_text="USD", quote_ref="ev-backlog"),
        locator={"page": "42"},
        evidence_refs=["ev-backlog"], knowledge_time=T0, source_available_at=T0,
        retrieved_at=T1, created_at=T1, pit_grade=PitGrade.A,
    ))
    return kb, metrics, events, writer, mw, obs_id


def save_claim(metrics, claim_id, *, statement, support, counter=(), status="validated",
               kind="inference", question_id=None):
    metrics.save_claim(claim_id=claim_id, namespace="prod", payload={
        "claim_id": claim_id, "entity_kind": "stock", "entity_id": "BE",
        "statement": statement, "kind": kind, "status": status,
        "question_id": question_id, "support_refs": list(support),
        "counter_refs": list(counter), "limitations": [],
        "created_at": T1.isoformat(), "namespace": "prod",
    })


def review_llm(atomic: list[dict], *, reasoning=None, next_actions=None) -> MockLLM:
    return MockLLM([AssistantReply(content=json.dumps({
        "atomic_claims": atomic,
        "reasoning_review": reasoning or {"premises_explicit": True, "boundary_ok": True,
                                          "alternative_explanations": []},
        "next_actions": next_actions or [],
    }, ensure_ascii=False))])


# ---------------- EvidencePack ----------------


class TestEvidencePack:
    def test_pack_resolves_spans_and_pulls_observation_evidence(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-p1",
                   statement="在手订单 300 million 支撑增长", support=[obs_id])
        payload = metrics.get_claim("claim-p1")
        pack = build_evidence_pack(kb, metrics, entity_kind="stock", entity_id="BE",
                                   claim_payload=payload)
        refs = {s.ref for s in pack.supporting_spans}
        assert obs_id in refs
        assert "ev-backlog" in refs, "观测背后的证据原文必须进包（核验看原文不看摘要）"
        assert pack.observation_refs == [obs_id]
        assert pack.source_families.get("issuer_filing") == 1
        assert pack.input_hash

    def test_pack_input_hash_stable(self, tmp_path):
        kb, metrics, *_ , obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-p2", statement="订单支撑增长", support=[obs_id])
        payload = metrics.get_claim("claim-p2")
        h1 = build_evidence_pack(kb, metrics, entity_kind="stock", entity_id="BE",
                                 claim_payload=payload, as_of=T1).input_hash
        h2 = build_evidence_pack(kb, metrics, entity_kind="stock", entity_id="BE",
                                 claim_payload=payload, as_of=T1).input_hash
        assert h1 == h2

    def test_unresolved_refs_visible(self, tmp_path):
        kb, metrics, *_ = make_env(tmp_path)
        save_claim(metrics, "claim-p3", statement="某个结论", support=["ev-ghost"])
        pack = build_evidence_pack(kb, metrics, entity_kind="stock", entity_id="BE",
                                   claim_payload=metrics.get_claim("claim-p3"))
        assert pack.unresolved_refs == ["ev-ghost"]
        assert any("不可解析" in m for m in pack.missing_evidence)


# ---------------- 硬检查 ----------------


class TestHardChecks:
    def test_numeric_match_and_year_exclusion(self, tmp_path):
        kb, metrics, *_, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-h1",
                   statement="2023 年末在手订单达 300 million", support=[obs_id])
        pack = build_evidence_pack(kb, metrics, entity_kind="stock", entity_id="BE",
                                   claim_payload=metrics.get_claim("claim-h1"))
        refs_valid, numeric, issues = run_hard_checks(metrics.get_claim("claim-h1"), pack)
        assert refs_valid and numeric == "passed", issues
        # 年份 2023 不参与数字核对（否则必然误报）

    def test_numeric_mismatch_failed(self, tmp_path):
        kb, metrics, *_, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-h2",
                   statement="在手订单达 950 million", support=[obs_id])
        pack = build_evidence_pack(kb, metrics, entity_kind="stock", entity_id="BE",
                                   claim_payload=metrics.get_claim("claim-h2"))
        _, numeric, issues = run_hard_checks(metrics.get_claim("claim-h2"), pack)
        assert numeric == "failed"
        assert any("950" in i for i in issues)

    def test_unresolved_refs_fail(self, tmp_path):
        kb, metrics, *_ = make_env(tmp_path)
        save_claim(metrics, "claim-h3", statement="结论", support=["ev-ghost"])
        pack = build_evidence_pack(kb, metrics, entity_kind="stock", entity_id="BE",
                                   claim_payload=metrics.get_claim("claim-h3"))
        refs_valid, _, issues = run_hard_checks(metrics.get_claim("claim-h3"), pack)
        assert not refs_valid and any("不可解析" in i for i in issues)


# ---------------- 内容核验 + 发布规则联动 ----------------


class TestContentVerification:
    def test_supported_keeps_validated_and_records(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-v1",
                   statement="在手订单 300 million 创历史新高", support=[obs_id])
        llm = review_llm([{"text": "在手订单 300 million", "verdict": "supported",
                           "supporting_refs": [obs_id], "missing_conditions": [],
                           "mismatches": [], "notes": ""}])
        result = verify_claim(kb, metrics, claim_id="claim-v1", llm=llm,
                              events=events,
                              manifest=RunManifest(run_id="r-v", mode=RunMode.LIVE),
                              entity_kind="stock", entity_id="BE")
        assert result.evidence_support == "supported"
        assert result.content_review_available is True
        assert result.status_before == "validated" and result.status_after == "validated"
        updated = metrics.get_claim("claim-v1")
        assert updated["verification"]["evidence_support"] == "supported"
        assert updated["verification"]["references_valid"] is True
        # 无 counter_refs 且未传 counter_search → 反证补检索行动项（不制造反证凑数）
        assert any("反证" in a for a in result.next_actions)
        evs = [e for e in events.read("r-v") if e.type == "research/claim_verified"]
        assert evs and evs[0].payload["evidence_support"] == "supported"

    def test_contradicted_downgrades_and_blocks_publish(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-v2",
                   statement="在手订单 300 million 已全部转化为收入", support=[obs_id])
        llm = review_llm([
            {"text": "在手订单 300 million", "verdict": "supported",
             "supporting_refs": [obs_id]},
            {"text": "已全部转化为收入", "verdict": "contradicted",
             "supporting_refs": [], "notes": "原文只说订单余额，未说转化完成"},
        ])
        result = verify_claim(kb, metrics, claim_id="claim-v2", llm=llm, events=events,
                              manifest=RunManifest(run_id="r-v2", mode=RunMode.LIVE),
                              entity_kind="stock", entity_id="BE")
        assert result.evidence_support == "contradicted"
        assert result.status_before == "validated" and result.status_after == "draft"
        # 发布规则：报告引用 contradicted 论断 → 硬失败（ArtifactValidator）
        from finance_agent.research.artifacts import (
            ArtifactValidator,
            ClaimBlock,
            ReportDocument,
            ResearchArtifact,
        )

        doc = ReportDocument(entity_kind="stock", entity_id="BE", title="t",
                             blocks=[ClaimBlock(claim_id="claim-v2")])
        artifact = ResearchArtifact(
            entity_kind="stock", entity_id="BE", title="t", report_document=doc,
            claim_ids=["claim-v2"], status="draft", sufficiency="partial",
            created_at=NOW, run_id="r-v2",
        ).with_id()
        issues = ArtifactValidator(kb=kb, metric_store=metrics).validate(artifact)
        codes = {(i.code, i.hard) for i in issues}
        assert ("contradicted_claim_ref", True) in codes

    def test_numeric_failed_caps_at_partially_supported(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-v3",
                   statement="在手订单达 950 million", support=[obs_id])
        llm = review_llm([{"text": "在手订单达 950 million", "verdict": "supported",
                           "supporting_refs": [obs_id]}])
        result = verify_claim(kb, metrics, claim_id="claim-v3", llm=llm, events=events,
                              manifest=RunManifest(run_id="r-v3", mode=RunMode.LIVE),
                              entity_kind="stock", entity_id="BE")
        assert result.numeric_checks == "failed"
        assert result.evidence_support == "partially_supported", \
            "模型说 supported 也压不过数字硬检查（不得用软评分抵消硬失败）"
        assert result.status_after == "validated"  # partially 不降级，但记录在案

    def test_llm_unavailable_honest_degradation(self, tmp_path):
        """LLM 输出不可解析 → 只做硬检查，evidence_support 保持 unchecked（不冒充核验）。"""
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-v4", statement="在手订单 300 million", support=[obs_id])
        llm = MockLLM([AssistantReply(content="我觉得这条论断基本没问题（非 JSON）")])
        result = verify_claim(kb, metrics, claim_id="claim-v4", llm=llm, events=events,
                              manifest=RunManifest(run_id="r-v4", mode=RunMode.LIVE),
                              entity_kind="stock", entity_id="BE")
        assert result.content_review_available is False
        assert result.evidence_support == "unchecked"
        assert result.references_valid is True and result.numeric_checks == "passed"
        assert result.status_after == "validated"  # 未核验 ≠ 推翻，不降级
        assert any("不可解析" in n for n in
                   metrics.get_claim("claim-v4")["verification"]["notes"])

    def test_no_llm_hard_checks_only(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-v5", statement="在手订单 300 million", support=[obs_id])
        result = verify_claim(kb, metrics, claim_id="claim-v5", llm=None, events=events,
                              manifest=RunManifest(run_id="r-v5", mode=RunMode.LIVE),
                              entity_kind="stock", entity_id="BE")
        assert result.content_review_available is False
        assert result.reviewed_by == "hard-checks-only"

    def test_counter_search_recorded(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-v6", statement="在手订单 300 million", support=[obs_id])
        llm = review_llm([{"text": "在手订单 300 million", "verdict": "supported",
                           "supporting_refs": [obs_id]}])
        result = verify_claim(kb, metrics, claim_id="claim-v6", llm=llm, events=events,
                              manifest=RunManifest(run_id="r-v6", mode=RunMode.LIVE),
                              entity_kind="stock", entity_id="BE",
                              counter_search={"queries": ["backlog cancellation"],
                                              "sources": ["web_search"],
                                              "found": False, "notes": "未见取消公告"})
        assert result.counter_evidence_search is True
        verification = metrics.get_claim("claim-v6")["verification"]
        assert verification["counter_evidence_search"] is True
        assert any("反证检索记录" in n for n in verification["notes"])
        assert not any("补充反证检索" in a for a in result.next_actions)

    def test_cross_entity_claim_rejected(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        metrics.save_claim(claim_id="claim-other", namespace="prod", payload={
            "claim_id": "claim-other", "entity_kind": "stock", "entity_id": "NVDA",
            "statement": "别的公司的论断", "kind": "inference", "status": "draft",
            "support_refs": [], "counter_refs": [], "limitations": [],
            "created_at": T1.isoformat(), "namespace": "prod",
        })
        with pytest.raises(ValueError, match="跨上下文拒绝"):
            verify_claim(kb, metrics, claim_id="claim-other", llm=None,
                         entity_kind="stock", entity_id="BE")


# ---------------- 工具层：verify_claim / submit_question_result / track_sub_question ----


class TestToolWiring:
    def _tools(self, tmp_path, llm=None):
        from finance_agent.research.evidence_desk import ChunkStore
        from finance_agent.research.tools import make_research_tools

        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        tools, tracker = make_research_tools(
            store=kb, writer=writer,
            manifest=RunManifest(run_id="r-t", mode=RunMode.LIVE),
            entity_kind="stock", entity_id="BE", chunk_store=ChunkStore(),
            events=events, metrics=metrics, metric_writer=mw,
            verify_llm=llm,
        )
        return tools, tracker, kb, metrics, events, obs_id

    def test_verify_claim_tool_roundtrip(self, tmp_path):
        llm = review_llm([{"text": "订单 300 million", "verdict": "supported",
                           "supporting_refs": []}])
        tools, tracker, kb, metrics, events, obs_id = self._tools(tmp_path, llm=llm)
        save_claim(metrics, "claim-t1", statement="在手订单 300 million", support=[obs_id])
        out = json.loads(tools["verify_claim"]({"claim_id": "claim-t1"})["content"])
        assert out["evidence_support"] == "supported"
        assert out["claim_id"] == "claim-t1"

    def test_verify_claim_tool_unknown_rejected(self, tmp_path):
        tools, tracker, *_ = self._tools(tmp_path)
        out = tools["verify_claim"]({"claim_id": "claim-nope"})
        assert out["content"].startswith("rejected:")
        assert tracker.rejected

    def test_submit_question_result_batch_gates_per_item(self, tmp_path):
        """批量提交：合法观测+合法论断+答案一次通过；非法条目逐项给拒绝原因。"""
        self._tools(tmp_path)  # 先建库与证据（同一 tmp_path 的 SQLite 文件共享）
        kb, metrics, events, writer, mw, _ = _reopen(tmp_path)
        from finance_agent.research.plan import Budgets, ResearchPlan, ResearchQuestion

        plan = ResearchPlan(
            plan_id="plan-sq", entity_kind="stock", entity_id="BE", objective="o",
            mode="targeted",
            questions=[ResearchQuestion(question_id="q1", text="订单规模？",
                                        priority="high", module="business")],
            budgets=Budgets(), created_at=T0,
        )
        metrics.save_plan(plan_id="plan-sq", namespace="prod",
                          payload=plan.model_dump(mode="json"))
        tools2, _tracker = _tools_with_plan(tmp_path, "plan-sq")
        out = json.loads(tools2["submit_question_result"]({
            "observations": [{
                "metric_key": "backlog", "value_text": "300 million",
                "unit": "USD", "unit_text": "USD", "currency": "USD",
                "period": {"start": "2023-01-01", "end": "2023-12-31",
                           "frequency": "FY", "fiscal_label": "FY2023"},
                "evidence_ids": ["ev-backlog"],
                "locator": {"page": "42"},
            }, {
                "metric_key": "backlog", "value_text": "999 million",  # 数字不在摘录
                "unit": "USD", "currency": "USD",
                "period": {"start": "2023-01-01", "end": "2023-12-31",
                           "frequency": "FY"},
                "evidence_ids": ["ev-backlog"],
            }],
            "claims": [{
                "statement": "在手订单 300 million 创历史新高", "kind": "fact_summary",
                "support_refs": ["ev-backlog"], "question_id": "q1",
            }],
            "question_id": "q1", "status": "answered",
            "conclusion": "FY2023 末在手订单 300 million USD（发行人披露）",
        })["content"])
        # 观测 1 接受、观测 2 拒绝（逐项门禁，不整批失败）
        assert out["observations"][0].get("observation_id")
        assert "rejected" in out["observations"][1]
        assert out["claims"][0].get("claim_id")
        assert out["answer"].get("status") == "answered"
        # 新接受的 obs/claim id 自动并入 support_refs（减少机械往返）
        q = next(q for q in metrics.get_plan("plan-sq")["questions"]
                 if q["question_id"] == "q1")
        assert out["observations"][0]["observation_id"] in q["support_refs"]
        assert out["claims"][0]["claim_id"] in q["support_refs"]

    def test_track_sub_question_appends_without_budget_change(self, tmp_path):
        self._tools(tmp_path)  # 建库与证据
        kb, metrics, events, writer, mw, _ = _reopen(tmp_path)
        from finance_agent.research.plan import Budgets, ResearchPlan, ResearchQuestion

        plan = ResearchPlan(
            plan_id="plan-sub", entity_kind="stock", entity_id="BE", objective="o",
            mode="targeted",
            questions=[ResearchQuestion(question_id="q1", text="订单？",
                                        priority="high", module="business")],
            budgets=Budgets(max_rounds=3, retrieval_calls=30), created_at=T0,
        )
        metrics.save_plan(plan_id="plan-sub", namespace="prod",
                          payload=plan.model_dump(mode="json"))
        tools2, _ = _tools_with_plan(tmp_path, "plan-sub")
        out = json.loads(tools2["track_sub_question"]({
            "parent_question_id": "q1",
            "text": "订单中 Oracle 占比是否披露",
            "trigger_evidence": ["ev-backlog"],
            "priority": "high",
            "exit_condition": "找到分部披露或确认未披露",
        })["content"])
        assert out["sub_question"]["sub_id"].startswith("sub-")
        # 预算与范围不变（硬约束）
        after = metrics.get_plan("plan-sub")
        assert after["budgets"]["max_rounds"] == 3
        assert after["budgets"]["retrieval_calls"] == 30
        assert len(after["questions"]) == 1
        assert after["questions"][0]["sub_questions"][0]["text"] == "订单中 Oracle 占比是否披露"
        # 幂等：同文本再登记返回已有条目
        again = json.loads(tools2["track_sub_question"]({
            "parent_question_id": "q1", "text": "订单中 Oracle 占比是否披露",
        })["content"])
        assert again["sub_question"]["sub_id"] == out["sub_question"]["sub_id"]
        # 事件留痕
        evs = [e for e in events.read("r-plan") if e.type == "research/subquestion_added"]
        assert evs and evs[0].payload["parent_question_id"] == "q1"

    def test_track_sub_question_unknown_parent_rejected(self, tmp_path):
        self._tools(tmp_path)
        from finance_agent.research.plan import Budgets, ResearchPlan, ResearchQuestion

        kb, metrics, events, writer, mw, _ = _reopen(tmp_path)
        plan = ResearchPlan(
            plan_id="plan-sub2", entity_kind="stock", entity_id="BE", objective="o",
            mode="targeted",
            questions=[ResearchQuestion(question_id="q1", text="订单？",
                                        priority="high", module="business")],
            budgets=Budgets(), created_at=T0,
        )
        metrics.save_plan(plan_id="plan-sub2", namespace="prod",
                          payload=plan.model_dump(mode="json"))
        tools2, tracker2 = _tools_with_plan(tmp_path, "plan-sub2")
        out = tools2["track_sub_question"]({
            "parent_question_id": "q-ghost", "text": "幽灵子问题",
        })
        assert out["content"].startswith("rejected:")
        assert "计划范围不可扩展" in out["content"]
        assert tracker2.rejected


def _reopen(tmp_path):
    """重新打开同一 tmp_path 的存储（SQLite 文件共享，跨连接可见）。"""
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    return kb, metrics, events, writer, mw, None


def _tools_with_plan(tmp_path, plan_id):
    """带冻结计划的工具集（submit/track 门禁需要 plan_id 注入）。"""
    from finance_agent.research.evidence_desk import ChunkStore
    from finance_agent.research.tools import make_research_tools

    kb, metrics, events, writer, mw, _ = _reopen(tmp_path)
    return make_research_tools(
        store=kb, writer=writer,
        manifest=RunManifest(run_id="r-plan", mode=RunMode.LIVE),
        entity_kind="stock", entity_id="BE", chunk_store=ChunkStore(),
        events=events, metrics=metrics, metric_writer=mw, plan_id=plan_id,
    )


# ---------------- 语义压缩状态卡（§8.3） ----------------


class TestStateCard:
    def test_compressed_event_and_brief_reflow(self, tmp_path):
        from finance_agent.gateway.adapters.fixture import FixtureAdapter
        from finance_agent.gateway.gateway import DataGateway
        from finance_agent.gateway.models import DataRecord, SourceCapability
        from finance_agent.knowledge.schema import STOCK_SCHEMA
        from finance_agent.llm.base import ToolCall
        from finance_agent.research.loop import ResearchLoop

        kb, metrics, events, writer, mw, _ = make_env(tmp_path)
        gateway = DataGateway(mode="live", events=events, run_id="live-sc")
        gateway.register(FixtureAdapter(
            SourceCapability(source_id="demo", pit_grade=PitGrade.A,
                             server_side_asof=False, description="夹具源"),
            records=[DataRecord(source_id="demo",
                                payload={"form": "10-K", "text": "backlog 300 million"},
                                url="demo://f", available_at=T0)],
        ))
        field = next(iter(STOCK_SCHEMA.required))
        script = [
            # 第 1 轮：检索 → 登记 → 写事实（有进展；轮末产状态卡）
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c0", name="query_demo", arguments={})]),
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c1", name="register_evidence", arguments={
                    "chunk_id": "chk-0001", "verbatim_quote": "backlog 300 million",
                    "evidence_id": "ev-round1"})]),
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c2", name="propose_fact", arguments={
                    "field": field, "value": "backlog 300 million 的披露描述",
                    "evidence_ids": ["ev-round1"]})]),
            AssistantReply(content="round1 done"),
            # 第 2 轮：无进展 → stalled（此前 brief 应带状态卡）
            AssistantReply(content="nothing new"),
        ]
        llm = MockLLM(script)
        loop = ResearchLoop(
            store=kb, events=events, writer=writer, gateway=gateway, llm=llm,
            manifest=RunManifest(run_id="live-sc", mode=RunMode.LIVE),
            completeness_target=0.99, gateway_sources=gateway.source_ids(),
            max_rounds=3,
        )
        loop.run("stock", "BE", "订单口径验证")
        compressed = [e for e in events.read("live-sc")
                      if e.type == "research/context_compressed"]
        assert compressed, "轮末必须落语义压缩状态卡事件"
        card = compressed[0].payload
        assert card["objective"] == "订单口径验证" and card["round"] == 1
        assert card["state_hash"]
        assert card["source_events"]["run_id"] == "live-sc"
        assert card["source_events"]["to_seq"] >= card["source_events"]["from_seq"]
        assert any(e["evidence_id"] == "ev-round1" for e in card["evidence_recent"])
        # 状态卡回流下一轮 brief（裁剪丢工具结果，不丢问题状态与证据 ID）
        briefs = "\n".join(
            m["content"] for call in llm.received for m in call if m.get("role") == "user"
        )
        assert "状态卡" in briefs and "ev-round1" in briefs
