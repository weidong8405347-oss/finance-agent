"""HKEXnews 披露易 adapter（A 级 PIT 源，research-capability-upgrade §4.5 港股披露）。

spike 结论（2026-09-01 实测，代理可达）：
- 两步协议：①GET /search/prefix.do?name=<代码>&type=A&market=SEHK&lang=ZH（JSONP）
  解析出内部 stockId；②GET /search/titleSearchServlet.do 带 stockId 查询公告列表；
- **日期必须 YYYYMMDD 紧凑格式**（YYYY/MM/DD 会静默返回空 200——坑）；
- rowRange 参数必填（缺省同样静默空）；
- 记录字段：DATE_TIME（"19/08/2026 18:09"，DD/MM/YYYY HH:MM，香港时间）、TITLE、
  LONG_TEXT（公告类别）、FILE_LINK（PDF 相对路径，不可变文档 → PIT 安全）；
- 披露时刻 = 官方发布时刻 → A 级 PIT；as_of 经 toDate 服务端过滤 + 网关复核。

懒加载 httpx；单元测试不依赖网络（monkeypatch transport）。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability

_PREFIX_URL = "https://www1.hkexnews.hk/search/prefix.do"
_SEARCH_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
_FILE_BASE = "https://www1.hkexnews.hk"
_HKT = timedelta(hours=8)  # 披露时刻为香港时间

_UA = {"User-Agent": "Mozilla/5.0 (finance-agent research)"}
_AJAX = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh",
}


class HKEXNewsAdapter:
    def __init__(self):
        self._stock_id_cache: dict[str, str] = {}  # 代码 → 内部 stockId（进程内缓存）

    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="hkex_news",
            pit_grade=PitGrade.A,
            server_side_asof=True,
            description="HKEXnews 披露易公告（DATE_TIME 披露时刻精确到分钟，A 级 PIT）",
        )

    def healthcheck(self) -> dict:
        try:
            recs = self.query({"ticker": "00700", "days": 30})
            return {"ok": True, "detail": f"披露易可取（30 天 {len(recs)} 条）"}
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
        import httpx  # lazy：核心与测试不依赖网络库

        ticker = str(request["ticker"])
        code = ticker.upper().removesuffix(".HK")
        if not code.isdigit():
            raise ValueError(f"HKEXnews 只接受港股数字代码（收到 {ticker}）")
        stock_id = self._resolve_stock_id(code, httpx)

        to_dt = as_of or datetime.now(UTC)
        days = int(request.get("days", 365))
        from_dt = to_dt - timedelta(days=days)
        params = {
            "sortDir": "0",
            "sortByOptions": "DateTime",
            "category": "0",
            "market": "SEHK",
            "stockId": stock_id,
            "documentType": "-1",
            # 坑（spike 实测）：日期必须 YYYYMMDD 紧凑格式，否则静默返回空 200
            "fromDate": from_dt.strftime("%Y%m%d"),
            "toDate": to_dt.strftime("%Y%m%d"),
            "title": "",
            "searchType": "1",
            "t1code": "-2",
            "t2Gcode": "-2",
            "t2code": "-2",
            "rowRange": "100",  # 必填，缺省静默空
            "lang": "zh",
        }
        resp = httpx.get(_SEARCH_URL, params=params, headers={**_UA, **_AJAX}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("result")
        rows = json.loads(raw) if raw and raw != "null" else []

        records: list[DataRecord] = []
        for r in rows:
            link = r.get("FILE_LINK") or ""
            available_at = _parse_hkt(r.get("DATE_TIME", ""))
            if as_of is not None and available_at is not None and available_at > as_of:
                continue  # 双保险（toDate 已服务端过滤，网关层还会复核一次）
            records.append(
                DataRecord(
                    source_id="hkex_news",
                    payload={
                        "title": r.get("TITLE"),
                        "category": (r.get("LONG_TEXT") or "").strip(),
                        "stock_code": r.get("STOCK_CODE"),
                        "stock_name": r.get("STOCK_NAME"),
                        "file_type": r.get("FILE_TYPE"),
                        "news_id": r.get("NEWS_ID"),
                    },
                    available_at=available_at,
                    event_time=available_at,  # 披露时刻即为真时刻（公告语义）
                    url=f"{_FILE_BASE}{link}" if link else None,
                )
            )
        return records

    def _resolve_stock_id(self, code: str, httpx) -> str:
        """港股代码 → 披露易内部 stockId（prefix.do JSONP；进程内缓存）。"""
        if code in self._stock_id_cache:
            return self._stock_id_cache[code]
        resp = httpx.get(
            _PREFIX_URL,
            params={"callback": "cb", "lang": "ZH", "type": "A", "name": code, "market": "SEHK"},
            headers=_UA,
            timeout=30,
        )
        resp.raise_for_status()
        m = re.search(r"\((.*)\)", resp.text, flags=re.DOTALL)  # 剥 JSONP 外壳
        if not m:
            raise ValueError(f"披露易代码解析返回非 JSONP：{resp.text[:100]}")
        info = json.loads(m.group(1))
        for s in info.get("stockInfo") or []:
            if s.get("code") == code.zfill(5):
                self._stock_id_cache[code] = str(s["stockId"])
                return self._stock_id_cache[code]
        raise KeyError(f"披露易未找到港股代码：{code}")


def _parse_hkt(raw: str) -> datetime | None:
    """"19/08/2026 18:09"（HKT）→ UTC；解析失败 → None（诚实降级，评估模式会被网关丢弃）。"""
    try:
        naive = datetime.strptime(raw.strip(), "%d/%m/%Y %H:%M")
        return (naive - _HKT).replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None
