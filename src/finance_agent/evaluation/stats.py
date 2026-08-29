"""评估统计：Sharpe / MDD / PSR / DSR（Bailey & López de Prado 的防过拟合纪律）。

DSR 校正两类业绩虚高：多重检验选择偏差（n_trials）+ 收益非正态（偏度/峰度）。
仅用标准库实现（statistics.NormalDist 提供 Φ 与 Φ⁻¹）。
"""

from __future__ import annotations

import math
from statistics import NormalDist

_EULER_MASCHERONI = 0.5772156649015329
_ND = NormalDist()


def sharpe(returns: list[float], *, periods_per_year: float) -> float:
    if len(returns) < 2:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    if std == 0.0:
        return 0.0
    return mean / std * math.sqrt(periods_per_year)


def max_drawdown(equity_curve: list[float]) -> float:
    peak, mdd = float("-inf"), 0.0
    for x in equity_curve:
        peak = max(peak, x)
        if peak > 0:
            mdd = max(mdd, (peak - x) / peak)
    return mdd


def _skew_excess_kurt(returns: list[float]) -> tuple[float, float]:
    n = len(returns)
    if n < 3:
        return 0.0, 0.0
    mean = sum(returns) / n
    m2 = sum((r - mean) ** 2 for r in returns) / n
    if m2 == 0:
        return 0.0, 0.0
    m3 = sum((r - mean) ** 3 for r in returns) / n
    m4 = sum((r - mean) ** 4 for r in returns) / n
    skew = m3 / m2**1.5
    excess_kurt = m4 / m2**2 - 3.0
    return skew, excess_kurt


def prob_sharpe(sr: float, *, benchmark_sr: float, returns: list[float]) -> float:
    """PSR：观察到的 sr 显著高于 benchmark_sr 的概率。"""
    n = len(returns)
    if n < 2:
        return 0.0
    skew, kurt = _skew_excess_kurt(returns)
    # López de Prado 公式中 γ4 为峰度（非超额），此处 excess+3 还原
    denom = math.sqrt(max(1e-12, 1 - skew * sr + ((kurt + 3) - 1) / 4 * sr**2))
    z = (sr - benchmark_sr) * math.sqrt(n - 1) / denom
    return _ND.cdf(z)


def deflated_sharpe(sr: float, *, returns: list[float], n_trials: int) -> float:
    """DSR：校正 N 次试验的选择偏差后的夏普显著性。"""
    n = len(returns)
    if n < 2:
        return 0.0
    if n_trials <= 1:
        return prob_sharpe(sr, benchmark_sr=0.0, returns=returns)
    skew, kurt = _skew_excess_kurt(returns)
    var_sr = max(1e-12, (1 - skew * sr + ((kurt + 3) - 1) / 4 * sr**2) / (n - 1))
    # E[max SR | N 次独立无效试验]（Bailey & López de Prado 2014）
    p1 = min(max(1 - 1 / n_trials, 1e-12), 1 - 1e-12)
    p2 = min(max(1 - 1 / (n_trials * math.e), 1e-12), 1 - 1e-12)
    sr_star = math.sqrt(var_sr) * (
        (1 - _EULER_MASCHERONI) * _ND.inv_cdf(p1) + _EULER_MASCHERONI * _ND.inv_cdf(p2)
    )
    return prob_sharpe(sr, benchmark_sr=sr_star, returns=returns)
