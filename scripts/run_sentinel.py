#!/usr/bin/env python3
"""哨兵题集运行器（tools-plugins 方案 §10.1/§10.2 的评估脚手架）。

对 evals/sentinel_tasks.yaml 的冻结题目跑真实研究管道（/research、/industry），
采集确定性验收信号（研究充分度评估、产物状态、预算与重复资料、耗时），
落成可对照的 JSON 结果——A（基线）/B（工具改善）/C（工具+流程）组间比较的输入。

这是**真实付费运行**（LLM + 网络数据源），不会在 CI/测试中自动执行。

用法：
    # 只列出题集（离线，不装配）
    uv run python scripts/run_sentinel.py --list

    # 干跑：校验题集与装配可用性，不发起研究
    uv run python scripts/run_sentinel.py --task be-orders-revenue --dry-run

    # 真实运行（默认隔离数据目录 data/sentinel/<ts>，不碰生产档案）
    uv run python scripts/run_sentinel.py --task be-orders-revenue
    uv run python scripts/run_sentinel.py --all --data-dir data/sentinel/custom

结果：<data-dir>/sentinel-results/<task>.json + 终端摘要表。
待冻结 ticker 的题目（command 含 {TICKER}）会被显式跳过并说明原因。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

TASKS_FILE = REPO_ROOT / "evals" / "sentinel_tasks.yaml"

#: 采集的事件类型（确定性验收信号，方案 §10.2 指标的可自动采集子集）
_COLLECT_TYPES = (
    "research/assessment",        # 充分度评估：verdict/覆盖/硬门禁/来源质量分档
    "research/artifact_created",  # 产物状态（draft/validated）与充分度
    "research/budget",            # 预算消耗/终止原因/重复资料计数
    "research/partial_published", # 部分成果（取消/超时路径的保留成果）
    "research/question_stall",    # 问题零推进诊断
    "research/stall_diagnostic",  # 停滞诊断
    "command/done",               # 命令结果与摘要
    "step_agent/end",             # 各 step 状态
    "dossier/published",          # 快照发布
    "gateway/preflight",          # 数据源预检（缺 key 的源可见）
)


def load_tasks() -> list[dict]:
    import yaml

    data = yaml.safe_load(TASKS_FILE.read_text(encoding="utf-8"))
    tasks = data.get("tasks") or []
    if not tasks:
        raise SystemExit(f"题集为空: {TASKS_FILE}")
    return tasks


def cmd_list() -> int:
    tasks = load_tasks()
    print(f"哨兵题集（frozen {TASKS_FILE}）：{len(tasks)} 题")
    for t in tasks:
        pending = "（待冻结 ticker）" if "{TICKER}" in str(t.get("command", "")) else ""
        print(f"- {t['id']}: {t.get('title', '')} [{t.get('market', '?')}]{pending}")
        print(f"    {t.get('command', '')}")
    return 0


def _collect_events(events, since_seq: int) -> list[dict]:
    rows = events._conn.execute(  # noqa: SLF001 - 脚本层只读投影（与 steps 投影同法）
        "SELECT seq, run_id, type, payload, ts FROM events"
        " WHERE seq > ? AND type IN ({}) ORDER BY seq".format(
            ",".join("?" for _ in _COLLECT_TYPES)
        ),
        (since_seq, *_COLLECT_TYPES),
    ).fetchall()
    out = []
    for seq, run_id, type_, payload, ts in rows:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = {"raw": payload[:500]}
        out.append({"seq": seq, "run_id": run_id, "type": type_, "ts": ts, "payload": data})
    return out


def _extract_signals(collected: list[dict], session: str) -> dict:
    """从事件流提取确定性验收信号（人工评审项不在这里，见方案 §10.2 盲评）。"""
    signals: dict = {"session": session}
    assessments = [e["payload"] for e in collected if e["type"] == "research/assessment"]
    if assessments:
        a = assessments[-1]
        cov = a.get("question_coverage") or {}
        signals["assessment"] = {
            "verdict": a.get("verdict"),
            "hard_gate_passed": a.get("hard_gate_passed"),
            "coverage": cov.get("coverage"),
            "key_coverage": cov.get("key_coverage"),
            "answered": cov.get("answered"), "applicable": cov.get("applicable"),
            "violations": cov.get("violations"),
            "evidence_quality": a.get("evidence_quality"),
            "analytical_depth": a.get("analytical_depth"),
            "numeric_consistency": a.get("numeric_consistency"),  # F17：量表离群/同值异键扫描进信号采集
            "integrity_checks": [
                {"name": c.get("name"), "passed": c.get("passed"), "detail": c.get("detail")}
                for c in (a.get("integrity_checks") or [])
            ],
            "gaps": a.get("gaps"), "notes": a.get("notes"),
            "stop_reason": a.get("stop_reason"),
        }
    artifacts = [e["payload"] for e in collected if e["type"] == "research/artifact_created"]
    signals["artifacts"] = [
        {"artifact_id": p.get("artifact_id"), "status": p.get("status"),
         "sufficiency": p.get("sufficiency")}
        for p in artifacts
    ]
    budgets = [e["payload"] for e in collected
               if e["type"] == "research/budget" and e["payload"].get("action") == "research_end"]
    if budgets:
        b = budgets[-1]
        budget = b.get("budget") or {}
        signals["budget"] = {
            "stop_reason": b.get("stop_reason"), "exhausted": b.get("exhausted"),
            "duplicate_chunks": budget.get("duplicate_chunks"),
            "unique_chunks": budget.get("unique_chunks"),
            "duplicate_documents": budget.get("duplicate_documents"),
            "documents_stored": budget.get("documents_stored"),
            "spent": budget.get("spent"),
        }
    stalls = [e["payload"] for e in collected if e["type"] == "research/question_stall"]
    signals["question_stalls"] = [s.get("causes") for s in stalls]
    done = [e["payload"] for e in collected
            if e["type"] == "command/done" and e["payload"].get("session") in (None, session)]
    signals["command_done"] = done[-1] if done else None
    steps = [e["payload"] for e in collected if e["type"] == "step_agent/end"]
    signals["steps"] = [{"step": s.get("step"), "status": s.get("status"),
                         "summary": str(s.get("summary") or "")[:300]} for s in steps]
    signals["dossier_published"] = [
        e["payload"] for e in collected if e["type"] == "dossier/published"
    ]
    preflight = [e["payload"] for e in collected if e["type"] == "gateway/preflight"]
    signals["gateway_preflight"] = preflight[-1] if preflight else None
    return signals


def run_task(task: dict, data_dir: Path, *, dry_run: bool,
             auto_approve_gates: bool = False) -> dict:
    task_id = str(task["id"])
    command = str(task.get("command") or "")
    result: dict = {
        "task_id": task_id, "title": task.get("title"), "market": task.get("market"),
        "command": command,
        "started_at": datetime.now(UTC).isoformat(),
        "environment": _environment(auto_approve_gates),
    }
    if "{TICKER}" in command:
        result.update({
            "status": "skipped",
            "reason": ("题目待冻结 ticker（command 含 {TICKER}）——按题集 pick_ticker_hint "
                       "选定标的并写回 evals/sentinel_tasks.yaml 后再跑"),
        })
        return result
    if not command.startswith("/"):
        result.update({"status": "error", "reason": f"command 必须是显式 command：{command!r}"})
        return result

    from finance_agent.cli import build_orchestrator
    from finance_agent.commands.registry import parse_command
    from finance_agent.commands.runner import CommandRequest

    parsed = parse_command(command)
    if parsed is None or not parsed.name:
        result.update({"status": "error", "reason": f"命令解析失败: {command!r}"})
        return result
    if dry_run:
        result.update({"status": "dry-run", "parsed": {
            "name": parsed.name, "ticker": parsed.ticker,
            "objective": parsed.objective, "extra": parsed.extra,
        }})
        return result

    orch = build_orchestrator(data_dir)
    events = orch["events"]
    runner = orch["command_runner"]
    # 哨兵基线是无人值守的可复现测量：禁用主 agent 唤醒（否则 command 结束后
    # 主 agent 会自主在同一会话重发研究命令，污染题目范围与预算；
    # 2026-09-10 r2 实测：stalled 后主 agent 自発 10 题 standard 重试）
    runner.set_wake(None)
    # 全库事件水位：只采集本题目运行期间新增的事件
    row = events._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()  # noqa: SLF001
    since_seq = int(row[0])
    session = f"sentinel-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{task_id}"
    timeout_s = float(task.get("timeout_minutes") or 60) * 60

    t0 = time.monotonic()
    command_id = runner.start(CommandRequest(session_run_id=session, parsed=parsed))

    # 人工闸口代行（--auto-approve-gates）：基线运行需要无人值守可复现；
    # 每次代行都落 approval/decided 审计事件 + 记入结果 JSON（非人工判断，如实标注）
    auto_approvals: list[dict] = []
    stop_watch = threading.Event()

    def watch_gates() -> None:
        approvals = orch["approvals"]
        while not stop_watch.is_set():
            try:
                for req in approvals.pending():
                    approvals.decide(
                        req.approval_id, True,
                        comment="哨兵基线运行：脚本代行人工闸口（非人工判断，"
                                "见 run_sentinel --auto-approve-gates）",
                    )
                    auto_approvals.append({
                        "approval_id": req.approval_id,
                        "op": (req.detail or {}).get("op"),
                        "round": (req.detail or {}).get("round"),
                        "recommended": (req.detail or {}).get("recommended"),
                        "at": datetime.now(UTC).isoformat(),
                    })
            except Exception as e:  # noqa: BLE001 - 监视线程失败不拖死主运行，但留痕
                auto_approvals.append({"watcher_error": f"{type(e).__name__}: {e}"})
            stop_watch.wait(1.0)

    watcher = None
    if auto_approve_gates:
        watcher = threading.Thread(target=watch_gates, daemon=True,
                                   name=f"sentinel-gate-{task_id}")
        watcher.start()

    done_payload = None
    deadline = t0 + timeout_s
    # 宽限窗口（基线实测教训）：command 线程是 daemon，主进程退出会把进行中的 step
    # 静默杀死（ai4s 首跑 120 分钟到点时 rank_report 正在写报告，F5 成果丢失）。
    # 超时后不立即返回：再等宽限窗口内 command 自然完成，基准数据完整性优先于准时。
    grace_s = 2700.0  # 45 分钟
    grace_deadline = deadline + grace_s
    timed_out = False
    while True:
        rows = events.read(session)
        done = [e for e in rows if e.type == "command/done"
                and e.payload.get("command_id") == command_id]
        if done:
            done_payload = done[0].payload
            break
        now_m = time.monotonic()
        if now_m >= deadline:
            if not timed_out:
                timed_out = True
                print(f"   ⚠ {task_id} 超过题目时限 {timeout_s / 60:.0f} 分钟，"
                      f"进入宽限窗口（{grace_s / 60:.0f} 分钟）等待进行中 step 完成——"
                      "不杀 daemon 线程，避免丢失在飞成果")
            if now_m >= grace_deadline:
                break
        time.sleep(2.0)
    elapsed = time.monotonic() - t0
    stop_watch.set()
    if watcher is not None:
        watcher.join(timeout=3.0)

    collected = _collect_events(events, since_seq)
    signals = _extract_signals(collected, session)
    status = "completed" if done_payload and not timed_out else (
        "completed_after_grace" if done_payload else "timeout")
    result.update({
        "status": status,
        "elapsed_seconds": round(elapsed, 1),
        "grace_used": timed_out,
        "command_done": done_payload,
        "auto_approvals": auto_approvals,
        "signals": signals,
        "events_collected": len(collected),
        "data_dir": str(data_dir),
        "note": ("超时不代表无成果：检查 partial_published 与已落库档案"
                 if not done_payload else ""),
    })
    return result


def _environment(auto_approve_gates: bool) -> dict:
    """A 组基线冻结（方案 §10.1）：代码版本与模型配置随结果存档，事后可归因。"""
    env: dict = {"auto_approve_gates": auto_approve_gates}
    try:
        env["git_commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
            text=True, timeout=10,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - 环境信息缺失不阻断运行，但如实留空
        env["git_commit"] = "unknown"
    try:
        from finance_agent.cli import _router

        router = _router(None)
        env["models"] = {}
        for role in ("research", "fast"):
            try:
                env["models"][role] = getattr(router.get(role), "model_name", "?")
            except Exception as e:  # noqa: BLE001
                env["models"][role] = f"error: {type(e).__name__}"
    except Exception as e:  # noqa: BLE001
        env["models"] = {"error": str(e)[:100]}
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="哨兵题集运行器（真实付费运行，默认隔离数据目录）")
    ap.add_argument("--list", action="store_true", help="只列出题集")
    ap.add_argument("--task", action="append", default=[], help="按 id 选题（可多次）")
    ap.add_argument("--all", action="store_true", help="跑全部题目")
    ap.add_argument("--dry-run", action="store_true", help="校验题目与解析，不发起研究")
    ap.add_argument("--auto-approve-gates", action="store_true",
                    help="代行人工闸口（如 /industry F3）：基线无人值守可复现；"
                         "每次代行落审计事件并记入结果（非人工判断）")
    ap.add_argument("--data-dir", default="", help="隔离数据目录（默认 data/sentinel/<ts>）")
    args = ap.parse_args(argv)

    tasks = load_tasks()
    if args.list:
        return cmd_list()
    if args.all:
        selected = tasks
    else:
        by_id = {str(t["id"]): t for t in tasks}
        unknown = [i for i in args.task if i not in by_id]
        if unknown:
            print(f"未知题目 id: {unknown}（可用：{sorted(by_id)}）", file=sys.stderr)
            return 2
        # 按 --task 参数顺序执行（先快后慢：短题先验证管道，长题后置）
        selected = [by_id[i] for i in args.task]
    if not selected:
        print(f"未选择题目（--task {'/'.join(str(t['id']) for t in tasks)} 或 --all）",
              file=sys.stderr)
        return 2
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    data_dir = Path(args.data_dir) if args.data_dir else REPO_ROOT / "data" / "sentinel" / ts
    data_dir.mkdir(parents=True, exist_ok=True)
    out_dir = data_dir / "sentinel-results"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        # 与 serve 同一约定：.env 的 HTTPS_PROXY/HTTP_PROXY 自动注入进程环境
        from finance_agent.cli import _apply_dotenv_proxy

        applied = _apply_dotenv_proxy(REPO_ROOT / ".env")
        if applied:
            print(f"代理已注入：{applied}")

    failures = 0
    for task in selected:
        task_id = str(task["id"])
        print(f"\n== {task_id}: {task.get('title')} ==\n   {task.get('command')}")
        try:
            result = run_task(task, data_dir, dry_run=args.dry_run,
                              auto_approve_gates=args.auto_approve_gates)
        except Exception as e:  # noqa: BLE001 - 单题失败不拖死整批，但必须可见
            result = {"task_id": task_id, "status": "error",
                      "error": f"{type(e).__name__}: {e}"}
        path = out_dir / f"{task_id}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
        status = result.get("status")
        verdict = (result.get("signals") or {}).get("assessment", {}).get("verdict")
        print(f"   → {status}" + (f"（verdict={verdict}）" if verdict else "")
              + f"  结果: {path}")
        if status not in ("completed", "completed_after_grace", "dry-run", "skipped"):
            failures += 1
    print(f"\n完成：{len(selected) - failures}/{len(selected)}；结果目录 {out_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
