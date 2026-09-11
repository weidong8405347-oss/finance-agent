#!/usr/bin/env python3
"""盲评打包器（tools-plugins 方案 §10.2 P3：隐去来源组别、随机顺序、同 rubric）。

输入两组运行结果目录（如 baseline-A-r4 与 baseline-C），产出评审包：
- bundle/<task>/report-X.md / report-Y.md：两份产物（组别匿名，随机分配 X/Y）；
- bundle/<task>/rubric.md：评审卡（有效问题覆盖/引用支持/反证质量/关键遗漏/清晰度
  + 总体胜负），两位评审独立打分；
- bundle/INDEX.md：评审指引与任务清单；
- bundle/key.json：**评审后开封**的映射表（哪个 X/Y 属于哪组；权限 600）。

纪律（方案 §10.2）：
- 评审者需要访问报告对应的证据原件——产物 markdown 里本就带引用/校验备注，
  打包不删内容（匿名只针对组别身份）；
- LLM judge 不得替代人工终评（校准用）；小样本胜率只支持阶段决策。

用法：
    uv run python scripts/package_blind_review.py \
        --a data/sentinel/baseline-A-r4 --c data/sentinel/baseline-C \
        --out data/sentinel/blind-review-2026-09-12 --seed 42
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

RUBRIC = """# 评审卡（{task_id}）

> 同 rubric 双评：两位评审独立打分，1–5 分；总体胜负单独勾选。

| 维度 | report-X | report-Y | 评审备注 |
| --- | --- | --- | --- |
| 有效问题覆盖（关键问题是否真正回答，unavailable 不算已回答） |  |  |  |
| 引用支持（关键结论是否有可定位的原文/观测支撑） |  |  |  |
| 反证与限制（是否给出反证检索/替代解释/限制条件） |  |  |  |
| 关键遗漏（有无重要缺口被掩盖而非显式标注） |  |  |  |
| 清晰度与可执行性 |  |  |  |

总体（勾选其一）：X 明显更好 / X 略好 / 平局 / Y 略好 / Y 明显更好

不确定之处（自由文本）：
"""


def _find_report_md(data_dir: Path, task_result: dict) -> Path | None:
    """从运行目录找最终报告 markdown（synthesize 子 run 的 report.md）。"""
    reports = data_dir / "reports"
    if not reports.is_dir():
        return None
    # synthesize 子 run 的 report.md 优先；退化取最新修改的 report.md
    candidates = sorted(reports.glob("*-synthesize/report.md"))
    if not candidates:
        candidates = sorted(reports.glob("*/report.md"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _load_task_results(run_dir: Path) -> dict[str, dict]:
    results = run_dir / "sentinel-results"
    out: dict[str, dict] = {}
    for f in sorted(results.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if data.get("status") in ("completed", "completed_after_grace"):
            out[f.stem] = data
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="盲评打包器（A/C 组产物匿名对照）")
    ap.add_argument("--a", required=True, help="A 组运行目录（含 sentinel-results/）")
    ap.add_argument("--c", required=True, help="C 组运行目录（同上）")
    ap.add_argument("--out", required=True, help="评审包输出目录")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（X/Y 分配可复现）")
    ap.add_argument("--label-a", default="A", help="A 组匿名标签（默认 A）")
    ap.add_argument("--label-c", default="C", help="C 组匿名标签（默认 C）")
    args = ap.parse_args(argv)

    dir_a, dir_c = Path(args.a), Path(args.c)
    tasks_a = _load_task_results(dir_a)
    tasks_c = _load_task_results(dir_c)
    common = sorted(set(tasks_a) & set(tasks_c))
    if not common:
        print("两组无共同 completed 题目，无法配对盲评", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    key: dict[str, dict] = {}
    index_rows: list[str] = []
    skipped: list[str] = []

    for task_id in common:
        report_a = _find_report_md(Path(tasks_a[task_id].get("data_dir") or dir_a),
                                   tasks_a[task_id])
        report_c = _find_report_md(Path(tasks_c[task_id].get("data_dir") or dir_c),
                                   tasks_c[task_id])
        if report_a is None or report_c is None:
            skipped.append(f"{task_id}（缺 report.md："
                           f"{args.label_a}={'有' if report_a else '无'}，"
                           f"{args.label_c}={'有' if report_c else '无'}）")
            continue
        # 随机分配 X/Y（每题独立，种子可复现）
        x_group, y_group = (args.label_a, args.label_c) if rng.random() < 0.5 \
            else (args.label_c, args.label_a)
        task_dir = out_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "report-X.md").write_text(
            report_a.read_text(encoding="utf-8") if x_group == args.label_a
            else report_c.read_text(encoding="utf-8"), encoding="utf-8")
        (task_dir / "report-Y.md").write_text(
            report_c.read_text(encoding="utf-8") if y_group == args.label_c
            else report_a.read_text(encoding="utf-8"), encoding="utf-8")
        (task_dir / "rubric.md").write_text(RUBRIC.format(task_id=task_id),
                                            encoding="utf-8")
        key[task_id] = {"X": x_group, "Y": y_group}
        index_rows.append(f"- [{task_id}]({task_id}/rubric.md)：report-X.md vs report-Y.md")

    (out_dir / "INDEX.md").write_text(
        "# 盲评评审包\n\n"
        f"- 生成：{datetime.now(UTC).isoformat()}（种子 {args.seed}）\n"
        f"- 任务数：{len(key)}；跳过：{len(skipped)}\n\n"
        "## 评审指引\n\n"
        "1. 两位评审独立评全部题目（不打小分前不看另一份）；\n"
        "2. 按 rubric 打分 1–5 并勾总体胜负；不确定就写不确定（不猜）；\n"
        "3. 评审完成后由主持人开封 key.json 计票：C 胜率 + 样本数 + 平局 +\n"
        "   不确定区间一并报告（小样本只支持阶段决策）。\n\n"
        "## 任务清单\n\n" + "\n".join(index_rows) + "\n\n"
        "## 跳过（缺产物，如实记录）\n\n" + ("\n".join(f"- {s}" for s in skipped) or "无")
        + "\n",
        encoding="utf-8",
    )
    key_path = out_dir / "key.json"
    key_path.write_text(json.dumps({
        "sealed_until": "评审完成后开封",
        "labels": {args.label_a: str(dir_a), args.label_c: str(dir_c)},
        "seed": args.seed,
        "mapping": key,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    key_path.chmod(0o600)
    print(f"盲评包：{out_dir}（{len(key)} 题配对；跳过 {len(skipped)}）")
    print(f"映射表（评审后开封）：{key_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
