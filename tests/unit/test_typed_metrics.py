"""typed 观测契约验收（knowledge-dossier-research-redesign §6/§13.1）。

测试组：数值语义 / 财务期间 / 来源 / 历史与评估 / 发布恢复（存储层）。
失败路径同时断言：事件落库 + 日志输出 + 异常上浮（工程约定 1 三通道）。
"""

import logging
from datetime import UTC, date, datetime

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.errors import KnowledgeInvariantError, KnowledgeLeakError
from finance_agent.knowledge.metric_store import ConflictResolution, MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.metrics import (
    CalculatedObservation,
    ConsensusObservation,
    GuidanceObservation,
    MetricPeriod,
    ModelEstimateObservation,
    RawValue,
    ReportedObservation,
    observation_from_dict,
)
from finance_agent.knowledge.models import Evidence, PitGrade
from finance_agent.knowledge.normalization import (
    NormalizationError,
    NormalizationStep,
    assert_typed_leaves,
    normalize_raw,
    parse_raw_number,
    recompute_lineage,
)
from finance_agent.knowledge.store import BitemporalStore

T0 = datetime(2024, 1, 31, tzinfo=UTC)  # 财报发布/可知时刻
T1 = datetime(2024, 6, 1, tzinfo=UTC)
T2 = datetime(2025, 1, 31, tzinfo=UTC)
FY2023 = MetricPeriod(start=date(2023, 1, 1), end=date(2023, 12, 31), frequency="FY", fiscal_label="FY2023")


def make_stores(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "metrics.db")
    events = EventStore(tmp_path / "e.db")
    writer = TypedMetricWriter(store=metrics, kb=kb, events=events)
    return kb, metrics, events, writer


def seed_evidence(kb, evidence_id="ev-1", quote="Total revenue was 1.2 billion in fiscal 2023",
                  available_at=T0, grade=PitGrade.A):
    kb.add_evidence(Evidence(
        evidence_id=evidence_id, source_id="edgar", url="https://sec.gov/filing/1",
        verbatim_quote=quote, retrieved_at=T1, available_at=available_at, pit_grade=grade,
    ))


def reported(value="1200000000", raw_text="1.2 billion", key="revenue", period=FY2023,
             evidence=("ev-1",), knowledge_time=T0, **kw):
    steps = []
    if raw_text and value:
        _v, steps = normalize_raw(raw_text, kw.pop("unit_text", "USD"))
    return ReportedObservation(
        entity_kind="stock", entity_id="BE", metric_key=key, period=period,
        basis=kw.pop("basis", "GAAP"), value=value, unit=kw.pop("unit", "USD"),
        currency=kw.pop("currency", "USD"),
        raw=RawValue(value_text=raw_text, unit_text="USD") if raw_text else None,
        normalization=[s.model_dump(mode="json") for s in steps],
        evidence_refs=list(evidence), knowledge_time=knowledge_time,
        source_available_at=T0, retrieved_at=T1, created_at=T1,
        pit_grade=PitGrade.A, **kw,
    )


# ---------------- 数值语义（§13.1 组 1） ----------------


