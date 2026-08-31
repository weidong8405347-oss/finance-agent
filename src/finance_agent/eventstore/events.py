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
ASSISTANT_CHUNK = "assistant/chunk"  # 流式增量（入日志保 replay/UI 保真，不进模型上下文）
CONTEXT_INJECT = "context/inject"
TOOL_CALL = "tool/call"  # 审计用；模型上下文中的工具调用折叠在 assistant/message
TOOL_RESULT = "tool/result"
HOOK_VERDICT = "hook/verdict"
FACT_ASSERTED = "fact/asserted"
FACT_SUPERSEDED = "fact/superseded"
FACT_CONFLICT = "fact/conflict_raised"
FACT_CONFLICT_RESOLVED = "fact/conflict_resolved"  # {field, kept_fact_id, note}（裁决闭环）
DECISION_CARD = "decision/card_issued"
LEAKAGE_ATTEMPT = "leakage/attempt"
RESEARCH_ROUND_START = "research/round_start"
RESEARCH_ROUND_END = "research/round_end"
RESEARCH_RUBRIC = "research/rubric"

# ---- 编排层（command 制交互，redesign-interaction-orchestration.md §3.5） ----
COMMAND_RUN = "command/run"            # {command_id, name, args, raw_input}
COMMAND_DONE = "command/done"          # {command_id, outcome, summary}
STEP_AGENT_START = "step_agent/start"  # {command_id, child_run_id, step, title}
STEP_AGENT_PROGRESS = "step_agent/progress"  # {child_run_id, step, summary}（子流摘要桥接）
STEP_AGENT_END = "step_agent/end"      # {command_id, child_run_id, step, status, summary}
APPROVAL_ASKED = "approval/asked"      # {approval_id, op, detail}
APPROVAL_DECIDED = "approval/decided"  # {approval_id, approved}
APPROVAL_WAIVED = "approval/waived"    # {op, basis}（豁免当次有效，可审计）
SESSION_TITLE = "session/title"        # {title}
REPORT_PUBLISHED = "report/published"  # {child_run_id, kind, title, summary, artifact_path}

#: command/done 的终态集合
COMMAND_OUTCOMES = frozenset(
    {"completed", "blocked", "cancelled", "error", "rejected", "usage_error", "unknown", "needs_config"}
)

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
