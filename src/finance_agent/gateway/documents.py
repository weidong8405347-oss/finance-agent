"""run 级文档库（Document Read v2，tools-plugins 方案 §5.1 实现顺序 2/4）。

把抓取从工具闭包抽成文档服务：
- 原件与规范正文按**内容哈希**保存，同 run 内同版本复用（同一财报只抓取解析一次，
  各 worker 共享只读文档引用——方案 §8.3）；
- 保页码：PDF 逐页存储，首次解析上限之外的页可**惰性续解**（重要表在后半部必须可达）；
- 完整性显式：total/parsed/failed 与 full/partial/truncated/failed 状态随文档走，
  读取工具据此告知模型「还有未读内容」，截断不静默；
- 时间纪律：文档从检索记录（chunk）继承 available_at/pit_grade；直接 URL 抓取
  无时间保证（C 级诚实降级）。文档库不做时间准入——那是 DataGateway 的职责。

文档库是 run 级内存台账（与 ChunkStore 同级），不是新的事实真相源：
证据仍经 register_evidence 落 Evidence（带 document/page locator），
正式数值仍走 typed observation（方案 §4.1「避免平行建库」）。
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..knowledge.models import PitGrade
from .fetch import DEFAULT_PARSE_CAP, FetchedDocument, pdf_pages


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class StoredDocument:
    """一份文档的一个版本（内容哈希标识；run 内只读共享）。"""

    document_id: str
    url: str | None
    source_id: str
    kind: str  # pdf / html / text
    content_hash: str
    #: 页码 → 页文本（1-based；已解析的页）
    page_texts: dict[int, str] = field(default_factory=dict)
    failed_pages: list[int] = field(default_factory=list)
    total_pages: int = 1
    parse_cap: int = DEFAULT_PARSE_CAP
    quality: str = "ok"
    #: 目录线索 [{"page": 1, "title": ...}]（HTML 标题；PDF 可为空）
    toc: list[dict[str, Any]] = field(default_factory=list)
    available_at: datetime | None = None
    pit_grade: PitGrade = PitGrade.C
    #: 继承自检索记录的定位（如 {"source_chunk": "chk-0003"}）
    locator: dict[str, str] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    parser: str = ""
    #: PDF 原始 bytes（惰性续解用）；HTML/text 不保存
    raw: bytes | None = None
    #: 复用计数（重复抓取被短路时 +1，重复资料率可见）
    reuses: int = 0

    @property
    def parsed_pages(self) -> list[int]:
        return sorted(self.page_texts)

    @property
    def max_parsed_page(self) -> int:
        return max(self.page_texts) if self.page_texts else 0

    @property
    def completeness(self) -> str:
        """full / partial / truncated / failed（方案 §4.1 读取完整性）。"""
        if not self.page_texts or not any(t.strip() for t in self.page_texts.values()):
            return "failed"
        if self.failed_pages:
            return "partial"
        if self.total_pages > self.max_parsed_page:
            return "truncated"
        return "full"

    def completeness_payload(self) -> dict[str, Any]:
        return {
            "status": self.completeness,
            "total_pages": self.total_pages,
            "parsed_pages": len(self.page_texts),
            "failed_pages": list(self.failed_pages),
            "unparsed_pages": max(0, self.total_pages - self.max_parsed_page),
        }

    def merged_text(self, pages: list[int] | None = None) -> str:
        wanted = pages if pages is not None else self.parsed_pages
        return "\n".join(self.page_texts.get(p, "") for p in wanted)


class DocumentStore:
    """run 级文档台账：内容哈希去重 + URL 短路复用 + PDF 惰性续解。"""

    def __init__(self) -> None:
        self._docs: dict[str, StoredDocument] = {}
        self._by_hash: dict[str, str] = {}
        #: (url, source_id, available_at, pit) → document_id：同来源同 URL 直接复用
        self._by_origin: dict[tuple, str] = {}
        self._n = 0
        self._duplicates = 0
        self._lock = threading.Lock()  # 维度并行 worker 共享（同 ChunkStore）

    def __len__(self) -> int:
        with self._lock:
            return len(self._docs)

    @property
    def duplicates(self) -> int:
        """被短路复用的抓取次数（重复资料率分子之一）。"""
        with self._lock:
            return self._duplicates

    def get(self, document_id: str) -> StoredDocument | None:
        with self._lock:
            return self._docs.get(document_id)

    def find_by_origin(
        self, url: str | None, source_id: str,
        available_at: datetime | None, pit_grade: PitGrade,
    ) -> StoredDocument | None:
        key = (url, source_id,
               available_at.isoformat() if available_at else None, pit_grade.value)
        with self._lock:
            doc_id = self._by_origin.get(key)
            if doc_id is None:
                return None
            doc = self._docs.get(doc_id)
        if doc is not None:
            with self._lock:
                doc.reuses += 1
                self._duplicates += 1
        return doc

    def add(
        self, *, url: str | None, source_id: str, fetched: FetchedDocument,
        available_at: datetime | None, pit_grade: PitGrade,
        locator: dict[str, str] | None = None, parser: str = "",
    ) -> StoredDocument:
        """登记一个抓取结果；同内容哈希 + 同来源上下文 → 复用已有版本。"""
        page_texts = dict(fetched.page_texts)
        content_hash = _hash_text(fetched.merged_text)
        origin_key = (url, source_id,
                      available_at.isoformat() if available_at else None, pit_grade.value)
        with self._lock:
            existing_id = self._by_hash.get(content_hash)
            if existing_id is not None:
                existing = self._docs[existing_id]
                if origin_key in self._by_origin:
                    existing.reuses += 1
                    self._duplicates += 1
                    return existing
                # 同内容不同来源上下文（如 A 级 filing 记录 vs C 级网页转载同一 URL）：
                # 复用解析结果但单独登记身份——PIT 元数据不得互相污染
            else:
                existing = None
            self._n += 1
            doc = StoredDocument(
                document_id=f"doc-{self._n:04d}",
                url=url, source_id=source_id, kind=fetched.kind,
                content_hash=content_hash,
                page_texts=dict(existing.page_texts) if existing else page_texts,
                failed_pages=list(existing.failed_pages if existing else fetched.failed_pages),
                total_pages=fetched.total_pages if not existing else existing.total_pages,
                parse_cap=fetched.parse_cap,
                quality=fetched.quality.quality,
                toc=[{"page": p, "title": t} for p, t in fetched.headings],
                available_at=available_at, pit_grade=pit_grade,
                locator=dict(locator or {}),
                parser=parser or ("pypdf" if fetched.kind == "pdf" else "html_strip"),
                raw=fetched.raw if not existing else existing.raw,
            )
            self._docs[doc.document_id] = doc
            self._by_hash.setdefault(content_hash, doc.document_id)
            self._by_origin[origin_key] = doc.document_id
            return doc

    def add_text(
        self, *, url: str | None, source_id: str, text: str, quality: str = "ok",
        available_at: datetime | None, pit_grade: PitGrade,
        locator: dict[str, str] | None = None, parser: str = "legacy_fetch",
    ) -> StoredDocument:
        """旧式纯文本抓取结果（fetch_document 返回 str）的登记入口：单页文档。"""
        from .text_quality import TextQuality

        fetched = FetchedDocument(
            kind="text", url=url or "", page_texts=((1, text),) if text.strip() else (),
            total_pages=1, quality=TextQuality(quality),
        )
        return self.add(url=url, source_id=source_id, fetched=fetched,
                        available_at=available_at, pit_grade=pit_grade,
                        locator=locator, parser=parser)

    def ensure_pages(self, doc: StoredDocument, pages: list[int]) -> list[int]:
        """惰性续解（PDF）：请求的页未解析且原始 bytes 在手 → 现场补解。

        返回本次成功补解的页码；失败页计入 doc.failed_pages（可见，不静默）。
        """
        missing = [p for p in pages
                   if p not in doc.page_texts and p not in doc.failed_pages
                   and 1 <= p <= doc.total_pages]
        if not missing or doc.raw is None:
            return []
        parsed, failed, total = pdf_pages(
            doc.raw, start=min(missing), max_pages=max(missing) - min(missing) + 1,
        )
        with self._lock:
            wanted = set(missing)
            got = []
            for page_no, text in parsed:
                if page_no in wanted:
                    doc.page_texts[page_no] = text
                    got.append(page_no)
            for page_no in failed:
                if page_no in wanted and page_no not in doc.failed_pages:
                    doc.failed_pages.append(page_no)
            doc.failed_pages.sort()
            doc.total_pages = max(doc.total_pages, total)
        return sorted(got)


__all__ = ["DocumentStore", "StoredDocument"]
