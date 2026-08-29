"""PriceBook / CostModel / 统计函数契约。"""

from datetime import date

from finance_agent.evaluation.costs import CostModel
from finance_agent.evaluation.prices import PriceBook
from finance_agent.evaluation.stats import deflated_sharpe, max_drawdown, prob_sharpe, sharpe


def make_prices() -> PriceBook:
    # 月末收盘：2023-01 ~ 2023-12，单调 100 → 210
    closes = {}
    for i, month in enumerate(range(1, 13)):
        closes[date(2023, month, 28)] = 100.0 + i * 10.0
    return PriceBook({"AAA": closes})


def test_forward_return_entry_next_day_exit_at_horizon():
    pb = make_prices()
    # T=2023-03-30 → 入场取 T 之后首个交易日（03-31 无数据 → 04-28）；出场 = T+3m 之后首个交易日
    fr = pb.forward_return("AAA", date(2023, 3, 30), horizon_months=3)
    assert fr is not None
    entry, exit_, ret = fr
    assert entry == (date(2023, 4, 28), 130.0)
    assert exit_ == (date(2023, 7, 28), 160.0)
    assert abs(ret - (160.0 / 130.0 - 1)) < 1e-12


def test_forward_return_incomplete_when_insufficient_data():
    pb = make_prices()
    assert pb.forward_return("AAA", date(2023, 11, 1), horizon_months=6) is None  # 超出数据范围
    assert pb.forward_return("BBB", date(2023, 1, 1), horizon_months=3) is None  # 未知标的


def test_cost_model_round_trip():
    cm = CostModel(commission_bps=5.0, slippage_bps=10.0)
    assert abs(cm.round_trip_cost_rate() - 0.003) < 1e-12  # 双边 (5+10)*2 bps
    assert abs(cm.net_return(0.10, sizing_pct=0.2) - (0.10 - 0.003) * 0.2) < 1e-12


def test_sharpe_basic_properties():
    assert sharpe([0.01] * 4, periods_per_year=4) == 0.0  # 零波动 → 0（约定）
    assert sharpe([0.1, -0.05, 0.02, 0.03], periods_per_year=4) != 0.0
    assert sharpe([], periods_per_year=4) == 0.0


def test_max_drawdown():
    assert abs(max_drawdown([1.0, 1.1, 0.9, 1.0]) - (1.1 - 0.9) / 1.1) < 1e-9
    assert max_drawdown([1.0, 1.05, 1.1]) == 0.0


def test_psr_and_dsr_ranges_and_deflation():
    rets = [0.05, 0.02, -0.01, 0.03, 0.04, -0.02, 0.01, 0.02]
    sr = sharpe(rets, periods_per_year=1)
    psr = prob_sharpe(sr, benchmark_sr=0.0, returns=rets)
    dsr = deflated_sharpe(sr, returns=rets, n_trials=10)
    assert 0.0 <= psr <= 1.0 and 0.0 <= dsr <= 1.0
    # 多重检验校正：试验次数越多，显著性越低
    assert deflated_sharpe(sr, returns=rets, n_trials=100) <= dsr
    assert dsr <= psr + 1e-9
