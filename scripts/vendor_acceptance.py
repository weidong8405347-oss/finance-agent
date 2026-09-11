#!/usr/bin/env python3
"""供应商验收卡线束（tools-plugins 方案 §7.2，电话会/一致预期接入前检查）。

方案纪律：「每个候选服务先提交一份供应商验收卡：目标 ticker 清单、市场与历史
覆盖、20 个真实查询结果、来源定位、数据可知时间、空值和更正行为、失败码、
限流、单次成本、允许存储/展示范围。抽样与原始披露逐字段对照。」

本线束对候选供应商跑**只读小样本探针**并生成验收卡（JSON + Markdown）：
- 缺凭证 → 卡片状态 blocked（缺失项显式列出，不假装验收过）；
- 探针失败/字段缺失/无 PIT 字段 → 如实进卡（验收卡的用途就是暴露这些）；
- 单元测试注入假 transport，不在 CI 打真实网络。

候选（方案 §7.1）：FMP（analyst estimates + earnings call transcripts）、
Financial Datasets（结构化财务，带 filing 回指）。

用法：
    uv run python scripts/vendor_acceptance.py --vendor fmp --tickers AAPL,NVDA,0700.HK
    uv run python scripts/vendor_acceptance.py --all --out data/vendor-cards/
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 探针定义：端点 + 期望字段（方案验收卡逐项）
#: 期望字段缺失 ≠ 拒绝，但必须进卡（「服务有出处不等于其所有字段具备 PIT」）
PROBES: dict[str, dict[str, Any]] = {
    "fmp": {
        "vendor": "Financial Modeling Prep (FMP)",
        "env_keys": ["FMP_API_KEY"],
        "capabilities": ["earnings.call_transcript", "expectations.analyst_estimates"],
        "base": "https://financialmodelingprep.com",
        "endpoints": [
            {
                "name": "analyst_estimates",
                "path": "/stable/analyst-estimates?symbol={ticker}&period=annual&limit=4",
                "expect_fields": ["date", "estimatedRevenueAvg", "estimatedEpsAvg"],
                "pit_fields": ["date", "updatedDate"],
            },
            {
                "name": "earnings_call_transcript",
                "path": "/stable/earning-call-transcript?symbol={ticker}&year=2025&quarter=3",
                "expect_fields": ["content", "symbol", "period"],
                "pit_fields": ["date"],
            },
        ],
        "notes": ["套餐权限/历史快照/再分发范围以合同为准（本文档不能证明已获支持）",
                   "港股覆盖需单独验证（小盘/港股覆盖是已知风险）"],
    },
    "financial_datasets": {
        "vendor": "Financial Datasets",
        "env_keys": ["FINANCIAL_DATASETS_API_KEY"],
        "capabilities": ["financials.structured"],
        "base": "https://api.financialdatasets.ai",
        "endpoints": [
            {
                "name": "income_statements",
                "path": "/financials/income-statements/?ticker={ticker}&period=annual&limit=2",
                "expect_fields": ["filing_url", "fiscal_period", "report_period"],
                "pit_fields": ["filing_date", "acceptance_date_time", "filing_url"],
            },
        ],
        "notes": ["结构化字段与原始 filing 的抽样对账是验收必要项（方案 §7.1）"],
    },
}


def _probe(
    vendor: dict[str, Any], endpoint: dict[str, Any], tickers: list[str],
    get: Callable[[str], tuple[int, Any]],
) -> dict[str, Any]:
    """单端点小样本探针：状态码/延迟/字段存在性/PIT 字段/空值率（全部如实）。"""
    samples: list[dict[str, Any]] = []
    for ticker in tickers:
        url = vendor["base"] + endpoint["path"].format(ticker=ticker)
        t0 = time.monotonic()
        try:
            status, body = get(url)
        except Exception as e:  # noqa: BLE001 - 探针失败如实进卡（不包装为空成功）
            samples.append({"ticker": ticker, "error": f"{type(e).__name__}: {e}"})
            continue
        elapsed_ms = round((time.monotonic() - t0) * 1000)
        rows = body if isinstance(body, list) else (
            body.get("results") or body.get("transcript") or [] if isinstance(body, dict)
            else [])
        if isinstance(body, dict) and not rows and body:
            rows = [body]
        first = rows[0] if rows and isinstance(rows[0], dict) else {}
        expect = endpoint["expect_fields"]
        pit = endpoint["pit_fields"]
        samples.append({
            "ticker": ticker, "http_status": status,
            "latency_ms": elapsed_ms, "rows": len(rows),
            "expect_fields_present": sorted(k for k in expect if k in first),
            "expect_fields_missing": sorted(k for k in expect if k not in first),
            "pit_fields_present": sorted(k for k in pit if k in first and first[k]),
            "sample_keys": sorted(first)[:20],
        })
    ok = [s for s in samples if s.get("http_status") == 200 and s.get("rows")]
    return {
        "endpoint": endpoint["name"], "samples": samples,
        "coverage": f"{len(ok)}/{len(samples)}",
        "expect_fields_union_missing": sorted(
            {k for s in samples for k in s.get("expect_fields_missing", [])}),
    }


def build_card(
    vendor_id: str, tickers: list[str], *,
    env: dict[str, str] | None = None,
    get: Callable[[str], tuple[int, Any]] | None = None,
) -> dict[str, Any]:
    """生成一家供应商的验收卡（缺凭证 → blocked，缺失项显式列出）。"""
    vendor = PROBES[vendor_id]
    environ = os.environ if env is None else env
    card: dict[str, Any] = {
        "vendor_id": vendor_id, "vendor": vendor["vendor"],
        "capabilities": vendor["capabilities"],
        "created_at": datetime.now(UTC).isoformat(),
        "target_tickers": tickers,
        "checklist": [
            "市场与历史覆盖（目标 ticker 全量可查，历史区间满足研究需要）",
            "来源定位（字段可回指 filing/transcript 原文 URL）",
            "数据可知时间（快照/修订有 PIT 依据，能回答「当时可知什么」）",
            "空值与更正行为（缺值形态、更正是否留痕）",
            "失败码与限流（错误形态、速率上限、退避语义）",
            "单次成本与配额（按套餐实测）",
            "允许存储/展示范围（合同条款确认）",
            "抽样与原始披露逐字段对账（至少 5 条）",
        ],
        "vendor_notes": vendor["notes"],
        "probes": [],
        "status": "probed",
        "sign_off": "（待人工签署：以上证据充分 / 需补探针 / 不接入）",
    }
    key = next((k for k in vendor["env_keys"] if environ.get(k)), None)
    if key is None:
        card["status"] = "blocked"
        card["blocked_reason"] = f"缺少凭证环境变量：{'|'.join(vendor['env_keys'])}"
        card["checklist_status"] = "未执行探针（接入前检查项见 checklist）"
        return card
    if get is None:
        import httpx

        def get(url: str) -> tuple[int, Any]:  # noqa: F811
            headers = {}
            if vendor_id == "financial_datasets":
                headers["X-API-KEY"] = environ[key]
            elif vendor_id == "fmp":
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}apikey={environ[key]}"
            resp = httpx.get(url, headers=headers, timeout=30)
            try:
                return resp.status_code, resp.json()
            except Exception:  # noqa: BLE001 - 非 JSON 响应如实记录
                return resp.status_code, {"_raw": resp.text[:500]}

    for endpoint in vendor["endpoints"]:
        card["probes"].append(_probe(vendor, endpoint, tickers, get))
    # 卡结论不自动给：probe 证据 + checklist 由人签署（方案：验收是决策不是过场）
    return card


def card_to_markdown(card: dict[str, Any]) -> str:
    lines = [
        f"# 供应商验收卡：{card['vendor']}（{card['vendor_id']}）",
        "",
        f"- 生成：{card['created_at']}",
        f"- 状态：{card['status']}",
        f"- 目标 ticker：{', '.join(card['target_tickers'])}",
        f"- 能力：{', '.join(card['capabilities'])}",
        "",
    ]
    if card["status"] == "blocked":
        lines += [f"> **blocked**：{card['blocked_reason']}", ""]
    for probe in card["probes"]:
        lines += [
            f"## 探针 {probe['endpoint']}（覆盖 {probe['coverage']}）",
            "",
            "| ticker | http | 延迟 ms | 行数 | 缺期望字段 | PIT 字段 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for s in probe["samples"]:
            lines.append(
                "| {t} | {h} | {l} | {r} | {m} | {p} |".format(
                    t=s.get("ticker"), h=s.get("http_status", "ERR"),
                    l=s.get("latency_ms", "-"), r=s.get("rows", "-"),
                    m=",".join(s.get("expect_fields_missing", [])) or "—",
                    p=",".join(s.get("pit_fields_present", [])) or "无",
                )
            )
        lines.append("")
    lines += ["## 验收 checklist（逐项人工确认）", ""]
    lines += [f"- [ ] {c}" for c in card["checklist"]]
    lines += ["", "## 供应商注意事项", ""]
    lines += [f"- {n}" for n in card["vendor_notes"]]
    lines += ["", f"签署：{card['sign_off']}", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vendor", choices=sorted(PROBES), default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--tickers", default="AAPL,NVDA,0700.HK,3988.HK",
                    help="逗号分隔的目标 ticker（默认覆盖 US+HK）")
    ap.add_argument("--out", default="data/vendor-cards")
    args = ap.parse_args(argv)

    vendor_ids = sorted(PROBES) if args.all else ([args.vendor] if args.vendor else [])
    if not vendor_ids:
        ap.error("--vendor 或 --all 必选")
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for vid in vendor_ids:
        card = build_card(vid, tickers)
        (out_dir / f"{vid}.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / f"{vid}.md").write_text(card_to_markdown(card), encoding="utf-8")
        print(f"{vid}: {card['status']} → {out_dir / (vid + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
