"""PriceBook：历史行情簿（评估器的对账数据源）。

注意权力边界：PriceBook 只供 Evaluator 使用（算前瞻收益必须看未来），
绝不进入 agent 的工具面——agent 侧的行情查询走时间锁 DataGateway。

成交口径（保守）：决策时刻 T（收盘后）→ 入场 = T 之后首个交易日收盘；
出场 = T+horizon 之后首个交易日收盘。数据不足 → None（incomplete，不计入聚合）。
"""

from __future__ import annotations

from datetime import date


def add_months(d: date, months: int) -> date:
    """日历月加法（月末钳制）。"""
    y, m = divmod(d.year * 12 + (d.month - 1) + months, 12)
    m += 1
    # 月末钳制
    for day in (31, 30, 29, 28):
        try:
            return date(y, m, min(d.day, day))
        except ValueError:
            continue
    raise AssertionError("unreachable")


class PriceBook:
    def __init__(self, closes: dict[str, dict[date, float]]):
        # 按交易日升序存 [(date, close)]
        self._series = {
            ticker: sorted(bars.items()) for ticker, bars in closes.items()
        }

    @classmethod
    def from_records(cls, records: list[dict]) -> PriceBook:
        """从网关价格记录构造：{"ticker", "date", "close"}。"""
        closes: dict[str, dict[date, float]] = {}
        for r in records:
            d = date.fromisoformat(r["date"]) if isinstance(r["date"], str) else r["date"]
            closes.setdefault(r["ticker"], {})[d] = float(r["close"])
        return cls(closes)

    def forward_return(
        self, ticker: str, t: date, *, horizon_months: int
    ) -> tuple[tuple[date, float], tuple[date, float], float] | None:
        """((入场日, 价), (出场日, 价), 收益率)；数据不足返回 None。"""
        series = self._series.get(ticker)
        if not series:
            return None
        entry = next(((d, c) for d, c in series if d > t), None)
        exit_date = add_months(t, horizon_months)
        exit_ = next(((d, c) for d, c in series if d >= exit_date), None)
        if entry is None or exit_ is None or entry[0] >= exit_[0]:
            return None
        return entry, exit_, exit_[1] / entry[1] - 1.0
