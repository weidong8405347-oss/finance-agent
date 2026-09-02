"""Tavily web 搜索 adapter（C 级 PIT，research-capability-upgrade §4.5 备选搜索源）。

定位：Exa 的补充/备份——免费额度更慷慨（1000 credits/月），关键词检索强；
混合双搜索源对标的池挖掘召回有实打实的好处（两源取并集，偏差互补）。
PIT 语义：结果无可靠逐条发布时间 → C 级（生产可用，评估模式 fail-closed 禁用；
records 的 available_at=None，knowledge_time 由证据纪律兜底为检索时刻）。

fail-closed：未配置 TAVILY_API_KEY → 装配层不注册本 adapter。
"""

from __future__ import annotations

import os
from datetime import datetime  # noqa: F401 - query 签名的 as_of 形参用
from typing import Any

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability

_API = "https://api.tavily.com/search"


class TavilySearchAdapter:
    def __init__(self, *, api_key: str | None = None, env: dict[str, str] | None = None):
        # 显式参数 > 注入 env > 进程环境；缺 key fail-closed（装配层据此不注册）
        environ = env if env is not None else os.environ
        self._key = api_key or environ.get("TAVILY_API_KEY") or None

    @property
    def configured(self) -> bool:
        return self._key is not None

    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="web_search_tavily",
            pit_grade=PitGrade.C,
            server_side_asof=False,
            description="Tavily 关键词搜索（无逐条发布时间保证 → C 级；Exa 的备份/并集源）",
        )

    def healthcheck(self) -> dict:
        if not self.configured:
            return {"ok": False, "detail": "未配置 TAVILY_API_KEY"}
        try:
            recs = self.query({"query": "test", "max_results": 1})
            return {"ok": True, "detail": f"Tavily 可取（{len(recs)} 条）"}
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:  # noqa: ARG002 - C 级源无 as_of 语义
        import httpx  # lazy

        if not self._key:
            raise RuntimeError("未配置 TAVILY_API_KEY（装配层应已拦截注册）")
        body: dict[str, Any] = {
            "api_key": self._key,
            "query": request["query"],
            "max_results": min(int(request.get("max_results", 8)), 20),
            "search_depth": "basic",
            "include_answer": False,
        }
        resp = httpx.post(_API, json=body, timeout=30)
        resp.raise_for_status()
        results = resp.json().get("results") or []

        records: list[DataRecord] = []
        for r in results:
            if not r.get("url"):
                continue
            records.append(
                DataRecord(
                    source_id="web_search_tavily",
                    payload={
                        "title": r.get("title"),
                        "text": (r.get("content") or "")[:1200],  # chunk 摘录上限
                        "score": r.get("score"),
                    },
                    available_at=None,  # C 级：无逐条发布时间保证
                    url=r["url"],
                )
            )
        return records
