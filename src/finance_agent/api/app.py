"""FastAPI 投影层：UI 的唯一数据入口（只读投影 + 命令入口）。

所有读端点都是 EventStore / BitemporalStore / DecisionStore 的投影——
「UI 不拥有状态」（DESIGN.md §4.4），每个字段可回指 event id / fact 版本。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..decision.store import DecisionStore
from ..eventstore.events import Event
from ..eventstore.store import EventStore
from ..harness.approvals import ApprovalService
from ..knowledge.store import BitemporalStore
from .sse import iter_sse_events

# research_runner(run_id, ticker, objective, events) —— 命令入口的注入点（测试用假 runner）
ResearchRunner = Callable[[str, str, str, EventStore], None]


class ResearchRequest(BaseModel):
    """模块级定义：函数内局部类在 `from __future__ import annotations` 下
    无法被 FastAPI 解析为请求体模型（会被误当 query 参数）。"""

    ticker: str
    objective: str = ""
    require_approval: bool = False  # milestone 档：高成本操作先审批


def create_app(
    *,
    kb: BitemporalStore,
    events: EventStore,
    decisions: DecisionStore,
    evals_dir: str | Path,
    research_runner: ResearchRunner | None = None,
) -> FastAPI:
    app = FastAPI(title="finance-agent", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],  # vite dev
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    evals_path = Path(evals_dir)
    approvals = ApprovalService(events)

    # ---------------- Sessions ----------------

    @app.get("/api/sessions")
    def list_sessions() -> list[dict[str, Any]]:
        rows = events._conn.execute(  # noqa: SLF001 - 投影层只读聚合
            "SELECT run_id, COUNT(*), MIN(ts) FROM events GROUP BY run_id ORDER BY MIN(ts) DESC"
        ).fetchall()
        return [
            {"run_id": r[0], "event_count": r[1], "started_at": r[2]}
            for r in rows
        ]

    @app.get("/api/sessions/{run_id}/events")
    def session_events(run_id: str) -> list[dict[str, Any]]:
        return [
            {
                "seq": e.seq,
                "type": e.type,
                "turn": e.turn,
                "step": e.step,
                "payload": e.payload,
                "ts": e.ts.isoformat(),
            }
            for e in events.read(run_id)
        ]

    # ---------------- Knowledge（as_of 时光机） ----------------

    @app.get("/api/knowledge/entities")
    def list_entities(namespace: str = "prod") -> list[dict[str, Any]]:
        rows = kb._conn.execute(  # noqa: SLF001
            "SELECT entity_kind, entity_id, COUNT(DISTINCT field) FROM facts"
            " WHERE namespace = ? GROUP BY entity_kind, entity_id ORDER BY entity_kind, entity_id",
            (namespace,),
        ).fetchall()
        return [{"kind": r[0], "id": r[1], "field_count": r[2]} for r in rows]

    @app.get("/api/knowledge/{kind}/{entity_id}")
    def entity_profile(
        kind: str,
        entity_id: str,
        as_of: Annotated[datetime | None, Query()] = None,
        namespace: str = "prod",
    ) -> dict[str, Any]:
        t = as_of or datetime.now(UTC)
        profile = kb.view(kind, entity_id, t, namespace=namespace)
        return {
            "kind": kind,
            "id": entity_id,
            "as_of": t.isoformat(),
            "namespace": namespace,
            "facts": {
                field: {
                    "value": rec.value,
                    "event_time": rec.event_time.isoformat() if rec.event_time else None,
                    "knowledge_time": rec.knowledge_time.isoformat(),
                    "version": rec.version,
                    "conflict": rec.conflict_flag,
                    "evidence": [
                        _evidence_json(kb, eid) for eid in rec.evidence_ids
                    ],
                }
                for field, rec in sorted(profile.items())
            },
        }

    @app.get("/api/sessions/{run_id}/stream")
    def stream_events(run_id: str) -> StreamingResponse:
        return StreamingResponse(
            iter_sse_events(events, run_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---------------- 命令入口（写操作经审批闸） ----------------

    @app.post("/api/research")
    def start_research(req: ResearchRequest) -> dict[str, Any]:
        run_id = f"live-{uuid.uuid4().hex[:8]}"

        def work() -> None:
            if req.require_approval:
                approval_id = approvals.request(
                    run_id,
                    {"op": "research", "ticker": req.ticker, "objective": req.objective},
                )
                if not approvals.wait(approval_id):
                    events.append(
                        Event(run_id=run_id, type="research/cancelled", payload={"reason": "rejected"})
                    )
                    return
            if research_runner is not None:
                research_runner(run_id, req.ticker, req.objective, events)

        threading.Thread(target=work, daemon=True).start()
        return {"run_id": run_id, "status": "started"}

    @app.get("/api/approvals/pending")
    def list_pending_approvals() -> list[dict[str, Any]]:
        return [
            {
                "approval_id": r.approval_id,
                "run_id": r.run_id,
                "detail": r.detail,
                "created_at": r.created_at.isoformat(),
            }
            for r in approvals.pending()
        ]

    @app.post("/api/approvals/{approval_id}")
    def decide_approval(approval_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            approvals.decide(approval_id, bool(body.get("approved")))
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown approval_id") from None
        return {"ok": True}

    # ---------------- Decisions ----------------

    @app.get("/api/decisions")
    def list_decisions(namespace: str = "prod") -> list[dict[str, Any]]:
        return [c.model_dump(mode="json") for c in decisions.list(namespace=namespace)]

    # ---------------- Evaluations ----------------

    @app.get("/api/evaluations")
    def list_evaluations() -> list[dict[str, Any]]:
        if not evals_path.exists():
            return []
        out = []
        for d in sorted(evals_path.iterdir()):
            report_file = d / "report.json"
            if report_file.exists():
                import json

                report = json.loads(report_file.read_text())
                out.append(
                    {
                        "eval_run_id": report["eval_run_id"],
                        "config_name": report["config_name"],
                        "verdict": report["verdict"],
                        "leakage_events": report["leakage_events"],
                        "mean_net_return": report["aggregate"]["mean_net_return"],
                        "kb_delta": report["aggregate"]["kb_delta"],
                    }
                )
        return out

    @app.get("/api/evaluations/{eval_run_id}")
    def evaluation_report(eval_run_id: str) -> dict[str, Any]:
        import json

        report_file = evals_path / eval_run_id / "report.json"
        if not report_file.exists():
            return {"error": "not found"}
        return json.loads(report_file.read_text())

    return app


def _evidence_json(kb: BitemporalStore, evidence_id: str) -> dict[str, Any]:
    try:
        ev = kb.get_evidence(evidence_id)
    except Exception:
        return {"evidence_id": evidence_id, "missing": True}
    return {
        "evidence_id": ev.evidence_id,
        "source_id": ev.source_id,
        "url": ev.url,
        "verbatim_quote": ev.verbatim_quote,
        "available_at": ev.available_at.isoformat() if ev.available_at else None,
        "pit_grade": ev.pit_grade.value,
    }
