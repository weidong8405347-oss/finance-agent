"""extract_table 轻量路径验收（tools-plugins 方案 §5.1，P1-A 剩余项）。

判据：
- 启发式候选抽取：多列对齐的含数字行块 → 表头/行/单元格 + 币种/期间候选；
- 校验问题可见：列数不齐记 ragged_rows（不丢行）；非表格文本不误报；
- 定位可用：单元格 page/table/row/column locator，供 read_document 回读 +
  register_evidence 逐字绑定（数字不直接成事实）；
- 完整性纪律延伸：只扫已解析页，未解析页显式列出（skipped_unparsed），
  请求页有原件时惰性补解；
- 装配：extract_table 进研究工具面 + 插件声明（documents.reader）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.documents import DocumentStore
from finance_agent.gateway.fetch import FetchedDocument
from finance_agent.gateway.tables import extract_tables, extract_tables_from_text
from finance_agent.gateway.text_quality import TextQuality
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.models import PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.research.evidence_desk import ChunkStore
from finance_agent.research.tools import make_research_tools

NOW = datetime.now(UTC)
T0 = datetime(2024, 3, 1, tzinfo=UTC)

PAGE_WITH_TABLE = """\
Segment results for the year
分部          收入(百万元)    毛利率
燃料电池系统     1,234          35.2%
零部件           456            18.0%
合计           1,690           33.1%
Notes to financial statements follow.
"""

PAGE_NO_TABLE = """\
The company discussed its outlook. Revenue grew compared to last year.
Management remains confident about the pipeline and customer demand.
"""

PAGE_RAGGED = """\
Item            FY2023     FY2022
Revenue         1,690      1,200
Gross profit    560
Net income      210        150
"""


class TestHeuristicExtraction:
    def test_table_detected_with_candidates(self):
        tables = extract_tables_from_text(PAGE_WITH_TABLE, page=7)
        assert len(tables) == 1
        t = tables[0]
        assert t.headers == ["分部", "收入(百万元)", "毛利率"]
        assert len(t.rows) == 3
        assert t.rows[0][1] == "1,234"
        # 币种/期间候选
        assert any("百万" in c for c in t.currency_candidates)
        # 定位：page/table/row/column
        assert t.cell_locator(0, 1) == {"page": "7", "table": "0", "row": "0", "column": "1"}

    def test_no_table_no_false_positive(self):
        assert extract_tables_from_text(PAGE_NO_TABLE, page=1) == []

    def test_ragged_rows_flagged_not_dropped(self):
        tables = extract_tables_from_text(PAGE_RAGGED, page=12)
        assert len(tables) == 1
        t = tables[0]
        assert len(t.rows) == 3, "列数不齐的行保留（不静默丢行）"
        assert any("ragged_rows" in i for i in t.issues)
        assert "FY2023" in t.period_candidates


class TestDocumentLevel:
    def _doc(self, ds: DocumentStore, pages: dict[int, str], *, total=None):
        return ds.add(
            url="u", source_id="edgar",
            fetched=FetchedDocument(
                kind="pdf", url="u",
                page_texts=tuple(sorted(pages.items())),
                total_pages=total or max(pages),
                quality=TextQuality("ok"),
            ),
            available_at=T0, pit_grade=PitGrade.A,
        )

    def test_extract_over_parsed_pages_and_skip_listed(self):
        ds = DocumentStore()
        doc = self._doc(ds, {1: PAGE_NO_TABLE, 3: PAGE_WITH_TABLE}, total=4)
        out = extract_tables(doc)
        assert len(out["tables"]) == 1
        assert out["pages_scanned"] == [1, 3]
        assert out["pages_skipped_unparsed"] == [2, 4]  # 未解析页显式可见（review R11 延伸）
        assert out["completeness"]["status"] == "truncated"
        assert "未经核验" in out["note"]

    def test_page_filter(self):
        ds = DocumentStore()
        doc = self._doc(ds, {1: PAGE_WITH_TABLE, 2: PAGE_RAGGED})
        out = extract_tables(doc, pages=[2])
        assert [t["page"] for t in out["tables"]] == [2]


class TestToolWiring:
    def test_extract_table_tool_roundtrip(self, tmp_path):
        kb = BitemporalStore(tmp_path / "kb.db")
        events = EventStore(tmp_path / "e.db")
        doc_store = DocumentStore()
        doc = doc_store.add(
            url="u", source_id="edgar",
            fetched=FetchedDocument(
                kind="pdf", url="u", page_texts=((1, PAGE_WITH_TABLE),),
                total_pages=1, quality=TextQuality("ok"),
            ),
            available_at=T0, pit_grade=PitGrade.A,
        )
        tools, tracker = make_research_tools(
            store=kb, writer=ProfileWriter(store=kb),
            manifest=RunManifest(run_id="r-tbl", mode=RunMode.LIVE),
            entity_kind="stock", entity_id="BE", chunk_store=ChunkStore(),
            events=events, doc_store=doc_store, fetch_paged=lambda url: None,
        )
        assert "extract_table" in tools
        out = json.loads(tools["extract_table"]({"document_id": doc.document_id})["content"])
        assert out["tables"] and out["tables"][0]["headers"][0] == "分部"
        assert out["tables"][0]["heuristic_parse"] is True
        # 未知文档显式报错（error 与 empty 区分）
        err = tools["extract_table"]({"document_id": "doc-9999"})
        assert err["content"].startswith("error:")

    def test_extract_table_declared_in_plugin(self):
        """schema 与声明同源（能力页与运行一致，P1-C parity 纪律）。"""
        from finance_agent.plugins.builtin import build_builtin_registry
        from finance_agent.research.tools import TOOL_SCHEMAS

        registry = build_builtin_registry(env={"NOVITA_API_KEY": "n"})
        compiled = registry.compile(stage="research", env={"NOVITA_API_KEY": "n"})
        assert "extract_table" in compiled.declared_schemas
        assert compiled.declared_schemas["extract_table"] is TOOL_SCHEMAS["extract_table"]
