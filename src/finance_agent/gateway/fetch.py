"""统一文档正文抓取：HTML + PDF（P4，港股披露全是 PDF——research-capability-upgrade §5.1 D3）。

PIT 语义不变：披露文档自发布起不可变，抓取不产生新时间线；
available_at 一律由 filing 记录继承（read_edgar_filing 的既有纪律）。
"""

from __future__ import annotations

import re
from html import unescape


def fetch_document(
    url: str, *, user_agent: str = "finance-agent research (contact: local@example.com)"
) -> str:
    """抓取 URL 并返回纯文本正文（HTML 剥离标签；PDF 走 pypdf 抽取）。"""
    import httpx  # lazy：核心与测试不依赖网络库

    resp = httpx.get(url, headers={"User-Agent": user_agent}, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "").lower()
    if "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf"):
        return pdf_to_text(resp.content)
    return html_to_text(resp.text)


def html_to_text(html: str) -> str:
    """HTML → 纯文本（原 edgar.fetch_filing_text 的剥离逻辑，保持行为不变）。"""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def pdf_to_text(content: bytes, *, max_pages: int = 80) -> str:
    """PDF bytes → 纯文本（pypdf 抽取；页数上限防巨型年报打爆上下文）。"""
    import io

    from pypdf import PdfReader  # lazy：--extra data

    reader = PdfReader(io.BytesIO(content))
    pages: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue  # 单页抽取失败跳过（扫描件/损坏页），不拖死整份
    return re.sub(r"\s+", " ", " ".join(pages)).strip()
