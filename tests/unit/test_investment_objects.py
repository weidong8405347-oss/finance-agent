"""Investment Objects 推导验收（升级方案 §12-§15/§28/§34）。

关键场景：ThesisObject 从 claims 确定性推导（计数/状态映射置信度/监测链接/
反证义务）；fact_summary 不进论点对象（§15 事实分层）；superseded 不进；
confidence 不从文本猜（无映射源 → None）；monitor 只做确定性关联；
MoatAssessment 十维骨架分数留空（不编造）。
"""

from datetime import UTC, datetime

import pytest

from finance_agent.dossier.investment_objects import (
    THESIS_CAP,
    derive_investment_objects,
    derive_moat_assessments,
    derive_theses,
)
from finance_agent.dossier.projector import DossierProjector
from finance_agent.dossier.service import DossierService
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.models import Evidence, PitGrade
from finance_agent.knowledge.store import BitemporalStore

T1 = datetime(2025, 1, 31, tzinfo=UTC)


def _claim(cid, *, kind="analysis", status="validated", statement="论点",
           support=None, counter=None, limitations=None, question_id=None,
           evidence_support="unchecked"):
    return {
        "claim_id": cid, "kind": kind, "status": status, "statement": statement,
        "support_refs": support or [], "counter_refs": counter or [],
        "limitations": limitations or [], "question_id": question_id,
        "verification": {"evidence_support": evidence_support},
        "created_at": "2025-01-01T00:00:00+00:00",
    }


class TestDeriveTheses:
    def test_counts_and_bear_obligation(self):
        claims = [_claim("c1", support=["ev-1", "ev-2", "obs-1"], counter=["ev-9"],
                         limitations=["待验证 X"])]
        theses = derive_theses(claims, {}, None)
        assert len(theses) == 1
        t = theses[0]
        assert t["support_count"] == 3 and t["counter_count"] == 1
        assert t["unresolved_count"] == 1
        assert t["bear_case_status"] == "met"
        assert t["supports"] == ["ev-1", "ev-2", "obs-1"]
        assert t["contradicts"] == ["ev-9"]

    def test_bear_obligation_unmet_without_counter(self):
        """§14：counter_refs 为空 → unmet（反证义务未履行），不假装无反证。"""
        theses = derive_theses([_claim("c1", support=["ev-1"])], {}, None)
        assert theses[0]["bear_case_status"] == "unmet"

    def test_fact_summary_not_a_thesis(self):
        """§15 事实分层：fact_summary 是事实层，不进论点对象。"""
        claims = [_claim("c1", kind="fact_summary"), _claim("c2", kind="inference")]
        theses = derive_theses(claims, {}, None)
        assert [t["id"] for t in theses] == ["c2"]

    def test_superseded_excluded(self):
        claims = [_claim("c1", status="superseded"), _claim("c2")]
        assert [t["id"] for t in derive_theses(claims, {}, None)] == ["c2"]

    def test_confidence_from_status_mapping_only(self):
        """confidence 只来自状态映射；basis 写明推导链；不从文本猜。"""
        cases = [
            ({"status": "validated", "evidence_support": "supported"}, 0.8),
            ({"status": "validated", "evidence_support": "partially_supported"}, 0.6),
            ({"status": "validated", "evidence_support": "unchecked"}, 0.5),
            ({"status": "validated", "evidence_support": "insufficient"}, 0.35),
            ({"status": "validated", "evidence_support": "contradicted"}, 0.15),
            ({"status": "draft", "evidence_support": "unchecked"}, 0.3),
        ]
        for opts, expected in cases:
            c = _claim("c1", status=opts["status"],
                       evidence_support=opts["evidence_support"])
            t = derive_theses([c], {}, None)[0]
            assert t["confidence"] == expected, opts
            assert "状态映射" in t["confidence_basis"]
        # 未知状态 → None（不猜）
        t = derive_theses([_claim("c1", status="archived")], {}, None)[0]
        assert t["confidence"] is None
        # 方向无确定性来源 → 恒 None
        assert all(
            derive_theses([_claim("c9", statement="强烈看涨超预期")], {}, None)[0]["direction"]
            is None for _ in range(1)
        )

    def test_importance_from_plan_priority(self):
        plan = {"questions": [
            {"question_id": "q1", "text": "高优先问题？", "priority": "high"},
            {"question_id": "q2", "text": "低优先问题？", "priority": "low"},
        ]}
        claims = [_claim("c1", question_id="q1"), _claim("c2", question_id="q2"),
                  _claim("c3", question_id="q-unknown")]
        theses = derive_theses(claims, {}, plan)
        by_id = {t["id"]: t for t in theses}
        assert by_id["c1"]["importance"] == 0.9
        assert by_id["c2"]["importance"] == 0.3
        assert by_id["c3"]["importance"] is None
        # 标题来自问题文本
        assert by_id["c1"]["title"] == "高优先问题？"
        # 排序：importance 高者在前
        assert theses[0]["id"] == "c1"

    def test_title_fallback_first_sentence(self):
        c = _claim("c1", statement="瓶颈迁移到临床验证。详细论述展开。" * 30)
        t = derive_theses([c], {}, None)[0]
        assert t["title"] == "瓶颈迁移到临床验证"
        assert t["summary"].startswith("瓶颈迁移到临床验证")

    def test_monitor_links_deterministic(self):
        """monitor：证据相交或公司相交才链接；毫不相干不硬连。"""
        structures = {
            "validation_timeline": {"items": [
                {"event": "III 期读出", "evidence_refs": ["ev-1"], "company_refs": []},
                {"event": "产能爬坡", "evidence_refs": [], "company_refs": ["xtalpi-2228hk"]},
                {"event": "无关事件", "evidence_refs": ["ev-99"], "company_refs": []},
            ]},
            "candidate_assessment": {"candidates": [
                {"entity_id": "xtalpi-2228hk", "name": "晶泰控股"},
            ]},
        }
        c = _claim("c1", statement="晶泰控股的平台壁垒", support=["ev-1"])
        t = derive_theses([c], structures, None)[0]
        assert "III 期读出" in t["monitor"]  # 证据相交
        assert "产能爬坡" in t["monitor"]    # 公司相交
        assert "无关事件" not in t["monitor"]
        assert t["related_companies"] == ["xtalpi-2228hk"]

    def test_thesis_cap(self):
        claims = [_claim(f"c{i}") for i in range(THESIS_CAP + 5)]
        assert len(derive_theses(claims, {}, None)) == THESIS_CAP


