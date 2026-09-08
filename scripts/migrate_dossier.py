#!/usr/bin/env python
"""档案迁移工具（knowledge-dossier-research-redesign §11.1：先投影，后丰富）。

三个子命令（全部对旧库只读——不合并实体、不删除版本、不改写旧 fact/evidence）：

  inventory   盘点：字段类型、证据缺口、别名、冲突、时间戳、HTML/报告工件 →
              dry-run 清单（JSON + Markdown），标记 needs_normalization 候选
  shadow      影子生成：从既有数据投影 dossier 快照，对照原事实/证据/时态/质量，
              记录 projector 版本与来源 fact_id（对账 manifest）
  apply-typed 兼容映射：只把「单位、期间、来源都能确定」的旧值转为 MetricObservation
              （幂等：legacy_fact_id + mapping_version + metric_key 去重）；
              无法确定的值保持原文 + needs_normalization，留给补研，不批量猜测

用法：
  uv run python scripts/migrate_dossier.py inventory --data-dir data
  uv run python scripts/migrate_dossier.py shadow --data-dir data [--entity stock:BE]
  uv run python scripts/migrate_dossier.py apply-typed --data-dir data [--apply] [--entity stock:BE]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

MAPPING_VERSION = "1"  # 映射配方版本（幂等键的一部分）

#: 明确结构化形态的旧值（dict 带 currency+date）→ 高置信候选
_STRUCTURED_PRICE_KEYS = ("close", "price", "value")
_STRUCTURED_DATE_KEYS = ("trading_date", "date", "as_of")


def _load_stores(data_dir: Path):
    from finance_agent.eventstore.store import EventStore
    from finance_agent.knowledge.metric_store import MetricStore
    from finance_agent.knowledge.store import BitemporalStore

    kb = BitemporalStore(data_dir / "kb.db")
    metrics = MetricStore(data_dir / "metrics.db")
    events = EventStore(data_dir / "events.db")
    return kb, metrics, events


def _entities(kb, namespace: str) -> list[tuple[str, str]]:
    rows = kb._conn.execute(  # noqa: SLF001 - 只读盘点
        "SELECT entity_kind, entity_id FROM facts WHERE namespace = ?"
        " GROUP BY entity_kind, entity_id ORDER BY entity_kind, entity_id",
        (namespace,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


# ---------------- 候选解析（保守：确定不了就标 needs_normalization） ----------------


_SCALE_WORD_RE = re.compile(
    r"(million|billion|thousand|trillion|百万|亿|万|mn|bn)\b", re.IGNORECASE
)


def _parse_accounting_number(text: Any) -> str | None:
    """会计格式数值（'5,130' / '( 1,234 )' / 119.51）→ 十进制字符串；不确定 None。

    含规模词（million/亿…）的文本返回 None——规模换算必须走显式链路，
    迁移不在这里猜量级（宁可标 needs_normalization）。"""
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return repr(text) if isinstance(text, float) else str(text)
    if not isinstance(text, str):
        return None
    s = text.strip()
    if _SCALE_WORD_RE.search(s):
        return None
    m = re.search(r"\(\s*(-?\d[\d,]*\.?\d*)\s*\)", s)  # 会计负数：( 1,234 )
    neg_paren = m is not None
    if m is None:
        m = re.search(r"(-?\d[\d,]*\.?\d*)", s)
    if m is None:
        return None
    core = m.group(1).replace(",", "")
    if not re.fullmatch(r"-?\d+\.?\d*", core):
        return None
    if neg_paren and not core.startswith("-"):
        core = "-" + core
    return core


def classify_fact(field: str, value: Any) -> dict[str, Any]:
    """单个旧字段 → 迁移分类。confidence: high 可自动映射 / review 需人工或补研 /
    text 保持 legacy 文本投影。"""
    out: dict[str, Any] = {"field": field, "confidence": "review", "reason": ""}

    # 1) 结构化价格/市值快照（dict 带 currency + date）→ high
    if isinstance(value, dict):
        value_key = next((k for k in _STRUCTURED_PRICE_KEYS if k in value), None)
        date_key = next((k for k in _STRUCTURED_DATE_KEYS if k in value), None)
        if value_key and date_key and value.get("currency"):
            num = _parse_accounting_number(value[value_key])
            if num:
                out.update({
                    "confidence": "high", "metric_key": _metric_key_of(field),
                    "value": num, "currency": value["currency"],
                    "frequency": "instant", "period_end": str(value[date_key]),
                    "reason": "结构化快照（显式币种+日期）",
                })
                return out
        out["reason"] = "dict 值缺显式币种/日期/数值键——保留 legacy 投影"
        return out
    if isinstance(value, list):
        out["confidence"] = "text"
        out["reason"] = "list 值（结构化清单）——保留 legacy 投影"
        return out

    # 2) 标量：字段名带明确日期（market_cap_2026-07-03 / 市值（2026-08-21））→ instant
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", field)
    num = _parse_accounting_number(value)
    if m and num is not None:
        currency = _currency_hint(field, value)
        if currency:
            out.update({
                "confidence": "high", "metric_key": _metric_key_of(field),
                "value": num, "currency": currency,
                "frequency": "instant", "period_end": f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
                "reason": "字段名含显式日期 + 可解析数值 + 币种提示",
            })
            return out
        out["reason"] = "有日期无数种币种提示——不猜货币（needs_normalization）"
        return out

    # 3) 财年/期间模式（revenue_fy2025 / revenue_h1_2026 / revenue_q1_2026）→ review：
    #    期间可从字段名推断，但财年是否等于自然年、单位与币种无法从旧数据确定——
    #    不批量猜测（§11.1「无法确定的值显示原文和 needs_normalization」）
    if num is not None and re.search(r"(fy|q[1-4]|h[12]|ttm)?_?(19|20)\d{2}", field, re.I):
        unit_hint = _unit_hint(field)
        out.update({
            "confidence": "review",
            "metric_key": _metric_key_of(field),
            "value": num,
            "reason": (
                "期间可由字段名推断，但财年口径/单位不确定"
                + (f"（单位提示 {unit_hint}）" if unit_hint else "")
                + "——留待补研确认，不自动映射"
            ),
        })
        return out
    if num is not None:
        out["reason"] = "数值可解析但期间不可确定（needs_normalization）"
        return out
    out["confidence"] = "text"
    out["reason"] = "文本/散文——由兼容 projector 投影为 LegacySection"
    return out


def _metric_key_of(field: str) -> str:
    base = re.split(r"[_（(]", field)[0].strip().lower()
    aliases = {
        "revenue": "revenue", "收入": "revenue", "net": "net_income", "盈利": "net_income",
        "cash": "cash", "现金": "cash", "market": "market_cap", "市值": "market_cap",
        "capex": "capex", "shares": "shares_outstanding", "receivables": "receivables",
        "valuation": "valuation_snapshot", "rd": "rd_intensity",
    }
    return aliases.get(base, base or field.lower())


def _currency_hint(field: str, value: Any) -> str | None:
    """币种提示（review #31）：具体符号优先于通用 $——HK$ 不得被当成 USD。

    顺序：HK$ → US$ → 裸 $（低置信 USD）→ 中文币种词。"""
    text = f"{field} {value}".lower()
    if "hk$" in text or "hkd" in text or "港币" in text or "港元" in text:
        return "HKD"
    if "us$" in text or "usd" in text or "美元" in text:
        return "USD"
    if "cny" in text or "rmb" in text or "人民币" in text:
        return "CNY"
    if "$" in text:
        return "USD"  # 裸 $ 默认美元（已排除 HK$/US$ 先行匹配）
    if "元" in text:
        return "CNY"
    return None


def _unit_hint(field: str) -> str | None:
    low = field.lower()
    if "_usd_m" in low or "usd_m" in low:
        return "USD millions"
    if low.endswith("_m"):
        return "millions?"
    return None


# ---------------- inventory ----------------


def cmd_inventory(args) -> int:
    kb, metrics, events = _load_stores(Path(args.data_dir))
    from finance_agent.knowledge.gaps import GapAnalyzer
    from finance_agent.knowledge.schema import SCHEMAS

    analyzer = GapAnalyzer(kb)
    now = datetime.now(UTC)
    report: dict[str, Any] = {
        "generated_at": now.isoformat(), "mapping_version": MAPPING_VERSION,
        "namespace": args.namespace, "entities": [],
    }
    knowledge_dir = Path(args.data_dir) / "knowledge"
    for kind, eid in _entities(kb, args.namespace):
        view = kb.view(kind, eid, now, namespace=args.namespace)
        gaps = analyzer.analyze(kind, eid, now, namespace=args.namespace)
        candidates, needs_norm, texts = [], [], []
        for field, rec in sorted(view.items()):
            c = classify_fact(field, rec.value)
            c["fact_id"] = rec.fact_id
            c["knowledge_time"] = rec.knowledge_time.isoformat()
            c["conflict"] = rec.conflict_flag
            missing_ev = [e for e in rec.evidence_ids if not _evidence_exists(kb, e)]
            c["missing_evidence"] = missing_ev
            schema_fields = set(SCHEMAS.get(kind, SCHEMAS["stock"]).required) | set(
                SCHEMAS.get(kind, SCHEMAS["stock"]).optional)
            if field not in schema_fields and c["confidence"] != "text":
                candidates.append(c)
            if c["confidence"] == "review" or missing_ev:
                needs_norm.append({"field": field, "reason": c["reason"],
                                   "missing_evidence": missing_ev})
            if c["confidence"] == "text":
                texts.append(field)
        archive_dir = knowledge_dir / f"{kind}s" / eid / "archive"
        report["entities"].append({
            "entity": f"{kind}:{eid}",
            "field_count": len(view),
            "completeness": gaps.completeness,
            "open_conflicts": gaps.conflicts,
            "stale": gaps.stale,
            "typed_candidates_high": [c for c in candidates if c["confidence"] == "high"],
            "typed_candidates_review": [c for c in candidates if c["confidence"] == "review"],
            "needs_normalization": needs_norm,
            "legacy_text_fields": texts,
            "has_html_archive": archive_dir.exists() and bool(list(archive_dir.glob("*.html"))),
            "observation_count": len(
                metrics.observations_as_of(kind, eid, now, namespace=args.namespace)
            ),
        })
    out_dir = Path(args.data_dir) / "migration"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"inventory-{stamp}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_dir / f"inventory-{stamp}.md"
    md_path.write_text(_inventory_markdown(report), encoding="utf-8")
    print(f"[inventory] {len(report['entities'])} 个实体 → {json_path}")
    print(f"[inventory] 摘要 → {md_path}")
    total_high = sum(len(e["typed_candidates_high"]) for e in report["entities"])
    total_review = sum(len(e["typed_candidates_review"]) for e in report["entities"])
    print(f"[inventory] 高置信 typed 候选 {total_high}；待复核 {total_review}（不自动映射）")
    return 0


def _evidence_exists(kb, evidence_id: str) -> bool:
    try:
        kb.get_evidence(evidence_id)
        return True
    except Exception:
        return False


def _inventory_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 档案迁移盘点（dry-run，不写库）",
        "",
        f"- 生成时间: {report['generated_at']}",
        f"- 映射版本: {report['mapping_version']}",
        f"- 实体数: {len(report['entities'])}",
        "",
        "| 实体 | 字段 | 完整度 | 冲突 | typed 候选(高/复核) | needs_norm | HTML 存档 | 观测数 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for e in report["entities"]:
        lines.append(
            f"| {e['entity']} | {e['field_count']} | {e['completeness']:.0%} "
            f"| {len(e['open_conflicts'])} "
            f"| {len(e['typed_candidates_high'])}/{len(e['typed_candidates_review'])} "
            f"| {len(e['needs_normalization'])} | {'✓' if e['has_html_archive'] else '✗'} "
            f"| {e['observation_count']} |"
        )
    lines.append("")
    lines.append("## 待复核候选明细（不自动映射的原因）")
    for e in report["entities"]:
        if not e["typed_candidates_review"] and not e["needs_normalization"]:
            continue
        lines.append(f"\n### {e['entity']}")
        for c in e["typed_candidates_review"]:
            lines.append(f"- `{c['field']}` → {c.get('metric_key')}: {c['reason']}")
        for n in e["needs_normalization"]:
            if n["missing_evidence"]:
                lines.append(f"- `{n['field']}`: 证据缺失 {n['missing_evidence']}")
    return "\n".join(lines) + "\n"


# ---------------- shadow ----------------


def cmd_shadow(args) -> int:
    from finance_agent.dossier.projector import DossierProjector
    from finance_agent.dossier.service import DossierService

    data_dir = Path(args.data_dir)
    kb, metrics, events = _load_stores(data_dir)
    projector = DossierProjector(kb=kb, metrics=metrics)
    service = DossierService(kb=kb, metrics=metrics, projector=projector, events=events)
    targets = _entities(kb, args.namespace)
    if args.entity:
        kind, eid = args.entity.split(":", 1)
        targets = [(kind, eid)]
    now = datetime.now(UTC)
    out_dir = data_dir / "migration" / f"shadow-{now.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"generated_at": now.isoformat(), "projector_version": "1", "snapshots": []}
    failed = 0
    for kind, eid in targets:
        try:
            snap, created = service.open(kind, eid, namespace=args.namespace,
                                         run_id="migration-shadow")
            view = kb.view(kind, eid, snap["context"]["as_of"] and
                           datetime.fromisoformat(snap["context"]["as_of"]),
                           namespace=args.namespace)
            entry = {
                "entity": f"{kind}:{eid}",
                "snapshot_id": snap["context"]["snapshot_id"],
                "data_hash": snap["data_hash"],
                "created": created,
                "source_fact_ids": sorted(r.fact_id for r in view.values()),
                "module_status": {m: s["status"] for m, s in snap["modules"].items()},
                "evidence_count": len(snap["evidence_refs"]),
            }
            manifest["snapshots"].append(entry)
            (out_dir / f"{kind}-{eid}.json").write_text(
                json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # 失败可见：记入 manifest，不中断其余实体
            failed += 1
            manifest["snapshots"].append({
                "entity": f"{kind}:{eid}", "error": f"{type(e).__name__}: {e}",
            })
            print(f"[shadow] ✗ {kind}:{eid}: {type(e).__name__}: {e}", file=sys.stderr)
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = len(manifest["snapshots"]) - failed
    print(f"[shadow] 成功 {ok}，失败 {failed} → {out_dir}")
    return 1 if failed and ok == 0 else 0


# ---------------- apply-typed ----------------


def cmd_apply_typed(args) -> int:
    from finance_agent.harness.manifest import RunManifest, RunMode
    from finance_agent.knowledge.metric_writer import TypedMetricWriter
    from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation

    data_dir = Path(args.data_dir)
    kb, metrics, events = _load_stores(data_dir)
    writer = TypedMetricWriter(store=metrics, kb=kb, events=events)
    run = RunManifest(run_id="kb-migration", mode=RunMode.LIVE)
    checkpoint_path = data_dir / "migration" / "apply-typed-checkpoint.json"
    checkpoint: dict[str, Any] = {"applied": {}, "mapping_version": MAPPING_VERSION}
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("mapping_version") != MAPPING_VERSION:
            print(f"[apply-typed] 映射版本变化（{checkpoint.get('mapping_version')} → "
                  f"{MAPPING_VERSION}），重新盘点（幂等键含版本）")
            checkpoint = {"applied": {}, "mapping_version": MAPPING_VERSION}
    now = datetime.now(UTC)
    targets = _entities(kb, args.namespace)
    if args.entity:
        kind, eid = args.entity.split(":", 1)
        targets = [(kind, eid)]
    applied = skipped = rejected = 0
    for kind, eid in targets:
        view = kb.view(kind, eid, now, namespace=args.namespace)
        for field, rec in sorted(view.items()):
            c = classify_fact(field, rec.value)
            if c["confidence"] != "high":
                continue
            dedup_key = f"{rec.fact_id}:{MAPPING_VERSION}:{c['metric_key']}"
            if dedup_key in checkpoint["applied"]:
                skipped += 1
                continue  # 幂等：断点恢复不重复写
            period_end = date.fromisoformat(c["period_end"])
            try:
                obs = ReportedObservation(
                    entity_kind=kind, entity_id=eid,  # type: ignore[arg-type]
                    metric_key=c["metric_key"],
                    period=MetricPeriod(end=period_end, frequency=c["frequency"],
                                        fiscal_label=c.get("period_label", "")),
                    dimensions={"legacy_field": field},
                    basis="operating_metric",
                    value=c["value"], unit=c["currency"], currency=c["currency"],
                    raw=RawValue(
                        value_text=str(rec.value.get(next(
                            k for k in _STRUCTURED_PRICE_KEYS if k in rec.value
                        )) if isinstance(rec.value, dict) else rec.value),
                        unit_text=c["currency"], quote_ref=rec.evidence_ids[0],
                    ),
                    evidence_refs=list(rec.evidence_ids),
                    knowledge_time=rec.knowledge_time,
                    source_available_at=rec.knowledge_time,
                    retrieved_at=now, created_at=now,
                    pit_grade="B",  # 迁移值：旧库时态可信但非原始披露定位（不冒充 A）
                    run_id=run.run_id,
                    note=f"legacy 迁移自 {field}（mapping v{MAPPING_VERSION}）",
                )
                if args.apply:
                    writer.write_observation(obs, run=run, namespace=args.namespace)
                applied += 1
                checkpoint["applied"][dedup_key] = {
                    "entity": f"{kind}:{eid}", "field": field,
                    "metric_key": c["metric_key"], "at": now.isoformat(),
                }
            except Exception as e:
                rejected += 1
                print(f"[apply-typed] ✗ {kind}:{eid} {field}: {type(e).__name__}: {e}",
                      file=sys.stderr)
    if args.apply:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[apply-typed] {mode}: 可映射 {applied}，已处理跳过 {skipped}，拒绝 {rejected}"
          + ("" if args.apply else "（加 --apply 落库）"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--namespace", default="prod")
    parser.add_argument("--entity", default=None, help="kind:id（缺省 = 全部实体）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventory", help="只读盘点 → dry-run 清单")
    sub.add_parser("shadow", help="影子快照生成 + 对账 manifest")
    apply_p = sub.add_parser("apply-typed", help="高置信候选 → typed 观测（默认 dry-run）")
    apply_p.add_argument("--apply", action="store_true", help="真正落库（缺省 dry-run）")
    args = parser.parse_args(argv)
    if args.cmd == "inventory":
        return cmd_inventory(args)
    if args.cmd == "shadow":
        return cmd_shadow(args)
    return cmd_apply_typed(args)


if __name__ == "__main__":
    sys.exit(main())
