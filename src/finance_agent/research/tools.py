"""研究工具集：register_evidence / propose_fact / query_kb。

关键纪律（DESIGN.md §4.1）：
- knowledge_time 由证据推导（= 所绑证据的最大 available_at），**不信任模型自报**；
- 落库必经 ProfileWriter（单写者），硬门禁在 writer 内执行；
- 工具结果携带 provenance，供评估模式的 leakage-audit 审计。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest
from ..knowledge.errors import KnowledgeError
from ..knowledge.models import Evidence, Fact, PitGrade
from ..knowledge.store import BitemporalStore
from ..knowledge.writer import ProfileWriter


class _Tracker:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.rejected: list[dict] = []


def make_research_tools(
    *,
    store: BitemporalStore,
    writer: ProfileWriter,
    manifest: RunManifest,
    entity_kind: str,
    entity_id: str,
    namespace: str = "prod",
    events: EventStore | None = None,  # noqa: ARG001 - 预留给工具级审计
) -> tuple[dict[str, Any], _Tracker]:
    tracker = _Tracker()

    def register_evidence(args: dict[str, Any]) -> dict[str, Any]:
        eid = args.get("evidence_id") or f"ev-{uuid.uuid4().hex[:12]}"
        ev = Evidence(
            evidence_id=eid,
            source_id=args["source_id"],
            url=args.get("url"),
            verbatim_quote=args["verbatim_quote"],
            retrieved_at=datetime.now(UTC),
            available_at=datetime.fromisoformat(args["available_at"]) if args.get("available_at") else None,
            pit_grade=PitGrade(args.get("pit_grade", "C")),
        )
        store.add_evidence(ev)
        return {
            "content": json.dumps({"evidence_id": eid}, ensure_ascii=False),
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

    tools = {
        "register_evidence": register_evidence,
        "propose_fact": propose_fact,
        "query_kb": query_kb,
    }
    return tools, tracker


TOOL_SCHEMAS: dict[str, dict] = {
    "register_evidence": {
        "name": "register_evidence",
        "description": "登记一条证据（来源 + 原文摘录 + 可知时刻）。落库事实前必须先登记证据。",
        "parameters": {
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string", "description": "可选；不提供则自动生成"},
                "source_id": {"type": "string"},
                "url": {"type": "string"},
                "verbatim_quote": {"type": "string", "description": "原文摘录（数字保护的锚点）"},
                "available_at": {"type": "string", "description": "ISO8601；该信息何时公开可知"},
                "pit_grade": {"type": "string", "enum": ["A", "B", "C"]},
            },
            "required": ["source_id", "verbatim_quote", "pit_grade"],
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
}
