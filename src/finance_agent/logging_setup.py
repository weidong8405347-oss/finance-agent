"""logging 基底（RCA R1 修复：事件库 ≠ 日志，记录 ≠ 可见）。

- setup_logging：stderr 输出，幂等；
- mirror_events_to_logging：EventStore subscriber，把错误类事件镜像到日志——
  任何进入真相源的错误必然出现在运维通道，杜绝「静默吞掉」。
"""

from __future__ import annotations

import logging
import sys

from .eventstore.events import (
    HOOK_VERDICT,
    LEAKAGE_ATTEMPT,
    Event,
    StoredEvent,
)
from .eventstore.store import EventStore

# 事件类型 → 日志级别。不在表中的事件不镜像（避免刷屏）。
MIRROR_LEVELS: dict[str, int] = {
    "research/error": logging.ERROR,
    "research/cancelled": logging.WARNING,
    LEAKAGE_ATTEMPT: logging.WARNING,
    HOOK_VERDICT: logging.WARNING,  # 门禁拒绝（含 numeric-guard / risk-review）
}


def setup_logging(name: str = "finance_agent", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:  # 幂等
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    )
    logger.addHandler(handler)
    logger.setLevel(level)
    # propagate=True：让测试的 caplog（挂 root）能捕获；本项目 root 无 handler，不会重复输出
    logger.propagate = True
    return logger


def mirror_events_to_logging(store: EventStore, logger: logging.Logger | None = None) -> None:
    """订阅事件流：错误类事件镜像到日志。"""
    log = logger or setup_logging()

    def _mirror(event: Event | StoredEvent) -> None:
        level = MIRROR_LEVELS.get(event.type)
        if level is None:
            return
        log.log(level, "%s run=%s %s", event.type, event.run_id, _brief(event.payload))

    store.subscribe(_mirror)


def _brief(payload: dict) -> str:
    reason = payload.get("reason") or payload.get("violations") or payload.get("detail") or ""
    return str(reason)[:300]
