"""SearchBroker（tools-plugins 方案 §5.1，P1-A 输入能力）：双源搜索代理。

初版策略（方案「SearchBroker 初版复用 Exa 为主、Tavily 为备」）：
- 默认只打主源；主源未注册/报错 → 回退备源（失败回退必须可见，进 trace）；
- 主源低召回（< min_recall 条）或 mode="dual" → 加打备源合并；
- 去重：canonical URL 相同 = 同一来源（保主源版本——PIT 等级更高）；
  正文哈希相同但 URL 不同 = 转载族（同一原始来源只算一个，标 family_size，
  两个引擎返回同一公告不得记成两个独立来源）；
- 排序确定性：主源命中保持原序，备源新增按备源原序附加；
- 参数/失败回退/去重统计全部写 trace（每次调用落 gateway/search_broker 事件
  并随结果返回 trace 字段）。

PIT 纪律不变：每条记录保留各自 source_id/available_at/pit_grade——时间准入
仍在 DataGateway（broker 经 gateway.query 逐源调用，不绕过时间闸）。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from ..eventstore.events import Event
from ..eventstore.store import EventStore
from .models import DataRecord

logger = logging.getLogger("finance_agent.gateway.search_broker")

#: broker 每次调用的 trace 事件（参数/回退/去重统计可归因）
SEARCH_BROKER_TRACE = "gateway/search_broker"

#: canonical URL 归一时丢弃的跟踪参数（转载/分享场景的身份噪声）
_TRACKING_PARAMS = re.compile(
    r"^(utm_|from$|ref$|ref_src$|spm|gclid|fbclid|igshid|_hsenc|_hsmi|mc_cid|mc_eid)",
    re.IGNORECASE,
)


def canonical_url(url: str | None) -> str:
    """URL → 规范身份（scheme/host 小写、去 www.、去尾斜杠、去跟踪参数、去 fragment）。

    解析失败的原文返回小写原文（不静默丢记录——身份不明时宁可不去重）。
    """
    if not url:
        return ""
    text = url.strip()
    if not text:
        return ""
    if "://" not in text:
        text = "//" + text
    try:
        parts = urlparse(text)
    except ValueError:
        return text.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return text.lower()
    scheme = (parts.scheme or "https").lower()
    path = parts.path.rstrip("/") or "/"
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not _TRACKING_PARAMS.match(k)]
    )
    return urlunparse((scheme, host, path, "", query, ""))


def _content_family(record: DataRecord) -> str:
    """正文族哈希（转载族识别）：标题+正文归一后的指纹。"""
    text = " ".join(str(record.payload.get(k) or "") for k in ("title", "text"))
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16] if norm else ""


@dataclass
class BrokerResult:
    """一次代理搜索的结果：合并后的记录 + 可归因 trace。"""

    records: list[DataRecord]
    trace: dict[str, Any] = field(default_factory=dict)


class SearchBroker:
    """Exa 主 / Tavily 备的双源代理（供应商标识由构造注入，可替换）。"""

    def __init__(
        self, gateway: Any, *, primary: str = "web_search",
        backup: str = "web_search_tavily", min_recall: int = 3,
        events: EventStore | None = None, run_id: str = "",
        budget: Any | None = None,
    ):
        self._gateway = gateway
        self._primary = primary
        self._backup = backup
        self._min_recall = max(1, min_recall)
        self._events = events
        self._run_id = run_id
        #: RunBudget（可选）：每个实际调用的引擎真实扣减一次检索预算——
        #: 双源合并是双份成本，不得在预算账外（方案 §8.3 统一扣费）
        self._budget = budget

    @property
    def gateway(self) -> Any:
        return self._gateway

    @property
    def available_engines(self) -> list[str]:
        """网关注册了哪些搜索源（缺凭证的源 fail-closed 不在网关注册表中）。"""
        registered = set(self._gateway.source_ids())
        return [e for e in (self._primary, self._backup) if e in registered]

    def available(self) -> bool:
        return bool(self.available_engines)

    def search(
        self, request: dict[str, Any], *, mode: str = "auto",
    ) -> BrokerResult:
        """代理搜索：策略决策 → 逐源查询 → 去重合并 → trace。

        mode: auto（默认：主源优先，低召回/失败才动备源）/ primary（只打主源）/
        dual（强制双源合并）。
        """
        engines = self.available_engines
        trace: dict[str, Any] = {
            "query": str(request.get("query") or "")[:200],
            "mode": mode, "engines_available": engines,
            "engines_called": [], "per_engine": {}, "fallback_reason": "",
        }
        if not engines:
            trace["fallback_reason"] = "无可用搜索源（未注册/缺凭证）"
            self._emit(trace)
            return BrokerResult(records=[], trace=trace)

        records: list[DataRecord] = []
        primary_failed = False

        def _call(engine: str) -> list[DataRecord]:
            if self._budget is not None:
                ok, reason = self._budget.admit_retrieval()
                if not ok:
                    trace["per_engine"][engine] = {
                        "skipped": "budget_denied", "reason": reason, "count": 0}
                    return []
            trace["engines_called"].append(engine)
            try:
                out = self._gateway.query(engine, request)
            except Exception as e:  # noqa: BLE001 - 单源失败降级可见，不拖死整次搜索
                trace["per_engine"][engine] = {
                    "error": f"{type(e).__name__}: {e}", "count": 0}
                logger.warning("SearchBroker 源 %s 失败：%s", engine, e)
                return []
            trace["per_engine"][engine] = {"count": len(out)}
            return out

        if mode == "dual":
            for engine in engines:
                records.extend(_call(engine))
            trace["fallback_reason"] = "mode=dual 强制双源"
        else:
            primary = engines[0]
            records = _call(primary)
            if len(records) == 0 and len(engines) > 1:
                # 主源失败或零召回 → 备源（失败/低召回回退都可见）
                primary_failed = True
                trace["fallback_reason"] = (
                    "主源报错回退" if "error" in trace["per_engine"].get(primary, {})
                    else "主源零召回回退")
                records = _call(engines[1])
            elif len(records) < self._min_recall and len(engines) > 1 and mode == "auto":
                trace["fallback_reason"] = (
                    f"主源低召回（{len(records)} < {self._min_recall}）合并备源")
                records = [*records, *_call(engines[1])]

        merged, stats = self._dedupe(records)
        trace.update(stats)
        trace["merged_count"] = len(merged)
        trace["primary_failed"] = primary_failed
        self._emit(trace)
        return BrokerResult(records=merged, trace=trace)

    def _dedupe(self, records: list[DataRecord]) -> tuple[list[DataRecord], dict[str, int]]:
        """canonical URL 去重 + 转载族归并（主源版本优先，确定性排序保持）。"""
        seen_url: dict[str, int] = {}
        seen_family: dict[str, int] = {}
        out: list[DataRecord] = []
        dup_url = 0
        dup_family = 0
        for rec in records:
            canon = canonical_url(rec.url)
            family = _content_family(rec)
            if canon and canon in seen_url:
                dup_url += 1
                idx = seen_url[canon]
                out[idx].payload.setdefault("_broker", {})["family_size"] = (
                    out[idx].payload.get("_broker", {}).get("family_size", 1) + 1)
                continue
            if family and family in seen_family:
                # 同文不同源 URL：转载族——同一原始来源只算一个（方案 §5.1）
                dup_family += 1
                idx = seen_family[family]
                meta = out[idx].payload.setdefault("_broker", {})
                meta["family_size"] = meta.get("family_size", 1) + 1
                meta.setdefault("family_urls", []).append(rec.url)
                continue
            meta = {
                "origin": ("primary" if rec.source_id == self._primary else "backup"),
                "canonical_url": canon, "family_size": 1,
            }
            rec.payload["_broker"] = meta
            if canon:
                seen_url[canon] = len(out)
            if family:
                seen_family[family] = len(out)
            out.append(rec)
        return out, {"url_duplicates_removed": dup_url, "family_merges": dup_family}

    def _emit(self, trace: dict[str, Any]) -> None:
        if self._events is None:
            return
        self._events.append(Event(
            run_id=self._run_id or "search-broker",
            type=SEARCH_BROKER_TRACE,
            payload=dict(trace),
        ))


__all__ = ["SearchBroker", "BrokerResult", "canonical_url", "SEARCH_BROKER_TRACE"]
