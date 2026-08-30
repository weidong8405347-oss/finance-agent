"""EDGAR adapter（A 级 PIT 源骨架）。

PIT 语义：filingDate → available_at（何时可知）；reportDate（报告期截止）→ event_time。
支持服务端过滤：传入 as_of 时只返回 filingDate ≤ as_of 的 filing。

注意：网络访问 lazy import httpx；单元测试不依赖网络（用 FixtureAdapter 替代）。
真实使用前需在 request 里带合规 User-Agent（SEC 要求）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


def fetch_filing_text(
    url: str, *, user_agent: str = "finance-agent research (contact: local@example.com)"
) -> str:
    """抓取 filing 正文并剥离 HTML（read_edgar_filing 工具的抓取函数）。

    PIT 语义：Archives 下的 filing 文档自发布起不可变，available_at 由
    filing 记录（filingDate）继承——抓取动作本身不产生新的时间线。
    """
    import httpx  # lazy：核心与测试不依赖网络库

    resp = httpx.get(url, headers={"User-Agent": user_agent}, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


class EdgarAdapter:
    def __init__(self, *, user_agent: str = "finance-agent research (contact: local@example.com)"):
        self._ua = user_agent
        self._ticker_map: dict[str, str] | None = None  # ticker -> CIK（惰性加载）

    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="edgar",
            pit_grade=PitGrade.A,
            server_side_asof=True,
            description="SEC EDGAR submissions：filingDate 精确到日，A 级 PIT",
        )

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
        import httpx  # lazy：核心与测试不依赖网络库

        cik = request.get("cik") or self._resolve_cik(request["ticker"], httpx)
        cik = str(cik).zfill(10)
        forms = set(request.get("forms") or [])
        resp = httpx.get(_SUBMISSIONS_URL.format(cik=cik), headers={"User-Agent": self._ua}, timeout=30)
        resp.raise_for_status()
        recent = resp.json()["filings"]["recent"]

        records: list[DataRecord] = []
        for form, filed, period, doc, accession in zip(
            recent["form"],
            recent["filingDate"],
            recent["reportDate"],
            recent["primaryDocument"],
            recent["accessionNumber"],
            strict=True,
        ):
            available_at = datetime.fromisoformat(filed).replace(tzinfo=UTC)
            if as_of is not None and available_at > as_of:
                continue  # 源头过滤（网关层还会复核一次）
            if forms and form not in forms:
                continue
            acc = accession.replace("-", "")
            records.append(
                DataRecord(
                    source_id="edgar",
                    payload={"form": form, "accession": accession},
                    available_at=available_at,
                    event_time=datetime.fromisoformat(period).replace(tzinfo=UTC) if period else None,
                    url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}",
                )
            )
        return records

    def _resolve_cik(self, ticker: str, httpx) -> str:
        """ticker → CIK（SEC 官方映射表，进程内缓存）。"""
        if self._ticker_map is None:
            resp = httpx.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers={"User-Agent": self._ua},
                timeout=30,
            )
            resp.raise_for_status()
            self._ticker_map = {
                row["ticker"].upper(): str(row["cik_str"]) for row in resp.json().values()
            }
        cik = self._ticker_map.get(ticker.upper())
        if cik is None:
            raise KeyError(f"EDGAR 未找到 ticker: {ticker}")
        return cik
