"""统一知识与档案读取工具（tools-plugins 方案 §5.3，PR#2 shared-knowledge-tools）。

把合成阶段已有的 typed 查询抽成共享模块，供 S1 研究 / S2 档案更新 / 报告合成共用，
让 research、profile、报告合成共享可检索的档案上下文——复用已有 typed 数据，
减少重复搜索和重新编写结论：

- `get_research_context`：按问题返回现有事实、观测、论断、计算、缺口与冲突入口；
- `query_observations / query_claims / query_calculations`：指标/性质/状态/问题等
  过滤 + 游标分页，取代固定取前 200/100 条（截断不再静默）；
- `read_evidence`：批量读取原始证据与定位（ev-/obs-/calc-/claim-/fact-），
  不要求模型用搜索重新发现已存资料；
- `list_conflicts / adjudicate_conflict`：legacy 字段与 typed 语义键冲突的统一入口；
  裁决校验语义键与获胜方归属后追加，投影层据裁决更新当前值。

纪律：
- as_of / namespace / 实体由运行上下文固定，模型参数只能进一步缩小范围（过滤），
  不能扩大（不接受 entity/namespace 参数）；
- 读取工具不产生写入；adjudicate 走受控 writer（legacy）/ MetricStore 裁决记录（typed），
  全部落事件可审计；
- 批量读取逐项返回成功/失败（error 与 empty 区分，不静默丢引用）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..eventstore.events import Event
from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest
from ..knowledge.errors import KnowledgeError
from ..knowledge.store import BitemporalStore
from ..knowledge.writer import ProfileWriter

#: typed 裁决事件（与 legacy fact/conflict_resolved 对应，可审计）
METRIC_CONFLICT_RESOLVED = "metric/conflict_resolved"

#: 单工具返回条数硬上限（模型参数只能在此范围内缩小）
MAX_LIMIT = 500
DEFAULT_LIMIT = 50
#: get_research_context 每节默认条数与总字符预算（超出显式截断并提示分工具查询）
CONTEXT_SECTION_LIMIT = 20
CONTEXT_MAX_CHARS = 24000
#: read_evidence 单条摘录上限（全文用 span/locator 回读原文档）
QUOTE_MAX_CHARS = 1500


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if isinstance(dt, datetime) else (str(dt) if dt else None)


def _as_aware(dt: datetime) -> datetime:
    """naive datetime 按 UTC 处理（时态比较不产生 TypeError/语义漂移）。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _limit_of(args: dict[str, Any], default_limits: dict[str, int] | None, tool: str) -> int:
    raw = args.get("limit")
    default = (default_limits or {}).get(tool, DEFAULT_LIMIT)
    try:
        limit = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, MAX_LIMIT))


def _cursor_of(args: dict[str, Any]) -> int:
    try:
        return max(0, int(args.get("cursor") or 0))
    except (TypeError, ValueError):
        return 0


def _paginate(items: list[Any], limit: int, cursor: int) -> tuple[list[Any], int | None]:
    """确定性分页：返回 (本页, next_cursor|None)。total 由调用方随包返回。"""
    page = items[cursor : cursor + limit]
    next_cursor = cursor + len(page) if cursor + len(page) < len(items) else None
    return page, next_cursor


def observation_summary(obs: Any) -> dict[str, Any]:
    """观测的紧凑投影（列表/上下文用；完整 payload 走 read_evidence(obs-…)）。"""
    period = obs.period
    out: dict[str, Any] = {
        "observation_id": obs.observation_id,
        "metric_key": obs.metric_key,
        "value": obs.value,
        "unit": obs.unit,
        "currency": obs.currency,
        "period": {
            "start": _iso(period.start), "end": _iso(period.end),
            "frequency": period.frequency, "fiscal_label": period.fiscal_label,
        },
        "nature": obs.nature,
        "basis": obs.basis,
        "dimensions": obs.dimensions,
        "status": obs.status,
        "pit_grade": getattr(obs.pit_grade, "value", obs.pit_grade),
        "knowledge_time": _iso(obs.knowledge_time),
        "source_available_at": _iso(obs.source_available_at),
        "evidence_refs": list(obs.evidence_refs or []),
        "locator": dict(obs.locator or {}),
    }
    if obs.calculation_ref:
        out["calculation_ref"] = obs.calculation_ref
    if getattr(obs, "is_cross_subject", False):
        out["subject"] = {"entity_kind": obs.subject_kind, "entity_id": obs.subject_id}
    if obs.raw is not None:
        out["raw"] = {
            "value_text": obs.raw.value_text, "unit_text": obs.raw.unit_text,
            "quote_ref": obs.raw.quote_ref, "span": obs.raw.span,
        }
    return out


