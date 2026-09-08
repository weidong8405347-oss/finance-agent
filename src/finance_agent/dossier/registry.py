"""模块注册表：问题 → 指标/产物 → 模块 → payload schema → renderer → applicability。

audit §3.6 事故：行业 recipe 定义六个模块，后端却遍历固定十个股票模块，前端也有
固定 SECTION_ORDER 只做四个标题替换——行业页面仍占用「财务/预期/估值」栏目，
「子赛道」实际读公司收入，「候选池」从 peers 字段找代码，与已有 player_landscape
不连通。

本注册表是**同一个版本化真相源**：后端按它决定模块清单、标题、适用性与 payload
形状，前端按快照里的 `modules` 顺序渲染（不再硬编码 SECTION_ORDER）。
不适用项标 `not_applicable` 并从默认导航隐藏——不当作研究缺失。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

REGISTRY_VERSION = "2"

Applicability = Literal["always", "stock_only", "industry_only", "data_dependent"]


@dataclass(frozen=True)
class ModuleSpec:
    """一个档案模块的契约（前后端共用）。"""

    module_id: str
    title: str
    #: 渲染器标识（前端据此选组件；未知 renderer 必须降级为键值表，不许倒 JSON）
    renderer: str
    applicability: Applicability = "always"
    #: 该模块消费的指标键（typed 观测 → payload）
    metric_keys: tuple[str, ...] = ()
    #: 该模块消费的旧字段（legacy 区，带 needs_normalization）
    legacy_fields: tuple[str, ...] = ()
    #: 该模块消费的结构产物（§3.7）
    structures: tuple[str, ...] = ()
    #: 关联的问题 id 前缀/模块名（claim → 模块归属）
    question_modules: tuple[str, ...] = ()
    #: 默认导航是否展示（not_applicable 模块不占位）
    default_nav: bool = True
    #: payload 里必须出现的键（前端契约；投影层保证）
    payload_keys: tuple[str, ...] = ()
    notes: str = ""


#: 股票实体模块（十模块，§4.4）
STOCK_MODULES: tuple[ModuleSpec, ...] = (
    ModuleSpec("investment_snapshot", "研究结论", "executive_summary",
               structures=("executive_summary",), question_modules=("objective",)),
    ModuleSpec("business_engine", "商业引擎", "narrative_with_evidence",
               legacy_fields=("business_model", "future_space"),
               question_modules=("business_engine",)),
    ModuleSpec("revenue_segments", "收入与分部", "segment_table",
               question_modules=("revenue_segments", "financials"),
               notes="带 segment 维度的收入观测归本模块；总量收入归 financial_quality"),
    ModuleSpec("key_kpi", "关键 KPI", "kpi_cards",
               metric_keys=("revenue", "gross_margin", "cfo", "capex", "net_debt",
                            "orders", "firm_backlog", "capacity", "deliveries",
                            "pipeline", "cash_runway", "quarterly_burn", "r_and_d"),
               question_modules=("key_kpi", "financial_quality")),
    ModuleSpec("financial_quality", "财务质量", "financial_statements",
               metric_keys=("revenue", "net_income", "cfo", "capex", "fcf", "ebitda",
                            "gross_profit", "operating_income", "cash_and_equivalents"),
               legacy_fields=("revenue_fy", "net_income_fy", "cash_flow"),
               question_modules=("financial_quality",)),
    ModuleSpec("expectations", "预期差", "expectation_table",
               metric_keys=("share_dilution",), question_modules=("expectations",)),
    ModuleSpec("valuation_lab", "估值实验", "valuation_lab",
               metric_keys=("market_cap", "enterprise_value", "share_price"),
               legacy_fields=("valuation",), question_modules=("valuation", "valuation_lab")),
    ModuleSpec("peers", "同业与竞争", "peer_table",
               metric_keys=("market_share",),
               legacy_fields=("peers", "moat", "market_share", "management", "talent_density"),
               question_modules=("peers", "management", "moat")),
    ModuleSpec("catalysts_risks", "催化与风险", "claim_list",
               legacy_fields=("risks", "catalysts", "counter_evidence"),
               structures=("validation_timeline",),
               question_modules=("risks", "catalysts", "catalysts_risks", "counter_evidence")),
    ModuleSpec("research_sources", "研究与来源", "sources_and_progress",
               structures=(), question_modules=("research_sources",)),
)

#: 行业实体模块（audit §4：先回答「哪些公司、依据是什么、还差哪一步验证」）
INDUSTRY_MODULES: tuple[ModuleSpec, ...] = (
    ModuleSpec("investment_snapshot", "研究结论", "executive_summary",
               structures=("executive_summary", "candidate_assessment"),
               question_modules=("objective",)),
    ModuleSpec("industry_chain", "产业链与技术路线", "industry_map",
               structures=("industry_map",),
               legacy_fields=("value_chain", "sub_sectors"),
               question_modules=("industry_chain", "investment_snapshot"),
               payload_keys=("nodes", "edges", "layers")),
    ModuleSpec("candidate_pool", "公司与护城河", "candidate_matrix",
               structures=("candidate_assessment", "comparison_matrix"),
               legacy_fields=("player_landscape", "competition"),
               question_modules=("candidate_pool",),
               payload_keys=("candidates", "criteria")),
    ModuleSpec("key_kpi", "商业兑现", "kpi_cards",
               metric_keys=("market_size", "growth_rate", "capacity_supply", "revenue"),
               legacy_fields=("market_size", "growth_rate"),
               structures=("comparison_matrix",),
               question_modules=("key_kpi", "market"),
               payload_keys=("metrics",)),
    ModuleSpec("catalysts_risks", "催化与证伪", "validation_timeline",
               structures=("validation_timeline",),
               legacy_fields=("policy",),
               question_modules=("catalysts_risks", "policy"),
               payload_keys=("items",)),
    ModuleSpec("research_sources", "研究过程与来源", "sources_and_progress",
               question_modules=("research_sources",)),
    # 股票专属模块：行业实体标 not_applicable，不占默认导航（不当作研究缺失）
    ModuleSpec("financial_quality", "财务质量（个股口径）", "financial_statements",
               applicability="stock_only", default_nav=False),
    ModuleSpec("expectations", "预期差（个股口径）", "expectation_table",
               applicability="stock_only", default_nav=False),
    ModuleSpec("valuation_lab", "估值实验（个股口径）", "valuation_lab",
               applicability="stock_only", default_nav=False),
    ModuleSpec("business_engine", "商业引擎（个股口径）", "narrative_with_evidence",
               applicability="stock_only", default_nav=False),
    ModuleSpec("revenue_segments", "收入与分部（个股口径）", "segment_table",
               applicability="stock_only", default_nav=False),
    ModuleSpec("peers", "同业与竞争（个股口径）", "peer_table",
               applicability="stock_only", default_nav=False),
)

REGISTRY: dict[str, tuple[ModuleSpec, ...]] = {
    "stock": STOCK_MODULES,
    "industry": INDUSTRY_MODULES,
}


def modules_for(entity_kind: str) -> tuple[ModuleSpec, ...]:
    """该实体类型的模块清单（含 not_applicable 项，顺序即默认导航顺序）。"""
    return REGISTRY.get(entity_kind, STOCK_MODULES)


def nav_modules(entity_kind: str) -> list[ModuleSpec]:
    """默认导航展示的模块（not_applicable 不占位）。"""
    return [m for m in modules_for(entity_kind) if m.default_nav and _applicable(m, entity_kind)]


def spec_of(entity_kind: str, module_id: str) -> ModuleSpec | None:
    return next((m for m in modules_for(entity_kind) if m.module_id == module_id), None)


def _applicable(spec: ModuleSpec, entity_kind: str) -> bool:
    if spec.applicability == "always":
        return True
    if spec.applicability == "stock_only":
        return entity_kind == "stock"
    if spec.applicability == "industry_only":
        return entity_kind == "industry"
    return True


def titles_for(entity_kind: str) -> dict[str, str]:
    return {m.module_id: m.title for m in modules_for(entity_kind)}


def module_of_question(entity_kind: str, question_module: str) -> str | None:
    """问题 module → 档案模块（claim/问题进度归位；未登记 → None，不硬塞）。"""
    if not question_module:
        return None
    for spec in modules_for(entity_kind):
        if question_module in spec.question_modules or question_module == spec.module_id:
            return spec.module_id
    return None


def module_of_metric(entity_kind: str, metric_key: str) -> str | None:
    """指标键 → 档案模块（KPI/图表归位）。"""
    for spec in modules_for(entity_kind):
        if metric_key in spec.metric_keys:
            return spec.module_id
    return None


def as_payload(entity_kind: str) -> dict[str, Any]:
    """注册表投影（进快照 `module_registry`，前端据此渲染，不再硬编码顺序）。"""
    return {
        "registry_version": REGISTRY_VERSION,
        "entity_kind": entity_kind,
        "modules": [
            {
                "module_id": m.module_id, "title": m.title, "renderer": m.renderer,
                "applicability": m.applicability, "default_nav": m.default_nav,
                "metric_keys": list(m.metric_keys), "legacy_fields": list(m.legacy_fields),
                "structures": list(m.structures), "payload_keys": list(m.payload_keys),
                "notes": m.notes,
            }
            for m in modules_for(entity_kind)
        ],
    }


__all__ = [
    "ModuleSpec", "REGISTRY", "REGISTRY_VERSION", "STOCK_MODULES", "INDUSTRY_MODULES",
    "modules_for", "nav_modules", "spec_of", "titles_for", "module_of_question",
    "module_of_metric", "as_payload",
]
