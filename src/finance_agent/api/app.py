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


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str


def create_app(
    *,
    kb: BitemporalStore,
    events: EventStore,
    decisions: DecisionStore,
    evals_dir: str | Path,
    research_runner: ResearchRunner | None = None,
    decision_runner: Callable[[str, str, EventStore], None] | None = None,
    research_preflight: Callable[[], str | None] | None = None,
    approval_timeout_s: float = 600.0,
    static_dir: str | Path | None = None,
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
        return [_session_summary(events, r[0], r[1], r[2]) for r in rows]

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
        if research_runner is None:
            raise HTTPException(status_code=503, detail="研究功能未装配")
        if research_preflight is not None:
            problem = research_preflight()
            if problem is not None:
                raise HTTPException(status_code=422, detail=problem)
        run_id = f"live-{uuid.uuid4().hex[:8]}"

        def work() -> None:
            if req.require_approval:
                approval_id = approvals.request(
                    run_id,
                    {"op": "research", "ticker": req.ticker, "objective": req.objective},
                )
                if not approvals.wait(approval_id, timeout=approval_timeout_s):
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

    # ---------------- Chat（对话式主交互，参考 dsh） ----------------

    @app.post("/api/chat")
    def chat(req: ChatRequest) -> dict[str, Any]:
        from .intent import classify_intent, extract_tickers

        run_id = req.session_id or f"live-{uuid.uuid4().hex[:8]}"
        events.append(
            Event(run_id=run_id, type="user/message", payload={"content": req.message})
        )
        intent = classify_intent(req.message)
        tickers = extract_tickers(req.message)

        def assistant(text: str) -> None:
            events.append(
                Event(run_id=run_id, type="assistant/message", payload={"content": text})
            )

        if not tickers:
            if intent == "decide":
                assistant("想让我出投资建议的话，请带上标的代码（如 AAPL、600519）。")
            else:
                assistant(
                    "请告诉我要研究的具体标的（如 AAPL、600519），"
                    "或说明你想做什么：深度研究 / 投资建议。"
                )
            return {"run_id": run_id}

        ticker = tickers[0]
        if research_preflight is not None:
            problem = research_preflight()
            if problem is not None:
                assistant(f"⚠ 配置缺失：{problem}")  # 对话式报错（可见、可复制）
                return {"run_id": run_id}

        def work() -> None:
            if intent == "decide" and decision_runner is not None:
                decision_runner(run_id, ticker, events)
            elif research_runner is not None:
                research_runner(run_id, ticker, req.message, events)
            else:
                assistant("⚠ 后端未装配研究/决策 runner。")

        threading.Thread(target=work, daemon=True).start()
        return {"run_id": run_id}

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

    # ---------------- 前端静态伺服（一条命令 = API + UI，参考 dsh web） ----------------
    _mount_static(app, static_dir)

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


_BUILD_HINT = """<!doctype html><html><body style="font-family:monospace;padding:2em">
<h2>前端未构建</h2>
<p>API 正常（/api/* 可用）。构建 UI：</p>
<pre>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>或重新运行 finance-agent serve（dist 缺失且本机有 npm 时会自动构建）。</p>
</body></html>"""


def _mount_static(app: FastAPI, static_dir: str | Path | None) -> None:
    """SPA 静态伺服：/api/* 端点先行（注册顺序优先）；其余路径先找静态文件，
    再回退 index.html（前端路由）。路径穿越防护：候选必须落在 dist 内。"""
    from fastapi.responses import FileResponse, HTMLResponse

    dist = Path(static_dir).resolve() if static_dir else None

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):  # type: ignore[no-untyped-def]
        if path.startswith("api"):
            raise HTTPException(status_code=404)
        if dist is None or not (dist / "index.html").exists():
            return HTMLResponse(_BUILD_HINT)
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")


def _session_summary(events: EventStore, run_id: str, count: int, started_at: str) -> dict[str, Any]:
    """会话状态投影：error > cancelled > done > running；错误原因直接带出。"""
    rows = events._conn.execute(  # noqa: SLF001
        "SELECT type, payload FROM events WHERE run_id = ? AND"
        " type IN ('research/error', 'research/cancelled', 'research/completed',"
        " 'decision/error', 'decision/completed')",
        (run_id,),
    ).fetchall()
    types = {r[0] for r in rows}
    status = "running"
    detail = None
    if "research/error" in types or "decision/error" in types:
        status = "error"
        import json

        detail = next(
            (
                json.loads(r[1]).get("reason")
                for r in rows
                if r[0] in ("research/error", "decision/error")
            ),
            None,
        )
    elif "research/cancelled" in types:
        status = "cancelled"
    elif "research/completed" in types or "decision/completed" in types:
        status = "done"
    return {
        "run_id": run_id,
        "event_count": count,
        "started_at": started_at,
        "status": status,
        "status_detail": detail,
    }
