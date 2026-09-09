"""/api/v2 档案与研究路由（设计 §10.1）。

契约纪律：
- 读请求不隐式发起研究或金融网络抓取（命令入口除外）；
- 所有 source/metric/artifact 引用都校验实体、命名空间、截止时间与快照可见性——
  不能因为知道一个 id 就绕过过滤；
- URL 同时给 snapshot 与 as_of/entity/namespace 时以快照 manifest 为约束，
  不匹配返回 409 与正确上下文（不静默重建另一个快照）；
- 模块缺数据 = 200 + status/reasons；实体不存在 404；参数非法 422。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..commands.registry import ParsedCommand
from ..commands.runner import CommandRequest, CommandRunner
from ..dossier.service import DossierError, DossierService
from ..knowledge.metric_store import MetricStore
from ..knowledge.store import BitemporalStore
from ..research.calculations import CalculationError, CalculationService, InputRef

logger = logging.getLogger("finance_agent.api.dossier")

#: 补研请求的幂等表（单进程；重复点击返回同一 command，不启动多份全量研究）
_IDEMPOTENCY: dict[str, dict[str, Any]] = {}


class ResearchRequest(BaseModel):
    entity_kind: str = "stock"
    entity_id: str = Field(min_length=1)
    objective: str = ""
    depth: str = "standard"  # standard/deep/refresh/targeted
    focus: str = ""
    base_snapshot: str | None = None
    session_run_id: str | None = None
    idempotency_key: str | None = None


class ValuationPreviewRequest(BaseModel):
    entity_kind: str = "stock"
    entity_id: str = Field(min_length=1)
    formula_id: str = Field(min_length=1)
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: dict[str, str] = Field(default_factory=dict)
    #: 绑定基线快照（§10.1）：给出时引用必须属于该快照的冻结输入且 as_of 前可知
    base_snapshot: str | None = None


class ExportRequest(BaseModel):
    format: str = "json"  # json/markdown（HTML 在组件契约稳定后加入）
    saved_scenario_artifact_id: str | None = None


class ScenarioSaveRequest(BaseModel):
    """保存研究情景（§8.5）：滑动不写事实，只有显式保存才建立模型 artifact。"""

    base_snapshot: str = Field(min_length=1)
    model_version: str = "reverse_dcf@1"
    assumption_hash: str = Field(min_length=1)  # 必须匹配已验证计算的 input_hash
    validated_calculation_id: str = Field(min_length=1)
    name: str = ""
    idempotency_key: str | None = None


def create_dossier_router(
    *,
    kb: BitemporalStore,
    metrics: MetricStore,
    dossier: DossierService,
    calculations: CalculationService | None = None,
    command_runner: CommandRunner | None = None,
    exports_dir: Path | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v2")

    # ---------------- 档案库与快照 ----------------

    @router.get("/knowledge/entities")
    def list_entities(
        namespace: str = "prod",
        as_of: Annotated[datetime | None, Query()] = None,
        include_purged: bool = False,
    ) -> list[dict[str, Any]]:
        rows = dossier.entities(namespace=namespace, as_of=as_of)
        if include_purged:
            for row in rows:
                row["purged"] = kb.is_purged(
                    str(row.get("kind") or row.get("entity_kind") or ""),
                    str(row.get("id") or row.get("entity_id") or ""),
                    namespace=namespace,
                )
            return rows
        # 已删除（墓碑/硬删）实体不进默认列表
        return [
            row for row in rows
            if not kb.is_purged(
                str(row.get("kind") or row.get("entity_kind") or ""),
                str(row.get("id") or row.get("entity_id") or ""),
                namespace=namespace,
            )
        ]

    @router.get("/knowledge/{kind}/{entity_id}/dossier")
    def open_dossier(
        kind: str,
        entity_id: str,
        as_of: Annotated[datetime | None, Query()] = None,
        namespace: str = "prod",
        mode: str = "live",
    ) -> dict[str, Any]:
        if kind not in ("stock", "industry"):
            raise HTTPException(status_code=422, detail=f"未知实体类型 {kind!r}")
        if kb.is_purged(kind, entity_id, namespace=namespace):
            raise HTTPException(
                status_code=410,
                detail=f"{kind}:{entity_id} 已删除（墓碑），不投影档案；"
                       f"恢复调 POST /api/knowledge/{kind}/{entity_id}/restore",
            )
        if mode not in ("live", "historical", "rebuilt"):
            raise HTTPException(status_code=422, detail=f"未知模式 {mode!r}")
        if as_of is not None and mode == "live":
            # live + as_of = 打开时刻即用户指定截止（服务端仍固定为快照时刻）
            mode = "historical" if as_of < datetime.now(UTC) else mode
        try:
            payload, _created = dossier.open(
                kind, entity_id, as_of=as_of, namespace=namespace, mode=mode
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except DossierError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        return payload

    @router.get("/dossiers/{snapshot_id}")
    def get_snapshot(
        snapshot_id: str,
        as_of: Annotated[datetime | None, Query()] = None,
        namespace: str | None = None,
        entity: str | None = None,
    ) -> dict[str, Any]:
        try:
            snap = dossier.get(snapshot_id)
        except DossierError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        _assert_context_match(snap, as_of=as_of, namespace=namespace, entity=entity)
        return snap

    @router.get("/dossiers/{snapshot_id}/modules/{module}")
    def get_module(
        snapshot_id: str,
        module: str,
        metric: str = "",
        frequency: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if metric:
            params["metric"] = metric
        if frequency:
            if frequency not in ("FY", "Q", "H1", "TTM", "instant"):
                raise HTTPException(status_code=422, detail=f"未知 frequency {frequency!r}")
            params["frequency"] = frequency
        try:
            payload = dossier.module(snapshot_id, module, params=params)
        except DossierError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        return payload.model_dump(mode="json")

    @router.get("/dossiers/{snapshot_id}/evidence/{evidence_id}")
    def get_evidence(snapshot_id: str, evidence_id: str) -> dict[str, Any]:
        """仅允许该快照引用的可见摘录（知道 id 也不能绕过快照过滤）。"""
        try:
            snap = dossier.get(snapshot_id)
        except DossierError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        if evidence_id not in snap.get("evidence_refs", []):
            raise HTTPException(
                status_code=404,
                detail=f"快照 {snapshot_id} 未引用证据 {evidence_id}（不允许越快照读取）",
            )
        try:
            ev = kb.get_evidence(evidence_id)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"证据不可解析: {e}") from e
        document = None
        for ref in snap.get("document_refs", []):
            doc = metrics.get_document(ref)
            if doc is not None and doc.url == ev.url:
                document = doc.model_dump(mode="json")
                break
        return {
            "evidence_id": ev.evidence_id,
            "provider_id": ev.source_id,  # 命名三层：旧 source_id = provider 层
            "document": document,
            "url": ev.url,
            "verbatim_quote": ev.verbatim_quote,
            "available_at": ev.available_at.isoformat() if ev.available_at else None,
            "retrieved_at": ev.retrieved_at.isoformat(),
            "pit_grade": ev.pit_grade.value,
            "raw_ref": ev.raw_ref,
        }

    @router.get("/dossiers/{snapshot_id}/documents/{document_id}")
    def get_document(snapshot_id: str, document_id: str) -> dict[str, Any]:
        try:
            snap = dossier.get(snapshot_id)
        except DossierError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        if document_id not in snap.get("document_refs", []):
            raise HTTPException(
                status_code=404, detail=f"快照 {snapshot_id} 未引用文档 {document_id}"
            )
        doc = metrics.get_document(document_id)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"文档未登记: {document_id}")
        return doc.model_dump(mode="json")

    @router.get("/dossiers/{snapshot_id}/series")
    def get_series(
        snapshot_id: str,
        metric: str = "",
        frequency: str = "",
    ) -> dict[str, Any]:
        """规范化序列（同一冻结快照，review #2）：按快照输入版本集读取，
        缺期、重述与冲突显式返回，不猜数。"""
        if frequency and frequency not in ("FY", "Q", "H1", "TTM", "instant"):
            raise HTTPException(status_code=422, detail=f"未知 frequency {frequency!r}")
        try:
            return dossier.frozen_series(snapshot_id, metric=metric, frequency=frequency)
        except DossierError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e

    @router.get("/knowledge/compare")
    def compare(
        entities: str,
        metric: str,
        as_of: Annotated[datetime | None, Query()] = None,
        namespace: str = "prod",
        frequency: str = "",
        period_end: str = "",
    ) -> dict[str, Any]:
        """同一截止时点的跨实体比较（review #16）：共同期间才算可比——
        FY2024 全年与 2024Q4 单季不得返回 comparable；口径/币种不一致同样降级。"""
        t = as_of or datetime.now(UTC)
        if frequency and frequency not in ("FY", "Q", "H1", "TTM", "instant"):
            raise HTTPException(status_code=422, detail=f"未知 frequency {frequency!r}")
        items: list[dict[str, Any]] = []
        exclusions: list[dict[str, str]] = []
        for part in [p for p in entities.split(",") if p]:
            if ":" not in part:
                exclusions.append({"entity": part, "reason": "格式应为 kind:id"})
                continue
            kind, eid = part.split(":", 1)
            obs = metrics.observations_as_of(
                kind, eid, t, namespace=namespace, metric_key=metric
            )
            usable = [
                o for o in obs
                if o.status == "ok" and o.value is not None and not o.dimensions
                and o.nature in ("reported", "calculated")
            ]
            if frequency:
                usable = [o for o in usable if o.period.frequency == frequency]
            if period_end:
                usable = [o for o in usable if o.period.end.isoformat() == period_end]
            if not usable:
                reason = f"as_of 无 {metric} 的 typed 观测"
                if frequency or period_end:
                    reason += f"（限定 {frequency or '任意频率'}/{period_end or '任意期间'}）"
                exclusions.append({"entity": part, "reason": reason})
                continue
            best = max(usable, key=lambda o: (o.period.end, o.knowledge_time))
            items.append({
                "entity": part,
                "metric_key": metric,
                "value": best.value,
                "unit": best.unit,
                "currency": best.currency,
                "period_label": best.period.fiscal_label or best.period.end.isoformat(),
                "period_end": best.period.end.isoformat(),
                "frequency": best.period.frequency,
                "basis": best.basis,
                "nature": best.nature,
                "observation_id": best.observation_id,
            })
        # 口径一致性：共同期间/频率/币种/basis 全部一致才 comparable（§4.4 模块 8）
        notes = []
        currencies = {i["currency"] for i in items if i["currency"]}
        bases = {i["basis"] for i in items}
        period_ends = {i["period_end"] for i in items}
        frequencies = {i["frequency"] for i in items}
        if len(currencies) > 1:
            notes.append(f"币种不一致（{sorted(currencies)}）——需显式 FX 换算后才可比")
        if len(bases) > 1:
            notes.append(f"会计口径不一致（{sorted(bases)}）")
        if len(period_ends) > 1:
            notes.append(
                f"期间不一致（{sorted(period_ends)}）——全年与单季/不同截止日不可直接比较；"
                "可用 frequency/period_end 参数限定共同期间"
            )
        if len(frequencies) > 1:
            notes.append(f"频率不一致（{sorted(frequencies)}）")
        return {"metric": metric, "as_of": t.isoformat(), "items": items,
                "exclusions": exclusions, "notes": notes,
                "comparable": not notes and len(items) >= 2}

    # ---------------- 研究产物 / 变化 ----------------

    @router.get("/research/artifacts/{artifact_id}")
    def get_artifact(artifact_id: str) -> dict[str, Any]:
        artifact = metrics.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail=f"研究产物不存在: {artifact_id}")
        return artifact

    @router.get("/research/plans/{plan_id}")
    def get_plan(plan_id: str) -> dict[str, Any]:
        plan = metrics.get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail=f"研究计划不存在: {plan_id}")
        return plan

    @router.get("/dossiers/{snapshot_id}/changes")
    def get_changes(snapshot_id: str, baseline_snapshot_id: str) -> dict[str, Any]:
        try:
            return dossier.changes(snapshot_id, baseline_snapshot_id)
        except DossierError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e

    # ---------------- 补研入口（唯一可触发研究的 v2 端点） ----------------

    @router.post("/research/requests")
    def request_research(req: ResearchRequest) -> dict[str, Any]:
        if command_runner is None:
            raise HTTPException(status_code=503, detail="command_runner 未装配，无法发起补研")
        if req.depth not in ("standard", "deep", "refresh", "targeted"):
            raise HTTPException(status_code=422, detail=f"未知 depth {req.depth!r}")
        if req.idempotency_key and req.idempotency_key in _IDEMPOTENCY:
            return _IDEMPOTENCY[req.idempotency_key]
        if req.entity_kind not in ("stock", "industry"):
            raise HTTPException(status_code=422, detail=f"未知 entity_kind {req.entity_kind!r}")
        session = req.session_run_id or f"live-{uuid.uuid4().hex[:8]}"
        args = [f"--depth={req.depth}"]
        if req.focus:
            args.append(f"--focus={req.focus}")
        objective = req.objective or f"深度研究 {req.entity_id}"
        # 实体类型保留（review #23）：行业档案发起的补研必须仍研究行业实体，
        # 不得退化成同名股票（industry:<slug> 形态由 parse_target 识别）
        ticker = (
            f"industry:{req.entity_id.lower()}" if req.entity_kind == "industry"
            else req.entity_id.upper()
        )
        raw = f"/research {ticker} {' '.join(args)} {objective}".strip()
        parsed = ParsedCommand(
            name="research", raw_input=raw, ticker=ticker,
            objective=objective,
            extra={"depth": req.depth, "focus": req.focus,
                   "base_snapshot": req.base_snapshot or ""},
        )
        command_id = command_runner.start(CommandRequest(session_run_id=session, parsed=parsed))
        result = {
            "session_run_id": session,
            "command_id": command_id,
            "depth": req.depth,
            "status": "started",
            "idempotency_key": req.idempotency_key,
        }
        if req.idempotency_key:
            _IDEMPOTENCY[req.idempotency_key] = result
        return result

    # ---------------- 估值预览（只计算，不写事实） ----------------

    @router.post("/valuations/preview")
    def valuation_preview(req: ValuationPreviewRequest) -> dict[str, Any]:
        if calculations is None:
            raise HTTPException(status_code=503, detail="计算服务未装配")
        try:
            inputs = [InputRef.model_validate(i) for i in req.inputs]
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"inputs 非法: {e}") from e
        as_of = None
        namespace = "prod"
        if req.base_snapshot:
            # 绑定基线快照（review #4）：引用必须属于快照冻结输入且 as_of 前可知
            try:
                snap = dossier.get(req.base_snapshot)
            except DossierError as e:
                raise HTTPException(status_code=e.status, detail=str(e)) from e
            if (snap["entity"]["kind"], snap["entity"]["id"]) != (req.entity_kind, req.entity_id):
                raise HTTPException(
                    status_code=409,
                    detail=f"基线快照属于 {snap['entity']['kind']}:{snap['entity']['id']}，"
                           f"与请求实体 {req.entity_kind}:{req.entity_id} 不符",
                )
            namespace = snap["context"]["namespace"]
            as_of = datetime.fromisoformat(snap["context"]["as_of"])
            frozen_obs = set((snap.get("inputs") or {}).get("observation_ids") or [])
            for ref in inputs:
                if ref.kind == "observation" and ref.ref_id and frozen_obs \
                        and ref.ref_id not in frozen_obs:
                    raise HTTPException(
                        status_code=409,
                        detail=f"引用 {ref.ref_id} 不属于基线快照的冻结输入"
                               "（计算必须基于同一可见世界）",
                    )
        try:
            result = calculations.calculate(
                entity_kind=req.entity_kind, entity_id=req.entity_id,
                formula_id=req.formula_id, inputs=inputs,
                assumptions=req.assumptions, run_id="valuation-preview",
                namespace=namespace, as_of=as_of,
            )
        except CalculationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        payload = result.to_payload()
        payload["note"] = "预览计算：不写事实、不创建 DecisionCard（§8.4/§8.5）"
        return payload

    # ---------------- 情景保存（模型 artifact；不创建事实或 DecisionCard，§8.4/§8.5） ----------------

    _SCENARIO_IDEM: dict[str, dict[str, Any]] = {}

    @router.post("/valuations/scenarios")
    def save_scenario(req: ScenarioSaveRequest) -> dict[str, Any]:
        if req.idempotency_key and req.idempotency_key in _SCENARIO_IDEM:
            return _SCENARIO_IDEM[req.idempotency_key]
        # 基线快照必须存在且与计算同实体同命名空间（review #25）
        try:
            snap = dossier.get(req.base_snapshot)
        except DossierError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        calc = metrics.get_calculation(req.validated_calculation_id)
        if calc is None:
            raise HTTPException(status_code=404,
                                detail=f"计算不存在: {req.validated_calculation_id}")
        if (calc.entity_kind, calc.entity_id) != (snap["entity"]["kind"], snap["entity"]["id"]):
            raise HTTPException(
                status_code=409,
                detail=f"计算属于 {calc.entity_kind}:{calc.entity_id}，"
                       f"与基线快照实体 {snap['entity']['kind']}:{snap['entity']['id']} 不符",
            )
        if calc.namespace != snap["context"]["namespace"]:
            raise HTTPException(
                status_code=409,
                detail=f"计算命名空间 {calc.namespace!r} 与基线快照 "
                       f"{snap['context']['namespace']!r} 不符（跨命名空间保存拒绝）",
            )
        # 模型版本必须与计算一致（容忍 @1 / @v1 两种写法，不容忍任意版本）
        expected_version = f"{calc.formula_id}@v{calc.formula_version}"
        norm = lambda s: s.replace("@v", "@")  # noqa: E731
        if norm(req.model_version) != norm(expected_version):
            raise HTTPException(
                status_code=422,
                detail=f"model_version 必须与计算一致：期望 {expected_version}，"
                       f"收到 {req.model_version!r}（不得声明任意模型版本）",
            )
        if calc.input_hash != req.assumption_hash:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "assumption_hash 与计算输入不匹配（假设已变，需重算后再保存）",
                    "expected": calc.input_hash, "got": req.assumption_hash,
                },
            )
        if calc.status != "ok":
            raise HTTPException(status_code=422,
                                detail=f"计算状态 {calc.status} 不可保存为情景（失败不产出貌似有效的结果）")
        # 计算输入必须属于基线快照的冻结世界（review #25：不得用快照外观测支撑情景）
        frozen_obs = set((snap.get("inputs") or {}).get("observation_ids") or [])
        if frozen_obs:
            for ref in calc.payload.get("input_refs", []):
                if ref.get("kind") == "observation" and ref.get("ref_id") \
                        and ref["ref_id"] not in frozen_obs:
                    raise HTTPException(
                        status_code=409,
                        detail=f"计算输入 {ref['ref_id']} 不属于基线快照的冻结输入",
                    )
        from datetime import UTC
        from datetime import datetime as _dt

        from ..research.artifacts import (
            AssumptionTableBlock,
            HeadingBlock,
            ParagraphBlock,
            ReportDocument,
            ResearchArtifact,
        )

        now = _dt.now(UTC)
        name = req.name or f"情景 {calc.formula_id} {now.strftime('%m-%d %H:%M')}"
        doc = ReportDocument(
            title=name,
            entity_kind=calc.entity_kind,  # type: ignore[arg-type]
            entity_id=calc.entity_id,
            blocks=[
                HeadingBlock(level=1, text="研究假设情景（用户保存）"),
                ParagraphBlock(text=(
                    f"本情景是研究假设产物（model_estimate），不是披露事实或投资建议；"
                    f"基线快照 {req.base_snapshot}，模型 {req.model_version}。"
                )),
                AssumptionTableBlock(
                    title=f"{calc.formula_id}@v{calc.formula_version} 假设与结果",
                    assumptions={**calc.payload.get("assumptions", {}),
                                 "result": calc.result or "—",
                                 "input_hash": calc.input_hash},
                    calculation_ref=calc.calculation_id,
                ),
            ],
            limitations=["情景不自动成为默认发布版；导出时显示生成日期与假设变化"],
        ).with_id()
        artifact = ResearchArtifact(
            entity_kind=calc.entity_kind,  # type: ignore[arg-type]
            entity_id=calc.entity_id,
            title=name,
            report_document=doc,
            calculation_ids=[calc.calculation_id],
            snapshot_refs=[req.base_snapshot],
            status="validated",  # 引用完整性已验（计算存在、hash 匹配、同实体同命名空间）
            sufficiency="partial",  # 情景不是完整研究
            purpose="scenario",  # review #26：不进默认发布投影/首屏结论
            created_at=now,
            evidence_cutoff=now,
        ).with_id()
        # 保存前渲染可读正文（review #27：产物页只渲染 markdown，不得存空壳）
        from ..research.artifacts import render_markdown

        artifact.markdown = render_markdown(artifact, store=metrics, kb=kb)
        metrics.save_artifact(artifact_id=artifact.artifact_id, namespace=calc.namespace,
                              payload=artifact.model_dump(mode="json"))
        result = {
            "artifact_id": artifact.artifact_id,
            "name": name,
            "base_snapshot": req.base_snapshot,
            "calculation_id": calc.calculation_id,
            "assumption_hash": req.assumption_hash,
            "note": "已保存为模型 artifact；未创建事实或 DecisionCard（D1 边界）",
        }
        if req.idempotency_key:
            _SCENARIO_IDEM[req.idempotency_key] = result
        return result

    @router.get("/valuations/scenarios/{artifact_id}")
    def get_scenario(artifact_id: str) -> dict[str, Any]:
        """恢复已保存情景：假设、计算与基线（检查当前上下文可见性）。"""
        artifact = metrics.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail=f"情景不存在: {artifact_id}")
        refs = artifact.get("snapshot_refs") or []
        for sid in refs:
            if metrics.get_snapshot(sid) is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"情景绑定的基线快照不可见: {sid}（命名空间/截止时间不匹配或已不可恢复）",
                )
        return artifact

    # ---------------- 冻结导出 ----------------

    @router.post("/dossiers/{snapshot_id}/exports")
    def export_dossier(snapshot_id: str, req: ExportRequest) -> dict[str, Any]:
        try:
            name, content = dossier.export(snapshot_id, req.format)
        except DossierError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        if req.saved_scenario_artifact_id:
            # 导出用户情景：情景必须匹配基线，并注明与发布版差异（§10.1）
            scen = metrics.get_artifact(req.saved_scenario_artifact_id)
            if scen is None:
                raise HTTPException(status_code=404,
                                    detail=f"情景不存在: {req.saved_scenario_artifact_id}")
            if snapshot_id not in (scen.get("snapshot_refs") or []):
                raise HTTPException(
                    status_code=409,
                    detail=f"情景绑定的基线不是 {snapshot_id}（不能把其他快照的情景附加到本导出）",
                )
            header = (
                f"\n\n---\n## 附加用户情景（非发布版）\n"
                f"- 情景: {scen.get('title')}（{req.saved_scenario_artifact_id}）\n"
                f"- 生成日期: {scen.get('created_at')} · 基线: {snapshot_id}\n"
                f"- 与发布版差异: 本情景是用户假设产物，不改变发布快照的任何结论\n"
            )
            if req.format == "json":
                data = json.loads(content)
                data["saved_scenario"] = {
                    "artifact_id": req.saved_scenario_artifact_id,
                    "title": scen.get("title"),
                    "created_at": scen.get("created_at"),
                    "note": "用户情景，非发布版",
                }
                content = json.dumps(data, ensure_ascii=False, indent=2)
            else:
                content += header
        job_id = f"job-{uuid.uuid4().hex[:10]}"
        base = exports_dir or Path("data/exports")
        job_dir = base / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / name).write_text(content, encoding="utf-8")
        manifest = {
            "job_id": job_id, "status": "completed", "snapshot_id": snapshot_id,
            "format": req.format, "artifact_name": name,
            "created_at": datetime.now(UTC).isoformat(),
            "saved_scenario_artifact_id": req.saved_scenario_artifact_id,
        }
        (job_dir / "job.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return manifest

    @router.get("/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        base = exports_dir or Path("data/exports")
        manifest_path = base / job_id / "job.json"
        if not manifest_path.is_file():
            raise HTTPException(status_code=404, detail=f"job 不存在: {job_id}")
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    @router.get("/jobs/{job_id}/artifact")
    def job_artifact(job_id: str) -> Any:
        from fastapi.responses import PlainTextResponse

        base = (exports_dir or Path("data/exports")).resolve()
        job_dir = (base / job_id).resolve()
        manifest_path = job_dir / "job.json"
        if not manifest_path.is_file() or not job_dir.is_relative_to(base):
            raise HTTPException(status_code=404, detail=f"job 不存在: {job_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        candidate = (job_dir / manifest["artifact_name"]).resolve()
        if not candidate.is_file() or not candidate.is_relative_to(job_dir):
            raise HTTPException(status_code=404, detail="工件缺失")
        media = "application/json" if manifest["format"] == "json" else "text/markdown"
        return PlainTextResponse(
            candidate.read_text(encoding="utf-8"), media_type=f"{media}; charset=utf-8"
        )

    return router


def _assert_context_match(
    snap: dict[str, Any], *, as_of: datetime | None, namespace: str | None, entity: str | None
) -> None:
    """URL 参数与快照 manifest 不一致 → 409 + 正确上下文（不静默重建）。"""
    ctx = snap["context"]
    ent = snap["entity"]
    problems = []
    if namespace and namespace != ctx["namespace"]:
        problems.append(f"namespace 应为 {ctx['namespace']}")
    if entity and entity != f"{ent['kind']}:{ent['id']}":
        problems.append(f"entity 应为 {ent['kind']}:{ent['id']}")
    if as_of and as_of.isoformat() != ctx["as_of"]:
        problems.append(f"as_of 应为 {ctx['as_of']}（快照已冻结上下文）")
    if problems:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "URL 参数与快照上下文不匹配",
                "problems": problems,
                "snapshot_context": ctx,
            },
        )
