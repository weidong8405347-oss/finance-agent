"""行业信息架构与结构化产物验收（audit §3.6/§3.7/§3.8 + §5 验收用例 5/6）。

事故形态：
- 行业 recipe 定义六个模块，后端遍历固定十个股票模块，前端固定 SECTION_ORDER 只做
  四个标题替换——行业页面仍占用财务/预期/估值栏目，「子赛道」实际读公司收入，
  「候选池」从 peers 字段找代码，与已有 player_landscape 不连通；
- `business_graph()` 只拼旧文本，nodes=0、edges=0，页面显示长文本与原始 JSON；
- 首屏直接取最后创建的 validated claim 当总论，最近变化是最后三条 claim 各截
  120 字，反证在「68% vs 传统 3」处截断，行业驱动为空（只认三个股票问题 id）；
- verdict=null 时连 0/9 也隐藏。
"""

from datetime import UTC, date, datetime

import pytest

from finance_agent.decision.store import DecisionStore
from finance_agent.dossier import registry as module_registry
from finance_agent.dossier.projector import DossierProjector, _claim_module, business_graph
from finance_agent.dossier.service import DossierService
from finance_agent.dossier.structures import (
    ComparisonMatrix,
    ComparisonRow,
    StructureError,
    parse_structures,
    validate_structures,
)
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.normalization import normalize_raw
from finance_agent.knowledge.store import BitemporalStore

T0 = datetime(2024, 6, 1, tzinfo=UTC)
T1 = datetime(2025, 1, 31, tzinfo=UTC)
H1_2024 = MetricPeriod(start=date(2024, 1, 1), end=date(2024, 6, 30), frequency="H1",
                       fiscal_label="2024H1")
FY2024 = MetricPeriod(start=date(2024, 1, 1), end=date(2024, 12, 31), frequency="FY",
                      fiscal_label="FY2024")
OBJECTIVE = "究竟哪些公司是真的在形成技术护城河以及有比较大可能能够取得商业爆发"


@pytest.fixture()
def env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    decisions = DecisionStore(tmp_path / "d.db")
    projector = DossierProjector(kb=kb, metrics=metrics, decisions=decisions)
    service = DossierService(kb=kb, metrics=metrics, projector=projector, events=events,
                             decisions=decisions)
    return kb, metrics, events, projector, service


def seed_ev(kb, eid="ev-1", quote="AI for Science 平台收入 1500 million（FY2024）"):
    kb.add_evidence(Evidence(
        evidence_id=eid, source_id="web_search", url="https://x.com/a",
        verbatim_quote=quote, retrieved_at=T0, available_at=T0, pit_grade=PitGrade.B,
    ))


def seed_obs(metrics, kb, *, metric_key="revenue", value_text="1500 million",
             period=FY2024, entity_kind="industry", entity_id="ai-for-science",
             dimensions=None, evidence=("ev-1",)):
    value, steps = normalize_raw(value_text, "USD")
    obs = ReportedObservation(
        entity_kind=entity_kind, entity_id=entity_id, metric_key=metric_key, period=period,
        value=value, unit="USD", currency="USD", dimensions=dimensions or {},
        raw=RawValue(value_text=value_text, unit_text="USD"),
        normalization=[s.model_dump(mode="json") for s in steps],
        evidence_refs=list(evidence), knowledge_time=T1, source_available_at=T0,
        retrieved_at=T1, created_at=T1, pit_grade=PitGrade.B,
    )
    return metrics.assert_observation(obs)[0]


