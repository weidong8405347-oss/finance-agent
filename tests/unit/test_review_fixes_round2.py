"""第二轮 code review 修复的回归验收（tools-plugins 方案交付复核，12 条意见）。

每条测试对应一个 review 复现路径：
- R1 发布门禁直接消费数值/推理硬检查（不仅 evidence_support）；
- R2 read_evidence 上下文隔离（namespace/主体/截止时间）；
- R3 核验结果落 claim 版本链（时态修订，历史投影不被改写）；
- R4 claim_invalidations 贯通读取（claims_as_of）与发布（ArtifactValidator）；
- R5 prepare/commit 缺省基线快照一致（变化集绑定选定快照）；
- R6 整合提交原子化（先全量校验，失效记录+台账同事务）；
- R7 失效沿完整依赖闭包传播（obs→calc→claim，限主体与时间范围）;
- R8 EvidencePack 间接引用递归展开到原始证据（循环/深度/总量上限）；
- R9 核验 verdict 枚举约束（未识别值丢弃，按核验不可用诚实降级）；
- R10 PDF 版本以原件哈希识别（解析前缀相同、原件不同 → 两份文档）；
- R11 完整性按实际页集合（中间缺页不被最大页码掩盖）；
- R12 官方来源按真实 hostname 识别（查询参数里的 sec.gov 不算发行人披露）。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from finance_agent.dossier.consolidator import ConsolidationError, ProfileConsolidator
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.documents import DocumentStore
from finance_agent.gateway.fetch import FetchedDocument
from finance_agent.gateway.text_quality import TextQuality
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation
from finance_agent.knowledge.models import Evidence, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply
from finance_agent.llm.mock import MockLLM
from finance_agent.research.artifacts import (
    ArtifactValidator,
    ClaimBlock,
    ReportDocument,
    ResearchArtifact,
)
from finance_agent.research.assessment import source_role
from finance_agent.research.context_tools import make_context_tools
from finance_agent.research.evidence_pack import build_evidence_pack
from finance_agent.research.verifier import verify_claim

NOW = datetime.now(UTC)
T0 = datetime(2024, 3, 1, tzinfo=UTC)
T1 = datetime(2024, 6, 1, tzinfo=UTC)
T2 = datetime(2024, 9, 1, tzinfo=UTC)
T3 = datetime(2024, 12, 1, tzinfo=UTC)
FY2023 = MetricPeriod(start=date(2023, 1, 1), end=date(2023, 12, 31),
                      frequency="FY", fiscal_label="FY2023")
RUN = RunManifest(run_id="r-rev2", mode=RunMode.LIVE)


def make_env(tmp_path, *, entity_id="BE"):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    kb.add_evidence(Evidence(
        evidence_id="ev-backlog", source_id="edgar", url="https://sec.gov/f",
        verbatim_quote="Firm backlog reached 300 million USD as of December 2023",
        retrieved_at=T0, available_at=T0, pit_grade=PitGrade.A,
    ))
    obs_id, _ = metrics.assert_observation(ReportedObservation(
        entity_kind="stock", entity_id=entity_id, metric_key="backlog", period=FY2023,
        value="300000000", unit="USD", currency="USD", basis="GAAP",
        raw=RawValue(value_text="300 million", unit_text="USD", quote_ref="ev-backlog"),
        evidence_refs=["ev-backlog"], knowledge_time=T0, source_available_at=T0,
        retrieved_at=T0, created_at=T0, pit_grade=PitGrade.A,
    ))
    return kb, metrics, events, writer, mw, obs_id


def save_claim(metrics, claim_id, *, statement="论断", support=(), status="validated",
               kind="inference", entity_id="BE", created=T1, recorded_at=None,
               namespace="prod", verification=None):
    payload = {
        "claim_id": claim_id, "entity_kind": "stock", "entity_id": entity_id,
        "statement": statement, "kind": kind, "status": status,
        "support_refs": list(support), "counter_refs": [], "limitations": [],
        "created_at": created.isoformat(), "namespace": namespace,
    }
    if verification:
        payload["verification"] = verification
    metrics.save_claim(claim_id=claim_id, namespace=namespace, payload=payload,
                       recorded_at=recorded_at)
    return payload


def review_llm(atomic: list[dict], *, reasoning=None) -> MockLLM:
    return MockLLM([AssistantReply(content=json.dumps({
        "atomic_claims": atomic,
        "reasoning_review": reasoning or {"premises_explicit": True, "boundary_ok": True,
                                          "alternative_explanations": []},
        "next_actions": [],
    }, ensure_ascii=False))])


def validate_claim_artifact(kb, metrics, claim_id):
    doc = ReportDocument(entity_kind="stock", entity_id="BE", title="t",
                         blocks=[ClaimBlock(claim_id=claim_id)])
    artifact = ResearchArtifact(
        entity_kind="stock", entity_id="BE", title="t", report_document=doc,
        claim_ids=[claim_id], status="draft", sufficiency="partial",
        created_at=NOW,
    ).with_id()
    return ArtifactValidator(kb=kb, metric_store=metrics).validate(artifact)


# ---------------- R1 发布门禁消费数值/推理硬检查 ----------------


class TestR1PublishGateConsumesHardChecks:
    def test_numeric_failed_claim_blocked_at_publish(self, tmp_path):
        """review 复现：原文 300 million、论断 950 million → numeric_checks=failed，
        报告校验此前返回零问题。现在：论断降级 draft + 发布门禁硬失败。"""
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-num", statement="在手订单达 950 million",
                   support=[obs_id])
        llm = review_llm([{"text": "在手订单达 950 million", "verdict": "supported",
                           "supporting_refs": [obs_id]}])
        result = verify_claim(kb, metrics, claim_id="claim-num", llm=llm,
                              events=events, manifest=RUN, now=T2,
                              entity_kind="stock", entity_id="BE")
        assert result.numeric_checks == "failed"
        assert result.status_after == "draft", "数值硬检查失败不得保持 validated"
        issues = validate_claim_artifact(kb, metrics, "claim-num")
        codes = {(i.code, i.hard) for i in issues}
        assert ("claim_numeric_check_failed", True) in codes

    def test_numeric_failed_without_llm_still_blocked(self, tmp_path):
        """内容审查不可用（evidence_support=unchecked）时，数值硬失败同样拦发布。"""
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-num2", statement="在手订单达 950 million",
                   support=[obs_id])
        result = verify_claim(kb, metrics, claim_id="claim-num2", llm=None,
                              events=events, manifest=RUN, now=T2,
                              entity_kind="stock", entity_id="BE")
        assert result.numeric_checks == "failed"
        assert result.evidence_support == "unchecked"
        assert result.status_after == "draft"
        codes = {(i.code, i.hard) for i in validate_claim_artifact(kb, metrics, "claim-num2")}
        assert ("claim_numeric_check_failed", True) in codes

    def test_analysis_review_failed_blocked(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-ana", statement="在手订单 300 million 证明护城河已形成",
                   support=[obs_id])
        llm = review_llm(
            [{"text": "在手订单 300 million", "verdict": "supported",
              "supporting_refs": [obs_id]}],
            reasoning={"premises_explicit": False, "boundary_ok": False,
                       "alternative_explanations": ["一次性订单堆叠"]},
        )
        result = verify_claim(kb, metrics, claim_id="claim-ana", llm=llm,
                              events=events, manifest=RUN, now=T2,
                              entity_kind="stock", entity_id="BE")
        assert result.analysis_review == "failed"
        assert result.status_after == "draft"
        codes = {(i.code, i.hard) for i in validate_claim_artifact(kb, metrics, "claim-ana")}
        assert ("claim_analysis_review_failed", True) in codes

    def test_partially_supported_visible_soft(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-part", statement="在手订单 300 million 且持续增长",
                   support=[obs_id])
        llm = review_llm([
            {"text": "在手订单 300 million", "verdict": "supported",
             "supporting_refs": [obs_id]},
            {"text": "持续增长", "verdict": "insufficient", "supporting_refs": []},
        ])
        result = verify_claim(kb, metrics, claim_id="claim-part", llm=llm,
                              events=events, manifest=RUN, now=T2,
                              entity_kind="stock", entity_id="BE")
        assert result.evidence_support == "partially_supported"
        codes = {(i.code, i.hard) for i in validate_claim_artifact(kb, metrics, "claim-part")}
        assert ("claim_evidence_partial", False) in codes
        assert not any(hard for _, hard in codes), "partially_supported 是降级可见而非硬拦"


# ---------------- R2 read_evidence 上下文隔离 ----------------


class TestR2ReadEvidenceIsolation:
    @pytest.fixture()
    def isolated(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        # 晚于上下文截止的证据与观测（生产库中的「后续资料」）
        kb.add_evidence(Evidence(
            evidence_id="ev-future", source_id="web_search",
            url="https://news.example/later",
            verbatim_quote="later filings show backlog doubled",
            retrieved_at=T2, available_at=T2, pit_grade=PitGrade.B,
        ))
        obs_future, _ = metrics.assert_observation(ReportedObservation(
            entity_kind="stock", entity_id="BE", metric_key="backlog_v2", period=FY2023,
            value="600000000", unit="USD", currency="USD", basis="GAAP",
            raw=RawValue(value_text="600 million", unit_text="USD",
                         quote_ref="ev-future"),
            evidence_refs=["ev-future"], knowledge_time=T2, source_available_at=T2,
            retrieved_at=T2, created_at=T2, pit_grade=PitGrade.B,
        ))
        # 其他实体的论断（生产库 BE 资料 vs 评估上下文 AAPL）
        save_claim(metrics, "claim-be", statement="BE 的订单结论",
                   support=[obs_id], entity_id="BE")
        # 评估上下文：2020 年时点的 AAPL
        tools = make_context_tools(
            kb=kb, metrics=metrics, entity_kind="stock", entity_id="AAPL",
            namespace="prod", as_of=datetime(2020, 1, 1, tzinfo=UTC),
        )
        return tools, obs_id, obs_future

    def test_cross_entity_typed_ref_rejected(self, isolated):
        tools, obs_id, _ = isolated
        out = json.loads(tools["read_evidence"]({"refs": [obs_id, "claim-be"]})["content"])
        assert out["failed"] == 2
        assert all("跨上下文拒绝" in i["error"] for i in out["items"])

    def test_future_evidence_rejected_by_cutoff(self, isolated):
        tools, _, _ = isolated
        out = json.loads(tools["read_evidence"]({"refs": ["ev-future"]})["content"])
        assert out["failed"] == 1
        assert "时态隔离" in out["items"][0]["error"]

    def test_in_scope_refs_still_readable(self, tmp_path):
        """同实体 + 截止前的引用不受影响（隔离不误伤正常回读）。"""
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        tools = make_context_tools(
            kb=kb, metrics=metrics, entity_kind="stock", entity_id="BE",
            namespace="prod", as_of=T1,
        )
        out = json.loads(tools["read_evidence"](
            {"refs": ["ev-backlog", obs_id]})["content"])
        assert out["resolved"] == 2 and out["failed"] == 0

    def test_cross_namespace_claim_rejected(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-eval", statement="评估命名空间的论断",
                   entity_id="BE", namespace="eval")
        tools = make_context_tools(
            kb=kb, metrics=metrics, entity_kind="stock", entity_id="BE",
            namespace="prod", as_of=T2,
        )
        out = json.loads(tools["read_evidence"]({"refs": ["claim-eval"]})["content"])
        assert out["failed"] == 1
        assert "跨命名空间" in out["items"][0]["error"]


# ---------------- R3 核验结果 = 时态修订 ----------------


class TestR3VerificationIsTemporalRevision:
    def test_historical_state_not_rewritten_by_later_downgrade(self, tmp_path):
        """review 复现：今天把论断降级后，过去时点的查询结果也变了。
        现在：版本链重建——T1 时点仍是 validated/未核验，当前时点是 draft/已核验。"""
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-hist", statement="在手订单 300 million",
                   support=[obs_id], created=T1, recorded_at=T1)
        mid = T1 + timedelta(days=1)
        before = metrics.claims_as_of("stock", "BE", mid, statuses=("validated",))
        assert len(before) == 1
        assert (before[0].get("verification") or {}).get("evidence_support", "unchecked") \
            == "unchecked"

        llm = review_llm([{"text": "已全部转化为收入", "verdict": "contradicted",
                           "supporting_refs": []}])
        result = verify_claim(kb, metrics, claim_id="claim-hist", llm=llm,
                              events=events, manifest=RUN, now=T2,
                              entity_kind="stock", entity_id="BE")
        assert result.status_after == "draft"

        current = metrics.claims_as_of("stock", "BE", NOW, statuses=("draft",))
        assert len(current) == 1
        assert current[0]["verification"]["evidence_support"] == "contradicted"
        # 历史投影不变：T1+1d 时点仍是当时的 validated + 未核验
        history = metrics.claims_as_of("stock", "BE", mid, statuses=("validated",))
        assert len(history) == 1
        assert history[0]["status"] == "validated"
        assert (history[0].get("verification") or {}).get("evidence_support", "unchecked") \
            == "unchecked"
        # 创建之前不可见
        assert metrics.claims_as_of("stock", "BE", T0, statuses=("validated",)) == []

    def test_preexisting_claims_backfilled_baseline_version(self, tmp_path):
        """版本表建立前的存量论断：迁移按 created_at 补基线版本，升级后修订有时态。"""
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        # 模拟迁移前入库的遗留行（只有当前指针，无版本记录）
        legacy = {
            "claim_id": "claim-legacy", "entity_kind": "stock", "entity_id": "BE",
            "statement": "存量论断", "kind": "inference", "status": "validated",
            "support_refs": [obs_id], "counter_refs": [], "limitations": [],
            "created_at": T1.isoformat(), "namespace": "prod",
        }
        metrics._conn.execute(  # noqa: SLF001 - 构造迁移前遗留形态（绕过 save_claim）
            "INSERT INTO research_claims (claim_id, namespace, entity_kind, entity_id,"
            " kind, question_id, status, statement, created_at, evidence_cutoff, run_id,"
            " superseded_by, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("claim-legacy", "prod", "stock", "BE", "inference", None, "validated",
             "存量论断", T1.isoformat(), None, None, None,
             json.dumps(legacy, ensure_ascii=False)),
        )
        metrics._conn.commit()  # noqa: SLF001
        # 重开库触发迁移：基线版本按 created_at 回填
        metrics2 = MetricStore(tmp_path / "m.db")
        history = metrics2.claims_as_of("stock", "BE", T1 + timedelta(days=1),
                                        statuses=("validated",))
        assert len(history) == 1
        # 迁移后的修订是时态的：降级后历史时点仍是 validated
        updated = dict(legacy, status="draft",
                       verification={"evidence_support": "contradicted"})
        metrics2.save_claim(claim_id="claim-legacy", namespace="prod", payload=updated,
                            recorded_at=T2)
        assert metrics2.claims_as_of("stock", "BE", NOW, statuses=("validated",)) == []
        assert len(metrics2.claims_as_of("stock", "BE", T1 + timedelta(days=1),
                                         statuses=("validated",))) == 1


# ---------------- R4 失效记录贯通读取与发布 ----------------


class TestR4InvalidationSemanticsLive:
    def test_invalidated_claim_leaves_current_reads(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-inv", statement="在手订单支撑增长",
                   support=[obs_id], created=T1, recorded_at=T1)
        assert metrics.claims_as_of("stock", "BE", NOW, statuses=("validated",))
        metrics.save_claim_invalidation(
            invalidation_id="inval-1", namespace="prod", entity_kind="stock",
            entity_id="BE", claim_id="claim-inv", reason="依赖观测口径作废",
            source_refs=[obs_id], invalidated_at=T2,
        )
        # 当前读取不再出现（失效语义生效）
        assert metrics.claims_as_of("stock", "BE", NOW, statuses=("validated",)) == []
        # 失效前的历史时点仍可见（时态合并，不改写历史）
        history = metrics.claims_as_of("stock", "BE", T1 + timedelta(days=1),
                                       statuses=("validated",))
        assert len(history) == 1
        # 审计视图可带标记取回
        audit = metrics.claims_as_of("stock", "BE", NOW, statuses=("validated",),
                                     include_invalidated=True)
        assert audit[0]["invalidated"] is True
        assert audit[0]["invalidation"]["reason"] == "依赖观测口径作废"

    def test_invalidated_claim_hard_fails_publish(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-inv2", statement="在手订单支撑增长",
                   support=[obs_id], created=T1, recorded_at=T1)
        metrics.save_claim_invalidation(
            invalidation_id="inval-2", namespace="prod", entity_kind="stock",
            entity_id="BE", claim_id="claim-inv2", reason="依赖已作废",
            source_refs=[], invalidated_at=T2,
        )
        codes = {(i.code, i.hard) for i in validate_claim_artifact(kb, metrics, "claim-inv2")}
        assert ("invalidated_claim_ref", True) in codes

    def test_read_evidence_flags_invalidated_claim(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-inv3", statement="在手订单支撑增长",
                   support=[obs_id], created=T1, recorded_at=T1)
        metrics.save_claim_invalidation(
            invalidation_id="inval-3", namespace="prod", entity_kind="stock",
            entity_id="BE", claim_id="claim-inv3", reason="重审待定",
            source_refs=[], invalidated_at=T2,
        )
        tools = make_context_tools(
            kb=kb, metrics=metrics, entity_kind="stock", entity_id="BE",
            namespace="prod", as_of=T3,
        )
        out = json.loads(tools["read_evidence"]({"refs": ["claim-inv3"]})["content"])
        assert out["resolved"] == 1  # 按 id 回读是合法审计
        assert out["items"][0]["invalidated"] is True
        assert out["items"][0]["invalidation"]["reason"] == "重审待定"


# ---------------- R5 prepare/commit 缺省快照一致 ----------------


class TestR5CommitSharesPrepareDefaultSnapshot:
    def _env_with_snapshot(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        metrics.save_snapshot(snapshot_id="snap-1", namespace="prod", payload={
            "entity": {"kind": "stock", "id": "BE"},
            "context": {"mode": "frozen", "namespace": "prod",
                        "as_of": T1.isoformat(), "snapshot_id": "snap-1",
                        "generated_at": T1.isoformat(), "projector_version": "test"},
            "data_hash": "hash-1",
            "inputs": {"fact_ids": {}, "observation_ids": [], "claims": [],
                       "artifacts": [], "resolution_ids": [], "calculation_ids": []},
        })
        return kb, metrics, events, obs_id

    def test_commit_without_base_uses_latest_snapshot_like_prepare(self, tmp_path):
        """review 复现：已有快照时 prepare 默认取最新、commit 默认按无快照算哈希，
        无并发修改也误报「基线已变化」。现在两端默认一致，直接提交成功。"""
        kb, metrics, events, obs_id = self._env_with_snapshot(tmp_path)
        cons = ProfileConsolidator(kb=kb, metrics=metrics, events=events)
        out = cons.prepare_update("stock", "BE")
        assert out["base_snapshot"]["snapshot_id"] == "snap-1"
        committed = cons.commit_update(
            "stock", "BE", change_set_id=out["change_set_id"],
            expected_base_hash=out["expected_base_hash"],
            note="无并发修改的默认提交", manifest=RUN,
        )
        assert committed["committed"] is True
        # 选定快照绑定到变化集（可追溯）
        assert committed["base_snapshot_id"] == "snap-1"
        stored = metrics.get_profile_update_commit(out["change_set_id"])
        assert stored["base_snapshot_id"] == "snap-1"

    def test_explicit_base_snapshot_still_honored(self, tmp_path):
        kb, metrics, events, obs_id = self._env_with_snapshot(tmp_path)
        cons = ProfileConsolidator(kb=kb, metrics=metrics, events=events)
        out = cons.prepare_update("stock", "BE", base_snapshot_id="snap-1")
        committed = cons.commit_update(
            "stock", "BE", change_set_id=out["change_set_id"],
            expected_base_hash=out["expected_base_hash"],
            note="显式基线", base_snapshot_id="snap-1", manifest=RUN,
        )
        assert committed["committed"] is True


# ---------------- R6 整合提交原子化 ----------------


class TestR6AtomicCommit:
    def test_invalid_second_entry_rolls_back_everything(self, tmp_path):
        """review 复现：第一条合法、第二条引用不存在 → 整体拒绝，但第一条已写入。
        现在：先全量校验再原子落库——零失效记录、零台账、零事件。"""
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-ok", statement="合法论断", support=[obs_id])
        cons = ProfileConsolidator(kb=kb, metrics=metrics, events=events)
        out = cons.prepare_update("stock", "BE")
        with pytest.raises(ConsolidationError, match="不存在或不属于"):
            cons.commit_update(
                "stock", "BE", change_set_id=out["change_set_id"],
                expected_base_hash=out["expected_base_hash"], note="n", manifest=RUN,
                invalidate_claims=[
                    {"claim_id": "claim-ok", "reason": "第一条合法"},
                    {"claim_id": "claim-ghost", "reason": "第二条引用不存在"},
                ],
            )
        assert metrics.invalidations_as_of("stock", "BE", NOW) == [], \
            "整体拒绝时不得落半截失效记录"
        assert metrics.get_profile_update_commit(out["change_set_id"]) is None
        assert [e for e in events.read("r-rev2")
                if e.type in ("profile/claim_invalidated", "profile/update_committed")] == []
        # 修正后重试：正常提交且只落一次
        committed = cons.commit_update(
            "stock", "BE", change_set_id=out["change_set_id"],
            expected_base_hash=out["expected_base_hash"], note="修正后提交", manifest=RUN,
            invalidate_claims=[{"claim_id": "claim-ok", "reason": "第一条合法"}],
        )
        assert committed["committed"] is True
        assert len(metrics.invalidations_as_of("stock", "BE", datetime.now(UTC))) == 1


# ---------------- R7 失效沿完整依赖闭包传播 ----------------


class TestR7DependencyClosure:
    def test_observation_calculation_claim_chain(self, tmp_path):
        """review 复现：observation → calculation → claim，预览只看到计算，
        漏掉下游论断。现在闭包遍历找出全部下游，且限制主体与时间范围。"""
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        metrics.save_calculation(
            calculation_id="calc-chain", namespace="prod", entity_kind="stock",
            entity_id="BE", formula_id="backlog_coverage", formula_version=1,
            status="ok", result="2.5", unit="ratio", input_hash="h-chain",
            created_at=T0, run_id="r-rev2",
            payload={"calculation_id": "calc-chain", "formula_id": "backlog_coverage",
                     "formula_version": 1, "status": "ok", "result": "2.5",
                     "unit": "ratio", "created_at": T0.isoformat(),
                     "input_refs": [{"kind": "observation", "label": "backlog",
                                     "ref_id": obs_id, "value": "300000000"}]},
        )
        save_claim(metrics, "claim-via-calc", statement="订单覆盖倍数 2.5 支撑扩产",
                   support=["calc-chain"], created=T1, recorded_at=T1)
        save_claim(metrics, "claim-direct", statement="在手订单 300 million",
                   support=[obs_id], created=T1, recorded_at=T1)
        save_claim(metrics, "claim-late", statement="截止后才登记的论断",
                   support=["calc-chain"], created=T3, recorded_at=T3)
        # 跨实体 payload 里恰好含同 id 字符串（不应被闭包捞到）
        save_claim(metrics, "claim-other-entity", statement="别家的论断",
                   support=[obs_id], entity_id="NVDA", created=T1, recorded_at=T1)
        # 失效记录生效时刻固定在 T2（可控时点，区别于真实写入时间）
        obs_payload = metrics.get_observation(obs_id).model_dump(mode="json")
        metrics.save_revision(
            observation_id=obs_id, namespace="prod", action="invalidated",
            reason="口径错误", entity_kind="stock", entity_id="BE",
            payload=obs_payload, revised_at=T2, run_id=RUN.run_id,
        )

        cons = ProfileConsolidator(kb=kb, metrics=metrics, events=events)
        out = cons.prepare_update("stock", "BE", as_of=T2)
        stale = {d["ref"]: d for d in out["invalidated_dependencies"]["stale_dependents"]}
        assert "calc-chain" in stale, "直接下游计算必须列出"
        assert "claim-via-calc" in stale, "review R7：经计算的下游论断不得再漏掉"
        assert stale["claim-via-calc"]["via_observation"] == obs_id
        assert stale["claim-via-calc"]["via_ref"] == "calc-chain"
        assert "claim-direct" in stale
        assert "claim-other-entity" not in stale, "依赖闭包不得跨主体"
        assert "claim-late" not in stale, "截止后才登记的下游不进本次预览（时态纪律）"


# ---------------- R8 间接引用展开到原始证据 ----------------


class TestR8LineageExpansion:
    def test_claim_of_claim_expands_to_original_evidence(self, tmp_path):
        """review 复现：论断引用另一论断时，核验员只看到该论断的陈述。
        现在：血缘递归展开——核验看到底层观测与原文，且带 via 血缘标记。"""
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-inner", statement="在手订单 300 million 创纪录",
                   support=[obs_id], created=T1, recorded_at=T1)
        save_claim(metrics, "claim-outer", statement="订单强劲支撑明年收入",
                   support=["claim-inner"], created=T1, recorded_at=T1)
        pack = build_evidence_pack(
            kb, metrics, entity_kind="stock", entity_id="BE",
            claim_payload=metrics.get_claim("claim-outer"), as_of=T2,
        )
        refs = {s.ref: s for s in pack.supporting_spans}
        assert "claim-inner" in refs, "被引论断本身仍在包内（作为分析材料）"
        assert obs_id in refs, "被引论断的支持观测必须展开进包"
        assert "ev-backlog" in refs, "原始证据原文必须展开进包（不能只看论断陈述）"
        assert refs["ev-backlog"].meta.get("indirect") is True
        assert refs["ev-backlog"].meta.get("via")
        assert "300 million" in refs["ev-backlog"].text
        assert obs_id in pack.observation_refs

    def test_cycle_and_depth_are_bounded(self, tmp_path):
        """claim 互引不死循环；展开深度有上限。"""
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-a", statement="A 引 B", support=["claim-b"],
                   created=T1, recorded_at=T1)
        save_claim(metrics, "claim-b", statement="B 引 A", support=["claim-a"],
                   created=T1, recorded_at=T1)
        pack = build_evidence_pack(
            kb, metrics, entity_kind="stock", entity_id="BE",
            claim_payload=metrics.get_claim("claim-a"), as_of=T2,
        )
        refs = [s.ref for s in pack.supporting_spans]
        assert refs.count("claim-b") == 1 and refs.count("claim-a") == 1
        assert len(refs) <= 8, "循环链路必须被截断"


# ---------------- R9 核验 verdict 枚举约束 ----------------


class TestR9VerdictEnum:
    def test_unknown_verdict_not_treated_as_supported(self, tmp_path):
        """review 复现：离线输入 NOT_SUPPORTED 实际得到「支持」。
        现在：非法 verdict 丢弃 → 全部非法时按核验不可用诚实降级。"""
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-v9", statement="在手订单 300 million",
                   support=[obs_id])
        llm = review_llm([{"text": "在手订单 300 million", "verdict": "NOT_SUPPORTED",
                           "supporting_refs": [obs_id]}])
        result = verify_claim(kb, metrics, claim_id="claim-v9", llm=llm,
                              events=events, manifest=RUN, now=T2,
                              entity_kind="stock", entity_id="BE")
        assert result.content_review_available is False, \
            "全部 verdict 非法 = 内容核验不可用（诚实降级，不冒充已核验）"
        assert result.evidence_support == "unchecked"
        assert result.evidence_support != "supported"
        assert any("verdict 非法" in n for n in
                   metrics.get_claim("claim-v9")["verification"]["notes"])

    def test_mixed_verdicts_keep_valid_drop_invalid(self, tmp_path):
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-v9b", statement="在手订单 300 million",
                   support=[obs_id])
        llm = review_llm([
            {"text": "在手订单 300 million", "verdict": "Supported",  # 大小写宽容
             "supporting_refs": [obs_id]},
            {"text": "另一条", "verdict": "NOT_SUPPORTED", "supporting_refs": []},
        ])
        result = verify_claim(kb, metrics, claim_id="claim-v9b", llm=llm,
                              events=events, manifest=RUN, now=T2,
                              entity_kind="stock", entity_id="BE")
        assert result.content_review_available is True
        assert [a.verdict for a in result.atomic] == ["supported"]
        assert any("verdict 非法" in n for n in
                   metrics.get_claim("claim-v9b")["verification"]["notes"])


# ---------------- R10 原件哈希识别 PDF 版本 ----------------


def _make_pdf(page_texts: list[str]) -> bytes:
    """最小合法 PDF（与 test_document_read_v2 同款构造器，保持本文件自包含）。"""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    n = len(page_texts)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(page_texts):
        page_no = 4 + 2 * i
        safe = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode()
        add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            f" /Resources << /Font << /F1 3 0 R >> >> /Contents {page_no + 1} 0 R >>".encode())
        add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
            + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for idx, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n"
            f"%%EOF").encode()
    return bytes(out)


class TestR10RawHashIdentity:
    def test_same_parsed_prefix_different_originals_are_two_documents(self, tmp_path):
        """review 复现：两份 PDF 已解析部分相同、后续内容不同，第二份复用了
        第一份的原 bytes 与页数。现在：版本身份 = 原件哈希，必须是两份文档。"""
        pdf_a = _make_pdf(["Same first page text", "alpha tail content"])
        pdf_b = _make_pdf(["Same first page text", "beta different tail"])
        ds = DocumentStore()
        fetched_a = FetchedDocument(
            kind="pdf", url="https://x.example/a.pdf",
            page_texts=((1, "Same first page text"),), total_pages=2,
            quality=TextQuality("ok"), raw=pdf_a,
        )
        fetched_b = FetchedDocument(
            kind="pdf", url="https://x.example/b.pdf",
            page_texts=((1, "Same first page text"),), total_pages=2,
            quality=TextQuality("ok"), raw=pdf_b,
        )
        da = ds.add(url="https://x.example/a.pdf", source_id="web_fetch",
                    fetched=fetched_a, available_at=None, pit_grade=PitGrade.C)
        db = ds.add(url="https://x.example/b.pdf", source_id="web_fetch",
                    fetched=fetched_b, available_at=None, pit_grade=PitGrade.C)
        assert da.document_id != db.document_id, \
            "解析前缀相同 ≠ 同版本：原件不同的 PDF 不得复用同一份 bytes"
        assert db.raw is not None and db.raw == pdf_b
        assert da.raw_hash and db.raw_hash and da.raw_hash != db.raw_hash
        # 惰性续解读到的是各自原件的后续页
        got = ds.ensure_pages(db, [2])
        assert got == [2] and "beta different tail" in db.page_texts[2]
        assert "alpha" not in db.page_texts[2]

    def test_identical_original_reuses_and_merges_pages(self, tmp_path):
        """同一原件（同哈希）再次登记：复用同一份文档，并并入新解析的页。"""
        pdf = _make_pdf(["page one", "page two", "page three"])
        ds = DocumentStore()
        first = ds.add(url="u", source_id="edgar",
                       fetched=FetchedDocument(
                           kind="pdf", url="u", page_texts=((1, "page one"),),
                           total_pages=3, quality=TextQuality("ok"), raw=pdf),
                       available_at=T0, pit_grade=PitGrade.A)
        second = ds.add(url="u2", source_id="edgar_copy",
                        fetched=FetchedDocument(
                            kind="pdf", url="u2",
                            page_texts=((1, "page one"), (2, "page two")),
                            total_pages=3, quality=TextQuality("ok"), raw=pdf),
                        available_at=T0, pit_grade=PitGrade.A)
        # 同原件不同来源上下文：身份分开（PIT 纪律），但解析结果共享
        assert second.page_texts[2] == "page two"
        again = ds.add(url="u", source_id="edgar",
                       fetched=FetchedDocument(
                           kind="pdf", url="u", page_texts=((1, "page one"),),
                           total_pages=3, quality=TextQuality("ok"), raw=pdf),
                       available_at=T0, pit_grade=PitGrade.A)
        assert again.document_id == first.document_id
        assert first.page_texts.get(2) == "page two", "同原件的新解析页并入已有版本"
        assert ds.duplicates >= 1


# ---------------- R11 完整性按实际页集合 ----------------


class TestR11CompletenessByActualPageSet:
    def test_middle_gap_not_hidden_by_max_page(self):
        """review 复现：3 页文档只解析第 1、3 页 → 旧口径 full/未解析 0。
        现在：缺页显式可见（truncated + missing_pages）。"""
        ds = DocumentStore()
        doc = ds.add(url="u", source_id="edgar",
                     fetched=FetchedDocument(
                         kind="pdf", url="u",
                         page_texts=((1, "intro"), (3, "tail")),
                         total_pages=3, quality=TextQuality("ok")),
                     available_at=T0, pit_grade=PitGrade.A)
        assert doc.completeness == "truncated"
        payload = doc.completeness_payload()
        assert payload["unparsed_pages"] == 1
        assert payload["missing_pages"] == [2]

    def test_failed_pages_stay_partial_with_gap_listed(self):
        ds = DocumentStore()
        doc = ds.add(url="u", source_id="edgar",
                     fetched=FetchedDocument(
                         kind="pdf", url="u",
                         page_texts=((1, "intro"), (3, "tail")),
                         failed_pages=(2,), total_pages=3,
                         quality=TextQuality("ok")),
                     available_at=T0, pit_grade=PitGrade.A)
        assert doc.completeness == "partial"
        payload = doc.completeness_payload()
        assert payload["failed_pages"] == [2]
        assert payload["unparsed_pages"] == 0  # 第 2 页是失败而非未解析

    def test_full_set_is_full(self):
        ds = DocumentStore()
        doc = ds.add(url="u", source_id="edgar",
                     fetched=FetchedDocument(
                         kind="pdf", url="u",
                         page_texts=((1, "a"), (2, "b"), (3, "c")),
                         total_pages=3, quality=TextQuality("ok")),
                     available_at=T0, pit_grade=PitGrade.A)
        assert doc.completeness == "full"
        assert doc.completeness_payload()["unparsed_pages"] == 0


# ---------------- 连带修复：实体删除不漏新表 ----------------


class TestEntityPurgeCoversNewTables:
    def test_delete_entity_removes_claim_versions_invalidations_commits(self, tmp_path):
        """delete_entity 承诺「不静默漏表」：research_claim_versions /
        claim_invalidations / profile_update_commits 必须随实体一并删除。"""
        kb, metrics, events, writer, mw, obs_id = make_env(tmp_path)
        save_claim(metrics, "claim-purge", statement="待删论断", support=[obs_id],
                   recorded_at=T1)
        metrics.save_claim_invalidation(
            invalidation_id="inval-purge", namespace="prod", entity_kind="stock",
            entity_id="BE", claim_id="claim-purge", reason="x", source_refs=[],
            invalidated_at=T2,
        )
        metrics.record_profile_update(
            commit={"change_set_id": "chg-purge", "namespace": "prod",
                    "entity_kind": "stock", "entity_id": "BE",
                    "expected_base_hash": "h", "committed_at": T2.isoformat(),
                    "run_id": None, "payload": {"change_set_id": "chg-purge"}},
            invalidations=[],
        )
        counts = metrics.delete_entity("stock", "BE")
        for table in ("research_claims", "research_claim_versions",
                      "claim_invalidations", "profile_update_commits"):
            assert counts.get(table, 0) >= 1, f"{table} 必须计入删除"
            remaining = metrics._conn.execute(  # noqa: SLF001 - 测试直接验证行数
                f"SELECT COUNT(*) FROM {table} WHERE entity_kind = 'stock'"
                " AND entity_id = 'BE'").fetchone()[0]
            assert remaining == 0, f"{table} 不得残留"


# ---------------- R12 官方来源按真实 hostname 识别 ----------------


class TestR12OfficialSourceByHostname:
    def test_query_param_mention_is_not_issuer_disclosure(self):
        """review 复现：查询参数里含 sec.gov 的普通网页被归发行人披露。"""
        assert source_role("web_fetch", "https://news.example.com/a?src=sec.gov") == "unknown"
        assert source_role("web_fetch", "https://evil.com/path/sec.gov/x") == "unknown"
        assert source_role("web_fetch", "https://sec.gov.evil.com/x") == "unknown"
        assert source_role("web_fetch", "https://sec.gov@evil.com/x") == "unknown"

    def test_real_official_hosts_and_subdomains(self):
        assert source_role("web_fetch", "https://www.sec.gov/Archives/edgar/x.htm") \
            == "issuer_filing"
        assert source_role("web_fetch", "https://sec.gov/cgi-bin/browse-edgar?x=1") \
            == "issuer_filing"
        assert source_role("web_fetch", "sec.gov/Archives/x.htm") == "issuer_filing"
        assert source_role("web_fetch",
                           "https://www1.hkexnews.hk/listedco/listconews/sehk/2024/x.pdf") \
            == "issuer_filing"

    def test_media_source_not_upgraded_by_official_url(self):
        """媒体源转发官方原文不升档（只有 unknown 档可按域名细化）。"""
        assert source_role("web_search", "https://sec.gov/x") == "media_secondary"

    def test_garbage_url_safe_unknown(self):
        assert source_role("web_fetch", "not a url at all :://") == "unknown"
        assert source_role("web_fetch", "") == "unknown"
