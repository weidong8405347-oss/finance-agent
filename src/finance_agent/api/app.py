"""FastAPI 投影层：UI 的唯一数据入口（只读投影 + 命令入口）。

所有读端点都是 EventStore / BitemporalStore / DecisionStore 的投影——
「UI 不拥有状态」（DESIGN.md §4.4），每个字段可回指 event id / fact 版本。

交互模型（redesign §3）：/api/chat 是唯一写入口——
- 以 / 开头 → command 确定性派发（CommandRunner）；
- 否则 → 主 agent 对话 turn（ChatService 串行认领）。
意图理解在主 agent（LLM），没有正则路由层。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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
from ..harness.manifest import RunManifest, RunMode
from ..knowledge.errors import (
    KnowledgeInvariantError,
    KnowledgeQualityError,
    NumericGuardError,
)
from ..knowledge.models import Fact
from ..knowledge.store import BitemporalStore
from ..knowledge.writer import ProfileWriter
from .sse import iter_sse_events

logger = logging.getLogger("finance_agent.api")


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str


class SteerRequest(BaseModel):
    """运行中改向（Q6 后置项）：注入到当前 step 的子 run，后续 step 继承。"""

    message: str = Field(min_length=1, description="改向指令（如「重点看竞争对手格局」）")
    command_id: str | None = Field(default=None, description="可选；缺省注入全部活跃 command")


class ResolveConflictRequest(BaseModel):
    """详情页人工裁决请求：keep_fact_id = 「以此为准」的版本（详情页/版本链上的 fact_id）。"""

    field: str
    keep_fact_id: str
    note: str | None = None


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
    knowledge_dir: str | Path = "knowledge",
    reports_dir: str | Path = "data/reports",
    capabilities_info: Callable[[], dict[str, Any]] | None = None,
    data_dir: str | Path = "data",
    router_factory: Callable[[], Any] | None = None,  # 有效 LLMRouter（cli 注入，P5 自配页用）
    metrics: Any | None = None,  # MetricStore（v2 档案路由；缺省 = v2 不挂载）
    dossier_service: Any | None = None,  # DossierService
    calculation_service: Any | None = None,  # CalculationService（估值预览）
) -> FastAPI:
    app = FastAPI(title="finance-agent", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],  # vite dev
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    evals_path = Path(evals_dir)
    knowledge_path = Path(knowledge_dir)
    reports_path = Path(reports_dir)
    approvals = approvals or ApprovalService(events)
    kb_writer = ProfileWriter(store=kb, events=events)  # 唯一写入者（铁律 3）：人工裁决也走单写者

    # ---------------- Sessions ----------------

    @app.get("/api/sessions")
    def list_sessions() -> list[dict[str, Any]]:
        # 会话识别下沉到 EventStore（单一口径）：排除子 run、系统/脚本 run
        # （kb-* 维护审计、migration-* 影子迁移、dossier 快照发布），以及只有投影
        # 副作用事件、点开什么都没有的空壳 run（migration-shadow 即此形态）
        return [
            _session_summary(events, r["run_id"], r["started_at"], r["last_active"])
            for r in events.session_runs()
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
        """档案列表投影：完整度/陈旧/冲突/最近可知时刻 + verify 准入质量投影。

        完整度只回答「schema 字段有没有值」；质量分/验收状态回答「值配不配进知识库」
        （验收事故：完整度 100% 但点进去没内容——两个口径从此并排展示）。
        """
        from ..knowledge.gaps import GapAnalyzer
        from ..knowledge.verify import verify_entity

        rows = kb._conn.execute(  # noqa: SLF001
            "SELECT entity_kind, entity_id, COUNT(DISTINCT field), MAX(knowledge_time) FROM facts"
            " WHERE namespace = ? GROUP BY entity_kind, entity_id ORDER BY entity_kind, entity_id",
            (namespace,),
        ).fetchall()
        analyzer = GapAnalyzer(kb)
        now = datetime.now(UTC)
        out = []
        for r in rows:
            g = analyzer.analyze(r[0], r[1], now, namespace=namespace)
            q = verify_entity(kb, r[0], r[1], now, namespace=namespace)
            out.append(
                {
                    "kind": r[0],
                    "id": r[1],
                    "field_count": r[2],
                    "last_knowledge_time": r[3],
                    "completeness": g.completeness,
                    "stale_count": len(g.stale),
                    "conflict_count": len(g.conflicts),
                    "quality_score": q.quality_score,
                    "quality_status": q.status,
                    "quality_issues": q.issues,
                }
            )
        return out

    @app.get("/api/knowledge/{kind}/{entity_id}")
    def entity_profile(
        kind: str,
        entity_id: str,
        as_of: Annotated[datetime | None, Query()] = None,
        namespace: str = "prod",
    ) -> dict[str, Any]:
        t = as_of or datetime.now(UTC)
        profile = kb.view(kind, entity_id, t, namespace=namespace)
        from ..knowledge.verify import field_issues, verify_entity

        quality = verify_entity(kb, kind, entity_id, t, namespace=namespace)

        def _issues(rec: Any) -> list[str]:
            evidences = []
            for eid in rec.evidence_ids:
                with contextlib.suppress(Exception):
                    evidences.append(kb.get_evidence(eid))
            return field_issues(rec.field, rec, evidences)

        return {
            "kind": kind,
            "id": entity_id,
            "as_of": t.isoformat(),
            "namespace": namespace,
            "quality": quality.model_dump(mode="json"),
            "facts": {
                field: {
                    "fact_id": rec.fact_id,
                    "value": rec.value,
                    "event_time": rec.event_time.isoformat() if rec.event_time else None,
                    "knowledge_time": rec.knowledge_time.isoformat(),
                    "version": rec.version,
                    "conflict": rec.conflict_flag,
                    "issues": _issues(rec),
                    "evidence": [_evidence_json(kb, eid) for eid in rec.evidence_ids],
                }
                for field, rec in sorted(profile.items())
            },
        }

    @app.post("/api/knowledge/{kind}/{entity_id}/resolve")
    def resolve_field_conflict(
        kind: str, entity_id: str, req: ResolveConflictRequest
    ) -> dict[str, Any]:
        """人工裁决字段的开放冲突（详情页「以此版本为准」按钮的后端入口）。

        - keep 与「当前投影」不同 → 经 ProfileWriter 补写一条同值新版本（append-only，
          证据/事件时点沿用被裁决版本），让「以此为准」落到当前投影。落版判据看投影
          而非版本链尾：合并重放可使 version 序与 knowledge_time 序错位，链尾未必是
          投影（2026-09-03 实测 valuation 裁决曾需人工补落版）；
        - 被保留值未过写侧准入闸（如门禁上线前的序列化 JSON 字符串旧形态）→ 409 明确
          报错并给出出路，不报 500；豁免通道不开——准入闸 fail-closed 对裁决入口同样成立；
        - 随后清除该字段全部竞争版本标记，落 fact/conflict_resolved（可审计）；
        - 审计事件落 kb-<kind>-<entity_id> 维护 run（非会话，sessions 列表排除 kb-*）；
        - 仅 prod 命名空间：eval 命名空间的裁决权属于回放纪律，不升人工入口（铁律 6）。
        """
        history = kb.history(kind, entity_id, req.field)
        if not history:
            raise HTTPException(status_code=404, detail=f"实体 {kind}:{entity_id} 无字段 {req.field}")
        kept = next((r for r in history if r.fact_id == req.keep_fact_id), None)
        if kept is None:
            raise HTTPException(
                status_code=404, detail=f"keep_fact_id {req.keep_fact_id} 不在 {req.field} 的版本链中"
            )
        run = RunManifest(run_id=f"kb-{kind}-{entity_id}", mode=RunMode.LIVE)
        new_fact_id: str | None = None
        current = kb.view(kind, entity_id, datetime.now(UTC)).get(req.field)
        if current is None or kept.fact_id != current.fact_id:
            try:
                new_fact_id = kb_writer.write_fact(
                    Fact(
                        entity_kind=kind,
                        entity_id=entity_id,
                        field=req.field,
                        value=kept.value,
                        event_time=kept.event_time,
                        knowledge_time=datetime.now(UTC),
                        evidence_ids=kept.evidence_ids,
                        run_id=run.run_id,
                    ),
                    run=run,
                )
            except (KnowledgeQualityError, KnowledgeInvariantError, NumericGuardError) as e:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"被保留版本的值未过写侧准入闸：{e}——"
                        "请改选其他版本，或先以合规形态（散文/结构化对象）重写该值后再裁决"
                    ),
                ) from e
        cleared = kb_writer.resolve_conflict(
            kind,
            entity_id,
            req.field,
            keep_fact_id=req.keep_fact_id,
            note=req.note or f"人工裁决：以 {req.keep_fact_id} 为准",
            run=run,
        )
        return {"resolved": req.field, "cleared": cleared, "new_fact_id": new_fact_id}

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
        # /steer 是会话级控制动作（非 pipeline command）：确定性注入运行中的 command
        if text.split(None, 1)[0].lower() == "/steer":
            parts = text.split(None, 1)
            message = parts[1].strip() if len(parts) > 1 else ""
            if not message:
                events.append(Event(run_id=run_id, type="assistant/message", payload={
                    "content": "用法：/steer <改向内容>——在 command 运行中注入新方向"
                               "（如 /steer 重点看竞对）。",
                }))
                return {"run_id": run_id, "steered": []}
            steered = (
                command_runner.steer(run_id, message)
                if command_runner is not None
                else []
            )
            if not steered:
                events.append(Event(run_id=run_id, type="assistant/message", payload={
                    "content": "当前没有正在运行的 command，/steer 仅在运行中生效（可先 /research 启动）。",
                }))
            return {"run_id": run_id, "steered": steered}
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

    @app.post("/api/sessions/{run_id}/steer")
    def steer_session(run_id: str, req: SteerRequest) -> dict[str, Any]:
        """改向注入（Stop 的对偶）：注入到运行中 command 的当前 step 子 run，
        后续 step 启动时继承；会话流落 steer/requested（UI 可见）。"""
        if command_runner is None:
            return {"steered": []}
        return {"steered": command_runner.steer(run_id, req.message, req.command_id)}

    @app.get("/api/knowledge/{kind}/{entity_id}/archives")
    def list_archives(kind: str, entity_id: str) -> list[dict[str, Any]]:
        """HTML 存档版本列表（最新在前；latest 软链副本除外）。"""
        archive_dir = knowledge_path / f"{kind}s" / entity_id / "archive"
        if not archive_dir.exists():
            return []
        files = sorted(
            (f for f in archive_dir.glob("*.html") if f.name != "latest.html"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        return [
            {
                "name": f.name,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime, tz=UTC).isoformat(),
                "is_latest": files[0] == f if files else False,
            }
            for f in files
        ]

    @app.get("/api/knowledge/{kind}/{entity_id}/archives/{name}")
    def read_archive(kind: str, entity_id: str, name: str) -> Any:
        """读取某个版本的 HTML 存档（路径穿越防护：必须落在 archive 目录内）。

        latest.html 缺失时惰性物化：存档是 KB 的纯投影（内容寻址幂等），
        读路径缺档即同步重渲染——根治「研究走了非 command 路径 → 档案无存档 →
        详情页 404」的事故（2026-09-03 验收：20/28 个实体无存档）。
        """
        from fastapi.responses import FileResponse

        from ..knowledge.render import maybe_archive

        archive_dir = (knowledge_path / f"{kind}s" / entity_id / "archive").resolve()
        if name == "latest.html" and not (archive_dir / "latest.html").is_file():
            try:
                maybe_archive(kb, knowledge_path, kind, entity_id, namespace="prod")
            except Exception:
                logger.warning("存档惰性物化失败 %s:%s", kind, entity_id, exc_info=True)
        candidate = (archive_dir / name).resolve()
        if not name.endswith(".html") or not candidate.is_relative_to(archive_dir) or not candidate.is_file():
            raise HTTPException(status_code=404, detail="archive not found")
        return FileResponse(candidate, media_type="text/html")

    @app.get("/api/reports/{child_run_id}/{name}")
    def read_report(child_run_id: str, name: str) -> Any:
        """研究报告 artifact 在线阅读（ResearchFoldCard 展开的数据源）。"""
        from fastapi.responses import PlainTextResponse

        base = reports_path.resolve()
        candidate = (base / child_run_id / name).resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file():
            raise HTTPException(status_code=404, detail="report not found")
        return PlainTextResponse(candidate.read_text(encoding="utf-8"), media_type="text/markdown")

    @app.get("/api/capabilities")
    def capabilities() -> dict[str, Any]:
        """能力目录：主 agent + 每个 command 的 step 分解（工具/插件/hook/预算/模型角色）。

        过程透明与可扩展性的入口：新增 tools/skills/MCP 后在 STEP_MANIFEST 登记即可见。
        """
        from ..commands.registry import COMMANDS
        from ..commands.steps import STEP_MANIFEST
        from ..main_agent import MAIN_AGENT_TOOL_SCHEMAS

        dynamic: dict[str, Any] = capabilities_info() if capabilities_info else {}
        return {
            "main_agent": {
                "tools": sorted(MAIN_AGENT_TOOL_SCHEMAS),
                "model": (dynamic.get("models") or {}).get("research", "未配置"),
            },
            "gateway_sources": dynamic.get("gateway_sources", []),
            "models": dynamic.get("models", {}),
            "commands": [
                {
                    "name": spec.name,
                    "summary": spec.summary,
                    "usage": spec.usage,
                    "needs_approval": spec.needs_approval,
                    "steps": [
                        {"step": s, **STEP_MANIFEST.get(s, {})} for s in spec.steps
                    ],
                }
                for spec in COMMANDS.values()
            ],
        }

    # ---------------- Providers（P5 模型自配页） ----------------

    from ..llm import provider_config as pcfg

    providers_cfg_path = Path(data_dir) / pcfg.CONFIG_FILENAME

    def _providers_payload() -> dict[str, Any]:
        raw = pcfg.load_raw(providers_cfg_path)  # JSON 损坏 → 422（见 error handler 纪律）
        source = "own" if raw is not None else "fallback"
        effective: dict[str, Any] = {}
        if router_factory is not None:
            try:
                effective = router_factory().describe()
            except Exception as e:  # 有效配置装不起来 = 如实上报（不伪装正常）
                effective = {"error": f"{type(e).__name__}: {e}"}
        return {
            "source": source,  # own=自有文件生效；fallback=pi/.env 兜底
            "config_path": str(providers_cfg_path),
            "file": pcfg.masked_view(raw) if raw is not None else None,
            "effective": effective,
        }

    @app.get("/api/providers")
    def get_providers() -> dict[str, Any]:
        """当前生效的 provider 配置视图（脱敏：明文 key 永不出 API）。"""
        try:
            return _providers_payload()
        except pcfg.ProviderConfigValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.post("/api/providers")
    def save_providers(body: dict[str, Any]) -> dict[str, Any]:
        """保存自有配置（全量替换语义）。校验先试装（fail-closed），非法配置 422 不落盘。"""
        try:
            candidate = pcfg.prepare_candidate(body, pcfg.load_raw(providers_cfg_path))
            pcfg.validate_candidate(candidate)
        except pcfg.ProviderConfigValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        pcfg.save(providers_cfg_path, candidate)
        return _providers_payload()

    @app.post("/api/providers/reset")
    def reset_providers() -> dict[str, Any]:
        """删除自有配置 → 回落 pi/.env（设计 §4.4：缺省回落为现状行为）。"""
        providers_cfg_path.unlink(missing_ok=True)
        return _providers_payload()

    @app.post("/api/providers/test")
    def test_provider(body: dict[str, Any]) -> dict[str, Any]:
        """测活：一条最小 chat completion（max_tokens=8，30s 超时）。

        body: {name?, base_url, api_key?, model}——api_key 缺省/"***" 时按 name
        从已存配置继承；env:VAR 间接引用解析后使用。key 不落日志不回显。
        """
        import time

        import httpx

        base_url = str(body.get("base_url") or "").rstrip("/")
        model = str(body.get("model") or "")
        api_key = str(body.get("api_key") or "")
        if not base_url or not model:
            raise HTTPException(status_code=422, detail="base_url 与 model 必填")
        if api_key in ("", pcfg.KEY_MASK):
            name = str(body.get("name") or "")
            stored = (pcfg.load_raw(providers_cfg_path) or {}).get("providers", {}).get(name, {})
            api_key = str(stored.get("api_key") or "")
            if not api_key:
                raise HTTPException(
                    status_code=422,
                    detail=f"provider {name!r} 无已存 key 可继承——请填明文或 env:VAR",
                )
        from ..llm.router import _read_dotenv

        env = {**_read_dotenv(), **os.environ}
        if api_key.startswith("env:"):
            api_key = env.get(api_key[4:], "")
            if not api_key:
                raise HTTPException(status_code=422, detail="env:VAR 引用的环境变量不存在")
        t0 = time.monotonic()
        try:
            resp = httpx.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model,
                      "messages": [{"role": "user", "content": "ping"}],
                      "max_tokens": 8, "stream": False},
                timeout=30.0,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code != 200:
                return {"ok": False, "latency_ms": latency_ms,
                        "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            data = resp.json()
            ok = bool((data.get("choices") or [{}])[0].get("message", {}).get("content"))
            return {"ok": ok, "latency_ms": latency_ms,
                    "error": None if ok else "200 但无 choices 内容"}
        except Exception as e:
            return {"ok": False, "latency_ms": int((time.monotonic() - t0) * 1000),
                    "error": f"{type(e).__name__}: {e}"}

    @app.get("/api/knowledge/{kind}/{entity_id}/series")
    def fact_series(
        kind: str, entity_id: str, fields: str = "", namespace: str = "prod"
    ) -> dict[str, Any]:
        """字段时序（图表数据源）：每字段的全部版本（event_time/knowledge_time/值）。"""
        out: dict[str, list[dict[str, Any]]] = {}
        for field in [f for f in fields.split(",") if f]:
            history = kb.history(kind, entity_id, field, namespace=namespace)
            out[field] = [
                {
                    "fact_id": r.fact_id,
                    "event_time": r.event_time.isoformat() if r.event_time else None,
                    "knowledge_time": r.knowledge_time.isoformat(),
                    "value": r.value,
                    "version": r.version,
                    "conflict": r.conflict_flag,
                }
                for r in history
            ]
        return {"kind": kind, "id": entity_id, "fields": out}

    @app.get("/api/knowledge/compare")
    def compare_entities(field: str, kind: str = "stock", namespace: str = "prod") -> dict[str, Any]:
        """跨实体同字段对比（竞对条形图数据源）：每实体该字段的最新版本值。"""
        rows = kb._conn.execute(  # noqa: SLF001
            "SELECT entity_id FROM facts WHERE namespace = ? AND entity_kind = ? AND field = ?"
            " GROUP BY entity_id",
            (namespace, kind, field),
        ).fetchall()
        now = datetime.now(UTC)
        items = []
        for (eid,) in rows:
            view = kb.view(kind, eid, now, namespace=namespace)
            rec = view.get(field)
            if rec is not None:
                items.append(
                    {
                        "id": eid,
                        "value": rec.value,
                        "knowledge_time": rec.knowledge_time.isoformat(),
                        "conflict": rec.conflict_flag,
                    }
                )
        return {"field": field, "kind": kind, "items": items}

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
            approvals.decide(
                approval_id,
                bool(body.get("approved")),
                comment=body.get("comment") or None,  # 打回反馈（F3 闸口回环）
            )
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

    # ---------------- /api/v2 档案与研究路由（knowledge-dossier-research-redesign §10.1） ----------------
    if metrics is not None and dossier_service is not None:
        from .dossier import create_dossier_router

        app.include_router(
            create_dossier_router(
                kb=kb,
                metrics=metrics,
                dossier=dossier_service,
                calculations=calculation_service,
                command_runner=command_runner,
                exports_dir=Path(data_dir) / "exports",
            )
        )

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
    """会话状态投影：**运行中 > 错误 > 拦停 > 取消 > 完成 > 空闲**。

    两个口径分开（旧实现把 error 做成 sticky，导致「还在跑却显示失败」）：
    - `status`：当前活动状态——有未闭合的 command/turn 就是 running，与上一条命令结果无关；
    - `last_outcome` / `status_detail`：上一条命令的结果与原因；blocked 是「研究停滞、
      管道拦停」，不是系统故障，单独表达（不并入 error）。
    """
    evs = events.read(run_id)
    title = next(
        (e.payload.get("title") for e in evs if e.type == SESSION_TITLE),
        None,
    )
    outcome: str | None = None       # 最后一条 command/done 的 outcome
    detail: str | None = None
    hard_error: str | None = None    # 真错误（outcome=error 或 */error 事件）
    blocked_detail: str | None = None
    cancelled = False
    open_turns = 0
    open_commands: set[str] = set()
    saw_done = False
    last_open_seq = 0                # 最后一个 command/run 或 turn/start 的 seq
    last_error_seq = 0               # 最后一条硬错误事件的 seq
    for e in evs:
        if e.type == TURN_START:
            open_turns += 1
            last_open_seq = max(last_open_seq, e.seq)
        elif e.type == TURN_END:
            open_turns = max(0, open_turns - 1)
        elif e.type == COMMAND_RUN:
            open_commands.add(e.payload.get("command_id", ""))
            last_open_seq = max(last_open_seq, e.seq)
        elif e.type == COMMAND_DONE:
            open_commands.discard(e.payload.get("command_id", ""))
            oc = e.payload.get("outcome")
            outcome = oc
            if oc == "completed":
                saw_done = True
                detail = e.payload.get("summary")
            elif oc == "error":
                hard_error = e.payload.get("summary")
                last_error_seq = max(last_error_seq, e.seq)
            elif oc == "blocked":
                blocked_detail = e.payload.get("summary")
            elif oc in ("cancelled", "rejected"):
                cancelled = True
        elif e.type in ("research/error", "decision/error", "turn/error"):
            hard_error = e.payload.get("reason")
            last_error_seq = max(last_error_seq, e.seq)
        elif e.type in ("research/completed", "decision/completed"):
            saw_done = True
        elif e.type == "research/cancelled":
            cancelled = True
    in_flight = open_turns > 0 or bool(open_commands)
    # 错误归属于本次未完成的活动（error 在最后一个 open 标记之后）= 这次跑挂了，
    # 不能因为「turn 没收到 turn/end」而永远显示运行中
    dead_in_flight = in_flight and bool(hard_error) and last_error_seq > last_open_seq
    if in_flight and not dead_in_flight:
        status = "running"
    elif hard_error:
        status = "error"
    elif blocked_detail:
        status = "blocked"
    elif cancelled:
        status = "cancelled"
    elif saw_done:
        status = "done"
    else:
        status = "idle"
    status_detail = (
        hard_error if status == "error"
        else blocked_detail if status == "blocked"
        else detail
    )
    return {
        "run_id": run_id,
        "title": title,
        "started_at": started_at,
        "last_active": last_active,
        "status": status,
        "status_detail": status_detail,
        "last_outcome": outcome,
        "running_commands": sorted(open_commands),
        "open_turns": open_turns,
        "last_blocked": blocked_detail,
        "last_error": hard_error,
        # 进程被杀时不会有 command/done：超过阈值仍「运行中」要诚实标可能已中断
        "possibly_stale": status == "running" and _is_stale(last_active),
    }


#: 「运行中但很久没动静」的阈值（分钟）：超过则标 possibly_stale
STALE_RUNNING_MINUTES = 30


def _is_stale(last_active: str) -> bool:
    try:
        last = datetime.fromisoformat(last_active)
    except Exception:  # noqa: BLE001 - 时间不可解析时不乱标
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (datetime.now(UTC) - last).total_seconds() > STALE_RUNNING_MINUTES * 60


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
