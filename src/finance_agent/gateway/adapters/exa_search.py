"""Exa web 搜索 adapter（B 级 PIT 源，research-capability-upgrade §4.5 主用搜索源）。

调用通道（2026-09-08 起默认走 Novita 网关）：
- `NOVITA_API_KEY` → POST https://api.novita.ai/v3/exa/search，`Authorization: Bearer <key>`；
  Novita 官方文档明示该路由是 **Exa Search API 的 passthrough**（请求体/响应体同构：
  `numResults` / `contents.{text,highlights,summary}` / `startPublishedDate` /
  `endPublishedDate` 照传，响应 `results[]` 带 `title/url/publishedDate/author/text`），
  故本 adapter 的 PIT 语义与解析逻辑一条不改。
- 仅有 `EXA_API_KEY` → 回退直连 https://api.exa.ai/search（`x-api-key`），兼容旧配置。

PIT 语义：publishedDate（网页自述发布时刻）→ available_at；逐条无 publishedDate
→ available_at=None（诚实降级，评估模式网关自动丢弃该条）。
API 支持 startPublishedDate/endPublishedDate 服务端过滤 → server_side_asof=True。

fail-closed：两个 key 都没配 → 装配层不注册本 adapter（能力不存在）；
运行期 key 失效 → query 抛错（fail-loud，进 step 错误三通道）。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability

#: Novita 网关的 Exa passthrough（Bearer 认证）
_API_NOVITA = "https://api.novita.ai/v3/exa/search"
#: Exa 官方端点（x-api-key 认证）——仅在无 NOVITA_API_KEY 时回退使用
_API_EXA = "https://api.exa.ai/search"


class ExaSearchAdapter:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        novita_api_key: str | None = None,
        env: dict[str, str] | None = None,
    ):
        # 显式参数 > 注入 env > 进程环境；缺 key fail-closed（装配层据此不注册）
        environ = env if env is not None else os.environ
        self._novita_key = novita_api_key or environ.get("NOVITA_API_KEY") or None
        self._exa_key = api_key or environ.get("EXA_API_KEY") or None

    @property
    def configured(self) -> bool:
        return self._novita_key is not None or self._exa_key is not None

    @property
    def channel(self) -> str | None:
        """实际生效的调用通道：novita（优先）/ exa（回退）/ None（未配置）。"""
        if self._novita_key is not None:
            return "novita"
        if self._exa_key is not None:
            return "exa"
        return None

    def _target(self) -> tuple[str, dict[str, str]]:
        """通道 → (endpoint, 认证头)。Novita 用 Bearer，Exa 官方用 x-api-key。"""
        if self._novita_key is not None:
            return _API_NOVITA, {"Authorization": f"Bearer {self._novita_key}"}
        if self._exa_key is not None:
            return _API_EXA, {"x-api-key": self._exa_key}
        raise RuntimeError("未配置 NOVITA_API_KEY / EXA_API_KEY（装配层应已拦截注册）")

    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="web_search",
            pit_grade=PitGrade.B,
            server_side_asof=True,  # endPublishedDate 服务端过滤 + 网关层复核
            description=(
                "Exa 语义搜索（网页自述 publishedDate → B 级；无日期条目降级无 PIT）"
                f"，通道：{self.channel or '未配置'}"
            ),
        )

    def healthcheck(self) -> dict:
        channel = self.channel
        if channel is None:
            return {"ok": False, "detail": "未配置 NOVITA_API_KEY / EXA_API_KEY"}
        try:
            recs = self.query({"query": "test", "num_results": 1})
            return {"ok": True, "detail": f"Exa 可取（{channel} 通道，{len(recs)} 条）"}
        except Exception as e:
            return {"ok": False, "detail": f"{channel} 通道 {type(e).__name__}: {e}"}

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
        import httpx  # lazy

        url, auth_headers = self._target()
        body: dict[str, Any] = {
            "query": request["query"],
            "numResults": min(int(request.get("num_results", 8)), 25),
            "contents": {"text": {"maxCharacters": 1200}},  # 摘录进 chunk，供证据引用
        }
        if as_of is not None:
            body["endPublishedDate"] = as_of.isoformat()  # 服务端 PIT 过滤
        resp = httpx.post(
            url,
            headers={**auth_headers, "Content-Type": "application/json"},
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
