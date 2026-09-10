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
from .context_tools import CONTEXT_TOOL_SCHEMAS, make_context_tools
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
        #: answer_question 被拒记录（audit §3.1）：问题零推进时区分「未分发」与「提交失败」
        self.answer_rejections: list[dict] = []
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


def _reject_answer(tracker: _Tracker, question_id: str, reason: str) -> dict[str, Any]:
    """answer_question 被拒的统一出口：同时计入 tracker.answer_rejections。

    audit §3.1：问题零推进时必须能区分「未分发」与「提交失败」，所以拒绝不能只
    回给模型就丢掉。
    """
    tracker.answer_rejections.append({"question_id": question_id, "reason": reason})
    tracker.rejected.append({"question": question_id, "reason": reason})
    return {"content": f"rejected: {reason}", "provenance": []}


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
    allowed_question_ids: list[str] | None = None,  # 调度器下发的本 worker 问题集
) -> tuple[dict[str, Any], _Tracker]:
    tracker = _Tracker()

    def register_evidence(args: dict[str, Any]) -> dict[str, Any]:
        """证据登记：接受 span_id（优先）或 chunk_id + 逐字摘录（服务端子串校验）。"""
        try:
            ev = verify_and_build(
                chunk_store,
                chunk_id=str(args.get("chunk_id") or ""),
                verbatim_quote=str(args.get("verbatim_quote") or ""),
                evidence_id=args.get("evidence_id"),
                span_id=(str(args["span_id"]) if args.get("span_id") else None),
            )
        except EvidenceVerificationError as e:
            # 拒绝码 + 可修复提示（audit §3.5）：防止模型反复重试或缩短摘录丢语义
            tracker.rejected.append({
                "evidence": str(args.get("chunk_id") or args.get("span_id")),
                "reason": str(e), "code": e.code,
            })
            return {
                "content": json.dumps({
                    "rejected": str(e), "code": e.code, "hint": e.hint,
                }, ensure_ascii=False),
                "provenance": [],
            }
        store.add_evidence(ev)
        tracker.registered.append(ev.evidence_id)
        return {
            "content": json.dumps({"evidence_id": ev.evidence_id, "quality": ev.quality},
                                  ensure_ascii=False),
            "provenance": [
                {
                    "source_id": ev.source_id,
                    "available_at": ev.available_at.isoformat() if ev.available_at else None,
                    "pit_grade": ev.pit_grade.value,
                }
            ],
        }

    def read_chunk(args: dict[str, Any]) -> dict[str, Any]:
        """按需 evidence bundle（audit §3.3/§3.5）：取回 chunk 的规范正文与可用 span。

        工具响应被截断时，全文不无条件灌进上下文，而是凭 chunk_id 按需取；
        返回的 span_id 可直接给 register_evidence（服务端填摘录，转写误差为零）。
        """
        cid = str(args.get("chunk_id") or "")
        chunk = chunk_store.get(cid)
        if chunk is None:
            return {"content": f"error: 未知 chunk_id {cid}", "provenance": []}
        query = str(args.get("query") or "").strip()
        idxs = list(range(len(chunk.spans)))
        if query:
            hits = [i for i in idxs if query.lower() in chunk.spans[i].lower()]
            idxs = hits or idxs
        window = max(1, int(args.get("max_spans") or 8))
        payload = {
            "chunk_id": cid,
            "source_id": chunk.source_id,
            "url": chunk.url,
            "quality": chunk.quality,
            "locator": chunk.locator,
            "available_at": chunk.available_at.isoformat() if chunk.available_at else None,
            "pit_grade": chunk.pit_grade.value,
            "span_count": len(chunk.spans),
            "spans": [{"span_id": f"{cid}#s{i}", "text": chunk.spans[i]}
                      for i in idxs[:window]],
        }
        return {
            "content": json.dumps(payload, ensure_ascii=False),
            "provenance": [{
                "source_id": chunk.source_id,
                "available_at": payload["available_at"],
                "pit_grade": chunk.pit_grade.value,
            }],
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
        """裁决字段冲突（Q2 + tools-plugins 方案 P0）：真正选择并保存获胜版本。

        旧缺陷：收 keep_evidence_id 却向 writer 传空 keep_fact_id，底层只清标记——
        能显示「已处理」却没有保存获胜事实。现走 writer.adjudicate_conflict：
        定位获胜版本 → 非最新则同值晋升 → 清标记，全部留痕；定位失败即拒。
        """
        field = str(args["field"])
        keep_fact_id = str(args.get("keep_fact_id") or "")
        keep_evidence_id = str(args.get("keep_evidence_id") or "")
        if not keep_fact_id and not keep_evidence_id:
            return {
                "content": "rejected: 必须给出 keep_fact_id 或 keep_evidence_id（裁决需要明确的获胜方）",
                "provenance": [],
            }
        try:
            out = writer.adjudicate_conflict(
                entity_kind, entity_id, field,
                keep_fact_id=keep_fact_id,
                keep_evidence_id=keep_evidence_id,
                note=str(args.get("note") or ""),
                run=manifest,
                namespace=namespace,
            )
        except (KnowledgeError, KeyError, ValueError) as e:
            tracker.rejected.append({"conflict": field, "reason": str(e)})
            return {"content": f"rejected: {e}", "provenance": []}
        return {
            "content": json.dumps({
                "resolved": field,
                "winner_fact_id": out["winner_fact_id"],
                "promoted_fact_id": out["promoted_fact_id"],
                "cleared": out["cleared"],
            }, ensure_ascii=False),
            "provenance": [],
        }

    tools: dict[str, Any] = {
        "register_evidence": register_evidence,
        "read_chunk": read_chunk,
        "propose_fact": propose_fact,
        "query_kb": query_kb,
        "resolve_conflict": resolve_conflict,  # adjudicate_conflict(target=field) 的兼容别名
        "calc": calc_tool,  # §4.7：数字保护双保险（逐字 + 计算一致性）
    }

    # ---------------- 统一知识读取（tools-plugins 方案 §5.3，S1/S2/合成共享模块） ----
    # 复用已有 typed 数据（观测/论断/计算/冲突）与证据原文，减少重复搜索与重写结论；
    # as_of/namespace/实体由运行上下文固定，模型参数只能缩小范围。
    for name, fn in make_context_tools(
        kb=store, metrics=metrics, entity_kind=entity_kind, entity_id=entity_id,
        namespace=namespace, plan_id=plan_id, writer=writer, manifest=manifest, events=events,
    ).items():
        tools.setdefault(name, fn)

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
            # 抽取质量前置拦（audit §3.2）：乱码/需 OCR 的正文不得支撑正式数值——
            # 在工具层就给可读原因，不让模型反复重试
            bad_quality = [
                (e.evidence_id, e.quality) for e in evidences
                if getattr(e, "quality", "ok") in ("garbled", "needs_ocr")
            ]
            if bad_quality and len(bad_quality) == len(evidences):
                reason = (
                    f"证据抽取质量不可用 {bad_quality}：乱码/扫描件正文不得进入指标库"
                    "（请重新抽取、换源，或改标 status=missing/unavailable）"
                )
                tracker.rejected.append({"metric": metric_key, "reason": reason})
                return {"content": f"rejected: {reason}", "provenance": []}
            # 指标主体（audit §3.2）：默认 = 研究实体；显式给出则走授权闸
            subject_arg = args.get("subject") or {}
            if isinstance(subject_arg, str):
                subject_arg = {"entity_id": subject_arg}
            _subject_kind = str(subject_arg.get("entity_kind") or entity_kind)
            _subject_id = normalize_entity_id(
                _subject_kind, str(subject_arg.get("entity_id") or entity_id)
            )
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
                                quote_ref=evidence_ids[0],
                                span=str(args.get("value_span") or "")),
                # 主体与研究范围分离（audit §3.2）：公司财务写公司实体，
                # 行业实体只能引用已入候选/已授权的主体（门禁在 writer）
                "subject_entity_kind": _subject_kind,
                "subject_entity_id": _subject_id,
                # 文档定位（document/page/table/row/column）：金额类必需
                "locator": {str(k): str(v) for k, v in (args.get("locator") or {}).items()},
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
            """研究论断（分析层）：支持/反方引用可解析 → validated（仅引用校验），
            否则 draft 留痕。分项核验状态写入 verification（方案 §5.4）：
            validated 不得被下游当作「内容已核验」。"""
            from ..eventstore.events import RESEARCH_CLAIM_VALIDATED
            from .artifacts import ClaimVerification

            statement = str(args.get("statement") or "").strip()
            support = [str(r) for r in (args.get("support_refs") or [])]
            counter = [str(r) for r in (args.get("counter_refs") or [])]
            if len(statement) < 4:
                return {"content": "rejected: statement 过短（至少 4 字符）", "provenance": []}
            kind = str(args.get("kind") or "inference")
            if kind not in ("fact_summary", "inference", "hypothesis", "analysis"):
                return {"content": f"rejected: 未知 kind {kind!r}", "provenance": []}
            # 引用未知 question_id 的 claim 不得正式归档（audit §3.1）：编造的问题归属
            # 会把论断投到错误模块（本次事故：三条 claim 误用模块名 key_kpi 当 question_id）
            question_id = args.get("question_id")
            if question_id:
                plan_payload = metrics.get_plan(plan_id) if plan_id else None
                known = {str(q.get("question_id") or "")
                         for q in (plan_payload or {}).get("questions", [])}
                if plan_payload is not None and str(question_id) not in known:
                    reason = (
                        f"question_id {question_id!r} 不在冻结计划中（可用：{sorted(known)}）"
                    )
                    tracker.rejected.append({"claim": statement[:40], "reason": reason})
                    return {"content": f"rejected: {reason}", "provenance": []}
                if allowed_question_ids is not None and str(question_id) not in allowed_question_ids:
                    # 本 worker 未被分配该问题：仍可写，但不得 validated（越位结论不入正式产物）
                    pass
            # 支持与反方引用都验（存在性+命名空间+实体，review #5）
            canonical_id = normalize_entity_id(entity_kind, entity_id)
            ctx = {"namespace": namespace, "entity_kind": entity_kind, "entity_id": canonical_id}
            unresolved_refs = [r for r in support if not _ref_resolvable(store, metrics, r, **ctx)]
            unresolved_counter = [r for r in counter if not _ref_resolvable(store, metrics, r, **ctx)]
            out_of_scope = bool(
                question_id and allowed_question_ids is not None
                and str(question_id) not in allowed_question_ids
            )
            status = (
                "validated"
                if support and not unresolved_refs and not unresolved_counter and not out_of_scope
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
                    # 分项核验状态（方案 §5.4）：本工具只做引用校验；内容级支持性、
                    # 数值一致性、推理审查留给独立 verifier（P2），缺省 unchecked 诚实标记。
                    verification=ClaimVerification(
                        references_valid=status == "validated",
                        evidence_support="unchecked",
                        numeric_checks="unchecked",
                        analysis_review="not_required" if kind == "fact_summary" else "unchecked",
                        counter_evidence_search=bool(counter),
                        verified_by="propose_claim:references",
                    ),
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
                    # 诚实标记：本事件只代表引用校验，不代表内容级核验已过
                    "content_checked": False,
                }))
            tracker.claims.append(claim.claim_id)
            note = ""
            if status != "validated":
                if out_of_scope:
                    note = (
                        f"（draft：问题 {question_id} 未分配给本 worker，越位结论不入正式产物）"
                    )
                elif unresolved_refs or unresolved_counter:
                    note = (
                        f"（draft：引用不可解析/跨上下文 support={unresolved_refs} "
                        f"counter={unresolved_counter}）"
                    )
                elif not support:
                    note = "（draft：无支持引用）"
            else:
                note = "（validated = 引用可解析；内容级核验尚未执行，见 verification.evidence_support）"
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
                return _reject_answer(
                    tracker, qid,
                    f"问题不在冻结计划中: {qid}（计划范围不可扩展）",
                )
            if allowed_question_ids is not None and qid not in allowed_question_ids:
                # 调度器已把问题显式分配给各 worker（audit §3.1）：越位回答会让验收无法归因
                return _reject_answer(
                    tracker, qid,
                    f"问题 {qid} 未分配给本 worker（本组待答：{sorted(allowed_question_ids)}）",
                )
            del question  # 门禁只用计划判存在性；更新走存储层原子入口（review #8）
            status = str(args.get("status") or "")
            if status not in ("gathering", "answered", "disputed", "unavailable", "not_applicable"):
                return _reject_answer(tracker, qid, f"未知状态 {status!r}")
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
                    return _reject_answer(tracker, qid, "answered 必须给出 conclusion")
                if not support:
                    return _reject_answer(
                        tracker, qid,
                        "answered 必须给出 support_refs（证据/观测/计算/论断 id）",
                    )
                bad = [r for r in support if not _ref_resolvable(store, metrics, r, **ctx)]
                if bad:
                    return _reject_answer(
                        tracker, qid, f"支持引用不可解析或跨上下文: {bad}"
                    )
            if counter:
                bad_counter = [r for r in counter if not _ref_resolvable(store, metrics, r, **ctx)]
                if bad_counter:
                    return _reject_answer(
                        tracker, qid, f"反方引用不可解析或跨上下文: {bad_counter}"
                    )
            if status in ("disputed", "unavailable") and not (unresolved or attempts):
                return _reject_answer(
                    tracker, qid,
                    f"{status} 必须记录原因（unresolved）与尝试（attempts）",
                )
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
                return _reject_answer(
                    tracker, qid, f"计划/问题在更新窗口内消失: {plan_id}/{qid}"
                )
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
            """抓取 filing 正文并按 query 切窗口，窗口落 ChunkStore 供 register_evidence 引用。

            抓取函数可返回 str 或 (str, TextQuality)；后者把抽取质量带进 chunk
            （audit §3.5：乱码/扫描件单独标记，不得当作可靠数字来源）。
            """
            record_chunk = chunk_store.get(str(args.get("chunk_id") or ""))
            if record_chunk is None:
                return {"content": "error: 未知 chunk_id（先 query_edgar 拿 filing 记录）", "provenance": []}
            if not record_chunk.url:
                return {"content": "error: 该记录没有可抓取的 url", "provenance": []}
            try:
                fetched = fetch_document(record_chunk.url)
            except Exception as e:
                return {"content": f"error: 抓取失败：{type(e).__name__}: {e}", "provenance": []}
            quality = "ok"
            if isinstance(fetched, tuple):
                text, tq = fetched[0], fetched[1]
                quality = getattr(tq, "quality", str(tq))
            else:
                from ..gateway.text_quality import assess_text_quality

                text = fetched
                quality = assess_text_quality(text).quality
            out = []
            for window in _windows(text, str(args.get("query") or "")):
                cid = chunk_store.add(
                    source_id=record_chunk.source_id,
                    text=window,
                    url=record_chunk.url,
                    available_at=record_chunk.available_at,  # PIT 元数据从 filing 记录继承
                    pit_grade=record_chunk.pit_grade,
                    quality=quality,
                    locator={"source_chunk": record_chunk.chunk_id},
                )
                out.append({"chunk_id": cid, "text": window, "quality": quality})
            note = ""
            if quality in ("garbled", "needs_ocr"):
                note = (
                    f"\n⚠ 抽取质量={quality}：该正文不可作为结构化数值来源"
                    "（propose_metric 会被拒）；需 OCR 或人工核对后重试。"
                )
            return {
                "content": json.dumps({"windows": out, "quality": quality}, ensure_ascii=False)
                + note,
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
            "登记一条证据。两种引用方式，优先用 span_id（服务端填摘录，不会因转写差异被拒）："
            "① span_id=read_chunk/query_* 返回的稳定片段 id；"
            "② chunk_id + verbatim_quote（quote 必须是该 chunk 的逐字原文，服务端校验子串）。"
            "来源与可知时刻由系统从 chunk 推导，无需自报。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string",
                             "description": "检索内容 chunk id（query_* 或 read_edgar_filing 返回）"},
                "verbatim_quote": {"type": "string", "description": "该 chunk 内的逐字原文摘录"},
                "span_id": {"type": "string",
                            "description": "稳定片段 id（形如 chk-0001#s3）；给了就不需 verbatim_quote"},
                "evidence_id": {"type": "string", "description": "可选；不提供则自动生成"},
            },
        },
    },
    "read_chunk": {
        "name": "read_chunk",
        "description": (
            "按需读取一条检索内容（chunk）的规范正文与可用 span 列表（工具响应被截断时用）。"
            "返回的 span_id 可直接交给 register_evidence；可用 query 关键词筛选 span。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string"},
                "query": {"type": "string", "description": "可选：只返回包含该词的 span"},
                "max_spans": {"type": "integer", "description": "返回 span 上限（默认 8）"},
            },
            "required": ["chunk_id"],
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
        "description": (
            "裁决字段的开放冲突（adjudicate_conflict(target=field) 的兼容别名）："
            "必须指定获胜方（keep_fact_id 或支撑保留值的 keep_evidence_id）。"
            "服务端会真正保存获胜版本（非最新版时同值晋升为当前投影）"
            "并清除竞争标记；获胜方不在版本链中会被拒绝，不会静默清标记。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string"},
                "keep_fact_id": {"type": "string", "description": "保留哪个事实版本（优先）"},
                "keep_evidence_id": {"type": "string",
                                     "description": "或以哪条证据为准（服务端反查绑定该证据的版本）"},
                "note": {"type": "string", "description": "裁决理由"},
            },
            "required": ["field"],
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
    # read_evidence 已迁至共享上下文工具（context_tools.CONTEXT_TOOL_SCHEMAS，
    # S1/S2/合成/委员会/rank_report 共用同一契约：单条 evidence_id 或批量 refs）。
    # 此处引用同一 schema 对象，保持 TOOL_SCHEMAS 消费方（路由/测试）可见。
    "read_evidence": CONTEXT_TOOL_SCHEMAS["read_evidence"],
    # ---- typed 工具（档案升级；未装配 MetricStore 时路由层不会绑定） ----
    "propose_metric": {
        "name": "propose_metric",
        "description": (
            "登记一条 typed 指标观测（结构化数值，图表/计算只消费这里）。"
            "value_text 必须是证据摘录中逐字出现的原文值（如 '1.2 billion'）；"
            "服务端自动登记规模词换算并逐步重算；期间/口径/维度必填。"
            "数字语义门禁（违者拒写）：① metric_key 必须是注册表语义键（比例不得带币种，"
            "合同潜在总额/已收首付款/里程碑上限是三个不同指标）；② 金额类必须给 currency "
            "与 locator（document/page/table/row/column），裸数字摘录不得当成可靠金额；"
            "③ 摘录含多个数字时必须给 value_span 显式选定 cell/span；"
            "④ 公司财务写在该公司主体上（subject），不得把不同公司的收入记在行业实体上。"
            "nature=guidance 时额外给 guidance={issuer,published_at,target_period}；"
            "nature=consensus 时给 consensus={vendor,snapshot_at}。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "metric_key": {"type": "string", "description": "语义键：revenue/capex/firm_backlog/arr..."},
                "value_text": {"type": "string", "description": "原文值文本（逐字，含规模词）"},
                "value_span": {"type": "string",
                               "description": "摘录含多个数字时必填：包含本值的原文片段（逐字）"},
                "unit": {"type": "string", "description": "目标单位（USD/units/ratio...）"},
                "unit_text": {"type": "string", "description": "原文单位文本（如 'USD millions'）"},
                "currency": {"type": "string"},
                "subject": {
                    "type": "object",
                    "description": "指标主体（缺省 = 研究实体）；跨主体需在授权范围内",
                    "properties": {
                        "entity_kind": {"type": "string", "enum": ["stock", "industry"]},
                        "entity_id": {"type": "string"},
                    },
                },
                "locator": {
                    "type": "object",
                    "description": "文档定位（金额类必填）：document/page/table/row/column/section",
                },
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
            "注意：validated 仅表示引用校验通过；原文是否支持整句结论属于内容级核验"
            "（verification.evidence_support），未核验前不得当作已证事实引用。"
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
