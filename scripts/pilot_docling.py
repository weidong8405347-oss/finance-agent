#!/usr/bin/env python3
"""Docling 试点对照（tools-plugins 方案 §5.1 实现顺序 5，P1-A 重依赖决策点）。

方案纪律：「Docling 作为结构解析首选试点；现有 pypdf 做轻量路径；只对低质量页
做 OCR。用同一批中文扫描、跨页表和英文年报与 Unstructured 对照后再决定生产组合。」
本脚本交付该对照的 Docling 半边（Unstructured 半边见未做清单——同属重依赖，
先出一边证据再决定是否值得双装）。

样本（全部来自真实研究运行的证据台账，权威 URL）：
1. 2228.HK 中报（pypdf 判定 garbled 的中文 PDF）——OCR 修复能力检验；
2. 3988.HK 2025 年报（411 页，pp.315–320 分部报告附注）——跨页表/后半部表格
   结构还原检验；
3. 2228.HK 英文/双语公告（文本型 PDF 对照组）——正常文本页两路径应一致。

判读维度（报告如实呈现，不自动下生产结论）：文本质量（乱码率）、表格结构
（单元格/表头还原）、页级定位、耗时与资源代价。

用法：uv run python scripts/pilot_docling.py [--out data/pilot/docling]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

SAMPLES = [
    {
        "name": "2228hk-interim-garbled",
        "url": "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0819/2026081900920_c.pdf",
        "why": "pypdf 抽取乱码（B 组 2228 题 4/8 文档之一）——OCR 修复检验",
        "page_range": (1, 6),
    },
    {
        "name": "3988hk-annual-segment-note",
        "url": "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0330/2026033000533_c.pdf",
        "why": "411 页年报的分部报告附注（pp.315–320）——跨页表结构还原检验",
        "page_range": (315, 320),
    },
    {
        "name": "2228hk-bilingual-text",
        "url": "https://www.hkexnews.hk/listedco/listconews/sehk/2026/0325/2026032501035.pdf",
        "why": "文本型 PDF 对照组——两路径应一致（不改善也不退化）",
        "page_range": (1, 4),
    },
]


def _fetch(url: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / (url.rsplit("/", 1)[-1])
    if out.exists() and out.stat().st_size > 1000:
        return out
    import httpx

    resp = httpx.get(url, timeout=120, follow_redirects=True,
                     headers={"User-Agent": "finance-agent docling pilot (local)"})
    resp.raise_for_status()
    out.write_bytes(resp.content)
    return out


def _pypdf_path(pdf: Path, page_range: tuple[int, int]) -> dict:
    from finance_agent.gateway.fetch import pdf_pages
    from finance_agent.gateway.text_quality import assess_text_quality

    start, end = page_range
    t0 = time.monotonic()
    pages, failed, total = pdf_pages(
        pdf.read_bytes(), start=start, max_pages=end - start + 1)
    text = "\n".join(t for _, t in pages)
    quality = assess_text_quality(text, pages=len(pages) or None)
    return {
        "elapsed_s": round(time.monotonic() - t0, 1),
        "pages_parsed": [p for p, _ in pages], "failed_pages": failed,
        "total_pages": total, "chars": len(text),
        "quality": quality.quality, "issues": list(quality.reasons),
        "sample": text[:600],
    }


def _docling_path(pdf: Path, page_range: tuple[int, int], *, ocr: bool) -> dict:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    from finance_agent.gateway.text_quality import assess_text_quality

    opts = PdfPipelineOptions(do_ocr=ocr, do_table_structure=True)
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    t0 = time.monotonic()
    result = converter.convert(str(pdf), page_range=page_range)
    doc = result.document
    md = doc.export_to_markdown()
    tables = []
    for t in getattr(doc, "tables", []) or []:
        try:
            grid = t.export_to_dataframe().shape  # 行×列的结构证据
        except Exception:  # noqa: BLE001 - 单表导出失败不拖死试点
            grid = None
        tables.append({"shape": list(grid) if grid else None,
                       "num_cells": len(getattr(t, "data", []).table_cells
                                          if getattr(t, "data", None) else [])})
    quality = assess_text_quality(md)
    return {
        "elapsed_s": round(time.monotonic() - t0, 1),
        "chars": len(md), "tables": tables,
        "pages": len(getattr(doc, "pages", {}) or {}),
        "quality": quality.quality, "issues": list(quality.reasons),
        "sample": md[:600],
        "conversion_status": str(getattr(result, "status", "?")),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/pilot/docling")
    args = ap.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "samples": []}
    for sample in SAMPLES:
        print(f"[pilot] {sample['name']}: 下载…", flush=True)
        try:
            pdf = _fetch(sample["url"], out_dir / "pdfs")
        except Exception as e:  # noqa: BLE001 - 单样本失败不拖死试点（如实记录）
            report["samples"].append({"name": sample["name"],
                                      "error": f"{type(e).__name__}: {e}"})
            continue
        print(f"[pilot] {sample['name']}: pypdf 路径…", flush=True)
        pypdf_r = _pypdf_path(pdf, sample["page_range"])
        print(f"[pilot] {sample['name']}: docling 路径（OCR 开）…", flush=True)
        try:
            docling_r = _docling_path(pdf, sample["page_range"], ocr=True)
        except Exception as e:  # noqa: BLE001
            docling_r = {"error": f"{type(e).__name__}: {e}"}
        report["samples"].append({
            "name": sample["name"], "why": sample["why"], "url": sample["url"],
            "page_range": list(sample["page_range"]),
            "pypdf": pypdf_r, "docling_ocr": docling_r,
        })
        print(f"  pypdf:   quality={pypdf_r['quality']} chars={pypdf_r['chars']} "
              f"{pypdf_r['elapsed_s']}s")
        if "error" in docling_r:
            print(f"  docling: ERROR {docling_r['error'][:120]}")
        else:
            print(f"  docling: quality={docling_r['quality']} chars={docling_r['chars']} "
                  f"tables={len(docling_r['tables'])} {docling_r['elapsed_s']}s")

    out = out_dir / "pilot-report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[pilot] 报告落盘 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
