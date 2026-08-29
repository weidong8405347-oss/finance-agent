"""反事实扰动探针（FinLeak-Bench 的 PC/CI/IDS 方法移植，评估对齐稿 §2.3）。

做法：对决策输入（档案投影）施加扰动，观察决策是否变化：
- PC（Prediction Consistency）：扰动后行动不变的比例——越高越像在背答案；
- CI（Confidence Invariance）：置信度（conviction 归一化）对扰动的稳定性；
- IDS（Input Dependency Score）：行动分布的 KL 散度——越高越真在用输入。

decider 抽象：profile dict → {"action", "conviction"}。回放集成时由
DecisionLoop 包装提供；测试用规则型 decider 验证探针本身。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..knowledge.store import BitemporalStore

ProfileDict = dict[str, dict[str, Any]]
Decider = Callable[[ProfileDict], dict[str, Any]]


class Perturbation(Protocol):
    def apply(self, view: ProfileDict) -> ProfileDict: ...


@dataclass(frozen=True)
class ScaleField:
    """数值缩放扰动：field 的值 × factor。"""

    field: str
    factor: float

    def apply(self, view: ProfileDict) -> ProfileDict:
        out = {f: dict(v) for f, v in view.items()}
        if self.field in out and isinstance(out[self.field].get("value"), (int, float)):
            out[self.field]["value"] = out[self.field]["value"] * self.factor
        return out


@dataclass(frozen=True)
class DropField:
    """删除关键字段扰动。"""

    field: str

    def apply(self, view: ProfileDict) -> ProfileDict:
        return {f: v for f, v in view.items() if f != self.field}


@dataclass
class CounterfactualResult:
    trials: int
    pc: float  # [0,1] 越高越糟
    ci: float  # [0,1] 越接近 1 越糟
    ids: float  # ≥0，越高越好


class CounterfactualProbe:
    def __init__(
        self,
        kb: BitemporalStore,
        *,
        entity_kind: str,
        entity_id: str,
        as_of: datetime,
        namespace: str = "prod",
    ):
        self._kb = kb
        self._kind = entity_kind
        self._id = entity_id
        self._as_of = as_of
        self._namespace = namespace

    def base_view(self) -> ProfileDict:
        profile = self._kb.view(self._kind, self._id, self._as_of, namespace=self._namespace)
        return {
            f: {"value": r.value, "knowledge_time": r.knowledge_time.isoformat()}
            for f, r in profile.items()
        }

    def run(self, decider: Decider, perturbations: list[Perturbation]) -> CounterfactualResult:
        base_view = self.base_view()
        base = decider(base_view)
        base_action, base_conv = base["action"], float(base.get("conviction", 3))

        unchanged = 0
        conv_deltas: list[float] = []
        cf_actions: list[str] = []
        for p in perturbations:
            cf = decider(p.apply(base_view))
            cf_actions.append(cf["action"])
            if cf["action"] == base_action:
                unchanged += 1
            conv_deltas.append(abs(float(cf.get("conviction", 3)) - base_conv) / 4.0)

        trials = len(perturbations)
        pc = unchanged / trials if trials else 1.0
        ci = 1.0 - (sum(conv_deltas) / trials if trials else 0.0)
        ids = _kl_action_divergence(base_action, cf_actions)
        return CounterfactualResult(trials=trials, pc=pc, ci=ci, ids=ids)


def _kl_action_divergence(base_action: str, cf_actions: list[str], eps: float = 0.05) -> float:
    """p_base = 退化分布于 base_action；p_cf = 扰动试验的行动直方图（eps 平滑）。"""
    if not cf_actions:
        return 0.0
    actions = sorted(set(cf_actions) | {base_action})
    n = len(cf_actions)
    kl = 0.0
    for a in actions:
        p = 1.0 if a == base_action else 0.0
        p = max(p, eps)
        q = max(cf_actions.count(a) / n, eps)
        kl += p * math.log(p / q)
    # 归一化 p 后和不为 1（eps 填充），结果保留相对比较意义
    return kl
