"""统一知识与档案工具（tools-plugins 方案 §5.3，PR#2 shared-knowledge-tools）。

验收点：
- typed 查询带过滤与游标（截断不静默：total/next_cursor 显式）；
- read_evidence 批量读取逐项成功/失败（error 与 empty 区分，跨实体拒绝）；
- get_research_context 一次取回问题结论/字段/观测/论断/计算/冲突（超预算显式降级）；
- list_conflicts / adjudicate_conflict 覆盖 legacy 字段与 typed 语义键，
  裁决校验版本链归属与证据关联，投影按裁决取获胜版本；
- S1（make_research_tools）/ S2（step_profile_update）/ 合成（step_synthesize）
  共用同一模块；S2 从「重写 thesis」升级为整合（读上下文 → 裁决 → thesis Fact+Claim）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from finance_agent.commands.steps import StepContext, StepDeps, step_profile_update
from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.gateway import DataGateway
from finance_agent.harness.approvals import ApprovalService
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.research.context_tools import CONTEXT_TOOL_SCHEMAS, make_context_tools

NOW = datetime.now(UTC)
T0 = datetime(2024, 3, 1, tzinfo=UTC)
T1 = datetime(2024, 6, 1, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def make_obs(*, value: str, evidence: str, metric_key: str = "revenue",
             dimensions: dict | None = None, knowledge_time: datetime = T0,
             nature: str = "reported") -> ReportedObservation:
    return ReportedObservation(
        entity_kind="stock", entity_id="BE", metric_key=metric_key,
        period=MetricPeriod(start=datetime(2023, 1, 1, tzinfo=UTC).date(),
                            end=datetime(2023, 12, 31, tzinfo=UTC).date(),
                            frequency="FY", fiscal_label="FY2023"),
        value=value, unit="USD", currency="USD", basis="GAAP",
        dimensions=dimensions or {},
        raw=RawValue(value_text=value, unit_text="USD", quote_ref=evidence),
        evidence_refs=[evidence],
        knowledge_time=knowledge_time, retrieved_at=knowledge_time, created_at=knowledge_time,
        pit_grade=PitGrade.A,
    )


@pytest.fixture()
def env(tmp_path: Path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)

    kb.add_evidence(Evidence(
        evidence_id="ev-sec", source_id="edgar", url="https://sec.gov/f1",
        verbatim_quote="Total revenue was " + "1" * 1982,  # 2000 字符上限，超过 read 截断阈值
        retrieved_at=NOW, available_at=T0, pit_grade=PitGrade.A,
    ))
    kb.add_evidence(Evidence(
        evidence_id="ev-web", source_id="web_search", url="https://news.example/r",
        verbatim_quote="revenue reached 120000000", retrieved_at=NOW,
        available_at=T1, pit_grade=PitGrade.B,
    ))
    # 竞争观测：同语义键不同值 → conflict_flag（第二版本）
    oid1, _ = metrics.assert_observation(make_obs(value="100000000", evidence="ev-sec"))
    oid2, _ = metrics.assert_observation(
        make_obs(value="120000000", evidence="ev-web", knowledge_time=T1))
    # 另一个指标 + 维度
    oid3, _ = metrics.assert_observation(make_obs(
        value="500", evidence="ev-sec", metric_key="gross_margin",
        dimensions={"segment": "platform"}))
    # 证据不可解析的竞争版本（裁决必须拒绝它获胜）
    oid4, _ = metrics.assert_observation(
        make_obs(value="999", evidence="ev-sec", metric_key="capex"))
    metrics.assert_observation(ReportedObservation(
        entity_kind="stock", entity_id="BE", metric_key="capex",
        period=MetricPeriod(start=datetime(2023, 1, 1, tzinfo=UTC).date(),
                            end=datetime(2023, 12, 31, tzinfo=UTC).date(),
                            frequency="FY", fiscal_label="FY2023"),
        value="111", unit="USD", currency="USD",
        raw=RawValue(value_text="111", quote_ref="ev-gone"),
        evidence_refs=["ev-gone"],  # 未登记
        knowledge_time=T1, retrieved_at=T1, created_at=T1, pit_grade=PitGrade.C,
    ))
    # 论断与计划
    metrics.save_claim(claim_id="claim-a1", namespace="prod", payload={
        "claim_id": "claim-a1", "entity_kind": "stock", "entity_id": "BE",
        "statement": "订单口径为含税合同额", "kind": "fact_summary", "status": "validated",
        "question_id": "q1", "support_refs": ["ev-sec"], "counter_refs": [],
        "limitations": [], "created_at": _iso(T1), "namespace": "prod",
    })
    metrics.save_claim(claim_id="claim-b2", namespace="prod", payload={
        "claim_id": "claim-b2", "entity_kind": "stock", "entity_id": "BE",
        "statement": "毛利率改善趋势待验证", "kind": "hypothesis", "status": "draft",
        "question_id": "q2", "support_refs": [], "counter_refs": [],
        "limitations": [], "created_at": _iso(T1), "namespace": "prod",
    })
    metrics.save_calculation(
        calculation_id="calc-x1", namespace="prod", entity_kind="stock", entity_id="BE",
        formula_id="yoy_growth", formula_version=1, status="ok", result="0.2", unit="ratio",
        input_hash="h1", created_at=T1, run_id="r1",
        payload={"calculation_id": "calc-x1", "formula_id": "yoy_growth",
                 "formula_version": 1, "status": "ok", "result": "0.2", "unit": "ratio",
                 "created_at": _iso(T1), "input_refs": [
                     {"kind": "observation", "label": "current", "ref_id": oid1}]},
    )
    metrics.save_plan(plan_id="plan-c1", namespace="prod", payload={
        "plan_id": "plan-c1", "entity_kind": "stock", "entity_id": "BE",
        "objective": "验证订单与收入", "mode": "targeted", "created_at": _iso(T0),
        "recipe_id": "general", "recipe_version": "v1",
        "questions": [
            {"question_id": "q1", "text": "订单口径？", "priority": "high",
             "status": "answered", "module": "business",
             "conclusion": "含税合同额", "support_refs": ["ev-sec"], "unresolved": []},
            {"question_id": "q2", "text": "毛利率趋势？", "priority": "medium",
             "status": "gathering", "module": "financial_quality",
             "conclusion": "", "support_refs": [], "unresolved": []},
        ],
        "budgets": {"question_coverage_target": 0.8},
    })
    # legacy 字段冲突
    kb.assert_fact(Fact(entity_kind="stock", entity_id="BE", field="moat",
                        value="网络效应", knowledge_time=T0, evidence_ids=["ev-sec"]))
    kb.assert_fact(Fact(entity_kind="stock", entity_id="BE", field="moat",
                        value="转换成本", knowledge_time=T1, evidence_ids=["ev-web"]))
    return kb, metrics, events, writer, mw, {
        "oid1": oid1, "oid2": oid2, "oid3": oid3, "oid4": oid4,
    }


def ctx_tools(env, **kw):
    kb, metrics, events, writer, mw, ids = env
    return make_context_tools(
        kb=kb, metrics=metrics, entity_kind="stock", entity_id="BE",
        namespace="prod", plan_id="plan-c1", writer=writer,
        manifest=RunManifest(run_id="live-ctx", mode=RunMode.LIVE), events=events, **kw,
    )


def payload(out: dict) -> dict:
    return json.loads(out["content"])


# ---------------- typed 查询：过滤 + 游标 ----------------


class TestTypedQueries:
    def test_query_observations_pagination_not_silent(self, env):
        tools = ctx_tools(env, default_limits={"query_observations": 2})
        out = payload(tools["query_observations"]({}))
        # 投影按语义键收敛：revenue（竞争版本折叠）/ gross_margin / capex = 3 条当前值
        assert out["total"] == 3 and out["returned"] == 2
        assert out["next_cursor"] == 2
        page2 = payload(tools["query_observations"]({"cursor": out["next_cursor"]}))
        assert page2["returned"] == 1 and page2["next_cursor"] is None
        ids1 = {i["observation_id"] for i in out["items"]}
        ids2 = {i["observation_id"] for i in page2["items"]}
        assert not ids1 & ids2, "分页必须不重不漏（排序确定性）"

    def test_query_observations_filters(self, env):
        tools = ctx_tools(env)
        ids = env[5]
        by_key = payload(tools["query_observations"]({"metric_key": "gross_margin"}))
        assert by_key["total"] == 1
        assert by_key["items"][0]["dimensions"] == {"segment": "platform"}
        by_dim = payload(tools["query_observations"](
            {"dimensions": {"segment": "platform"}}))
        assert by_dim["total"] == 1
        by_period = payload(tools["query_observations"]({"period_end": "2023-12-31"}))
        assert by_period["total"] == 3
        assert payload(tools["query_observations"]({"period_end": "1999-01-01"}))["total"] == 0
        assert ids["oid1"]  # 夹具自检

    def test_query_claims_filters(self, env):
        tools = ctx_tools(env)
        all_claims = payload(tools["query_claims"]({}))
        assert all_claims["total"] == 2
        q1 = payload(tools["query_claims"]({"question_id": "q1"}))
        assert q1["total"] == 1 and q1["items"][0]["claim_id"] == "claim-a1"
        hypo = payload(tools["query_claims"]({"kind": "hypothesis"}))
        assert hypo["total"] == 1
        only_validated = payload(tools["query_claims"]({"statuses": ["validated"]}))
        assert only_validated["total"] == 1
        # 核验状态随投影透出（validated ≠ 内容已核验）
        assert only_validated["items"][0]["verification"]["evidence_support"] == "unchecked"

    def test_query_calculations_filter_and_shape(self, env):
        tools = ctx_tools(env)
        out = payload(tools["query_calculations"]({}))
        assert out["total"] == 1
        item = out["items"][0]
        assert item["calculation_id"] == "calc-x1"
        assert item["input_refs"][0]["ref_id"].startswith("obs-")
        assert payload(tools["query_calculations"]({"formula_id": "cagr"}))["total"] == 0


# ---------------- read_evidence：批量 + 逐项失败可见 ----------------


class TestReadEvidence:
    def test_batch_mixed_refs(self, env):
        tools = ctx_tools(env)
        ids = env[5]
        out = payload(tools["read_evidence"]({"refs": [
            "ev-sec", ids["oid1"], "calc-x1", "claim-a1", "ev-does-not-exist", "bogus-ref",
        ]}))
        assert out["resolved"] == 4 and out["failed"] == 2
        by_ref = {i["ref"]: i for i in out["items"]}
        assert by_ref["ev-sec"]["kind"] == "evidence"
        assert by_ref["ev-sec"]["source_id"] == "edgar"
        assert by_ref["ev-sec"]["quote_truncated"] is True  # 超长摘录显式截断
        assert by_ref[ids["oid1"]]["kind"] == "observation"
        assert by_ref["calc-x1"]["kind"] == "calculation"
        assert by_ref["claim-a1"]["statement"] == "订单口径为含税合同额"
        assert "error" in by_ref["ev-does-not-exist"]
        assert "未知引用前缀" in by_ref["bogus-ref"]["error"]

    def test_single_evidence_id_compat(self, env):
        """旧用法（synthesize/committee 的 evidence_id 单参）保持兼容。"""
        tools = ctx_tools(env)
        out = payload(tools["read_evidence"]({"evidence_id": "ev-web"}))
        assert out["resolved"] == 1
        assert out["items"][0]["verbatim_quote"] == "revenue reached 120000000"

    def test_cross_entity_fact_ref_rejected(self, env):
        kb, metrics, events, writer, mw, ids = env
        kb.assert_fact(Fact(entity_kind="stock", entity_id="OTHER", field="moat",
                            value="别的公司", knowledge_time=T0, evidence_ids=["ev-sec"]))
        row = kb._conn.execute(  # noqa: SLF001 - 测试取 fact_id
            "SELECT fact_id FROM facts WHERE entity_id = 'OTHER'").fetchone()
        tools = ctx_tools(env)
        out = payload(tools["read_evidence"]({"refs": [row[0]]}))
        assert out["failed"] == 1
        assert "跨上下文拒绝" in out["items"][0]["error"]

    def test_empty_refs_is_error_not_empty(self, env):
        tools = ctx_tools(env)
        out = payload(tools["read_evidence"]({}))
        assert "error" in out, "空参数必须显式报错（error 与 empty 区分）"


# ---------------- get_research_context：聚合 + 降级 ----------------


class TestResearchContext:
    def test_aggregates_questions_facts_typed(self, env):
        tools = ctx_tools(env)
        out = payload(tools["get_research_context"]({}))
        assert out["entity"] == "stock:BE"
        q1 = next(q for q in out["questions"] if q["question_id"] == "q1")
        assert q1["status"] == "answered" and q1["conclusion"] == "含税合同额"
        assert "moat" in out["facts"] and out["facts"]["moat"]["conflict"] is True
        assert out["observations"]["total"] == 3  # 语义键收敛后的当前投影
        assert out["claims"]["total"] == 2
        assert out["calculations"]["total"] == 1
        assert out["conflicts"]["legacy_fields"] == ["moat"]
        assert out["conflicts"]["typed_semantic_keys"], "typed 竞争语义键必须列出"

    def test_question_ids_narrow_scope(self, env):
        tools = ctx_tools(env)
        out = payload(tools["get_research_context"]({"question_ids": ["q2"]}))
        assert [q["question_id"] for q in out["questions"]] == ["q2"]

    def test_over_budget_degrades_to_ids_with_hint(self, env, monkeypatch):
        # 把上下文预算压小，确定性触发降级路径（不依赖数据量碰巧超限）
        monkeypatch.setattr("finance_agent.research.context_tools.CONTEXT_MAX_CHARS", 2500)
        kb, metrics, events, writer, mw, ids = env
        for i in range(10):
            metrics.save_claim(claim_id=f"claim-bulk-{i:03d}", namespace="prod", payload={
                "claim_id": f"claim-bulk-{i:03d}", "entity_kind": "stock", "entity_id": "BE",
                "statement": "很长的论断内容" * 40, "kind": "analysis", "status": "draft",
                "support_refs": [], "counter_refs": [], "limitations": [],
                "created_at": _iso(T1), "namespace": "prod",
            })
        tools = ctx_tools(env)
        out = tools["get_research_context"]({"limit": 80})
        assert out["truncated"] is True
        data = json.loads(out["content"])
        assert data["claims"]["truncated"] is True
        assert data["claims"]["total"] == 12
        assert data["claims"]["ids"], "降级后保留 id 清单（可回读，不是丢弃）"
        assert "query_claims" in data["hint"]


# ---------------- 冲突：列出与裁决 ----------------


class TestConflicts:
    def test_list_conflicts_both_layers(self, env):
        tools = ctx_tools(env)
        out = payload(tools["list_conflicts"]({}))
        legacy = out["legacy_fields"]
        assert legacy and legacy[0]["field"] == "moat"
        assert len(legacy[0]["versions"]) == 2
        typed = out["typed_semantic_keys"]
        assert {t["metric_key"] for t in typed} >= {"revenue", "capex"}
        rev = next(t for t in typed if t["metric_key"] == "revenue")
        assert len(rev["versions"]) == 2

    def test_adjudicate_typed_winner_projects(self, env):
        """裁决旧版本获胜 → 当前投影取获胜版本（不是只清标记）。"""
        kb, metrics, events, writer, mw, ids = env
        tools = ctx_tools(env)
        rev_sem = payload(tools["list_conflicts"]({}))
        sem = next(t["semantic_hash"] for t in rev_sem["typed_semantic_keys"]
                   if t["metric_key"] == "revenue")
        out = payload(tools["adjudicate_conflict"]({
            "target": "observation", "semantic_hash": sem,
            "keep_observation_id": ids["oid1"],
            "rationale": "10-K 原文披露优先于媒体转载",
        }))
        assert out["resolved"] == "observation"
        current = metrics.observations_as_of("stock", "BE", datetime.now(UTC),
                                             metric_key="revenue")
        assert [o.observation_id for o in current] == [ids["oid1"]]
        # 裁决事件可审计
        evs = [e for e in events.read("live-ctx") if e.type == "metric/conflict_resolved"]
        assert evs and evs[0].payload["keep_observation_id"] == ids["oid1"]
        assert evs[0].payload["rationale"] == "10-K 原文披露优先于媒体转载"
        # 已裁决的语义键不再出现在开放冲突里
        after = payload(tools["list_conflicts"]({}))
        assert sem not in {t["semantic_hash"] for t in after["typed_semantic_keys"]}

    def test_adjudicate_typed_rejections(self, env):
        kb, metrics, events, writer, mw, ids = env
        tools = ctx_tools(env)
        # 无 rationale → 拒
        out = payload(tools["adjudicate_conflict"]({
            "target": "observation", "semantic_hash": "sem-x",
            "keep_observation_id": ids["oid1"]}))
        assert "rejected" in out and "rationale" in out["rejected"]
        # 获胜方不在版本链 → 拒
        sem = next(t["semantic_hash"] for t in payload(tools["list_conflicts"]({}))
                   ["typed_semantic_keys"] if t["metric_key"] == "revenue")
        out = payload(tools["adjudicate_conflict"]({
            "target": "observation", "semantic_hash": sem,
            "keep_observation_id": ids["oid3"], "rationale": "r"}))
        assert "rejected" in out and "不在语义键" in out["rejected"]
        # 获胜方证据不可解析 → 拒（不落无效裁决）
        capex_sem = next(t["semantic_hash"] for t in payload(tools["list_conflicts"]({}))
                         ["typed_semantic_keys"] if t["metric_key"] == "capex")
        bad = next(v["observation_id"] for v in
                   next(t for t in payload(tools["list_conflicts"]({}))
                        ["typed_semantic_keys"] if t["metric_key"] == "capex")["versions"]
                   if v["value"] == "111")
        out = payload(tools["adjudicate_conflict"]({
            "target": "observation", "semantic_hash": capex_sem,
            "keep_observation_id": bad, "rationale": "r"}))
        assert "rejected" in out and "不可解析" in out["rejected"]

    def test_adjudicate_field_target_promotes(self, env):
        """field 目标与 resolve_conflict 同引擎：获胜方非最新时同值晋升。"""
        kb, metrics, events, writer, mw, ids = env
        tools = ctx_tools(env)
        hist = kb.history("stock", "BE", "moat")
        out = payload(tools["adjudicate_conflict"]({
            "target": "field", "field": "moat",
            "keep_fact_id": hist[0].fact_id, "rationale": "一手披露定义优先",
        }))
        assert out["winner_fact_id"] == hist[0].fact_id
        assert out["promoted_fact_id"]
        assert kb.as_of("stock", "BE", datetime.now(UTC))["moat"].value == "网络效应"

    def test_read_only_excludes_adjudicate(self, env):
        tools = ctx_tools(env, read_only=True)
        assert "adjudicate_conflict" not in tools
        assert "list_conflicts" in tools and "read_evidence" in tools


# ---------------- S1 / S2 / 合成共用装配 ----------------


class TestSharedWiring:
    def test_s1_research_tools_include_context(self, env):
        """S1 worker 也拿到统一读取工具（研究复用已有 typed 数据）。"""
        from finance_agent.research.evidence_desk import ChunkStore
        from finance_agent.research.tools import make_research_tools

        kb, metrics, events, writer, mw, ids = env
        tools, _ = make_research_tools(
            store=kb, writer=writer,
            manifest=RunManifest(run_id="live-s1", mode=RunMode.LIVE),
            entity_kind="stock", entity_id="BE", chunk_store=ChunkStore(),
            events=events, metrics=metrics, metric_writer=mw, plan_id="plan-c1",
        )
        for name in ("get_research_context", "query_observations", "query_claims",
                     "query_calculations", "read_evidence", "list_conflicts",
                     "adjudicate_conflict"):
            assert name in tools, f"S1 缺共享工具 {name}"
        assert "resolve_conflict" in tools, "旧别名必须保留（方案 §11.1 旧工具保别名）"
        # 每个共享工具都有 schema（路由层不回退空参 schema）
        for name in CONTEXT_TOOL_SCHEMAS:
            assert CONTEXT_TOOL_SCHEMAS[name]["parameters"].get("properties") is not None

    def test_s2_profile_update_consolidates(self, env, tmp_path):
        """S2 整合语义：读上下文 → 核冲突 → thesis 同时落 Fact（兼容）+ Claim（新读侧）。"""
        kb, metrics, events, writer, mw, ids = env
        script = [
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c0", name="get_research_context", arguments={})]),
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c1", name="list_conflicts", arguments={})]),
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c2", name="adjudicate_conflict", arguments={
                    "target": "field", "field": "moat",
                    "keep_fact_id": kb.history("stock", "BE", "moat")[0].fact_id,
                    "rationale": "一手披露定义优先",
                })]),
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c3", name="propose_thesis", arguments={
                    "thesis": "订单口径已核实为含税合同额，收入确认风险下降。",
                    "evidence_ids": ["ev-sec"],
                    "limitations": ["毛利率趋势待验证"],
                })]),
            AssistantReply(content="done"),
        ]
        deps = StepDeps(
            events=events, kb=kb, writer=writer,
            gateway=DataGateway(mode="live", events=events, run_id="live-s2"),
            decisions=DecisionService(kb=kb, decisions=DecisionStore(tmp_path / "d.db"),
                                      events=events),
            llm_for=lambda role: MockLLM(script),
            approvals=ApprovalService(events),
            evals_dir=tmp_path / "evals", reports_dir=tmp_path / "reports",
            knowledge_dir=tmp_path / "knowledge",
            metrics=metrics, metric_writer=mw,
        )
        ctx = StepContext(
            command_id="cmd-s2", session_run_id="live-s2", child_run_id="live-s2--cmd-s2-2",
            ticker="BE", objective="", config="", should_cancel=lambda: False,
            entity_kind="stock",
        )
        result = step_profile_update(deps, ctx)
        assert result.status == "completed"
        assert "thesis 已修订" in result.summary and "claim-" in result.summary

        # 共享工具确实被装配并执行（真实装配：tool/result 事件为证）
        tool_results = [e for e in events.read(ctx.child_run_id) if e.type == "tool/result"]
        names = [e.payload["name"] for e in tool_results]
        assert "get_research_context" in names and "list_conflicts" in names
        ctx_out = json.loads(next(e.payload["content"] for e in tool_results
                                  if e.payload["name"] == "get_research_context"))
        assert ctx_out["questions"], "S2 必须能看到冻结计划的问题结论"

        # 冲突被真裁决（获胜版本晋升为当前投影）
        assert kb.as_of("stock", "BE", datetime.now(UTC))["moat"].value == "网络效应"

        # thesis 分层落库：Fact 兼容投影 + Claim 新读侧
        assert kb.as_of("stock", "BE", datetime.now(UTC))["thesis"].value.startswith("订单口径已核实")
        claims = metrics.claims_as_of("stock", "BE", datetime.now(UTC))
        thesis_claims = [c for c in claims if c.get("legacy_field") == "thesis"]
        assert len(thesis_claims) == 1
        claim = thesis_claims[0]
        assert claim["kind"] == "analysis" and claim["status"] == "validated"
        assert claim["limitations"] == ["毛利率趋势待验证"]
        assert claim["verification"]["references_valid"] is True
        assert claim["verification"]["evidence_support"] == "unchecked"

    def test_s2_without_metrics_keeps_legacy_behavior(self, env, tmp_path):
        """旧装配（metrics=None）：S2 行为不破坏（query_kb → propose_thesis 只落 Fact）。"""
        kb, metrics, events, writer, mw, ids = env
        kb.assert_fact(Fact(entity_kind="stock", entity_id="BE", field="business_model",
                            value="燃料电池系统", knowledge_time=T0, evidence_ids=["ev-sec"]))
        script = [
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c0", name="query_kb", arguments={})]),
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c1", name="propose_thesis", arguments={
                    "thesis": "燃料电池系统商，订单口径已核实。",
                    "evidence_ids": ["ev-sec"]})]),
            AssistantReply(content="done"),
        ]
        deps = StepDeps(
            events=events, kb=kb, writer=writer,
            gateway=DataGateway(mode="live", events=events, run_id="live-s2l"),
            decisions=DecisionService(kb=kb, decisions=DecisionStore(tmp_path / "d2.db"),
                                      events=events),
            llm_for=lambda role: MockLLM(script),
            approvals=ApprovalService(events),
            evals_dir=tmp_path / "evals", reports_dir=tmp_path / "reports",
            knowledge_dir=tmp_path / "knowledge",
        )
        ctx = StepContext(
            command_id="cmd-s2l", session_run_id="live-s2l", child_run_id="live-s2l--c-2",
            ticker="BE", objective="", config="", should_cancel=lambda: False,
            entity_kind="stock",
        )
        result = step_profile_update(deps, ctx)
        assert result.status == "completed" and "thesis 已修订" in result.summary
        assert "claim" not in result.summary  # 旧装配不产 claim
