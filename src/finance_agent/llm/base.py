"""LLM 抽象：协议 + 回复模型。真实 provider 适配在 P1 接入（移植 V1.0 LLMRouter）。"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AssistantReply(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)


class LLM(Protocol):
    """kernel 与模型之间的唯一通道。输入消息必须来自 EventStore.derive_messages。"""

    def complete(self, messages: list[dict[str, Any]], tools: list[str]) -> AssistantReply: ...
