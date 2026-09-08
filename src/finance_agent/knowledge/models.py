"""知识库数据模型：双时态事实 + 证据（DESIGN.md §4.1）。

硬约束（schema 层 forbid，不依赖自觉）：
- 每条 Fact 必须绑 ≥1 条 Evidence；
- Evidence 的 available_at 与 pit_grade 一致性校验：
  A/B 级必须给出 available_at；available_at 缺失只允许 C 级（评估模式永不可用）。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PitGrade(StrEnum):
    """数据源的 PIT（point-in-time）能力等级，评估模式只放行 A/B。"""

    A = "A"  # 精确时间戳，可按 T 过滤（EDGAR filing date、行情日线…）
    B = "B"  # 有发布时间但内容可能被编辑（Substack、新闻归档…）
    C = "C"  # 无 PIT 保证（通用 web 搜索、社媒当前页…）


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    source_id: str
    url: str | None = None
    verbatim_quote: str = Field(min_length=1, max_length=2000)  # 原文摘录（数字保护的锚点）
    retrieved_at: datetime
    available_at: datetime | None = None  # 何时可知（= 数据源的公开时刻）
    pit_grade: PitGrade
    raw_ref: str | None = None  # 原始工件引用（文件路径 / blob hash）
    #: 抽取质量（audit §3.5）：ok/partial/garbled/needs_ocr。PIT 等级只表达时间来源
    #: 性质，表达不了乱码与扫描件；garbled/needs_ocr 的摘录不得支撑结构化数值。
    quality: Literal["ok", "partial", "garbled", "needs_ocr"] = "ok"
    #: 文档内定位（page/table/row/column/section）：财务证据绑定用（audit §3.2）
    locator: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_available_at_consistency(self) -> Evidence:
        if self.available_at is None and self.pit_grade is not PitGrade.C:
            raise ValueError("A/B 级证据必须给出 available_at；给不出就只能标 C")
        if self.pit_grade is PitGrade.C and self.available_at is not None:
            # C 级也可以有 retrieved 时间，但 available_at 语义不保证——禁止填，避免误用
            raise ValueError("C 级证据不允许声称 available_at（无 PIT 保证）")
        return self


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_kind: Literal["stock", "industry"]
    entity_id: str
    field: str
    value: Any
    event_time: datetime | None = None  # 事实在世界中何时为真（如财年截止日）
    knowledge_time: datetime  # 系统何时可知（评估/决策的唯一依据）
    evidence_ids: list[str] = Field(min_length=1)  # 证据绑定（原则 9）
    run_id: str | None = None


class FactRecord(Fact):
    """落库后的事实：版本链 + 冲突标记。"""

    fact_id: str
    version: int
    supersedes: str | None = None
    conflict_flag: bool = False
    namespace: str = "prod"
