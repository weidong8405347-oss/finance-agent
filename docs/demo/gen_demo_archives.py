#!/usr/bin/env python3
"""生成 demo 用的档案存档 HTML（示意 ProfileRenderer 产物形态）。

正式实现是知识库投影 + 版本化；demo 里只是让 5 个实体都有可打开的样例文件。
BE 用精修的 archive-sample-BE.html，本脚本生成其余 4 个（同款简化模板）。
"""

from pathlib import Path

OUT = Path(__file__).parent

STYLE = """
body { font: 14px/1.7 Georgia, "Songti SC", serif; color: #1a1a1a; max-width: 760px; margin: 0 auto; padding: 32px 28px 80px; }
.mono { font-family: ui-monospace, Menlo, monospace; }
header.doc { border-bottom: 2px solid #1a1a1a; padding-bottom: 14px; margin-bottom: 22px; }
header.doc h1 { font-size: 22px; margin: 0 0 6px; }
header.doc .meta { font-family: ui-monospace, Menlo, monospace; font-size: 11px; color: #666; }
h2 { font-size: 15px; margin: 28px 0 8px; border-left: 4px solid #1a1a1a; padding-left: 10px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 8px 0; }
td, th { border-bottom: 1px solid #e0e0e0; padding: 6px 8px; text-align: left; }
th { font-size: 11px; color: #777; font-weight: 600; }
.num { font-family: ui-monospace, Menlo, monospace; font-size: 12.5px; }
.ev { border-bottom: 1px dotted #999; cursor: help; position: relative; }
.ev:hover { background: #fffbe6; }
.ev:hover::after { content: attr(data-ev); position: absolute; left: 0; top: 130%; z-index: 5;
  background: #1a1a1a; color: #fff; font-size: 11px; font-family: ui-monospace, Menlo, monospace;
  padding: 8px 10px; border-radius: 6px; width: 340px; white-space: pre-wrap; line-height: 1.5; }
.neg { color: #b91c1c; }
.cap { font-size: 11px; color: #777; font-family: ui-monospace, Menlo, monospace; margin-bottom: 14px; }
.thesis { background: #fafaf7; border: 1px solid #e8e6df; border-radius: 8px; padding: 14px 18px; font-size: 13.5px; }
footer.doc { margin-top: 40px; border-top: 1px solid #ddd; padding-top: 12px; font-size: 11px; color: #888; font-family: ui-monospace, Menlo, monospace; }
"""


def line_svg(points: list[float], label: str) -> str:
    """12 点价格折线 → 内联 SVG。"""
    w, h, lo, hi = 700, 200, min(points), max(points)
    rng = (hi - lo) or 1
    xs = [48 + i * (636 / 11) for i in range(12)]
    ys = [168 - (p - lo) / rng * 140 for p in points]
    poly = " ".join(f"{x:.0f},{y:.0f}" for x, y in zip(xs, ys))
    return f"""<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">
<rect width="{w}" height="{h}" fill="#fff"/>
<line x1="48" y1="20" x2="48" y2="168" stroke="#ddd"/><line x1="48" y1="168" x2="684" y2="168" stroke="#ddd"/>
<g font-family="ui-monospace,Menlo,monospace" font-size="9" fill="#999">
<text x="8" y="172">{lo:.0f}</text><text x="8" y="24">{hi:.0f}</text>
<text x="52" y="184">12 个月前</text><text x="620" y="184">最近</text></g>
<polyline fill="none" stroke="#1a1a1a" stroke-width="1.6" points="{poly}"/>
<circle cx="{xs[-1]:.0f}" cy="{ys[-1]:.0f}" r="3" fill="#1a1a1a"/>
<text x="{xs[-1]-130:.0f}" y="{max(ys[-1]-8,12):.0f}" font-family="ui-monospace,Menlo,monospace" font-size="10" fill="#1a1a1a">{label}</text>
</svg>"""


def bars_svg(rev: list[tuple[str, float]], unit: str) -> str:
    hi = max(v for _, v in rev)
    bars, labels = [], []
    for i, (yr, v) in enumerate(rev):
        x, bh = 90 + i * 140, v / hi * 118
        bars.append(f'<rect x="{x}" y="{144-bh:.1f}" width="60" height="{bh:.1f}" fill="#1a1a1a"/>')
        labels.append(f'<text x="{x+30}" y="{136-bh:.0f}" font-size="10" text-anchor="middle">{v:g}{unit}</text>')
        labels.append(f'<text x="{x+30}" y="160" font-size="10" fill="#999" text-anchor="middle">{yr}</text>')
    return f"""<svg width="700" height="180" viewBox="0 0 700 180" xmlns="http://www.w3.org/2000/svg">
<rect width="700" height="180" fill="#fff"/>
<line x1="44" y1="144" x2="660" y2="144" stroke="#ddd"/>
<g font-family="ui-monospace,Menlo,monospace" fill="#1a1a1a">{''.join(bars)}</g>
<g font-family="ui-monospace,Menlo,monospace">{''.join(labels)}</g>
</svg>"""