class TestNumericSemantics:
    def test_year_not_taken_as_value(self):
        # 「2026 revenue 1.2 billion」不取 2026
        assert parse_raw_number("2026 revenue 1.2 billion") == parse_raw_number("1.2 billion")
        value, steps = normalize_raw("2026 revenue 1.2 billion", "USD")
        assert value == "1200000000"
        assert steps[0].formula_id == "unit_word_scale" and steps[0].params["word"] == "billion"

    def test_scale_and_currency_consistency(self):
        value, steps = normalize_raw("1,200", "USD millions")
        assert value == "1200000000"
        assert steps[0].params["word"] == "million"

    def test_chinese_scale_word(self):
        value, _ = normalize_raw("收入 100亿 元", "CNY")
        assert value == "10000000000"  # 亿 = 10^8

    def test_lineage_recompute_mismatch_rejected(self, tmp_path, caplog):
        kb, metrics, events, writer = make_stores(tmp_path)
        seed_evidence(kb)
        # 换算链与登记值不一致（原文 1.2 billion，却登记 1.3e9）→ fail-loud
        obs = reported(value="1300000000")
        with (
            caplog.at_level(logging.WARNING, logger="finance_agent.knowledge.metrics"),
            pytest.raises(NormalizationError),
        ):
            writer.write_observation(obs, run=RunManifest(run_id="r1", mode=RunMode.LIVE))
        # 三通道：事件（hook/verdict）+ 日志 + 异常
        verdicts = [e for e in events.read("r1") if e.type == "hook/verdict"]
        assert verdicts and verdicts[0].payload["hook"] == "typed-metric-gate"
        assert "typed 观测拒写" in caplog.text
        assert metrics.observations_as_of("stock", "BE", T2) == []

    def test_nested_numbers_rejected_in_assumptions(self):
        with pytest.raises(NormalizationError):
            assert_typed_leaves({"wacc": 0.10})  # 裸 float 拒绝
        with pytest.raises(NormalizationError):
            assert_typed_leaves({"path": {"growth": [1, 2]}})  # 嵌套裸数值拒绝
        assert_typed_leaves({"wacc": "0.10", "years": "10"})  # 十进制字符串放行

    def test_missing_value_not_zero(self):
        obs = ReportedObservation(
            entity_kind="stock", entity_id="BE", metric_key="backlog", period=FY2023,
            value=None, status="missing", evidence_refs=[], knowledge_time=T0,
            retrieved_at=T1, created_at=T1,
        )
        assert obs.value is None  # 缺失不是 0
        with pytest.raises(ValueError):
            ReportedObservation(
                entity_kind="stock", entity_id="BE", metric_key="backlog", period=FY2023,
                value="5", status="ok", evidence_refs=[], knowledge_time=T0,
                retrieved_at=T1, created_at=T1,
            )  # ok 状态无证据 → schema 拒绝

    def test_nature_source_obligations(self):
        # calculated 必须引用 CalculationRun；model_estimate 必须引用 artifact；
        # guidance 必须给发布者与目标期间；consensus 必须给供应商与快照时点
        with pytest.raises(ValueError):
            CalculatedObservation(
                entity_kind="stock", entity_id="BE", metric_key="yoy", period=FY2023,
                value="0.2", status="ok", knowledge_time=T0, retrieved_at=T1, created_at=T1,
            )
        with pytest.raises(ValueError):
            ModelEstimateObservation(
                entity_kind="stock", entity_id="BE", metric_key="implied_g", period=FY2023,
                value="0.15", status="ok", assumptions={"wacc": "0.1"},
                knowledge_time=T0, retrieved_at=T1, created_at=T1,  # 无 artifact_ref
            )
        with pytest.raises(ValueError):
            GuidanceObservation(  # type: ignore[call-arg]
                entity_kind="stock", entity_id="BE", metric_key="revenue", period=FY2023,
                value="2000", status="ok", issuer="公司", guidance_published_at=T0,
                target_period=FY2023, evidence_refs=[],  # guidance 无证据 → 拒
                knowledge_time=T0, retrieved_at=T1, created_at=T1,
            )
        with pytest.raises(ValueError):
            ConsensusObservation(  # type: ignore[call-arg]
                entity_kind="stock", entity_id="BE", metric_key="eps", period=FY2023,
                value="1.5", status="ok", vendor="",  # 供应商必填
                consensus_snapshot_at=T0, knowledge_time=T0, retrieved_at=T1, created_at=T1,
            )


# ---------------- 财务期间（§13.1 组 2 / §6.4） ----------------


