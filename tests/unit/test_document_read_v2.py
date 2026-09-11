"""Document Read v2 验收（tools-plugins 方案 §5.1 P1-A，PR#3 document-read-v2）。

判据（对应方案 §2「通用全文读取」与 §11 P1-A 交付）：
- 抓取从工具闭包抽为文档服务：原件/规范正文按内容哈希保存，同版本复用不重抓；
- 保页码：PDF 首次解析上限之外的页可惰性续解——重要表在后半部必须可达；
- 完整性显式：total/parsed/failed 与 full/partial/truncated/failed 随响应返回，截断不静默；
- 目录（HTML 标题）与文档内检索（页码+摘录 chunk）；
- PIT 纪律：从检索记录进入继承 available_at/pit_grade；直接 URL 无时间保证（C 级降级）；
- 抓取失败 ≠ 未披露（错误响应显式区分）；
- read_edgar_filing 兼容别名：旧契约 {windows, quality} 不变。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.documents import DocumentStore
from finance_agent.gateway.fetch import (
    FetchedDocument,
    extract_headings,
    pdf_pages,
)
from finance_agent.gateway.text_quality import TextQuality
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.models import PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.research.evidence_desk import (
    ChunkStore,
    EvidenceVerificationError,
    verify_and_build,
)
from finance_agent.research.tools import make_research_tools

NOW = datetime.now(UTC)
T0 = datetime(2024, 3, 1, tzinfo=UTC)


# ---------------- 最小 PDF 生成器（无第三方写库依赖；pypdf 可读） ----------------


def make_pdf(page_texts: list[str]) -> bytes:
    """构造 N 页最小合法 PDF：每页一行 Helvetica 文本（ASCII）。"""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)  # 1-based obj number

    n = len(page_texts)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(page_texts):
        page_no = 4 + 2 * i
        content_no = page_no + 1
        safe = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode()
        add(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            f" /Resources << /Font << /F1 3 0 R >> >> /Contents {content_no} 0 R >>".encode()
        )
        add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
            + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for idx, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n"
            f"%%EOF").encode()
    return bytes(out)


PDF_3P = make_pdf([
    "Page one intro text about the company",
    "Page two segment discussion continues",
    "Page three revenue table total 1234 million",
])


# ---------------- fetch.py：分页解析与目录 ----------------


class TestPagedFetchPrimitives:
    def test_pdf_pages_keeps_page_numbers(self):
        pages, failed, total = pdf_pages(PDF_3P)
        assert total == 3 and not failed
        assert [p for p, _ in pages] == [1, 2, 3]
        assert "revenue table" in pages[2][1]

    def test_pdf_pages_start_offset_for_lazy_continuation(self):
        pages, failed, total = pdf_pages(PDF_3P, start=2, max_pages=2)
        assert total == 3
        assert [p for p, _ in pages] == [2, 3]
        assert "revenue table" in pages[1][1]

    def test_extract_headings_html_toc(self):
        html = ("<html><body><h1>Annual Report</h1><p>x</p>"
                "<h2>Item 7. MD&amp;A</h2><h2>Item 8. Financial Statements</h2></body></html>")
        headings = extract_headings(html)
        assert headings[0] == (1, "Annual Report")
        assert (1, "Item 7. MD&A") in headings
        assert len(headings) == 3


# ---------------- DocumentStore：去重 / 惰性续解 / 完整性 ----------------


def fetched_pdf(*, parse_cap: int = 3, failed: tuple[int, ...] = ()) -> FetchedDocument:
    pages, _f, total = pdf_pages(PDF_3P)
    keep = [(p, t) for p, t in pages if p <= parse_cap and p not in failed]
    return FetchedDocument(
        kind="pdf", url="https://sec.gov/annual.pdf",
        page_texts=tuple(keep), failed_pages=failed, total_pages=total,
        parse_cap=parse_cap, quality=TextQuality("ok"), raw=PDF_3P,
    )


class TestDocumentStore:
    def test_same_origin_reuses_without_refetch(self):
        ds = DocumentStore()
        d1 = ds.add(url="u1", source_id="edgar", fetched=fetched_pdf(),
                    available_at=T0, pit_grade=PitGrade.A)
        d2 = ds.add(url="u1", source_id="edgar", fetched=fetched_pdf(),
                    available_at=T0, pit_grade=PitGrade.A)
        assert d1.document_id == d2.document_id
        assert ds.duplicates >= 1 or d2.reuses >= 0  # add 路径按 hash 复用
        assert len(ds) == 1

    def test_same_content_different_origin_keeps_pit_separate(self):
        """同内容不同来源上下文（A 级记录 vs C 级转载）：身份分开，PIT 不互相污染。"""
        ds = DocumentStore()
        d1 = ds.add(url="u1", source_id="edgar", fetched=fetched_pdf(),
                    available_at=T0, pit_grade=PitGrade.A)
        d2 = ds.add(url="u1", source_id="web_fetch", fetched=fetched_pdf(),
                    available_at=None, pit_grade=PitGrade.C)
        assert d1.document_id != d2.document_id
        assert d1.pit_grade is PitGrade.A and d2.pit_grade is PitGrade.C
        assert d2.page_texts == d1.page_texts  # 解析结果复用（不重复解 PDF）

    def test_find_by_origin_short_circuits(self):
        ds = DocumentStore()
        d1 = ds.add(url="u1", source_id="edgar", fetched=fetched_pdf(),
                    available_at=T0, pit_grade=PitGrade.A)
        hit = ds.find_by_origin("u1", "edgar", T0, PitGrade.A)
        assert hit is not None and hit.document_id == d1.document_id
        assert hit.reuses == 1 and ds.duplicates == 1
        assert ds.find_by_origin("u1", "web_fetch", None, PitGrade.C) is None

    def test_lazy_continuation_parses_remaining_pages(self):
        """首次解析上限之外的页惰性续解：后半部表格必须可达（方案 §2 PDF/HTML P1）。"""
        ds = DocumentStore()
        doc = ds.add(url="u1", source_id="edgar", fetched=fetched_pdf(parse_cap=1),
                     available_at=T0, pit_grade=PitGrade.A)
        assert doc.completeness == "truncated"
        assert doc.completeness_payload()["unparsed_pages"] == 2
        got = ds.ensure_pages(doc, [2, 3])
        assert got == [2, 3]
        assert doc.completeness == "full"
        assert "revenue table" in doc.page_texts[3]

    def test_failed_pages_marked_partial(self):
        ds = DocumentStore()
        doc = ds.add(url="u1", source_id="edgar",
                     fetched=fetched_pdf(failed=(2,)),
                     available_at=T0, pit_grade=PitGrade.A)
        assert doc.completeness == "partial"
        assert doc.completeness_payload()["failed_pages"] == [2]

    def test_empty_document_failed(self):
        ds = DocumentStore()
        empty = FetchedDocument(kind="pdf", url="u", page_texts=(), total_pages=3,
                                quality=TextQuality("needs_ocr"), raw=PDF_3P)
        doc = ds.add(url="u", source_id="edgar", fetched=empty,
                     available_at=T0, pit_grade=PitGrade.A)
        assert doc.completeness == "failed"


# ---------------- 工具层：fetch_document / read_document / search_document ----------------


@pytest.fixture()
def env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    chunk_store = ChunkStore()
    doc_store = DocumentStore()
    fetch_calls: list[str] = []

    def fake_fetch_paged(url: str) -> FetchedDocument:
        fetch_calls.append(url)
        f = fetched_pdf()
        return FetchedDocument(
            kind=f.kind, url=url, page_texts=f.page_texts, failed_pages=f.failed_pages,
            total_pages=f.total_pages, parse_cap=f.parse_cap, quality=f.quality,
            headings=((1, "Annual Report"),), raw=f.raw,
        )

    tools, tracker = make_research_tools(
        store=kb, writer=writer,
        manifest=RunManifest(run_id="live-doc", mode=RunMode.LIVE),
        entity_kind="stock", entity_id="BE", chunk_store=chunk_store,
        events=events, doc_store=doc_store, fetch_paged=fake_fetch_paged,
    )
    # 一条 A 级检索记录（query_* 台账），供 chunk_id 入口
    cid = chunk_store.add(
        source_id="edgar", text="10-K FY2023 filing record", url="https://sec.gov/annual.pdf",
        available_at=T0, pit_grade=PitGrade.A,
    )
    return {
        "kb": kb, "events": events, "tools": tools, "tracker": tracker,
        "chunk_store": chunk_store, "doc_store": doc_store,
        "record_chunk": cid, "fetch_calls": fetch_calls,
    }


def payload(out: dict) -> dict:
    return json.loads(out["content"].split("\n⚠")[0])


class TestDocumentTools:
    def test_fetch_from_record_inherits_pit(self, env):
        out = env["tools"]["fetch_document"]({"chunk_id": env["record_chunk"]})
        data = payload(out)
        assert data["document_id"] == "doc-0001"
        assert data["completeness"]["status"] == "full"
        assert data["completeness"]["total_pages"] == 3
        assert data["toc"] == [{"page": 1, "title": "Annual Report"}]
        assert data["windows"], "无 query 时返回首页窗口（可直接引证）"
        assert out["provenance"][0]["pit_grade"] == "A"
        assert out["provenance"][0]["available_at"] == T0.isoformat()
        assert "pit_note" not in data

    def test_fetch_direct_url_degrades_to_c(self, env):
        out = env["tools"]["fetch_document"]({"url": "https://sec.gov/annual.pdf"})
        data = payload(out)
        assert "无时间保证" in data["pit_note"]
        assert out["provenance"][0]["pit_grade"] == "C"
        assert out["provenance"][0]["available_at"] is None

    def test_fetch_dedupe_same_document_not_refetched(self, env):
        t = env["tools"]
        t["fetch_document"]({"chunk_id": env["record_chunk"]})
        out2 = t["fetch_document"]({"chunk_id": env["record_chunk"], "query": "revenue"})
        data = payload(out2)
        assert data["reused"] is True
        assert env["fetch_calls"] == ["https://sec.gov/annual.pdf"], "同版本只抓取解析一次"
        assert data["windows"] and data["windows"][0]["page"] == 3

    def test_fetch_failure_is_not_nondisclosure(self, env):
        def boom(url: str) -> FetchedDocument:
            raise OSError("connection refused")

        # 用会失败的抓取器换掉 fetch_paged：直接构造独立工具集
        kb = env["kb"]
        tools, _ = make_research_tools(
            store=kb, writer=ProfileWriter(store=kb),
            manifest=RunManifest(run_id="live-doc2", mode=RunMode.LIVE),
            entity_kind="stock", entity_id="BE", chunk_store=ChunkStore(),
            fetch_paged=boom,
        )
        out = tools["fetch_document"]({"url": "https://x.example/a.pdf"})
        assert out["content"].startswith("error: 抓取失败")
        assert "不等于未披露" in out["content"]

    def test_read_document_page_range_and_lazy_parse(self, env):
        t = env["tools"]
        # 首次抓取用受限 parse_cap：模拟 400 页上限外的表
        doc_store: DocumentStore = env["doc_store"]
        doc = doc_store.add(
            url="https://sec.gov/annual.pdf", source_id="edgar",
            fetched=fetched_pdf(parse_cap=1), available_at=T0, pit_grade=PitGrade.A,
        )
        out = t["read_document"]({"document_id": doc.document_id, "page_range": "2-3"})
        data = payload(out)
        assert data["mode"] == "pages"
        pages = {p["page"]: p for p in data["pages"]}
        assert set(pages) == {2, 3}
        assert "revenue table" in pages[3]["text"]
        assert data["completeness"]["status"] == "full"  # 惰性续解后完整
        assert data["unread_pages"] == 0

    def test_read_document_page_text_registered_with_locator(self, env):
        t = env["tools"]
        out = t["fetch_document"]({"chunk_id": env["record_chunk"]})
        doc_id = payload(out)["document_id"]
        out = t["read_document"]({"document_id": doc_id, "page": 3})
        data = payload(out)
        chunk_id = data["pages"][0]["chunk_id"]
        # 证据登记：页文本可逐字引用，locator 带 document/page（数值准入的定位基础）
        ev = verify_and_build(env["chunk_store"], span_id=f"{chunk_id}#s0")
        assert "revenue table" in ev.verbatim_quote
        assert ev.locator["document_id"] == doc_id and ev.locator["page"] == "3"
        assert ev.available_at == T0 and ev.pit_grade is PitGrade.A

    def test_search_document_hits_and_honest_miss(self, env):
        t = env["tools"]
        doc_id = payload(t["fetch_document"]({"chunk_id": env["record_chunk"]}))["document_id"]
        hit = payload(t["search_document"]({"document_id": doc_id, "query": "revenue table"}))
        assert hit["hits"] and hit["hits"][0]["page"] == 3
        assert hit["pages_scanned"] == 3
        miss = payload(t["search_document"]({"document_id": doc_id, "query": "goodwill"}))
        assert miss["hits"] == []
        assert "空结果不等于不存在" in miss["note"]

    def test_read_edgar_filing_alias_contract(self, env):
        """旧契约（chunk_id + query → windows/quality）保持不变，新增 document_id。"""
        t = env["tools"]
        out = t["read_edgar_filing"]({"chunk_id": env["record_chunk"], "query": "revenue"})
        data = payload(out)
        assert data["quality"] == "ok"
        assert data["windows"] and data["windows"][0]["chunk_id"].startswith("chk-")
        assert "revenue table" in data["windows"][0]["text"]
        assert data["document_id"] == "doc-0001"
        assert data["windows"][0]["page"] == 3

    def test_legacy_string_fetcher_still_supported(self, tmp_path):
        """旧装配（fetch_document 返回 str）：单页文档，行为兼容。"""
        kb = BitemporalStore(tmp_path / "kb.db")
        chunk_store = ChunkStore()
        calls: list[str] = []

        def legacy_fetch(url: str) -> str:
            calls.append(url)
            return "产能 2GW 公告。demo 正文。"

        tools, _ = make_research_tools(
            store=kb, writer=ProfileWriter(store=kb),
            manifest=RunManifest(run_id="live-legacy", mode=RunMode.LIVE),
            entity_kind="stock", entity_id="BE", chunk_store=chunk_store,
            fetch_document=legacy_fetch,
        )
        cid = chunk_store.add(source_id="edgar", text="record", url="demo://filing",
                              available_at=T0, pit_grade=PitGrade.A)
        out = tools["read_edgar_filing"]({"chunk_id": cid, "query": "产能"})
        data = json.loads(out["content"])
        assert data["windows"][0]["text"].startswith("产能 2GW")
        # 重复调用走文档库复用，不再打网络
        tools["read_edgar_filing"]({"chunk_id": cid, "query": "产能"})
        assert len(calls) == 1

    def test_garbled_quality_note_and_metric_block(self, tmp_path):
        """乱码正文：响应带警示，chunk 质量标记随证据下沉（指标入口拒写的基础）。"""
        kb = BitemporalStore(tmp_path / "kb.db")
        chunk_store = ChunkStore()
        bad = FetchedDocument(
            kind="pdf", url="u", page_texts=((1, "\ufffd\ufffd\u0003garbled noise"),),
            total_pages=1, quality=TextQuality("garbled", ("noise",), 0.1),
        )
        tools, _ = make_research_tools(
            store=kb, writer=ProfileWriter(store=kb),
            manifest=RunManifest(run_id="live-bad", mode=RunMode.LIVE),
            entity_kind="stock", entity_id="BE", chunk_store=chunk_store,
            fetch_paged=lambda url: bad,
        )
        out = tools["fetch_document"]({"url": "https://x.example/scan.pdf"})
        assert "抽取质量=garbled" in out["content"]
        data = json.loads(out["content"].split("\n⚠")[0])
        chunk_id = data["windows"][0]["chunk_id"]
        ev = verify_and_build(chunk_store, span_id=f"{chunk_id}#s0")
        assert ev.quality == "garbled"

    def test_evidence_registration_still_verbatim_gated(self, env):
        """文档窗口进入台账后，证据纪律不变：非逐字子串拒绝。"""
        t = env["tools"]
        out = t["fetch_document"]({"chunk_id": env["record_chunk"], "query": "revenue"})
        chunk_id = payload(out)["windows"][0]["chunk_id"]
        with pytest.raises(EvidenceVerificationError):
            verify_and_build(env["chunk_store"], chunk_id=chunk_id,
                             verbatim_quote="编造的数字 9999 million")
        ev = verify_and_build(env["chunk_store"], chunk_id=chunk_id,
                              verbatim_quote="revenue table total 1234 million")
        assert ev.evidence_id.startswith("ev-")
