"""FixtureAdapter：测试/离线回放用 adapter。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ..models import DataRecord, SourceCapability

RecordsFn = Callable[[dict, datetime | None], list[DataRecord]]


class FixtureAdapter:
    def __init__(self, capability: SourceCapability, records: list[DataRecord] | RecordsFn):
        self._cap = capability
        self.records = records  # 公开：引擎按决策点替换诱饵内容
        self.seen_as_of: list[datetime | None] = []
        self.health_error: str | None = None  # 置为非 None 即探活失败（预检测试用）

    def capability(self) -> SourceCapability:
        return self._cap

    def healthcheck(self) -> dict:
        if self.health_error is not None:
            return {"ok": False, "detail": self.health_error}
        return {"ok": True, "detail": "fixture"}

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
        self.seen_as_of.append(as_of)
        if callable(self.records):
            return self.records(request, as_of)
        return list(self.records)
