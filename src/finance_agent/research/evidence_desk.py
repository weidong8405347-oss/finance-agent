"""EvidenceDesk：证据完整性防线的服务端执行点（堵「模型自编自引」漏洞）。

背景（2026-08-30 验收事故）：旧 register_evidence 信任模型自报的
source_id / available_at / verbatim_quote——模型凭参数记忆编一个数字、
再配一句它从未读过的「原文」，numeric-guard（值⊆摘录）对自编自引无效。

纪律：**证据只能登记自本 run 实际进入上下文的检索内容**（chunk）：
- chunk 来源只有两类：① 网关工具返回的 DataRecord；② read_* 工具抓取的文档段落；
- register_evidence(chunk_id, verbatim_quote) 或 register_evidence(span_id)：
  quote 必须是该 chunk 规范正文的逐珠子串（或直接引用服务端切好的 span，
  由服务端填 quote——转写误差为零），source/url/available_at/pit_grade 全部由
  chunk 元数据推导；
- 校验失败 = 拒绝登记（fail-closed），并按拒绝码给出可修复原因。

audit §3.5 整改（live-a2cce641：44 次拒绝中至少 9 次是「解码正文的逐字子串」被误拒）：
- 台账保存**规范正文**（原文换行/引号原样）+ locator + 抓取质量标记，JSON 只负责传输；
- 比对前做「无害归一」：JSON 转义还原、排版引号/破折号/中文标点折叠、空白折叠——
  只消除转写与编码差异，不改写语义（改写、换数字仍然拒）；
- chunk 附带稳定 span id，工具把 span 一并返回，登记优先引用 span。
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..knowledge.models import Evidence, PitGrade

_WS = re.compile(r"\s+")

#: 排版/全角变体 → 归一形式（只折叠「同形不同码」，不折叠语义）
_PUNCT_FOLD = {
    "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
    "\u300c": '"', "\u300d": '"', "\u300e": '"', "\u300f": '"',
    "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u00a0": " ", "\u3000": " ", "\u2028": "\n", "\u2029": "\n",
    "\uff0c": ",", "\uff1a": ":", "\uff1b": ";", "\uff08": "(", "\uff09": ")",
    "\uff05": "%", "\uff04": "$", "\u00b7": ".",
}


def _normalize(text: str) -> str:
    """比对用归一：JSON 转义还原 → 排版变体折叠 → 空白折叠。

    只消除「转写/编码差异」：换行被序列化成 \\n、直引号被排成弯引号、
    全角标点被输入法换成半角——这些都不该让真实摘录被拒。
    """
    s = _unescape(text)
    s = "".join(_PUNCT_FOLD.get(ch, ch) for ch in s)
    return _WS.sub(" ", s).strip()


def _unescape(text: str) -> str:
    """还原 JSON 序列化留下的转义（\\n / \\t / \\" / \\uXXXX）。

    工具响应经 json.dumps 传输，模型从响应里复制的正文常带转义形态；
    台账存的是规范正文，两边必须先回到同一形态再比对。
    """
    if "\\" not in text:
        return text
    try:
        decoded = json.loads(f'"{text.replace(chr(10), " ")}"')
    except Exception:  # noqa: BLE001 - 不是合法转义序列就按原文处理
        return text
    return decoded if isinstance(decoded, str) else text


#: 单个 span 的目标长度（字符）：够放一句完整披露，又不至于让摘录失去定位力
_SPAN_CHARS = 480
#: 句读切分点（中英）：span 尽量落在句边界，不切碎语义
_SENTENCE_END = re.compile(r"(?<=[。.!?;；\n])\s*")


def split_spans(text: str, *, max_chars: int = _SPAN_CHARS) -> list[str]:
    """规范正文 → 稳定 span 列表（确定性切分；同文本必得同 span 序列）。"""
    if not text.strip():
        return []
    pieces: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        for sent in _SENTENCE_END.split(para):
            sent = sent.strip()
            if not sent:
                continue
            while len(sent) > max_chars:  # 超长句硬切（保留可定位性）
                pieces.append(sent[:max_chars])
                sent = sent[max_chars:]
            pieces.append(sent)
    return pieces


@dataclass(frozen=True)
class RetrievedChunk:
    """一段实际进入过模型上下文的检索内容（服务端持有，模型只拿到 chunk_id）。"""

    chunk_id: str
    source_id: str
    text: str
    url: str | None
    available_at: datetime | None
    pit_grade: PitGrade
    #: 文档内定位（page/table/row/column/section…）：财务证据绑定用（audit §3.2）
    locator: dict[str, Any] = field(default_factory=dict)
    #: 抓取质量：ok / partial / garbled / needs_ocr（audit §3.5：质量单独标记，
    #: PIT 等级只表达「时间来源性质」，不表达文本可读性与数字可靠性）
    quality: str = "ok"
    #: 稳定 span（服务端切好，登记可直接引用，转写误差为零）
    spans: tuple[str, ...] = ()

    def span_text(self, span_id: str) -> str | None:
        """span_id（`chk-0001#s3` 或裸序号 `s3`/`3`）→ 原文片段。"""
        idx = _span_index(span_id)
        if idx is None or idx >= len(self.spans):
            return None
        return self.spans[idx]


def _span_index(span_id: str) -> int | None:
    tail = span_id.rsplit("#", 1)[-1].strip()
    if tail.startswith("s"):
        tail = tail[1:]
    try:
        return int(tail)
    except ValueError:
        return None


class ChunkStore:
    """run 级检索内容台账：工具把内容放进来，register_evidence 从这里验证。

    chunk_id 是 run 内单调递增序号（chk-0001…）——确定性利于测试与回放审计。
    去重（audit §3.3）：同 source_id + 同规范正文只存一份，重复命中计数——
    重复资料既是成本也是「上下文持续放大」的主因，必须可见。
    """

    def __init__(self) -> None:
        self._chunks: dict[str, RetrievedChunk] = {}
        self._by_text: dict[tuple[str, str], str] = {}
        self._n = 0
        self._duplicates = 0
        self._lock = threading.Lock()  # 维度并行 researcher 共享台账（P3）

    @property
    def duplicates(self) -> int:
        with self._lock:
            return self._duplicates

    def __len__(self) -> int:
        with self._lock:
            return len(self._chunks)

    def add(
        self,
        *,
        source_id: str,
        text: str,
        url: str | None,
        available_at: datetime | None,
        pit_grade: PitGrade,
        locator: dict[str, Any] | None = None,
        quality: str = "ok",
    ) -> str:
        with self._lock:
            key = (source_id, _normalize(text))
            existing = self._by_text.get(key)
            if existing is not None:
                self._duplicates += 1
                return existing
            self._n += 1
            chunk_id = f"chk-{self._n:04d}"
            self._chunks[chunk_id] = RetrievedChunk(
                chunk_id=chunk_id,
                source_id=source_id,
                text=text,
                url=url,
                available_at=available_at,
                pit_grade=pit_grade,
                locator=dict(locator or {}),
                quality=quality,
                spans=tuple(split_spans(text)),
            )
            self._by_text[key] = chunk_id
        return chunk_id

    def get(self, chunk_id: str) -> RetrievedChunk | None:
        with self._lock:
            return self._chunks.get(chunk_id)

    def find_by_span(self, span_id: str) -> RetrievedChunk | None:
        """span_id → 所属 chunk（`chk-0001#s3` 直接取；裸 `s3` 不支持，需带 chunk 前缀）。"""
        if "#" not in span_id:
            return None
        return self.get(span_id.split("#", 1)[0])


class EvidenceVerificationError(Exception):
    """证据校验失败：chunk 不存在或摘录不是逐珠子串（fail-closed）。"""

    def __init__(self, message: str, *, code: str = "quote_mismatch", hint: str = ""):
        super().__init__(message)
        #: 拒绝码（audit §3.5：按码反馈可修复原因，防止反复重试或缩短摘录丢语义）
        self.code = code
        self.hint = hint


def verify_and_build(
    store: ChunkStore,
    *,
    chunk_id: str = "",
    verbatim_quote: str = "",
    evidence_id: str | None = None,
    span_id: str | None = None,
) -> Evidence:
    """register_evidence 的唯一入口。

    两种引用方式（优先 span）：
    - `span_id`：服务端切好的片段，quote 由服务端填（不存在转写误差）；
    - `chunk_id + verbatim_quote`：quote 必须是 chunk 规范正文的逐珠子串
      （无害归一后比对）。
    """
    if span_id:
        chunk = store.find_by_span(span_id) or store.get(span_id.split("#", 1)[0])
        if chunk is None:
            raise EvidenceVerificationError(
                f"未知 span_id: {span_id}", code="unknown_chunk",
                hint="span_id 形如 chk-0001#s3，必须来自本 run 的 query_*/read_* 返回",
            )
        text = chunk.span_text(span_id)
        if text is None:
            raise EvidenceVerificationError(
                f"span 不存在: {span_id}（该 chunk 共 {len(chunk.spans)} 段）",
                code="unknown_span",
                hint="用 read_chunk(chunk_id) 重新取可用 span 列表",
            )
        return _build(chunk, text, evidence_id)

    chunk = store.get(chunk_id)
    if chunk is None:
        raise EvidenceVerificationError(
            f"未知 chunk_id: {chunk_id}（证据必须来自本 run 检索内容）",
            code="unknown_chunk",
            hint="先 query_* / read_* 拿到带 chunk_id 的记录，再登记",
        )
    quote_n = _normalize(verbatim_quote)
    if not quote_n:
        raise EvidenceVerificationError(
            "摘录为空", code="empty_quote", hint="逐字复制原文片段，或改用 span_id 引用",
        )
    if quote_n not in _normalize(chunk.text):
        raise EvidenceVerificationError(
            "摘录不是检索内容的逐珠子串（禁止凭记忆编写证据）。"
            f"chunk={chunk_id} 内容片段：{_normalize(chunk.text)[:200]}…",
            code="quote_mismatch",
            hint=(
                "常见原因：① 改写/概括了原文（必须逐字）；② 跨段拼接（改用 span_id 或"
                "分条登记）；③ 数字与原文不一致。用 read_chunk(chunk_id) 取回可用 span。"
            ),
        )
    return _build(chunk, verbatim_quote.strip(), evidence_id)


def _build(chunk: RetrievedChunk, quote: str, evidence_id: str | None) -> Evidence:
    return Evidence(
        evidence_id=evidence_id or f"ev-{uuid.uuid4().hex[:12]}",
        source_id=chunk.source_id,
        url=chunk.url,
        verbatim_quote=quote,
        retrieved_at=datetime.now(UTC),
        available_at=chunk.available_at,  # 由记录推导，不信模型自报
        pit_grade=chunk.pit_grade,
        # 质量与定位随证据一起存（audit §3.2/§3.5）：下游指标门禁可直接判
        quality=chunk.quality if chunk.quality in (
            "ok", "partial", "garbled", "needs_ocr") else "ok",
        locator={str(k): str(v) for k, v in chunk.locator.items()},
    )


__all__ = [
    "ChunkStore", "RetrievedChunk", "EvidenceVerificationError", "verify_and_build",
    "split_spans",
]