STRUCTURES = {
    "industry_map": {
        "nodes": [
            {"node_id": "compute", "label": "上游算力", "layer": "upstream",
             "bottleneck": True, "evidence_refs": ["ev-1"]},
            {"node_id": "platform", "label": "中游平台", "layer": "midstream",
             "company_refs": ["SDGR", "2228.HK"], "evidence_refs": ["ev-1"]},
            {"node_id": "apps", "label": "下游科研应用", "layer": "downstream"},
        ],
        "edges": [
            {"source": "compute", "target": "platform", "relation": "supplies",
             "flow_known": False, "evidence_refs": ["ev-1"]},
            {"source": "platform", "target": "apps", "relation": "enables"},
        ],
        "layers": ["upstream", "midstream", "downstream"],
        "bottlenecks": ["compute"],
        "routes": [{"route": "物理仿真", "maturity": "中", "companies": "SDGR"}],
    },
    "candidate_assessment": {
        "objective": OBJECTIVE,
        "criteria": ["技术壁垒有一手证据", "商业兑现有披露数字", "可交易"],
        "candidates": [
            {"entity_id": "SDGR", "name": "Schrödinger", "listing_status": "listed",
             "market": "NASDAQ", "security_relation": "同一主体", "tier": "included",
             "technology_stage": "外部验证", "commercial_stage": "复购扩单",
             "moat_evidence": ["ev-1"], "reason": "物理仿真平台 + 药企复购",
             "next_validation": "FY2025 Q1 财报的软件收入增速", "investable": True},
            {"entity_id": " XtalPi", "name": "晶泰科技", "listing_status": "listed",
             "market": "HKEX", "tier": "needs_review", "reason": "首付款与潜在总额未拆分",
             "investable": True},
        ],
        "stage_definitions": {"外部验证": "第三方客户/同行复核", "复购扩单": "已有重复订单"},
    },
    "validation_timeline": {
        "items": [
            {"event": "SDGR FY2025 Q1 财报", "window_start": "2025-02", "window_end": "2025-03",
             "status": "expected", "trigger_condition": "软件收入同比增速 ≥20%",
             "affected_judgment": "商业兑现阶段判定", "company_refs": ["SDGR"]},
        ],
    },
    "executive_summary": {
        "objective": OBJECTIVE,
        "answer": "现有证据只支持 Schrödinger 进入候选（平台 + 复购），晶泰待核实首付款拆分。",
        "tiers": {"included": ["Schrödinger"], "needs_review": ["晶泰科技"]},
        "main_basis": ["平台外部验证", "复购扩单证据"],
        "biggest_disagreement": "软件收入增速是否可持续（68% vs 传统 3%）",
        "limitations": ["未检索到独立第三方对平台精度的复核"],
        "question_progress": "关键问题 0/9 已回答",
        "refs": ["ev-1"],
        "credibility": {"data_cutoff": "2025-01-31"},
    },
}


def seed_artifact(metrics, *, structures=None, status="validated"):
    metrics.save_artifact(artifact_id="art-1", namespace="prod", payload={
        "artifact_id": "art-1", "entity_kind": "industry", "entity_id": "ai-for-science",
        "title": "AI for Science 研究", "status": status, "sufficiency": "partial",
        "purpose": "report", "created_at": T1.isoformat(),
        "structures": structures if structures is not None else STRUCTURES,
        "report_document": {"title": "t", "entity_kind": "industry",
                            "entity_id": "ai-for-science", "blocks": []},
    })


# ---------------- 1. 模块注册表（§3.6） ----------------


