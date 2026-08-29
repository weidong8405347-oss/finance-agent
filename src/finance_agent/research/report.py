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
