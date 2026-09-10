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
# stalled 三通道之事件通道：{entity, rounds_attempted, missing_fields, stale_fields,
# rejected, sources_available, suggestions}（research-capability-upgrade §4.3 L3）
RESEARCH_STALL_DIAGNOSTIC = "research/stall_diagnostic"
# command 启动前数据源探活：{mode, results: {source_id: {ok, detail}}}（§4.9 预检）
GATEWAY_PREFLIGHT = "gateway/preflight"

# ---- 审计整改（docs/ai-for-science-live-a2cce641-audit-and-optimization.md）----
# §3.1 调度装配回放：{round, items:[{group,fields,question_ids,acceptance}], unassigned}
RESEARCH_SCHEDULE = "research/schedule"
# §3.1 问题零推进的具体诊断（区分未分发/提交被拒/来源不可得/分析未完成）
RESEARCH_QUESTION_STALL = "research/question_stall"
# §3.3 真实预算扣减与终止：{deadline, spent, remaining, exhausted, action}
RESEARCH_BUDGET = "research/budget"
# §3.2 错误观测的修订/失效记录（保留旧版本审计链，不原位改冻结历史）
METRIC_REVISED = "metric/revised"
# §3.9 部分成果冻结：每完成一个问题/模块即校验并发布 partial artifact
RESEARCH_PARTIAL_PUBLISHED = "research/partial_published"

# ---- 档案升级（knowledge-dossier-research-redesign §6.5 新业务事件） ----
RESEARCH_PLAN_CREATED = "research/plan_created"      # {plan_id, mode, recipe, questions, budgets}
RESEARCH_QUESTION_UPDATED = "research/question_updated"  # {plan_id, question_id, status, conclusion?}
METRIC_ASSERTED = "metric/asserted"                  # 完整观测 payload（重建依据，metric_writer 落）
CALCULATION_COMPLETED = "calculation/completed"      # {calculation_id, formula, input_refs, result}
RESEARCH_CLAIM_VALIDATED = "research/claim_validated"  # {claim_id, checks}
RESEARCH_ASSESSMENT = "research/assessment"          # {plan_id, coverage, integrity, verdict, stop_reason}

# ---- 证据核验与研究路径（tools-plugins 方案 §5.4/§8.1/§8.3，P2-A） ----
# {claim_id, evidence_support, atomic verdicts, 反证检索记录}
RESEARCH_CLAIM_VERIFIED = "research/claim_verified"
# {plan_id, parent_question_id, sub}（子问题不扩预算/范围）
RESEARCH_SUBQUESTION_ADDED = "research/subquestion_added"
# {round, card, source_events, state_hash}（原日志不删，可重建）
RESEARCH_CONTEXT_COMPRESSED = "research/context_compressed"
RESEARCH_ARTIFACT_CREATED = "research/artifact_created"  # {artifact_id, status, sufficiency, refs}
DOSSIER_PUBLISHED = "dossier/published"              # {snapshot_id, entity, changed_modules}
DOSSIER_PUBLISH_FAILED = "dossier/publish_failed"    # {entity, reason}（失败可见，不静默）

# ---- 编排层（command 制交互，redesign-interaction-orchestration.md §3.5） ----
COMMAND_RUN = "command/run"            # {command_id, name, args, raw_input}
COMMAND_DONE = "command/done"          # {command_id, outcome, summary}
STEP_AGENT_START = "step_agent/start"  # {command_id, child_run_id, step, title}
STEP_AGENT_PROGRESS = "step_agent/progress"  # {child_run_id, step, summary}（子流摘要桥接）
STEP_AGENT_END = "step_agent/end"      # {command_id, child_run_id, step, status, summary}
APPROVAL_ASKED = "approval/asked"      # {approval_id, op, detail}
APPROVAL_DECIDED = "approval/decided"  # {approval_id, approved}
APPROVAL_WAIVED = "approval/waived"    # {op, basis}（豁免当次有效，可审计）
STEER_REQUESTED = "steer/requested"    # {command_id, child_run_id, message, delivered}（Q6 改向注入）
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
