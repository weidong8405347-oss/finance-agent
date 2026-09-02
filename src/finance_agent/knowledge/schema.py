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
        # P3 扩展（research-capability-upgrade §4.6）：先 optional 观察证据可得性，
        # 跑 2-3 个真实调研后再决定是否晋升 required
        "future_space": FieldPolicy(max_age_days=180),  # 未来空间（TAM/赛道增速事实）
        "market_share": FieldPolicy(max_age_days=400),  # 现有市场份额
        "talent_density": FieldPolicy(max_age_days=None),  # 人才密度
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
    optional={
        # P3 扩展：F1 赛道地图 / F2 标的池的落点
        "sub_sectors": FieldPolicy(max_age_days=None),  # 子赛道拆解（赛道地图）
        "player_landscape": FieldPolicy(max_age_days=180),  # 标的池（含证据绑定）
    },
)

SCHEMAS: dict[str, ProfileSchema] = {s.entity_kind: s for s in (STOCK_SCHEMA, INDUSTRY_SCHEMA)}
