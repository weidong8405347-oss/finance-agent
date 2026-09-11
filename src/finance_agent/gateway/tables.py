"""轻量表格抽取（tools-plugins 方案 §5.1 extract_table，无新依赖初版）。

定位与诚实边界：
- 输入是 DocumentStore 已解析的**页文本**（pypdf 纯文本路径）；用确定性启发式
  识别「多列对齐的数字行块」为候选表格——输出的是**候选结构**（表头/单元格/
  币种/期间候选 + 校验问题），不是已核验事实；
- 单元格定位随结果返回（page/table/row/col），供 read_document 回读原文、
  register_evidence 逐字绑定；不确定的数字不得直接写成观测（typed 准入不变）；
- Docling 结构解析试点落地后，本模块退为轻量回退路径（方案 §5.1 实现顺序 5）。

启发式规则（确定性、可测试）：
- 行切分：2+ 连续空白 / tab / │ 分隔出 ≥2 个单元格；
- 表块：一个表头候选行（≥2 单元格）+ ≥2 个连续「含数字行」，行间单元格数
  差异 >1 记 ragged_rows 问题（不丢行，如实展示）；
- 币种候选：$ ¥ € £ / USD RMB HKD / 千元 百万 百万港元 million thousand billion 亿 億；
- 期间候选：FY20xx / 20xx 年度 / Q1-Q4 / H1/H2 / 截至…年…月…日。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: 行内列分隔：2+ 空白 / tab / 竖线
_CELL_SPLIT_RE = re.compile(r"(?:\s{2,}|\t|\|)+")
_DIGIT_RE = re.compile(r"\d")
_CURRENCY_RE = re.compile(
    r"[$¥€£]|USD|RMB|HKD|CNY|千元|千港元|百万|百萬|million|thousand|billion|亿|億",
    re.IGNORECASE,
)
_PERIOD_RE = re.compile(
    r"FY\s?20\d{2}|20\d{2}\s?(?:年度|年报|年末|财年|财政年度)?|Q[1-4]|H[12]|"
    r"截至\s?20\d{2}\s?年\s?\d{1,2}\s?月\s?\d{1,2}\s?日",
    re.IGNORECASE,
)

#: 单页候选表上限（防巨型页打爆上下文；超了显式截断）
MAX_TABLES_PER_PAGE = 6
#: 单表行数上限（超出截断并记 issue）
MAX_ROWS_PER_TABLE = 60


def _split_row(line: str) -> list[str] | None:
    """一行 → 单元格列表；不足 2 格 → None（不是表格行）。"""
    cells = [c.strip() for c in _CELL_SPLIT_RE.split(line.strip()) if c.strip()]
    return cells if len(cells) >= 2 else None


@dataclass
class CandidateTable:
    """一个候选表（启发式识别，未经核验）。"""

    page: int
    table_index: int  # 页内序号（0-based）
    headers: list[str]
    rows: list[list[str]]
    issues: list[str] = field(default_factory=list)
    currency_candidates: list[str] = field(default_factory=list)
    period_candidates: list[str] = field(default_factory=list)

    def cell_locator(self, row: int, col: int) -> dict[str, str]:
        """单元格定位（read_document 回读/证据 locator 用，方案 §4.1 EvidenceSpan）。"""
        return {"page": str(self.page), "table": str(self.table_index),
                "row": str(row), "column": str(col)}

    def as_payload(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "table_index": self.table_index,
            "headers": self.headers,
            "rows": self.rows,
            "row_count": len(self.rows),
            "issues": self.issues,
            "currency_candidates": self.currency_candidates,
            "period_candidates": self.period_candidates,
            "heuristic_parse": True,
        }


def _candidates(cells_iter, pattern: re.Pattern[str], cap: int = 6) -> list[str]:
    out: list[str] = []
    for cell in cells_iter:
        for m in pattern.findall(cell):
            token = m if isinstance(m, str) else m[0]
            token = token.strip()
            if token and token not in out:
                out.append(token)
                if len(out) >= cap:
                    return out
    return out


def extract_tables_from_text(page_text: str, *, page: int) -> list[CandidateTable]:
    """单页文本 → 候选表列表（确定性启发式；无候选 = 空列表，不编造）。"""
    lines = (page_text or "").splitlines()
    tables: list[CandidateTable] = []
    i = 0
    table_index = 0
    while i < len(lines) and len(tables) < MAX_TABLES_PER_PAGE:
        header = _split_row(lines[i])
        if header is None:
            i += 1
            continue
        # 表头候选之后的连续「含数字行」块
        rows: list[list[str]] = []
        issues: list[str] = []
        j = i + 1
        while j < len(lines):
            cells = _split_row(lines[j])
            if cells is None or not any(_DIGIT_RE.search(c) for c in cells):
                break
            if len(cells) != len(header):
                issues.append(
                    f"ragged_rows: 第 {len(rows) + 1} 数据行 {len(cells)} 格 "
                    f"≠ 表头 {len(header)} 格（列对齐存疑，引用前回读原文核对）"
                )
            rows.append(cells)
            j += 1
            if len(rows) >= MAX_ROWS_PER_TABLE:
                issues.append(f"rows_truncated: 超过 {MAX_ROWS_PER_TABLE} 行已截断")
                break
        if len(rows) >= 2:  # ≥2 数据行才算候选表（单行对齐文本误报多）
            all_cells = [c for r in [header, *rows] for c in r]
            tables.append(CandidateTable(
                page=page, table_index=table_index, headers=header, rows=rows,
                issues=issues,
                currency_candidates=_candidates(all_cells, _CURRENCY_RE),
                period_candidates=_candidates(all_cells, _PERIOD_RE),
            ))
            table_index += 1
        i = max(j, i + 1)
    return tables


def extract_tables(
    doc: Any, *, pages: list[int] | None = None,
    max_tables: int = 10,
) -> dict[str, Any]:
    """文档（DocumentStore 的 StoredDocument）→ 候选表清单 + 覆盖说明。

    只扫描已解析页；未解析/失败页显式列出（结构缺口不静默——方案 §4.1
    完整性纪律延伸到表格抽取）：显式给 pages 时列出请求而未得的页，
    缺省扫全部已解析页时列出文档级缺页（missing + failed）。
    """
    if pages is not None:
        wanted = pages
        skipped = [p for p in wanted if p not in doc.page_texts]
    else:
        wanted = doc.parsed_pages
        skipped = [*doc.missing_pages, *doc.failed_pages]
    tables: list[CandidateTable] = []
    scanned: list[int] = []
    truncated = False
    for p in wanted:
        if p not in doc.page_texts:
            continue
        scanned.append(p)
        for t in extract_tables_from_text(doc.page_texts[p], page=p):
            if len(tables) >= max_tables:
                truncated = True
                break
            t.table_index = len(tables)  # 文档级重编号（跨页唯一）
            tables.append(t)
        if truncated:
            break
    return {
        "document_id": doc.document_id,
        "tables": [t.as_payload() for t in tables],
        "pages_scanned": scanned,
        "pages_skipped_unparsed": sorted(skipped),
        "tables_truncated": truncated,
        "completeness": doc.completeness_payload(),
        "note": ("启发式候选结构（未经核验）：数字不得直接写成观测/论断；"
                 "用 read_document 回读该页原文 → register_evidence 逐字绑定后"
                 "再走 propose_metric（typed 准入不变）"),
    }


__all__ = ["CandidateTable", "extract_tables", "extract_tables_from_text"]