class TestDeriveMoatAssessments:
    def test_skeleton_scores_empty(self):
        """十维骨架：score/confidence/trend 全空（无逐维度评级证据不编造）。"""
        structures = {"candidate_assessment": {"candidates": [{
            "entity_id": "a", "name": "甲", "tier": "included",
            "moat_evidence": ["闭环数据"], "commercial_evidence": ["FY25 盈利"],
            "sustainability_evidence": [], "counter_evidence": ["单一来源"],
        }]}}
        out = derive_moat_assessments([], structures)
        assert len(out) == 1
        m = out[0]
        assert len(m["dimensions"]) == 10
        assert all(d["score"] is None for d in m["dimensions"].values())
        assert m["evidence_groups"]["moat"] == ["闭环数据"]
        assert m["evidence_groups"]["counter"] == ["单一来源"]
        assert m["notes"]  # 诚实说明

    def test_claim_refs_by_name_mention(self):
        structures = {"candidate_assessment": {"candidates": [
            {"entity_id": "a", "name": "甲公司"},
        ]}}
        claims = [_claim("c1", statement="甲公司的数据飞轮成立"),
                  _claim("c2", statement="与任何候选无关")]
        out = derive_moat_assessments(claims, structures)
        assert out[0]["claim_refs"] == ["c1"]

    def test_empty_candidates(self):
        assert derive_moat_assessments([], {"candidate_assessment": {"candidates": []}}) == []


class TestSnapshotIntegration:
    """快照集成：investment_objects 进冻结快照；模块 payload 透出 theses。"""

    @pytest.fixture()
    def env(self, tmp_path):
        kb = BitemporalStore(tmp_path / "kb.db")
        metrics = MetricStore(tmp_path / "m.db")
        events = EventStore(tmp_path / "e.db")
        projector = DossierProjector(kb=kb, metrics=metrics)
        service = DossierService(kb=kb, metrics=metrics, projector=projector, events=events)
        return kb, metrics, events, projector, service

    def test_snapshot_carries_investment_objects(self, env):
        from finance_agent.research.artifacts import ResearchClaim

        kb, metrics, _e, _p, service = env
        kb.add_evidence(Evidence(
            evidence_id="ev-io1", source_id="edgar", url="https://sec.gov/a",
            verbatim_quote="revenue driven by datacenter orders",
            retrieved_at=T1, available_at=T1, pit_grade=PitGrade.A,
        ))
        claim = ResearchClaim(
            claim_id="claim-io1", entity_kind="stock", entity_id="BE",
            statement="订单驱动收入增长", kind="inference",
            support_refs=["ev-io1"], status="validated",
            created_at=T1, evidence_cutoff=T1,
        )
        metrics.save_claim(claim_id="claim-io1", namespace="prod",
                           payload=claim.model_dump(mode="json"))
        snap, created = service.open("stock", "BE", run_id="io-1")
        assert created
        ios = snap["investment_objects"]
        assert len(ios["theses"]) == 1
        t = ios["theses"][0]
        assert t["id"] == "claim-io1" and t["support_count"] == 1
        assert t["confidence"] == 0.5  # validated + 内容未核验
        # 无候选评估 → moat 列表为空（不造骨架）
        assert ios["moat_assessments"] == []
        # 模块 payload 透出 theses（investment_snapshot）
        payload = service.module(snap["context"]["snapshot_id"], "investment_snapshot")
        assert payload.payload["theses"][0]["id"] == "claim-io1"

    def test_targeted_question_text_not_used_as_title(self):
        """事故回归 2026-09-12：targeted 问题的「文本」是操作指令，不得当论点标题
        （研究过程语言不进投资者视图 §10）；配方问题原文可用。"""
        plan = {"questions": [
            {"question_id": "targeted-ab12", "text": "这是结构补齐题，不是新检索题……",
             "priority": "high"},
            {"question_id": "value-chain", "text": "产业链各环节是什么？", "priority": "high"},
        ]}
        claims = [
            _claim("c1", question_id="targeted-ab12", statement="产业处于商业兑现早期。展开。"),
            _claim("c2", question_id="value-chain", statement="价值链三层结构。"),
        ]
        theses = derive_theses(claims, {}, plan)
        by_id = {t["id"]: t for t in theses}
        assert by_id["c1"]["title"] == "产业处于商业兑现早期"  # 首句，不是指令原文
        assert by_id["c2"]["title"] == "产业链各环节是什么？"  # 配方问题原文可用
