"""What Changed 引擎验收（升级方案 §11/§32）。

关键场景：首次发布无变化日志（前端回退 key_changes）；结论/论断/指标/候选分层/
催化/模块状态六类变化的分类与图标；投资语义文案（不是研究过程日志）；
总数上限与类别优先级截断；历史投影不进入 live 基线序列；数字逐字搬移不改写。
"""

import contextlib
from datetime import UTC, date, datetime

import pytest

from finance_agent.dossier.changes import TOTAL_CAP, compute_change_log
from finance_agent.dossier.projector import DossierProjector
from finance_agent.dossier.service import DossierService
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.store import BitemporalStore

T0 = datetime(2024, 6, 1, tzinfo=UTC)
T1 = datetime(2025, 1, 31, tzinfo=UTC)
FY2024 = MetricPeriod(start=date(2024, 1, 1), end=date(2024, 12, 31),
                      frequency="FY", fiscal_label="FY2024")


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


def seed_typed(kb, metrics, *, knowledge_time=T1, value_text="1500 million",
               metric_key="revenue", namespace="prod", run_id="r1"):
    """写一条带原文锚点的 reported 观测（与 test_dossier.seed_typed 同形态）。"""
    from finance_agent.harness.manifest import RunManifest, RunMode
    from finance_agent.knowledge.metric_writer import TypedMetricWriter
    from finance_agent.knowledge.normalization import normalize_raw

    ev_id = f"ev-t-{knowledge_time.year}-{metric_key}-{namespace}-{value_text.split()[0]}"
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


def _snap(summary=None, claims=None, structures=None, modules=None):
    return {
        "summary": summary or {},
        "inputs": {"claims": claims or []},
        "structures": structures or {},
        "modules": modules or {},
    }


