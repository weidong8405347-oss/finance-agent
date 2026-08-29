"""protected paths：权力分离的技术落地（评估对齐稿 §2.2-4）。

评估代码、评估配置、holdout 数据对 agent 只读；写操作 fail-closed。
任何未来的文件系统工具必须先过这道闸。
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import PurePosixPath


class ProtectedPathViolation(Exception):
    pass


class ProtectedPaths:
    DEFAULT_PATTERNS = (
        "evals/**",  # 评估配置与报告
        "**/evaluation/**",  # 评估器代码
        "**/holdout*/**",  # holdout 数据
        "secrets/**",
    )

    def __init__(self, patterns: list[str] | tuple[str, ...] | None = None):
        self._patterns = tuple(patterns) if patterns else self.DEFAULT_PATTERNS

    def is_protected(self, path: str) -> bool:
        p = PurePosixPath(path).as_posix()
        return any(fnmatchcase(p, pat) for pat in self._patterns)

    def check_write(self, path: str) -> None:
        """写操作闸：命中保护路径即拒绝。读不受限（只读是默认姿态）。"""
        if self.is_protected(path):
            raise ProtectedPathViolation(f"protected path 写禁止: {path}")
