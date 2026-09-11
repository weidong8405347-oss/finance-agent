"""EDGAR adapter（A 级 PIT 源）。

PIT 语义（tools-plugins 方案 §4.1 时间准入与内容版本绑定）：
- 公开时刻优先取 **acceptanceDateTime**（分钟精度，SEC 受理即公开）→ available_at；
- 只有日精度 filingDate 时保守取当日 **UTC 日末**——不得随意当作零点公开
  （更早 = 评估模式可能提前放行，属穿越方向；日末是安全方向）；
- reportDate（报告期截止）→ event_time；
- 支持服务端过滤：传入 as_of 时只返回公开时刻 ≤ as_of 的 filing。

历史覆盖（方案 §5.2 list_filings）：submissions 的 `recent` 只含最近约千条；
更旧的 filing 在 `filings.files` 分段索引里——按需拉取（每段一次额外请求），
默认 0 段保持旧行为；单段失败降级可见（logger + last_history_errors），不假装没有旧文件。

限速：SEC 要求总体请求速率 ≤ 10 req/s——同进程共享节流（_throttled_get）。
网络访问 lazy import httpx；单元测试不依赖网络（monkeypatch httpx.get）。
真实使用前需在 request 里带合规 User-Agent（SEC 要求）。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability

logger = logging.getLogger("finance_agent.gateway.edgar")

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_SEGMENT_URL = "https://data.sec.gov/submissions/{name}"

# ---------------- SEC fair-access 节流（≤10 req/s，进程内单源共享） ----------------

_RATE_LOCK = threading.Lock()
_LAST_CALL = 0.0
_MIN_INTERVAL_S = 0.12


def _throttled_get(httpx_mod, url: str, headers: dict, timeout: float):
    """带节流的 GET + raise_for_status（SEC 总体速率 ≤10 req/s）。"""
    global _LAST_CALL  # noqa: PLW0603 - 进程内节流状态
    with _RATE_LOCK:
        wait = _MIN_INTERVAL_S - (time.monotonic() - _LAST_CALL)
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL = time.monotonic()
        resp = httpx_mod.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp


def acceptance_or_eod(filed: str, accepted: str | None) -> datetime:
    """filing 的公开时刻：acceptance（分钟精度）优先；缺失保守取 filingDate UTC 日末。

    方案 §4.1：SEC 只有日精度 filingDate 时，不能随意当作 UTC 零点公开——
    零点比真实公开时刻早，评估回放会提前放行（穿越方向）；日末是安全方向。
    acceptance 带时区偏移（美东），统一换算成 UTC。
    """
    if accepted:
        try:
            dt = datetime.fromisoformat(str(accepted).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except ValueError:
            logger.warning("acceptanceDateTime 不可解析（%r），退回 filingDate 日末", accepted)
    day = datetime.fromisoformat(str(filed))
    return day.replace(tzinfo=UTC, hour=23, minute=59, second=59, microsecond=0)


def fetch_filing_text(
    url: str, *, user_agent: str = "finance-agent research (contact: local@example.com)"
) -> str:
    """抓取 filing 正文（read_edgar_filing 工具的抓取函数，旧契约兼容入口）。

    PIT 语义：Archives 下的 filing 文档自发布起不可变，available_at 由
    filing 记录（acceptance/filingDate）继承——抓取动作本身不产生新的时间线。
    P4 起委托给统一抓取器（HTML + PDF 双格式，后者为港股披露）。
    """
    from ..fetch import fetch_document

    return fetch_document(url, user_agent=user_agent)


class EdgarAdapter:
    def __init__(self, *, user_agent: str = "finance-agent research (contact: local@example.com)"):
        self._ua = user_agent
        self._ticker_map: dict[str, str] | None = None  # ticker -> CIK（惰性加载）
        #: 历史分段拉取失败（可见降级：不假装没有旧 filing）
        self.last_history_errors: list[dict[str, str]] = []

    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="edgar",
            pit_grade=PitGrade.A,
            server_side_asof=True,
            description=(
                "SEC EDGAR submissions：公开时刻优先 acceptanceDateTime（分钟精度），"
                "缺失保守取 filingDate 日末，A 级 PIT；include_history 可遍历历史分段"
            ),
        )

    def healthcheck(self) -> dict:
        """探活：submissions 端点取 AAPL（短超时，不抛异常）。"""
        try:
            import httpx  # lazy

            _throttled_get(
                httpx, _SUBMISSIONS_URL.format(cik="0000320193"),
                {"User-Agent": self._ua}, 8,
            )
            return {"ok": True, "detail": "submissions 端点可达"}
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
        import httpx  # lazy：核心与测试不依赖网络库

        cik = request.get("cik") or self._resolve_cik(request["ticker"], httpx)
        cik = str(cik).zfill(10)
        forms = set(request.get("forms") or [])
        headers = {"User-Agent": self._ua}
        resp = _throttled_get(
            httpx, _SUBMISSIONS_URL.format(cik=cik), headers, 30,
        )
        data = resp.json()
        tables: list[dict] = [data["filings"]["recent"]]

        # 历史分段（方案 §5.2）：默认 0 段（旧行为不变）；include_history 拉前 N 段
        self.last_history_errors = []
        max_history = int(request.get("max_history_files") or 0)
        if request.get("include_history") and not max_history:
            max_history = 4  # 默认补 4 段（约再向前 3–8 年，取决于公司申报密度）
        for meta in (data["filings"].get("files") or [])[:max_history]:
            name = str(meta.get("name") or "")
            if not name:
                continue
            # as_of 早于该段起点 → 该段全部越界，不必拉（源头省流量）
            filing_from = str(meta.get("filingFrom") or "")
            if as_of is not None and filing_from and \
                    as_of.date().isoformat() < filing_from:
                continue
            try:
                seg = _throttled_get(httpx, _SEGMENT_URL.format(name=name), headers, 30)
                tables.append(seg.json())
            except Exception as e:  # noqa: BLE001 - 单段失败降级可见，不拖死 recent
                logger.warning("EDGAR 历史分段 %s 拉取失败：%s", name, e)
                self.last_history_errors.append(
                    {"segment": name, "error": f"{type(e).__name__}: {e}"}
                )

        records: list[DataRecord] = []
        for table in tables:
            forms_col = table.get("form") or []
            acceptances = table.get("acceptanceDateTime") or []
            for i, form in enumerate(forms_col):
                filed = (table.get("filingDate") or [])[i]
                period = (table.get("reportDate") or [])[i]
                doc = (table.get("primaryDocument") or [])[i]
                accession = (table.get("accessionNumber") or [])[i]
                accepted = acceptances[i] if i < len(acceptances) else None
                available_at = acceptance_or_eod(filed, accepted)
                if as_of is not None and available_at > as_of:
                    continue  # 源头过滤（网关层还会复核一次）
                if forms and form not in forms:
                    continue
                acc = accession.replace("-", "")
                records.append(
                    DataRecord(
                        source_id="edgar",
                        payload={
                            "form": form, "accession": accession,
                            "filingDate": filed,
                            "acceptanceDateTime": accepted,
                        },
                        available_at=available_at,
                        event_time=datetime.fromisoformat(period).replace(tzinfo=UTC)
                        if period else None,
                        url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}",
                    )
                )
        return records

    def _resolve_cik(self, ticker: str, httpx) -> str:
        """ticker → CIK（SEC 官方映射表，进程内缓存）。"""
        if self._ticker_map is None:
            resp = _throttled_get(
                httpx, "https://www.sec.gov/files/company_tickers.json",
                {"User-Agent": self._ua}, 30,
            )
            self._ticker_map = {
                row["ticker"].upper(): str(row["cik_str"]) for row in resp.json().values()
            }
        cik = self._ticker_map.get(ticker.upper())
        if cik is None:
            raise KeyError(f"EDGAR 未找到 ticker: {ticker}")
        return cik