class TestComputeChangeLogPure:
    """纯函数：两个冻结 payload → 结构化变化条目。"""

    def test_first_publish_returns_empty(self):
        assert compute_change_log(None, _snap()) == []

    def test_no_change_returns_empty(self):
        s = _snap(summary={"thesis": "同一句结论"})
        assert compute_change_log(s, s) == []

    def test_thesis_text_update(self):
        base = _snap(summary={"thesis": "旧结论"})
        cur = _snap(summary={"thesis": "新结论：瓶颈迁移到临床验证"})
        log = compute_change_log(base, cur)
        assert len(log) == 1
        e = log[0]
        assert e["kind"] == "thesis" and e["icon"] == "up" and e["tag"] == "核心结论"
        assert "新结论" in e["text"]

    def test_stage_and_value_capture(self):
        base = _snap(summary={})
        cur = _snap(summary={"stage": "商业兑现早期", "value_capture": "平台层捕获 55-65%"})
        log = compute_change_log(base, cur)
        tags = {e["tag"] for e in log}
        assert tags == {"阶段判断", "价值捕获"}
        assert all(e["icon"] == "new" for e in log)

    def test_new_validated_claim_and_upgrade(self):
        base = _snap(claims=[{
            "claim_id": "claim-a", "statement": "旧论断", "status": "draft",
            "support_refs": [],
        }])
        cur = _snap(claims=[
            {"claim_id": "claim-a", "statement": "旧论断", "status": "validated",
             "support_refs": ["ev-1"]},
            {"claim_id": "claim-b", "statement": "新增已校验论断", "status": "validated",
             "support_refs": ["ev-2", "obs-9"]},
        ])
        log = compute_change_log(base, cur)
        texts = [e["text"] for e in log]
        assert any("旧论断" in t for t in texts)  # draft→validated 升级
        assert any("新增已校验论断" in t for t in texts)
        upgraded = next(e for e in log if e["tag"] == "论断通过校验")
        assert upgraded["icon"] == "up" and upgraded["refs"] == ["ev-1"]
        new = next(e for e in log if e["tag"] == "新论断")
        # refs 只带 ev-*（obs/claim 引用不进证据抽屉）
        assert new["refs"] == ["ev-2"]

    def test_new_draft_claim_not_reported(self):
        """draft 新论断不是「变化」（未通过校验的结论不进投资者视图）。"""
        base = _snap()
        cur = _snap(claims=[{"claim_id": "claim-d", "statement": "草稿", "status": "draft",
                             "support_refs": []}])
        assert compute_change_log(base, cur) == []

    def test_superseded_claim_is_risk(self):
        base = _snap(claims=[{"claim_id": "claim-a", "statement": "被推翻的判断",
                              "status": "validated", "support_refs": []}])
        cur = _snap(claims=[{"claim_id": "claim-a", "statement": "被推翻的判断",
                             "status": "superseded", "support_refs": []}])
        log = compute_change_log(base, cur)
        assert any(e["icon"] == "risk" and e["tag"] == "论断被替代" for e in log)

    def test_counter_evidence_and_breakers_are_risk(self):
        base = _snap(summary={"counter_evidence": "旧反证", "thesis_breakers": ["旧证伪"]})
        cur = _snap(summary={"counter_evidence": "新反证：II 期成功率矛盾",
                             "thesis_breakers": ["旧证伪", "新增：里程碑收入未启动"]})
        log = compute_change_log(base, cur)
        risk = [e for e in log if e["kind"] == "risk"]
        assert any("新反证" in e["text"] for e in risk)
        assert any("里程碑收入未启动" in e["text"] for e in risk)
        assert all(e["icon"] in ("risk", "new") for e in risk)

    def test_metric_changes_verbatim_values(self):
        """数字逐字搬移（只截断不改写；old → new 用快照原值）。"""
        base = _snap(summary={"key_metrics": [
            {"metric_key": "revenue", "label": "收入", "value": "2869900000", "status": "ok"},
            {"metric_key": "growth_rate", "label": "增速", "value": None, "status": "missing"},
        ]})
        cur = _snap(summary={"key_metrics": [
            {"metric_key": "revenue", "label": "收入", "value": "3100000000", "status": "ok"},
            {"metric_key": "growth_rate", "label": "增速", "value": "0.182", "status": "ok"},
        ]})
        log = compute_change_log(base, cur)
        metric = [e for e in log if e["kind"] == "metric"]
        assert any("2869900000 → 3100000000" in e["text"] for e in metric)
        assert any("新增指标：增速 0.182" in e["text"] for e in metric)

    def test_metric_new_card_not_reported(self):
        """配方/路由变化带来的新卡不是数据变化（噪声过滤）。"""
        base = _snap(summary={"key_metrics": []})
        cur = _snap(summary={"key_metrics": [
            {"metric_key": "revenue", "label": "收入", "value": "1", "status": "ok"},
        ]})
        assert compute_change_log(base, cur) == []

    def test_company_tier_moves(self):
        ca_base = {"candidates": [
            {"entity_id": "a", "name": "甲", "tier": "watchlist", "evidence_refs": []},
            {"entity_id": "b", "name": "乙", "tier": "included", "evidence_refs": []},
            {"entity_id": "c", "name": "丙", "tier": "excluded", "evidence_refs": []},
        ]}
        ca_cur = {"candidates": [
            {"entity_id": "a", "name": "甲", "tier": "included", "evidence_refs": ["ev-9"]},
            {"entity_id": "b", "name": "乙", "tier": "watchlist", "evidence_refs": []},
            {"entity_id": "d", "name": "丁", "tier": "watchlist", "evidence_refs": []},
        ]}
        log = compute_change_log(_snap(structures={"candidate_assessment": ca_base}),
                                 _snap(structures={"candidate_assessment": ca_cur}))
        comp = [e for e in log if e["kind"] == "company_status"]
        # 甲 watchlist→included = 升级（↑）；乙 included→watchlist = 降级（!）；
        # 丙移除（!）；丁新增（+）
        assert any("甲" in e["text"] and e["icon"] == "up" for e in comp)
        assert any("乙" in e["text"] and e["icon"] == "risk" for e in comp)
        assert any("丙" in e["text"] and e["icon"] == "risk" for e in comp)
        assert any("丁" in e["text"] and e["icon"] == "new" for e in comp)

    def test_catalyst_changes(self):
        vt_base = {"items": [
            {"event": "III 期读出", "status": "expected", "window_start": "2029"},
            {"event": "旧节点", "status": "expected", "window_start": "2026"},
        ]}
        vt_cur = {"items": [
            {"event": "III 期读出", "status": "occurred", "window_start": "2029"},
            {"event": "新催化", "status": "expected", "window_start": "2027"},
        ]}
        log = compute_change_log(_snap(structures={"validation_timeline": vt_base}),
                                 _snap(structures={"validation_timeline": vt_cur}))
        cat = [e for e in log if e["kind"] == "catalyst"]
        assert any("已发生" in e["text"] and e["icon"] == "up" for e in cat)
        assert any("新催化" in e["text"] and e["icon"] == "new" for e in cat)
        assert any("旧节点" in e["text"] and e["icon"] == "risk" for e in cat)

    def test_module_status_transition_only(self):
        base = _snap(modules={
            "key_kpi": {"status": "missing", "title": "关键 KPI", "data_ref": "md-1"},
            "peers": {"status": "partial", "title": "同业与竞争", "data_ref": "md-2"},
        })
        cur = _snap(modules={
            "key_kpi": {"status": "ready", "title": "关键 KPI", "data_ref": "md-3"},
            # 纯 data_ref 变化（状态未变）不报——具体变化由 thesis/metric 等类别承担
            "peers": {"status": "partial", "title": "同业与竞争", "data_ref": "md-9"},
        })
        log = compute_change_log(base, cur)
        assert len(log) == 1
        assert log[0]["kind"] == "module" and log[0]["icon"] == "up"
        assert "关键 KPI" in log[0]["text"] and "缺口 → 就绪" in log[0]["text"]

    def test_total_cap_and_priority(self):
        """总上限 6；thesis/risk 优先于 module（密度硬规则 §9）。"""
        base = _snap(modules={f"m{i}": {"status": "missing", "title": f"模块{i}"}
                              for i in range(8)})
        cur = _snap(
            summary={"thesis": "新结论", "stage": "阶段", "value_capture": "捕获",
                     "counter_evidence": "反证", "thesis_breakers": ["a", "b", "c"]},
            modules={f"m{i}": {"status": "ready", "title": f"模块{i}"} for i in range(8)},
        )
        log = compute_change_log(base, cur)
        assert len(log) == TOTAL_CAP
        kinds = [e["kind"] for e in log]
        assert kinds[:3] == ["thesis"] * 3
        assert kinds[3:5] == ["risk"] * 2
        assert kinds[5] == "module"  # 低优先级类别只占剩余名额


