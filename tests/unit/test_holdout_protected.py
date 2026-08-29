"""holdout 预算（私有评测集查询预算制）+ protected paths（权力分离的技术落地）。"""

from datetime import date

import pytest

from finance_agent.evaluation.holdout import BudgetExhausted, HoldoutLedger
from finance_agent.harness.protected import ProtectedPaths, ProtectedPathViolation


def test_holdout_budget_decrements_and_exhausts(tmp_path):
    ledger = HoldoutLedger(tmp_path / "ledger.json")
    assert ledger.remaining("mandate-a") == 10  # 默认预算

    ledger.consume("mandate-a")
    ledger.consume("mandate-a")
    assert ledger.remaining("mandate-a") == 8

    # 换预算重载同一 ledger 文件（持久性）
    ledger2 = HoldoutLedger(tmp_path / "ledger.json")
    assert ledger2.remaining("mandate-a") == 8


def test_holdout_custom_budget_and_exhaustion(tmp_path):
    ledger = HoldoutLedger(tmp_path / "ledger.json")
    for _ in range(2):
        ledger.consume("mandate-b", budget=2)
    with pytest.raises(BudgetExhausted):
        ledger.consume("mandate-b", budget=2)


def test_holdout_assert_allowed_fail_closed(tmp_path):
    ledger = HoldoutLedger(tmp_path / "ledger.json")
    ledger.consume("mandate-c", budget=1)
    with pytest.raises(BudgetExhausted):
        ledger.assert_allowed("mandate-c", budget=1)


def test_protected_paths_guard():
    guard = ProtectedPaths(patterns=["evals/**", "**/evaluation/**", "secrets/**"])
    guard.check_write("knowledge/stocks/AAPL.md")  # 放行
    guard.check_write("README.md")  # 放行
    with pytest.raises(ProtectedPathViolation):
        guard.check_write("evals/mandates/example.json")
    with pytest.raises(ProtectedPathViolation):
        guard.check_write("src/finance_agent/evaluation/replay.py")
    with pytest.raises(ProtectedPathViolation):
        guard.check_write("secrets/holdout.json")


def test_holdout_report_redacts_case_level():
    """holdout 报告只含聚合统计（评估对齐稿 §2.5）。"""
    from finance_agent.evaluation.report import Aggregate, DecisionOutcome, EvalReport

    report = EvalReport(
        eval_run_id="eval-x",
        config_name="holdout",
        config_hash="sha256:x",
        verdict="clean",
        leakage_events=0,
        outcomes=[DecisionOutcome(point=date(2024, 1, 1), ticker="AAA", zone="honest")],
        aggregate=Aggregate(
            n_points=1, n_complete=1, mean_net_return=0.01, hit_rate=1.0,
            excess_vs_bh=0.0, llm_only_mean_net_return=0.0, kb_delta=0.01,
            sharpe=1.0, max_drawdown=0.0, psr=0.5, dsr=0.4,
            dev_mean_net=0.01, holdout_mean_net=0.0, generalization_gap=0.01,
        ),
    )
    redacted = report.redacted()
    assert redacted.outcomes == []
    assert redacted.aggregate.mean_net_return == 0.01
