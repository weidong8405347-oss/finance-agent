"""Dossier 投影与服务验收（设计 §4/§6.6/§10.1/§13.1「历史与评估」「发布恢复」组）。

关键场景：legacy 兼容投影（不猜数）、as_of 历史一致性（unavailable_at_as_of、
不借用未来产物）、data_hash 幂等发布、changed_modules、模块降级原因、
JSON/Markdown 冻结导出同源、发布失败三通道可见。
"""

import contextlib
import json
import logging
from datetime import UTC, date, datetime

import pytest

from finance_agent.dossier.projector import DossierProjector
from finance_agent.dossier.service import DossierError, DossierService
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.normalization import normalize_raw
from finance_agent.knowledge.store import BitemporalStore

T0 = datetime(2024, 6, 1, tzinfo=UTC)
T1 = datetime(2025, 1, 31, tzinfo=UTC)
T2 = datetime(2025, 6, 1, tzinfo=UTC)
FY2024 = MetricPeriod(start=date(2024, 1, 1), end=date(2024, 12, 31),
                      frequency="FY", fiscal_label="FY2024")


@pytest.fixture()
def env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    projector = DossierProjector(kb=kb, metrics=metrics)
    service = DossierService(kb=kb, metrics=metrics, projector=projector, events=events)
    return kb, metrics, events, projector, service


def seed_legacy(kb):
    kb.add_evidence(Evidence(
        evidence_id="ev-l1", source_id="edgar", url="https://sec.gov/a",
        verbatim_quote="sells fuel cell systems and services",
        retrieved_at=T0, available_at=T0, pit_grade=PitGrade.A,
    ))
    kb.assert_fact(Fact(
        entity_kind="stock", entity_id="BE", field="business_model",
        value="sells fuel cell systems and services", knowledge_time=T0,
        evidence_ids=["ev-l1"],
    ))
    kb.add_evidence(Evidence(
        evidence_id="ev-l2", source_id="edgar", url="https://sec.gov/b",
        verbatim_quote="Revenue for 2023 was 1.2 billion approximately",
        retrieved_at=T0, available_at=T0, pit_grade=PitGrade.A,
    ))
    kb.assert_fact(Fact(
        entity_kind="stock", entity_id="BE", field="revenue_fy",
        value="2023 年收入约 1.2 billion（口径未标注）", knowledge_time=T0,
        evidence_ids=["ev-l2"],
    ))
    kb.assert_fact(Fact(
        entity_kind="stock", entity_id="BE", field="thesis",
        value="旧投资论点：订单驱动增长", knowledge_time=T0, evidence_ids=["ev-l1"],
    ))


def seed_typed(kb, metrics, *, knowledge_time=T1, value_text="1500 million",
               metric_key="revenue", namespace="prod", run_id="r1"):
    from finance_agent.harness.manifest import RunManifest, RunMode
    from finance_agent.knowledge.metric_writer import TypedMetricWriter

    ev_id = f"ev-t-{knowledge_time.year}-{metric_key}-{namespace}"
    with contextlib.suppress(Exception):  # 重复 seed 幂等
        kb.add_evidence(Evidence(
            evidence_id=ev_id, source_id="edgar", url="https://sec.gov/c",
            verbatim_quote=f"Total revenue {value_text}",
            retrieved_at=knowledge_time, available_at=knowledge_time, pit_grade=PitGrade.A,
        ))
    w = TypedMetricWriter(store=metrics, kb=kb, events=None)
    value, steps = normalize_raw(value_text, "USD")
    obs = ReportedObservation(
        entity_kind="stock", entity_id="BE", metric_key=metric_key, period=FY2024,
        value=value, unit="USD", currency="USD",
        raw=RawValue(value_text=value_text, unit_text="USD"),
        normalization=[s.model_dump(mode="json") for s in steps],
        evidence_refs=[ev_id], knowledge_time=knowledge_time,
        source_available_at=knowledge_time, retrieved_at=knowledge_time,
        created_at=knowledge_time, pit_grade=PitGrade.A,
    )
    return w.write_observation(
        obs, run=RunManifest(run_id=run_id, mode=RunMode.LIVE), namespace=namespace
    )[0]


def seed_claim(metrics, *, created_at=T1, status="validated", claim_id="claim-1",
               statement="收入增长由数据中心订单驱动"):
    from finance_agent.research.artifacts import ResearchClaim

    claim = ResearchClaim(
        claim_id=claim_id, entity_kind="stock", entity_id="BE", statement=statement,
        kind="inference", support_refs=["ev-l1"], status=status,
        created_at=created_at, evidence_cutoff=created_at,
    )
    metrics.save_claim(claim_id=claim_id, namespace="prod",
                       payload=claim.model_dump(mode="json"))
    return claim_id


