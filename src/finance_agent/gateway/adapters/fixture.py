"""FixtureAdapter：测试/离线回放用 adapter。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ..models import DataRecord, SourceCapability

RecordsFn = Callable[[dict, datetime | None], list[DataRecord]]


class FixtureAdapter:
    def __init__(self, capability: SourceCapability, records: list[DataRecord] | RecordsFn):
        self._cap = capability
        self._records = records
        self.seen_as_of: list[datetime | None] = []

    def capability(self) -> SourceCapability:
        return self._cap

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:
        self.seen_as_of.append(as_of)
        if callable(self._records):
            return self._records(request, as_of)
        return list(self._records)
