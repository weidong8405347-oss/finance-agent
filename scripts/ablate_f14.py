#!/usr/bin/env python3
"""F14 消融驱动（B 组发现：ai4s F1 行业 typed 观测回归 15→0，需消融定位）。

对同一冻结主题跑两次 F1（industry_map 单步，经 /industry 命令收窄 steps）：
- 变体 on：默认装配（状态卡开启）；
- 变体 off：FA_ABLATE_STATE_CARD=1（状态卡语义压缩回流关闭）。

回答的问题（判读留给报告，不自动下结论）：状态卡回流是否挤压了 F1 的采集步数
（typed 观测数 / 检索与模型调用预算 / 耗时 / 问题覆盖）。

真实付费运行（LLM + 网络数据源）；隔离数据目录 data/sentinel/ablate-f14/<variant>/，
不碰生产档案。用法：
    uv run python scripts/ablate_f14.py                 # 双跑（先 on 后 off）
    uv run python scripts/ablate_f14.py --variant off   # 只跑某变体
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

TOPIC = "ai-for-science"  # 与哨兵题 ai4s-tech-commercial 同主题（B 组回归现场）
TIMEOUT_MIN = 30


def _run_variant(variant: str, *, state_card_off: bool, base_dir: Path) -> dict:
    """单变体运行：F1 单步（industry 命令收窄 steps）+ 信号采集。"""
    if state_card_off:
        os.environ["FA_ABLATE_STATE_CARD"] = "1"
    else:
        os.environ.pop("FA_ABLATE_STATE_CARD", None)

    from finance_agent.cli import build_orchestrator
    from finance_agent.commands.registry import COMMANDS, parse_command
    from finance_agent.commands.runner import CommandRequest

    data_dir = base_dir / variant
    data_dir.mkdir(parents=True, exist_ok=True)
    orch = build_orchestrator(data_dir)
    runner = orch["command_runner"]
    runner.set_wake(None)  # 无人值守测量：禁用主 agent 自主重发（哨兵纪律）

    parsed = parse_command(f"/industry {TOPIC}")
    assert parsed is not None and parsed.name == "industry"
    # F1 单步收窄（消融诊断不需要全漏斗；其余 step 不属于本问题）
    spec = COMMANDS["industry"]
    COMMANDS["industry"] = dataclasses.replace(spec, steps=("industry_map",))

    events = orch["events"]
    since = int(events._conn.execute(  # noqa: SLF001 - 脚本层只读水位
        "SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0])
    session = f"ablate-f14-{variant}-{datetime.now(UTC).strftime('%H%M%S')}"
    t0 = time.monotonic()
    command_id = runner.start(CommandRequest(session_run_id=session, parsed=parsed))
    deadline = t0 + TIMEOUT_MIN * 60
    done_payload = None
    while time.monotonic() < deadline:
        done = [e for e in events.read(session)
                if e.type == "command/done" and e.payload.get("command_id") == command_id]
        if done:
            done_payload = done[0].payload
            break
        time.sleep(2.0)
    elapsed = time.monotonic() - t0

    metrics = orch["metrics"]
    now = datetime.now(UTC)
    observations = metrics.observations_as_of("industry", TOPIC, now)
    claims = metrics.claims_as_of("industry", TOPIC, now)
    new_events = events._conn.execute(  # noqa: SLF001
        "SELECT type, payload FROM events WHERE seq > ?", (since,)).fetchall()
    budgets = [json.loads(p) for t, p in new_events if t == "research/budget"]
    final_budget = next((b for b in reversed(budgets)
                         if b.get("action") == "research_end"), None)
    compressed = [p for t, p in new_events if t == "research/context_compressed"]
    step_ends = [json.loads(p) for t, p in new_events if t == "step_agent/end"]
    return {
        "variant": variant,
        "state_card": "off" if state_card_off else "on",
        "topic": TOPIC,
        "elapsed_seconds": round(elapsed, 1),
        "command_done": done_payload,
        "f1_observations": len(observations),
        "f1_observation_keys": sorted({o.metric_key for o in observations}),
        "f1_claims": len(claims),
        "budget_end": (final_budget or {}).get("budget"),
        "context_compressed_events": len(compressed),
        "step_end_summaries": [s.get("summary", "")[:300] for s in step_ends],
        "data_dir": str(data_dir),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--variant", choices=["on", "off"], default=None,
                    help="只跑某变体（缺省双跑：先 on 后 off）")
    ap.add_argument("--out", default="data/sentinel/ablate-f14",
                    help="输出根目录（隔离数据目录按变体分子目录）")
    args = ap.parse_args(argv)

    base = Path(args.out)
    variants = [args.variant] if args.variant else ["on", "off"]
    results = []
    for v in variants:
        print(f"[ablate-f14] 运行变体 {v}（state_card={'off' if v == 'off' else 'on'}）…",
              flush=True)
        results.append(_run_variant(v, state_card_off=(v == "off"), base_dir=base))
        print(f"[ablate-f14] {v} 完成：obs={results[-1]['f1_observations']} "
              f"claims={results[-1]['f1_claims']} "
              f"elapsed={results[-1]['elapsed_seconds']}s", flush=True)

    out_path = base / f"ablation-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json"
    base.mkdir(parents=True, exist_ok=True)
    payload = {
        "question": "F14：状态卡回流是否挤压 F1 行业 typed 观测采集（B 组回归 15→0）",
        "topic": TOPIC, "created_at": datetime.now(UTC).isoformat(),
        "variants": results,
        "reading": ("判读纪律：两次运行的观测数/预算/耗时对照是消融证据，不是因果证明；"
                    "结论进报告由人判读"),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"[ablate-f14] 结果落盘 {out_path}")
    if len(results) == 2:
        on, off = results[0], results[1]
        print("\n=== F14 消融对照 ===")
        print(f"F1 typed 观测：on={on['f1_observations']} off={off['f1_observations']}")
        print(f"F1 claims：on={on['f1_claims']} off={off['f1_claims']}")
        print(f"耗时：on={on['elapsed_seconds']}s off={off['elapsed_seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
