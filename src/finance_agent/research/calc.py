"""calc 工具：服务端确定性计算（research-capability-upgrade §4.7）。

纪律对齐：
- 原则 8（数字保护）从「quote 逐字」升级为「逐字 + 计算一致性」双保险；
- 全部数值用 Decimal（Decimal(str(v)) 防二进制浮点污染），禁止模型心算进结论；
- 双源交叉 >1% 不一致 → 明确返回不一致信号（模型应走 conflict 机制而非二选一静默）。

操作：
- verify_market_cap：price × shares 验算 reported 市值，偏差 >1% 告警；
- cross_validate：同一字段多个独立来源的值交叉验证，极差 >1% 告警；
- three_scenario：三情景估值（eps × 增长复利 × 目标 PE → 目标价与空间）。
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

_D4 = Decimal("0.0001")


def _dec(v: Any) -> Decimal:
    """JSON 数值 → Decimal（str 转换防浮点尾差；非法值 fail-loud）。"""
    try:
        return Decimal(str(v))
    except InvalidOperation:
        raise ValueError(f"非法数值: {v!r}") from None


def _pct(diff: Decimal, base: Decimal) -> str:
    if base == 0:
        return "∞"
    return str((abs(diff) / abs(base) * 100).quantize(_D4, rounding=ROUND_HALF_UP))


def _verify_market_cap(args: dict[str, Any]) -> dict[str, Any]:
    price, shares, reported = _dec(args["price"]), _dec(args["shares"]), _dec(args["reported"])
    computed = price * shares
    diff_pct = _pct(computed - reported, reported)
    tol = _dec(args.get("tolerance", "0.01")) * 100
    ok = Decimal(diff_pct) if diff_pct != "∞" else Decimal("999")
    return {
        "computed_market_cap": str(computed.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
        "reported_market_cap": str(reported),
        "diff_pct": diff_pct,
        "ok": ok <= tol,
        "note": "ok=true 表示手算与上报值一致（≤容差）；false 时两个数字都别信，回源核对单位与股本口径",
    }


def _cross_validate(args: dict[str, Any]) -> dict[str, Any]:
    values = [_dec(v) for v in args["values"]]
    if len(values) < 2:
        raise ValueError("cross_validate 至少需要 2 个独立来源的值")
    tol = _dec(args.get("tolerance", "0.01")) * 100
    lo, hi = min(values), max(values)
    spread_pct = _pct(hi - lo, lo)
    ok = (Decimal(spread_pct) if spread_pct != "∞" else Decimal("999")) <= tol
    return {
        "values": [str(v) for v in values],
        "min": str(lo),
        "max": str(hi),
        "spread_pct": spread_pct,
        "ok": ok,
        "note": "ok=false → 来源间存在实质分歧，走 conflict 机制（不得静默二选一）",
    }


def _three_scenario(args: dict[str, Any]) -> dict[str, Any]:
    price, eps = _dec(args["price"]), _dec(args["eps"])
    growth = [_dec(g) for g in args["growth"]]  # 年化增速，如 0.25
    pe = [_dec(p) for p in args["pe"]]
    if len(growth) != 3 or len(pe) != 3:
        raise ValueError("growth 与 pe 都必须恰好 3 个值（悲观/中性/乐观）")
    years = int(args.get("years", 3))
    out = []
    for label, g, p in zip(("悲观", "中性", "乐观"), growth, pe, strict=True):
        future_eps = eps * (1 + g) ** years
        target = future_eps * p
        upside = (target / price - 1) * 100 if price != 0 else Decimal("0")
        out.append({
            "scenario": label,
            "future_eps": str(future_eps.quantize(_D4, rounding=ROUND_HALF_UP)),
            "target_price": str(target.quantize(_D4, rounding=ROUND_HALF_UP)),
            "upside_pct": str(upside.quantize(_D4, rounding=ROUND_HALF_UP)),
        })
    return {"years": years, "scenarios": out,
            "note": "判断性输出（假设驱动），不是事实；档案只存事实，此结果供报告/决策卡"}


def _valuation_snapshot(args: dict[str, Any]) -> dict[str, Any]:
    """估值快照（P4 §5.1）：market_cap + net_debt → EV；revenue/ebitda/fcf → 倍数。
    缺失输入跳过对应倍数（None 不进场）；全部 Decimal 精确算术。"""
    mc = _dec(args["market_cap"])
    nd = _dec(args.get("net_debt", 0))
    ev = mc + nd
    out: dict[str, Any] = {"market_cap": str(mc), "net_debt": str(nd), "ev": str(ev)}
    if args.get("revenue") is not None:
        rev = _dec(args["revenue"])
        out["ev_sales"] = str((ev / rev).quantize(_D4, rounding=ROUND_HALF_UP)) if rev else None
        out["ps"] = str((mc / rev).quantize(_D4, rounding=ROUND_HALF_UP)) if rev else None
    if args.get("ebitda") is not None:
        ebitda = _dec(args["ebitda"])
        out["ev_ebitda"] = (
            str((ev / ebitda).quantize(_D4, rounding=ROUND_HALF_UP)) if ebitda > 0 else "N/M（负 EBITDA）"
        )
    if args.get("fcf") is not None:
        fcf = _dec(args["fcf"])
        out["fcf_yield_pct"] = (
            str((fcf / mc * 100).quantize(_D4, rounding=ROUND_HALF_UP)) if mc else None
        )
    if args.get("eps") is not None and args.get("price") is not None:
        eps, price = _dec(args["eps"]), _dec(args["price"])
        out["pe"] = str((price / eps).quantize(_D4, rounding=ROUND_HALF_UP)) if eps > 0 else "N/M（亏损）"
    out["note"] = "估值倍数是计算产物（输入均为档案事实）；判断贵贱是分析层的事"
    return out


_OPS = {
    "verify_market_cap": _verify_market_cap,
    "cross_validate": _cross_validate,
    "three_scenario": _three_scenario,
    "valuation_snapshot": _valuation_snapshot,
}

CALC_TOOL_SCHEMA = {
    "name": "calc",
    "description": (
        "服务端确定性计算（Decimal 精确算术，禁止心算进结论）。"
        "op=verify_market_cap(price, shares, reported[, tolerance]) 市值验算；"
        "op=cross_validate(values[][, tolerance]) 多源交叉（>1% 分歧告警）；"
        "op=three_scenario(price, eps, growth[3], pe[3][, years]) 三情景估值；"
        "op=valuation_snapshot(market_cap[, net_debt, revenue, ebitda, fcf, eps, price]) 估值倍数快照。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": list(_OPS)},
            "price": {"type": "number"}, "shares": {"type": "number"},
            "reported": {"type": "number"}, "tolerance": {"type": "number"},
            "values": {"type": "array", "items": {"type": "number"}},
            "eps": {"type": "number"},
            "growth": {"type": "array", "items": {"type": "number"}},
            "pe": {"type": "array", "items": {"type": "number"}},
            "years": {"type": "integer"},
            "market_cap": {"type": "number"}, "net_debt": {"type": "number"},
            "revenue": {"type": "number"}, "ebitda": {"type": "number"},
            "fcf": {"type": "number"},
        },
        "required": ["op"],
    },
}


def calc_tool(args: dict[str, Any]) -> dict[str, Any]:
    op = str(args.get("op") or "")
    fn = _OPS.get(op)
    if fn is None:
        return {"content": f"error: 未知 op {op!r}；可用：{list(_OPS)}", "provenance": []}
    try:
        result = fn(args)
    except (ValueError, KeyError) as e:
        return {"content": f"error: {e}", "provenance": []}
    return {"content": json.dumps(result, ensure_ascii=False), "provenance": []}
