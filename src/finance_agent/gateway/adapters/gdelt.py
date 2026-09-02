"""GDELT 新闻 adapter（B 级 PIT 源，免费无 key）。

research-capability-upgrade §4.5：补 catalysts/risks 的新闻维度，全球含中文媒体。
PIT 语义：seendate（GDELT 收录时间，≈发布时刻）→ available_at；
API 支持 startdatetime/enddatetime 服务端过滤 → server_side_asof=True。
"""

from __future__ import annotations

from datetime import UTC, datetime

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability

_API = "https://api.gdeltproject.org/api/v2/doc/doc"


def _parse_seendate(raw: str) -> datetime | None:
    """GDELT seendate 格式 20240101T120000Z；解析失败 → None（诚实降级）。"""
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


class GdeltNewsAdapter:
    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="news_gdelt",
            pit_grade=PitGrade.B,
            server_side_asof=True,  # enddatetime 服务端过滤 + 网关层复核
            description="GDELT DOC 2.0 新闻检索（免费无 key，含中文媒体）；available_at=seendate",
        )

    def healthcheck(self) -> dict:
        """短超时探活（6s）：本机网络观测 GDELT 可能整体不可达（SSL 握手超时），
        探活必须快速失败，不能拖住 command 启动。"""
        try:
            import httpx

            resp = httpx.get(_API, params={"query": "market", "mode": "ArtList",
                                           "format": "json", "maxrecords": 1, "timespan": "1d"},
                             timeout=6)
            resp.raise_for_status()
            return {"ok": True, "detail": "GDELT 可取"}
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
        import httpx  # lazy：核心与测试不依赖网络库

        params = {
            "query": request["query"],
            "mode": "ArtList",
            "format": "json",
            "maxrecords": min(int(request.get("max_records", 25)), 250),
            "timespan": str(request.get("timespan") or "1m"),
            "sort": "HybridRel",
        }
        if as_of is not None:
            # 服务端 PIT 过滤：只收 as_of 之前收录的报道（网关层还会复核一次）
            params["enddatetime"] = as_of.strftime("%Y%m%d%H%M%S")
        resp = httpx.get(_API, params=params, timeout=30)
        resp.raise_for_status()
        try:
            articles = resp.json().get("articles") or []
        except ValueError:
            return []  # GDELT 偶发返回非 JSON（限流页等）→ 空结果，不污染证据链

        records: list[DataRecord] = []
        for a in articles:
            if not a.get("url") or not a.get("title"):
                continue
            records.append(
                DataRecord(
                    source_id="news_gdelt",
                    payload={
                        "title": a["title"],
                        "domain": a.get("domain"),
                        "language": a.get("language"),
                        "source_country": a.get("sourceCountry"),
                    },
                    available_at=_parse_seendate(a.get("seendate", "")),
                    event_time=_parse_seendate(a.get("seendate", "")),
                    url=a["url"],
                )
            )
        return records
