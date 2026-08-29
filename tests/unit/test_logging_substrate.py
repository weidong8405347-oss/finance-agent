"""L0 logging 基底 + 事件镜像契约（RCA 规矩 1 的通道 2：日志）。

- error 类事件自动镜像到日志（EventStore subscriber）；
- 普通事件不刷屏；
- setup_logging 幂等。
"""

import logging

from finance_agent.eventstore.events import Event
from finance_agent.eventstore.store import EventStore
from finance_agent.logging_setup import mirror_events_to_logging, setup_logging


def test_error_events_mirrored_to_log(tmp_path, caplog):
    store = EventStore(tmp_path / "e.db")
    logger = setup_logging("finance_agent_test_mirror")
    mirror_events_to_logging(store, logger)

    with caplog.at_level(logging.INFO, logger="finance_agent_test_mirror"):
        store.append(Event(run_id="r1", type="turn/start"))  # 普通事件不镜像
        store.append(
            Event(run_id="r1", type="research/error", payload={"reason": "no provider"})
        )
        store.append(
            Event(run_id="r1", type="leakage/attempt", payload={"reason": "future_record_dropped"})
        )

    error_lines = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("no provider" in r.getMessage() for r in error_lines)
    assert any("future_record_dropped" in r.getMessage() for r in error_lines)
    # 普通事件不产生 WARNING+
    assert not [r for r in caplog.records if "turn/start" in r.getMessage()]


def test_subscriber_failure_does_not_break_append(tmp_path):
    """订阅者异常不得影响事件落库（日志通道绝不能反噬真相源）。"""
    store = EventStore(tmp_path / "e.db")
    store.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("subscriber boom")))
    seq = store.append(Event(run_id="r1", type="user/message", payload={"content": "x"}))
    assert seq == 1
    assert len(store.read("r1")) == 1


def test_setup_logging_idempotent():
    l1 = setup_logging("finance_agent_test_idem")
    l2 = setup_logging("finance_agent_test_idem")
    assert l1 is l2
    assert len(l1.handlers) == 1  # 不重复挂 handler
