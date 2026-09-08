"""/api/v2 档案路由验收（设计 §10.1/§13.1「前端」「历史与评估」组的服务端契约）。

关键场景：快照幂等、上下文不匹配 409、越快照引用拒绝、模块降级 200+reasons、
导出 job 闭环、补研幂等（真实 CommandRunner 装配——工程约定 2：不注入假 runner）。
"""

import time
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.commands.runner import CommandRunner
from finance_agent.commands.steps import StepDeps
from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.dossier.projector import DossierProjector
from finance_agent.dossier.service import DossierService
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.gateway import DataGateway
from finance_agent.harness.approvals import ApprovalService
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.normalization import normalize_raw
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply
from finance_agent.llm.mock import MockLLM
from finance_agent.research.calculations import CalculationService

NOW = datetime(2025, 6, 1, tzinfo=UTC)
OLD = datetime(2024, 6, 1, tzinfo=UTC)
FY2024 = MetricPeriod(start=date(2024, 1, 1), end=date(2024, 12, 31),
                      frequency="FY", fiscal_label="FY2024")


def seeded_app(tmp_path: Path, *, with_runner: bool = True):
    from finance_agent.gateway.adapters.fixture import FixtureAdapter
    from finance_agent.gateway.models import DataRecord, SourceCapability

    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "metrics.db")
    events = EventStore(tmp_path / "events.db")
    decisions = DecisionStore(tmp_path / "decisions.db")
    kb.add_evidence(Evidence(
        evidence_id="ev-1", source_id="edgar", url="https://sec.gov/x",
        verbatim_quote="Total revenue 1500 million for fiscal 2024",
        retrieved_at=NOW, available_at=OLD, pit_grade=PitGrade.A,
    ))
    kb.assert_fact(Fact(
        entity_kind="stock", entity_id="BE", field="business_model",
        value="sells fuel cell systems", knowledge_time=OLD, evidence_ids=["ev-1"],
    ))
    value, steps = normalize_raw("1500 million", "USD")
    from finance_agent.harness.manifest import RunManifest, RunMode
    from finance_agent.knowledge.metric_writer import TypedMetricWriter

    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    obs_id, _ = mw.write_observation(
        ReportedObservation(
            entity_kind="stock", entity_id="BE", metric_key="revenue", period=FY2024,
            value=value, unit="USD", currency="USD",
            raw=RawValue(value_text="1500 million", unit_text="USD"),
            normalization=[s.model_dump(mode="json") for s in steps],
            evidence_refs=["ev-1"], knowledge_time=OLD, source_available_at=OLD,
            retrieved_at=NOW, created_at=NOW, pit_grade=PitGrade.A,
        ),
        run=RunManifest(run_id="seed", mode=RunMode.LIVE),
    )
    from finance_agent.research.artifacts import HeadingBlock, ReportDocument, ResearchArtifact

    doc = ReportDocument(title="BE 研究报告", entity_kind="stock", entity_id="BE",
                         blocks=[HeadingBlock(level=1, text="摘要")]).with_id()
    art = ResearchArtifact(
        entity_kind="stock", entity_id="BE", title="BE 研究报告", report_document=doc,
        status="validated", sufficiency="partial", created_at=NOW,
    ).with_id()
    metrics.save_artifact(artifact_id=art.artifact_id, namespace="prod",
                          payload=art.model_dump(mode="json"))

    projector = DossierProjector(kb=kb, metrics=metrics, decisions=decisions)
    service = DossierService(kb=kb, metrics=metrics, projector=projector,
                             events=events, decisions=decisions)
    calcs = CalculationService(metrics, events=events)

    runner = None
    if with_runner:
        gateway = DataGateway(mode="live", events=events, run_id="live-test")
        gateway.register(FixtureAdapter(
            SourceCapability(source_id="demo", pit_grade=PitGrade.C,
                             server_side_asof=False, description="夹具源"),
            records=[DataRecord(source_id="demo", payload={"form": "10-K"}, url="demo://f")],
        ))
        deps = StepDeps(
            events=events, kb=kb, writer=ProfileWriter(store=kb, events=events),
            gateway=gateway,
            decisions=DecisionService(kb=kb, decisions=decisions, events=events),
            llm_for=lambda role: MockLLM([AssistantReply(content="done")]),
            approvals=ApprovalService(events),
            evals_dir=tmp_path / "evals",
            reports_dir=tmp_path / "reports",
            knowledge_dir=tmp_path / "knowledge",
            metrics=metrics, metric_writer=mw, calculations=calcs,
            dossier_service=service,
            max_rounds=1,
        )
        runner = CommandRunner(deps, approval_timeout_s=0.2)

    app = create_app(
        kb=kb, events=events, decisions=decisions, evals_dir=tmp_path / "evals",
        command_runner=runner, data_dir=tmp_path,
        metrics=metrics, dossier_service=service, calculation_service=calcs,
        reports_dir=tmp_path / "reports", knowledge_dir=tmp_path / "knowledge",
    )
    return TestClient(app), {"kb": kb, "metrics": metrics, "events": events,
                             "service": service, "obs_id": obs_id,
                             "artifact_id": art.artifact_id, "runner": runner}