def claim_summary(payload: dict[str, Any], *, statement_chars: int = 300) -> dict[str, Any]:
    statement = str(payload.get("statement") or "")
    out = {
        "claim_id": payload.get("claim_id"),
        "statement": statement[:statement_chars]
        + ("…" if len(statement) > statement_chars else ""),
        "kind": payload.get("kind"),
        "status": payload.get("status"),
        "question_id": payload.get("question_id"),
        "support_refs": payload.get("support_refs") or [],
        "counter_refs": payload.get("counter_refs") or [],
        "limitations": payload.get("limitations") or [],
        "created_at": payload.get("created_at"),
    }
    ver = payload.get("verification") or {}
    out["verification"] = {
        "references_valid": ver.get("references_valid", False),
        "evidence_support": ver.get("evidence_support", "unchecked"),
    }
    return out


def calculation_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "calculation_id": payload.get("calculation_id"),
        "formula_id": payload.get("formula_id"),
        "formula_version": payload.get("formula_version"),
        "status": payload.get("status"),
        "result": payload.get("result"),
        "unit": payload.get("unit"),
        "warnings": payload.get("warnings") or [],
        "error": payload.get("error"),
        "created_at": payload.get("created_at"),
        "input_refs": [
            {k: r.get(k) for k in ("kind", "label", "ref_id", "value", "unit") if r.get(k)}
            for r in (payload.get("input_refs") or [])
        ],
    }


