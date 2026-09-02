"""Exa web 搜索 adapter（B 级 PIT 源，research-capability-upgrade §4.5 主用搜索源）。

PIT 语义：publishedDate（网页自述发布时刻）→ available_at；逐条无 publishedDate
→ available_at=None（诚实降级，评估模式网关自动丢弃该条）。
API 支持 startPublishedDate/endPublishedDate 服务端过滤 → server_side_asof=True。

fail-closed：未配置 EXA_API_KEY → 装配层不注册本 adapter（能力不存在）；
运行期 key 失效 → query 抛错（fail-loud，进 step 错误三通道）。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability

_API = "https://api.exa.ai/search"


class ExaSearchAdapter:
    def __init__(self, *, api_key: str | None = None, env: dict[str, str] | None = None):
        # 显式参数 > 注入 env > 进程环境；缺 key fail-closed（装配层据此不注册）
        environ = env if env is not None else os.environ
        self._key = api_key or environ.get("EXA_API_KEY") or None

    @property
    def configured(self) -> bool:
        return self._key is not None

    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="web_search",
            pit_grade=PitGrade.B,
            server_side_asof=True,  # endPublishedDate 服务端过滤 + 网关层复核
            description="Exa 语义搜索（网页自述 publishedDate → B 级；无日期条目降级无 PIT）",
        )

    def healthcheck(self) -> dict:
        if not self.configured:
            return {"ok": False, "detail": "未配置 EXA_API_KEY"}
        try:
            recs = self.query({"query": "test", "num_results": 1})
            return {"ok": True, "detail": f"Exa 可取（{len(recs)} 条）"}
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
        import httpx  # lazy

        if not self._key:
            raise RuntimeError("未配置 EXA_API_KEY（装配层应已拦截注册）")
        body: dict[str, Any] = {
            "query": request["query"],
            "numResults": min(int(request.get("num_results", 8)), 25),
            "contents": {"text": {"maxCharacters": 1200}},  # 摘录进 chunk，供证据引用
        }
        if as_of is not None:
            body["endPublishedDate"] = as_of.isoformat()  # 服务端 PIT 过滤
        resp = httpx.post(
            _API,
            headers={"x-api-key": self._key, "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []

        records: list[DataRecord] = []
        for r in results:
            if not r.get("url"):
                continue
            published = _parse_iso(r.get("publishedDate"))
            records.append(
                DataRecord(
                    source_id="web_search",
                    payload={
                        "title": r.get("title"),
                        "author": r.get("author"),
                        "text": (r.get("text") or "")[:1200],  # chunk 摘录上限
                    },
                    available_at=published,  # None = 该条无 PIT 保证（诚实降级）
                    event_time=published,
                    url=r["url"],
                )
            )
        return records


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None
