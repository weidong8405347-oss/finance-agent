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

from ..eventstore.events import Event
from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest
from ..knowledge.errors import KnowledgeError
from ..knowledge.models import Fact
from ..knowledge.normalize import normalize_entity_id
from ..knowledge.store import BitemporalStore
from ..knowledge.verify import STRUCTURED_LIST_FIELDS
from ..knowledge.writer import ProfileWriter
from .calc import CALC_TOOL_SCHEMA, calc_tool
from .evidence_desk import ChunkStore, EvidenceVerificationError, verify_and_build

#: 抓取函数的签名：filing URL → 纯文本正文（HTML 已剥离）
FetchDocument = Callable[[str], str]

_WS = re.compile(r"\s+")

#: 结构化字段校验注册表的唯一来源是 knowledge.verify.STRUCTURED_LIST_FIELDS
#: （2026-09-01 实测：模型把 player_landscape 写成 JSON 字符串，下游 F3 读到
#: 1491 个字符——类型不校验的静默腐化）。本层保留早期友好报错，硬门禁在 writer。
_STRUCTURED_LIST_FIELDS = STRUCTURED_LIST_FIELDS


def _ref_resolvable(
    kb: BitemporalStore, metrics: Any, ref: str, *,
    namespace: str = "prod", entity_kind: str | None = None, entity_id: str | None = None,
) -> bool:
    """引用可解析性（integrity 门禁的最小单元）：存在性 + 命名空间 + 实体上下文
    （review #5：其他命名空间/其他实体的引用不得支撑 validated 结论）。"""
    from .artifacts import ref_resolvable

    return ref_resolvable(
        kb, metrics, ref, namespace=namespace, entity_kind=entity_kind, entity_id=entity_id
    )


