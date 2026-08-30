"""LLM 抽象：协议 + 回复模型。真实 provider 适配在 P1 接入（移植 V1.0 LLMRouter）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AssistantReply(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)


#: 流式增量回调：每个文本 delta 调一次（UI streaming / assistant/chunk 事件）
OnDelta = Callable[[str], None]


class LLM(Protocol):
    """kernel 与模型之间的唯一通道。输入消息必须来自 EventStore.derive_messages。"""

    def complete(self, messages: list[dict[str, Any]], tools: list[str]) -> AssistantReply: ...


class StreamingLLM(LLM, Protocol):
    """可选能力：流式。kernel 检测到该能力时优先走流（assistant/chunk 落库）。"""

    def stream_complete(
        self, messages: list[dict[str, Any]], tools: list[str], *, on_delta: OnDelta
    ) -> AssistantReply: ...
