"""数据源 adapter 协议：声明 PIT 能力 + 按 as_of 过滤查询。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..models import DataRecord, SourceCapability


class SourceAdapter(Protocol):
    """所有数据源必须实现。评估模式下网关会把 as_of 传给 adapter 做源头过滤，
    并在网关层对返回记录逐条复核（双重过滤）。"""

    def capability(self) -> SourceCapability: ...

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]: ...