class _Tracker:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.rejected: list[dict] = []
        self.registered: list[str] = []  # 已登记证据 id（囤证据检测：登记多而写入少 = 空转）
        # 研究升级（§7.7）：进展不仅记录 facts_written，还记录观测/论断/计算/问题，
        # 防止「有价值分析但没写字段」被误判 stalled
        self.observations: list[str] = []
        self.claims: list[str] = []
        self.calculations: list[str] = []
        self.questions_advanced: list[str] = []

    @property
    def any_progress(self) -> bool:
        return bool(
            self.written or self.observations or self.claims
            or self.calculations or self.questions_advanced
        )


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
    events: EventStore | None = None,
    metrics: Any | None = None,  # MetricStore（typed 观测/论断/计算；缺省 = 新工具不注入）
    metric_writer: Any | None = None,  # TypedMetricWriter
    calculations: Any | None = None,  # CalculationService
    plan_id: str | None = None,  # 冻结的研究计划（answer_question 的落点）
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
        tracker.registered.append(ev.evidence_id)
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
        # 实体 ID 归一（写入口纵深防御；writer 层还有兜底）：同标的只允许一个档案
        canonical_id = normalize_entity_id(entity_kind, entity_id)
        # 结构化字段类型校验（写侧 fail-loud，防 JSON 字符串腐化下游）
        if field in _STRUCTURED_LIST_FIELDS:
            required_keys = _STRUCTURED_LIST_FIELDS[field]
            value = args.get("value")
            if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
                return {"content": f"rejected: 字段 {field} 的值必须是 list[object]（收到 "
                                   f"{type(value).__name__}）；逐条给对象，不要拼 JSON 字符串",
                        "provenance": []}
            bad = [i for i, v in enumerate(value)
                   if any(k not in v for k in required_keys)]
            if bad:
                return {"content": f"rejected: 字段 {field} 的第 {bad} 条缺必备键 {required_keys}",
                        "provenance": []}
        try:
            evidences = [store.get_evidence(e) for e in evidence_ids]
            # knowledge_time 推导：所绑证据的最晚可知时刻；全无（C 级）则取当前（生产模式）
            known = [e.available_at for e in evidences if e.available_at is not None]
            knowledge_time = max(known) if known else datetime.now(UTC)
            fact = Fact(
                entity_kind=entity_kind,
                entity_id=canonical_id,
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

    def resolve_conflict(args: dict[str, Any]) -> dict[str, Any]:
        """裁决字段冲突（Q2）：采集到更强证据后调用，清除该字段的竞争版本标记。"""
        field = str(args["field"])
        keep = str(args["keep_evidence_id"])
        n = writer.resolve_conflict(
            entity_kind, entity_id, field,
            keep_fact_id="",  # 以证据为准的裁决（keep_fact_id 仅作审计记录）
            note=str(args.get("note") or f"以证据 {keep} 为准"),
            run=manifest,
            namespace=namespace,
        )
        return {"content": json.dumps({"resolved": field, "cleared": n}, ensure_ascii=False),
                "provenance": []}

    tools: dict[str, Any] = {
        "register_evidence": register_evidence,
        "propose_fact": propose_fact,
        "query_kb": query_kb,
        "resolve_conflict": resolve_conflict,
        "calc": calc_tool,  # §4.7：数字保护双保险（逐字 + 计算一致性）
    }

    # ---------------- typed 工具（档案升级 §6.2/§7.3/§8.1；未装配新存储则不注入） ----------------
    if metrics is not None and metric_writer is not None:
        from ..knowledge.metrics import (
            ConsensusObservation,
            GuidanceObservation,
            MetricPeriod,
            RawValue,
            ReportedObservation,
        )
        from ..knowledge.normalization import NormalizationStep, normalize_raw
        from .artifacts import ResearchClaim

        def propose_metric(args: dict[str, Any]) -> dict[str, Any]:
            """typed 观测写入：原文值文本 + 显式换算 → 标准化十进制值。

            数字保护升级：value_text 的数字必须在所绑证据摘录中逐字出现（同旧 guard）；
            换算链服务端重算（不信任模型自报的最终值）；knowledge_time 由证据推导。
            """
            from ..knowledge.guard import _numbers

            metric_key = str(args.get("metric_key") or "")
            value_text = str(args.get("value_text") or "")
            evidence_ids = [str(e) for e in (args.get("evidence_ids") or [])]
            if not metric_key or not value_text or not evidence_ids:
                return {"content": "rejected: metric_key/value_text/evidence_ids 均必填", "provenance": []}
            try:
                evidences = [store.get_evidence(e) for e in evidence_ids]
            except Exception as e:
                tracker.rejected.append({"metric": metric_key, "reason": str(e)})
                return {"content": f"rejected: {e}", "provenance": []}
            # 数字逐字保护：value_text 中的数字必须出现在摘录里（防自编自引）
            quote_numbers = {n for ev in evidences for n in _numbers(ev.verbatim_quote)}
            for n in _numbers(value_text):
                if not any(abs(n - q) <= max(abs(q) * 1e-9, 1e-12) for q in quote_numbers):
                    tracker.rejected.append(
                        {"metric": metric_key, "reason": f"值文本数字 {n} 未在摘录中逐字出现"}
                    )
                    return {
                        "content": f"rejected: 值文本中的数字 {n} 未在任何证据摘录中逐字出现",
                        "provenance": [],
                    }
            known = [e.available_at for e in evidences if e.available_at is not None]
            knowledge_time = max(known) if known else datetime.now(UTC)
            grades = [e.pit_grade.value for e in evidences]
            pit = "A" if "A" in grades else ("B" if "B" in grades else "C")
            period_args = args.get("period") or {}
            try:
                period = MetricPeriod.model_validate({
                    "start": period_args.get("start"),
                    "end": period_args.get("end"),
                    "frequency": period_args.get("frequency", "FY"),
                    "fiscal_label": period_args.get("fiscal_label", ""),
                })
            except Exception as e:
                tracker.rejected.append({"metric": metric_key, "reason": f"period 非法: {e}"})
                return {"content": f"rejected: period 非法: {e}", "provenance": []}
            try:
                extra_steps = [
                    NormalizationStep.model_validate(s)
                    for s in (args.get("normalization") or [])
                ]
                value, steps = normalize_raw(
                    value_text, str(args.get("unit_text") or args.get("unit") or ""),
                    extra_steps=extra_steps,
                )
            except Exception as e:
                tracker.rejected.append({"metric": metric_key, "reason": f"标准化失败: {e}"})
                return {"content": f"rejected: 标准化失败: {e}", "provenance": []}
            common: dict[str, Any] = {
                "entity_kind": entity_kind, "entity_id": normalize_entity_id(entity_kind, entity_id),
                "metric_key": metric_key, "period": period,
                "dimensions": {str(k): str(v) for k, v in (args.get("dimensions") or {}).items()},
                "basis": args.get("basis", "GAAP"),
                "value": value,
                "unit": str(args.get("unit") or args.get("unit_text") or ""),
                "currency": args.get("currency"),
                "raw": RawValue(value_text=value_text, unit_text=str(args.get("unit_text") or ""),
                                quote_ref=evidence_ids[0]),
                "normalization": [s.model_dump(mode="json") for s in steps],
                "evidence_refs": evidence_ids,
                "document_refs": [str(d) for d in (args.get("document_refs") or [])],
                "knowledge_time": knowledge_time,
                "source_available_at": max(known) if known else None,
                "retrieved_at": datetime.now(UTC),
                "created_at": datetime.now(UTC),
                "pit_grade": pit,
                "run_id": manifest.run_id,
            }
            nature = str(args.get("nature") or "reported")
            try:
                if nature == "reported":
                    obs: Any = ReportedObservation(**common)
                elif nature == "guidance":
                    g = args.get("guidance") or {}
                    tp = g.get("target_period") or {}
                    obs = GuidanceObservation(
                        **common, issuer=str(g.get("issuer") or ""),
                        guidance_published_at=datetime.fromisoformat(
                            str(g.get("published_at") or knowledge_time.isoformat())
                        ),
                        target_period=MetricPeriod.model_validate(tp),
                    )
                elif nature == "consensus":
                    c = args.get("consensus") or {}
                    obs = ConsensusObservation(
                        **common, vendor=str(c.get("vendor") or ""),
                        consensus_snapshot_at=datetime.fromisoformat(
                            str(c.get("snapshot_at") or knowledge_time.isoformat())
                        ),
                    )
                else:
                    return {
                        "content": f"rejected: 工具只接受 reported/guidance/consensus（收到 {nature}）；"
                                   "calculated 用 calculate_metric，model_estimate 属于冻结产物",
                        "provenance": [],
                    }
            except Exception as e:
                tracker.rejected.append({"metric": metric_key, "reason": f"模型校验失败: {e}"})
                return {"content": f"rejected: {type(e).__name__}: {e}", "provenance": []}
            try:
                observation_id, created = metric_writer.write_observation(
                    obs, run=manifest, namespace=namespace
                )
            except Exception as e:
                tracker.rejected.append({"metric": metric_key, "reason": str(e)})
                return {"content": f"rejected: {e}", "provenance": []}
            tracker.observations.append(metric_key)
            return {
                "content": json.dumps({
                    "observation_id": observation_id, "metric_key": metric_key,
                    "value": value, "created": created,
                }, ensure_ascii=False),
                "provenance": [
                    {"source_id": e.source_id,
                     "available_at": e.available_at.isoformat() if e.available_at else None,
                     "pit_grade": e.pit_grade.value}
                    for e in evidences
                ],
            }

        def propose_claim(args: dict[str, Any]) -> dict[str, Any]:
            """研究论断（分析层）：支持/反方引用可解析 → validated，否则 draft 留痕。"""
            from ..eventstore.events import RESEARCH_CLAIM_VALIDATED

            statement = str(args.get("statement") or "").strip()
            support = [str(r) for r in (args.get("support_refs") or [])]
            counter = [str(r) for r in (args.get("counter_refs") or [])]
            if len(statement) < 4:
                return {"content": "rejected: statement 过短（至少 4 字符）", "provenance": []}
            kind = str(args.get("kind") or "inference")
            if kind not in ("fact_summary", "inference", "hypothesis", "analysis"):
                return {"content": f"rejected: 未知 kind {kind!r}", "provenance": []}
            # 支持与反方引用都验（存在性+命名空间+实体，review #5）
            canonical_id = normalize_entity_id(entity_kind, entity_id)
            ctx = {"namespace": namespace, "entity_kind": entity_kind, "entity_id": canonical_id}
            unresolved_refs = [r for r in support if not _ref_resolvable(store, metrics, r, **ctx)]
            unresolved_counter = [r for r in counter if not _ref_resolvable(store, metrics, r, **ctx)]
            status = (
                "validated"
                if support and not unresolved_refs and not unresolved_counter
                else "draft"
            )
            try:
                claim = ResearchClaim(
                    entity_kind=entity_kind,  # type: ignore[arg-type]
                    entity_id=normalize_entity_id(entity_kind, entity_id),
                    statement=statement, kind=kind,  # type: ignore[arg-type]
                    question_id=args.get("question_id"),
                    support_refs=support, counter_refs=counter,
                    limitations=[str(x) for x in (args.get("limitations") or [])],
                    status=status,  # type: ignore[arg-type]
                    evidence_cutoff=datetime.now(UTC),
                    run_id=manifest.run_id, namespace=namespace,
                ).with_id()
            except Exception as e:
                tracker.rejected.append({"claim": statement[:40], "reason": str(e)})
                return {"content": f"rejected: {e}", "provenance": []}
            metrics.save_claim(
                claim_id=claim.claim_id, namespace=namespace,
                payload=claim.model_dump(mode="json"),
            )
            if status == "validated" and events is not None:
                events.append(Event(run_id=manifest.run_id, type=RESEARCH_CLAIM_VALIDATED, payload={
                    "claim_id": claim.claim_id, "entity": f"{entity_kind}:{entity_id}",
                    "checks": ["support_refs_resolvable"],
                }))
            tracker.claims.append(claim.claim_id)
            note = ""
            if status != "validated":
                if unresolved_refs or unresolved_counter:
                    note = (
                        f"（draft：引用不可解析/跨上下文 support={unresolved_refs} "
                        f"counter={unresolved_counter}）"
                    )
                elif not support:
                    note = "（draft：无支持引用）"
            return {"content": json.dumps({"claim_id": claim.claim_id, "status": status, "note": note},
                                           ensure_ascii=False), "provenance": []}

        def answer_question(args: dict[str, Any]) -> dict[str, Any]:
            """更新冻结计划中问题的状态（§7.3 状态机 + §7.6 门禁）。"""
            from ..eventstore.events import RESEARCH_QUESTION_UPDATED

            if not plan_id:
                return {"content": "rejected: 本轮无冻结研究计划（plan 未装配）", "provenance": []}
            plan_payload = metrics.get_plan(plan_id)
            if plan_payload is None:
                return {"content": f"rejected: 计划不存在: {plan_id}", "provenance": []}
            qid = str(args.get("question_id") or "")
            question = next(
                (q for q in plan_payload.get("questions", []) if q.get("question_id") == qid), None
            )
            if question is None:
                return {
                    "content": f"rejected: 问题不在冻结计划中: {qid}（计划范围不可扩展）",
                    "provenance": [],
                }
            del question  # 门禁只用计划判存在性；更新走存储层原子入口（review #8）
            status = str(args.get("status") or "")
            if status not in ("gathering", "answered", "disputed", "unavailable", "not_applicable"):
                return {"content": f"rejected: 未知状态 {status!r}", "provenance": []}
            conclusion = str(args.get("conclusion") or "").strip()
            support = [str(r) for r in (args.get("support_refs") or [])]
            counter = [str(r) for r in (args.get("counter_refs") or [])]
            unresolved = [str(r) for r in (args.get("unresolved") or [])]
            attempts = [str(r) for r in (args.get("attempts") or [])]
            # 门禁：answered 需结论+引用（带上下文）；disputed/unavailable 需原因与尝试（硬规则）
            canonical_id = normalize_entity_id(entity_kind, entity_id)
            ctx = {"namespace": namespace, "entity_kind": entity_kind, "entity_id": canonical_id}
            if status == "answered":
                if not conclusion:
                    return {"content": "rejected: answered 必须给出 conclusion", "provenance": []}
                if not support:
                    return {
                        "content": "rejected: answered 必须给出 support_refs（证据/观测/计算/论断 id）",
                        "provenance": [],
                    }
                bad = [r for r in support if not _ref_resolvable(store, metrics, r, **ctx)]
                if bad:
                    return {
                        "content": f"rejected: 支持引用不可解析或跨上下文: {bad}",
                        "provenance": [],
                    }
            if counter:
                bad_counter = [r for r in counter if not _ref_resolvable(store, metrics, r, **ctx)]
                if bad_counter:
                    return {
                        "content": f"rejected: 反方引用不可解析或跨上下文: {bad_counter}",
                        "provenance": [],
                    }
            if status in ("disputed", "unavailable") and not (unresolved or attempts):
                return {
                    "content": f"rejected: {status} 必须记录原因（unresolved）与尝试（attempts）",
                    "provenance": [],
                }
            # 原子更新单问题（review #8）：锁内读-改-写，并行 worker 不互盖
            updated = metrics.update_plan_question(plan_id, qid, {
                "status": status,
                "conclusion": conclusion,
                "support_refs": support,
                "counter_refs": counter,
                "unresolved": unresolved,
                "attempts": attempts,
            }, namespace=namespace)
            if updated is None:
                return {
                    "content": f"rejected: 计划/问题在更新窗口内消失: {plan_id}/{qid}",
                    "provenance": [],
                }
            if events is not None:
                events.append(Event(run_id=manifest.run_id, type=RESEARCH_QUESTION_UPDATED, payload={
                    "plan_id": plan_id, "question_id": qid, "status": status,
                    "conclusion": conclusion or None,
                    "entity": f"{entity_kind}:{entity_id}",
                }))
            tracker.questions_advanced.append(qid)
            return {"content": json.dumps({"question_id": qid, "status": status}, ensure_ascii=False),
                    "provenance": []}

        tools.update({
            "propose_metric": propose_metric,
            "propose_claim": propose_claim,
            "answer_question": answer_question,
        })

    if metrics is not None and calculations is not None:
        from .calculations import CalculationError, InputRef

        def calculate_metric(args: dict[str, Any]) -> dict[str, Any]:
            """受控公式计算（input_refs 而非裸数字，§8.1）。"""
            try:
                inputs = [InputRef.model_validate(i) for i in (args.get("inputs") or [])]
            except Exception as e:
                return {"content": f"rejected: inputs 非法: {e}", "provenance": []}
            try:
                result = calculations.calculate(
                    entity_kind=entity_kind,
                    entity_id=normalize_entity_id(entity_kind, entity_id),
                    formula_id=str(args.get("formula_id") or ""),
                    inputs=inputs,
                    assumptions={str(k): str(v) for k, v in (args.get("assumptions") or {}).items()},
                    run_id=manifest.run_id, namespace=namespace,
                )
            except CalculationError as e:
                tracker.rejected.append({"formula": args.get("formula_id"), "reason": str(e)})
                return {"content": f"rejected: {e}", "provenance": []}
            tracker.calculations.append(result.calculation_id)
            out = result.to_payload()
            return {"content": json.dumps({
                "calculation_id": out["calculation_id"], "status": out["status"],
                "result": out["result"], "unit": out["unit"],
                "warnings": out["warnings"], "error": out["error"],
                "extra": out.get("extra", {}),
            }, ensure_ascii=False), "provenance": []}

        tools["calculate_metric"] = calculate_metric

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
    "calc": CALC_TOOL_SCHEMA,
    # F2/F3 漏斗专用工具（在 step 内动态注入 kernel；schema 登记在此供路由层绑定，
    # 否则模型看到空 schema 会以空参调用——2026-09-01 实测 submit_card({}) 空提事故）
    "propose_candidates": {
        "name": "propose_candidates",
        "description": "提交一批赛道候选标的（每只必须绑 ≥1 条已登记证据，否则拒收）",
        "parameters": {
            "type": "object",
            "properties": {
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string"},
                            "name": {"type": "string"},
                            "market": {"type": "string", "description": "US/HK/CN"},
                            "sub_sector": {"type": "string"},
                            "one_liner": {"type": "string"},
                            "listed": {"type": "boolean"},
                            "evidence_ids": {"type": "array", "items": {"type": "string"},
                                             "minItems": 1},
                        },
                        "required": ["ticker", "evidence_ids"],
                    },
                }
            },
            "required": ["candidates"],
        },
    },
    "submit_card": {
        "name": "submit_card",
        "description": "提交一只标的的粗调研卡（F3 闸口筛选用；全部字段必填）",
        "parameters": {
            "type": "object",
            "properties": {
                "one_liner": {"type": "string", "description": "业务一句话"},
                "key_metrics": {"type": "object", "description": "市值/收入/现金等（查不到标未知）"},
                "highlights": {"type": "array", "items": {"type": "string"}},
                "risks": {"type": "array", "items": {"type": "string"}},
                "richness": {"type": "string", "enum": ["A", "B", "C"],
                              "description": "信息丰富度评级"},
                "recommend": {"type": "boolean", "description": "是否推荐进入深研"},
                "reason": {"type": "string", "description": "推荐/淘汰的一句话理由"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["one_liner", "key_metrics", "recommend", "reason", "evidence_ids"],
        },
    },
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
    "resolve_conflict": {
        "name": "resolve_conflict",
        "description": "裁决字段的开放冲突：采集到更强证据后调用，清除该字段的竞争版本标记",
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string"},
                "keep_evidence_id": {"type": "string", "description": "以哪条证据为准"},
                "note": {"type": "string", "description": "裁决理由"},
            },
            "required": ["field", "keep_evidence_id"],
        },
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
    # read_evidence 在多个 step 内动态注入（synthesize/committee/rank_report）；
    # 缺 schema 时路由层回退空参 schema，模型会以 read_evidence({}) 空转——
    # 2026-09-02 P4 验收实测：委员会 financial 视角 8 步全烧在空参调用上
    "read_evidence": {
        "name": "read_evidence",
        "description": "读取一条已登记证据的原文（verbatim_quote）与来源，用于核对事实锚点",
        "parameters": {
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string", "description": "证据 id（ev- 前缀）"},
            },
            "required": ["evidence_id"],
        },
    },
    # ---- typed 工具（档案升级；未装配 MetricStore 时路由层不会绑定） ----
    "propose_metric": {
        "name": "propose_metric",
        "description": (
            "登记一条 typed 指标观测（结构化数值，图表/计算只消费这里）。"
            "value_text 必须是证据摘录中逐字出现的原文值（如 '1.2 billion'）；"
            "服务端自动登记规模词换算并逐步重算；期间/口径/维度必填。"
            "nature=guidance 时额外给 guidance={issuer,published_at,target_period}；"
            "nature=consensus 时给 consensus={vendor,snapshot_at}。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "metric_key": {"type": "string", "description": "语义键：revenue/capex/firm_backlog/arr..."},
                "value_text": {"type": "string", "description": "原文值文本（逐字，含规模词）"},
                "unit": {"type": "string", "description": "目标单位（USD/units/ratio...）"},
                "unit_text": {"type": "string", "description": "原文单位文本（如 'USD millions'）"},
                "currency": {"type": "string"},
                "period": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string", "description": "YYYY-MM-DD（instant 可省）"},
                        "end": {"type": "string", "description": "YYYY-MM-DD"},
                        "frequency": {"type": "string", "enum": ["FY", "Q", "H1", "TTM", "instant"]},
                        "fiscal_label": {"type": "string", "description": "如 FY2025/2025Q3"},
                    },
                    "required": ["end", "frequency"],
                },
                "dimensions": {"type": "object", "description": "segment/geography/product 维度（字符串值）"},
                "basis": {"type": "string", "enum": ["GAAP", "IFRS", "non_GAAP", "operating_metric"]},
                "nature": {"type": "string", "enum": ["reported", "guidance", "consensus"]},
                "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "normalization": {
                    "type": "array",
                    "description": "额外显式换算步骤（如 fx_convert）；规模词换算自动登记",
                    "items": {
                        "type": "object",
                        "properties": {
                            "formula_id": {"type": "string"},
                            "params": {"type": "object"},
                        },
                        "required": ["formula_id"],
                    },
                },
                "guidance": {"type": "object"},
                "consensus": {"type": "object"},
                "document_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["metric_key", "value_text", "period", "evidence_ids"],
        },
    },
    "propose_claim": {
        "name": "propose_claim",
        "description": (
            "提交一条研究论断（分析层，与事实分离）：kind=fact_summary 事实摘要 / "
            "inference 推论 / hypothesis 待验证假设 / analysis 分析。"
            "support_refs 全部可解析（ev-/obs-/calc-/fact-/claim-）才标 validated，否则 draft。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "statement": {"type": "string", "description": "论断一句话（结论尽量短）"},
                "kind": {"type": "string", "enum": ["fact_summary", "inference", "hypothesis", "analysis"]},
                "question_id": {"type": "string", "description": "关联的研究问题 id"},
                "support_refs": {"type": "array", "items": {"type": "string"}},
                "counter_refs": {"type": "array", "items": {"type": "string"}, "description": "反证引用"},
                "limitations": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["statement", "kind", "support_refs"],
        },
    },
    "answer_question": {
        "name": "answer_question",
        "description": (
            "更新冻结计划中问题的状态：gathering/answered/disputed/unavailable/not_applicable。"
            "answered 必须给 conclusion + 可解析的 support_refs；"
            "disputed/unavailable 必须记录 unresolved（原因）与 attempts（尝试）。"
            "找不到数据就标 unavailable，不能以模型猜测完成事实采集。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question_id": {"type": "string"},
                "status": {"type": "string",
                           "enum": ["gathering", "answered", "disputed", "unavailable", "not_applicable"]},
                "conclusion": {"type": "string"},
                "support_refs": {"type": "array", "items": {"type": "string"}},
                "counter_refs": {"type": "array", "items": {"type": "string"}},
                "unresolved": {"type": "array", "items": {"type": "string"}},
                "attempts": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["question_id", "status"],
        },
    },
    "calculate_metric": {
        "name": "calculate_metric",
        "description": (
            "受控公式计算（Decimal，输入带引用可重算）："
            "formula_id ∈ yoy_growth/cagr/margin/fcf_from_cfo/net_debt/enterprise_value/"
            "share_dilution/guidance_delta/ttm_sum/reverse_dcf/sensitivity_grid/unit_conversion。"
            "inputs=[{kind: observation|fact|calculation|assumption|market_data, label, "
            "ref_id?, value?, unit?}]；"
            "observation/calculation 必须给 ref_id（服务端解析并核对，禁止漂移）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "formula_id": {"type": "string"},
                "inputs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["observation", "fact", "calculation",
                                         "assumption", "market_data"],
                            },
                            "label": {
                                "type": "string",
                                "description": "公式输入名（current/prior/cfo/capex...）",
                            },
                            "ref_id": {"type": "string"},
                            "value": {"type": "string", "description": "十进制字符串"},
                            "unit": {"type": "string"},
                            "currency": {"type": "string"},
                        },
                        "required": ["kind", "label"],
                    },
                },
                "assumptions": {"type": "object", "description": "全字符串十进制参数（wacc/terminal_g/...）"},
            },
            "required": ["formula_id", "inputs"],
        },
    },
}
