"""统一文档正文抓取：HTML + PDF（P4，港股披露全是 PDF——research-capability-upgrade §5.1 D3）。

Document Read v2（tools-plugins 方案 §5.1 P1-A）：新增保页码的分页抽取——
旧路径把所有页合并为纯文本且默认前 80 页不可继续（重要表在后半部时不可达）；
新路径逐页保留、失败页显式记录、超出首次解析上限的部分可由文档库惰性地续解。

PIT 语义不变：披露文档自发布起不可变，抓取不产生新时间线；
available_at 一律由 filing 记录继承（read_edgar_filing 的既有纪律），
直接 URL 抓取无时间保证（诚实降级 C 级）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape

from .text_quality import TextQuality, assess_text_quality

#: 首次解析页数上限（不再是不可逾越的硬墙：文档库保存原始 bytes，
#: read_document 请求更后面的页时惰性续解；年报典型 100–400 页）
DEFAULT_PARSE_CAP = 400


@dataclass(frozen=True)
class FetchedDocument:
    """分页抓取结果（Document Read v2）：页码保留，失败/未解页显式可见。"""

    kind: str  # pdf / html / text
    url: str
    #: (页码, 页文本)，页码 1-based；HTML/纯文本 = 单页
    page_texts: tuple[tuple[int, str], ...] = ()
    #: 解析失败的页码（扫描件/损坏页；不拖死整份但必须可见）
    failed_pages: tuple[int, ...] = ()
    total_pages: int = 1
    #: 本次解析上限（total_pages 超出部分 = truncated，可惰性续解）
    parse_cap: int = DEFAULT_PARSE_CAP
    quality: TextQuality = field(default_factory=lambda: TextQuality("ok"))
    #: 目录线索：(页码, 标题)（HTML h1–h4；PDF 无可靠目录时为空）
    headings: tuple[tuple[int, str], ...] = ()
    #: PDF 原始 bytes（惰性续解用；HTML 不保存）
    raw: bytes | None = None
    content_type: str = ""

    @property
    def merged_text(self) -> str:
        return " ".join(t for _, t in self.page_texts)

    def as_legacy(self) -> tuple[str, TextQuality]:
        """旧契约兼容：合并纯文本 + 质量（fetch_document_checked 形态）。"""
        return re.sub(r"\s+", " ", self.merged_text).strip(), self.quality


def fetch_document(
    url: str, *, user_agent: str = "finance-agent research (contact: local@example.com)"
) -> str:
    """抓取 URL 并返回纯文本正文（HTML 剥离标签；PDF 走 pypdf 抽取）。"""
    text, _ = fetch_document_checked(url, user_agent=user_agent)
    return text


def fetch_document_checked(
    url: str, *, user_agent: str = "finance-agent research (contact: local@example.com)"
) -> tuple[str, TextQuality]:
    """同 fetch_document，额外返回抽取质量（audit §3.5）。

    质量必须单独标记：PIT 等级只说明时间来源性质，表达不了乱码/扫描件。
    乱码与 needs_ocr 的正文仍然返回（供诊断与人工核对），但下游指标入口会拒写。
    """
    import httpx  # lazy：核心与测试不依赖网络库

    resp = httpx.get(url, headers={"User-Agent": user_agent}, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "").lower()
    if "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf"):
        text, pages = pdf_to_text_paged(resp.content)
        return text, assess_text_quality(text, pages=pages)
    return html_to_text(resp.text), assess_text_quality(html_to_text(resp.text))


def html_to_text(html: str) -> str:
    """HTML → 纯文本（原 edgar.fetch_filing_text 的剥离逻辑，保持行为不变）。"""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def pdf_to_text(content: bytes, *, max_pages: int = 80) -> str:
    """PDF bytes → 纯文本（pypdf 抽取；页数上限防巨型年报打爆上下文）。"""
    text, _ = pdf_to_text_paged(content, max_pages=max_pages)
    return text


def pdf_to_text_paged(content: bytes, *, max_pages: int = 80) -> tuple[str, int]:
    """同 pdf_to_text，额外返回参与抽取的页数（needs_ocr 判定依据）。"""
    pages, _failed, _total = pdf_pages(content, max_pages=max_pages)
    text = re.sub(r"\s+", " ", " ".join(t for _, t in pages)).strip()
    return text, len(pages)


def pdf_pages(
    content: bytes, *, start: int = 1, max_pages: int = DEFAULT_PARSE_CAP,
) -> tuple[list[tuple[int, str]], list[int], int]:
    """PDF bytes → 逐页文本（保页码）：返回 ((页码,文本)列表, 失败页码, 总页数)。

    单页抽取失败（扫描件/损坏页）记入失败清单而不拖死整份；
    start/max_pages 支持惰性续解（文档库对超出首次上限的页按需再解）。
    """
    import io

    from pypdf import PdfReader  # lazy：--extra data

    reader = PdfReader(io.BytesIO(content))
    total = len(reader.pages)
    pages: list[tuple[int, str]] = []
    failed: list[int] = []
    first = max(1, start)
    last = min(total, first + max(0, max_pages) - 1)
    for page_no in range(first, last + 1):
        try:
            pages.append((page_no, reader.pages[page_no - 1].extract_text() or ""))
        except Exception:  # noqa: BLE001 - 失败页显式记录，不静默丢页
            failed.append(page_no)
    return pages, failed, total


_HEADING_RE = re.compile(r"(?is)<h([1-4])[^>]*>(.*?)</h\1>")


def extract_headings(html: str, *, limit: int = 80) -> list[tuple[int, str]]:
    """HTML 标题 → 目录线索（页码恒为 1；去标签/折叠空白，过长截断）。"""
    out: list[tuple[int, str]] = []
    for _level, inner in _HEADING_RE.findall(html):
        title = re.sub(r"\s+", " ", unescape(re.sub(r"(?s)<[^>]+>", " ", inner))).strip()
        if title:
            out.append((1, title[:120]))
        if len(out) >= limit:
            break
    return out


def fetch_document_paged(
    url: str, *,
    user_agent: str = "finance-agent research (contact: local@example.com)",
    max_pages: int = DEFAULT_PARSE_CAP,
) -> FetchedDocument:
    """抓取并保页码/目录/质量（Document Read v2 的统一入口）。

    PDF：逐页抽取（首次最多 max_pages 页，剩余可由文档库惰性续解）；
    HTML：单页 + h1–h4 目录线索；失败 fail-loud（抛异常，调用方区分
    「抓取失败」与「未披露」）。
    """
    import httpx  # lazy：核心与测试不依赖网络库

    resp = httpx.get(url, headers={"User-Agent": user_agent}, timeout=90, follow_redirects=True)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "").lower()
    if "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf"):
        pages, failed, total = pdf_pages(resp.content, max_pages=max_pages)
        merged = re.sub(r"\s+", " ", " ".join(t for _, t in pages)).strip()
        quality = assess_text_quality(merged, pages=len(pages) or None)
        return FetchedDocument(
            kind="pdf", url=url, page_texts=tuple(pages), failed_pages=tuple(failed),
            total_pages=total, parse_cap=max_pages, quality=quality,
            raw=resp.content, content_type=content_type,
        )
    text = html_to_text(resp.text)
    return FetchedDocument(
        kind="html", url=url,
        page_texts=((1, text),) if text.strip() else (),
        total_pages=1, parse_cap=max_pages,
        quality=assess_text_quality(text),
        headings=tuple(extract_headings(resp.text)),
        content_type=content_type,
    )
