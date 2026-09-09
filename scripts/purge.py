#!/usr/bin/env python3
"""清理低质量历史：会话与知识实体的删除入口（干跑默认，--apply 才落库）。

两种删除对象：

1. **会话**（Sessions 页里的运行记录）：删事件（级联子 run）+ 报告目录。
   正在跑的会话需要先停（或 --force）。
2. **知识实体**（Knowledge 页里的档案）：
   - `--mode tombstone`（默认）：写墓碑 + 审计事件，所有读路径立即过滤；
     事实/观测行仍在，可 `--restore` 撤销；
   - `--mode hard`：额外真删 kb.facts / metrics 九表 / decisions 的行，
     清两个库都不再引用的孤儿证据，删磁盘存档——**不可恢复**。

事件日志保留 `session/deleted` / `knowledge/purged` 审计记录，所以删除本身可追溯，
重放也不会静默复活已删实体。

用法：
    # 盘点：哪些会话/实体占了多少（只读）
    uv run python scripts/purge.py --data-dir data list-sessions
    uv run python scripts/purge.py --data-dir data list-entities --sort-by facts

    # 删一个空壳/低质量会话（干跑 → 落库）
    uv run python scripts/purge.py --data-dir data session --run-id live-xxxx
    uv run python scripts/purge.py --data-dir data session --run-id live-xxxx --apply

    # 删一个低质量档案（墓碑，可恢复）
    uv run python scripts/purge.py --data-dir data entity \
        --entity industry:ai-for-science-美股港股 --reason "旧口径重复档案，质量差"
    # 同上但硬删（不可恢复）
    uv run python scripts/purge.py --data-dir data entity \
        --entity stock:LODE --mode hard --reason "低质量内容，硬删" --apply

    # 撤销墓碑
    uv run python scripts/purge.py --data-dir data restore --entity stock:LODE --apply

    # 批量（每行一个 "kind:id"，# 开头为注释）
    uv run python scripts/purge.py --data-dir data entity --batch purge-list.txt \
        --reason "批量清理低质量档案" --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


def _load(data_dir: Path):
    from finance_agent.decision.store import DecisionStore
    from finance_agent.eventstore.store import EventStore
    from finance_agent.knowledge.metric_store import MetricStore
    from finance_agent.knowledge.store import BitemporalStore

    kb = BitemporalStore(data_dir / "kb.db")
    metrics = MetricStore(data_dir / "metrics.db")
    events = EventStore(data_dir / "events.db")
    decisions_path = data_dir / "decisions.db"
    decisions = DecisionStore(decisions_path) if decisions_path.exists() else None
    return kb, metrics, events, decisions


def _split_entity(raw: str) -> tuple[str, str]:
    if ":" not in raw:
        raise SystemExit(f"[purge] 实体格式必须是 kind:id（收到 {raw!r}）")
    kind, eid = raw.split(":", 1)
    if kind not in ("stock", "industry"):
        raise SystemExit(f"[purge] 未知实体类型 {kind!r}（可用：stock / industry）")
    return kind, eid.strip()


def cmd_list_sessions(args) -> int:
    from finance_agent.api.app import _session_summary

    _, _, events, _ = _load(Path(args.data_dir))
    children = events.child_run_ids()
    runs = events.list_runs()
    session_count = sum(
        1 for r in runs
        if r["run_id"] not in children and events.is_session_run(r["run_id"])
    )
    print(f"[purge] 全部 run {len(runs)} 个；子 run {len(children)} 个；"
          f"会话 {session_count} 个")
    for r in runs:
        rid = r["run_id"]
        if rid in children:
            kind = "child"
        elif not events.is_session_run(rid):
            kind = "system/ghost"
        else:
            kind = "session"
        if args.sessions_only and kind != "session":
            continue
        extra = ""
        if kind == "session":
            s = _session_summary(events, rid, r["started_at"], r["last_active"])
            extra = f" status={s['status']}"
            if s.get("possibly_stale"):
                extra += "（可能已中断）"
            if s.get("last_outcome") and s["status"] == "running":
                extra += f" 上一条={s['last_outcome']}"
            if s.get("title"):
                extra += f" title={str(s['title'])[:28]!r}"
        print(f"  {kind:<12} {rid:<58} events={r['event_count']:<6} "
              f"last={r['last_active'][:19]}{extra}")
    return 0


def cmd_list_entities(args) -> int:
    from finance_agent.knowledge.purge import preview_entity

    kb, metrics, _, decisions = _load(Path(args.data_dir))
    rows = kb._conn.execute(  # noqa: SLF001 - 只读盘点
        "SELECT entity_kind, entity_id, COUNT(*) FROM facts WHERE namespace = ?"
        " GROUP BY 1, 2", (args.namespace,),
    ).fetchall()
    out = []
    for kind, eid, n in rows:
        pv = preview_entity(kb=kb, metrics=metrics, decisions=decisions,
                            entity_kind=kind, entity_id=eid, namespace=args.namespace)
        out.append((kind, eid, n, pv["total_rows"], kb.is_purged(kind, eid,
                                                                 namespace=args.namespace)))
    key = {"facts": 2, "rows": 3, "entity": 1}[args.sort_by]
    out.sort(key=lambda r: (-r[key] if key in (2, 3) else r[key], r[1]))
    print(f"[purge] {args.namespace} 命名空间实体 {len(out)} 个"
          f"（按 {args.sort_by} 排序；purged=已删除）")
    for kind, eid, facts, total, purged in out:
        flag = " [PURGED]" if purged else ""
        print(f"  {kind}:{eid:<34} facts={facts:<5} 全部行={total:<5}{flag}")
    return 0


def _select_sessions(events, args) -> list[str]:
    """批量选择：--run-id 可重复 + --zombies + --status（一次确认删多个）。"""
    from finance_agent.api.app import _session_summary

    ids = list(args.run_id)
    kids = events.child_run_ids()
    rows = [r for r in events.list_runs()
            if r["run_id"] not in kids and events.is_session_run(r["run_id"])]
    if args.zombies:
        for r in rows:
            s = _session_summary(events, r["run_id"], r["started_at"], r["last_active"])
            if s["status"] == "running" and s["possibly_stale"]:
                ids.append(r["run_id"])
    if args.status:
        wanted = {x.strip() for x in args.status.split(",") if x.strip()}
        for r in rows:
            s = _session_summary(events, r["run_id"], r["started_at"], r["last_active"])
            if s["status"] in wanted:
                ids.append(r["run_id"])
    # 去重且保持顺序
    return list(dict.fromkeys(ids))


def cmd_session(args) -> int:
    from finance_agent.knowledge.purge import PurgeError, purge_session

    _, _, events, _ = _load(Path(args.data_dir))
    reports_dir = Path(args.data_dir) / "reports"
    targets = _select_sessions(events, args)
    if not targets:
        print("[purge] 没有选中会话（--run-id / --zombies / --status）", file=sys.stderr)
        return 2
    force = args.force or args.zombies  # 僵尸会话的 command 永远不会有 done，需 force
    failed = 0
    for run_id in targets:
        try:
            if args.apply:
                result = purge_session(events=events, run_id=run_id, force=force,
                                       reason=args.reason, reports_dir=reports_dir)
                print(f"[purge] ✓ 已删除会话 {run_id}：{result['total_events']} 条事件，"
                      f"{len(result['deleted_runs'])} 个 run"
                      f"（含子 run），文件 {len(result['files_removed'])} 个目录")
            else:
                active = events.is_active(run_id)
                children = sorted(r["run_id"] for r in events.list_runs()
                                  if r["run_id"].startswith(f"{run_id}--"))
                n = sum(r["event_count"] for r in events.list_runs()
                        if r["run_id"] == run_id or r["run_id"] in children)
                print(f"[purge] DRY-RUN 会话 {run_id}：将删 {n} 条事件，"
                      f"{len(children)} 个子 run，报告目录 {reports_dir / run_id}")
                if active and not force:
                    print("        ⚠ 该会话仍在运行：需要 --force（--zombies 自动带 force）或先 stop")
        except PurgeError as e:
            print(f"[purge] ✗ {run_id}: {e}", file=sys.stderr)
            failed += 1
    if not args.apply:
        print(f"[purge] DRY-RUN：{len(targets)} 个会话待删（加 --apply 落库）")
    return 1 if failed else 0


def cmd_entity(args) -> int:
    from finance_agent.knowledge.purge import preview_entity, purge_entity

    kb, metrics, events, decisions = _load(Path(args.data_dir))
    targets = []
    if args.batch:
        for line in Path(args.batch).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                targets.append(_split_entity(line))
    for raw in args.entity:
        targets.append(_split_entity(raw))
    if args.quality_below is not None:
        from datetime import UTC
        from datetime import datetime as _dt

        from finance_agent.knowledge.verify import verify_entity

        now = _dt.now(UTC)
        rows = kb._conn.execute(  # noqa: SLF001 - 只读盘点
            "SELECT entity_kind, entity_id FROM facts WHERE namespace = ?"
            " GROUP BY 1, 2", (args.namespace,),
        ).fetchall()
        for kind, eid in rows:
            q = verify_entity(kb, kind, eid, now, namespace=args.namespace)
            if q.quality_score >= args.quality_below:
                continue
            if args.require_no_typed:
                n_obs = metrics._conn.execute(  # noqa: SLF001
                    "SELECT COUNT(*) FROM metric_observations WHERE entity_kind=? AND entity_id=?",
                    (kind, eid)).fetchone()[0]
                n_cl = metrics._conn.execute(  # noqa: SLF001
                    "SELECT COUNT(*) FROM research_claims WHERE entity_kind=? AND entity_id=?",
                    (kind, eid)).fetchone()[0]
                if n_obs or n_cl:
                    continue
            if (kind, eid) not in targets:
                targets.append((kind, eid))
    if not targets:
        print("[purge] 没有目标（--entity / --batch / --quality-below）", file=sys.stderr)
        return 2

    failed = 0
    for kind, eid in targets:
        pv = preview_entity(kb=kb, metrics=metrics, decisions=decisions,
                            entity_kind=kind, entity_id=eid, namespace=args.namespace)
        print(f"[purge] {kind}:{eid} 盘点：{pv['counts']}（合计 {pv['total_rows']} 行）"
              + ("；已有墓碑" if pv["already_purged"] else ""))
        if not args.apply:
            continue
        report = purge_entity(
            kb=kb, metrics=metrics, decisions=decisions, events=events,
            entity_kind=kind, entity_id=eid, mode=args.mode, namespace=args.namespace,
            reason=args.reason, reports_dir=Path(args.data_dir) / "reports",
            knowledge_dir=Path(args.data_dir) / "knowledge",
        )
        payload = report.as_payload()
        print(f"        ✓ {args.mode} 删除：{payload['counts']}，孤儿证据 "
              f"{payload['orphan_evidence_deleted']} 条，文件 {payload['files_removed']}；"
              f"可恢复={payload['restorable']}")
        for w in payload["warnings"]:
            print(f"        · {w}")
        if not payload["restorable"]:
            print("        ⚠ hard 模式不可恢复（事件日志保留审计记录）")
    if not args.apply:
        print(f"[purge] DRY-RUN：{len(targets)} 个实体待删（加 --apply 落库）")
    return 1 if failed else 0


def cmd_restore(args) -> int:
    from finance_agent.knowledge.purge import restore_entity

    kb, _, events, _ = _load(Path(args.data_dir))
    kind, eid = _split_entity(args.entity)
    if not args.apply:
        purged = kb.is_purged(kind, eid, namespace=args.namespace)
        tomb = next((t for t in kb.purged_entities(namespace=args.namespace)
                     if t["entity_kind"] == kind and t["entity_id"] == eid), None)
        print(f"[purge] DRY-RUN 恢复 {kind}:{eid}：有墓碑={purged}"
              + (f"，模式={tomb['mode']}" if tomb else "")
              + ("（hard 模式无法恢复：行已删）" if tomb and tomb["mode"] == "hard" else ""))
        return 0
    ok = restore_entity(kb=kb, entity_kind=kind, entity_id=eid, events=events,
                        namespace=args.namespace, reason=args.reason)
    print(f"[purge] {'✓ 已恢复' if ok else '✗ 无法恢复（不是 tombstone 删除或未曾删除）'}"
          f" {kind}:{eid}")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--namespace", default="prod")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list-sessions", help="列出 run/会话（含系统幽灵 run 标注）")
    p.add_argument("--sessions-only", action="store_true")
    p.set_defaults(fn=cmd_list_sessions)

    p = sub.add_parser("list-entities", help="列出实体与行数（删除前盘点）")
    p.add_argument("--sort-by", default="facts", choices=("facts", "rows", "entity"))
    p.set_defaults(fn=cmd_list_entities)

    p = sub.add_parser("session", help="删除会话（可批量；级联子 run + 报告目录）")
    p.add_argument("--run-id", action="append", default=[], help="可重复，批量删除")
    p.add_argument("--zombies", action="store_true",
                   help="选中所有「running 但可能已中断」的僵尸会话（自动 force）")
    p.add_argument("--status", default="", help="按状态选：blocked,error（逗号分隔）")
    p.add_argument("--force", action="store_true", help="正在跑也删（默认拒绝）")
    p.add_argument("--reason", default="")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=cmd_session)

    p = sub.add_parser("entity", help="删除知识实体（默认墓碑，可 hard）")
    p.add_argument("--entity", action="append", default=[], help="kind:id，可重复（批量）")
    p.add_argument("--batch", default=None, help="每行一个 kind:id 的文本文件")
    p.add_argument("--quality-below", type=float, default=None,
                   help="自动选中质量分低于该值的实体（与 --entity/--batch 叠加）")
    p.add_argument("--require-no-typed", action="store_true",
                   help="与 --quality-below 联用：只选无 typed 观测/论断的实体")
    p.add_argument("--mode", default="tombstone", choices=("tombstone", "hard"))
    p.add_argument("--reason", default="")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=cmd_entity)

    p = sub.add_parser("restore", help="撤销墓碑（仅 tombstone 可恢复）")
    p.add_argument("--entity", required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=cmd_restore)

    args = ap.parse_args(argv)
    if not Path(args.data_dir).is_dir():
        print(f"[purge] 数据目录不存在：{args.data_dir}", file=sys.stderr)
        return 2
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