def fact_rows(facts: list[dict]) -> str:
    rows = []
    for f in facts:
        val = f["value"]
        if f.get("ev"):
            val = f'<span class="ev" data-ev="{f["ev"]}">{val}</span>'
        neg = ' class="neg"' if f.get("neg") else ""
        rows.append(
            f'<tr><td class="mono">{f["field"]}</td><td class="num"{neg}>{val}</td>'
            f'<td class="mono">{f["et"]}</td><td class="mono">{f["kt"]}</td>'
            f'<td class="mono">{f["ver"]}</td></tr>'
        )
    return "\n".join(rows)


def render(e: dict) -> str:
    tl = "\n".join(
        f'<tr><td class="mono">{t[0]}</td><td>{t[1]}</td><td class="mono">{t[2]}</td></tr>'
        for t in e["timeline"]
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{e["id"]} · {e["name"]} 档案存档 · {e["ver"]}</title><style>{STYLE}</style></head>
<body>
<header class="doc">
  <h1>{e["name"]} <span class="mono" style="font-size:14px;color:#666">{e["id"]}</span></h1>
  <div class="meta">档案版本 {e["ver"]} · kb_snapshot {e["snap"]} · 生成于 {e["gen"]}
    · 事实 {e["nfacts"]} 条 · 证据绑定率 100% · 冲突 {e["nconf"]}</div>
</header>
<h2>投资论点（thesis）</h2>
<div class="thesis">{e["thesis"]}</div>
<h2>价格走势（日线 · 近 12 个月）</h2>
<div class="chart">{line_svg(e["prices"], e["price_label"])}</div>
<div class="cap">数据：DataGateway · 日线 · available_at = 交易日 +1d · PIT-A</div>
<h2>关键财务指标（营收）</h2>
<div class="chart">{bars_svg(e["revenue"], e["unit"])}</div>
<div class="cap">柱：营收 · 数据：定期报告 · PIT-A</div>
<h2>核心事实（按维度）</h2>
<table><tr><th>字段</th><th>值</th><th>event_time</th><th>knowledge_time</th><th>版本</th></tr>
{fact_rows(e["facts"])}
</table>
<h2>关键事件时间线</h2>
<table><tr><th>knowledge_time</th><th>事件</th><th>证据</th></tr>
{tl}
</table>
<footer class="doc">finance-agent · ProfileRenderer（demo 样例）· 本文件是 EventStore/双时态 KB 的投影
· 每个数字 hover 可见证据原文/available_at/PIT 等级</footer>
</body></html>"""


ENTITIES = [
    {
        "file": "archive-demo-600519.html",
        "id": "600519.SH", "name": "贵州茅台", "ver": "v23", "snap": "77ab…42",
        "gen": "2026-08-28 18:03", "nfacts": 61, "nconf": 1,
        "thesis": "高端白酒需求承压但茅台批价韧性仍存；直营占比提升对冲渠道利润压缩。"
        "<b>失效条件</b>：批价跌破 2000 元且连续两月无修复；营收增速 &lt; 5%。",
        "prices": [1680, 1650, 1620, 1580, 1520, 1490, 1460, 1505, 1470, 1430, 1455, 1442],
        "price_label": "¥1442（2026-08-28）",
        "revenue": [("FY21", 1062), ("FY22", 1241), ("FY23", 1477), ("FY24", 1741)], "unit": "亿",
        "facts": [
            {"field": "revenue.fy2024", "value": "¥1741亿", "et": "2024-12-31", "kt": "2025-04-03", "ver": "v4",
             "ev": "ev-mt-01 · 2024 年报\n'营业总收入 1741.44 亿元'\nretrieved 2026-08-20 · available 2025-04-03 · PIT-A"},
            {"field": "margins.gross", "value": "91.9%", "et": "2024-12-31", "kt": "2025-04-03", "ver": "v3"},
            {"field": "批价.飞天散瓶", "value": "¥2080", "et": "2026-08-25", "kt": "2026-08-26", "ver": "v9"},
            {"field": "channel.direct_share", "value": "45.8%", "et": "2024-12-31", "kt": "2025-04-03", "ver": "v2"},
            {"field": "dividend.payout", "value": "75%", "et": "2024-12-31", "kt": "2025-04-03", "ver": "v1"},
        ],
        "timeline": [
            ("2025-04-03", "2024 年报发布：营收 1741 亿（+15.7%）", "ev-mt-01"),
            ("2026-08-26", "飞天散瓶批价 2080 元（周度渠道调研）", "ev-mt-44"),
        ],
    },
    {
        "file": "archive-demo-tsla.html",
        "id": "TSLA", "name": "Tesla, Inc.", "ver": "v15", "snap": "c01d…9b",
        "gen": "2026-08-26 11:20", "nfacts": 47, "nconf": 2,
        "thesis": "汽车主业毛利率触底，储能业务成为第二增长曲线；估值由 Robotaxi 叙事主导，"
        "基本面对股价的解释力下降。<b>失效条件</b>：储能装机增速连续两季 &lt; 30%。",
        "prices": [210, 225, 248, 238, 255, 282, 268, 290, 275, 262, 285, 301],
        "price_label": "$301.10（2026-08-28）",
        "revenue": [("FY21", 53.8), ("FY22", 81.5), ("FY23", 96.8), ("FY24", 97.7)], "unit": "B",
        "facts": [
            {"field": "revenue.fy2024", "value": "$97.7B", "et": "2024-12-31", "kt": "2025-01-29", "ver": "v3",
             "ev": "ev-ts-02 · 10-K FY2024\n'Total revenues were $97.7 billion'\nretrieved 2026-08-26 · available 2025-01-29 · PIT-A"},
            {"field": "deliveries.fy2024", "value": "1.79M", "et": "2024-12-31", "kt": "2025-01-02", "ver": "v2"},
            {"field": "energy.storage_deployed", "value": "31.4GWh", "et": "2024-12-31", "kt": "2025-01-29", "ver": "v2"},
            {"field": "margins.auto_gross", "value": "16.4%", "et": "2025-06-30", "kt": "2025-07-23", "ver": "v4", "neg": True},
        ],
        "timeline": [
            ("2025-01-29", "FY2024 10-K：营收 $97.7B（+0.9%）", "ev-ts-02"),
            ("2025-07-23", "2025Q2：汽车毛利率 16.4%（两个来源口径冲突，已标 conflict）", "ev-ts-19"),
        ],
    },
    {
        "file": "archive-demo-gps.html",
        "id": "GPS", "name": "Gap Inc.", "ver": "v4", "snap": "3e55…a8",
        "gen": "2026-05-30 09:12", "nfacts": 18, "nconf": 0,
        "thesis": "（已剔除关注）品牌重塑未见拐点，档案停止更新；保留作为历史研究样本。",
        "prices": [22, 21, 23, 22, 20, 19, 21, 20, 18, 19, 18, 17.5],
        "price_label": "$17.50（2026-08-28）",
        "revenue": [("FY21", 16.7), ("FY22", 15.6), ("FY23", 14.9), ("FY24", 15.1)], "unit": "B",
        "facts": [
            {"field": "revenue.fy2024", "value": "$15.1B", "et": "2025-01-31", "kt": "2025-03-08", "ver": "v1",
             "ev": "ev-gps-01 · 10-K FY2024\n'Net sales were $15.1 billion'\nretrieved 2026-05-30 · available 2025-03-08 · PIT-A"},
            {"field": "margins.gross", "value": "41.3%", "et": "2025-01-31", "kt": "2025-03-08", "ver": "v1"},
        ],
        "timeline": [("2025-03-08", "FY2024 10-K 发布", "ev-gps-01")],
    },
    {
        "file": "archive-demo-sofc.html",
        "id": "industry:sofc", "name": "固体氧化物燃料电池（SOFC）行业", "ver": "v3", "snap": "5d21…c7",
        "gen": "2026-08-29 09:40", "nfacts": 22, "nconf": 0,
        "thesis": "AI 数据中心供电缺口把 SOFC 从「分布式电源备选项」推为「主供补充」；"
        "行业瓶颈在电堆寿命与规模化制造成本。",
        "prices": [100, 103, 108, 112, 118, 125, 131, 138, 142, 150, 158, 166],
        "price_label": "行业景气指数 166（合成）",
        "revenue": [("2022", 1.1), ("2023", 1.6), ("2024", 2.4), ("2025E", 3.5)], "unit": "B",
        "facts": [
            {"field": "market.size.2024", "value": "$2.4B", "et": "2024-12-31", "kt": "2025-03-15", "ver": "v2",
             "ev": "ev-in-03 · 行业白皮书 2025\n'global SOFC market reached $2.4 billion'\nretrieved 2026-08-29 · available 2025-03-15 · PIT-B"},
            {"field": "market.cagr", "value": "31%", "et": "2024-12-31", "kt": "2025-03-15", "ver": "v1"},
            {"field": "chain.key_bottleneck", "value": "电堆寿命/热循环", "et": "2025-06-30", "kt": "2025-07-10", "ver": "v1"},
        ],
        "timeline": [("2025-03-15", "行业白皮书：2024 市场规模 $2.4B", "ev-in-03")],
    },
]

if __name__ == "__main__":
    for e in ENTITIES:
        (OUT / e["file"]).write_text(render(e), encoding="utf-8")
        print("written", e["file"])
