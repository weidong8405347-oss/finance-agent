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

    def healthcheck(self) -> dict:
        """探活（command 启动前预检用，research-capability-upgrade §4.9）。

        返回 {"ok": bool, "detail": str}；短超时（≤8s），实现不得抛异常——
        探活的意义就是把「源挂了」变成可读报告，不是制造新异常。
        """
        ...
