"""统一文档正文抓取：HTML + PDF（P4，港股披露全是 PDF——research-capability-upgrade §5.1 D3）。

PIT 语义不变：披露文档自发布起不可变，抓取不产生新时间线；
available_at 一律由 filing 记录继承（read_edgar_filing 的既有纪律）。
"""

from __future__ import annotations

import re
from html import unescape

from .text_quality import TextQuality, assess_text_quality


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
    import io

    from pypdf import PdfReader  # lazy：--extra data

    reader = PdfReader(io.BytesIO(content))
    pages: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue  # 单页抽取失败跳过（扫描件/损坏页），不拖死整份
    text = re.sub(r"\s+", " ", " ".join(pages)).strip()
    return text, min(len(reader.pages), max_pages)