class TestPeriods:
    def test_ttm_restricted_to_additive_flows(self):
        ttm = MetricPeriod(start=date(2023, 1, 1), end=date(2023, 12, 31), frequency="TTM")
        with pytest.raises(ValueError):
            reported(period=ttm, key="cash")  # 余额类禁 TTM
        obs = reported(period=ttm, key="revenue")  # 流量允许
        assert obs.period.frequency == "TTM"

    def test_instant_rejects_start(self):
        with pytest.raises(ValueError):
            MetricPeriod(start=date(2023, 1, 1), end=date(2023, 12, 31), frequency="instant")

    def test_semantic_key_separates_period_from_version_time(self, tmp_path):
        """季度演进 ≠ 同季重述：不同 period_end 是不同语义键（各自 v1，不冲突）。"""
        kb, metrics, events, writer = make_stores(tmp_path)
        seed_evidence(kb)
        q3 = MetricPeriod(start=date(2023, 7, 1), end=date(2023, 9, 30),
                          frequency="Q", fiscal_label="2023Q3")
        q4 = MetricPeriod(start=date(2023, 10, 1), end=date(2023, 12, 31),
                          frequency="Q", fiscal_label="2023Q4")
        seed_evidence(kb, "ev-2", "Q4 revenue 300 million", available_at=T2)
        seed_evidence(kb, "ev-q3", "Q3 revenue 250 million", available_at=T0)
        id_a, _ = writer.write_observation(
            reported(value="250000000", raw_text="Q3 revenue 250 million", period=q3,
                     knowledge_time=T0, evidence=("ev-q3",)),
            run=RunManifest(run_id="r1", mode=RunMode.LIVE))
        id_b, _ = writer.write_observation(
            reported(value="300000000", raw_text="Q4 revenue 300 million", period=q4,
                     knowledge_time=T2, evidence=("ev-2",)),
            run=RunManifest(run_id="r1", mode=RunMode.LIVE))
        assert id_a != id_b
        assert metrics.conflicted_semantic_hashes("stock", "BE") == []

    def test_restatement_same_period_creates_competing_version(self, tmp_path):
        """同期间重述：新版本 conflict_flag=1，旧版本保留（append-only）。"""
        kb, metrics, events, writer = make_stores(tmp_path)
        seed_evidence(kb)
        seed_evidence(kb, "ev-2", "Restated: total revenue 1.25 billion", available_at=T2)
        run = RunManifest(run_id="r1", mode=RunMode.LIVE)
        id1, created1 = writer.write_observation(reported(), run=run)
        assert created1
        id2, created2 = writer.write_observation(
            reported(value="1250000000", raw_text="1.25 billion", evidence=("ev-2",),
                     knowledge_time=T2),
            run=run)
        assert created2 and id2 != id1
        sems = metrics.conflicted_semantic_hashes("stock", "BE")
        assert len(sems) == 1
        history = metrics.observation_history(sems[0])
        assert [o.value for o in history] == ["1200000000", "1250000000"]  # 两版都保留
        # as_of 重述前 → 原值；重述后 → 新值（先按 as_of 过滤再选版本）
        before = metrics.observations_as_of("stock", "BE", datetime(2024, 7, 1, tzinfo=UTC))
        assert before[0].value == "1200000000"
        after = metrics.observations_as_of("stock", "BE", datetime(2025, 6, 1, tzinfo=UTC))
        assert after[0].value == "1250000000"

    def test_idempotent_same_semantic_same_value(self, tmp_path):
        kb, metrics, events, writer = make_stores(tmp_path)
        seed_evidence(kb)
        run = RunManifest(run_id="r1", mode=RunMode.LIVE)
        id1, c1 = writer.write_observation(reported(), run=run)
        id2, c2 = writer.write_observation(reported(), run=run)
        assert id1 == id2 and c1 is True and c2 is False  # 幂等：不产生重复版本
        asserted = [e for e in events.read("r1") if e.type == "metric/asserted"]
        assert len(asserted) == 2 and asserted[1].payload["created"] is False


# ---------------- 来源与时态（§13.1 组 3/4） ----------------


class TestSourcesAndTime:
    def test_unregistered_evidence_rejected(self, tmp_path):
        from finance_agent.knowledge.errors import MissingEvidenceError

        kb, metrics, events, writer = make_stores(tmp_path)
        with pytest.raises(MissingEvidenceError):
            writer.write_observation(reported(), run=RunManifest(run_id="r1", mode=RunMode.LIVE))

    def test_knowledge_time_invariant(self, tmp_path, caplog):
        kb, metrics, events, writer = make_stores(tmp_path)
        seed_evidence(kb, available_at=T1)
        obs = reported(knowledge_time=T0)  # 早于证据可知时刻
        with (
            caplog.at_level(logging.WARNING, logger="finance_agent.knowledge.metrics"),
            pytest.raises(KnowledgeInvariantError),
        ):
            writer.write_observation(obs, run=RunManifest(run_id="r1", mode=RunMode.LIVE))
        assert any(e.type == "hook/verdict" for e in events.read("r1"))
        assert caplog.text

    def test_eval_leak_blocked(self, tmp_path):
        """eval 防线：证据 available_at > eval_as_of → 拒写 + leakage/attempt 事件。"""
        kb, metrics, events, writer = make_stores(tmp_path)
        seed_evidence(kb, available_at=T1)
        run = RunManifest(run_id="eval-r", mode=RunMode.EVAL, eval_as_of=T0)
        obs = reported(knowledge_time=T1)
        with pytest.raises(KnowledgeLeakError):
            writer.write_observation(obs, run=run, namespace="eval:run-1")
        leaks = [e for e in events.read("eval-r") if e.type == "leakage/attempt"]
        assert leaks and leaks[0].payload["reason"] == "metric_evidence_after_as_of"

    def test_consensus_cannot_backdate(self, tmp_path):
        """consensus 缺历史快照不可回填：knowledge_time 早于快照时点 → 拒。"""
        kb, metrics, events, writer = make_stores(tmp_path)
        seed_evidence(kb)
        obs = ConsensusObservation(
            entity_kind="stock", entity_id="BE", metric_key="eps", period=FY2023,
            value="1.5", unit="USD", status="ok", vendor="vendor-x",
            consensus_snapshot_at=T1, knowledge_time=T0,  # 早于快照时点
            retrieved_at=T1, created_at=T1, pit_grade=PitGrade.B,
        )
        with pytest.raises(KnowledgeInvariantError):
            writer.write_observation(obs, run=RunManifest(run_id="r1", mode=RunMode.LIVE))

    def test_calculated_requires_registered_calculation(self, tmp_path):
        kb, metrics, events, writer = make_stores(tmp_path)
        obs = CalculatedObservation(
            entity_kind="stock", entity_id="BE", metric_key="yoy", period=FY2023,
            value="0.2", unit="ratio", status="ok", calculation_ref="calc-unknown",
            knowledge_time=T1, retrieved_at=T1, created_at=T1,
        )
        from finance_agent.knowledge.errors import MissingEvidenceError
        with pytest.raises(MissingEvidenceError):
            writer.write_observation(obs, run=RunManifest(run_id="r1", mode=RunMode.LIVE))


