"""SSE：把 EventStore 的新事件实时推给 UI（dsh 的 session/event 下行范式）。

实现选型：轮询 SQLite（0.5s）——简单、可靠、跨进程可用（CLI serve 与 API 分离时仍正确）。
iter_sse_events 为同步生成器，便于测试；FastAPI 端点直接用 StreamingResponse 包装。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from ..eventstore.store import EventStore


def iter_sse_events(
    store: EventStore,
    run_id: str,
    *,
    after_seq: int = 0,
    max_polls: int | None = None,
    poll_interval: float = 0.5,
) -> Iterator[str]:
    """产出 SSE data 帧；max_polls 供测试收敛。"""
    last = after_seq
    polls = 0
    while True:
        new = [e for e in store.read(run_id) if e.seq > last]
        for e in new:
            yield "data: " + json.dumps(
                {
                    "seq": e.seq,
                    "type": e.type,
                    "turn": e.turn,
                    "step": e.step,
                    "payload": e.payload,
                    "ts": e.ts.isoformat(),
                },
                ensure_ascii=False,
            ) + "\n\n"
            last = e.seq
        polls += 1
        if max_polls is not None and polls >= max_polls:
            return
        if not new:
            yield ": heartbeat\n\n"  # 保活
        time.sleep(poll_interval)
