#!/usr/bin/env python3
"""观测修订/失效（audit §3.2 修复方案 6）。

对已落库的错误观测建立修订记录：不原位修改冻结历史，而是追加修订行 +
`metric/revised` 事件，并回出需要重审的下游依赖（计算/论断/产物）。

用法：
    # 干跑（默认）：只打印将要做的修订与受影响依赖
    uv run python scripts/revise_observations.py --data-dir data \
        --observation obs-1ce021570310 --action invalidated \
        --reason "摘录是 \$73.7 million, or 37%：金额被作为 ratio 保存"

    # 落库
    uv run python scripts/revise_observations.py --data-dir data --apply \
        --observation obs-79a868c353bb --action needs_review \
        --reason "摘录只有 106,303 27,456，缺表头与规模词，量级待核"

    # 批量（JSON 文件：[{"observation_id":..., "action":..., "reason":...,
    #           "replacement_observation_id":...}]）
    uv run python scripts/revise_observations.py --data-dir data --apply \
        --batch revisions.json

修订后可用 scripts/migrate_dossier.py 或页面「刷新（检查新快照）」重投影，
生成不含已失效观测的新快照；旧快照仍指向旧版本（历史一致性不变）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

ACTIONS = ("invalidated", "corrected", "needs_review", "superseded")


def _load(data_dir: Path):
    from finance_agent.eventstore.store import EventStore
    from finance_agent.harness.manifest import RunManifest, RunMode
    from finance_agent.knowledge.metric_store import MetricStore
    from finance_agent.knowledge.metric_writer import TypedMetricWriter
    from finance_agent.knowledge.store import BitemporalStore

    kb = BitemporalStore(data_dir / "kb.db")
    metrics = MetricStore(data_dir / "metrics.db")
    events = EventStore(data_dir / "events.db")
    writer = TypedMetricWriter(store=metrics, kb=kb, events=events)
    run = RunManifest(run_id=f"revise-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
                      mode=RunMode.LIVE)
    return metrics, writer, run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--observation", action="append", default=[],
                    help="观测 id（可重复）")
    ap.add_argument("--action", default="needs_review", choices=ACTIONS)
    ap.add_argument("--reason", default="")
    ap.add_argument("--replacement", default=None, help="corrected 时的新观测 id")
    ap.add_argument("--batch", default=None, help="JSON 文件：修订条目列表")
    ap.add_argument("--apply", action="store_true", help="落库（默认干跑）")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"[revise] 数据目录不存在：{data_dir}", file=sys.stderr)
        return 2

    items: list[dict] = []
    if args.batch:
        items = json.loads(Path(args.batch).read_text(encoding="utf-8"))
    for oid in args.observation:
        items.append({"observation_id": oid, "action": args.action,
                      "reason": args.reason, "replacement_observation_id": args.replacement})
    if not items:
        print("[revise] 没有要处理的观测（--observation 或 --batch）", file=sys.stderr)
        return 2

    metrics, writer, run = _load(data_dir)
    ok = failed = 0
    for item in items:
        oid = str(item.get("observation_id") or "")
        action = str(item.get("action") or "needs_review")
        reason = str(item.get("reason") or "").strip()
        if action not in ACTIONS:
            print(f"[revise] ✗ {oid}: 未知 action {action!r}")
            failed += 1
            continue
        if not reason:
            print(f"[revise] ✗ {oid}: 必须给 --reason（修订依据要可审计）")
            failed += 1
            continue
        obs = metrics.get_observation(oid)
        if obs is None:
            print(f"[revise] ✗ {oid}: 观测不存在")
            failed += 1
            continue
        dependents = metrics.refs_to(oid)
        print(f"[revise] {oid} {obs.entity_kind}:{obs.entity_id} {obs.metric_key}"
              f"={obs.value} {obs.unit} → {action}")
        print(f"         原文：{(obs.raw.value_text if obs.raw else '（无 raw）')!r}"
              f"；期间：{obs.period.fiscal_label or obs.period.end.isoformat()}"
              f"；主体：{obs.subject_kind}:{obs.subject_id}")
        print(f"         依据：{reason}")
        flat = [f"calc:{c}" for c in dependents["calculations"]] \
            + [f"claim:{c}" for c in dependents["claims"]] \
            + [f"artifact:{a}" for a in dependents["artifacts"]]
        print(f"         待重审依赖 {len(flat)} 项：{flat[:10]}{'…' if len(flat) > 10 else ''}")
        if not args.apply:
            continue
        try:
            result = writer.revise_observation(
                oid, action=action, reason=reason, run=run,
                replacement_observation_id=item.get("replacement_observation_id"),
            )
            print(f"         ✓ 修订 {result['revision_id']} 已落库（旧版本保留在审计链）")
            ok += 1
        except Exception as e:  # noqa: BLE001 - 逐条失败不拖死批量
            print(f"         ✗ {type(e).__name__}: {e}")
            failed += 1

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[revise] {mode}: 成功 {ok}，失败 {failed}，共 {len(items)} 条"
          + ("" if args.apply else "（加 --apply 落库）"))
    if args.apply and ok:
        print("[revise] 下一步：重投影生成新快照（页面「刷新（检查新快照）」或 "
              "scripts/migrate_dossier.py），并重审上面列出的依赖 claim/计算/产物。")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
