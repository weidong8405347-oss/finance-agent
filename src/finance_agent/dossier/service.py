"""DossierService：快照生命周期与模块读模型（设计 §6.6/§10.1）。

- open：live 模式把「现在」固定为服务端时刻 → 投影 → data_hash 幂等落库 →
  dossier/published 事件（changed_modules 供 UI 提示）；
- module：从冻结快照的 context 重投影同一 as_of 的模块 payload（不自动跳最新）；
- changes：两个快照之间的数据/claim/来源/质量 diff；
- export：JSON/Markdown 冻结导出（与页面同源，绑定 data_hash）；
- 失败可见：publish 失败落 dossier/publish_failed 事件 + 日志（三通道纪律）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from ..eventstore.events import DOSSIER_PUBLISH_FAILED, DOSSIER_PUBLISHED, Event
from ..eventstore.store import EventStore
from ..knowledge.metric_store import MetricStore
from ..knowledge.store import BitemporalStore
from .models import MODULE_IDS, ModulePayload
from .projector import (
    DossierProjector,
    business_graph,
    evidence_items,
    legacy_fact_items,
    series_set,
)

logger = logging.getLogger("finance_agent.dossier")


class DossierError(Exception):
    """服务层错误（API 映射 404/409/422）。"""

    def __init__(self, message: str, *, status: int = 404):
        super().__init__(message)
        self.status = status


class DossierService:
    def __init__(
        self,
        *,
        kb: BitemporalStore,
        metrics: MetricStore,
        projector: DossierProjector | None = None,
        events: EventStore | None = None,
        decisions: Any | None = None,
        recipes_dir: str | None = None,
    ):
        self._kb = kb
        self._metrics = metrics
        self._events = events
        self._projector = projector or DossierProjector(
            kb=kb, metrics=metrics, decisions=decisions, recipes_dir=recipes_dir
        )

    # ---------------- 打开/发布 ----------------

    def open(
        self,
        entity_kind: str,
        entity_id: str,
        *,
        as_of: datetime | None = None,
        namespace: str = "prod",
        mode: str = "live",
        name: str = "",
        run_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """返回 (snapshot payload, created)。同 data_hash 幂等（刷新不重复发布）。"""
        try:
            ctx = self._projector.build_context(
                entity_kind, entity_id, as_of=as_of, namespace=namespace, mode=mode
            )
            snapshot = self._projector.project(entity_kind, entity_id, ctx, name=name)
            short = snapshot.data_hash.split(":", 1)[-1][:12]
            snapshot.context.snapshot_id = f"dossier-{entity_id.lower()}-{short}"
            payload = snapshot.model_dump(mode="json")
            previous = self._metrics.latest_snapshot(entity_kind, entity_id, namespace=namespace)
            snapshot_id, created = self._metrics.save_snapshot(
                snapshot_id=snapshot.context.snapshot_id, namespace=namespace, payload=payload
            )
            payload["context"]["snapshot_id"] = snapshot_id
            if created:
                changed = _changed_modules(previous, payload)
                self._emit(run_id or "dossier", DOSSIER_PUBLISHED, {
                    "snapshot_id": snapshot_id,
                    "entity": f"{entity_kind}:{entity_id}",
                    "namespace": namespace,
                    "mode": mode,
                    "as_of": ctx.as_of.isoformat(),
                    "data_hash": snapshot.data_hash,
                    "changed_modules": changed,
                })
            return payload, created
        except Exception as e:
            logger.error("dossier 发布失败 %s:%s: %s", entity_kind, entity_id, e, exc_info=True)
            self._emit(run_id or "dossier", DOSSIER_PUBLISH_FAILED, {
                "entity": f"{entity_kind}:{entity_id}",
                "namespace": namespace,
                "reason": f"{type(e).__name__}: {e}",
            })
            raise

    def get(self, snapshot_id: str) -> dict[str, Any]:
        payload = self._metrics.get_snapshot(snapshot_id)
        if payload is None:
            raise DossierError(f"快照不存在: {snapshot_id}", status=404)
        return payload

    def latest(self, entity_kind: str, entity_id: str, *, namespace: str = "prod") -> dict | None:
        return self._metrics.latest_snapshot(entity_kind, entity_id, namespace=namespace)

    # ---------------- 模块 payload ----------------

    def module(
        self, snapshot_id: str, module: str, *, params: dict[str, Any] | None = None
    ) -> ModulePayload:
        if module not in MODULE_IDS and module not in ("legacy_audit", "timeline"):
            raise DossierError(f"未知模块 {module!r}（可用：{list(MODULE_IDS)}）", status=422)
        snap = self.get(snapshot_id)
        ctx = snap["context"]
        entity = snap["entity"]
        t = datetime.fromisoformat(ctx["as_of"])
        ns = ctx["namespace"]
        kind, eid = entity["kind"], entity["id"]
        state = snap["modules"].get(module, {"status": "missing", "reasons": []})
        payload = self._build_module_payload(kind, eid, module, t, ns, snap, params or {})
        # 数据漂移检测：重投影 hash 与冻结快照不一致 → 提示刷新（不静默替换）
        refresh = False
        try:
            current, _ = self.open(
                kind, eid, as_of=t, namespace=ns, mode=ctx["mode"], name=entity.get("name", "")
            )
            refresh = current["data_hash"] != snap.get("data_hash")
        except Exception:
            logger.warning("模块漂移检测失败（忽略）%s", snapshot_id, exc_info=True)
        out = ModulePayload(
            module=module,
            snapshot_id=snapshot_id,
            status=state.get("status", "missing"),
            reasons=state.get("reasons", []),
            as_of=ctx["as_of"],
            payload=payload,
        )
        if refresh:
            out.reasons = [*out.reasons, "底层数据已有更新（刷新可切到新快照）"]
        return out

    def _build_module_payload(
        self,
        entity_kind: str,
        entity_id: str,
        module: str,
        t: datetime,
        ns: str,
        snap: dict[str, Any],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        facts = self._kb.view(entity_kind, entity_id, t, namespace=ns)
        observations = self._metrics.observations_as_of(entity_kind, entity_id, t, namespace=ns)
        claims = self._metrics.claims_as_of(entity_kind, entity_id, t, namespace=ns)
        conflicted = set(self._metrics.conflicted_semantic_hashes(entity_kind, entity_id, namespace=ns))
        resolutions = {
            r.semantic_hash
            for r in self._metrics.resolutions_as_of(entity_kind, entity_id, t, namespace=ns)
        }
        conflicted -= resolutions
        frequency = params.get("frequency")  # 白名单视图参数：metric/frequency

        if module == "investment_snapshot":
            return {
                "summary": snap.get("summary", {}),
                "claims": [_claim_item(c) for c in claims[:20]],
                "assessment": self._latest_assessment(entity_kind, entity_id, ns, t),
            }
        if module == "business_engine":
            graph = business_graph(facts, claims)
            return {"graph": graph.model_dump(mode="json")}
        if module == "revenue_segments":
            seg_obs = [o for o in observations if o.dimensions.get("segment")]
            segments = sorted({o.dimensions["segment"] for o in seg_obs})
            series = []
            for seg in segments:
                subset = [o for o in seg_obs if o.dimensions.get("segment") == seg]
                keys = sorted({o.metric_key for o in subset})
                s = series_set(subset, keys, frequency=frequency, conflicted_sems=conflicted)
                for ms in s.series:
                    ms.label = f"{seg} · {ms.metric_key}"
                series.extend(s.series)
            total = series_set(
                [o for o in observations if o.metric_key == "revenue" and not o.dimensions],
                ["revenue"], {"revenue": "总收入"}, frequency=frequency, conflicted_sems=conflicted,
            )
            notes = []
            if segments and total.series:
                notes.append("分部合计与总收入不一致时以未分配/抵销项解释（§6.4.10）")
            if not segments:
                notes.append("无分部观测——仅展示总收入（缺分部不编造拆分）")
            return {
                "total": total.model_dump(mode="json"),
                "segments": [s.model_dump(mode="json") for s in series],
                "notes": notes,
            }
        if module == "key_kpi":
            from ..research.plan import load_recipe

            recipe_id = snap.get("recipe", {}).get("id", "general")
            try:
                recipe = load_recipe(recipe_id)
            except FileNotFoundError:
                recipe = load_recipe("general")
            keys = [k.key for k in recipe.kpis]
            labels = {k.key: k.label for k in recipe.kpis}
            s = series_set(observations, keys, labels, frequency=frequency, conflicted_sems=conflicted)
            missing = [k for k in keys if k not in {x.metric_key for x in s.series}]
            for m in missing:
                s.notes.append(f"KPI 缺口: {labels.get(m, m)}（未披露项保留缺口，不猜数）")
            return {"series_set": s.model_dump(mode="json"),
                    "kpi_definitions": [k.model_dump() for k in recipe.kpis]}
        if module == "financial_quality":
            keys = ["revenue", "gross_profit", "operating_income", "net_income", "ebitda",
                    "cfo", "capex", "fcf", "net_debt", "cash", "total_debt"]
            labels = {"revenue": "收入", "gross_profit": "毛利", "operating_income": "营业利润",
                      "net_income": "净利润", "cfo": "经营现金流", "capex": "资本开支",
                      "fcf": "自由现金流(CFO−Capex)", "net_debt": "净债务", "cash": "现金",
                      "total_debt": "有息负债", "ebitda": "EBITDA"}
            fy = series_set(
                observations, keys, labels, frequency=frequency or "FY", conflicted_sems=conflicted
            )
            q = series_set(observations, keys, labels, frequency="Q", conflicted_sems=conflicted)
            calcs = self._calculations(entity_kind, entity_id, ns, t)
            legacy = [
                f.model_dump(mode="json")
                for f in legacy_fact_items(self._kb, facts)
                if f.field in ("revenue_fy", "net_income_fy", "cash_flow")
            ]
            notes = ["毛利→营业利润→FCF 不是恒等链：桥接项（税/非现金/营运资本/Capex）见计算引用"]
            return {
                "fy": fy.model_dump(mode="json"),
                "quarterly": q.model_dump(mode="json"),
                "calculations": calcs,
                "legacy": legacy,
                "notes": notes,
            }
        if module == "expectations":
            g = [o for o in observations if o.nature in ("guidance", "consensus")]
            actuals = [o for o in observations if o.nature == "reported"]
            calcs = [c for c in self._calculations(entity_kind, entity_id, ns, t)
                     if c.get("formula_id") == "guidance_delta"]
            notes = []
            if not any(o.nature == "consensus" for o in g):
                notes.append("无 consensus 快照——只比较公司指引（缺快照不可回填）")
            if not g:
                notes.append("本模块数据能力缺口：无指引/一致预期观测")
            return {
                "guidance_consensus": series_set(g, sorted({o.metric_key for o in g}),
                                                 conflicted_sems=conflicted).model_dump(mode="json"),
                "actuals": series_set(actuals, sorted({o.metric_key for o in actuals})[:6],
                                      conflicted_sems=conflicted).model_dump(mode="json"),
                "guidance_delta": calcs,
                "notes": notes,
            }
        if module == "valuation_lab":
            calcs = self._calculations(entity_kind, entity_id, ns, t)
            model_calcs = [c for c in calcs if c.get("formula_id") in
                           ("reverse_dcf", "sensitivity_grid", "enterprise_value", "margin")]
            legacy = [
                f.model_dump(mode="json")
                for f in legacy_fact_items(self._kb, facts) if f.field == "valuation"
            ]
            notes = [
                "反向求解显示「在这些假设下价格隐含的增长率」，不是唯一反推（§8.3）",
                "买卖评级/目标价区间归 /decide——本页只呈现研究假设与已披露倍数（D1 边界）",
            ]
            return {"calculations": model_calcs, "legacy": legacy, "notes": notes}
        if module == "peers":
            legacy = [
                f.model_dump(mode="json")
                for f in legacy_fact_items(self._kb, facts)
                if f.field in ("peers", "moat", "market_share", "competition", "player_landscape")
            ]
            peer_obs = self._peer_observations(entity_kind, entity_id, facts, t, ns)
            notes = []
            if not peer_obs:
                notes.append("无可比口径的同业 typed 观测——比较表降级（不满足同截止时点/期间口径不硬比）")
            return {"legacy": legacy, "peer_series": peer_obs, "notes": notes}
        if module == "catalysts_risks":
            legacy = [
                f.model_dump(mode="json")
                for f in legacy_fact_items(self._kb, facts)
                if f.field in ("risks", "catalysts", "counter_evidence", "policy")
            ]
            risk_claims = [
                _claim_item(c) for c in claims
                if c.get("question_id") in ("catalysts-risks", "counter-evidence",
                                            "customer-concentration", "regulatory-path", "policy")
            ]
            return {"legacy": legacy, "claims": risk_claims}
        if module == "research_sources":
            artifacts = self._metrics.artifacts_as_of(entity_kind, entity_id, t, namespace=ns)
            plans = self._metrics.plans_for(entity_kind, entity_id, namespace=ns, limit=5)
            evidences = evidence_items(self._kb, facts, observations, claims)
            legacy = legacy_fact_items(self._kb, facts)
            conflicts = [
                {"semantic_hash": h, "history": [
                    o.model_dump(mode="json")
                    for o in self._metrics.observation_history(h, namespace=ns)
                ]}
                for h in sorted(conflicted)
            ]
            fact_conflicts = [
                f.model_dump(mode="json") for f in legacy if f.conflict
            ]
            return {
                "artifacts": [
                    {k: a.get(k) for k in ("artifact_id", "title", "status", "sufficiency",
                                           "created_at", "plan_id", "run_id")}
                    for a in artifacts
                ],
                "plans": [
                    {k: p.get(k) for k in ("plan_id", "mode", "objective", "recipe_id",
                                           "status", "created_at", "questions")}
                    for p in plans
                ],
                "claims": [_claim_item(c) for c in claims],
                "evidence": [e.model_dump(mode="json") for e in evidences],
                "legacy_facts": [f.model_dump(mode="json") for f in legacy],
                "observation_conflicts": conflicts,
                "fact_conflicts": fact_conflicts,
                "assessment": self._latest_assessment(entity_kind, entity_id, ns, t),
            }
        raise DossierError(f"未知模块 {module!r}", status=422)

    # ---------------- changes / export ----------------

    def changes(self, snapshot_id: str, baseline_snapshot_id: str) -> dict[str, Any]:
        cur = self.get(snapshot_id)
        base = self.get(baseline_snapshot_id)
        if (cur["entity"]["kind"], cur["entity"]["id"]) != (base["entity"]["kind"], base["entity"]["id"]):
            raise DossierError("两个快照不属于同一实体，无法比较", status=422)
        changed_modules = _changed_modules(base, cur)
        base_claims = {c["claim_id"] for c in self._claims_of(base)}
        cur_claims = {c["claim_id"] for c in self._claims_of(cur)}
        base_arts = set(base.get("research", {}).get("artifact_refs", []))
        cur_arts = set(cur.get("research", {}).get("artifact_refs", []))
        return {
            "baseline": {"snapshot_id": baseline_snapshot_id, "as_of": base["context"]["as_of"],
                         "generated_at": base["context"]["generated_at"]},
            "current": {"snapshot_id": snapshot_id, "as_of": cur["context"]["as_of"],
                        "generated_at": cur["context"]["generated_at"]},
            "changed_modules": changed_modules,
            "new_claims": sorted(cur_claims - base_claims),
            "removed_claims": sorted(base_claims - cur_claims),
            "new_artifacts": sorted(cur_arts - base_arts),
            "key_metric_changes": _key_metric_changes(base, cur),
        }

    def export(self, snapshot_id: str, fmt: str = "json") -> tuple[str, str]:
        """冻结导出（§11.4 第一期 JSON+Markdown）：返回 (文件名, 内容)。"""
        snap = self.get(snapshot_id)
        entity = snap["entity"]
        stamp = snap["context"]["as_of"][:10]
        if fmt == "json":
            return (
                f"dossier-{entity['id']}-{stamp}.json",
                json.dumps(snap, ensure_ascii=False, indent=2),
            )
        if fmt == "markdown":
            from .export import render_snapshot_markdown

            return (
                f"dossier-{entity['id']}-{stamp}.md",
                render_snapshot_markdown(snap, self._kb, self._metrics),
            )
        raise DossierError(f"未知导出格式 {fmt!r}（第一期支持 json/markdown）", status=422)

    # ---------------- 实体列表（v2） ----------------

    def entities(self, *, namespace: str = "prod", as_of: datetime | None = None) -> list[dict[str, Any]]:
        """档案库列表投影：合并旧 KB 实体与 typed 观测实体 + 研究覆盖 + 数据状态。"""
        from ..knowledge.gaps import GapAnalyzer
        from ..knowledge.verify import verify_entity

        t = as_of or datetime.now(UTC)
        rows = self._kb._conn.execute(  # noqa: SLF001 - 投影层只读聚合
            "SELECT entity_kind, entity_id, COUNT(DISTINCT field), MAX(knowledge_time) FROM facts"
            " WHERE namespace = ? AND knowledge_time <= ? GROUP BY entity_kind, entity_id",
            (namespace, t.isoformat()),
        ).fetchall()
        known = {(r[0], r[1]) for r in rows}
        for kind, eid in self._metrics.list_entities_with_snapshots(namespace=namespace):
            if (kind, eid) not in known:
                rows.append((kind, eid, 0, None))
        analyzer = GapAnalyzer(self._kb)
        out: list[dict[str, Any]] = []
        for r in rows:
            kind, eid = r[0], r[1]
            g = analyzer.analyze(kind, eid, t, namespace=namespace)
            q = verify_entity(self._kb, kind, eid, t, namespace=namespace)
            plans = self._metrics.plans_for(kind, eid, namespace=namespace, limit=1)
            plan = plans[0] if plans else None
            artifacts = self._metrics.artifacts_as_of(kind, eid, t, namespace=namespace)
            obs = self._metrics.observations_as_of(kind, eid, t, namespace=namespace)
            coverage = {"answered": 0, "required": 0, "verdict": None}
            if plan:
                qs = [x for x in plan.get("questions", []) if x.get("status") != "not_applicable"]
                coverage = {
                    "answered": sum(1 for x in qs if x.get("status") == "answered"),
                    "required": len(qs),
                    "verdict": self._assessment_verdict(kind, eid, ns=namespace),
                }
            latest = self._metrics.latest_snapshot(kind, eid, namespace=namespace)
            out.append({
                "kind": kind,
                "id": eid,
                "field_count": r[2],
                "last_knowledge_time": r[3],
                "completeness": g.completeness,
                "stale_count": len(g.stale),
                "conflict_count": len(g.conflicts),
                "quality_score": q.quality_score,
                "quality_status": q.status,
                "quality_issues": q.issues,
                "observation_count": len(obs),
                "artifact_count": len(artifacts),
                "latest_artifact_title": artifacts[0].get("title") if artifacts else None,
                "research_coverage": coverage,
                "recipe_id": (latest or {}).get("recipe", {}).get("id"),
                "snapshot_id": (latest or {}).get("context", {}).get("snapshot_id"),
            })
        out.sort(key=lambda e: (e["kind"], e["id"]))
        return out

    # ---------------- 内部 ----------------

    def _claims_of(self, snap: dict[str, Any]) -> list[dict[str, Any]]:
        t = datetime.fromisoformat(snap["context"]["as_of"])
        return self._metrics.claims_as_of(
            snap["entity"]["kind"], snap["entity"]["id"], t, namespace=snap["context"]["namespace"]
        )

    def _calculations(self, kind: str, eid: str, ns: str, t: datetime) -> list[dict[str, Any]]:
        rows = self._metrics._conn.execute(  # noqa: SLF001 - 投影层只读
            "SELECT payload_json FROM calculation_runs WHERE namespace = ? AND entity_kind = ?"
            " AND entity_id = ? AND created_at <= ? ORDER BY created_at DESC LIMIT 50",
            (ns, kind, eid, t.isoformat()),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def _latest_assessment(self, kind: str, eid: str, ns: str, t: datetime) -> dict | None:
        if self._events is None:
            return None
        rows = self._events._conn.execute(  # noqa: SLF001 - 投影层只读
            "SELECT payload FROM events WHERE type = 'research/assessment'"
            " AND json_extract(payload, '$.entity') = ? ORDER BY seq DESC LIMIT 1",
            (f"{kind}:{eid}",),
        ).fetchall()
        if not rows:
            return None
        payload = json.loads(rows[0][0]) if isinstance(rows[0][0], str) else rows[0][0]
        created = payload.get("created_at", "")
        if created and created > t.isoformat():
            return None  # 历史视图不借用未来评估
        return payload

    def _assessment_verdict(self, kind: str, eid: str, *, ns: str) -> str | None:
        a = self._latest_assessment(kind, eid, ns, datetime.now(UTC))
        return (a or {}).get("verdict")

    def _peer_observations(
        self, entity_kind: str, entity_id: str, facts: dict, t: datetime, ns: str
    ) -> list[dict[str, Any]]:
        """同业比较：peers 字段给出代码 → 取各实体同 metric 的最新观测（同截止时点）。"""
        peers_rec = facts.get("peers")
        if peers_rec is None:
            return []
        from ..knowledge.normalize import normalize_entity_id

        candidates: list[str] = []
        value = peers_rec.value
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    candidates.extend(str(v) for v in item.values() if isinstance(v, str))
                elif isinstance(item, str):
                    candidates.append(item)
        elif isinstance(value, str):
            candidates = re_split_peers(value)
        out = []
        for raw in candidates:
            pid = normalize_entity_id("stock", raw.strip())
            if not pid or pid == entity_id:
                continue
            obs = self._metrics.observations_as_of("stock", pid, t, namespace=ns)
            if obs:
                out.append({
                    "entity_id": pid,
                    "metrics": [
                        {"metric_key": o.metric_key, "value": o.value, "unit": o.unit,
                         "period_label": o.period.fiscal_label or o.period.end.isoformat(),
                         "observation_id": o.observation_id}
                        for o in obs if o.status == "ok"
                    ][:8],
                })
        return out

    def _emit(self, run_id: str, type_: str, payload: dict[str, Any]) -> None:
        if self._events is not None:
            self._events.append(Event(run_id=run_id, type=type_, payload=payload))


def re_split_peers(text: str) -> list[str]:
    import re

    return [p for p in re.split(r"[,，;；、\s]+", text) if p and len(p) <= 12]


def _claim_item(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": c.get("claim_id"),
        "statement": c.get("statement"),
        "kind": c.get("kind"),
        "status": c.get("status"),
        "question_id": c.get("question_id"),
        "support_refs": c.get("support_refs", []),
        "counter_refs": c.get("counter_refs", []),
        "limitations": c.get("limitations", []),
        "created_at": c.get("created_at", ""),
        "legacy": bool(c.get("legacy_field")),
    }


def _changed_modules(base: dict | None, cur: dict) -> list[str]:
    if base is None:
        return [m for m, s in cur.get("modules", {}).items() if s.get("status") != "missing"]
    out = []
    for m, s in cur.get("modules", {}).items():
        old = (base.get("modules") or {}).get(m, {})
        if old.get("status") != s.get("status"):
            out.append(m)
    return out


def _key_metric_changes(base: dict, cur: dict) -> list[dict[str, Any]]:
    old = {m["metric_key"]: m for m in base.get("summary", {}).get("key_metrics", [])}
    out = []
    for m in cur.get("summary", {}).get("key_metrics", []):
        prev = old.get(m["metric_key"])
        if prev is None or prev.get("value") != m.get("value") or prev.get("status") != m.get("status"):
            out.append({
                "metric_key": m["metric_key"],
                "old_value": (prev or {}).get("value"),
                "new_value": m.get("value"),
                "old_status": (prev or {}).get("status"),
                "new_status": m.get("status"),
            })
    return out
