"""行情日线 adapter（A 级 PIT 源骨架，基于 yfinance）。

PIT 语义（保守）：日线 bar 在当日收盘后才完整可知 →
available_at = 交易日 + 1 天（宁可保守，不抢跑）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability


class YFinancePricesAdapter:
    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="prices",
            pit_grade=PitGrade.A,
            server_side_asof=True,
            description="日线 OHLCV；available_at = 交易日 +1d（保守收盘近似）",
        )

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
        import yfinance as yf  # lazy

        ticker = request["ticker"]
        start = request.get("start")
        end = request.get("end")
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)

        records: list[DataRecord] = []
        for day, row in df.iterrows():
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
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": int(row["Volume"]),
                    },
                    available_at=available_at,
                    event_time=day_dt,
                )
            )
        return records
