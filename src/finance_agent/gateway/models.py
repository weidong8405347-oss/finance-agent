"""DataGateway 数据模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from ..knowledge.models import PitGrade


class SourceCapability(BaseModel):
    """数据源能力声明：PIT 等级决定评估模式下的可用性（fail-closed）。"""

    source_id: str
    pit_grade: PitGrade
    server_side_asof: bool = True  # 是否支持服务端 as_of 过滤（验收清单第 3 条）
    description: str = ""


class DataRecord(BaseModel):
    """网关返回的一条数据记录。available_at 是 PIT 语义的载体。"""

    source_id: str
    payload: dict[str, Any]
    available_at: datetime | None = None  # None = 无 PIT 保证，评估模式必被丢弃
    event_time: datetime | None = None
    url: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
