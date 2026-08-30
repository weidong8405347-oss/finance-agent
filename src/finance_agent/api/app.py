"""FastAPI 投影层：UI 的唯一数据入口（只读投影 + 命令入口）。

所有读端点都是 EventStore / BitemporalStore / DecisionStore 的投影——
「UI 不拥有状态」（DESIGN.md §4.4），每个字段可回指 event id / fact 版本。

交互模型（redesign §3）：/api/chat 是唯一写入口——
- 以 / 开头 → command 确定性派发（CommandRunner）；
- 否则 → 主 agent 对话 turn（ChatService 串行认领）。
意图理解在主 agent（LLM），没有正则路由层。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..chat.service import ChatService
from ..commands.registry import catalog as command_catalog
from ..commands.registry import parse_command
from ..commands.runner import CommandRequest, CommandRunner
from ..decision.store import DecisionStore
from ..eventstore.events import (
    COMMAND_DONE,
    COMMAND_RUN,
    RUN_CREATED,
    SESSION_TITLE,
    TURN_END,
    TURN_START,
    USER_MESSAGE,
    Event,
)
from ..eventstore.store import EventStore
from ..harness.approvals import ApprovalService
from ..knowledge.store import BitemporalStore
from .sse import iter_sse_events

logger = logging.getLogger("finance_agent.api")


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str


def create_app(
    *,
    kb: BitemporalStore,
    events: EventStore,
    decisions: DecisionStore,
    evals_dir: str | Path,
    chat_service: ChatService | None = None,
    command_runner: CommandRunner | None = None,
    approvals: ApprovalService | None = None,
    static_dir: str | Path | None = None,
) -> FastAPI:
    app = FastAPI(title="finance-agent", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],  # vite dev
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    evals_path = Path(evals_dir)
    approvals = approvals or ApprovalService(events)

    # ---------------- Sessions ----------------

    @app.get("/api/sessions")
    def list_sessions() -> list[dict[str, Any]]:
        rows = events._conn.execute(  # noqa: SLF001 - 投影层只读聚合
            "SELECT run_id, COUNT(*), MIN(ts), MAX(ts) FROM events GROUP BY run_id"
            " ORDER BY MAX(ts) DESC"
        ).fetchall()
        child_ids = _child_run_ids(events)
        return [
            _session_summary(events, r[0], r[2], r[3])
            for r in rows
            if r[0] not in child_ids
        ]

    @app.get("/api/sessions/{run_id}/events")
    def session_events(run_id: str) -> list[dict[str, Any]]:
        return [_event_json(e) for e in events.read(run_id)]

    @app.get("/api/sessions/{run_id}/children")
    def session_children(run_id: str) -> list[dict[str, Any]]:
        """子 run 目录（step agent 钻取）：run/created 里 parent_run_id 指向本会话者。"""
        rows = events._conn.execute(  # noqa: SLF001
            "SELECT run_id, payload, MIN(ts) FROM events WHERE type = ?"
            " AND json_extract(payload, '$.parent_run_id') = ? GROUP BY run_id",
            (RUN_CREATED, run_id),
        ).fetchall()
        out = []
        for child_id, payload, started in rows:
            meta = json.loads(payload)
            out.append(
                {
                    "run_id": child_id,
                    "step": meta.get("step"),
                    "kind": meta.get("kind"),
                    "command_id": meta.get("command_id"),
                    "started_at": started,
                    "status": _child_status(events, child_id),
                }
            )
        return out

    @app.get("/api/sessions/{run_id}/stream")
    def stream_events(run_id: str) -> StreamingResponse:
        return StreamingResponse(
            iter_sse_events(events, run_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---------------- Commands ----------------

    @app.get("/api/commands")
    def list_commands() -> list[dict[str, Any]]:
        return command_catalog()  # composer 补全 + 帮助

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
                    "evidence": [_evidence_json(kb, eid) for eid in rec.evidence_ids],
                }
                for field, rec in sorted(profile.items())
            },
        }

    # ---------------- Chat（唯一写入口） ----------------

    @app.post("/api/chat")
    def chat(req: ChatRequest) -> dict[str, Any]:
        text = req.message.strip()
        if not text:
            raise HTTPException(status_code=422, detail="消息为空")
        run_id = req.session_id or f"live-{uuid.uuid4().hex[:8]}"
        is_new = events.head_seq(run_id) == 0
        if is_new:
            events.append(Event(run_id=run_id, type=SESSION_TITLE, payload={"title": text[:30]}))

        parsed = parse_command(text)
        if parsed is not None:
            if command_runner is None:
                command_id = f"cmd-{uuid.uuid4().hex[:8]}"
                events.append(Event(run_id=run_id, type=COMMAND_RUN, payload={
                    "command_id": command_id, "name": parsed.name, "args": {},
                    "raw_input": parsed.raw_input,
                }))
                events.append(Event(run_id=run_id, type=COMMAND_DONE, payload={
                    "command_id": command_id, "outcome": "error",
                    "summary": "command 执行器未装配（command_runner 缺失）",
                }))
                return {"run_id": run_id, "command_id": command_id}
            command_id = command_runner.start(
                CommandRequest(session_run_id=run_id, parsed=parsed)
            )
            return {"run_id": run_id, "command_id": command_id}

        # 主 agent 路径：新会话先落 system 契约（模型上下文首条 = 契约）。
        # provider 未配置 → 对话式报错（可见、可操作），不落垃圾 turn。
        if is_new and chat_service is not None:
            from ..llm.router import ProviderConfigError

            try:
                chat_service.begin_session(run_id)
            except ProviderConfigError as e:
                logger.warning("provider 未配置，对话降级为指引：%s", e)  # 通道二：日志
                events.append(Event(run_id=run_id, type=USER_MESSAGE, payload={"content": text}))
                events.append(Event(run_id=run_id, type="assistant/message", payload={
                    "content": f"⚠ 未配置 LLM provider：{e}",
                }))
                return {"run_id": run_id}
        events.append(Event(run_id=run_id, type=USER_MESSAGE, payload={"content": text}))
        if chat_service is None:
            events.append(Event(run_id=run_id, type="assistant/message", payload={
                "content": "⚠ 主 agent 未装配（chat_service 缺失）。",
            }))
        else:
            chat_service.submit_message(run_id)
        return {"run_id": run_id}

    @app.post("/api/sessions/{run_id}/stop")
    def stop_session(run_id: str) -> dict[str, Any]:
        """Stop 按钮：中断该会话正在运行的 command（轮次边界安全停，Q6）。"""
        if command_runner is None:
            return {"stopped": None}
        return {"stopped": command_runner.cancel(run_id)}

    # ---------------- 审批 ----------------

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
        report_file = evals_path / eval_run_id / "report.json"
        if not report_file.exists():
            return {"error": "not found"}
        return json.loads(report_file.read_text())

    # ---------------- 前端静态伺服（一条命令 = API + UI，参考 dsh web） ----------------
    _mount_static(app, static_dir)

    return app


# ---------------- 投影辅助 ----------------


def _event_json(e: Any) -> dict[str, Any]:
    return {
        "seq": e.seq,
        "type": e.type,
        "turn": e.turn,
        "step": e.step,
        "payload": e.payload,
        "ts": e.ts.isoformat(),
    }


def _child_run_ids(events: EventStore) -> set[str]:
    rows = events._conn.execute(  # noqa: SLF001
        "SELECT DISTINCT run_id FROM events WHERE type = ?"
        " AND json_extract(payload, '$.parent_run_id') IS NOT NULL",
        (RUN_CREATED,),
    ).fetchall()
    return {r[0] for r in rows}


def _child_status(events: EventStore, run_id: str) -> str:
    types = [e.type for e in events.read(run_id)]
    if any(t.endswith("/error") for t in types):
        return "error"
    opens = sum(1 for t in types if t == TURN_START) - sum(1 for t in types if t == TURN_END)
    return "running" if opens > 0 else "done"


def _session_summary(events: EventStore, run_id: str, started_at: str, last_active: str) -> dict[str, Any]:
    """会话状态投影：error > running > cancelled > done/idle；错误原因直接带出。"""
    evs = events.read(run_id)
    title = next(
        (e.payload.get("title") for e in evs if e.type == SESSION_TITLE),
        None,
    )
    status, detail = "idle", None
    open_turns = 0
    open_commands: set[str] = set()
    saw_done = False
    for e in evs:
        if e.type == TURN_START:
            open_turns += 1
        elif e.type == TURN_END:
            open_turns = max(0, open_turns - 1)
        elif e.type == COMMAND_RUN:
            open_commands.add(e.payload.get("command_id", ""))
        elif e.type == COMMAND_DONE:
            open_commands.discard(e.payload.get("command_id", ""))
            oc = e.payload.get("outcome")
            if oc == "completed":
                saw_done = True
            elif oc in ("error", "blocked"):
                status, detail = "error", e.payload.get("summary")
            elif oc in ("cancelled", "rejected"):
                status = "cancelled"  # 用户拒绝/主动停：可见但不算错误
        elif e.type in ("research/error", "decision/error", "turn/error"):
            status, detail = "error", e.payload.get("reason")
        elif e.type in ("research/completed", "decision/completed"):
            saw_done = True
        elif e.type == "research/cancelled":
            status = "cancelled"
    if status != "error":
        if open_turns > 0 or open_commands:
            status = "running"
        elif saw_done:
            status = "done"
    return {
        "run_id": run_id,
        "title": title,
        "started_at": started_at,
        "last_active": last_active,
        "status": status,
        "status_detail": detail,
    }


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
