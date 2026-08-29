"""DataGateway：所有数据采集的唯一入口（时间锁 + fail-closed）。

评估模式双重过滤（DESIGN.md §4.2）：
1. adapter 层：as_of 传给数据源做源头过滤；
2. 网关层：逐条复核 available_at ≤ T，越界/缺时戳一律丢弃并记 leakage/attempt。

C 级源在评估模式直接 blocked；B 级默认 blocked，manifest.allow_pit_b 显式放行。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from ..eventstore.events import LEAKAGE_ATTEMPT, Event
from ..eventstore.store import EventStore
from ..knowledge.models import PitGrade
from .adapters.base import SourceAdapter
from .models import DataRecord


class SourceBlockedError(Exception):
    """fail-closed：数据源在当前模式不可用。"""


class DataGateway:
    def __init__(
        self,
        *,
        mode: Literal["live", "eval"],
        eval_as_of: datetime | None = None,
        allow_pit_b: bool = False,
        events: EventStore | None = None,
        run_id: str = "",
    ):
        if mode == "eval" and eval_as_of is None:
            raise ValueError("eval 模式的网关必须提供 eval_as_of")
        self._mode = mode
        self._as_of = eval_as_of
        self._allow_b = allow_pit_b
        self._events = events
        self._run_id = run_id
        self._adapters: dict[str, SourceAdapter] = {}

    def register(self, adapter: SourceAdapter) -> None:
        cap = adapter.capability()
        self._adapters[cap.source_id] = adapter

    def query(self, source_id: str, request: dict) -> list[DataRecord]:
        adapter = self._adapters.get(source_id)
        if adapter is None:
            # fail-closed：未注册 = 能力不存在
            self._audit("unknown_source", source_id, None)
            raise SourceBlockedError(f"未注册的数据源: {source_id}")

        cap = adapter.capability()
        if self._mode == "eval":
            if cap.pit_grade is PitGrade.C:
                self._audit("source_blocked_pit_grade_c", source_id, None)
                raise SourceBlockedError(f"评估模式禁用 C 级（无 PIT 保证）数据源: {source_id}")
            if cap.pit_grade is PitGrade.B and not self._allow_b:
                self._audit("source_blocked_pit_grade_b", source_id, None)
                raise SourceBlockedError(
                    f"评估模式默认禁用 B 级数据源（manifest.allow_pit_b 可放行）: {source_id}"
                )

        records = adapter.query(request, as_of=self._as_of if self._mode == "eval" else None)

        if self._mode == "live":
            return list(records)

        assert self._as_of is not None
        survivors: list[DataRecord] = []
        for r in records:
            if r.available_at is None:
                self._audit("missing_available_at", source_id, r)
                continue
            if r.available_at > self._as_of:
                self._audit("future_record_dropped", source_id, r)
                continue
            survivors.append(r)
        return survivors

    def _audit(self, reason: str, source_id: str, record: DataRecord | None) -> None:
        if self._events is None:
            return
        self._events.append(
            Event(
                run_id=self._run_id,
                type=LEAKAGE_ATTEMPT,
                payload={
                    "reason": reason,
                    "source_id": source_id,
                    "eval_as_of": self._as_of.isoformat() if self._as_of else None,
                    "record_available_at": (
                        record.available_at.isoformat() if record and record.available_at else None
                    ),
                    "record_payload_preview": str(record.payload)[:200] if record else None,
                },
            )
        )
