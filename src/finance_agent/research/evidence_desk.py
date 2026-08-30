"""EvidenceDesk：证据完整性防线的服务端执行点（堵「模型自编自引」漏洞）。

背景（2026-08-30 验收事故）：旧 register_evidence 信任模型自报的
source_id / available_at / verbatim_quote——模型凭参数记忆编一个数字、
再配一句它从未读过的「原文」，numeric-guard（值⊆摘录）对自编自引无效。

新纪律：**证据只能登记自本 run 实际进入上下文的检索内容**（chunk）：
- chunk 来源只有两类：① 网关工具返回的 DataRecord；② read_* 工具抓取的文档段落；
- register_evidence(chunk_id, verbatim_quote)：quote 必须是该 chunk 文本的逐珠子串
  （空白归一化后比对），source/url/available_at/pit_grade 全部由 chunk 元数据推导——
  模型自报的时间与来源一律不信；
- 校验失败 = 拒绝登记（fail-closed），记 hook/verdict 供审计。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from ..knowledge.models import Evidence, PitGrade

_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS.sub(" ", text).strip()


@dataclass(frozen=True)
class RetrievedChunk:
    """一段实际进入过模型上下文的检索内容（服务端持有，模型只拿到 chunk_id）。"""

    chunk_id: str
    source_id: str
    text: str
    url: str | None
    available_at: datetime | None
    pit_grade: PitGrade


class ChunkStore:
    """run 级检索内容台账：工具把内容放进来，register_evidence 从这里验证。

    chunk_id 是 run 内单调递增序号（chk-0001…）——确定性利于测试与回放审计。
    """

    def __init__(self) -> None:
        self._chunks: dict[str, RetrievedChunk] = {}
        self._n = 0

    def add(
        self,
        *,
        source_id: str,
        text: str,
        url: str | None,
        available_at: datetime | None,
        pit_grade: PitGrade,
    ) -> str:
        self._n += 1
        chunk_id = f"chk-{self._n:04d}"
        self._chunks[chunk_id] = RetrievedChunk(
            chunk_id=chunk_id,
            source_id=source_id,
            text=text,
            url=url,
            available_at=available_at,
            pit_grade=pit_grade,
        )
        return chunk_id

    def get(self, chunk_id: str) -> RetrievedChunk | None:
        return self._chunks.get(chunk_id)


class EvidenceVerificationError(Exception):
    """证据校验失败：chunk 不存在或摘录不是逐珠子串（fail-closed）。"""


def verify_and_build(
    store: ChunkStore,
    *,
    chunk_id: str,
    verbatim_quote: str,
    evidence_id: str | None = None,
) -> Evidence:
    """register_evidence 的唯一入口。quote 必须是 chunk 文本的逐珠子串。"""
    chunk = store.get(chunk_id)
    if chunk is None:
        raise EvidenceVerificationError(f"未知 chunk_id: {chunk_id}（证据必须来自本 run 检索内容）")
    quote_n = _normalize(verbatim_quote)
    if not quote_n or quote_n not in _normalize(chunk.text):
        raise EvidenceVerificationError(
            "摘录不是检索内容的逐珠子串（禁止凭记忆编写证据）。"
            f"chunk={chunk_id} 内容片段：{_normalize(chunk.text)[:200]}…"
        )
    return Evidence(
        evidence_id=evidence_id or f"ev-{uuid.uuid4().hex[:12]}",
        source_id=chunk.source_id,
        url=chunk.url,
        verbatim_quote=verbatim_quote.strip(),
        retrieved_at=datetime.now(UTC),
        available_at=chunk.available_at,  # 由记录推导，不信模型自报
        pit_grade=chunk.pit_grade,
    )
