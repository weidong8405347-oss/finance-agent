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
                item["chunk_id"] = chunk_store.add(
                    source_id=r.source_id,
                    text=item_text,
                    url=r.url,
                    available_at=r.available_at,
                    pit_grade=PitGrade(gateway_grade(gateway, r.source_id)),
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
