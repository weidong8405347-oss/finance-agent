"""Stooq 行情日线 adapter（A 级 PIT，零新依赖——httpx 直连 CSV）。

引入原因（2026-08-30）：yfinance 依赖装不上时行情能力停摆；
Stooq 免费 CSV 日线（stooq.com/q/d/l/?s=<ticker>.us&i=d）直连即可。

可用性实录（2026-08-31，真实冒烟）：CSV 端点已全局启用 JS PoW 反爬——
直连返回 200 挑战页（解析不出行情行 → 空结果），经代理返回 404。
两种姿态都降级为空列表，不抛错：作为回退链首跳（PRICE_SOURCE_ORDER），
不可达时由 DataGateway.query_any 回退 yfinance；eval 对账侧的硬失败
门禁在 PriceBook.from_gateway(require=True)。

PIT 语义与 YFinancePricesAdapter 一致（保守）：available_at = 交易日 +1d。
代码映射：美股 ticker 小写 + ".us"（AAPL → aapl.us）；暂不支持的市场在查询时明确报错。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability

_URL = "https://stooq.com/q/d/l/"


class StooqPricesAdapter:
    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="prices_stooq",
            pit_grade=PitGrade.A,
            server_side_asof=True,  # 客户端按日期参数过滤，网关层再复核
            description="Stooq 日线 OHLCV（CSV，免费无 key）；available_at = 交易日 +1d",
        )

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
        import httpx  # lazy：核心与测试不依赖网络库

        ticker = str(request["ticker"])
        symbol = self._to_symbol(ticker)
        params = {
            "s": symbol,
            "i": "d",
            "d1": str(request.get("start") or "2000-01-01").replace("-", ""),
            "d2": str(request.get("end") or date.today().isoformat()).replace("-", ""),
        }
        try:
            resp = httpx.get(_URL, params=params, timeout=30)
            resp.raise_for_status()
        except httpx.HTTPError:
            # 免费源可用性漂移（限流/404/网络）→ 空结果，回退链继续；
            # 硬失败门禁在 PriceBook.from_gateway(require=True)。
            return []
        text = resp.text.strip()
        if not text or "Exceeded" in text or text.startswith("No data"):
            return []

        records: list[DataRecord] = []
        for line in text.splitlines()[1:]:  # 表头 Date,Open,High,Low,Close,Volume
            cols = line.split(",")
            if len(cols) < 6 or cols[4] in ("", "null"):
                continue
            day = date.fromisoformat(cols[0])
            day_dt = datetime.combine(day, time(0, 0), tzinfo=UTC)
            available_at = day_dt + timedelta(days=1)
            if as_of is not None and available_at > as_of:
                continue
            records.append(
                DataRecord(
                    source_id="prices_stooq",
                    payload={
                        "ticker": ticker,
                        "date": day.isoformat(),
                        "open": float(cols[1]),
                        "high": float(cols[2]),
                        "low": float(cols[3]),
                        "close": float(cols[4]),
                        "volume": int(float(cols[5] or 0)),
                    },
                    available_at=available_at,
                    event_time=day_dt,
                )
            )
        return records

    @staticmethod
    def _to_symbol(ticker: str) -> str:
        if ticker.endswith((".us", ".uk", ".de", ".jp")):
            return ticker.lower()
        if ticker.isdigit():
            raise ValueError(
                f"Stooq adapter 暂不支持数字代码市场（{ticker}）；"
                "用美股代码或等 PyPI 恢复后走 yfinance"
            )
        return f"{ticker.lower()}.us"