# ---------------- 裁决时态化（§2.2「历史冲突状态不冻结」整改） ----------------


class TestTemporalResolution:
    def test_resolution_not_visible_before_resolved_at(self, tmp_path):
        kb, metrics, events, writer = make_stores(tmp_path)
        seed_evidence(kb)
        seed_evidence(kb, "ev-2", "Restated: total revenue 1.25 billion", available_at=T2)
        run = RunManifest(run_id="r1", mode=RunMode.LIVE)
        id1, _ = writer.write_observation(reported(), run=run)
        id2, _ = writer.write_observation(
            reported(value="1250000000", raw_text="1.25 billion", evidence=("ev-2",),
                     knowledge_time=T2),
            run=run)
        sem = metrics.conflicted_semantic_hashes("stock", "BE")[0]
        resolved_at = datetime(2025, 3, 1, tzinfo=UTC)
        metrics.add_resolution(ConflictResolution(
            resolution_id="res-1", namespace="prod", entity_kind="stock", entity_id="BE",
            target_kind="observation", semantic_hash=sem, keep_observation_id=id1,
            resolved_at=resolved_at, note="以原披露为准（重述为口径调整）",
        ))
        # 裁决前：两版竞争，as_of 取最新可知（重述版）
        before = metrics.observations_as_of("stock", "BE", datetime(2025, 2, 1, tzinfo=UTC))
        assert before[0].observation_id == id2
        # 裁决后：以保留版本为当前值（历史不借用未来裁决，未来不覆盖历史）
        after = metrics.observations_as_of("stock", "BE", datetime(2025, 4, 1, tzinfo=UTC))
        assert after[0].observation_id == id1
        # 冲突行 flag 未被清空（append-only）：竞争历史仍可审计
        assert metrics.conflicted_semantic_hashes("stock", "BE") == [sem]


# ---------------- 序列化契约 ----------------


class TestSerialization:
    def test_roundtrip_and_union_discrimination(self):
        obs = reported()
        data = obs.model_dump(mode="json")
        restored = observation_from_dict(data)
        assert restored.nature == "reported" and restored.value == obs.value
        assert restored.semantic_hash() == obs.semantic_hash()
        data["nature"] = "calculated"  # 判别字段改变 → 目标类别义务生效
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            observation_from_dict(data)

    def test_lineage_of_fx_conversion(self):
        fx = NormalizationStep(formula_id="fx_convert", params={
            "rate": "7.8", "from_currency": "USD", "to_currency": "HKD",
            "rate_as_of": "2024-01-31", "rate_evidence_ref": "ev-fx",
        })
        value, steps = normalize_raw("100 million", "USD", extra_steps=[fx])
        assert value == "780000000"
        assert [s.formula_id for s in steps] == ["unit_word_scale", "fx_convert"]
        # 缺汇率来源 → fail-loud（跨币必须显式引用 FX 与换算日）
        with pytest.raises(NormalizationError):
            normalize_raw("100", "USD", extra_steps=[NormalizationStep(
                formula_id="fx_convert", params={"rate": "7.8"})])

    def test_recompute_lineage_helper(self):
        obs = reported()
        assert recompute_lineage(obs) == 1200000000
        broken = obs.model_copy(update={"value": "99"})
        with pytest.raises(NormalizationError):
            recompute_lineage(broken)
