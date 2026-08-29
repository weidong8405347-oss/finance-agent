"""EDGAR adapter（A 级 PIT 源骨架）。

PIT 语义：filingDate → available_at（何时可知）；reportDate（报告期截止）→ event_time。
支持服务端过滤：传入 as_of 时只返回 filingDate ≤ as_of 的 filing。

注意：网络访问 lazy import httpx；单元测试不依赖网络（用 FixtureAdapter 替代）。
真实使用前需在 request 里带合规 User-Agent（SEC 要求）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


class EdgarAdapter:
    def __init__(self, *, user_agent: str = "finance-agent research (contact: local@example.com)"):
        self._ua = user_agent

    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="edgar",
            pit_grade=PitGrade.A,
            server_side_asof=True,
            description="SEC EDGAR submissions：filingDate 精确到日，A 级 PIT",
        )

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
        import httpx  # lazy：核心与测试不依赖网络库

        cik = str(request["cik"]).zfill(10)
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