def make_context_tools(
    *,
    kb: BitemporalStore,
    metrics: Any | None,
    entity_kind: str,
    entity_id: str,
    namespace: str = "prod",
    as_of: datetime | None = None,
    plan_id: str | None = None,
    writer: ProfileWriter | None = None,
    manifest: RunManifest | None = None,
    events: EventStore | None = None,
    default_limits: dict[str, int] | None = None,
    read_only: bool = False,
) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    """构造统一知识读取/裁决工具集（metrics=None 时只装配 legacy 能力子集）。

    as_of 固定于构造时刻的运行上下文（None = 每次调用取当前时间）；
    模型参数只能过滤缩小，不能跨实体/跨命名空间/跨时间放大范围。
    read_only=True（报告合成/委员会等只读阶段）不装配 adjudicate_conflict。
    """

    def _now() -> datetime:
        return as_of or datetime.now(UTC)

    def _content(payload: Any) -> dict[str, Any]:
        return {"content": json.dumps(payload, ensure_ascii=False, default=str), "provenance": []}

    # ---------------- 观测 ----------------

    def _observations(args: dict[str, Any]) -> list[Any]:
        if metrics is None:
            return []
        obs = metrics.observations_as_of(
            entity_kind, entity_id, _now(), namespace=namespace,
            metric_key=(str(args["metric_key"]) if args.get("metric_key") else None),
            frequency=(str(args["frequency"]) if args.get("frequency") else None),
            nature=(str(args["nature"]) if args.get("nature") else None),
        )
        if args.get("status"):
            obs = [o for o in obs if o.status == str(args["status"])]
        if args.get("period_end"):
            obs = [o for o in obs if _iso(o.period.end) == str(args["period_end"])]
        dims = args.get("dimensions")
        if isinstance(dims, dict) and dims:
            want = {str(k): str(v) for k, v in dims.items()}
            obs = [o for o in obs if all(o.dimensions.get(k) == v for k, v in want.items())]
        # 确定性排序（分页稳定）：指标 → 期间 → 记录时间
        return sorted(
            obs,
            key=lambda o: (o.metric_key, _iso(o.period.end) or "", _iso(o.created_at) or "",
                           o.observation_id),
        )

    def query_observations(args: dict[str, Any]) -> dict[str, Any]:
        obs = _observations(args)
        limit = _limit_of(args, default_limits, "query_observations")
        cursor = _cursor_of(args)
        page, next_cursor = _paginate(obs, limit, cursor)
        return _content({
            "total": len(obs), "returned": len(page), "cursor": cursor,
            "next_cursor": next_cursor,
            "items": [observation_summary(o) for o in page],
            "hint": ("结果被分页（total > returned）：用 cursor 继续，或加 metric_key/"
                     "nature/period_end 过滤缩小范围" if next_cursor is not None else ""),
        })

    # ---------------- 论断 ----------------

    def _claims(args: dict[str, Any]) -> list[dict[str, Any]]:
        if metrics is None:
            return []
        statuses = args.get("statuses") or ["draft", "validated"]
        if isinstance(statuses, str):
            statuses = [statuses]
        claims = metrics.claims_as_of(
            entity_kind, entity_id, _now(), namespace=namespace,
            statuses=tuple(str(s) for s in statuses),
        )
        if args.get("kind"):
            claims = [c for c in claims if c.get("kind") == str(args["kind"])]
        if args.get("question_id"):
            claims = [c for c in claims if c.get("question_id") == str(args["question_id"])]
        return sorted(claims, key=lambda c: (str(c.get("created_at") or ""),
                                             str(c.get("claim_id") or "")))

    def query_claims(args: dict[str, Any]) -> dict[str, Any]:
        claims = _claims(args)
        limit = _limit_of(args, default_limits, "query_claims")
        cursor = _cursor_of(args)
        page, next_cursor = _paginate(claims, limit, cursor)
        return _content({
            "total": len(claims), "returned": len(page), "cursor": cursor,
            "next_cursor": next_cursor,
            "items": [claim_summary(c) for c in page],
            "hint": ("分页未尽：用 cursor 继续" if next_cursor is not None else ""),
        })

    # ---------------- 计算 ----------------

    def _calculations(args: dict[str, Any]) -> list[dict[str, Any]]:
        if metrics is None:
            return []
        try:
            ids = metrics.list_calculation_ids(
                entity_kind, entity_id, _now(), namespace=namespace
            )
        except Exception:  # noqa: BLE001 - 存储不支持时降级为空（能力可选）
            return []
        out = []
        for cid in ids:
            stored = metrics.get_calculation(cid)
            if stored is None:
                continue
            payload = stored.payload
            if args.get("formula_id") and payload.get("formula_id") != str(args["formula_id"]):
                continue
            if args.get("status") and payload.get("status") != str(args["status"]):
                continue
            out.append(payload)
        return sorted(out, key=lambda p: (str(p.get("created_at") or ""),
                                          str(p.get("calculation_id") or "")))

    def query_calculations(args: dict[str, Any]) -> dict[str, Any]:
        calcs = _calculations(args)
        limit = _limit_of(args, default_limits, "query_calculations")
        cursor = _cursor_of(args)
        page, next_cursor = _paginate(calcs, limit, cursor)
        return _content({
            "total": len(calcs), "returned": len(page), "cursor": cursor,
            "next_cursor": next_cursor,
            "items": [calculation_summary(p) for p in page],
        })

    # ---------------- 证据批量读取 ----------------

    def _temporally_blocked(known_at: datetime | None) -> str | None:
        """时态隔离（review R2）：资料可知时间晚于本上下文截止 → 拒绝读取。

        历史评估/回放上下文的 as_of 固定在过去时点，生产库中后续登记的资料
        不得泄入（复现：2020 年评估上下文读到了后来才登记的记录）。
        """
        if known_at is not None and _as_aware(known_at) > _now():
            return (
                f"资料可知/登记时间 {known_at.isoformat()} 晚于本上下文截止 "
                f"{_iso(_now())}（时态隔离：未来资料不得进入历史上下文）"
            )
        return None

    def _typed_meta_guard(ref: str) -> dict[str, Any] | None:
        """typed 引用的上下文隔离（review R2）：namespace + 主体 + 登记时间。

        返回 None = 通过；否则返回带 error 的拒绝条目。
        """
        meta = metrics.get_ref_meta(ref)  # type: ignore[union-attr]
        if meta is None or meta["namespace"] != namespace:
            return {"ref": ref, "error": "引用不存在或跨命名空间（上下文隔离）"}
        if (meta["entity_kind"], meta["entity_id"]) != (entity_kind, entity_id):
            return {"ref": ref, "error": (
                f"引用属于其他实体 {meta['entity_kind']}:{meta['entity_id']}（跨上下文拒绝）"
            )}
        known = meta.get("knowledge_time")
        if known:
            blocked = _temporally_blocked(_as_aware(datetime.fromisoformat(str(known))))
            if blocked:
                return {"ref": ref, "error": blocked}
        return None

    def _read_one(ref: str) -> dict[str, Any]:
        if ref.startswith("ev-"):
            ev = kb.get_evidence(ref)  # MissingEvidenceError → 调用方逐项记错
            # 证据库全局（不绑实体），但必须守时态隔离：可知时间（缺失时退取
            # 登记时间）晚于上下文截止的证据不得进入历史研究输入
            blocked = _temporally_blocked(ev.available_at or ev.retrieved_at)
            if blocked:
                return {"ref": ref, "error": blocked}
            quote = ev.verbatim_quote
            return {
                "ref": ref, "kind": "evidence", "source_id": ev.source_id, "url": ev.url,
                "verbatim_quote": quote[:QUOTE_MAX_CHARS]
                + ("…(截断)" if len(quote) > QUOTE_MAX_CHARS else ""),
                "quote_truncated": len(quote) > QUOTE_MAX_CHARS,
                "available_at": _iso(ev.available_at),
                "pit_grade": getattr(ev.pit_grade, "value", ev.pit_grade),
                "quality": getattr(ev, "quality", "ok"),
                "locator": dict(getattr(ev, "locator", None) or {}),
            }
        if ref.startswith("obs-"):
            if metrics is None:
                return {"ref": ref, "error": "typed 存储未装配"}
            denied = _typed_meta_guard(ref)
            if denied is not None:
                return denied
            obs = metrics.get_observation(ref)
            if obs is None:
                return {"ref": ref, "error": "观测不存在或不在本命名空间"}
            return {"ref": ref, "kind": "observation", **observation_summary(obs)}
        if ref.startswith("calc-"):
            if metrics is None:
                return {"ref": ref, "error": "typed 存储未装配"}
            denied = _typed_meta_guard(ref)
            if denied is not None:
                return denied
            stored = metrics.get_calculation(ref)
            if stored is None:
                return {"ref": ref, "error": "计算不存在"}
            return {"ref": ref, "kind": "calculation", **stored.payload}
        if ref.startswith("claim-"):
            if metrics is None:
                return {"ref": ref, "error": "typed 存储未装配"}
            denied = _typed_meta_guard(ref)
            if denied is not None:
                return denied
            payload = metrics.get_claim(ref)
            if payload is None:
                return {"ref": ref, "error": "论断不存在"}
            out = {"ref": ref, "kind": "claim", **payload}
            # 失效可见性（review R4）：按 id 回读已失效论断是合法审计行为，
            # 但必须带失效标记与原因（不得看上去仍是有效结论）
            inv = metrics.claim_invalidation(ref, namespace=namespace)
            if inv is not None:
                out["invalidated"] = True
                out["invalidation"] = {
                    "reason": inv.get("reason"),
                    "invalidated_at": inv.get("invalidated_at"),
                }
            return out
        if ref.startswith("fact-"):
            row = kb._conn.execute(  # noqa: SLF001 - 只读存在性+上下文（同 ref_resolvable）
                "SELECT namespace, entity_kind, entity_id, field, value_json, knowledge_time,"
                " evidence_ids, conflict_flag FROM facts WHERE fact_id = ?", (ref,),
            ).fetchone()
            if row is None or row[0] != namespace:
                return {"ref": ref, "error": "事实不存在或跨命名空间"}
            if (row[1], row[2]) != (entity_kind, entity_id):
                return {"ref": ref, "error": f"事实属于其他实体 {row[1]}:{row[2]}（跨上下文拒绝）"}
            if row[5]:
                blocked = _temporally_blocked(_as_aware(datetime.fromisoformat(row[5])))
                if blocked:
                    return {"ref": ref, "error": blocked}
            return {
                "ref": ref, "kind": "fact", "field": row[3], "value": json.loads(row[4]),
                "knowledge_time": row[5], "evidence_ids": json.loads(row[6]),
                "conflict_flag": bool(row[7]),
            }
        return {"ref": ref, "error": f"未知引用前缀（支持 ev-/obs-/calc-/claim-/fact-）: {ref}"}

    def read_evidence(args: dict[str, Any]) -> dict[str, Any]:
        refs: list[str] = []
        if args.get("evidence_id"):
            refs.append(str(args["evidence_id"]))
        extra = args.get("refs") or []
        if isinstance(extra, str):
            extra = [extra]
        refs.extend(str(r) for r in extra)
        refs = list(dict.fromkeys(refs))  # 去重保序
        if not refs:
            return _content({"items": [], "resolved": 0, "failed": 0,
                             "error": "必须给出 evidence_id 或 refs（ev-/obs-/calc-/claim-/fact-）"})
        items = []
        for ref in refs[:50]:  # 单次批量上限（防上下文打爆；分批调用即可）
            try:
                items.append(_read_one(ref))
            except Exception as e:  # noqa: BLE001 - 逐项失败可见，不拖死整批
                items.append({"ref": ref, "error": f"{type(e).__name__}: {e}"})
        failed = sum(1 for i in items if "error" in i)
        return _content({"items": items, "resolved": len(items) - failed, "failed": failed})

    # ---------------- 冲突 ----------------

    def _legacy_conflicts() -> list[dict[str, Any]]:
        flagged = kb.open_conflicts(entity_kind, entity_id, namespace=namespace)
        out = []
        for rec in flagged:
            history = kb.history(entity_kind, entity_id, rec.field, namespace=namespace)
            out.append({
                "target": "field", "field": rec.field,
                "versions": [
                    {"fact_id": h.fact_id, "value": h.value,
                     "knowledge_time": _iso(h.knowledge_time),
                     "evidence_ids": list(h.evidence_ids or []),
                     "conflict_flag": h.conflict_flag, "version": h.version}
                    for h in history
                ],
            })
        # 同字段多条竞争版本只出一张卡
        seen: set[str] = set()
        deduped = []
        for card in out:
            if card["field"] in seen:
                continue
            seen.add(card["field"])
            deduped.append(card)
        return deduped

    def _typed_conflicts() -> list[dict[str, Any]]:
        if metrics is None:
            return []
        hashes = metrics.conflicted_semantic_hashes(
            entity_kind, entity_id, namespace=namespace, as_of=_now(), exclude_resolved=True,
        )
        out = []
        for sem in hashes:
            versions = metrics.observation_history(sem, namespace=namespace, as_of=_now())
            if not versions:
                continue
            key = versions[0].semantic_key()
            out.append({
                "target": "observation", "semantic_hash": sem,
                "metric_key": key.get("metric_key"), "period_end": key.get("period_end"),
                "frequency": key.get("frequency"), "basis": key.get("basis"),
                "dimensions": key.get("dimensions"), "currency": key.get("currency"),
                "versions": [
                    {"observation_id": v.observation_id, "value": v.value, "unit": v.unit,
                     "nature": v.nature, "pit_grade": getattr(v.pit_grade, "value", v.pit_grade),
                     "knowledge_time": _iso(v.knowledge_time),
                     "evidence_refs": list(v.evidence_refs or []),
                     "calculation_ref": v.calculation_ref}
                    for v in versions
                ],
            })
        return out

    def list_conflicts(args: dict[str, Any]) -> dict[str, Any]:
        del args
        return _content({
            "as_of": _iso(_now()),
            "legacy_fields": _legacy_conflicts(),
            "typed_semantic_keys": _typed_conflicts(),
            "hint": ("裁决用 adjudicate_conflict：field 目标给 keep_fact_id/keep_evidence_id；"
                     "observation 目标给 semantic_hash + keep_observation_id + rationale"),
        })

    def adjudicate_conflict(args: dict[str, Any]) -> dict[str, Any]:
        target = str(args.get("target") or "")
        rationale = str(args.get("rationale") or args.get("note") or "").strip()
        if not rationale:
            return _content({"rejected": "裁决必须给出 rationale（裁决理由进审计事件）"})
        if target == "field":
            if writer is None or manifest is None:
                return _content({"rejected": "本 step 未装配受控 writer，不能裁决 legacy 字段冲突"})
            field = str(args.get("field") or "")
            if not field:
                return _content({"rejected": "target=field 必须给出 field"})
            try:
                out = writer.adjudicate_conflict(
                    entity_kind, entity_id, field,
                    keep_fact_id=str(args.get("keep_fact_id") or ""),
                    keep_evidence_id=str(args.get("keep_evidence_id") or ""),
                    note=rationale, run=manifest, namespace=namespace,
                )
            except (KnowledgeError, KeyError, ValueError) as e:
                return _content({"rejected": f"{type(e).__name__}: {e}"})
            return _content({"resolved": "field", "field": field, **out})
        if target == "observation":
            if metrics is None:
                return _content({"rejected": "typed 存储未装配，不能裁决观测冲突"})
            sem = str(args.get("semantic_hash") or "")
            keep = str(args.get("keep_observation_id") or "")
            if not sem or not keep:
                return _content({
                    "rejected": "target=observation 必须给出 semantic_hash 与 keep_observation_id"
                })
            versions = metrics.observation_history(sem, namespace=namespace)
            chain_ids = [v.observation_id for v in versions]
            if not versions:
                return _content({"rejected": f"语义键 {sem} 无版本链（不属于本实体/命名空间？）"})
            if keep not in chain_ids:
                return _content({
                    "rejected": f"keep_observation_id {keep} 不在语义键 {sem} 的版本链中"
                                f"（可用：{chain_ids}）"
                })
            winner = next(v for v in versions if v.observation_id == keep)
            # 证据关联校验（方案 §5.3）：reported/guidance 获胜方必须仍有可解析证据
            for ref in winner.evidence_refs or []:
                if ref.startswith("ev-"):
                    try:
                        kb.get_evidence(ref)
                    except Exception as e:  # noqa: BLE001 - fail-loud，不落无效裁决
                        return _content({
                            "rejected": f"获胜观测的证据 {ref} 不可解析（{e}）——先补证据再裁决"
                        })
            supporting = [str(r) for r in (args.get("supporting_refs") or [])]
            resolution_id = f"res-{uuid.uuid4().hex[:12]}"
            from ..knowledge.metric_store import ConflictResolution

            metrics.add_resolution(ConflictResolution(
                resolution_id=resolution_id, namespace=namespace,
                entity_kind=entity_kind, entity_id=entity_id,
                target_kind="observation", semantic_hash=sem,
                keep_observation_id=keep, resolved_at=_now(),
                note=rationale + (f"；supporting={supporting}" if supporting else ""),
                run_id=manifest.run_id if manifest else None,
            ))
            if events is not None:
                events.append(Event(
                    run_id=manifest.run_id if manifest else "context-tools",
                    type=METRIC_CONFLICT_RESOLVED,
                    payload={
                        "resolution_id": resolution_id,
                        "entity": f"{entity_kind}:{entity_id}",
                        "semantic_hash": sem, "keep_observation_id": keep,
                        "discarded": [i for i in chain_ids if i != keep],
                        "rationale": rationale, "namespace": namespace,
                    },
                ))
            return _content({
                "resolved": "observation", "resolution_id": resolution_id,
                "semantic_hash": sem, "keep_observation_id": keep,
                "note": "当前投影已按裁决取获胜版本（observations_as_of 时态化）",
            })
        return _content({"rejected": f"未知 target {target!r}（field/observation）"})

    # ---------------- 聚合上下文 ----------------

    def get_research_context(args: dict[str, Any]) -> dict[str, Any]:
        limit = _limit_of(args, default_limits, "get_research_context")
        limit = min(limit, CONTEXT_SECTION_LIMIT * 4)
        wanted_qids = args.get("question_ids")
        if isinstance(wanted_qids, str):
            wanted_qids = [wanted_qids]

        payload: dict[str, Any] = {
            "entity": f"{entity_kind}:{entity_id}",
            "namespace": namespace,
            "as_of": _iso(_now()),
        }
        # 1) 冻结计划的问题状态（按 question_ids 缩小；范围不可扩大）
        questions: list[dict[str, Any]] = []
        if plan_id and metrics is not None:
            plan_payload = metrics.get_plan(plan_id) or {}
            for q in plan_payload.get("questions", []):
                if wanted_qids and q.get("question_id") not in set(map(str, wanted_qids)):
                    continue
                questions.append({
                    "question_id": q.get("question_id"), "text": q.get("text"),
                    "priority": q.get("priority"), "status": q.get("status"),
                    "module": q.get("module"),
                    "conclusion": (str(q.get("conclusion") or ""))[:300] or None,
                    "support_refs": q.get("support_refs") or [],
                    "unresolved": q.get("unresolved") or [],
                })
        payload["questions"] = questions
        # 2) legacy 字段投影（含冲突标记）
        view = kb.view(entity_kind, entity_id, _now(), namespace=namespace)
        payload["facts"] = {
            f: {"value": r.value, "knowledge_time": _iso(r.knowledge_time),
                "evidence_ids": list(r.evidence_ids or []), "conflict": r.conflict_flag}
            for f, r in view.items()
        }
        # 3) typed 数据（紧凑投影；详情走 read_evidence）
        obs = _observations({})
        payload["observations"] = {
            "total": len(obs),
            "items": [observation_summary(o) for o in obs[:limit]],
        }
        claims = _claims({})
        payload["claims"] = {
            "total": len(claims),
            "items": [claim_summary(c, statement_chars=160) for c in claims[:limit]],
        }
        calcs = _calculations({})
        payload["calculations"] = {
            "total": len(calcs),
            "items": [calculation_summary(p) for p in calcs[:limit]],
        }
        # 4) 冲突与缺口入口
        payload["conflicts"] = {
            "legacy_fields": [c["field"] for c in _legacy_conflicts()],
            "typed_semantic_keys": [
                {"semantic_hash": c["semantic_hash"], "metric_key": c["metric_key"]}
                for c in _typed_conflicts()
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, default=str)
        truncated = False
        if len(text) > CONTEXT_MAX_CHARS:
            # 一次性降级：观测/论断/计算只留计数与 id，提示分工具查询
            payload["observations"] = {
                "total": len(obs), "truncated": True,
                "ids": [o.observation_id for o in obs[:limit]],
            }
            payload["claims"] = {
                "total": len(claims), "truncated": True,
                "ids": [c.get("claim_id") for c in claims[:limit]],
            }
            payload["calculations"] = {
                "total": len(calcs), "truncated": True,
                "ids": [p.get("calculation_id") for p in calcs[:limit]],
            }
            payload["hint"] = (
                "上下文超预算已降级为 id 清单：用 query_observations/query_claims/"
                "query_calculations（带过滤与游标）和 read_evidence 按需取详情"
            )
            text = json.dumps(payload, ensure_ascii=False, default=str)
            truncated = True
            if len(text) > CONTEXT_MAX_CHARS:
                text = text[:CONTEXT_MAX_CHARS] + "…(硬截断)"
        return {"content": text, "provenance": [], "truncated": truncated}

    tools: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "get_research_context": get_research_context,
        "read_evidence": read_evidence,
        "list_conflicts": list_conflicts,
    }
    if not read_only:
        tools["adjudicate_conflict"] = adjudicate_conflict
    if metrics is not None:
        tools["query_observations"] = query_observations
        tools["query_claims"] = query_claims
        tools["query_calculations"] = query_calculations
    return tools


