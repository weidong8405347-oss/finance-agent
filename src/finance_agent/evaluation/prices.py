"""PriceBook：历史行情簿（评估器的对账数据源）。

注意权力边界：PriceBook 只供 Evaluator 使用（算前瞻收益必须看未来），
绝不进入 agent 的工具面——agent 侧的行情查询走时间锁 DataGateway。

成交口径（保守）：决策时刻 T（收盘后）→ 入场 = T 之后首个交易日收盘；
出场 = T+horizon 之后首个交易日收盘。数据不足 → None（incomplete，不计入聚合）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING

from ..gateway.adapters.prices import PRICE_SOURCE_ORDER

if TYPE_CHECKING:
    from ..gateway.gateway import DataGateway


class PriceDataUnavailableError(RuntimeError):
    """行情对账数据不可得——eval 不应带着空 PriceBook 静默烧预算。"""


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

    @classmethod
    def from_gateway(
        cls,
        gateway: DataGateway,
        tickers: Sequence[str],
        *,
        sources: tuple[str, ...] = PRICE_SOURCE_ORDER,
        start: str = "2000-01-01",
        end: str = "2100-01-01",
        require: bool = False,
    ) -> PriceBook:
        """按回退次序从网关装配对账行情（evaluator 专用，agent 工具面不可见）。

        require=True：任一标的在所有源上都无数据 → 显式失败（铁律 4）——
        空 PriceBook 会把整轮 LLM 预算烧成全 incomplete。
        """
        payloads: list[dict] = []
        missing: list[str] = []
        for ticker in tickers:
            records = gateway.query_any(sources, {"ticker": ticker, "start": start, "end": end})
            if records:
                payloads.extend(r.payload for r in records)
            else:
                missing.append(ticker)
        if require and missing:
            raise PriceDataUnavailableError(
                f"行情对账数据不可得（尝试源 {list(sources)} 全空）: {missing}；"
                "检查网络/代理（Yahoo 通常需 HTTPS_PROXY）后再评估"
            )
        return cls.from_records(payloads)

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
