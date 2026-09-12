#!/usr/bin/env python
"""结构产物回收（profile 内容质量升级 §2）：把被旧契约拒掉的结构提交恢复进档案。

事故背景（docs/finance_agent_profile_upgrade_plan.md 落地诊断，2026-09-09）：
synthesize 运行里合成器多次调用 submit_structures，内容完备、引用全部可解析，
却因形状漂移（bottleneck 描述字符串 / relation 中文 / status "pending" /
layers 显示名 / cells 数组）被旧 fail-closed 契约整批拒绝；步数预算烧光后
产物冻结 structures=[] —— 行业页面退化为占位符与长文本。

本脚本是**数据回收**而不是内容生成：只重放事件日志里模型真实提交过的
payload，经新归一层（确定性形状修复，逐条留痕）+ 原语义校验（引用可解析/
图完整/可比口径）后，把通过的 kind 合并进该实体最新 report 产物，并落
`research/structures_recovered` 审计事件（来源 run/seq/修复清单可回溯）。

纪律：
- 默认 dry-run，只打印将回收什么；--apply 才写库；
- 逐 kind 回收：语义校验不过的 kind 保持拒绝（不靠放松校验通过）；
- 同一 kind 多次提交取时间上最后通过的版本（后续提交是修复版）；
- 幂等：产物已含相同 kind 且内容一致 → 跳过；
- 回收后重开档案快照（structures 进 data_hash → 新快照，页面「刷新」即见）。

用法：
  uv run python scripts/recover_structures.py --data-dir data                 # dry-run 全部实体
  uv run python scripts/recover_structures.py --entity industry:ai-for-science # dry-run 单实体
  uv run python scripts/recover_structures.py --entity industry:ai-for-science --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


def _load(data_dir: Path):
    from finance_agent.eventstore.store import EventStore
    from finance_agent.knowledge.metric_store import MetricStore
    from finance_agent.knowledge.store import BitemporalStore

    kb = BitemporalStore(data_dir / "kb.db")
    metrics = MetricStore(data_dir / "metrics.db")
    events = EventStore(data_dir / "events.db")
    return kb, metrics, events


def _submit_attempts(events, run_ids: set[str]) -> list[tuple[int, str, dict[str, Any]]]:
    """事件日志里的 submit_structures 提交（按全局 seq 升序 = 时间序）。"""
    out: list[tuple[int, str, dict[str, Any]]] = []
    for rid in sorted(run_ids):
        for ev in events.read(rid, types=["tool/call"]):
            if ev.payload.get("name") != "submit_structures":
                continue
            structures = (ev.payload.get("arguments") or {}).get("structures")
            if isinstance(structures, dict) and structures:
                out.append((ev.seq, rid, structures))
    out.sort(key=lambda x: x[0])
    return out


def recover_entity(kb, metrics, events, kind: str, eid: str, namespace: str,
                   extra_run_ids: set[str] | None = None) -> dict[str, Any]:
    """回收一个实体：返回报告 dict（kinds/来源/修复/拒绝原因），不写库。

    extra_run_ids：产物之外的扫描源（如「模型已提交但 run 未定稿」的
    synthesize 子 run——2026-09-12 事故：合成步 LLM 流式连接挂死，
    submit_structures 已接受 5 个 kind 但产物未冻结）。
    """
    from finance_agent.dossier.structures import (
        parse_structures_partial,
        structures_payload,
        validate_structures,
    )
    from finance_agent.research.artifacts import ref_resolvable

    now = datetime.now(UTC)
    artifacts = metrics.artifacts_as_of(kind, eid, now, namespace=namespace)
    reports = [a for a in artifacts if a.get("purpose", "report") == "report"]
    if not reports:
        return {"entity": f"{kind}:{eid}", "skip": "无 report 产物"}
    target = reports[0]  # 最新 report 产物（projector 从新到旧取 structures）
    existing = target.get("structures") or {}
    run_ids = {str(a["run_id"]) for a in reports if a.get("run_id")}
    run_ids |= {str(r) for r in (extra_run_ids or set())}
    attempts = _submit_attempts(events, run_ids)
    if not attempts:
        return {"entity": f"{kind}:{eid}", "skip": "事件日志无 submit_structures 提交",
                "artifact_id": target.get("artifact_id")}

    def _resolvable(ref: str) -> bool:
        return ref_resolvable(kb, metrics, ref, namespace=namespace,
                              entity_kind=kind, entity_id=eid)

    recovered: dict[str, Any] = {}       # kind → payload（后提交覆盖先提交）
    provenance: dict[str, dict] = {}     # kind → {run_id, seq}
    rejected: dict[str, str] = {}        # kind → 最后一次的拒绝原因
    repairs_all: list[str] = []
    for seq, rid, structures in attempts:
        accepted, failures, repairs = parse_structures_partial(structures)
        repairs_all.extend(f"[seq={seq}] {r}" for r in repairs)
        for issue in validate_structures(accepted, resolvable=_resolvable):
            bad = issue.split(":", 1)[0].strip()
            failures.setdefault(bad, "")
            failures[bad] = (failures[bad] + "；" + issue if failures[bad] else issue)
            accepted.pop(bad, None)
        for kind_name, reason in failures.items():
            rejected[kind_name] = reason[:300]
        for kind_name, model in accepted.items():
            recovered[kind_name] = structures_payload({kind_name: model})[kind_name]
            provenance[kind_name] = {"run_id": rid, "seq": seq}
            rejected.pop(kind_name, None)  # 后续尝试修好了 → 撤销拒绝记录
    # 终态口径：取「时间上最后通过的版本」——后续尝试失败的 kind 若早先有过
    # 合法版本，仍用合法版（不幽灵引用）；从未通过过的 kind 才计入拒绝
    rejected = {k: v for k, v in rejected.items() if k not in recovered}

    to_add = {
        k: v for k, v in recovered.items()
        if existing.get(k) != v  # 幂等：已含相同内容 → 跳过
    }
    return {
        "entity": f"{kind}:{eid}",
        "artifact_id": target.get("artifact_id"),
        "attempts": len(attempts),
        "existing_kinds": sorted(existing),
        "recoverable": {k: provenance[k] for k in to_add},
        "unchanged": sorted(set(recovered) - set(to_add)),
        "rejected": rejected,
        "repairs": repairs_all,
        "structures_to_merge": to_add,
    }


def apply_recovery(kb, metrics, events, report: dict[str, Any], namespace: str,
                   data_dir: Path) -> str | None:
    """把回收结果写进产物 + 审计事件 + 重开快照。返回新快照 id（无变更 → None）。"""
    from finance_agent.decision.store import DecisionStore
    from finance_agent.dossier.projector import DossierProjector
    from finance_agent.dossier.service import DossierService
    from finance_agent.eventstore.events import Event

    merge = report.get("structures_to_merge") or {}
    if not merge:
        return None
    kind, eid = report["entity"].split(":", 1)
    artifact = metrics.get_artifact(report["artifact_id"])
    if artifact is None:
        raise SystemExit(f"产物不可读：{report['artifact_id']}")
    artifact["structures"] = {**(artifact.get("structures") or {}), **merge}
    doc = artifact.get("report_document") or {}
    limits = list(doc.get("limitations") or [])
    note = (
        f"structures 由 scripts/recover_structures.py 从提交日志回收"
        f"（{sorted(merge)}；原提交未进入产物——形状被拒或运行未定稿，"
        f"见 research/structures_recovered 事件）"
    )
    if note not in limits:
        limits.append(note)
    doc["limitations"] = limits
    artifact["report_document"] = doc
    metrics.save_artifact(artifact_id=artifact["artifact_id"], namespace=namespace,
                          payload=artifact)
    run_id = f"structures-recovery-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    events.append(Event(
        run_id=run_id, type="research/structures_recovered",
        payload={
            "entity": report["entity"], "artifact_id": artifact["artifact_id"],
            "kinds": sorted(merge), "provenance": report["recoverable"],
            "attempts": report["attempts"], "repairs": report["repairs"][:60],
            "rejected": report.get("rejected") or {},
        },
    ))
    # 重开档案：structures 进 data_hash → 发布新快照（页面「刷新」即见回收内容）
    decisions = DecisionStore(data_dir / "decisions.db") \
        if (data_dir / "decisions.db").exists() else None
    projector = DossierProjector(kb=kb, metrics=metrics, decisions=decisions)
    service = DossierService(kb=kb, metrics=metrics, projector=projector, events=events,
                             decisions=decisions)
    snap, _created = service.open(kind, eid, namespace=namespace, run_id=run_id)
    return snap["context"]["snapshot_id"]


def _entities(metrics, kb, namespace: str) -> list[tuple[str, str]]:
    seen = set(metrics.list_entities_with_snapshots(namespace=namespace))
    rows = kb._conn.execute(  # noqa: SLF001 - 只读盘点（与 migrate_dossier 同法）
        "SELECT DISTINCT entity_kind, entity_id FROM facts WHERE namespace = ?",
        (namespace,),
    ).fetchall()
    seen.update((r[0], r[1]) for r in rows)
    return sorted(seen)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--namespace", default="prod")
    ap.add_argument("--entity", action="append", default=[],
                    help="kind:id（可重复；缺省 = 全部实体）")
    ap.add_argument("--run-id", dest="run_ids", action="append", default=[],
                    help="额外扫描的 run_id（可重复；用于回收未定稿 run 里的提交）")
    ap.add_argument("--apply", action="store_true", help="写库（默认 dry-run）")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"[recover] 数据目录不存在：{data_dir}", file=sys.stderr)
        return 2
    kb, metrics, events = _load(data_dir)
    targets = (
        [tuple(e.split(":", 1)) for e in args.entity]
        if args.entity else _entities(metrics, kb, args.namespace)
    )
    changed = 0
    for kind, eid in targets:
        report = recover_entity(kb, metrics, events, kind, eid, args.namespace,
                                extra_run_ids=set(args.run_ids))
        if report.get("skip"):
            continue
        merge = report.get("structures_to_merge") or {}
        head = (f"[recover] {report['entity']} 产物 {report['artifact_id']}："
                f"{report['attempts']} 次提交，现有 {report['existing_kinds'] or '[]'}")
        if not merge:
            unchanged = report.get("unchanged") or []
            rejected = report.get("rejected") or {}
            print(head + f" → 无可回收（已含 {unchanged or '—'}；仍拒 {sorted(rejected) or '—'}）")
            continue
        print(head + f" → 可回收 {sorted(merge)}")
        for k, prov in report["recoverable"].items():
            print(f"    + {k} ← run {prov['run_id']} seq {prov['seq']}")
        for k, reason in (report.get("rejected") or {}).items():
            print(f"    ✗ {k} 仍被拒：{reason[:160]}")
        for r in report["repairs"][:12]:
            print(f"    ~ {r}")
        if len(report["repairs"]) > 12:
            print(f"    ~ …另 {len(report['repairs']) - 12} 条修复")
        if args.apply:
            snapshot_id = apply_recovery(kb, metrics, events, report, args.namespace,
                                         data_dir)
            changed += 1
            print(f"    ✓ 已写入并重开快照 {snapshot_id}")
    if args.apply and changed:
        print(f"[recover] APPLIED：{changed} 个实体已回收（审计事件 research/structures_recovered）")
    elif not args.apply:
        print("[recover] DRY-RUN（加 --apply 写库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
