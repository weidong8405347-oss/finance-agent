"""SEC XBRL companyfacts adapter（A 级 PIT 源，tools-plugins 方案 §5.2/§7.1「必做」）。

让财务数字直接来自结构化披露：原始 tag、unit、期间、filing 版本完整，
带 accession 与 filing 原文链接——美股收入/利润/现金流/资产负债不再只靠
正文/PDF 抽取（页、表头、期间、单位可能丢失）的路径。

PIT 语义（方案 §4.1）：每条 fact 自带 `accepted`（受理时刻，分钟精度）与 `filed`；
available_at = acceptance 优先，缺失保守取 filed 日末（不随意当作 UTC 零点公开）。
支持服务端 as_of 过滤（server_side_asof=True）。

边界（方案 §7.1 来源与事实边界）：companyfacts 聚合范围有限——自定义 taxonomy、
部分分部 KPI 可能缺失，仍需回原始 filing 正文（fetch_document 读原件）；
本 adapter 不猜测、不补齐缺失 tag，返回的就是 SEC 聚合到的原始条目。

限速与缓存：SEC 总体速率 ≤10 req/s（与 EdgarAdapter 共享节流）；companyfacts
响应可达数 MB——按 CIK 进程内缓存（TTL 10 分钟），同 run 复用不重复拉取。
网络访问 lazy import httpx；单元测试 monkeypatch httpx.get，不依赖网络。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability
from .edgar import EdgarAdapter, _throttled_get, acceptance_or_eod

logger = logging.getLogger("finance_agent.gateway.edgar_facts")

_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
#: companyfacts 进程内缓存 TTL（新 filing 到达后最迟 10 分钟可见）
_CACHE_TTL_S = 600.0
#: 返回条数上限（schema 里向模型说明：用 tags/forms/period 过滤缩小）
_DEFAULT_LIMIT = 120
_MAX_LIMIT = 400


class EdgarFactsAdapter:
    """query_edgar_facts：结构化财务事实（XBRL 原始 tag + 期间 + filing 版本 + 原文链接）。"""

    def __init__(
        self, *,
        user_agent: str = "finance-agent research (contact: local@example.com)",
        edgar: EdgarAdapter | None = None,
    ):
        self._ua = user_agent
        #: 复用 EdgarAdapter 的 ticker→CIK 解析（官方映射表 + 进程内缓存）
        self._edgar = edgar or EdgarAdapter(user_agent=user_agent)
        self._cache: dict[str, tuple[float, dict]] = {}

    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="edgar_facts",
            pit_grade=PitGrade.A,
            server_side_asof=True,
            description=(
                "SEC XBRL companyfacts：结构化财务事实（原始 tag/unit/期间/filing 版本），"
                "公开时刻取 acceptance（分钟精度），A 级 PIT；分部/自定义口径仍需回原文"
            ),
        )

    def healthcheck(self) -> dict:
        """探活用小表（company_tickers.json）；不拉数 MB 的 companyfacts 全量。"""
        try:
            import httpx  # lazy

            _throttled_get(httpx, _TICKERS_URL, {"User-Agent": self._ua}, 8)
            return {"ok": True, "detail": "SEC XBRL 端点可达（companyfacts）"}
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
        import httpx  # lazy

        cik = request.get("cik") or self._edgar._resolve_cik(  # noqa: SLF001 - 复用解析
            str(request.get("ticker") or ""), httpx
        )
        cik = str(cik).zfill(10)
        data = self._companyfacts(httpx, cik)

        tags = {str(t).strip() for t in (request.get("tags") or []) if str(t).strip()}
        forms = {str(f).strip() for f in (request.get("forms") or []) if str(f).strip()}
        units = {str(u).strip() for u in (request.get("units") or []) if str(u).strip()}
        try:
            limit = max(1, min(int(request.get("limit") or _DEFAULT_LIMIT), _MAX_LIMIT))
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT
        period_start = str(request.get("period_start") or "")  # 期间末不早于
        period_end = str(request.get("period_end") or "")      # 期间末不晚于

        entity_name = data.get("entityName") or ""
        out: list[DataRecord] = []
        facts = data.get("facts") or {}
        for taxonomy in ("us-gaap", "ifrs-full", "dei"):
            for tag, tag_data in (facts.get(taxonomy) or {}).items():
                if tags and tag not in tags:
                    continue
                label = str(tag_data.get("label") or tag)
                for unit, entries in (tag_data.get("units") or {}).items():
                    if units and unit not in units:
                        continue
                    for e in entries:
                        record = self._entry_record(
                            cik, entity_name, taxonomy, tag, label, unit, e,
                            as_of=as_of, forms=forms,
                            period_start=period_start, period_end=period_end,
                        )
                        if record is not None:
                            out.append(record)
        # 最近披露在前（同刻按 tag 稳定排序）；limit 截断在 schema 中向模型说明
        out.sort(key=lambda r: (r.available_at or datetime.min.replace(tzinfo=UTC),
                                str(r.payload.get("tag") or "")), reverse=True)
        return out[:limit]

    # ---------------- 内部 ----------------

    def _entry_record(
        self, cik: str, entity_name: str, taxonomy: str, tag: str, label: str,
        unit: str, e: dict, *, as_of: datetime | None, forms: set[str],
        period_start: str, period_end: str,
    ) -> DataRecord | None:
        filed = e.get("filed")
        if not filed:
            return None  # 无公开日期 = 无法做时间准入（诚实丢弃，不猜）
        accepted = e.get("accepted")
        available_at = acceptance_or_eod(str(filed), accepted)
        if as_of is not None and available_at > as_of:
            return None  # 源头过滤（网关层还会复核一次）
        form = str(e.get("form") or "")
        if forms and form not in forms:
            return None
        start, end = e.get("start"), e.get("end")
        # 期间过滤（都按「期间末」比较；instant 无 end 用 filed 语义不比较）
        effective_end = str(end or "")
        if period_start and effective_end and effective_end < period_start:
            return None
        if period_end and effective_end and effective_end > period_end:
            return None
        accn = str(e.get("accn") or "")
        accn_dashless = accn.replace("-", "")
        fy, fp = e.get("fy"), e.get("fp")
        event_time = None
        for d in (end, start):
            if d:
                try:
                    event_time = datetime.fromisoformat(str(d)).replace(tzinfo=UTC)
                    break
                except ValueError:
                    continue
        val = e.get("val")
        return DataRecord(
            source_id="edgar_facts",
            payload={
                "entity": entity_name,
                "taxonomy": taxonomy,
                "tag": tag,
                "label": label,
                "unit": unit,
                "val": val,
                # 逐字值文本：数字保护（propose_metric 值⊆摘录）可直接引用
                "value_text": str(val) if val is not None else "",
                "start": start,
                "end": end,
                "fy": fy,
                "fp": fp,
                "fiscal_label": f"FY{fy} {fp}" if fy and fp else "",
                "form": form,
                "accn": accn or None,
                "filed": filed,
                "accepted": accepted,
                "frame": e.get("frame"),
                "cik": cik,
            },
            available_at=available_at,
            event_time=event_time,
            # 原文链接：accession 目录（可下钻 primary document / index.json）
            url=(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn_dashless}/"
                 if accn_dashless else None),
        )

    def _companyfacts(self, httpx_mod, cik: str) -> dict:
        now = time.monotonic()
        cached = self._cache.get(cik)
        if cached is not None and now - cached[0] < _CACHE_TTL_S:
            return cached[1]
        resp = _throttled_get(
            httpx_mod, _COMPANYFACTS_URL.format(cik=cik),
            {"User-Agent": self._ua}, 60,
        )
        data = resp.json()
        if not isinstance(data, dict) or "facts" not in data:
            # fail-loud：响应形状异常不静默变空结果（错误不被包装为空）
            raise ValueError(f"companyfacts 响应形状异常（CIK={cik}）：缺 facts 键")
        self._cache[cik] = (now, data)
        return data
