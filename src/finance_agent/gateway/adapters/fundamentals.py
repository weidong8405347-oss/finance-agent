"""基本面快照 adapter（C 级 PIT，research-capability-upgrade §4.5）。

PIT 语义（诚实声明）：市值/股本/财务是「当前快照」，无历史回溯保证 →
C 级（available_at=None；生产模式可用，评估模式 fail-closed 禁用）。
用途：F2/F3 粗筛与档案 valuation 维度的生产态数据源；严肃历史口径以
EDGAR/HKEXnews 披露原文（A 级）为准。

两个 adapter：
- YFinanceFundamentalsAdapter（source_id=fundamentals）：美股为主（yfinance .info）；
- AkshareHKFundamentalsAdapter（source_id=fundamentals_hk）：港股
  （akshare 东财港股快照全表过滤单票；akshare 接口漂移频繁，fail-loud 报错可见）。
"""

from __future__ import annotations

from datetime import datetime  # noqa: F401 - query 签名的 as_of 形参用
from typing import Any

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability

#: info 里提取的字段（yfinance 键 → 档案 payload 键）；缺的键跳过（部分股票字段不全）
_YF_FIELDS = {
    "longName": "name",
    "marketCap": "market_cap",
    "sharesOutstanding": "shares_outstanding",
    "currency": "currency",
    "exchange": "exchange",
    "quoteType": "quote_type",
    "sector": "sector",
    "industry": "industry",
    "totalRevenue": "total_revenue",
    "netIncomeToCommon": "net_income",
    "operatingCashflow": "operating_cashflow",
    "freeCashflow": "free_cashflow",
    "trailingPE": "pe_ttm",
    "priceToSalesTrailing12Months": "ps_ttm",
    "currentPrice": "price",
    "website": "website",
    "longBusinessSummary": "business_summary",
}


class YFinanceFundamentalsAdapter:
    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="fundamentals",
            pit_grade=PitGrade.C,
            server_side_asof=False,
            description="yfinance 基本面快照（市值/股本/TTM 财务）；当前值无 PIT → C 级",
        )

    def healthcheck(self) -> dict:
        try:
            recs = self.query({"ticker": "AAPL"})
            if recs and recs[0].payload.get("market_cap"):
                return {"ok": True, "detail": "yfinance info 可取"}
            return {"ok": False, "detail": "空结果或缺 market_cap"}
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:  # noqa: ARG002 - C 级源无 as_of 语义
        import yfinance as yf  # lazy

        ticker = str(request["ticker"])
        info: dict[str, Any] = yf.Ticker(ticker).info or {}
        payload = {dst: info[src] for src, dst in _YF_FIELDS.items() if info.get(src) is not None}
        if not payload.get("market_cap") and not payload.get("name"):
            return []  # 无效 ticker / 限流空响应 → 空结果（不编造）
        payload["ticker"] = ticker
        return [
            DataRecord(
                source_id="fundamentals",
                payload=payload,
                available_at=None,  # C 级快照：无 PIT 保证
                url=f"https://finance.yahoo.com/quote/{ticker}",
            )
        ]


class AkshareHKFundamentalsAdapter:
    """港股基本面快照（东财港股全表过滤单票）。ticker 接受 "2228.HK" / "02228" / "2228"。"""

    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="fundamentals_hk",
            pit_grade=PitGrade.C,
            server_side_asof=False,
            description="akshare/东财港股快照（市值/最新价）；当前值无 PIT → C 级",
        )

    @staticmethod
    def _normalize_code(ticker: str) -> str:
        code = ticker.upper().removesuffix(".HK")
        if not code.isdigit():
            raise ValueError(f"港股代码应为数字（收到 {ticker}）")
        return code.zfill(5)

    def healthcheck(self) -> dict:
        try:
            recs = self.query({"ticker": "00700"})
            if recs:
                return {"ok": True, "detail": "akshare 港股快照可取"}
            return {"ok": False, "detail": "空结果"}
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:  # noqa: ARG002
        import akshare as ak  # lazy（重依赖，仅 --extra data）

        code = self._normalize_code(str(request["ticker"]))
        spot = ak.stock_hk_spot_em()  # 东财港股全市场快照（大表，进程内不缓存—— freshness 优先）
        row = spot[spot["代码"] == code]
        if row.empty:
            return []
        r = row.iloc[0]
        payload = {
            "ticker": f"{code}.HK",
            "name": r.get("名称"),
            "price": _fnum(r.get("最新价")),
            "market_cap": _fnum(r.get("总市值")),
            "currency": "HKD",
            "exchange": "HKEX",
        }
        return [
            DataRecord(
                source_id="fundamentals_hk",
                payload={k: v for k, v in payload.items() if v is not None},
                available_at=None,  # C 级快照
                url=f"https://quote.eastmoney.com/hk/{code}.html",
            )
        ]


def _fnum(v) -> float | None:
    """东财快照的数值列可能是 '-' 等占位符；转 float，失败 → None（宁缺勿假）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
