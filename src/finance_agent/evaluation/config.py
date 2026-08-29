"""EvalConfig：评估配置即声明式 mandate（冻结 + 内容哈希，对应评估对齐稿 §2.2-5）。

改动配置 = 新一轮评估；哈希进报告，可复现、可审计。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from .costs import CostModel


class EvalConfig(BaseModel, frozen=True):
    name: str
    tickers: list[str] = Field(min_length=1)
    decision_points: list[date] = Field(min_length=1)  # 决策时点序列（walk-forward）
    horizon_months: int = Field(default=3, gt=0)
    backbone_model: str | None = None
    backbone_cutoff: date | None = None  # 截止日分区依据；不登记则全区间 UNKNOWN
    cost: CostModel = Field(default_factory=lambda: CostModel())
    incremental_research: bool = False  # D7：允许 eval 命名空间内补研究
    n_trials: int = Field(default=10, ge=1)  # dev 区间上的迭代次数估计（DSR 多重检验校正）
    dev_fraction: float = 0.7  # 时间序前段为 dev，后段为 holdout（报 generalization gap）
    allow_pit_b: bool = False

    @model_validator(mode="after")
    def _sorted_points(self) -> EvalConfig:
        if list(self.decision_points) != sorted(self.decision_points):
            raise ValueError("decision_points 必须按时间升序")
        return self

    def config_hash(self) -> str:
        canonical = self.model_dump_json()
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

    @classmethod
    def from_json(cls, path: str | Path) -> EvalConfig:
        return cls(**json.loads(Path(path).read_text()))
