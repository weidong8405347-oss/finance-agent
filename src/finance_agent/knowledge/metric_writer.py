"""TypedMetricWriter：typed 观测的单写者入口（设计 §5/§6.2/§6.5）。

与 ProfileWriter（旧 Fact 通道）并行，纪律一致：
- 写入即事件：metric/asserted 携带完整不可变 payload（事件可重建索引）；
- 硬门禁 fail-closed：
  1. 证据可解析（reported/guidance 绑定的 evidence_refs 必须已登记）；
  2. 换算血缘可重算（raw + normalization 步骤 → value 逐步一致）；
  3. calculated 必须引用已登记 CalculationRun；model_estimate 必须引用已冻结 artifact；
  4. knowledge_time ≥ 所绑证据的最晚 available_at（时态不变量）；
  5. eval 模式逐条校验证据 available_at ≤ eval_as_of（防线 2）；
  6. 嵌套数值逐叶校验（assumptions/dimensions 不许裸数值）。
- 拒绝路径三通道：hook/verdict 事件 + 日志 + 异常上浮（工程约定 1）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..eventstore.events import HOOK_VERDICT, LEAKAGE_ATTEMPT, METRIC_REVISED, Event
from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest, RunMode
from .errors import KnowledgeInvariantError, KnowledgeLeakError, MissingEvidenceError
from .metric_spec import MetricSpecError, check_observation, spec_for
from .metric_store import MetricStore
from .metrics import MetricObservation
from .normalization import (
    NormalizationError,
    assert_magnitude_bound,
    assert_typed_leaves,
    assert_value_context,
    detect_scale_word,
    recompute_lineage,
)
from .store import BitemporalStore

logger = logging.getLogger("finance_agent.knowledge.metrics")

METRIC_ASSERTED = "metric/asserted"

#: 不可作为数值来源的抽取质量（audit §3.2/§3.5）
_UNUSABLE_QUALITY = frozenset({"garbled", "needs_ocr"})

#: 主体授权解析器：(scope_kind, scope_id, subject_kind, subject_id) → (允许, 依据/原因)
SubjectGate = Callable[[str, str, str, str], tuple[bool, str]]


def _unit_context(obs, evidences) -> list[str]:
    """从观测/证据的 locator 里取表头与单位声明（量级绑定的第三类定位依据）。

    财报表常见形态：正文只有 `106,303`，量表写在表头「单位：千元」——
    两者分开存放不是可疑信号，但必须可定位到同一份文档的表/页。
    """
    keys = ("header", "unit", "units", "scale", "table", "column", "caption")
    out: list[str] = []
    for locator in (obs.locator, *(getattr(e, "locator", {}) or {} for e in evidences)):
        for key in keys:
            value = (locator or {}).get(key)
            if value and str(value).strip():
                out.append(str(value))
    return out


class TypedMetricWriter:
    def __init__(
        self, *, store: MetricStore, kb: BitemporalStore, events: EventStore | None = None,
        subject_gate: SubjectGate | None = None,
    ):
        self._store = store
        self._kb = kb
        self._events = events
        #: 跨主体引用授权（audit §3.2）：None = 不允许跨主体（主体必等于研究实体）
        self._subject_gate = subject_gate

    def write_observation(
        self, obs: MetricObservation, *, run: RunManifest, namespace: str = "prod"
    ) -> tuple[str, bool]:
        """门禁全过 → 落索引 + 落事件。返回 (observation_id, created)。"""
        try:
            self._gate(obs, run=run, namespace=namespace)
        except Exception:
            self._verdict(run.run_id, "typed-metric-gate", obs.metric_key, "观测未过 typed 门禁")
            logger.warning(
                "typed 观测拒写 %s:%s %s", obs.entity_kind, obs.entity_id, obs.metric_key,
                exc_info=True,
            )
            raise
        observation_id, created = self._store.assert_observation(obs, namespace=namespace)
        payload = obs.model_dump(mode="json")
        payload["observation_id"] = observation_id
        self._emit(
            METRIC_ASSERTED,
            run.run_id,
            {
                "observation_id": observation_id,
                "namespace": namespace,
                "entity": f"{obs.entity_kind}:{obs.entity_id}",
                "metric_key": obs.metric_key,
                "nature": obs.nature,
                "semantic_hash": obs.semantic_hash(),
                "created": created,
                # 完整不可变 payload（§6.5：事件是重建依据）
                "observation": payload,
            },
        )
        return observation_id, created

    # ---------------- 修订/失效（audit §3.2 修复方案 6） ----------------

    def revise_observation(
        self,
        observation_id: str,
        *,
        action: str,
        reason: str,
        run: RunManifest,
        namespace: str = "prod",
        replacement_observation_id: str | None = None,
    ) -> dict:
        """对已落库观测建立修订/失效记录，并回出需重审的下游依赖。

        纪律：不原位修改冻结历史——旧行与旧 payload 全部保留（审计链），
        失效以追加修订行 + 投影过滤实现；依赖该观测的计算/论断/产物列为待重审。
        """
        obs = self._store.get_observation(observation_id)
        if obs is None:
            raise MissingEvidenceError(f"观测不存在: {observation_id}")
        dependents = self._store.refs_to(observation_id, namespace=namespace)
        flat = [f"calc:{c}" for c in dependents["calculations"]]
        flat += [f"claim:{c}" for c in dependents["claims"]]
        flat += [f"artifact:{a}" for a in dependents["artifacts"]]
        revision_id = self._store.save_revision(
            observation_id=observation_id, namespace=namespace, action=action, reason=reason,
            entity_kind=obs.entity_kind, entity_id=obs.entity_id,
            replacement_observation_id=replacement_observation_id,
            dependent_refs=flat,
            payload=obs.model_dump(mode="json"),
            run_id=run.run_id,
        )
        self._emit(
            METRIC_REVISED, run.run_id,
            {
                "revision_id": revision_id,
                "observation_id": observation_id,
                "entity": f"{obs.entity_kind}:{obs.entity_id}",
                "metric_key": obs.metric_key,
                "value": obs.value,
                "action": action,
                "reason": reason,
                "replacement_observation_id": replacement_observation_id,
                "dependent_refs": flat,
                "observation": obs.model_dump(mode="json"),
                "namespace": namespace,
            },
        )
        logger.warning(
            "观测修订 %s %s：%s（待重审依赖 %d 项）",
            action, observation_id, reason, len(flat),
        )
        return {
            "revision_id": revision_id,
            "observation_id": observation_id,
            "action": action,
            "dependent_refs": flat,
            "dependents": dependents,
        }

    # ---------------- 门禁 ----------------

    def _gate(self, obs: MetricObservation, *, run: RunManifest, namespace: str) -> None:
        # 6) 嵌套数值逐叶校验（assumptions/dimensions）
        if obs.nature == "model_estimate":
            assert_typed_leaves(obs.assumptions, path="$.assumptions")  # type: ignore[attr-defined]
        assert_typed_leaves(obs.dimensions, path="$.dimensions")

        # 1) 证据可解析
        evidences = []
        for eid in obs.evidence_refs:
            evidences.append(self._kb.get_evidence(eid))  # raises MissingEvidenceError

        # 1b) 抽取质量（audit §3.2/§3.5）：乱码/扫描件不得支撑正式数值
        if obs.status == "ok":
            bad_quality = [
                (e.evidence_id, getattr(e, "quality", "ok"))
                for e in evidences
                if getattr(e, "quality", "ok") in _UNUSABLE_QUALITY
            ]
            if bad_quality and all(
                getattr(e, "quality", "ok") in _UNUSABLE_QUALITY for e in evidences
            ):
                raise KnowledgeInvariantError(
                    f"观测 {obs.metric_key} 的全部证据抽取质量为 {bad_quality}："
                    "乱码/需 OCR 的正文不得进入指标库（请重新抽取或人工核对后重试，"
                    "或将观测标为 status=missing/unavailable）"
                )

        # 1c) 指标语义准入（audit §3.2 修复方案 2）：经济含义/单位/币种/主体/期间/
        #     维度/值域/定位/摘录语义一次报清
        quotes = [e.verbatim_quote for e in evidences]
        try:
            spec = spec_for(obs.metric_key, unit=obs.unit)
        except MetricSpecError as e:
            raise KnowledgeInvariantError(str(e)) from e
        try:
            check_observation(
                metric_key=obs.metric_key, unit=obs.unit, currency=obs.currency,
                value=obs.value, frequency=obs.period.frequency,
                dimensions=obs.dimensions, subject_kind=obs.subject_kind,
                locator={**{k: str(v) for k, v in obs.locator.items()},
                         **{k: str(v) for k, v in
                            ((evidences[0].locator if evidences else {}) or {}).items()}},
                quotes=quotes, basis=obs.basis,
            )
        except MetricSpecError as e:
            raise KnowledgeInvariantError(str(e)) from e

        # 1d) 主体授权（audit §3.2 修复方案 1）：scope 与 subject 分离，跨主体需授权
        if obs.is_cross_subject:
            if self._subject_gate is None:
                raise KnowledgeInvariantError(
                    f"跨主体观测未装配授权闸：{obs.subject_kind}:{obs.subject_id} ≠ 研究实体 "
                    f"{obs.entity_kind}:{obs.entity_id}（公司财务应写在公司实体上，"
                    "行业通过候选关系读取）"
                )
            ok, reason = self._subject_gate(
                obs.entity_kind, obs.entity_id, obs.subject_kind, obs.subject_id
            )
            if not ok:
                raise KnowledgeInvariantError(f"观测 {obs.metric_key} 主体未授权：{reason}")

        # 2) 换算血缘可重算（有 raw 的观测必须逐步一致；无 raw 只校验 Decimal 合法性——
        #    模型层已保证）+ 量级绑定（review #1：数字+规模词必须在摘录中逐字可定位，
        #    堵「证据 million 提交 billion」与「丢规模词缩小 1000 倍」两类量级事故）
        if obs.raw is not None:
            recompute_lineage(obs)
            if evidences:
                # 表头/单位上下文（audit §3.2）：locator 里的 header/unit/table 声明的量表
                # 与正文数字分开存放是财报正常形态，量级绑定得认它
                unit_ctx = _unit_context(obs, evidences)
                # 2c) 规模词必须已显式换算（基线发现 F10：千元/'000 原样入库 →
                #     同库 1000 倍量表漂移，数值准确率命门）：原文值/单位/表头声明
                #     了规模词而 normalization 没有对应步骤 = 疑漏乘，拒写并给修法。
                #     先于量级绑定执行：拒绝信息更可操作（告知登记换算步骤）
                scale_ctx = [obs.raw.value_text, obs.raw.unit_text, *unit_ctx]
                word = next(
                    (w for w in (detect_scale_word(t, "") for t in scale_ctx) if w is not None),
                    None,
                )
                if word is not None:
                    applied = any(
                        str((s or {}).get("formula_id"))
                        in ("unit_word_scale", "scale_by_power_of_ten")
                        for s in obs.normalization
                    )
                    if not applied:
                        raise KnowledgeInvariantError(
                            f"观测 {obs.metric_key} 的原文/表头含规模词 {word!r}"
                            f"（上下文：{[t for t in scale_ctx if t][:3]}），但 normalization "
                            f"无换算步骤——疑量表漏乘（value={obs.value} 可能偏 10^3/10^6）："
                            "在 normalization 登记 unit_word_scale（服务端重算），"
                            "或修正 value_text/unit_text 使其反映原文真实量表"
                        )
                assert_magnitude_bound(
                    obs.raw.value_text,
                    obs.raw.unit_text,
                    quotes,
                    unit_context=unit_ctx,
                )
                # 2b) 证据上下文准入（audit §3.2 修复方案 3/4）：多数字摘录必须显式
                #     指定 cell/span；金额类必须有规模词或表头定位
                assert_value_context(
                    obs.raw.value_text, obs.raw.unit_text, quotes,
                    value_span=obs.raw.span or None,
                    locator={**obs.locator,
                             **{k: str(v) for k, v in
                                ((evidences[0].locator if evidences else {}) or {}).items()}},
                    value_kind=spec.value_kind,
                )

        # 3) 引用完整性：calculated → CalculationRun 已登记；model_estimate → artifact 已冻结
        if (
            obs.nature == "calculated"
            and obs.calculation_ref
            and self._store.get_calculation(obs.calculation_ref) is None
        ):
            raise MissingEvidenceError(
                f"calculated 观测引用的 calculation 未登记: {obs.calculation_ref}"
            )
        if (
            obs.nature == "model_estimate"
            and obs.artifact_ref
            and self._store.get_artifact(obs.artifact_ref) is None
        ):
            raise MissingEvidenceError(
                f"model_estimate 观测引用的 artifact 未冻结: {obs.artifact_ref}"
            )

        # 4) knowledge_time ≥ max(evidence.available_at)
        known_ats = [e.available_at for e in evidences if e.available_at is not None]
        if known_ats and obs.knowledge_time < max(known_ats):
            raise KnowledgeInvariantError(
                f"观测 {obs.metric_key} 的 knowledge_time 早于其证据的 available_at"
            )

        # 5) eval 防线
        if run.mode is RunMode.EVAL:
            assert run.eval_as_of is not None
            for ev in evidences:
                if ev.available_at is None or ev.available_at > run.eval_as_of:
                    self._emit(
                        LEAKAGE_ATTEMPT,
                        run.run_id,
                        {
                            "reason": "metric_evidence_after_as_of",
                            "evidence_id": ev.evidence_id,
                            "metric_key": obs.metric_key,
                            "namespace": namespace,
                        },
                    )
                    raise KnowledgeLeakError(
                        f"证据 {ev.evidence_id} 越过 eval_as_of={run.eval_as_of.isoformat()}，拒绝落库"
                    )

        # guidance/consensus 的发布时刻一致性：指引发布不能晚于 knowledge_time
        if obs.nature == "guidance":
            published = obs.guidance_published_at  # type: ignore[attr-defined]
            if obs.knowledge_time < published:
                raise KnowledgeInvariantError(
                    f"guidance 观测的 knowledge_time 早于指引发布时刻 {published.isoformat()}"
                )
        if obs.nature == "consensus":
            snapshot_at = obs.consensus_snapshot_at  # type: ignore[attr-defined]
            if obs.knowledge_time < snapshot_at:
                raise KnowledgeInvariantError(
                    "consensus 观测的 knowledge_time 早于供应商快照时点（缺快照不可回填）"
                )

    def _emit(self, type_: str, run_id: str, payload: dict) -> None:
        if self._events is not None:
            self._events.append(Event(run_id=run_id, type=type_, payload=payload))

    def _verdict(self, run_id: str, hook: str, key: str, detail: str) -> None:
        self._emit(
            HOOK_VERDICT, run_id,
            {"hook": hook, "verdict": "rejected", "field": key, "detail": detail},
        )


__all__ = ["TypedMetricWriter", "METRIC_ASSERTED", "NormalizationError"]
