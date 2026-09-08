"""把 DataGateway 包装成 agent tool：工具结果自动携带 provenance。

provenance 是 leakage-audit hook 的审计锚点（DESIGN.md §4.3 防线 3 的机械部分）。
"""

from __future__ import annotations

import json
from typing import Any

from .gateway import DataGateway


def make_gateway_tool(gateway: DataGateway, source_id: str, chunk_store=None):
    """生成一个工具函数：query(source 固定, request=arguments) → ToolResult dict。

    传入 chunk_store 时，每条记录同时落检索台账（EvidenceDesk）：返回项带 chunk_id，
    供 register_evidence 引用——「模型可见的记录才可引为证据」由此闭环。
    """

    def tool(arguments: dict[str, Any]) -> dict[str, Any]:
        records = gateway.query(source_id, arguments)
        items = []
        for r in records:
            item = {**r.payload, "url": r.url, "available_at": r.available_at.isoformat()
                    if r.available_at else None}
            if chunk_store is not None:
                from ..knowledge.models import PitGrade

                # chunk 文本必须与模型所见逐项一致（子串校验的基准）
                item_text = json.dumps(item, ensure_ascii=False, default=str)
                # 逐条有效等级（2026-09-01 实测修复）：源级 B 但本条无 available_at
                # → 本条降级 C（Evidence 校验：A/B 级必须有时刻；不给就拒登记）
                grade = gateway_grade(gateway, r.source_id)
                effective = grade if r.available_at is not None else "C"
                item["chunk_id"] = chunk_store.add(
                    source_id=r.source_id,
                    text=item_text,
                    url=r.url,
                    available_at=r.available_at,
                    pit_grade=PitGrade(effective),
                )
            items.append(item)
        return {
            "content": json.dumps(items, ensure_ascii=False, default=str),
            "provenance": [
                {
                    "source_id": r.source_id,
                    "available_at": r.available_at.isoformat() if r.available_at else None,
                    "pit_grade": gateway_grade(gateway, r.source_id),
                }
                for r in records
            ],
        }

    return tool


def gateway_grade(gateway: DataGateway, source_id: str) -> str:
    adapter = gateway._adapters.get(source_id)  # noqa: SLF001 - 内部协作函数
    return adapter.capability().pit_grade.value if adapter else "C"


#: 网关工具 schema（供 LLMRouter 绑定；按数据源 source_id 逐个生成）
GATEWAY_TOOL_SCHEMAS: dict[str, dict] = {
    "query_edgar": {
        "name": "query_edgar",
        "description": "查询 SEC EDGAR 披露（filingDate 为 PIT 可知时刻）",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "forms": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ticker"],
        },
    },
    "query_prices": {
        "name": "query_prices",
        "description": "查询日线行情（available_at = 交易日 +1d）",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
            "required": ["ticker"],
        },
    },
    "query_prices_stooq": {
        "name": "query_prices_stooq",
        "description": "查询日线行情（Stooq 源，零依赖；available_at = 交易日 +1d）",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
            "required": ["ticker"],
        },
    },
    "query_web_search": {
        "name": "query_web_search",
        "description": (
            "web 语义搜索（Exa，默认经 Novita 网关；B 级：publishedDate 为可知时刻，"
            "无日期的条目无 PIT 保证）。"
            "护城河/管理层/市场份额/行业空间等定性维度的主要证据源"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词（中英文皆可，尽量具体）"},
                "num_results": {"type": "integer", "description": "条数（默认 8，上限 25）"},
            },
            "required": ["query"],
        },
    },
    "query_web_search_tavily": {
        "name": "query_web_search_tavily",
        "description": (
            "web 关键词搜索（Tavily，C 级：无逐条发布时间保证，评估模式不可用）。"
            "与 query_web_search（Exa）互为补充/并集，提高召回"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "description": "默认 8，上限 20"},
            },
            "required": ["query"],
        },
    },
    "query_fundamentals": {
        "name": "query_fundamentals",
        "description": (
            "查询美股基本面快照（市值/股本/TTM 财务，C 级：当前值无历史 PIT，"
            "评估模式不可用；严肃口径以 EDGAR 披露原文为准）"
        ),
        "parameters": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    "query_fundamentals_hk": {
        "name": "query_fundamentals_hk",
        "description": "查询港股基本面快照（akshare/东财，C 级；ticker 如 2228.HK 或 02228）",
        "parameters": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    "query_hkex_news": {
        "name": "query_hkex_news",
        "description": (
            "查询港股披露易公告（HKEXnews，A 级：披露时刻精确到分钟）。"
            "港股财报/公告的一手来源；ticker 如 2228.HK 或 02228"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days": {"type": "integer", "description": "回看天数（默认 365）"},
            },
            "required": ["ticker"],
        },
    },
    "query_news_gdelt": {
        "name": "query_news_gdelt",
        "description": (
            "查询全球新闻（GDELT，B 级：seendate 收录时刻；含中文媒体）。"
            "催化剂/风险/舆情维度的证据源"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_records": {"type": "integer", "description": "默认 25，上限 250"},
                "timespan": {"type": "string", "description": "如 1d/1w/1m（默认 1m）"},
            },
            "required": ["query"],
        },
    },
    # 仅 eval canary 模式注册的源。模型侧描述必须与真源不可区分——诱饵的全部意义
    # 在于考验 agent 是否复述上下文；操作员侧的 capability 描述保持诚实标注（能力页）。
    "query_canary_news": {
        "name": "query_canary_news",
        "description": "查询市场新闻与公司公告（available_at = 发布时刻）",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
            },
            "required": ["ticker"],
        },
    },
}
