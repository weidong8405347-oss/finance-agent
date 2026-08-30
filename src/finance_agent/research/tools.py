"""研究工具集：read_edgar_filing / register_evidence / propose_fact / query_kb。

关键纪律（DESIGN.md §4.1 + 2026-08-30 验收事故整改）：
- **证据只能登记自本 run 实际检索到的内容**（EvidenceDesk chunk）：
  网关记录自带 chunk_id；filing 正文用 read_edgar_filing 抓取切块；
- register_evidence 服务端校验 quote 是 chunk 的逐珠子串，来源/available_at 由
  chunk 元数据推导——模型自报的来源与时刻一律不信（堵参数记忆自编自引）；
- knowledge_time 由证据推导（= 所绑证据的最大 available_at），不信任模型自报；
- 落库必经 ProfileWriter（单写者），硬门禁在 writer 内执行；
- 工具结果携带 provenance，供评估模式的 leakage-audit 审计。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest
from ..knowledge.errors import KnowledgeError
from ..knowledge.models import Fact
from ..knowledge.store import BitemporalStore
from ..knowledge.writer import ProfileWriter
from .evidence_desk import ChunkStore, EvidenceVerificationError, verify_and_build

#: 抓取函数的签名：filing URL → 纯文本正文（HTML 已剥离）
FetchDocument = Callable[[str], str]

_WS = re.compile(r"\s+")


class _Tracker:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.rejected: list[dict] = []


def _windows(text: str, query: str, *, width: int = 1600, max_windows: int = 4) -> list[str]:
    """围绕 query 命中点切窗口；无命中/无 query → 文档开头两段窗口。"""
    if query:
        hits = [m.start() for m in re.finditer(re.escape(query), text, flags=re.IGNORECASE)]
        if hits:
            out: list[str] = []
            for pos in hits:
                start = max(0, pos - width // 3)
                window = text[start : start + width]
                if not out or window not in out[-1]:  # 粗略去重（重叠窗口）
                    out.append(window)
                if len(out) >= max_windows:
                    break
            return out
    return [text[:width], text[width : width * 2]] if len(text) > width else [text]


def make_research_tools(
    *,
    store: BitemporalStore,
    writer: ProfileWriter,
    manifest: RunManifest,
    entity_kind: str,
    entity_id: str,
    namespace: str = "prod",
    chunk_store: ChunkStore,
    fetch_document: FetchDocument | None = None,
    events: EventStore | None = None,  # noqa: ARG001 - 预留给工具级审计
) -> tuple[dict[str, Any], _Tracker]:
    tracker = _Tracker()

    def register_evidence(args: dict[str, Any]) -> dict[str, Any]:
        """证据登记：只接受 chunk_id + 逐字摘录（服务端子串校验，fail-closed）。"""
        try:
            ev = verify_and_build(
                chunk_store,
                chunk_id=str(args.get("chunk_id") or ""),
                verbatim_quote=str(args.get("verbatim_quote") or ""),
                evidence_id=args.get("evidence_id"),
            )
        except EvidenceVerificationError as e:
            tracker.rejected.append({"evidence": str(args.get("chunk_id")), "reason": str(e)})
            return {"content": f"rejected: {e}", "provenance": []}
        store.add_evidence(ev)
        return {
            "content": json.dumps({"evidence_id": ev.evidence_id}, ensure_ascii=False),
            "provenance": [
                {
                    "source_id": ev.source_id,
                    "available_at": ev.available_at.isoformat() if ev.available_at else None,
                    "pit_grade": ev.pit_grade.value,
                }
            ],
        }

    def propose_fact(args: dict[str, Any]) -> dict[str, Any]:
        field = args["field"]
        evidence_ids: list[str] = args["evidence_ids"]
        try:
            evidences = [store.get_evidence(e) for e in evidence_ids]
            # knowledge_time 推导：所绑证据的最晚可知时刻；全无（C 级）则取当前（生产模式）
            known = [e.available_at for e in evidences if e.available_at is not None]
            knowledge_time = max(known) if known else datetime.now(UTC)
            fact = Fact(
                entity_kind=entity_kind,
                entity_id=entity_id,
                field=field,
                value=args["value"],
                event_time=datetime.fromisoformat(args["event_time"]) if args.get("event_time") else None,
                knowledge_time=knowledge_time,
                evidence_ids=evidence_ids,
                run_id=manifest.run_id,
            )
            fact_id = writer.write_fact(fact, run=manifest, namespace=namespace)
        except (KnowledgeError, KeyError, ValueError) as e:
            tracker.rejected.append({"field": field, "reason": str(e)})
            return {"content": f"rejected: {e}", "provenance": []}
        tracker.written.append(field)
        return {
            "content": json.dumps({"fact_id": fact_id, "field": field}, ensure_ascii=False),
            "provenance": [
                {
                    "source_id": e.source_id,
                    "available_at": e.available_at.isoformat() if e.available_at else None,
                    "pit_grade": e.pit_grade.value,
                }
                for e in evidences
            ],
        }

    def query_kb(args: dict[str, Any]) -> dict[str, Any]:
        del args
        profile = store.as_of(entity_kind, entity_id, datetime.now(UTC), namespace=namespace)
        return {
            "content": json.dumps(
                {
                    f: {"value": r.value, "knowledge_time": r.knowledge_time.isoformat()}
                    for f, r in profile.items()
                },
                ensure_ascii=False,
                default=str,
            ),
            "provenance": [
                {"source_id": "kb", "available_at": r.knowledge_time.isoformat(), "pit_grade": "A"}
                for r in profile.values()
            ],
        }

    tools: dict[str, Any] = {
        "register_evidence": register_evidence,
        "propose_fact": propose_fact,
        "query_kb": query_kb,
    }

    if fetch_document is not None:
        def read_edgar_filing(args: dict[str, Any]) -> dict[str, Any]:
            """抓取 filing 正文并按 query 切窗口，窗口落 ChunkStore 供 register_evidence 引用。"""
            record_chunk = chunk_store.get(str(args.get("chunk_id") or ""))
            if record_chunk is None:
                return {"content": "error: 未知 chunk_id（先 query_edgar 拿 filing 记录）", "provenance": []}
            if not record_chunk.url:
                return {"content": "error: 该记录没有可抓取的 url", "provenance": []}
            try:
                text = fetch_document(record_chunk.url)
            except Exception as e:
                return {"content": f"error: 抓取失败：{type(e).__name__}: {e}", "provenance": []}
            out = []
            for window in _windows(text, str(args.get("query") or "")):
                cid = chunk_store.add(
                    source_id=record_chunk.source_id,
                    text=window,
                    url=record_chunk.url,
                    available_at=record_chunk.available_at,  # PIT 元数据从 filing 记录继承
                    pit_grade=record_chunk.pit_grade,
                )
                out.append({"chunk_id": cid, "text": window})
            return {
                "content": json.dumps({"windows": out}, ensure_ascii=False),
                "provenance": [
                    {
                        "source_id": record_chunk.source_id,
                        "available_at": record_chunk.available_at.isoformat()
                        if record_chunk.available_at
                        else None,
                        "pit_grade": record_chunk.pit_grade.value,
                    }
                ],
            }

        tools["read_edgar_filing"] = read_edgar_filing

    return tools, tracker


TOOL_SCHEMAS: dict[str, dict] = {
    "register_evidence": {
        "name": "register_evidence",
        "description": (
            "登记一条证据。quote 必须是所引 chunk 的逐字原文（服务端校验子串，不符即拒）；"
            "来源与可知时刻由系统从 chunk 推导，无需自报。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string",
                             "description": "检索内容 chunk id（query_* 或 read_edgar_filing 返回）"},
                "verbatim_quote": {"type": "string", "description": "该 chunk 内的逐字原文摘录"},
                "evidence_id": {"type": "string", "description": "可选；不提供则自动生成"},
            },
            "required": ["chunk_id", "verbatim_quote"],
        },
    },
    "propose_fact": {
        "name": "propose_fact",
        "description": "把一条事实写入实体档案（必须绑 ≥1 条已登记证据；knowledge_time 由证据推导）",
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string"},
                "value": {},
                "event_time": {"type": "string", "description": "事实何时为真（如财年截止日）"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["field", "value", "evidence_ids"],
        },
    },
    "query_kb": {
        "name": "query_kb",
        "description": "查询当前实体档案（as_of 现在的投影）",
        "parameters": {"type": "object", "properties": {}},
    },
    "read_edgar_filing": {
        "name": "read_edgar_filing",
        "description": (
            "抓取一条 EDGAR filing 记录的正文，按 query 关键词切出原文窗口"
            "（返回的窗口带新 chunk_id，供 register_evidence 引用）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string", "description": "query_edgar 返回的 filing 记录 chunk_id"},
                "query": {"type": "string", "description": "定位关键词（如 'total revenue'）"},
            },
            "required": ["chunk_id"],
        },
    },
}