class TestLegacyProjection:
    def test_legacy_entity_readable_without_typed_data(self, env):
        """一票从旧 KB 到新页面可读（M1 完成条件）：无 typed 观测也有档案。"""
        kb, metrics, events, projector, service = env
        seed_legacy(kb)
        snap, created = service.open("stock", "BE")
        assert created
        assert snap["entity"]["id"] == "BE"
        mods = snap["modules"]
        # 旧文本字段 → 模块 partial（legacy），不伪装 ready
        assert mods["business_engine"]["status"] == "partial"
        assert any("旧字段" in r for r in mods["business_engine"]["reasons"])
        assert mods["financial_quality"]["status"] == "partial"
        # 无 typed 数据模块 → missing 且给原因（不以空图宣称完成）
        assert mods["expectations"]["status"] == "missing"
        assert mods["expectations"]["reasons"]
        # 旧 thesis → legacy_analysis（不是 reported fact）
        assert snap["summary"]["thesis_kind"] == "legacy_analysis"
        assert "旧投资论点" in snap["summary"]["thesis"]
        # 指标栏全部缺口（不从文本猜数）
        assert all(m["status"] == "missing" for m in snap["summary"]["key_metrics"])
        # 配方识别：fuel cell 提示 → 工业设备模板（识别有来源、可更改）
        assert snap["recipe"]["id"] == "industrial_equipment"

    def test_legacy_numeric_flagged_needs_normalization(self, env):
        kb, metrics, events, projector, service = env
        seed_legacy(kb)
        snap, _ = service.open("stock", "BE")
        payload = service.module(snap["context"]["snapshot_id"], "research_sources")
        legacy = {f["field"]: f for f in payload.payload["legacy_facts"]}
        assert legacy["revenue_fy"]["needs_normalization"] is True  # 文本含数字口径不明
        assert legacy["business_model"]["needs_normalization"] is False


class TestTypedProjection:
    def test_typed_observation_lights_up_modules(self, env):
        kb, metrics, events, projector, service = env
        seed_legacy(kb)
        seed_typed(kb, metrics)
        seed_claim(metrics)
        snap, _ = service.open("stock", "BE")
        km = {m["metric_key"]: m for m in snap["summary"]["key_metrics"]}
        assert km["revenue"]["value"] == "1500000000"  # 图表/指标只消费 typed 观测
        assert km["revenue"]["observation_id"]
        assert snap["summary"]["thesis_kind"] == "claim"  # validated claim 优先于 legacy thesis
        assert snap["research"]["answered"] == 0  # 无计划时覆盖为 0（不虚构覆盖）

    def test_module_series_payload(self, env):
        kb, metrics, events, projector, service = env
        seed_typed(kb, metrics)
        snap, _ = service.open("stock", "BE")
        payload = service.module(snap["context"]["snapshot_id"], "financial_quality")
        series = {s["metric_key"]: s for s in payload.payload["fy"]["series"]}
        assert "revenue" in series
        pt = series["revenue"]["points"][0]
        assert pt["value"] == "1500000000" and pt["nature"] == "reported"
        assert pt["observation_id"]  # 每个点可回指（来源抽屉一次点击）


class TestHistoricalConsistency:
    def test_as_of_excludes_future_data_and_marks_unavailable(self, env):
        """历史视图：T0 时点看不到 T1 的观测/论断；模块标 unavailable_at_as_of。"""
        kb, metrics, events, projector, service = env
        seed_legacy(kb)  # T0 有旧字段
        seed_typed(kb, metrics, knowledge_time=T1)
        seed_claim(metrics, created_at=T1)
        snap_now, _ = service.open("stock", "BE")
        assert snap_now["summary"]["thesis_kind"] == "claim"
        snap_hist, _ = service.open("stock", "BE", as_of=T0, mode="historical")
        # T0：claim（T1 创建）不可见 → 回到 legacy thesis
        assert snap_hist["summary"]["thesis_kind"] == "legacy_analysis"
        assert "claim-1" not in snap_hist["evidence_refs"]
        km = {m["metric_key"]: m for m in snap_hist["summary"]["key_metrics"]}
        assert km["revenue"]["status"] == "missing"  # T1 观测在 T0 不可知
        assert snap_hist["context"]["mode"] == "historical"
        assert any("历史视图" in x for x in snap_hist["limitations"])

    def test_historical_requires_past_as_of(self, env):
        kb, metrics, events, projector, service = env
        with pytest.raises(ValueError):
            service.open("stock", "BE", mode="historical")  # 缺 as_of
        with pytest.raises(ValueError):
            service.open(
                "stock", "BE", mode="historical",
                as_of=datetime(2099, 1, 1, tzinfo=UTC),  # 未来时刻
            )

    def test_eval_namespace_isolated(self, env):
        """eval 命名空间观测不泄漏进 prod 投影（反向隔离）。"""
        kb, metrics, events, projector, service = env
        seed_typed(kb, metrics, namespace="eval:run-9", metric_key="capex")
        snap, _ = service.open("stock", "BE", namespace="prod")
        payload = service.module(snap["context"]["snapshot_id"], "financial_quality")
        keys = {s["metric_key"] for s in payload.payload["fy"]["series"]}
        assert "capex" not in keys


