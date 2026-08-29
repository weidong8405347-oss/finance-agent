"""holdout 查询预算账本（评估对齐稿 §2.5：私有评测集有限次查询）。

- 按 mandate（评估配置名）记账，JSON 持久化；
- 预算耗尽 → BudgetExhausted（fail-closed，不给例外）；
- holdout 报告由 EvalReport.redacted() 脱敏为仅聚合。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class BudgetExhausted(Exception):
    """holdout 查询预算耗尽。"""


class HoldoutLedger:
    def __init__(self, path: str | Path, *, default_budget: int = 10):
        self._path = Path(path)
        self._default = default_budget

    def remaining(self, key: str, *, budget: int | None = None) -> int:
        budget = budget if budget is not None else self._default
        return budget - self._load().get(key, 0)

    def assert_allowed(self, key: str, *, budget: int | None = None) -> None:
        if self.remaining(key, budget=budget) <= 0:
            raise BudgetExhausted(f"holdout {key!r} 查询预算已耗尽（预算制，fail-closed）")

    def consume(self, key: str, *, budget: int | None = None) -> None:
        self.assert_allowed(key, budget=budget)
        data = self._load()
        data[key] = data.get(key, 0) + 1
        self._save(data)

    def _load(self) -> dict[str, int]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text())

    def _save(self, data: dict[str, int]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self._path)  # 原子写
