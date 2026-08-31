"""ProfileRenderer：档案的 HTML 存档投影（redesign §3.8 / D8）。

- 自包含单文件 HTML（内联样式 + 内联 SVG 图表，无 JS/无外部依赖）：
  可离线打开、可 diff、可审计；
- 版本化：S2 完成且档案有变化时写 knowledge/<kind>s/<id>/archive/<snapshot>.html，
  并更新 latest.html；历史版本永不删除（as_of 时光机的磁盘对应物）；
- 每个事实数字带证据锚点（hover 出原文摘录 + available_at + PIT 等级）。
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path

from .store import BitemporalStore

_CSS = """
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
.ev { border-bottom: 1px dotted #999; cursor: help; }
.ev:hover { background: #fffbe6; }
.neg { color: #b91c1c; }
.cap { font-size: 11px; color: #777; font-family: ui-monospace, Menlo, monospace; margin-bottom: 14px; }
.thesis { background: #fafaf7; border: 1px solid #e8e6df; border-radius: 8px; padding: 14px 18px; font-size: 13.5px; }
.conflict { color: #b45309; font-family: ui-monospace, Menlo, monospace; font-size: 11px; }
footer.doc { margin-top: 40px; border-top: 1px solid #ddd; padding-top: 12px; font-size: 11px; color: #888; font-family: ui-monospace, Menlo, monospace; }
"""


def _esc(v: object) -> str:
    return html.escape(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v)


def _price_svg(prices: list[dict], width: int = 700, height: int = 200) -> str:
    """收盘价序列 → 内联 SVG 折线（服务端渲染，无 JS）。"""
    closes = [float(p["close"]) for p in prices if p.get("close") is not None]
    if len(closes) < 2:
        return ""
    lo, hi = min(closes), max(closes)
    rng = (hi - lo) or 1.0
    n = len(closes)
    xs = [48 + i * (636 / (n - 1)) for i in range(n)]
    ys = [168 - (c - lo) / rng * 140 for c in closes]
    poly = " ".join(f"{x:.0f},{y:.0f}" for x, y in zip(xs, ys))
    first, last = prices[0].get("date", ""), prices[-1].get("date", "")
    e = html.escape
    return f"""<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
<rect width="{width}" height="{height}" fill="#fff"/>
<line x1="48" y1="20" x2="48" y2="168" stroke="#ddd"/><line x1="48" y1="168" x2="684" y2="168" stroke="#ddd"/>
<g font-family="ui-monospace,Menlo,monospace" font-size="9" fill="#999">
<text x="8" y="172">{lo:.2f}</text><text x="8" y="24">{hi:.2f}</text>
<text x="52" y="184">{e(str(first))}</text><text x="620" y="184">{e(str(last))}</text></g>
<polyline fill="none" stroke="#1a1a1a" stroke-width="1.4" points="{poly}"/>
<circle cx="{xs[-1]:.0f}" cy="{ys[-1]:.0f}" r="3" fill="#1a1a1a"/>
<text x="{max(xs[-1]-150,50):.0f}" y="{max(ys[-1]-8,12):.0f}" font-family="ui-monospace,Menlo,monospace" font-size="10">{closes[-1]:.2f}</text>
</svg>"""


def _content_hash(kb: BitemporalStore, kind: str, entity_id: str, namespace: str) -> str:
    """档案内容哈希（不含时刻）：版本化存档的幂等键。

    与 kb_snapshot_id 不同——后者绑定「as_of(T) 时刻」用于决策卡复现；
    存档版本只关心「内容是否变了」，同一内容不得重复出版本。
    """
    import hashlib

    now = datetime.now(UTC)
    view = kb.view(kind, entity_id, now, namespace=namespace)
    canonical = json.dumps(
        {
            field: {
                "value": rec.value,
                "version": rec.version,
                "evidence_ids": sorted(rec.evidence_ids),
                "knowledge_time": rec.knowledge_time.isoformat(),
            }
            for field, rec in sorted(view.items())
        },
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def render_profile_html(
    kb: BitemporalStore,
    kind: str,
    entity_id: str,
    *,
    namespace: str = "prod",
    prices: list[dict] | None = None,
    generated_at: datetime | None = None,
) -> str:
    """渲染当前档案投影为自包含 HTML（as_of = 生成时刻）。"""
    now = generated_at or datetime.now(UTC)
    view = kb.view(kind, entity_id, now, namespace=namespace)
    snap = _content_hash(kb, kind, entity_id, namespace)
    e = html.escape

    # facts 表（证据锚点 hover）
    rows = []
    for field, rec in sorted(view.items()):
        ev_cells = []
        for eid in rec.evidence_ids:
            try:
                ev = kb.get_evidence(eid)
                tip = (
                    f"{eid} · {ev.source_id}\\A"
                    f"「{ev.verbatim_quote[:400]}」\\A"
                    f"available {ev.available_at.date() if ev.available_at else '?'} · PIT-{ev.pit_grade.value}"
                )
                ev_cells.append((eid, tip))
            except Exception:
                ev_cells.append((eid, f"{eid}（缺失）"))
        value_html = f'<span class="ev" data-ev="{e(chr(10).join(t for _, t in ev_cells))}">{_esc(rec.value)}</span>' if ev_cells else _esc(rec.value)
        neg = ""
        conflict = f' <span class="conflict">⚠冲突 v{rec.version}</span>' if rec.conflict_flag else ""
        rows.append(
            f'<tr><td class="mono">{e(field)}</td><td class="num"{neg}>{value_html}{conflict}</td>'
            f'<td class="mono">{rec.event_time.date() if rec.event_time else "—"}</td>'
            f'<td class="mono">{rec.knowledge_time.date()}</td>'
            f'<td class="mono">v{rec.version}</td></tr>'
        )

    thesis_html = ""
    if "thesis" in view:
        thesis_html = f'<h2>投资论点（thesis v{view["thesis"].version}）</h2><div class="thesis">{e(str(view["thesis"].value))}</div>'

    # 时间线（按 knowledge_time 排序的事实首次/变更）
    timeline_rows = []
    for field, rec in sorted(view.items(), key=lambda kv: kv[1].knowledge_time):
        timeline_rows.append(
            f'<tr><td class="mono">{rec.knowledge_time.date()}</td>'
            f"<td>{e(field)} → {_esc(rec.value)}（v{rec.version}）</td></tr>"
        )

    chart_html = ""
    if prices:
        svg = _price_svg(prices)
        if svg:
            chart_html = (
                "<h2>价格走势（日线）</h2>"
                f'<div class="chart">{svg}</div>'
                '<div class="cap">数据：DataGateway · 日线 · available_at = 交易日 +1d · PIT-A</div>'
            )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{e(kind)}:{e(entity_id)} 档案存档 · 内容哈希 {e(snap)}</title>
<style>{_CSS}
.ev {{ position: relative; }}
.ev:hover::after {{ content: attr(data-ev); white-space: pre-wrap; position: absolute; left: 0; top: 130%;
  z-index: 5; background: #1a1a1a; color: #fff; font-size: 11px;
  font-family: ui-monospace, Menlo, monospace; padding: 8px 10px; border-radius: 6px;
  width: 340px; line-height: 1.5; }}</style></head>
<body>
<header class="doc">
  <h1>{e(entity_id)} <span class="mono" style="font-size:14px;color:#666">{e(kind)}</span></h1>
  <div class="meta">内容哈希 {e(snap)} · 生成于 {now.isoformat()}（knowledge_time ≤ 此时刻）
    · 事实 {len(view)} 条</div>
</header>
{thesis_html}
{chart_html}
<h2>核心事实（hover 数字看证据原文）</h2>
<table><tr><th>字段</th><th>值</th><th>event_time</th><th>knowledge_time</th><th>版本</th></tr>
{"".join(rows)}
</table>
<h2>变更时间线</h2>
<table><tr><th>knowledge_time</th><th>事实变更</th></tr>
{"".join(timeline_rows)}
</table>
<footer class="doc">finance-agent · ProfileRenderer · 本文件是 EventStore/双时态 KB 的投影，
内容哈希 {e(snap)}（同内容不重复出版本） · 数字未经验证改写即弃用（numeric-guard）</footer>
</body></html>"""


def maybe_archive(
    kb: BitemporalStore,
    knowledge_dir: Path,
    kind: str,
    entity_id: str,
    *,
    namespace: str = "prod",
    prices: list[dict] | None = None,
) -> Path | None:
    """档案有变化才生成新版本存档；返回路径（无变化 → None，不重复写）。"""
    snap = _content_hash(kb, kind, entity_id, namespace)
    archive_dir = knowledge_dir / f"{kind}s" / entity_id / "archive"
    target = archive_dir / f"{snap}.html"
    if target.exists():
        return None
    html_text = render_profile_html(
        kb, kind, entity_id, namespace=namespace, prices=prices, generated_at=datetime.now(UTC)
    )
    archive_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(html_text, encoding="utf-8")
    latest = archive_dir / "latest.html"
    latest.write_text(html_text, encoding="utf-8")  # 写文件而非软链（跨平台稳）
    return target