def wait_for(events, run_id, pred, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = [e for e in events.read(run_id) if pred(e)]
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError(f"等待事件超时: {pred}")


class TestSnapshotEndpoints:
    def test_open_dossier_and_idempotent_reopen(self, tmp_path):
        client, env = seeded_app(tmp_path)
        r1 = client.get("/api/v2/knowledge/stock/BE/dossier")
        assert r1.status_code == 200
        snap1 = r1.json()
        assert snap1["entity"]["id"] == "BE"
        assert snap1["summary"]["key_metrics"]
        r2 = client.get("/api/v2/knowledge/stock/BE/dossier")
        assert r2.json()["context"]["snapshot_id"] == snap1["context"]["snapshot_id"]
        assert r2.json()["data_hash"] == snap1["data_hash"]

    def test_unknown_entity_kind_422(self, tmp_path):
        client, _ = seeded_app(tmp_path)
        assert client.get("/api/v2/knowledge/crypto/BE/dossier").status_code == 422

    def test_snapshot_restore_and_context_mismatch_409(self, tmp_path):
        client, env = seeded_app(tmp_path)
        snap = client.get("/api/v2/knowledge/stock/BE/dossier").json()
        sid = snap["context"]["snapshot_id"]
        assert client.get(f"/api/v2/dossiers/{sid}").status_code == 200
        assert client.get("/api/v2/dossiers/dossier-nope-000000000000").status_code == 404
        # URL 参数与快照 manifest 不一致 → 409 + 正确上下文（不静默重建）
        r = client.get(f"/api/v2/dossiers/{sid}", params={"namespace": "eval:x"})
        assert r.status_code == 409
        assert r.json()["detail"]["snapshot_context"]["namespace"] == "prod"
        r2 = client.get(f"/api/v2/dossiers/{sid}", params={"entity": "stock:OTHER"})
        assert r2.status_code == 409

    def test_historical_dossier(self, tmp_path):
        client, env = seeded_app(tmp_path)
        r = client.get("/api/v2/knowledge/stock/BE/dossier",
                       params={"as_of": "2024-01-01T00:00:00+00:00", "mode": "historical"})
        assert r.status_code == 200
        snap = r.json()
        assert snap["context"]["mode"] == "historical"
        # 2024-01-01：观测（knowledge_time 2024-06）不可知 → 指标缺口（不借用未来）
        km = {m["metric_key"]: m for m in snap["summary"]["key_metrics"]}
        assert km["revenue"]["status"] == "missing"


class TestModuleAndSourceEndpoints:
    def test_module_payload_and_unknown_module(self, tmp_path):
        client, env = seeded_app(tmp_path)
        sid = client.get("/api/v2/knowledge/stock/BE/dossier").json()["context"]["snapshot_id"]
        r = client.get(f"/api/v2/dossiers/{sid}/modules/financial_quality")
        assert r.status_code == 200
        body = r.json()
        series = {s["metric_key"]: s for s in body["payload"]["fy"]["series"]}
        assert series["revenue"]["points"][0]["value"] == "1500000000"
        assert client.get(f"/api/v2/dossiers/{sid}/modules/nope").status_code == 422

    def test_module_missing_data_returns_200_with_reasons(self, tmp_path):
        client, env = seeded_app(tmp_path)
        sid = client.get("/api/v2/knowledge/stock/BE/dossier").json()["context"]["snapshot_id"]
        r = client.get(f"/api/v2/dossiers/{sid}/modules/expectations")
        assert r.status_code == 200  # 缺数据不是错误
        assert r.json()["status"] == "missing" and r.json()["reasons"]

    def test_evidence_visibility_bound_to_snapshot(self, tmp_path):
        """知道 id 也不能绕过快照过滤（§10.1 引用可见性纪律）。"""
        client, env = seeded_app(tmp_path)
        sid = client.get("/api/v2/knowledge/stock/BE/dossier").json()["context"]["snapshot_id"]
        r = client.get(f"/api/v2/dossiers/{sid}/evidence/ev-1")
        assert r.status_code == 200
        assert "1500 million" in r.json()["verbatim_quote"]
        assert r.json()["provider_id"] == "edgar"  # 命名三层：provider 层
        # 已登记但未被该快照引用的证据 → 404
        env["kb"].add_evidence(Evidence(
            evidence_id="ev-outside", source_id="exa", verbatim_quote="unrelated",
            retrieved_at=NOW, pit_grade=PitGrade.C,
        ))
        r2 = client.get(f"/api/v2/dossiers/{sid}/evidence/ev-outside")
        assert r2.status_code == 404 and "越快照" in r2.json()["detail"]

    def test_series_endpoint(self, tmp_path):
        client, env = seeded_app(tmp_path)
        sid = client.get("/api/v2/knowledge/stock/BE/dossier").json()["context"]["snapshot_id"]
        r = client.get(f"/api/v2/dossiers/{sid}/series", params={"metric": "revenue"})
        assert r.status_code == 200
        body = r.json()
        assert body["series"][0]["points"][0]["observation_id"] == env["obs_id"]
        assert client.get(f"/api/v2/dossiers/{sid}/series",
                          params={"frequency": "nope"}).status_code in (200, 422)

    def test_compare_endpoint_exclusions(self, tmp_path):
        client, env = seeded_app(tmp_path)
        r = client.get("/api/v2/knowledge/compare",
                       params={"entities": "stock:BE,stock:PLUG", "metric": "revenue"})
        assert r.status_code == 200
        body = r.json()
        assert body["items"][0]["entity"] == "stock:BE"
        assert any(e["entity"] == "stock:PLUG" for e in body["exclusions"])
        assert body["comparable"] is False  # 单实体不可比——不硬比


class TestArtifactAndExportEndpoints:
    def test_artifact_endpoint(self, tmp_path):
        client, env = seeded_app(tmp_path)
        r = client.get(f"/api/v2/research/artifacts/{env['artifact_id']}")
        assert r.status_code == 200 and r.json()["status"] == "validated"
        assert client.get("/api/v2/research/artifacts/artifact-nope").status_code == 404

    def test_export_job_roundtrip(self, tmp_path):
        client, env = seeded_app(tmp_path)
        sid = client.get("/api/v2/knowledge/stock/BE/dossier").json()["context"]["snapshot_id"]
        r = client.post(f"/api/v2/dossiers/{sid}/exports", json={"format": "markdown"})
        assert r.status_code == 200
        job = r.json()
        assert job["status"] == "completed" and job["job_id"].startswith("job-")
        status = client.get(f"/api/v2/jobs/{job['job_id']}").json()
        assert status["snapshot_id"] == sid
        artifact = client.get(f"/api/v2/jobs/{job['job_id']}/artifact")
        assert artifact.status_code == 200 and "1500000000" in artifact.text
        assert client.get("/api/v2/jobs/job-nope").status_code == 404
        bad = client.post(f"/api/v2/dossiers/{sid}/exports", json={"format": "pdf"})
        assert bad.status_code == 422

    def test_valuation_preview_no_fact_write(self, tmp_path):
        client, env = seeded_app(tmp_path)
        r = client.post("/api/v2/valuations/preview", json={
            "entity_kind": "stock", "entity_id": "BE", "formula_id": "reverse_dcf",
            "inputs": [{"kind": "observation", "label": "revenue_0", "ref_id": env["obs_id"]}],
            "assumptions": {"wacc": "0.10", "terminal_g": "0.025", "tax_rate": "0.21",
                            "years": "10", "target_ev": "4000000000", "ebit_margin": "0.05",
                            "da_ratio": "0.02", "capex_ratio": "0.03", "nwc_ratio": "0.04"},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok" and body["result"]
        assert "预览" in body["note"]
        # 不写事实：KB facts 无新增
        facts = env["kb"].view("stock", "BE", datetime.now(UTC))
        assert "implied_growth" not in facts
        bad = client.post("/api/v2/valuations/preview", json={
            "entity_id": "BE", "formula_id": "nope", "inputs": []})
        assert bad.status_code == 422


class TestResearchRequestEndpoint:
    def test_requires_runner_503(self, tmp_path):
        client, env = seeded_app(tmp_path, with_runner=False)
        r = client.post("/api/v2/research/requests",
                        json={"entity_id": "BE", "objective": "补研"})
        assert r.status_code == 503

    def test_invalid_depth_422(self, tmp_path):
        client, env = seeded_app(tmp_path)
        r = client.post("/api/v2/research/requests",
                        json={"entity_id": "BE", "depth": "ultra"})
        assert r.status_code == 422

    def test_real_runner_dispatch_and_idempotency(self, tmp_path):
        """真实装配（工程约定 2）：真 CommandRunner + 真 steps，LLM 只在边界替身。"""
        client, env = seeded_app(tmp_path)
        body = {"entity_id": "BE", "objective": "订单转化补研", "depth": "targeted",
                "focus": "订单转化", "idempotency_key": "key-1"}
        r1 = client.post("/api/v2/research/requests", json=body)
        assert r1.status_code == 200
        out1 = r1.json()
        assert out1["command_id"].startswith("cmd-")
        session = out1["session_run_id"]
        # 会话流可见 command/run（真实派发管线）
        run_ev = wait_for(env["events"], session, lambda e: e.type == "command/run")[0]
        assert run_ev.payload["name"] == "research"
        assert "--depth=targeted" in run_ev.payload["raw_input"]
        wait_for(env["events"], session, lambda e: e.type == "command/done")
        # 幂等：重复点击返回同一 command（不启动多份同票全量研究）
        r2 = client.post("/api/v2/research/requests", json=body)
        assert r2.json()["command_id"] == out1["command_id"]
        runs = [e for e in env["events"].read(session) if e.type == "command/run"]
        assert len(runs) == 1


class TestScenarioEndpoints:
    """情景保存（§8.5）：滑动不写事实；只有显式保存才建模型 artifact；
    assumption_hash 不匹配 → 409（假设已变需重算）；失败计算不可保存。"""

    def _preview(self, client, env):
        return client.post("/api/v2/valuations/preview", json={
            "entity_kind": "stock", "entity_id": "BE", "formula_id": "reverse_dcf",
            "inputs": [{"kind": "observation", "label": "revenue_0", "ref_id": env["obs_id"]}],
            "assumptions": {"wacc": "0.10", "terminal_g": "0.025", "tax_rate": "0.21",
                            "years": "10", "target_ev": "4000000000", "ebit_margin": "0.05",
                            "da_ratio": "0.02", "capex_ratio": "0.03", "nwc_ratio": "0.04"},
        }).json()

    def test_scenario_save_and_restore(self, tmp_path):
        client, env = seeded_app(tmp_path)
        sid = client.get("/api/v2/knowledge/stock/BE/dossier").json()["context"]["snapshot_id"]
        prev = self._preview(client, env)
        assert prev["status"] == "ok"
        r = client.post("/api/v2/valuations/scenarios", json={
            "base_snapshot": sid,
            "assumption_hash": prev["input_hash"],
            "validated_calculation_id": prev["calculation_id"],
            "name": "隐含增长情景",
            "idempotency_key": "scen-1",
        })
        assert r.status_code == 200
        out = r.json()
        assert out["artifact_id"].startswith("artifact-")
        assert "不创建事实" in out["note"] or "DecisionCard" in out["note"]
        # 幂等：同 key 重复保存返回同一 artifact
        r2 = client.post("/api/v2/valuations/scenarios", json={
            "base_snapshot": sid, "assumption_hash": prev["input_hash"],
            "validated_calculation_id": prev["calculation_id"], "idempotency_key": "scen-1",
        })
        assert r2.json()["artifact_id"] == out["artifact_id"]
        # 恢复：假设/计算/基线可回读
        got = client.get(f"/api/v2/valuations/scenarios/{out['artifact_id']}")
        assert got.status_code == 200
        assert got.json()["snapshot_refs"] == [sid]
        # 保存情景不写事实（KB 无新增字段）
        facts = env["kb"].view("stock", "BE", datetime.now(UTC))
        assert "implied_growth" not in facts

    def test_assumption_hash_mismatch_409(self, tmp_path):
        client, env = seeded_app(tmp_path)
        sid = client.get("/api/v2/knowledge/stock/BE/dossier").json()["context"]["snapshot_id"]
        prev = self._preview(client, env)
        r = client.post("/api/v2/valuations/scenarios", json={
            "base_snapshot": sid,
            "assumption_hash": "ih-stale0000000000",  # 假设已变（或慢响应覆盖新值）
            "validated_calculation_id": prev["calculation_id"],
        })
        assert r.status_code == 409
        assert r.json()["detail"]["expected"] == prev["input_hash"]

    def test_failed_calculation_cannot_be_saved(self, tmp_path):
        client, env = seeded_app(tmp_path)
        sid = client.get("/api/v2/knowledge/stock/BE/dossier").json()["context"]["snapshot_id"]
        bad = client.post("/api/v2/valuations/preview", json={
            "entity_kind": "stock", "entity_id": "BE", "formula_id": "reverse_dcf",
            "inputs": [{"kind": "assumption", "label": "revenue_0", "value": "1000"}],
            "assumptions": {"wacc": "0.02", "terminal_g": "0.05", "tax_rate": "0.2",
                            "years": "10", "target_ev": "3000", "ebit_margin": "0.1",
                            "da_ratio": "0.02", "capex_ratio": "0.03", "nwc_ratio": "0.04"},
        }).json()
        assert bad["status"] == "failed"
        r = client.post("/api/v2/valuations/scenarios", json={
            "base_snapshot": sid, "assumption_hash": bad["input_hash"],
            "validated_calculation_id": bad["calculation_id"],
        })
        assert r.status_code == 422

    def test_export_with_scenario_requires_matching_baseline(self, tmp_path):
        client, env = seeded_app(tmp_path)
        sid = client.get("/api/v2/knowledge/stock/BE/dossier").json()["context"]["snapshot_id"]
        prev = self._preview(client, env)
        scen = client.post("/api/v2/valuations/scenarios", json={
            "base_snapshot": sid, "assumption_hash": prev["input_hash"],
            "validated_calculation_id": prev["calculation_id"],
        }).json()
        # 匹配基线 → 导出附加情景说明（注明与发布版差异）
        r = client.post(f"/api/v2/dossiers/{sid}/exports",
                        json={"format": "markdown",
                              "saved_scenario_artifact_id": scen["artifact_id"]})
        assert r.status_code == 200
        content = client.get(f"/api/v2/jobs/{r.json()['job_id']}/artifact").text
        assert "附加用户情景（非发布版）" in content
        # 历史快照导出默认不附加今天新生成的情景：基线不匹配 → 409
        hist = client.get("/api/v2/knowledge/stock/BE/dossier",
                          params={"as_of": "2024-01-01T00:00:00+00:00", "mode": "historical"}).json()
        r2 = client.post(f"/api/v2/dossiers/{hist['context']['snapshot_id']}/exports",
                         json={"format": "markdown",
                               "saved_scenario_artifact_id": scen["artifact_id"]})
        assert r2.status_code == 409


class TestEntitiesEndpoint:
    def test_v2_entities_merge_research_state(self, tmp_path):
        client, env = seeded_app(tmp_path)
        r = client.get("/api/v2/knowledge/entities")
        assert r.status_code == 200
        rows = {e["id"]: e for e in r.json()}
        be = rows["BE"]
        assert be["observation_count"] == 1
        assert be["artifact_count"] == 1
        assert be["latest_artifact_title"] == "BE 研究报告"
        assert be["snapshot_id"] is None or isinstance(be["snapshot_id"], str)
        # 打开档案后 snapshot_id 出现在列表（最近冻结快照）
        client.get("/api/v2/knowledge/stock/BE/dossier")
        rows2 = {e["id"]: e for e in client.get("/api/v2/knowledge/entities").json()}
        assert rows2["BE"]["snapshot_id"]


class TestReviewFixesApi:
    """review #16/#23/#25/#26/#27 的 API 层回归。"""

    def _seed_quarter_obs(self, env, entity_id="BE"):
        """给 BE 补一条 2024Q4 单季观测（与 FY2024 并存，制造期间错位场景）。"""
        from finance_agent.harness.manifest import RunManifest, RunMode
        from finance_agent.knowledge.metric_writer import TypedMetricWriter
        from finance_agent.knowledge.metrics import MetricPeriod as MP
        from finance_agent.knowledge.metrics import RawValue as RV
        from finance_agent.knowledge.metrics import ReportedObservation as RO
        from finance_agent.knowledge.models import Evidence
        from finance_agent.knowledge.normalization import normalize_raw

        kb, metrics = env["kb"], env["metrics"]
        w = TypedMetricWriter(store=metrics, kb=kb)
        kb.add_evidence(Evidence(
            evidence_id="ev-q4", source_id="edgar", verbatim_quote="Q4 revenue 400 million",
            retrieved_at=NOW, available_at=OLD, pit_grade=PitGrade.A,
        ))
        value, steps = normalize_raw("400 million", "USD")
        obs = RO(
            entity_kind="stock", entity_id=entity_id, metric_key="revenue",
            period=MP(start=date(2024, 10, 1), end=date(2024, 12, 31),
                      frequency="Q", fiscal_label="2024Q4"),
            value=value, unit="USD", currency="USD",
            raw=RV(value_text="400 million", unit_text="USD"),
            normalization=[s.model_dump(mode="json") for s in steps],
            evidence_refs=["ev-q4"], knowledge_time=OLD, source_available_at=OLD,
            retrieved_at=NOW, created_at=NOW, pit_grade=PitGrade.A,
        )
        return w.write_observation(obs, run=RunManifest(run_id="seed2", mode=RunMode.LIVE))[0]

    def test_compare_fy_vs_quarter_not_comparable(self, tmp_path):
        """#16 复现路径：FY2024 全年 vs 2024Q4 单季不得 comparable=true。"""
        client, env = seeded_app(tmp_path)
        self._seed_quarter_obs(env, "PLUG")  # PLUG 只有 Q4 单季
        r = client.get("/api/v2/knowledge/compare",
                       params={"entities": "stock:BE,stock:PLUG", "metric": "revenue"})
        body = r.json()
        assert body["comparable"] is False
        # FY2024 与 2024Q4 的 period_end 相同但频率不同 → 频率/期间口径 note 必须出现
        assert any(("频率不一致" in n) or ("期间不一致" in n) for n in body["notes"])
        # 限定共同期间后可比性恢复判断（PLUG 无 FY 观测 → exclusion 而非硬比）
        r2 = client.get("/api/v2/knowledge/compare",
                        params={"entities": "stock:BE,stock:PLUG", "metric": "revenue",
                                "frequency": "FY"})
        body2 = r2.json()
        assert any(e["entity"] == "stock:PLUG" for e in body2["exclusions"])
        assert body2["comparable"] is False

    def test_research_request_preserves_industry_kind(self, tmp_path):
        """#23：行业档案发起的补研必须仍研究行业实体。"""
        client, env = seeded_app(tmp_path)
        r = client.post("/api/v2/research/requests", json={
            "entity_kind": "industry", "entity_id": "ai-for-science",
            "objective": "瓶颈环节更新", "depth": "refresh",
        })
        assert r.status_code == 200
        session = r.json()["session_run_id"]
        run_ev = wait_for(env["events"], session, lambda e: e.type == "command/run")[0]
        assert run_ev.payload["args"]["ticker"] == "industry:ai-for-science"
        # 非法 entity_kind 拒绝
        bad = client.post("/api/v2/research/requests", json={
            "entity_kind": "crypto", "entity_id": "BTC"})
        assert bad.status_code == 422

    def test_scenario_entity_and_version_binding(self, tmp_path):
        """#25：计算实体/命名空间/模型版本必须与基线快照一致。"""
        client, env = seeded_app(tmp_path)
        sid = client.get("/api/v2/knowledge/stock/BE/dossier").json()["context"]["snapshot_id"]
        # OTHER 实体的计算（同数值）不得绑定 BE 快照
        prev_other = client.post("/api/v2/valuations/preview", json={
            "entity_kind": "stock", "entity_id": "OTHER", "formula_id": "yoy_growth",
            "inputs": [{"kind": "assumption", "label": "current", "value": "150"},
                       {"kind": "assumption", "label": "prior", "value": "100"}],
        }).json()
        r = client.post("/api/v2/valuations/scenarios", json={
            "base_snapshot": sid,
            "model_version": "yoy_growth@v1",
            "assumption_hash": prev_other["input_hash"],
            "validated_calculation_id": prev_other["calculation_id"],
        })
        assert r.status_code == 409 and "实体" in str(r.json()["detail"])
        # 任意模型版本声明 → 422
        prev = client.post("/api/v2/valuations/preview", json={
            "entity_kind": "stock", "entity_id": "BE", "formula_id": "yoy_growth",
            "inputs": [{"kind": "observation", "label": "current", "ref_id": env["obs_id"]},
                       {"kind": "assumption", "label": "prior", "value": "1000000000"}],
        }).json()
        r2 = client.post("/api/v2/valuations/scenarios", json={
            "base_snapshot": sid, "model_version": "magic_model@v9",
            "assumption_hash": prev["input_hash"],
            "validated_calculation_id": prev["calculation_id"],
        })
        assert r2.status_code == 422 and "model_version" in str(r2.json()["detail"])

    def test_scenario_not_default_publish_and_has_markdown(self, tmp_path):
        """#26/#27：情景不进默认结论投影；保存前渲染可读正文。"""
        client, env = seeded_app(tmp_path)
        sid = client.get("/api/v2/knowledge/stock/BE/dossier").json()["context"]["snapshot_id"]
        before = client.get(f"/api/v2/dossiers/{sid}").json()["summary"]["thesis_kind"]
        prev = client.post("/api/v2/valuations/preview", json={
            "entity_kind": "stock", "entity_id": "BE", "formula_id": "reverse_dcf",
            "inputs": [{"kind": "observation", "label": "revenue_0", "ref_id": env["obs_id"]}],
            "assumptions": {"wacc": "0.10", "terminal_g": "0.025", "tax_rate": "0.21",
                            "years": "10", "target_ev": "4000000000", "ebit_margin": "0.05",
                            "da_ratio": "0.02", "capex_ratio": "0.03", "nwc_ratio": "0.04"},
        }).json()
        scen = client.post("/api/v2/valuations/scenarios", json={
            "base_snapshot": sid, "model_version": "reverse_dcf@v1",
            "assumption_hash": prev["input_hash"],
            "validated_calculation_id": prev["calculation_id"],
            "name": "隐含增长情景",
        }).json()
        # #27：markdown 已渲染（产物页不再是空壳）
        art = client.get(f"/api/v2/research/artifacts/{scen['artifact_id']}").json()
        assert art["markdown"] and "隐含增长情景" in art["markdown"]
        assert art["purpose"] == "scenario"
        assert "reverse_dcf" in art["markdown"]  # 假设与结果可读
        # #26：重新打开档案，默认研究结论不被情景替换
        snap2 = client.get("/api/v2/knowledge/stock/BE/dossier").json()
        assert snap2["summary"]["thesis_kind"] == before
        assert scen["artifact_id"] not in snap2["research"]["artifact_refs"]
        # research_sources 分区展示用户情景
        mod = client.get(
            f"/api/v2/dossiers/{snap2['context']['snapshot_id']}/modules/research_sources"
        ).json()
        assert any(s["artifact_id"] == scen["artifact_id"] for s in mod["payload"]["scenarios"])
