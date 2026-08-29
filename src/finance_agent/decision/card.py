"""DecisionCard：S3 的产出物（DESIGN.md §5.3）。

- 不可变：出卡即冻结；事后修改 = 新卡 + supersedes（P3 引入）；
- rationale 只接受证据 id 列表——拒绝自由文本事实（grounding 纪律）；
- 「不行动」是合法决策：watch/avoid 无需仓位。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..harness.manifest import RunMode


class Action(StrEnum):
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"
    AVOID = "avoid"
    WATCH = "watch"


class Horizon(StrEnum):
    M3 = "3m"
    M6 = "6m"
    M12 = "12m"


_TRADE_ACTIONS = {Action.BUY, Action.SELL}


class Subject(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["stock", "industry"]
    id: str


class Position(BaseModel):
    model_config = ConfigDict(frozen=True)

    sizing_pct: float = Field(gt=0, le=1)  # 占组合比例
    max_loss_pct: float = Field(gt=0, le=1)  # 最大可接受亏损预算


class DecisionCard(BaseModel):
    model_config = ConfigDict(frozen=True)

    card_id: str
    run_id: str
    mode: RunMode
    subject: Subject
    action: Action
    conviction: int = Field(ge=1, le=5)
    horizon: Horizon
    rationale: list[str] = Field(min_length=1)  # 仅证据 id
    thesis_points: list[str] = Field(default_factory=list)  # 引用档案字段名
    invalidation: list[str] = Field(min_length=1)  # 失效条件
    position: Position | None = None
    kb_snapshot_id: str  # 决策依据的档案版本（内容寻址）
    created_at: datetime  # 决策时刻（eval 模式 = T）
    quality_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_position_for_trade(self) -> DecisionCard:
        if self.action in _TRADE_ACTIONS and self.position is None:
            raise ValueError(f"{self.action} 决策必须给出仓位（sizing_pct + max_loss_pct）")
        return self
