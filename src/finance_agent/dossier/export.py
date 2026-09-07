"""冻结导出渲染（设计 §11.4 第一期：JSON + Markdown）。

导出与页面同源：消费同一 DossierSnapshot payload，绑定 as_of/生成时间/data_hash；
Markdown 里数值只来自 typed 观测/计算引用（不从文本猜数）。
HTML 导出在组件契约稳定后加入（复用 ReportDocument block 契约）。
"""

from __future__ import annotations

from typing import Any

from ..knowledge.metric_store import MetricStore
from ..knowledge.store import BitemporalStore


def render_snapshot_markdown(
    snap: dict[str, Any], kb: BitemporalStore, metrics: MetricStore
) -> str:
    entity = snap["entity"]
    ctx = snap["context"]
    lines: list[str] = [
        f"# {entity.get('name') or entity['id']} 研究档案（冻结导出）",
        "",
        f"- 实体: {entity['kind']}:{entity['id']}",
        f"- 模式: {ctx['mode']} · namespace: {ctx['namespace']}",
        f"- 知识截止（as_of）: {ctx['as_of']}",
        f"- 生成时间: {ctx['generated_at']}",
        f"- 快照: {ctx.get('snapshot_id', '')} · data_hash: {snap.get('data_hash', '')}",
        f"- 配方: {snap.get('recipe', {}).get('id', '')}@{snap.get('recipe', {}).get('version', '')}",
        "",
    ]
    summary = snap.get("summary", {})
    lines.append("## 研究结论")
    lines.append("")
    kind_note = {
        "claim": "已验证论断", "draft": "研究草稿（未验证）",
        "legacy_analysis": "旧 thesis 兼容投影（legacy analysis，非披露事实）",
        "none": "无",
    }.get(summary.get("thesis_kind", "none"), "")
    lines.append(f"{summary.get('thesis') or '（无研究结论——先补研）'}  \n_来源类别: {kind_note}_")
    if summary.get("key_changes"):
        lines.append("")
        lines.append("**最近变化**：")
        lines.extend(f"{i}. {c}" for i, c in enumerate(summary["key_changes"], 1))
    if summary.get("counter_evidence"):
        lines.append("")
        lines.append(f"**最大反证**：{summary['counter_evidence']}")
    lines.append("")
    lines.append("## 关键指标")
    lines.append("")
    lines.append("| 指标 | 值 | 单位 | 期间 | 性质 | 状态 |")
    lines.append("|---|---|---|---|---|---|")
    for m in summary.get("key_metrics", []):
        value = m.get("value") or "—"
        cur = f" {m['currency']}" if m.get("currency") else ""
        lines.append(
            f"| {m.get('label', m.get('metric_key'))} | {value}{cur} | {m.get('unit', '')} "
            f"| {m.get('period_label', '')} | {m.get('nature', '')} | {m.get('status', '')} |"
        )
    lines.append("")
    lines.append("## 模块状态")
    lines.append("")
    for mod, state in snap.get("modules", {}).items():
        reasons = f"（{'；'.join(state.get('reasons', []))}）" if state.get("reasons") else ""
        lines.append(f"- **{state.get('title', mod)}** [{mod}]: {state.get('status')}{reasons}")
    lines.append("")
    research = snap.get("research", {})
    lines.append("## 研究覆盖")
    lines.append("")
    lines.append(
        f"- 已回答关键问题: {research.get('answered', 0)} / {research.get('required', 0)}"
    )
    if research.get("artifact_refs"):
        lines.append(f"- 研究产物: {', '.join(research['artifact_refs'])}")
    lines.append("")
    # 已验证论断明细
    claims = _validated_claims(metrics, entity, ctx)
    if claims:
        lines.append("## 论断明细")
        lines.append("")
        for c in claims:
            status = c.get("status", "")
            lines.append(f"- [{status}] {c.get('statement', '')}")
            if c.get("support_refs"):
                lines.append(f"  - 支持: {', '.join(c['support_refs'])}")
            if c.get("counter_refs"):
                lines.append(f"  - 反方: {', '.join(c['counter_refs'])}")
            if c.get("limitations"):
                lines.append(f"  - 限制: {'；'.join(c['limitations'])}")
        lines.append("")
    lines.append("## 来源")
    lines.append("")
    for eid in snap.get("evidence_refs", [])[:50]:
        try:
            ev = kb.get_evidence(eid)
            avail = ev.available_at.isoformat() if ev.available_at else "未知"
            lines.append(
                f"- [{eid}] 「{ev.verbatim_quote[:80]}…」 — {ev.url or ev.source_id}"
                f"（available {avail} · PIT-{ev.pit_grade.value}）"
            )
        except Exception:
            lines.append(f"- [{eid}] ⚠ 不可解析")
    lines.append("")
    if snap.get("decision_refs"):
        lines.append(f"## 关联决策: {', '.join(snap['decision_refs'])}（详见 Decisions 页）")
        lines.append("")
    if snap.get("limitations"):
        lines.append("## 限制")
        lines.extend(f"- {x}" for x in snap["limitations"])
        lines.append("")
    lines.append(
        "_导出说明：本文件由 DossierSnapshot 渲染（与在线页面同源）；"
        "数值均为十进制字符串引用，不含导出时新生成的情景或建议。_"
    )
    return "\n".join(lines)


def _validated_claims(metrics: MetricStore, entity: dict, ctx: dict) -> list[dict]:
    from datetime import datetime

    t = datetime.fromisoformat(ctx["as_of"])
    return metrics.claims_as_of(
        entity["kind"], entity["id"], t,
        namespace=ctx["namespace"], statuses=("validated", "draft"),
    )
