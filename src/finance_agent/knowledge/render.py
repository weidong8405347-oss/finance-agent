"""ProfileRenderer：档案的 HTML 存档投影（redesign §3.8 / D8，R4 重做为 dashboard 式）。

设计参照 open-design 的 live-artifact / live-dashboard 契约（single-page、self-contained、
数据与呈现分离、provenance 内联）：
- 自包含单文件 HTML（内联样式 + 内联 SVG 图表，无 JS/无外部依赖）：
  可离线打开、可 diff、可审计；
- header 带验收状态 pill（verify 准入投影：✓ 已验收 / ◔ 待验收 / ⚠ 冲突）——
  「完整度 100% 但内容空」的事故级别问题，状态一眼可见；
- KPI 卡网格（1px hairline、tabular-nums）：每实体类型的 headline 字段 + 版本/可知时刻；
- 字段分区渲染：结构化 list → 表格、长文本 → 轻量 markdown 排版——
  不再是 JSON.stringify 一坨；
- 每个字段带证据锚点（hover 出原文摘录 + available_at + PIT 等级）；
- provenance footer：来源清单 + PIT 分布 + 内容哈希；
- 版本化：档案有变化时写 knowledge/<kind>s/<id>/archive/<snapshot>.html，
  并更新 latest.html；历史版本永不删除（as_of 时光机的磁盘对应物）。
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .store import BitemporalStore
from .verify import EntityQuality, looks_like_serialized_json, verify_entity

_CSS = """
:root { color-scheme: light; }
body { font: 14px/1.7 -apple-system, "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif;
       color: #1a1a1a; max-width: 880px; margin: 0 auto; padding: 32px 28px 80px;
       background: #fff; font-variant-numeric: tabular-nums; }
.mono { font-family: ui-monospace, Menlo, monospace; }
header.doc { border-bottom: 1px solid #1a1a1a; padding-bottom: 16px; margin-bottom: 20px; }
header.doc h1 { font-size: 26px; margin: 0; letter-spacing: -0.01em; font-weight: 700; }
header.doc .kind { font-family: ui-monospace, Menlo, monospace; font-size: 12px;
                   color: #666; margin-left: 8px; }
header.doc .meta { font-family: ui-monospace, Menlo, monospace; font-size: 11px;
                   color: #777; margin-top: 8px; }
.title-row { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px; }
.pill { display: inline-block; font-size: 11px; font-family: ui-monospace, Menlo, monospace;
        padding: 2px 10px; border-radius: 999px; border: 1px solid; line-height: 1.6; }
.pill.ok { color: #15803d; border-color: #86efac; background: #f0fdf4; }
.pill.draft { color: #b45309; border-color: #fcd34d; background: #fffbeb; }
.pill.conflict { color: #b91c1c; border-color: #fca5a5; background: #fef2f2; }
.pill.stale { color: #6b7280; border-color: #d1d5db; background: #f9fafb; }
.callout { border: 1px solid #e5e5e5; border-left: 3px solid #b45309; border-radius: 4px;
           padding: 8px 14px; font-size: 12.5px; color: #555; margin: 14px 0; }
.callout.ok { border-left-color: #15803d; color: #666; }
h2 { font-size: 13px; margin: 30px 0 10px; text-transform: uppercase; letter-spacing: 0.08em;
     color: #888; font-weight: 600; }
h3, h4, h5, h6 { font-size: 14px; margin: 14px 0 6px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 8px 0; }
td, th { border-bottom: 1px solid #ececec; padding: 6px 8px; text-align: left;
         vertical-align: top; }
th { font-size: 11px; color: #888; font-weight: 600; text-transform: uppercase;
     letter-spacing: 0.05em; }
.num { font-family: ui-monospace, Menlo, monospace; font-size: 12.5px; }
.ev { border-bottom: 1px dotted #999; cursor: help; }
.ev:hover { background: #fffbe6; }
.thesis { background: #fafaf7; border: 1px solid #e8e6df; border-radius: 8px;
          padding: 16px 20px; font-size: 13.5px; }
.thesis p { margin: 6px 0; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 0; border: 1px solid #e5e5e5; border-radius: 8px; overflow: hidden; }
.kpi { padding: 12px 16px; border-right: 1px solid #ececec; border-bottom: 1px solid #ececec; }
.kpi .k-label { font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 0.05em; }
.kpi .k-value { font-size: 18px; font-weight: 600; letter-spacing: -0.01em; margin: 2px 0;
                font-variant-numeric: tabular-nums; word-break: break-all; }
.kpi .k-meta { font-family: ui-monospace, Menlo, monospace; font-size: 10px; color: #999; }
.field { border-top: 1px solid #ececec; padding: 14px 0; }
.field .f-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.field .f-name { font-family: ui-monospace, Menlo, monospace; font-size: 13px; font-weight: 600; }
.field .f-meta { font-family: ui-monospace, Menlo, monospace; font-size: 10px; color: #999; }
.field .f-body { margin-top: 6px; font-size: 13.5px; }
.field .f-body p { margin: 6px 0; }
.field .f-body ul, .field .f-body ol { margin: 6px 0; padding-left: 22px; }
.field .f-issues { font-size: 11px; color: #b45309; font-family: ui-monospace, Menlo, monospace; }
.cap { font-size: 11px; color: #777; font-family: ui-monospace, Menlo, monospace;
      margin-bottom: 14px; }
.conflict { color: #b45309; font-family: ui-monospace, Menlo, monospace; font-size: 11px; }
footer.doc { margin-top: 44px; border-top: 1px solid #ddd; padding-top: 12px; font-size: 11px;
             color: #888; font-family: ui-monospace, Menlo, monospace; }
footer.doc .prov { margin: 4px 0; }
"""

#: KPI 卡网格的 headline 字段（按实体类型，顺序即展示顺序；只放数值锚点字段，
#: 定性字段的数字没有 headline 意义）
_KPI_FIELDS: dict[str, tuple[str, ...]] = {
    "stock": ("revenue_fy", "net_income_fy", "cash_flow", "valuation"),
    "industry": ("market_size", "growth_rate"),
}

#: 字段名 → 中文标签（KPI 卡与字段分区用）
_FIELD_LABELS: dict[str, str] = {
    "revenue_fy": "营收", "net_income_fy": "净利润", "cash_flow": "现金流",
    "valuation": "估值", "business_model": "商业模式", "moat": "护城河",
    "risks": "风险", "peers": "同行", "management": "管理层",
    "catalysts": "催化剂", "counter_evidence": "反方证据",
    "future_space": "未来空间", "market_share": "市场份额", "talent_density": "人才密度",
    "market_size": "市场规模", "growth_rate": "增速", "value_chain": "价值链",
    "competition": "竞争格局", "policy": "政策", "sub_sectors": "子赛道",
    "player_landscape": "标的池", "thesis": "投资论点",
}

_NUM_WITH_UNIT = re.compile(
    r"-?\d[\d,]*\.?\d*\s*(?:万亿|亿|万|%|B|M|K|billion|million|trillion|美元|港元|元|倍|港元/股)"
)
_ANY_NUM = re.compile(r"-?\d[\d,]*\.?\d*")
_YEAR = re.compile(r"^(19|20)\d{2}$")


def _join_scalars(v: list) -> str:
    """标量列表 → 顿号连接（表格单元格内联展示）。"""
    return html.escape("、".join(str(x) for x in v))


def _esc(v: object) -> str:
    return html.escape(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v)


def _label(field: str) -> str:
    return _FIELD_LABELS.get(field, field)


# ---------------- 轻量 markdown 排版（服务端渲染，无 JS） ----------------

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_UL_ITEM = re.compile(r"^[-*•]\s+(.*)$")
_OL_ITEM = re.compile(r"^\d+[.、)]\s*(.*)$")


def _inline(text: str) -> str:
    s = html.escape(text)
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    s = _CODE.sub(r'<code class="mono">\1</code>', s)
    return s


def _md(text: str) -> str:
    """极简 markdown：标题/粗体/行内代码/无序+有序列表/段落。先转义再套样式，注入安全。"""
    out: list[str] = []
    open_list: str | None = None

    def close_list() -> None:
        nonlocal open_list
        if open_list:
            out.append(f"</{open_list}>")
            open_list = None

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            close_list()
            continue
        m = _HEADING.match(line)
        if m:
            close_list()
            level = min(len(m.group(1)) + 3, 6)  # 存档内字段正文从 h4 起，避免喧宾夺主
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue
        m = _UL_ITEM.match(line)
        if m:
            if open_list != "ul":
                close_list()
                out.append("<ul>")
                open_list = "ul"
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue
        m = _OL_ITEM.match(line)
        if m:
            if open_list != "ol":
                close_list()
                out.append("<ol>")
                open_list = "ol"
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue
        close_list()
        out.append(f"<p>{_inline(line)}</p>")
    close_list()
    return "".join(out)


def _render_value(value: Any) -> str:
    """字段值 → HTML：字符串走 markdown-lite；list[dict] → 表格；list → ul；dict → kv 表。

    存量兼容：硬门禁上线前落库的「JSON 字符串」值（value_chain/sub_sectors 等）
    在渲染层解析回结构化再排版——历史不可改写（append-only），但投影可以美化。
    """
    if isinstance(value, str):
        if looks_like_serialized_json(value):
            try:
                return _render_value(json.loads(value))
            except (json.JSONDecodeError, ValueError):
                pass  # 解析失败按普通文本渲染
        return _md(value)
    if isinstance(value, list):
        if not value:
            return '<span class="cap">（空列表）</span>'
        if all(isinstance(v, dict) for v in value):
            keys: list[str] = []
            for item in value:
                for k in item:
                    if k != "evidence_ids" and k not in keys:
                        keys.append(k)
            head = "".join(f"<th>{html.escape(str(k))}</th>" for k in keys)
            rows = []
            for item in value:
                cells = "".join(
                    f"<td>{_render_cell(item.get(k))}</td>" for k in keys
                )
                rows.append(f"<tr>{cells}</tr>")
            return f"<table><tr>{head}</tr>{''.join(rows)}</table>"
        items = "".join(f"<li>{_render_cell(v)}</li>" for v in value)
        return f"<ul>{items}</ul>"
    if isinstance(value, dict):
        rows = "".join(
            f'<tr><td class="mono">{html.escape(str(k))}</td><td>{_render_cell(v)}</td></tr>'
            for k, v in value.items()
        )
        return f"<table>{rows}</table>"
    return f'<span class="num">{_esc(value)}</span>'


def _render_cell(v: Any) -> str:
    if v is None:
        return '<span class="cap">—</span>'
    if isinstance(v, bool):
        return "✓" if v else "—"
    if isinstance(v, str):
        if looks_like_serialized_json(v):
            try:
                return _render_value(json.loads(v))
            except (json.JSONDecodeError, ValueError):
                pass
        return _md(v) if "\n" in v else _inline(v)
    if isinstance(v, (int, float)):
        return f'<span class="num">{v:,}</span>'
    if isinstance(v, dict):
        return _render_value(v)
    if isinstance(v, list):
        if all(not isinstance(x, (dict, list)) for x in v):
            return _join_scalars(v)
        return _render_value(v)
    compact = html.escape(json.dumps(v, ensure_ascii=False))
    return f'<span class="mono" style="font-size:11px">{compact}</span>'


def _headline(value: Any, *, max_len: int = 40) -> str:
    """字段值 → KPI 卡 headline：优先提取首个带单位的数字（散文里的年份不算数），
    其次首个非年份数字，否则截断文本。"""
    if isinstance(value, (int, float)):
        return f"{value:,}"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    m = _NUM_WITH_UNIT.search(text)
    if m:
        return m.group(0).strip()
    for m in _ANY_NUM.finditer(text):
        if not _YEAR.match(m.group(0)):
            return m.group(0)
    text = text.strip()
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _price_svg(prices: list[dict], width: int = 820, height: int = 200) -> str:
    """收盘价序列 → 内联 SVG 折线（服务端渲染，无 JS）。"""
    closes = [float(p["close"]) for p in prices if p.get("close") is not None]
    if len(closes) < 2:
        return ""
    lo, hi = min(closes), max(closes)
    rng = (hi - lo) or 1.0
    n = len(closes)
    pad_l, pad_b, pad_t, pad_r = 48, 24, 16, 16
    baseline = height - pad_b
    xs = [pad_l + i * ((width - pad_l - pad_r) / (n - 1)) for i in range(n)]
    ys = [pad_t + (1 - (c - lo) / rng) * (height - pad_t - pad_b) for c in closes]
    poly = " ".join(f"{x:.0f},{y:.0f}" for x, y in zip(xs, ys, strict=True))
    area = f"{pad_l},{baseline} " + poly + f" {xs[-1]:.0f},{baseline}"
    first, last = prices[0].get("date", ""), prices[-1].get("date", "")
    e = html.escape
    return f"""<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
<rect width="{width}" height="{height}" fill="#fff"/>
<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{baseline}" stroke="#eee"/>
<line x1="{pad_l}" y1="{baseline}" x2="{width - pad_r}" y2="{baseline}" stroke="#eee"/>
<g font-family="ui-monospace,Menlo,monospace" font-size="9" fill="#aaa">
<text x="8" y="{baseline + 4}">{lo:.2f}</text><text x="8" y="{pad_t + 8}">{hi:.2f}</text>
<text x="{pad_l + 4}" y="{height - 4}">{e(str(first))}</text>
<text x="{width - 100}" y="{height - 4}">{e(str(last))}</text></g>
<polygon fill="#1a1a1a" fill-opacity="0.06" stroke="none" points="{area}"/>
<polyline fill="none" stroke="#1a1a1a" stroke-width="1.5" points="{poly}"/>
<circle cx="{xs[-1]:.0f}" cy="{ys[-1]:.0f}" r="3" fill="#1a1a1a"/>
<text x="{max(xs[-1]-150,50):.0f}" y="{max(ys[-1]-8,12):.0f}" font-family="ui-monospace,Menlo,monospace"
      font-size="10">{closes[-1]:.2f}</text>
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


def _evidence_tips(kb: BitemporalStore, evidence_ids: list[str]) -> str:
    tips = []
    for eid in evidence_ids:
        try:
            ev = kb.get_evidence(eid)
            available = ev.available_at.date() if ev.available_at else "?"
            tips.append(
                f"{eid} · {ev.source_id}\\A"
                f"「{ev.verbatim_quote[:400]}」\\A"
                f"available {available} · PIT-{ev.pit_grade.value}"
            )
        except Exception:
            tips.append(f"{eid}（缺失）")
    return chr(10).join(tips)


def _status_pills(quality: EntityQuality, conflict_count: int) -> str:
    pills = []
    if quality.status == "verified":
        pills.append('<span class="pill ok">✓ 已验收</span>')
    else:
        pills.append('<span class="pill draft">◔ 待验收</span>')
    if conflict_count:
        pills.append(f'<span class="pill conflict">⚠ {conflict_count} 冲突</span>')
    n_stale = sum(1 for f in quality.fields if f.status == "stale")
    if n_stale:
        pills.append(f'<span class="pill stale">{n_stale} 字段陈旧</span>')
    return "".join(pills)


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
    quality = verify_entity(kb, kind, entity_id, now, namespace=namespace)
    field_status = {f.field: f for f in quality.fields}
    e = html.escape

    conflict_count = sum(1 for f in quality.fields if f.status == "conflict")

    # ---- header ----
    callout = ""
    if quality.status != "verified":
        detail = "；".join(quality.issues) if quality.issues else "尚无事实落库"
        callout = (
            f'<div class="callout">本档案尚未通过验收（verify 准入投影）：{e(detail)}。'
            "内容仅供研究过程参考，不作为决策依据。</div>"
        )

    thesis_html = ""
    if "thesis" in view:
        thesis_html = (
            f'<h2>投资论点（thesis v{view["thesis"].version}）</h2>'
            f'<div class="thesis">{_md(str(view["thesis"].value))}</div>'
        )

    # ---- KPI 卡网格 ----
    kpi_html = ""
    kpi_fields = [f for f in _KPI_FIELDS.get(kind, ()) if f in view]
    if kpi_fields:
        cards = []
        for f in kpi_fields:
            rec = view[f]
            fq = field_status.get(f)
            status_dot = {"ok": "🟢", "weak": "🟡", "stale": "⚪", "conflict": "🔴"}.get(
                fq.status if fq else "ok", "🟢"
            )
            cards.append(
                f'<div class="kpi"><div class="k-label">{e(_label(f))} · {e(f)}</div>'
                f'<div class="k-value">{e(_headline(rec.value))}</div>'
                f'<div class="k-meta">{status_dot} v{rec.version} · 可知 '
                f'{rec.knowledge_time.date()}</div></div>'
            )
        kpi_html = f'<div class="kpis">{"".join(cards)}</div>'

    chart_html = ""
    if prices:
        svg = _price_svg(prices)
        if svg:
            chart_html = (
                "<h2>价格走势（日线）</h2>"
                f'<div class="chart">{svg}</div>'
                '<div class="cap">数据：DataGateway · 日线 · available_at = 交易日 +1d · PIT-A</div>'
            )

    # ---- 字段分区（结构化渲染 + 证据锚点） ----
    sections = []
    ordered_fields = [f for f in view if f != "thesis"]
    # schema 必填字段在前，其余按名字序——阅读顺序 = 重要度
    from .schema import SCHEMAS

    schema = SCHEMAS.get(kind)
    if schema:
        front = [f for f in schema.required if f in ordered_fields]
        mid = [f for f in schema.optional if f in ordered_fields]
        rest = sorted(f for f in ordered_fields if f not in set(front) | set(mid))
        ordered_fields = [*front, *mid, *rest]
    else:
        ordered_fields = sorted(ordered_fields)

    for field in ordered_fields:
        rec = view[field]
        fq = field_status.get(field)
        tips = _evidence_tips(kb, rec.evidence_ids)
        name_html = (
            f'<span class="f-name ev" data-ev="{e(tips)}">{e(_label(field))} · {e(field)}</span>'
            if tips
            else f'<span class="f-name">{e(_label(field))} · {e(field)}</span>'
        )
        meta = (
            f'<span class="f-meta">v{rec.version} · event '
            f'{rec.event_time.date() if rec.event_time else "—"} · 可知 {rec.knowledge_time.date()}'
            f' · {len(rec.evidence_ids)} 证据</span>'
        )
        flags = ""
        if rec.conflict_flag:
            flags += ' <span class="conflict">⚠冲突</span>'
        if fq and fq.issues:
            flags += f' <span class="f-issues">{"；".join(e(i) for i in fq.issues)}</span>'
        sections.append(
            f'<section class="field"><div class="f-head">{name_html}{meta}{flags}</div>'
            f'<div class="f-body">{_render_value(rec.value)}</div></section>'
        )

    # ---- 变更时间线 ----
    timeline_rows = []
    for field, rec in sorted(view.items(), key=lambda kv: kv[1].knowledge_time):
        headline = _headline(rec.value, max_len=60)
        timeline_rows.append(
            f'<tr><td class="mono">{rec.knowledge_time.date()}</td>'
            f"<td>{e(_label(field))} → {e(headline)}（v{rec.version}）</td></tr>"
        )

    # ---- provenance footer ----
    sources: dict[str, int] = {}
    pits: dict[str, int] = {}
    n_evidence = 0
    for rec in view.values():
        for eid in rec.evidence_ids:
            n_evidence += 1
            try:
                ev = kb.get_evidence(eid)
                sources[ev.source_id] = sources.get(ev.source_id, 0) + 1
                pits[ev.pit_grade.value] = pits.get(ev.pit_grade.value, 0) + 1
            except Exception:
                sources["(缺失)"] = sources.get("(缺失)", 0) + 1
    prov_sources = " · ".join(f"{e(s)} ×{n}" for s, n in sorted(sources.items())) or "无"
    prov_pits = " · ".join(f"PIT-{e(g)} ×{n}" for g, n in sorted(pits.items())) or "无"

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{e(kind)}:{e(entity_id)} 档案存档 · 内容哈希 {e(snap)}</title>
<style>{_CSS}
.ev {{ position: relative; }}
.ev:hover::after {{ content: attr(data-ev); white-space: pre-wrap; position: absolute; left: 0; top: 130%;
  z-index: 5; background: #1a1a1a; color: #fff; font-size: 11px;
  font-family: ui-monospace, Menlo, monospace; padding: 8px 10px; border-radius: 6px;
  width: 380px; line-height: 1.5; }}</style></head>
<body>
<header class="doc">
  <div class="title-row">
    <h1>{e(entity_id)}</h1><span class="kind">{e(kind)}</span>
    {_status_pills(quality, conflict_count)}
  </div>
  <div class="meta">内容哈希 {e(snap)} · 生成于 {now.isoformat()}（knowledge_time ≤ 此时刻）
    · 事实 {len(view)} 条 · 质量分 {quality.quality_score:.0%}</div>
</header>
{callout}
{thesis_html}
{kpi_html}
{chart_html}
<h2>核心事实（hover 字段名看证据原文）</h2>
{"".join(sections)}
<h2>变更时间线</h2>
<table><tr><th>knowledge_time</th><th>事实变更</th></tr>
{"".join(timeline_rows)}
</table>
<footer class="doc">
  <div class="prov">provenance · 来源：{prov_sources}</div>
  <div class="prov">PIT 分布：{prov_pits} · 证据 {n_evidence} 条（绑定 {len(view)} 个字段）</div>
  <div class="prov">finance-agent · ProfileRenderer · 本文件是 EventStore/双时态 KB 的投影，
  内容哈希 {e(snap)}（同内容不重复出版本） · 数字未经验证改写即弃用（numeric-guard）</div>
</footer>
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
    latest = archive_dir / "latest.html"
    if target.exists() and latest.exists():
        return None
    created = not target.exists()
    html_text = render_profile_html(
        kb, kind, entity_id, namespace=namespace, prices=prices, generated_at=datetime.now(UTC)
    )
    archive_dir.mkdir(parents=True, exist_ok=True)
    if created:
        target.write_text(html_text, encoding="utf-8")
    latest.write_text(html_text, encoding="utf-8")  # 写文件而非软链（跨平台稳）；缺失即修复
    return target if created else None