class TestModuleRegistry:
    def test_industry_navigation_is_industry_shaped(self):
        nav = [m.module_id for m in module_registry.nav_modules("industry")]
        assert nav == ["investment_snapshot", "industry_chain", "candidate_pool",
                       "key_kpi", "catalysts_risks", "research_sources"]

    def test_stock_only_modules_are_not_applicable_not_missing(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        snap, _ = service.open("industry", "ai-for-science")
        for mod in ("financial_quality", "expectations", "valuation_lab", "peers",
                    "business_engine", "revenue_segments"):
            assert snap["modules"][mod]["status"] == "not_applicable", mod
            assert "不适用" in snap["modules"][mod]["reasons"][0]

    def test_registry_is_projected_for_frontend(self, env):
        """前端按快照里的注册表渲染，不再硬编码 SECTION_ORDER。"""
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        snap, _ = service.open("industry", "ai-for-science")
        reg = snap["module_registry"]
        assert reg["registry_version"] == module_registry.REGISTRY_VERSION
        renderers = {m["module_id"]: m["renderer"] for m in reg["modules"]}
        assert renderers["industry_chain"] == "industry_map"
        assert renderers["candidate_pool"] == "candidate_matrix"
        assert renderers["catalysts_risks"] == "validation_timeline"

    def test_industry_module_accepts_module_id_lookup(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        snap, _ = service.open("industry", "ai-for-science")
        for mod in ("industry_chain", "candidate_pool", "catalysts_risks"):
            payload = service.module(snap["context"]["snapshot_id"], mod)
            assert payload.module == mod

    def test_h1_observations_are_not_dropped(self, env):
        """audit §3.6：财务只读 FY/Q，本次 H1 数据被排除。"""
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        seed_obs(metrics, kb, period=H1_2024, entity_kind="stock", entity_id="BE",
                 value_text="700 million")
        snap, _ = service.open("stock", "BE")
        payload = service.module(snap["context"]["snapshot_id"], "financial_quality")
        h1 = payload.payload["h1"]
        assert h1["series"], "H1 数据被排除在财务模块外"
        assert h1["series"][0]["points"][0]["value"] == "700000000"


class TestGapRefs:
    """audit §4：点击缺口可直接补研相应 question_id——模块必须带未完成问题 id。"""

    def test_open_questions_land_on_their_module(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        metrics.save_plan(plan_id="plan-gap", namespace="prod", payload={
            "plan_id": "plan-gap", "entity_kind": "industry",
            "entity_id": "ai-for-science", "objective": OBJECTIVE, "mode": "deep",
            "recipe_id": "industry", "recipe_version": "1",
            "created_at": T0.isoformat(), "status": "active",
            "questions": [
                {"question_id": "value-chain", "text": "?", "priority": "high",
                 "status": "unanswered", "module": "industry_chain"},
                {"question_id": "candidate-pool", "text": "?", "priority": "high",
                 "status": "unanswered", "module": "candidate_pool"},
                {"question_id": "objective-technology_moat-abc123", "text": "?",
                 "priority": "high", "status": "gathering", "module": "candidate_pool"},
                {"question_id": "demand-supply", "text": "?", "priority": "high",
                 "status": "answered", "module": "key_kpi"},
                {"question_id": "policy", "text": "?", "priority": "medium",
                 "status": "not_applicable", "module": "catalysts_risks"},
            ],
            "budgets": {}, "scope": {},
        })
        snap, _ = service.open("industry", "ai-for-science")
        mods = snap["modules"]
        assert mods["industry_chain"]["gap_refs"] == ["value-chain"]
        # 同一模块的两道未完成题（配方题 + 目标编译题）都带上
        assert set(mods["candidate_pool"]["gap_refs"]) == {
            "candidate-pool", "objective-technology_moat-abc123"}
        # answered / not_applicable 不算缺口（不该让用户去补已完成的题）
        assert mods["key_kpi"]["gap_refs"] == []
        assert mods["catalysts_risks"]["gap_refs"] == []

    def test_no_plan_means_no_gap_refs(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        snap, _ = service.open("industry", "ai-for-science")
        assert all(m["gap_refs"] == [] for m in snap["modules"].values())

    def test_stock_objective_questions_route_to_stock_modules(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        metrics.save_plan(plan_id="plan-s", namespace="prod", payload={
            "plan_id": "plan-s", "entity_kind": "stock", "entity_id": "BE",
            "objective": "护城河有多深", "mode": "deep", "recipe_id": "general",
            "recipe_version": "1", "created_at": T0.isoformat(), "status": "active",
            "questions": [
                {"question_id": "objective-technology_moat-xyz", "text": "?",
                 "priority": "high", "status": "unanswered", "module": "business_engine"},
                {"question_id": "objective-counter_evidence-xyz", "text": "?",
                 "priority": "high", "status": "unanswered", "module": "risks"},
            ],
            "budgets": {}, "scope": {},
        })
        snap, _ = service.open("stock", "BE")
        assert snap["modules"]["business_engine"]["gap_refs"] == ["objective-technology_moat-xyz"]
        assert snap["modules"]["catalysts_risks"]["gap_refs"] == ["objective-counter_evidence-xyz"]


class TestClaimModuleRouting:
    def test_module_name_misused_as_question_id_still_routes(self):
        """audit §3.6：三条 claim 误用模块名 key_kpi 当 question_id。"""
        assert _claim_module("industry", {"question_id": "key_kpi"}, None) == "key_kpi"
        assert _claim_module("stock", {"question_id": "financial_quality"}, None) \
            == "financial_quality"

    def test_objective_questions_route_by_entity_kind(self):
        qid = "objective-technology_moat-abc123"
        assert _claim_module("industry", {"question_id": qid}, None) == "candidate_pool"
        assert _claim_module("stock", {"question_id": qid}, None) == "business_engine"
        assert _claim_module("industry", {"question_id": "objective-counter_evidence-x"}, None) \
            == "catalysts_risks"

    def test_recipe_questions_map_to_industry_modules(self):
        assert _claim_module("industry", {"question_id": "value-chain"}, None) == "industry_chain"
        assert _claim_module("industry", {"question_id": "candidate-pool"}, None) \
            == "candidate_pool"
        assert _claim_module("industry", {"question_id": "demand-supply"}, None) == "key_kpi"


# ---------------- 2. 结构产物校验（§3.7） ----------------


class TestStructureValidation:
    def test_valid_structures_pass(self):
        parsed = parse_structures(STRUCTURES)
        assert validate_structures(parsed) == []

    def test_unknown_kind_rejected(self):
        with pytest.raises(StructureError, match="未知结构产物"):
            parse_structures({"sankey_chart": {"nodes": []}})

    def test_dangling_edge_rejected(self):
        broken = {"industry_map": {"nodes": [{"node_id": "a", "label": "A"}],
                                   "edges": [{"source": "a", "target": "ghost"}],
                                   "layers": ["upstream"]}}
        issues = validate_structures(parse_structures(broken))
        assert any("target 未定义" in i for i in issues)

    def test_fabricated_flow_rejected(self):
        """无流量数据不许编造 Sankey 宽度。"""
        bad = {"industry_map": {
            "nodes": [{"node_id": "a", "label": "A"}, {"node_id": "b", "label": "B"}],
            "edges": [{"source": "a", "target": "b", "flow_known": False,
                       "flow_value": "obs-1"}],
            "layers": ["upstream", "downstream"],
        }}
        assert any("flow_known" in i for i in validate_structures(parse_structures(bad)))

    def test_candidate_rules(self):
        bad = {"candidate_assessment": {"candidates": [
            {"entity_id": "X", "tier": "excluded"},                      # 淘汰无原因
            {"entity_id": "Y", "listing_status": "listed"},               # 上市无市场
            {"entity_id": "Z", "listing_status": "private", "investable": True},
            {"entity_id": "X", "tier": "included", "reason": "重复"},      # 公司重复
        ]}}
        issues = validate_structures(parse_structures(bad))
        assert any("淘汰必须给原因" in i for i in issues)
        assert any("上市必须给市场" in i for i in issues)
        assert any("不得标为可交易候选" in i for i in issues)
        assert any("公司重复" in i for i in issues)

    def test_comparison_matrix_chartable_requires_comparability(self):
        bad = {"comparison_matrix": {
            "columns": [{"id": "c1", "label": "收入"}],
            "rows": [{"label": "SDGR", "cells": {"c1": "100"}, "comparable": False}],
            "chartable": True,
        }}
        # 归一层：存在不可比行时 chartable 确定性降级 false（保表不画图，不整批拒）
        parsed = parse_structures(bad)
        assert parsed["comparison_matrix"].chartable is False
        issues = validate_structures(parsed)
        assert any("不可比却未给原因" in i for i in issues)
        assert not any("不得标 chartable" in i for i in issues)  # 已降级，不再硬画
        # 验证层自身仍拦截绕过归一的直接构造（双层防御）
        direct = ComparisonMatrix(
            columns=[{"id": "c1", "label": "收入"}],
            rows=[ComparisonRow(label="SDGR", cells={"c1": "100"}, comparable=False,
                                incomparable_reason="口径不同")],
            chartable=True,
        )
        assert any("不得标 chartable" in i
                   for i in validate_structures({"comparison_matrix": direct}))

    def test_timeline_expected_needs_window_and_trigger(self):
        bad = {"validation_timeline": {"items": [{"event": "财报", "status": "expected"}]}}
        issues = validate_structures(parse_structures(bad))
        assert any("缺时间范围" in i for i in issues)
        assert any("缺触发条件" in i for i in issues)

    def test_unresolvable_reference_reported(self):
        parsed = parse_structures(STRUCTURES)
        issues = validate_structures(parsed, resolvable=lambda ref: ref != "ev-1")
        assert any("引用不可解析 ev-1" in i for i in issues)

    def test_executive_summary_requires_answer(self):
        issues = validate_structures(parse_structures({"executive_summary": {"objective": "x"}}))
        assert any("必须给出回答用户目标的结论" in i for i in issues)


class TestBusinessGraphProjection:
    def test_nodes_and_edges_come_from_structure(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        kb.assert_fact(Fact(
            entity_kind="industry", entity_id="ai-for-science", field="value_chain",
            value="上游算力 → 中游平台 → 下游科研应用", knowledge_time=T0,
            evidence_ids=["ev-1"],
        ))
        graph = business_graph(kb.view("industry", "ai-for-science", T1), [],
                               entity_kind="industry", structures=STRUCTURES)
        assert len(graph.nodes) == 3 and len(graph.edges) == 2
        assert graph.layers == ["upstream", "midstream", "downstream"]
        assert any(n.bottleneck for n in graph.nodes)
        assert graph.nodes[1].company_refs == ["SDGR", "2228.HK"]
        assert graph.edges[0].flow_known is False  # 等宽边，不编造流量
        assert "上游算力" in graph.narrative  # 旧文本仍在（兼容区）

    def test_module_payload_exposes_graph_structurally(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        seed_obs(metrics, kb)  # 有 typed 观测支撑 → ready（只有结构无观测则 partial）
        seed_artifact(metrics)
        snap, _ = service.open("industry", "ai-for-science")
        payload = service.module(snap["context"]["snapshot_id"], "industry_chain")
        assert len(payload.payload["nodes"]) == 3
        assert payload.payload["edges"][0]["relation"] == "supplies"
        assert payload.payload["bottlenecks"] == ["compute"]
        assert payload.status == "ready"

    def test_candidate_pool_payload_has_companies_and_reasons(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        seed_artifact(metrics)
        snap, _ = service.open("industry", "ai-for-science")
        payload = service.module(snap["context"]["snapshot_id"], "candidate_pool")
        cands = payload.payload["candidates"]
        assert len(cands) == 2
        assert cands[0]["entity_id"] == "SDGR" and cands[0]["reason"]
        assert payload.payload["criteria"]
        assert payload.payload["stage_definitions"]
        # 旧 player_landscape 仍可读（连通，不是替换）
        assert "legacy" in payload.payload

    def test_candidate_pool_without_structure_is_honest(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        snap, _ = service.open("industry", "ai-for-science")
        payload = service.module(snap["context"]["snapshot_id"], "candidate_pool")
        assert payload.payload["candidates"] == []
        assert any("尚无结构化候选评估" in n for n in payload.payload["notes"])

    def test_timeline_reaches_catalysts_module(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        seed_artifact(metrics)
        snap, _ = service.open("industry", "ai-for-science")
        payload = service.module(snap["context"]["snapshot_id"], "catalysts_risks")
        items = payload.payload["items"]
        assert items and items[0]["trigger_condition"]
        assert items[0]["status"] == "expected"


# ---------------- 3. 首屏摘要与可信度（§3.8） ----------------


class TestExecutiveSummary:
    def test_thesis_answers_objective_not_last_claim(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        seed_artifact(metrics)
        # 一条更晚创建的 validated claim（旧实现会把它当总论）
        metrics.save_claim(claim_id="claim-late", namespace="prod", payload={
            "claim_id": "claim-late", "entity_kind": "industry",
            "entity_id": "ai-for-science", "kind": "inference", "status": "validated",
            "statement": "某条与目标无关的晚期论断", "created_at": T1.isoformat(),
            "support_refs": ["ev-1"], "counter_refs": [],
        })
        snap, _ = service.open("industry", "ai-for-science")
        summary = snap["summary"]
        assert summary["thesis"].startswith("现有证据只支持 Schrödinger")
        assert summary["objective"] == OBJECTIVE or summary["objective"] == ""
        assert summary["tiers"]["included"] == ["Schrödinger"]
        assert "68% vs 传统 3%" in summary["biggest_disagreement"]

    def test_disagreement_and_limitations_not_truncated(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        long_text = "反证：" + "证据削弱优势的具体说明。" * 30
        structures = dict(STRUCTURES)
        structures["executive_summary"] = {
            **STRUCTURES["executive_summary"],
            "biggest_disagreement": long_text,
            "limitations": ["限制：" + "未披露项说明。" * 30],
        }
        seed_artifact(metrics, structures=structures)
        snap, _ = service.open("industry", "ai-for-science")
        assert snap["summary"]["biggest_disagreement"] == long_text
        assert snap["summary"]["limitations"][0].endswith("未披露项说明。")

    def test_credibility_is_split_not_overclaimed(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        seed_artifact(metrics)
        seed_obs(metrics, kb)
        snap, _ = service.open("industry", "ai-for-science")
        cred = snap["summary"]["credibility"]
        assert set(cred) >= {"refs_resolvable", "facts_checked", "analysis_reviewed",
                             "sufficiency"}
        assert "基础校验" in cred["refs_resolvable"]
        assert "不表示证据充分支持整句话" in cred["analysis_reviewed"]
        assert cred["sufficiency"] == "partial"
        # 结构产物可以补充可信度条目（合并，不覆盖服务端算出的四项）
        assert cred["data_cutoff"] == "2025-01-31"

    def test_question_progress_visible_even_at_zero(self, env):
        """verdict=null / 0 覆盖时也必须显示问题进展（audit §3.8）。"""
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        metrics.save_plan(plan_id="plan-1", namespace="prod", payload={
            "plan_id": "plan-1", "entity_kind": "industry", "entity_id": "ai-for-science",
            "objective": OBJECTIVE, "mode": "deep", "recipe_id": "industry",
            "recipe_version": "1", "created_at": T0.isoformat(), "status": "active",
            "questions": [
                {"question_id": f"q{i}", "text": "?", "priority": "high",
                 "status": "unanswered", "module": "candidate_pool"} for i in range(9)
            ],
            "budgets": {}, "scope": {},
        })
        snap, _ = service.open("industry", "ai-for-science")
        assert snap["summary"]["question_progress"] == "关键问题 0/9 已回答（全部问题 0/9）；研究进行中"
        assert snap["research"]["answered"] == 0 and snap["research"]["required"] == 9
        assert snap["summary"]["objective"] == OBJECTIVE

    def test_key_changes_are_not_truncated_duplicates(self, env):
        """最近变化不再拿最后三条 claim 各截 120 字冒充 diff。"""
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        metrics.save_plan(plan_id="plan-1", namespace="prod", payload={
            "plan_id": "plan-1", "entity_kind": "industry", "entity_id": "ai-for-science",
            "objective": OBJECTIVE, "mode": "deep", "recipe_id": "industry",
            "recipe_version": "1", "created_at": T0.isoformat(), "status": "active",
            "questions": [
                {"question_id": "value-chain", "text": "?", "priority": "high",
                 "status": "answered", "module": "industry_chain",
                 "conclusion": "产业链分三层，利润集中在中游平台。"},
                {"question_id": "bottleneck", "text": "?", "priority": "high",
                 "status": "unavailable", "module": "industry_chain",
                 "conclusion": "瓶颈环节暂无公开数据。",
                 "unresolved": ["缺供需数据"], "attempts": ["检索行业报告未果"]},
            ],
            "budgets": {}, "scope": {},
        })
        snap, _ = service.open("industry", "ai-for-science")
        changes = snap["summary"]["key_changes"]
        assert any("利润集中在中游平台" in c for c in changes)
        assert all(len(c) > 20 for c in changes)  # 完整句，不是 120 字硬截
        assert any("缺供需数据" in x for x in snap["summary"]["limitations"])


# ---------------- 内容质量升级（profile 可视化与内容升级 §1-§3） ----------------


class TestKpiPayloadRouting:
    """事故：key_kpi 状态用注册表路由判 ready，payload 却只查配方键 → 永远空图。"""

    def test_payload_includes_routed_non_recipe_keys(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        # rd_spend 不在行业配方 kpis（market_size/growth_rate/capacity_supply）里，
        # 但注册表/兜底路由把它归到 key_kpi —— payload 必须与状态同口径
        seed_obs(metrics, kb, metric_key="rd_spend", value_text="159.1 billion",
                 dimensions={"scope": "Top 16 pharmaceutical companies"})
        seed_artifact(metrics)
        snap, _ = service.open("industry", "ai-for-science")
        # 状态 partial（必需配方 KPI 缺失如实标注），但 payload 必须包含已路由观测：
        # 旧 bug 是状态与 payload 口径不一致（ready/partial 但永远空图）
        assert snap["modules"]["key_kpi"]["status"] in ("ready", "partial")
        payload = service.module(snap["context"]["snapshot_id"], "key_kpi")
        keys = {s["metric_key"] for s in payload.payload["series_set"]["series"]}
        assert "rd_spend" in keys  # 路由到本模块的观测不得被 payload 丢掉
        assert any("配方外已登记指标" in n for n in payload.payload["series_set"]["notes"])
        # 配方必需键仍如实报缺口（不冒充）
        assert any("KPI 缺口" in n for n in payload.payload["series_set"]["notes"])

    def test_dimension_observation_not_dropped(self, env):
        """带 scope 维度的观测不再被 `not o.dimensions` 过滤掉（独立序列+维度标注）。"""
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        seed_obs(metrics, kb, metric_key="rd_spend", value_text="159.1 billion",
                 dimensions={"scope": "Top 16"})
        seed_artifact(metrics)
        snap, _ = service.open("industry", "ai-for-science")
        payload = service.module(snap["context"]["snapshot_id"], "key_kpi")
        series = [s for s in payload.payload["series_set"]["series"]
                  if s["metric_key"] == "rd_spend"]
        assert series and series[0]["dimensions"] == {"scope": "Top 16"}
        assert "Top 16" in series[0]["label"]  # 维度进标签（不冒充行业总量）


class TestKeyMetricFallback:
    """首屏指标条：配方 KPI 全缺但有 typed 观测时，回退填充真实数字（不空屏）。"""

    def test_fallback_fills_from_observations(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        seed_obs(metrics, kb, metric_key="rd_spend", value_text="159.1 billion",
                 dimensions={"scope": "Top 16"})
        seed_obs(metrics, kb, metric_key="clinical_phase1_success_rate",
                 value_text="80-90%", period=FY2024)
        snap, _ = service.open("industry", "ai-for-science")
        kms = snap["summary"]["key_metrics"]
        filled = [m for m in kms if m["status"] == "ok"]
        assert {m["metric_key"] for m in filled} >= {"rd_spend"}
        assert any("配方外" in m["as_of_note"] for m in filled)
        # 缺口卡仍在（配方必需键不冒充已答）
        assert any(m["status"] == "missing" for m in kms)

    def test_company_financial_keys_excluded_for_industry(self, env):
        """公司级财务键（revenue 等，无 scope 标注）不得冒充行业 KPI 上首屏。"""
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        seed_obs(metrics, kb, metric_key="revenue", value_text="106303")
        snap, _ = service.open("industry", "ai-for-science")
        filled = [m for m in snap["summary"]["key_metrics"] if m["status"] == "ok"]
        assert "revenue" not in {m["metric_key"] for m in filled}

    def test_raw_text_anchor_flows_to_card(self, env):
        """披露原文锚点进首屏卡：unit=ratio 的百分数（85 → 8500% bug）由前端按原文显示。"""
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        seed_obs(metrics, kb, metric_key="pharma_ai_adoption_share", value_text="85%")
        snap, _ = service.open("industry", "ai-for-science")
        m = next(m for m in snap["summary"]["key_metrics"]
                 if m["metric_key"] == "pharma_ai_adoption_share")
        assert m["raw_text"] == "85%"
        assert m["value"] == "85" and m["unit"] == "USD"  # 十进制值原样（前端负责显示语义）


class TestTearSheetProjection:
    """tear-sheet 首屏字段（升级方案 §5/§26）：结构产物 → summary 投影。"""

    TEAR_STRUCTURES = {
        "executive_summary": {
            "objective": OBJECTIVE, "answer": "平台层最先兑现",
            "stage": "商业兑现早期", "why_now": ["大药企采纳加速", "AI 渗透率仍低"],
            "value_capture": "平台与数据基础设施层捕获 55-65%",
            "thesis_breakers": ["首个 AI 药物 III 期失败", "里程碑收入不兑现"],
            "tiers": {"included": ["晶泰"]}, "refs": ["ev-1"],
        },
        "industry_map": {
            "nodes": [{"node_id": "a", "label": "A", "layer": "upstream"}],
            "edges": [], "layers": ["upstream"],
            "bottlenecks": ["临床验证端", "数据墙"],
        },
    }

    def test_tear_sheet_fields_reach_summary(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        seed_artifact(metrics, structures=self.TEAR_STRUCTURES)
        snap, _ = service.open("industry", "ai-for-science")
        s = snap["summary"]
        assert s["stage"] == "商业兑现早期"
        assert s["why_now"] == ["大药企采纳加速", "AI 渗透率仍低"]
        assert "55-65%" in s["value_capture"]
        assert s["thesis_breakers"] == ["首个 AI 药物 III 期失败", "里程碑收入不兑现"]
        assert s["bottlenecks"] == ["临床验证端", "数据墙"]

    def test_absent_tear_sheet_fields_degrade_empty(self, env):
        """旧产物无 tear-sheet 字段 → 空值降级（不编造、前端不渲染空块）。"""
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        seed_artifact(metrics)  # STRUCTURES 无 stage/why_now
        snap, _ = service.open("industry", "ai-for-science")
        s = snap["summary"]
        assert s["stage"] == "" and s["why_now"] == [] and s["thesis_breakers"] == []


class TestIndustryChainPayload:
    def test_layer_labels_and_value_flow_note_pass_through(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        structures = dict(STRUCTURES)
        structures["industry_map"] = {
            **STRUCTURES["industry_map"],
            "layer_labels": {"upstream": "上游算力与基础设施"},
            "value_flow_note": "平台层捕获 55-65%（ev-1）",
        }
        seed_artifact(metrics, structures=structures)
        snap, _ = service.open("industry", "ai-for-science")
        payload = service.module(snap["context"]["snapshot_id"], "industry_chain")
        assert payload.payload["layer_labels"] == {"upstream": "上游算力与基础设施"}
        assert "55-65%" in payload.payload["value_flow_note"]


class TestComparisonNumerics:
    """对照矩阵的可绘图数值：服务端从冻结观测解析（前端不从展示字符串猜数）。"""

    def test_numeric_cells_resolved_from_frozen_observations(self, env):
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        oid = seed_obs(metrics, kb, metric_key="revenue", value_text="1500 million")
        structures = dict(STRUCTURES)
        structures["comparison_matrix"] = {
            "title": "同口径收入对照",
            "columns": [{"id": "rev", "label": "收入", "period": "FY2024", "unit": "USD"}],
            "rows": [{"label": "SDGR", "cells": {"rev": "1,500"},
                      "observation_ids": {"rev": oid}, "comparable": True}],
            "chartable": True,
        }
        seed_artifact(metrics, structures=structures)
        snap, _ = service.open("industry", "ai-for-science")
        payload = service.module(snap["context"]["snapshot_id"], "candidate_pool")
        numerics = payload.payload["comparison_numerics"]
        assert numerics[0]["label"] == "SDGR"
        cell = numerics[0]["cells"]["rev"]
        assert cell["value"] == "1500000000"  # 十进制值来自 typed 观测，非 "1,500" 文本
        assert cell["observation_id"] == oid and cell["unit"] == "USD"

    def test_unresolvable_ref_yields_no_numeric_cell(self, env):
        """引用不在冻结集 → 不出数值（不猜、不查库外数据）。"""
        kb, metrics, events, projector, service = env
        seed_ev(kb)
        structures = dict(STRUCTURES)
        structures["comparison_matrix"] = {
            "columns": [{"id": "rev", "label": "收入"}],
            "rows": [{"label": "X", "cells": {"rev": "100"},
                      "observation_ids": {"rev": "obs-ghost"}, "comparable": True}],
        }
        seed_artifact(metrics, structures=structures)
        snap, _ = service.open("industry", "ai-for-science")
        payload = service.module(snap["context"]["snapshot_id"], "candidate_pool")
        assert payload.payload["comparison_numerics"][0]["cells"] == {}