class TestSnapshotLifecycle:
    def test_idempotent_publish_single_event(self, env):
        kb, metrics, events, projector, service = env
        seed_legacy(kb)
        snap1, c1 = service.open("stock", "BE", run_id="sess-1")
        snap2, c2 = service.open("stock", "BE", run_id="sess-1")
        assert c1 and not c2
        assert snap1["data_hash"] == snap2["data_hash"]
        assert snap1["context"]["snapshot_id"] == snap2["context"]["snapshot_id"]
        published = [e for e in events.read("sess-1") if e.type == "dossier/published"]
        assert len(published) == 1  # 幂等发布：不重复落事件

    def test_changed_modules_on_new_data(self, env):
        kb, metrics, events, projector, service = env
        seed_legacy(kb)
        snap1, _ = service.open("stock", "BE", run_id="sess-2")
        seed_typed(kb, metrics)
        snap2, created = service.open("stock", "BE", run_id="sess-2")
        assert created and snap2["data_hash"] != snap1["data_hash"]
        published = [e for e in events.read("sess-2") if e.type == "dossier/published"]
        assert len(published) == 2
        assert "key_kpi" in published[1].payload["changed_modules"] or \
               "financial_quality" in published[1].payload["changed_modules"]
        # changes diff：两个快照之间指标变化可见
        diff = service.changes(snap2["context"]["snapshot_id"], snap1["context"]["snapshot_id"])
        assert diff["changed_modules"]
        assert any(c["metric_key"] == "revenue" for c in diff["key_metric_changes"])

    def test_publish_failure_three_channels(self, env, caplog):
        """发布失败：dossier/publish_failed 事件 + 日志 + 异常上浮（工程约定 1）。"""
        kb, metrics, events, projector, service = env
        with (
            caplog.at_level(logging.ERROR, logger="finance_agent.dossier"),
            pytest.raises(ValueError),
        ):
            service.open("stock", "BE", mode="historical", run_id="sess-3")
        failed = [e for e in events.read("sess-3") if e.type == "dossier/publish_failed"]
        assert failed and "ValueError" in failed[0].payload["reason"]
        assert "dossier 发布失败" in caplog.text

    def test_snapshot_restore_and_context_mismatch(self, env):
        kb, metrics, events, projector, service = env
        seed_legacy(kb)
        snap, _ = service.open("stock", "BE")
        sid = snap["context"]["snapshot_id"]
        restored = service.get(sid)  # 深链/刷新恢复
        assert restored["data_hash"] == snap["data_hash"]
        with pytest.raises(DossierError):
            service.get("dossier-nope-000000000000")


class TestExport:
    def test_json_export_binds_hash_and_context(self, env):
        kb, metrics, events, projector, service = env
        seed_legacy(kb)
        seed_typed(kb, metrics)
        snap, _ = service.open("stock", "BE")
        sid = snap["context"]["snapshot_id"]
        name, content = service.export(sid, "json")
        data = json.loads(content)
        assert data["data_hash"] == snap["data_hash"]
        assert data["context"]["as_of"] == snap["context"]["as_of"]
        assert name.endswith(".json")

    def test_markdown_export_same_source(self, env):
        kb, metrics, events, projector, service = env
        seed_legacy(kb)
        seed_typed(kb, metrics)
        seed_claim(metrics)
        snap, _ = service.open("stock", "BE")
        sid = snap["context"]["snapshot_id"]
        name, md = service.export(sid, "markdown")
        assert name.endswith(".md")
        assert "1500000000" in md  # 数值来自 typed 观测（与页面同源）
        assert "收入增长由数据中心订单驱动" in md  # validated claim 明细
        assert snap["data_hash"] in md  # 绑定 hash
        assert "知识截止" in md and snap["context"]["as_of"] in md

    def test_unknown_format_rejected(self, env):
        kb, metrics, events, projector, service = env
        seed_legacy(kb)
        snap, _ = service.open("stock", "BE")
        with pytest.raises(DossierError):
            service.export(snap["context"]["snapshot_id"], "pdf")
