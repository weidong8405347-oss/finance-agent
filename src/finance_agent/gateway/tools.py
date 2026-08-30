"""把 DataGateway 包装成 agent tool：工具结果自动携带 provenance。

provenance 是 leakage-audit hook 的审计锚点（DESIGN.md §4.3 防线 3 的机械部分）。
"""

from __future__ import annotations

import json
from typing import Any

from .gateway import DataGateway


def make_gateway_tool(gateway: DataGateway, source_id: str):
    """生成一个工具函数：query(source 固定, request=arguments) → ToolResult dict。"""

    def tool(arguments: dict[str, Any]) -> dict[str, Any]:
        records = gateway.query(source_id, arguments)
        return {
            "content": json.dumps([r.payload for r in records], ensure_ascii=False, default=str),
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
}
