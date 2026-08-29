"""MockLLM：脚本化回复 + 记录每次调用实际收到的消息（不变量测试用）。"""

from __future__ import annotations

from typing import Any

from .base import AssistantReply


class MockLLM:
    def __init__(self, replies: list[AssistantReply]):
        self._replies = list(replies)
        self.received: list[list[dict[str, Any]]] = []  # 每次调用的消息快照
        self.received_seqs: list[int | None] = []  # 由 kernel 填：调用时的日志水位

    def complete(self, messages: list[dict[str, Any]], tools: list[str]) -> AssistantReply:
        self.received.append([dict(m) for m in messages])
        if not self._replies:
            raise RuntimeError("MockLLM 脚本耗尽")
        return self._replies.pop(0)
