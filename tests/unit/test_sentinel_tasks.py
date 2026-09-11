"""哨兵题集脚手架契约（tools-plugins 方案 §10.1；离线，不做真实付费运行）。

判据：题集冻结且字段完整（题目/目标/期望产物/时限），运行器 --list 离线可用，
待冻结 ticker 的题目显式跳过（不静默跑半截）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TASKS_FILE = REPO_ROOT / "evals" / "sentinel_tasks.yaml"
RUNNER = REPO_ROOT / "scripts" / "run_sentinel.py"

_REQUIRED_KEYS = {"id", "title", "market", "command", "why", "expected",
                  "key_questions", "timeout_minutes"}


def test_sentinel_tasks_frozen_and_complete():
    data = yaml.safe_load(TASKS_FILE.read_text(encoding="utf-8"))
    tasks = data["tasks"]
    assert len(tasks) == 6, "方案 §10.1：先选 6 个哨兵任务跑通"
    ids = [t["id"] for t in tasks]
    assert len(set(ids)) == len(ids), "题目 id 必须唯一（结果文件按 id 落盘）"
    for t in tasks:
        missing = _REQUIRED_KEYS - set(t)
        assert not missing, f"{t.get('id')} 缺冻结字段: {missing}"
        assert t["expected"], f"{t['id']} 期望产物不能为空"
        assert t["command"].startswith("/"), f"{t['id']} 必须是显式 command"
    # 方案哨兵任务建议的覆盖面：财务口径/中文财报/行业技术兑现/更正/后半部 PDF/指引兑现
    joined = " ".join(ids)
    for token in ("be-orders", "2228hk", "ai4s", "restatement", "late-table", "guidance"):
        assert token in joined, f"哨兵覆盖缺 {token}"


def test_runner_list_offline():
    """--list 不装配 LLM/网络（离线可用），列出全部题目。"""
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--list"],
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    for tid in ("be-orders-revenue", "2228hk-chinese-filings", "ai4s-tech-commercial",
                "restatement-handling", "deep-pdf-late-table", "guidance-to-actual"):
        assert tid in proc.stdout


def test_placeholder_task_skipped_explicitly(tmp_path):
    """command 含 {TICKER} 的题目：显式跳过并说明如何冻结（不静默跑半截）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_sentinel", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    task = {"id": "restatement-handling", "title": "t", "market": "US",
            "command": "/research {TICKER} --depth=targeted"}
    result = mod.run_task(task, tmp_path, dry_run=True)
    assert result["status"] == "skipped"
    assert "pick_ticker_hint" in result["reason"] or "冻结" in result["reason"]


# ---------------- P3 题集（24 题：16 迭代 + 8 留出 + 链式刷新） ----------------

TASKS_FILE_24 = REPO_ROOT / "evals" / "sentinel_tasks_24.yaml"


def test_p3_tasks_frozen_24_with_groups():
    """方案 §10.1 P3：24 题 = 12 单股 + 8 行业/比较 + 4 增量刷新；16 iter/8 holdout。"""
    data = yaml.safe_load(TASKS_FILE_24.read_text(encoding="utf-8"))
    tasks = data["tasks"]
    assert len(tasks) == 24
    groups = [t.get("group") for t in tasks]
    assert groups.count("iter") == 16 and groups.count("holdout") == 8
    chains = [t for t in tasks if t.get("chain")]
    assert len(chains) == 4, "历史档案增量刷新 4 题（链式：建档 → 刷新）"
    for t in chains:
        assert len(t["chain"]) == 2 and all(c.startswith("/") for c in t["chain"])
    # 关键任务重复 3 次（测波动，方案 §10.1）
    repeats = {t["id"]: t.get("repeat") for t in tasks if int(t.get("repeat") or 1) > 1}
    assert repeats and all(int(r) == 3 for r in repeats.values())
    ids = [t["id"] for t in tasks]
    assert len(set(ids)) == 24
    # 单股/行业/刷新三类的形态校验
    industry = [t for t in tasks if str(t.get("command", "")).startswith("/industry")]
    assert len(industry) == 8


def test_runner_list_24_and_group_filter():
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--tasks-file", str(TASKS_FILE_24),
         "--group", "holdout", "--list"],
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert "aapl-services-mix" in proc.stdout
    assert "be-orders-revenue" not in proc.stdout, "group=holdout 不应列出迭代组题目"


def test_holdout_gate_blocks_by_default():
    """留出组默认拒跑（预算制保护）：不带 --allow-holdout 时明确跳过。"""
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--tasks-file", str(TASKS_FILE_24),
         "--group", "holdout", "--task", "aapl-services-mix"],
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
    )
    assert proc.returncode == 2
    assert "--allow-holdout" in proc.stderr


def test_holdout_consumes_ledger_budget(tmp_path):
    """带 --allow-holdout 的 dry-run 不扣预算；真实门槛由 HoldoutLedger 把关。"""
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--tasks-file", str(TASKS_FILE_24),
         "--group", "holdout", "--task", "aapl-services-mix",
         "--allow-holdout", "--dry-run", "--data-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert "dry-run" in proc.stdout