class TestChangeLogPublishFlow:
    """发布链路集成：live 发布冻结 change_log；历史投影不进基线序列。"""

    @pytest.fixture()
    def env(self, tmp_path):
        kb = BitemporalStore(tmp_path / "kb.db")
        metrics = MetricStore(tmp_path / "m.db")
        events = EventStore(tmp_path / "e.db")
        projector = DossierProjector(kb=kb, metrics=metrics)
        service = DossierService(kb=kb, metrics=metrics, projector=projector, events=events)
        return kb, metrics, events, projector, service

    def test_first_publish_empty_then_thesis_entry(self, env):
        kb, metrics, _events, _projector, service = env
        seed_legacy(kb)
        snap1, c1 = service.open("stock", "BE", run_id="r1")
        assert c1 and snap1["change_log"] == []
        # 新 validated 论断 → 第二次发布带 thesis 变化
        seed_claim(metrics, statement="收入增长由数据中心订单驱动")
        snap2, c2 = service.open("stock", "BE", run_id="r1")
        assert c2
        assert any(e["kind"] == "thesis" for e in snap2["change_log"])
        # 冻结：重读旧快照不含后到的变化（历史快照不被改写）
        stored1 = service.get(snap1["context"]["snapshot_id"])
        assert stored1["change_log"] == []

    def test_dedup_republish_keeps_original_change_log(self, env):
        kb, metrics, _events, _projector, service = env
        seed_legacy(kb)
        service.open("stock", "BE", run_id="r2")
        seed_claim(metrics, statement="论断甲：订单驱动增长")
        snap2, c2 = service.open("stock", "BE", run_id="r2")
        assert c2 and snap2["change_log"]
        # 同数据再投影 → 幂等复用：change_log 保持首次发布时冻结的版本
        snap3, c3 = service.open("stock", "BE", run_id="r2")
        assert not c3
        assert snap3["change_log"] == snap2["change_log"]

    def test_metric_change_entry_verbatim(self, env):
        kb, metrics, _events, _projector, service = env
        seed_legacy(kb)
        seed_typed(kb, metrics)
        snap1, _ = service.open("stock", "BE", run_id="r3")
        seed_typed(kb, metrics, knowledge_time=datetime(2025, 3, 1, tzinfo=UTC),
                   value_text="1600 million", run_id="r3b")
        snap2, c2 = service.open("stock", "BE", run_id="r3")
        assert c2
        metric_entries = [e for e in snap2["change_log"] if e["kind"] == "metric"]
        assert any("→" in e["text"] for e in metric_entries)
        # 同实体的 changes API 仍可用（两种消费路径共存）
        diff = service.changes(snap2["context"]["snapshot_id"],
                               snap1["context"]["snapshot_id"])
        assert diff["key_metric_changes"]

    def test_historical_projection_not_in_live_baseline(self, env):
        """历史 as_of 投影不产生 change_log，也不成为后续 live 发布的基线。"""
        kb, metrics, _events, _projector, service = env
        seed_legacy(kb)
        live1, _ = service.open("stock", "BE", run_id="r4")
        # 历史投影（as_of 过去时点）
        hist, _ = service.open("stock", "BE", as_of=datetime(2024, 7, 1, tzinfo=UTC),
                               mode="historical", run_id="r4")
        assert hist["context"]["mode"] == "historical"
        assert hist["change_log"] == []
        # 后续 live 发布的基线仍是 live1（不是历史投影）
        seed_claim(metrics, statement="基线校验论断")
        live2, c2 = service.open("stock", "BE", run_id="r4")
        assert c2
        assert any("基线校验论断" in e["text"] for e in live2["change_log"])
        # 且与 live1 直接 diff 的结论一致（历史投影未插入序列）
        assert live2["data_hash"] != live1["data_hash"]
