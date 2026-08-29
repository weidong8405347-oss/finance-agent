"""RunManifest：run 创建时冻结的清单（模式 / backbone cutoff / 配置哈希）。

对应 DESIGN.md §1.2 两种运行模式 + §6.3 截止日分区。
冻结语义：pydantic frozen model，创建后任何修改都会抛 ValidationError——
「改配置 = 新一轮 run」由类型系统强制执行。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class RunMode(StrEnum):
    LIVE = "live"  # 生产：最新数据，产出真实投资意见
    EVAL = "eval"  # 模拟：历史回放，时间锁生效


class CutoffZone(StrEnum):
    """决策时刻相对 backbone 知识截止日的分区（DESIGN.md §6.3）。"""

    CONTAMINATED = "contaminated"  # T <= cutoff：模型可能背过，结果只作参考
    HONEST = "honest"  # T > cutoff：参数记忆天然排除，主结论只能出自这里
    UNKNOWN = "unknown"  # 未登记 cutoff


class RunManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    mode: RunMode
    eval_as_of: datetime | None = None
    backbone_model: str | None = None
    backbone_cutoff: date | None = None
    allow_pit_b: bool = False  # eval 模式是否放行 B 级 PIT 源（默认 fail-closed）
    config_hash: str | None = None
    created_at: datetime = None  # type: ignore[assignment]

    @model_validator(mode="after")
    def _check_mode_consistency(self) -> RunManifest:
        if self.mode is RunMode.EVAL and self.eval_as_of is None:
            raise ValueError("eval 模式必须提供 eval_as_of（回放决策时刻）")
        if self.mode is RunMode.LIVE and self.eval_as_of is not None:
            raise ValueError("live 模式不得携带 eval_as_of")
        if self.created_at is None:
            object.__setattr__(self, "created_at", datetime.now(UTC))
        return self

    def cutoff_zone(self, t: datetime) -> CutoffZone:
        """决策时刻 t 落在哪个区。backbone_cutoff 未登记 → UNKNOWN（结论口径需人工标注）。"""
        if self.backbone_cutoff is None:
            return CutoffZone.UNKNOWN
        return CutoffZone.HONEST if t.date() > self.backbone_cutoff else CutoffZone.CONTAMINATED
