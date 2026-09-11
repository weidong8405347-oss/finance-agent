#!/usr/bin/env python3
"""哨兵运行对照分析（方案 §10.2 指标口径的可自动采集子集）。

比较两次哨兵运行（如 基线A vs B组）的逐题信号：
研究充分度 verdict / 问题覆盖 / typed 产出 / 来源分档 / 内容核验覆盖 /
完整性硬门禁 / 重复资料 / 耗时 / 停滞形态。

用法：
    uv run python scripts/compare_sentinel.py \
        data/sentinel/baseline-A-r4 data/sentinel/baseline-B
    # 可选 --tasks be-orders-revenue guidance-to-actual（只比指定题）
    # A' 重跑目录可多次传入：--extra data/sentinel/baseline-A-prime

输出：终端对照表 + <最新目录>/comparison.json（可归档进报告）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


#: 对照的信号抽取（从 run_sentinel 结果 JSON 的 signals 里取）
def extract(result: dict) -> dict:
    sig = result.get("signals") or {}
    a = sig.get("assessment") or {}
    eq = a.get("evidence_quality") or {}
    budget = sig.get("budget") or {}
    steps = sig.get("steps") or []
    arts = sig.get("artifacts") or []
    support = eq.get("claims_by_evidence_support") or {}
    return {
        "status": result.get("status"),
        "elapsed_min": round(float(result.get("elapsed_seconds") or 0) / 60, 1),
        "verdict": a.get("verdict"),
        "hard_gate": a.get("hard_gate_passed"),
        "coverage": (f"{a.get('answered')}/{a.get('applicable')}"
                     if a.get("applicable") is not None else None),
        "key_coverage": a.get("key_coverage"),
        "observations": eq.get("observations"),
        "first_party": eq.get("first_party_observations"),
        "pit_a": eq.get("pit_a_observations"),
        "secondary": eq.get("secondary_observations"),
        "validated_claims": eq.get("validated_claims"),
        "content_unchecked": eq.get("validated_claims_content_unchecked"),
        "verified_non_unchecked": sum(
            v for k, v in support.items() if k != "unchecked"),
        "support_breakdown": support or None,
        "scale_suspects": (a.get("numeric_consistency") or {}).get("scale_suspect_total"),
        "counter_claims": (a.get("analytical_depth") or {}).get("counter_evidence_claims"),
        "integrity_failed": [c["name"] for c in (a.get("integrity_checks") or [])
                             if not c.get("passed")] or None,
        "artifacts": [(x.get("status"), x.get("sufficiency")) for x in arts] or None,
        "duplicate_chunks": budget.get("duplicate_chunks"),
        "unique_chunks": budget.get("unique_chunks"),
        "duplicate_documents": budget.get("duplicate_documents"),
        "documents_stored": budget.get("documents_stored"),
        "stop_reason": budget.get("stop_reason") or a.get("stop_reason"),
        "stalls": sig.get("question_stalls") or None,
        "steps_failed": [s["step"] for s in steps
                         if s.get("status") not in ("completed",)] or None,
        "gates_auto": len(result.get("auto_approvals") or []),
    }


def load_run(directory: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    results = directory / "sentinel-results"
    if not results.is_dir():
        raise SystemExit(f"结果目录不存在: {results}")
    for f in sorted(results.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"跳过不可解析结果 {f.name}: {e}", file=sys.stderr)
            continue
        out[f.stem] = extract(data)
    return out


KEYS = [
    "status", "elapsed_min", "verdict", "hard_gate", "coverage", "key_coverage",
    "observations", "first_party", "pit_a", "secondary",
    "validated_claims", "content_unchecked", "verified_non_unchecked",
    "scale_suspects", "counter_claims", "integrity_failed", "artifacts",
    "duplicate_chunks", "unique_chunks", "duplicate_documents", "documents_stored",
    "stop_reason", "stalls", "steps_failed", "gates_auto",
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="哨兵运行对照（A vs B）")
    ap.add_argument("baseline", help="基线运行目录（如 data/sentinel/baseline-A-r4）")
    ap.add_argument("candidate", help="对照运行目录（如 data/sentinel/baseline-B）")
    ap.add_argument("--extra", action="append", default=[],
                    help="补充运行目录（如 A' 重跑；同题以最新为准并入基线侧）")
    ap.add_argument("--tasks", nargs="*", default=[], help="只比较指定题目 id")
    ap.add_argument("--json", default="", help="对照结果另存 JSON 路径")
    args = ap.parse_args(argv)

    base = load_run(Path(args.baseline))
    for extra in args.extra:
        base.update(load_run(Path(extra)))  # 同题以补充运行（更新代码）为准
    cand = load_run(Path(args.candidate))
    tasks = args.tasks or sorted(set(base) | set(cand))

    width = max(len(t) for t in tasks) + 2 if tasks else 20
    print(f"{'题目'.ljust(width)}{'指标'.ljust(26)}{'基线(A/A′)'.ljust(34)}对照(B)")
    print("-" * 110)
    comparison: dict[str, dict] = {}
    for task in tasks:
        b, c = base.get(task), cand.get(task)
        comparison[task] = {"baseline": b, "candidate": c}
        if b is None or c is None:
            print(f"{task.ljust(width)}{'—'.ljust(26)}"
                  f"{('缺失' if b is None else 'ok').ljust(34)}"
                  f"{'缺失' if c is None else 'ok'}")
            continue
        for key in KEYS:
            bv, cv = b.get(key), c.get(key)
            if bv == cv and bv in (None, "", [], 0):
                continue  # 双方都无信号的指标不刷屏
            mark = "  " if bv == cv else "→ "
            print(f"{task.ljust(width)}{key.ljust(26)}"
                  f"{str(bv)[:32].ljust(34)}{mark}{str(cv)[:40]}")
        print()

    out_path = args.json or str(Path(args.candidate) / "comparison.json")
    Path(out_path).write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"对照 JSON: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
