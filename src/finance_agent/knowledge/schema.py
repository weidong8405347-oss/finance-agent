"""实体档案 schema：gap 分析的对照基准（DESIGN.md §5.1 gap 分析）。

每个必填字段带新鲜度策略（max_age_days=None 表示不过期）。
完整度口径：fresh 计 1，stale 计 0.5，missing 计 0。
"""

from __future__ import annotations

from pydantic import BaseModel


class FieldPolicy(BaseModel):
    max_age_days: int | None = None


class ProfileSchema(BaseModel):
    entity_kind: str
    required: dict[str, FieldPolicy]
    # 可选维度：不计入完整度，但作为研究 brief 的引导清单（R6/Q3 schema 扩展）
    optional: dict[str, FieldPolicy] = {}


STOCK_SCHEMA = ProfileSchema(
    entity_kind="stock",
    required={
        "revenue_fy": FieldPolicy(max_age_days=400),  # 年报维度，一年+缓冲
        "net_income_fy": FieldPolicy(max_age_days=400),
        "cash_flow": FieldPolicy(max_age_days=400),
        "valuation": FieldPolicy(max_age_days=180),
        "business_model": FieldPolicy(max_age_days=None),
        "moat": FieldPolicy(max_age_days=None),
        "risks": FieldPolicy(max_age_days=180),
        "peers": FieldPolicy(max_age_days=400),
    },
    optional={
        "management": FieldPolicy(max_age_days=None),
        "catalysts": FieldPolicy(max_age_days=180),
        "counter_evidence": FieldPolicy(max_age_days=180),  # 反方证据采集义务（DESIGN.md §5.1）
    },
)

INDUSTRY_SCHEMA = ProfileSchema(
    entity_kind="industry",
    required={
        "market_size": FieldPolicy(max_age_days=400),
        "growth_rate": FieldPolicy(max_age_days=180),
        "value_chain": FieldPolicy(max_age_days=None),
        "competition": FieldPolicy(max_age_days=None),
        "policy": FieldPolicy(max_age_days=180),
    },
)

SCHEMAS: dict[str, ProfileSchema] = {s.entity_kind: s for s in (STOCK_SCHEMA, INDUSTRY_SCHEMA)}
