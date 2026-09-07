"""IterationReport：每轮研究的可读记录（StepReport 的领域形态，DESIGN.md §4.1）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class IterationReport(BaseModel):
    run_id: str
    round: int
    entity: str  # "stock:AAPL"
    completeness_before: float
    completeness_after: float
    facts_written: list[str] = Field(default_factory=list)
    rejected: list[dict] = Field(default_factory=list)  # [{field, reason}]
    missing_after: list[str] = Field(default_factory=list)
    progress: bool = False  # 本轮是否有成功写入
    # 研究升级（§7.7）：进展不仅记录 facts_written——typed 观测/论断/计算/问题
    # 同样是有价值产出，防止「有价值分析但没写字段」被误判 stalled
    observations_written: list[str] = Field(default_factory=list)
    claims_written: list[str] = Field(default_factory=list)
    calculations_done: list[str] = Field(default_factory=list)
    questions_advanced: list[str] = Field(default_factory=list)
    question_coverage: float | None = None  # 冻结计划的适用问题覆盖率（无计划 = None）