CONTEXT_TOOL_SCHEMAS: dict[str, dict] = {
    "get_research_context": {
        "name": "get_research_context",
        "description": (
            "一次性取回本实体的研究上下文：冻结计划的问题状态与结论、legacy 字段投影、"
            "typed 观测/论断/计算（紧凑投影 + total）、开放冲突清单。"
            "开始研究或更新档案前先用它复用已有成果，避免重复搜索与重写结论；"
            "详情用 read_evidence / query_* 按 id 取。question_ids 可缩小到相关问题。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question_ids": {"type": "array", "items": {"type": "string"},
                                 "description": "可选：只看这些问题相关的计划状态"},
                "limit": {"type": "integer", "description": "每节条数上限（默认 20）"},
            },
        },
    },
    "query_observations": {
        "name": "query_observations",
        "description": (
            "查询当前实体的 typed 指标观测（结构化数值，带 observation_id/期间/口径/证据）。"
            "支持过滤与游标分页；total > returned 时用 cursor 继续，截断不静默。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "metric_key": {"type": "string", "description": "如 revenue/firm_backlog"},
                "nature": {"type": "string",
                           "enum": ["reported", "calculated", "guidance", "consensus",
                                    "model_estimate"]},
                "frequency": {"type": "string", "enum": ["FY", "Q", "H1", "TTM", "instant"]},
                "status": {"type": "string", "enum": ["ok", "missing", "conflicted",
                                                      "not_meaningful"]},
                "period_end": {"type": "string", "description": "YYYY-MM-DD（精确匹配期间末）"},
                "dimensions": {"type": "object", "description": "维度子集匹配（segment 等）"},
                "limit": {"type": "integer"},
                "cursor": {"type": "integer", "description": "分页游标（上次 next_cursor）"},
            },
        },
    },
    "query_claims": {
        "name": "query_claims",
        "description": (
            "查询当前实体的研究论断（claim_id/statement/kind/status/引用/核验状态）。"
            "validated 仅表示引用校验过，内容级核验状态见 verification.evidence_support。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "statuses": {"type": "array", "items": {"type": "string"},
                             "description": "默认 [draft, validated]"},
                "kind": {"type": "string",
                         "enum": ["fact_summary", "inference", "hypothesis", "analysis"]},
                "question_id": {"type": "string"},
                "limit": {"type": "integer"},
                "cursor": {"type": "integer"},
            },
        },
    },
    "query_calculations": {
        "name": "query_calculations",
        "description": (
            "查询已登记的受控计算（calculation_id/公式/输入引用/结果）——"
            "报告与结论用 calculation_ref 引用，不重算。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "formula_id": {"type": "string"},
                "status": {"type": "string", "enum": ["ok", "failed", "not_meaningful"]},
                "limit": {"type": "integer"},
                "cursor": {"type": "integer"},
            },
        },
    },
    "read_evidence": {
        "name": "read_evidence",
        "description": (
            "批量读取已登记引用的原文与定位：ev-（证据摘录+来源+质量+locator）/ "
            "obs-（观测完整投影）/ calc-（计算 payload）/ claim-（论断全文）/ "
            "fact-（字段值+证据）。逐项返回成功/失败，未知引用显式报错不静默。"
            "上下文隔离：跨实体/跨命名空间/晚于本上下文截止时间的引用逐项拒绝。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string", "description": "单条证据 id（ev- 前缀，兼容旧用法）"},
                "refs": {"type": "array", "items": {"type": "string"},
                         "description": "批量引用（ev-/obs-/calc-/claim-/fact-，单次 ≤50）"},
            },
        },
    },
    "list_conflicts": {
        "name": "list_conflicts",
        "description": (
            "列出本实体的开放冲突：legacy 字段竞争版本（fact 版本链）与 typed 语义键"
            "竞争观测（同指标/期间/口径不同值）。裁决前先看版本与证据。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    "adjudicate_conflict": {
        "name": "adjudicate_conflict",
        "description": (
            "裁决一个开放冲突（必须给 rationale，落审计事件）："
            "target=field → field + keep_fact_id/keep_evidence_id（获胜版本非最新时"
            "服务端同值晋升为当前投影）；target=observation → semantic_hash + "
            "keep_observation_id（校验版本链与证据关联后追加裁决，投影按时态取获胜版本）。"
            "获胜方不存在/证据不可解析会被拒绝，不会静默清标记。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["field", "observation"]},
                "field": {"type": "string"},
                "keep_fact_id": {"type": "string"},
                "keep_evidence_id": {"type": "string"},
                "semantic_hash": {"type": "string"},
                "keep_observation_id": {"type": "string"},
                "supporting_refs": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string", "description": "裁决理由（必填）"},
            },
            "required": ["target", "rationale"],
        },
    },
}
