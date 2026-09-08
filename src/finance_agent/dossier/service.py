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
            if not created:
                # 数据未变 → 复用已冻结快照（保留原 as_of/生成时刻，不在阅读中悄悄替换）
                stored = self._metrics.get_snapshot(snapshot_id)
                if stored is not None:
                    return stored, False
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
        # 数据漂移检测：同一上下文重投影（不落库），hash 不一致 = 有回填数据 → 提示刷新
        # （只读请求不发布新快照；新快照由用户重新打开档案时创建）
        refresh = False
        try:
            from .models import DossierContext

            ctx_model = DossierContext.model_validate(snap["context"])
            fresh = self._projector.project(kind, eid, ctx_model, name=entity.get("name", ""))
            refresh = fresh.data_hash != snap.get("data_hash")
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

    def _frozen_view(self, snap: dict[str, Any]) -> dict[str, Any]:
        """按快照冻结的输入版本集读数据（review #2）：模块/来源/序列请求不再
        重查当前库——补录历史观测不会改变已冻结快照的任何模块内容。

        旧快照（契约升级前，无 inputs）回退 as_of 查询并标注 frozen=False。"""
        inputs = snap.get("inputs") or {}
        ctx = snap["context"]
        t = datetime.fromisoformat(ctx["as_of"])
        ns = ctx["namespace"]
        kind, eid = snap["entity"]["kind"], snap["entity"]["id"]
        frozen = "observation_ids" in inputs or "fact_ids" in inputs
        if not frozen:
            return {
                "frozen": False,
                "facts": self._kb.view(kind, eid, t, namespace=ns),
                "observations": self._metrics.observations_as_of(kind, eid, t, namespace=ns),
                "claims": self._metrics.claims_as_of(kind, eid, t, namespace=ns),
                "artifacts": self._metrics.artifacts_as_of(kind, eid, t, namespace=ns),
                "conflicted": set(self._metrics.conflicted_semantic_hashes(
                    kind, eid, namespace=ns, as_of=t, exclude_resolved=True)),
                "calculation_ids": self._metrics.list_calculation_ids(kind, eid, t, namespace=ns),
                "plans": self._metrics.plans_for(kind, eid, namespace=ns, limit=5, as_of=t),
                "plan_status_reliable": True,
            }
        facts: dict[str, Any] = {}
        for field, fact_id in (inputs.get("fact_ids") or {}).items():
            rec = self._kb.get_fact(fact_id)
            if rec is not None:
                facts[field] = rec
        observations = []
        for oid in inputs.get("observation_ids") or []:
            obs = self._metrics.get_observation(oid)
            if obs is not None:
                observations.append(obs)
        observations.sort(key=lambda o: (o.metric_key, o.period.end, o.period.frequency))
        plan = inputs.get("plan")
        return {
            "frozen": True,
            "facts": facts,
            "observations": observations,
            "claims": list(inputs.get("claims") or []),
            "artifacts": list(inputs.get("artifacts") or []),
            "conflicted": set(inputs.get("conflicted_semantic_hashes") or []),
            "calculation_ids": list(inputs.get("calculation_ids") or []),
            "plans": [plan] if plan else [],
            "plan_status_reliable": bool(inputs.get("plan_status_reliable", True)),
        }

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
        view = self._frozen_view(snap)
        facts = view["facts"]
        observations = view["observations"]
        claims = view["claims"]
        conflicted = view["conflicted"]
        frequency = params.get("frequency")  # 白名单视图参数：metric/frequency
        frozen_note = [] if view["frozen"] else [
            "旧快照（无冻结输入清单）：按 as_of 重查，回填数据可能影响一致性"
        ]

        if module == "investment_snapshot":
            return {
                "summary": snap.get("summary", {}),
                "claims": [_claim_item(c) for c in claims[:20]],
                "assessment": self._latest_assessment(entity_kind, entity_id, ns, t),
                "notes": frozen_note,
            }
        if module == "business_engine":
            graph = business_graph(facts, claims, entity_kind=entity_kind)
            return {"graph": graph.model_dump(mode="json"), "notes": frozen_note}
        if module == "revenue_segments":
            seg_obs = [o for o in observations if o.dimensions.get("segment")]
            segments = sorted({o.dimensions["segment"] for o in seg_obs})
            # series_set 按完整语义键拆分（含 dimensions），分部不会混进合并序列
            seg_series = series_set(
                seg_obs, sorted({o.metric_key for o in seg_obs}),
                frequency=frequency, conflicted_sems=conflicted,
            )
            total = series_set(
                [o for o in observations if o.metric_key == "revenue" and not o.dimensions],
                ["revenue"], {"revenue": "总收入"}, frequency=frequency, conflicted_sems=conflicted,
                natures=("reported", "calculated"),
            )
            notes = list(frozen_note)
            if segments and total.series:
                notes.append("分部合计与总收入不一致时以未分配/抵销项解释（§6.4.10）")
            if not segments:
                notes.append("无分部观测——仅展示总收入（缺分部不编造拆分）")
            return {
                "total": total.model_dump(mode="json"),
                "segments": [s.model_dump(mode="json") for s in seg_series.series],
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
            # 主图只画合并口径的披露/计算值；指引/预期/分部各自归对应模块（review #15）
            consolidated = [o for o in observations if not o.dimensions]
            s = series_set(consolidated, keys, labels, frequency=frequency,
                           conflicted_sems=conflicted, natures=("reported", "calculated"))
            missing = [k for k in keys if k not in {x.metric_key for x in s.series}]
            for m in missing:
                s.notes.append(f"KPI 缺口: {labels.get(m, m)}（未披露项保留缺口，不猜数）")
            s.notes.extend(frozen_note)
            return {"series_set": s.model_dump(mode="json"),
                    "kpi_definitions": [k.model_dump() for k in recipe.kpis]}
        if module == "financial_quality":
            keys = ["revenue", "gross_profit", "operating_income", "net_income", "ebitda",
                    "cfo", "capex", "fcf", "net_debt", "cash", "total_debt"]
            labels = {"revenue": "收入", "gross_profit": "毛利", "operating_income": "营业利润",
                      "net_income": "净利润", "cfo": "经营现金流", "capex": "资本开支",
                      "fcf": "自由现金流(CFO−Capex)", "net_debt": "净债务", "cash": "现金",
                      "total_debt": "有息负债", "ebitda": "EBITDA"}
            consolidated = [
                o for o in observations
                if not o.dimensions and o.nature in ("reported", "calculated")
            ]
            fy = series_set(
                consolidated, keys, labels, frequency=frequency or "FY", conflicted_sems=conflicted
            )
            q = series_set(consolidated, keys, labels, frequency="Q", conflicted_sems=conflicted)
            calcs = self._calculations_by_ids(view["calculation_ids"], t)
            legacy = [
                f.model_dump(mode="json")
                for f in legacy_fact_items(self._kb, facts)
                if f.field in ("revenue_fy", "net_income_fy", "cash_flow")
            ]
            notes = ["毛利→营业利润→FCF 不是恒等链：桥接项（税/非现金/营运资本/Capex）见计算引用",
                     *frozen_note]
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
            calcs = [c for c in self._calculations_by_ids(view["calculation_ids"], t)
                     if c.get("formula_id") == "guidance_delta"]
            notes = list(frozen_note)
            if not any(o.nature == "consensus" for o in g):
                notes.append("无 consensus 快照——只比较公司指引（缺快照不可回填）")
            if not g:
                notes.append("本模块数据能力缺口：无指引/一致预期观测")
            return {
                # guidance/consensus 各自成序列（nature 进语义键，review #15）
                "guidance_consensus": series_set(g, sorted({o.metric_key for o in g}),
                                                 conflicted_sems=conflicted).model_dump(mode="json"),
                "actuals": series_set(actuals, sorted({o.metric_key for o in actuals})[:6],
                                      conflicted_sems=conflicted).model_dump(mode="json"),
                "guidance_delta": calcs,
                "notes": notes,
            }
        if module == "valuation_lab":
            calcs = self._calculations_by_ids(view["calculation_ids"], t)
            model_calcs = [c for c in calcs if c.get("formula_id") in
                           ("reverse_dcf", "sensitivity_grid", "enterprise_value", "margin")]
            legacy = [
                f.model_dump(mode="json")
                for f in legacy_fact_items(self._kb, facts) if f.field == "valuation"
            ]
            notes = [
                "反向求解显示「在这些假设下价格隐含的增长率」，不是唯一反推（§8.3）",
                "买卖评级/目标价区间归 /decide——本页只呈现研究假设与已披露倍数（D1 边界）",
                *frozen_note,
            ]
            return {"calculations": model_calcs, "legacy": legacy, "notes": notes}
        if module == "peers":
            legacy = [
                f.model_dump(mode="json")
                for f in legacy_fact_items(self._kb, facts)
                if f.field in ("peers", "moat", "market_share", "competition", "player_landscape")
            ]
            # 同业观测是跨实体视图（各自独立快照语义）：按同一 as_of 查询，不属于本快照冻结集
            peer_obs = self._peer_observations(entity_kind, entity_id, facts, t, ns)
            notes = list(frozen_note)
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
            return {"legacy": legacy, "claims": risk_claims, "notes": frozen_note}
        # 注：entity_kind/entity_id 仅供回退路径与 peers 跨实体查询使用；
        # 其余模块一律消费冻结视图（review #2）
        if module == "research_sources":
            artifacts = view["artifacts"]
            # 用户情景不是默认发布产物（review #26）：与研究报告分区展示
            report_arts = [a for a in artifacts if a.get("purpose", "report") == "report"]
            scenario_arts = [a for a in artifacts if a.get("purpose") == "scenario"]
            plans = view["plans"]
            evidences = evidence_items(self._kb, facts, observations, claims)
            legacy = legacy_fact_items(self._kb, facts)
            # 冲突历史按截止时点过滤（review #3）：历史页面不泄露未来重述版本
            conflicts = [
                {"semantic_hash": h, "history": [
                    o.model_dump(mode="json")
                    for o in self._metrics.observation_history(h, namespace=ns, as_of=t)
                ]}
                for h in sorted(conflicted)
            ]
            fact_conflicts = [
                f.model_dump(mode="json") for f in legacy if f.conflict
            ]
            plan_notes = list(frozen_note)
            if not view["plan_status_reliable"]:
                plan_notes.append(
                    "计划问题状态在 as_of 后被更新过：历史投影不可分辨当时进展，"
                    "状态显示为 historical_unknown（不借用今日状态）"
                )
            return {
                "artifacts": [
                    {k: a.get(k) for k in ("artifact_id", "title", "status", "sufficiency",
                                           "created_at", "plan_id", "run_id", "purpose")}
                    for a in report_arts
                ],
                "scenarios": [
                    {k: a.get(k) for k in ("artifact_id", "title", "status", "created_at")}
                    for a in scenario_arts
                ],
                "plans": [
                    {k: p.get(k) for k in ("plan_id", "mode", "objective", "recipe_id",
                                           "status", "created_at", "questions")}
                    for p in plans
                ],
                "plan_notes": plan_notes,
                "claims": [_claim_item(c) for c in claims],
                "evidence": [e.model_dump(mode="json") for e in evidences],
                "legacy_facts": [f.model_dump(mode="json") for f in legacy],
                "observation_conflicts": conflicts,
                "fact_conflicts": fact_conflicts,
                "assessment": self._latest_assessment(entity_kind, entity_id, ns, t),
            }
        raise DossierError(f"未知模块 {module!r}", status=422)

    def frozen_series(
        self, snapshot_id: str, *, metric: str = "", frequency: str = ""
    ) -> dict[str, Any]:
        """规范化序列（同一冻结快照，review #2）：按快照输入版本集读取，
        缺期/重述/冲突显式返回；完整语义键拆分（分部/指引不混入实际值）。"""
        from .projector import series_set

        snap = self.get(snapshot_id)
        view = self._frozen_view(snap)
        observations = view["observations"]
        if metric:
            observations = [o for o in observations if o.metric_key == metric]
        if frequency:
            observations = [o for o in observations if o.period.frequency == frequency]
        s = series_set(
            observations, sorted({o.metric_key for o in observations}),
            conflicted_sems=view["conflicted"],
        )
        out = s.model_dump(mode="json")
        out.update({
            "snapshot_id": snapshot_id,
            "as_of": snap["context"]["as_of"],
            "frozen": view["frozen"],
        })
        return out

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
        """快照的论断集：优先冻结 inputs（review #2），旧快照回退 as_of 查询。"""
        inputs = snap.get("inputs") or {}
        if "claims" in inputs:
            return list(inputs.get("claims") or [])
        t = datetime.fromisoformat(snap["context"]["as_of"])
        return self._metrics.claims_as_of(
            snap["entity"]["kind"], snap["entity"]["id"], t, namespace=snap["context"]["namespace"]
        )

    def _calculations_by_ids(self, calc_ids: list[str], t: datetime) -> list[dict[str, Any]]:
        """冻结计算 id 集 → payload（只取 created_at ≤ as_of 的，双重保险）。"""
        out: list[dict[str, Any]] = []
        for cid in calc_ids:
            stored = self._metrics.get_calculation(cid)
            if stored is None:
                continue
            created = stored.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if t.tzinfo is None:
                t = t.replace(tzinfo=UTC)
            if created > t:
                continue
            out.append(stored.payload)
        out.sort(key=lambda c: c.get("created_at", ""), reverse=True)
        return out

    def _latest_assessment(self, kind: str, eid: str, ns: str, t: datetime) -> dict | None:
        """最新评估投影（review #11）：命名空间过滤（eval 评估不泄漏进生产档案），
        时间过滤在 SQL 内完成（存在未来评估时返回当时最新一条，而非直接消失）。"""
        if self._events is None:
            return None
        rows = self._events._conn.execute(  # noqa: SLF001 - 投影层只读
            "SELECT payload FROM events WHERE type = 'research/assessment'"
            " AND json_extract(payload, '$.entity') = ?"
            " AND json_extract(payload, '$.namespace') = ?"
            " AND json_extract(payload, '$.created_at') <= ?"
            " ORDER BY seq DESC LIMIT 1",
            (f"{kind}:{eid}", ns, t.isoformat()),
        ).fetchall()
        if not rows:
            return None
        return json.loads(rows[0][0]) if isinstance(rows[0][0], str) else rows[0][0]

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
        # 状态变化或内容指纹变化（data_ref）都算变化——partial→partial 但数据更新也要提示
        if old.get("status") != s.get("status") or old.get("data_ref") != s.get("data_ref"):
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
