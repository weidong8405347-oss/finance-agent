"""行情日线 adapter（A 级 PIT 源，基于 yfinance）。

PIT 语义（保守）：日线 bar 在当日收盘后才完整可知 →
available_at = 交易日 + 1 天（宁可保守，不抢跑）。

yfinance ≥1.7 兼容（2026-08-31 真实首跑暴露）：yf.download 返回
(Price, Ticker) 双层列 DataFrame——先按 Ticker 层降层再取行；旧版单层列同样兼容。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isnan

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability

#: 行情源优先序（多源回退次序，DataGateway.query_any 消费）。
#: stooq 免费 CSV 零 key 更轻量故居首；2026-08-31 观测其 CSV 端点已全局启用
#: JS PoW 反爬（换网络也不通，不做绕过）——实际可用性按序容错：
#: stooq 不可达/被反爬（解析为空）时回退 yfinance（通常需 HTTPS_PROXY）。
PRICE_SOURCE_ORDER: tuple[str, ...] = ("prices_stooq", "prices")


class YFinancePricesAdapter:
    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="prices",
            pit_grade=PitGrade.A,
            server_side_asof=True,
            description="日线 OHLCV；available_at = 交易日 +1d（保守收盘近似）",
        )

    def healthcheck(self) -> dict:
        """探活：取 SPY 近 5 天日线；空结果即视为不健康（限流/网络）。"""
        try:
            start = (datetime.now(UTC) - timedelta(days=7)).date().isoformat()
            recs = self.query({"ticker": "SPY", "start": start})
            if recs:
                return {"ok": True, "detail": "yfinance 日线可取"}
            return {"ok": False, "detail": "空结果（疑似限流/网络不通）"}
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
        import yfinance as yf  # lazy

        ticker = request["ticker"]
        start = request.get("start")
        end = request.get("end")
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
        if df is None or df.empty:
            return []
        df = _flatten_columns(df)

        records: list[DataRecord] = []
        for day, row in df.iterrows():
            try:
                open_, high, low, close = (
                    float(row[k]) for k in ("Open", "High", "Low", "Close")
                )
                volume = int(float(row["Volume"]))
            except (TypeError, ValueError):
                continue  # 限流/退市股常见 NaN 行——跳过，不污染数值链
            if isnan(open_) or isnan(high) or isnan(low) or isnan(close):
                continue
            day_dt = day.to_pydatetime().replace(tzinfo=UTC)
            available_at = day_dt + timedelta(days=1)
            if as_of is not None and available_at > as_of:
                continue
            records.append(
                DataRecord(
                    source_id="prices",
                    payload={
                        "ticker": ticker,
                        "date": day_dt.date().isoformat(),
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                    },
                    available_at=available_at,
                    event_time=day_dt,
                )
            )
        return records


def _flatten_columns(df):
    """yfinance ≥1.7 双层列 (Price, Ticker) → 单层 Price；单层列原样返回。"""
    import pandas as pd

    if not isinstance(df.columns, pd.MultiIndex):
        return df
    level = "Ticker" if "Ticker" in (df.columns.names or []) else df.columns.nlevels - 1
    return df.droplevel(level, axis=1)
