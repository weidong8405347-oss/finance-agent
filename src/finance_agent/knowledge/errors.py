"""知识库层异常。"""

from __future__ import annotations


class KnowledgeError(Exception):
    pass


class MissingEvidenceError(KnowledgeError):
    """Fact 引用的 evidence_id 未登记。"""


class ConflictError(KnowledgeError):
    """主键冲突（如重复 evidence_id）。"""


class KnowledgeLeakError(KnowledgeError):
    """评估模式下写入引用了 knowledge_time/available_at 越界的证据（防线 2）。"""


class KnowledgeInvariantError(KnowledgeError):
    """基础不变量违例：事实的 knowledge_time 早于其证据的 available_at。"""


class NumericGuardError(KnowledgeError):
    """数字保护校验失败：数值未在证据原文摘录中出现（原则 8）。"""
