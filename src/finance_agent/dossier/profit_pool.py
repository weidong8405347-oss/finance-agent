"""Profit Pool 推导（升级方案 §22 + §48.4）：从 industry_map 的 value_flow 边提取
可定量的利润池份额，供前端画 Stacked Bar；证据不足时返回 None（前端维持定性展示）。

硬纪律：
- 只接受**显式百分数字符串**（"45%"）；裸数字、区间（"55-65%"）、金额一律不解析
  ——份额口径含糊时不画视觉精确的图；
- §48.4：单一来源的份额必须标 Estimated / Directional——estimated=True 当且仅当
  该边的独立证据引用 < 2 条（确定性规则，不评来源质量）；
- 份额合计偏离 100% 时在 notes 显式标注（可能未覆盖全链或口径重叠），不做归一化
  （归一化会改写原始数字）；
- 不生成「未分配/其他」补齐段——只画有证据的部分。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

#: 显式百分数（"45%" / "45.5%"）；区间与裸数字不匹配（不猜口径）
_PERCENT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*%$")


def _parse_share(flow_value: Any) -> str | None:
    """flow_value → 百分数数字部分的十进制字符串；不可解析 → None。"""
    if not isinstance(flow_value, str):
        return None
    m = _PERCENT_RE.match(flow_value.strip())
    if not m:
        return None
    try:
        # 十进制规范化（去尾零），不改写数值
        return format(Decimal(m.group(1)).normalize(), "f")
    except InvalidOperation:
        return None


def build_profit_pool(imap: dict[str, Any]) -> dict[str, Any] | None:
    """industry_map payload → profit_pool 视图对象（无可定量边 → None）。

    语义约定（写进 submit_structures 工具描述）：value_flow 边上的 flow_value =
    该边 target 环节捕获的价值份额（百分数）。
    """
    edges = imap.get("edges") or []
    nodes = {str(n.get("node_id")): n for n in imap.get("nodes") or []}
    entries: list[dict[str, Any]] = []
    for e in edges:
        if str(e.get("relation")) != "value_flow" or not e.get("flow_known"):
            continue
        share = _parse_share(e.get("flow_value"))
        if share is None:
            continue
        target = str(e.get("target") or "")
        node = nodes.get(target) or {}
        refs = [str(r) for r in e.get("evidence_refs") or []]
        entries.append({
            "node": target,
            "label": str(node.get("label") or target),
            "layer": str(node.get("layer") or ""),
            "share": share,  # 百分数（"45" = 45%），十进制字符串契约
            # §48.4：单一来源 → Estimated / Directional（前端加水印级标注）
            "estimated": len(refs) < 2,
            "evidence_refs": refs,
            "note": str(e.get("note") or ""),
        })
    if not entries:
        return None
    total = sum(Decimal(e["share"]) for e in entries)
    notes: list[str] = []
    if any(e["estimated"] for e in entries):
        notes.append("含单一来源份额：标记 Estimated / Directional（§48.4），不是精确测量")
    if abs(total - Decimal(100)) > Decimal(10):
        notes.append(
            f"份额合计 {format(total.normalize(), 'f')}%，偏离 100%——可能未覆盖全链或口径重叠，"
            "未做归一化（不改写原始数字）"
        )
    return {
        "kind": "stacked_bar",
        "unit": "percent",
        "entries": entries,
        "total_share": format(total.normalize(), "f"),
        "notes": notes,
    }


__all__ = ["build_profit_pool"]
