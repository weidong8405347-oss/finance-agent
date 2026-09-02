"""DataGateway：所有数据采集的唯一入口（时间锁 + fail-closed）。

评估模式双重过滤（DESIGN.md §4.2）：
1. adapter 层：as_of 传给数据源做源头过滤；
2. 网关层：逐条复核 available_at ≤ T，越界/缺时戳一律丢弃并记 leakage/attempt。

C 级源在评估模式直接 blocked；B 级默认 blocked，manifest.allow_pit_b 显式放行。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from ..eventstore.events import GATEWAY_PREFLIGHT, LEAKAGE_ATTEMPT, Event
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
        # 预检结果缓存（monotonic 时间戳, 结果），见 preflight()
        self._preflight_cache: tuple[float, dict[str, dict]] | None = None

    def register(self, adapter: SourceAdapter) -> None:
        cap = adapter.capability()
        self._adapters[cap.source_id] = adapter

    def source_ids(self) -> list[str]:
        return list(self._adapters)

    #: 预检结果缓存 TTL（秒）：探活是网络调用，源健康度是分钟级变化——
    #: TTL 内复用上次结果，避免每个 command 启动都做一轮网络探活（含超时尾巴）。
    PREFLIGHT_TTL_S = 300.0

    def preflight(self, *, run_id: str | None = None) -> dict[str, dict]:
        """command 启动前数据源探活（research-capability-upgrade §4.9）。

        防「后台 agent 静默退化」：源挂了要在启动前明示，而不是让研究 loop
        对着空结果空转。逐源调用 adapter.healthcheck()；adapter 未实现则默认放行
        （结构宽容，第三方/测试 adapter 不受阻）。结果落 gateway/preflight 事件
        （run_id 缺省用装配时的），并返回 {source_id: {"ok": bool, "detail": str}}。
        """
        import time

        now = time.monotonic()
        cached = False
        if self._preflight_cache is not None and now - self._preflight_cache[0] < self.PREFLIGHT_TTL_S:
            results = self._preflight_cache[1]
            cached = True
        else:
            results = self._probe_all()
            self._preflight_cache = (now, results)
        if self._events is not None:
            self._events.append(
                Event(
                    run_id=run_id or self._run_id,
                    type=GATEWAY_PREFLIGHT,
                    payload={"mode": self._mode, "results": results, "cached": cached},
                )
            )
        return results

    def _probe_all(self) -> dict[str, dict]:
        """并行探活（ThreadPoolExecutor）：各源 healthcheck 是网络调用，串行会
        把最慢源的超时累加进 command 启动延迟。"""
        from concurrent.futures import ThreadPoolExecutor

        def probe(source_id: str, adapter: SourceAdapter) -> tuple[str, dict]:
            hc = getattr(adapter, "healthcheck", None)
            if hc is None:
                return source_id, {"ok": True, "detail": "未实现 healthcheck，默认放行"}
            try:
                r = hc()
                return source_id, {"ok": bool(r.get("ok")), "detail": str(r.get("detail", ""))}
            except Exception as e:  # healthcheck 约定不抛，但这里再兜一层底
                return source_id, {"ok": False, "detail": f"{type(e).__name__}: {e}"}

        if not self._adapters:
            return {}
        with ThreadPoolExecutor(max_workers=len(self._adapters)) as pool:
            pairs = list(pool.map(lambda kv: probe(*kv), self._adapters.items()))
        return dict(pairs)

    def query_any(self, source_ids: Sequence[str], request: dict) -> list[DataRecord]:
        """按序尝试多个已注册源，第一个非空结果胜出；全部不可用/为空 → []。

        免费行情源可用性漂移（stooq 反爬 / Yahoo 限流）的容错原语：
        单源抛错或空结果不致命，逐个回退。空结果语义由调用方定：
        图表缺省（软降级）或显式失败（eval 对账 require=True，铁律 4）。
        未注册的源静默跳过——与 query 的 fail-closed 不冲突（那是显式单源调用）。
        """
        for source_id in source_ids:
            if source_id not in self._adapters:
                continue
            try:
                records = self.query(source_id, request)
            except Exception:
                continue  # 单源故障不阻断回退链
            if records:
                return list(records)
        return []

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
