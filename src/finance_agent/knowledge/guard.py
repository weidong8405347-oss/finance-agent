"""numeric-guard：数字保护（原则 8）的确定性校验。

事实中的数值必须与所绑证据的原文摘录逐字一致（允许千分位/小数格式差异，
不允许任何换算——宁严勿宽，已知限制记录在案）。
"""

from __future__ import annotations

import re
from typing import Any

from .errors import NumericGuardError

_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _numbers(text: str) -> list[float]:
    out = []
    for m in _NUM_RE.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def assert_numeric_consistent(value: Any, quotes: list[str], *, field: str) -> None:
    """数值型 value 必须出现在至少一条证据摘录中；非数值直接放行。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    target = float(value)
    pool = [n for q in quotes for n in _numbers(q)]
    if any(abs(n - target) <= max(abs(target) * 1e-9, 1e-12) for n in pool):
        return
    raise NumericGuardError(f"字段 {field} 的值 {value} 未在任何证据摘录中逐字出现")
