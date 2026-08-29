"""评估报告模型：聚合结果 + 硬门禁判定 + 可签名落盘。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class DecisionOutcome(BaseModel):
    point: date
    ticker: str
    zone: str  # contaminated / honest / unknown
    card_id: str | None = None  # None = 未出卡（被拒/模型选择不行动）
    action: str = "none"
    sizing_pct: float = 0.0
    gross_return: float = 0.0
    net_return: float = 0.0  # 仓位加权净收益（含成本）
    unit_net_return: float = 0.0  # 每单位敞口净收益（用于与基线公平对比）
    baseline_bh_net_return: float = 0.0  # 同窗口 B&H（同成本模型，sizing=1）
    baseline_llm_only_action: str = "none"
    baseline_llm_only_unit_net: float = 0.0
    incomplete: bool = False  # 价格数据不足，不计入聚合


class Aggregate(BaseModel):
    n_points: int
    n_complete: int
    mean_net_return: float
    hit_rate: float  # buy 决策中前瞻收益为正的比例
    excess_vs_bh: float  # 相对 B&H 的单位敞口超额
    llm_only_mean_net_return: float
    kb_delta: float  # 知识库真实增量 = agent 单位净收益 − LLM-only 单位净收益
    sharpe: float
    max_drawdown: float
    psr: float
    dsr: float
    dev_mean_net: float
    holdout_mean_net: float
    generalization_gap: float


class EvalReport(BaseModel):
    eval_run_id: str
    config_name: str
    config_hash: str
    verdict: Literal["clean", "contaminated"]  # 硬门禁：leakage>0 或 canary 命中即 contaminated
    leakage_events: int
    canary_triggered: bool = False
    outcomes: list[DecisionOutcome] = Field(default_factory=list)
    aggregate: Aggregate

    def redacted(self) -> EvalReport:
        """holdout 报告脱敏：只含聚合统计，不含逐 case 细节（评估对齐稿 §2.5）。"""
        return self.model_copy(update={"outcomes": []})
