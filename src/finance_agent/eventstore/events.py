"""canonical 事件类型与模型可见性集合。

铁律（DESIGN.md §3.4）：模型可见 = 已记录；反向不成立——
审计/钩子/状态迁移事件被记录，但永不进入模型上下文。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

# ---- 事件类型常量（命名空间/动作） ----
RUN_CREATED = "run/created"
TURN_START = "turn/start"
TURN_END = "turn/end"
STEP_START = "step/start"
STEP_END = "step/end"
USER_MESSAGE = "user/message"
ASSISTANT_MESSAGE = "assistant/message"
CONTEXT_INJECT = "context/inject"
TOOL_CALL = "tool/call"  # 审计用；模型上下文中的工具调用折叠在 assistant/message
TOOL_RESULT = "tool/result"
HOOK_VERDICT = "hook/verdict"
FACT_ASSERTED = "fact/asserted"
FACT_SUPERSEDED = "fact/superseded"
FACT_CONFLICT = "fact/conflict_raised"
DECISION_CARD = "decision/card_issued"
LEAKAGE_ATTEMPT = "leakage/attempt"

#: 可投影进模型上下文的事件类型（白名单）
MODEL_VISIBLE_TYPES: frozenset[str] = frozenset(
    {USER_MESSAGE, ASSISTANT_MESSAGE, TOOL_RESULT, CONTEXT_INJECT}
)


class Event(BaseModel):
    """追加进 EventStore 的一条事实。"""

    run_id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    turn: int = 0
    step: int = 0
    correlation_id: str | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StoredEvent(Event):
    """从 EventStore 读出的事件（带全局单调序号）。"""

    seq: int
