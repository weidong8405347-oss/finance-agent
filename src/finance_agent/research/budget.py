"""RunBudget：研究运行的真实预算闸（audit §3.3 P0）。

事故形态（live-a2cce641）：设计给了 deep 模式 40 分钟 / 80 次检索 / 4 worker / 5 轮，
代码只消费「轮数」——每组再固定最多 12 个模型步，5 轮 × 4 组 ≈ 240 次模型请求
（实际 236 次），研究阶段跑了 53 分 31 秒，检索 128 次（其中 Exa 84 次）无人扣减，
慢 worker（key_kpi 的 GLM 组，单步最长 355 秒）拖住整轮屏障。

本模块把预算变成一个**在入口真实扣减**的对象：

- wall-clock：deadline 由模式预算决定，并为「部分成果合成」预留末段
  （`synthesis_reserve`）；单次 LLM 请求 timeout 不超过剩余时间；
- tokens：从 reply.usage 累计；**缺 usage 不按零计费**——按上下文长度估算并标
  `tokens_estimated`；
- retrieval_calls / tool_calls / retries：网关工具与 kernel 入口逐次扣减，
  耗尽即拒（返回可读原因，不静默继续烧钱）；
- 线程安全：并行 worker 共享同一个预算对象（锁保护）。

判据一律 fail-loud + 事件留痕（`research/budget`），停止原因可归因到具体维度。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: 缺 usage 时的 token 估算：约 4 字符/token（中英混合的保守下界）。
#: 估算值单独标记，绝不与真实账单混同（audit：缺 usage 不能按零计费）。
_CHARS_PER_TOKEN = 4

#: 单请求 timeout 的地板：低于此值直接判预算耗尽，不发无意义的必然超时请求
_MIN_LLM_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class BudgetSnapshot:
    """预算快照（落事件/进摘要/给 UI 显示等待原因）。"""

    started_at: datetime | None
    deadline: datetime | None
    seconds_used: float
    seconds_limit: float | None
    seconds_remaining: float | None
    llm_calls: int
    tokens_used: int
    tokens_limit: int | None
    tokens_estimated: int
    retrieval_calls: int
    retrieval_limit: int | None
    tool_calls: int
    tool_limit: int | None
    retries_used: int
    retries_limit: int | None
    duplicate_retrievals: int
    exhausted: tuple[str, ...] = ()
    reserve_seconds: float = 0.0

    def as_payload(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "seconds_used": round(self.seconds_used, 1),
            "seconds_limit": self.seconds_limit,
            "seconds_remaining": (
                round(self.seconds_remaining, 1) if self.seconds_remaining is not None else None
            ),
            "llm_calls": self.llm_calls,
            "tokens_used": self.tokens_used,
            "tokens_limit": self.tokens_limit,
            "tokens_estimated": self.tokens_estimated,
            "retrieval_calls": self.retrieval_calls,
            "retrieval_limit": self.retrieval_limit,
            "tool_calls": self.tool_calls,
            "tool_limit": self.tool_limit,
            "retries_used": self.retries_used,
            "retries_limit": self.retries_limit,
            "duplicate_retrievals": self.duplicate_retrievals,
            "exhausted": list(self.exhausted),
            "reserve_seconds": self.reserve_seconds,
        }


@dataclass
class RunBudget:
    """一次研究运行的预算与扣减入口（None = 该维度不设限，兼容旧行为）。"""

    wall_clock_minutes: float | None = None
    retrieval_calls: int | None = None
    tool_calls: int | None = None
    tokens: int | None = None
    retries: int | None = None
    #: 末段预留给「部分成果合成」（audit §3.3：预算预留最后一段用于 partial 合成）
    synthesis_reserve_minutes: float = 0.0
    #: 单请求 timeout 上限（秒）；实际 timeout = min(此值, 剩余时间)
    llm_timeout_cap: float | None = None
    #: 时钟注入（测试用）：monotonic 秒
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)
    #: 墙钟注入（测试用）
    now: Callable[[], datetime] = field(
        default=lambda: datetime.now(UTC), repr=False  # noqa: E731
    )

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._t0: float | None = None
        self._started_at: datetime | None = None
        self._llm_calls = 0
        self._tokens = 0
        self._tokens_estimated = 0
        self._retrieval = 0
        self._tools = 0
        self._retries = 0
        self._duplicates = 0

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        """研究阶段开始（deadline 起点）。重复调用不重置。"""
        with self._lock:
            if self._t0 is None:
                self._t0 = self.monotonic()
                self._started_at = self.now()

    @property
    def reserve_seconds(self) -> float:
        return max(0.0, self.synthesis_reserve_minutes * 60.0)

    def seconds_used(self) -> float:
        with self._lock:
            if self._t0 is None:
                return 0.0
            return max(0.0, self.monotonic() - self._t0)

    def seconds_limit(self) -> float | None:
        """研究阶段可用墙钟（已扣合成预留）。"""
        if self.wall_clock_minutes is None:
            return None
        return max(0.0, self.wall_clock_minutes * 60.0 - self.reserve_seconds)

    def remaining_seconds(self) -> float | None:
        limit = self.seconds_limit()
        if limit is None:
            return None
        return max(0.0, limit - self.seconds_used())

    def deadline(self) -> datetime | None:
        with self._lock:
            limit = self.seconds_limit()
            if limit is None or self._started_at is None:
                return None
            return self._started_at.fromtimestamp(
                self._started_at.timestamp() + limit, tz=UTC
            )

    # ---------------- 耗尽判据 ----------------

    def exhausted(self) -> list[str]:
        """已耗尽的维度（顺序稳定，供 stop_reason 归因）。"""
        out: list[str] = []
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            out.append("wall_clock")
        with self._lock:
            if self.tokens is not None and self._tokens >= self.tokens:
                out.append("tokens")
            if self.tool_calls is not None and self._tools >= self.tool_calls:
                out.append("tool_calls")
            if self.retries is not None and self._retries >= self.retries:
                out.append("retries")
            if self.retrieval_calls is not None and self._retrieval >= self.retrieval_calls:
                out.append("retrieval_calls")
        return out

    def hard_exhausted(self) -> list[str]:
        """硬终止维度（不能再推进任何工作）：wall_clock / tokens / tool_calls。

        不在其列的两个维度：
        - `retrieval_calls`：检索用完不等于研究结束——模型还应该用已登记的证据
          把结论写完（audit §3.3：预算末段留给部分成果合成）；
        - `retries`：重试是单次 LLM 调用的子预算，耗尽只影响该次调用。
        """
        return [r for r in self.exhausted() if r in ("wall_clock", "tokens", "tool_calls")]

    def is_exhausted(self) -> bool:
        """是否应当停下（只看硬维度）。"""
        return bool(self.hard_exhausted())

    # ---------------- 入口扣减 ----------------

    def admit_llm_call(self) -> tuple[bool, str]:
        """LLM 调用前的准入：硬维度耗尽即拒（不发必然超时/超额的请求）。

        检索用完不拦模型调用：还要靠已登记证据写结论与提交答案。
        """
        reasons = self.hard_exhausted()
        if reasons:
            return False, "预算耗尽：" + ",".join(reasons)
        with self._lock:
            self._llm_calls += 1
        return True, ""

    def llm_timeout(self, default: float | None = None) -> float | None:
        """单请求 timeout：min(配置上限, 剩余墙钟)——绝不超过剩余时间（audit §3.3）。"""
        candidates = [t for t in (default, self.llm_timeout_cap) if t]
        remaining = self.remaining_seconds()
        if remaining is not None:
            candidates.append(remaining)
        if not candidates:
            return None
        return max(_MIN_LLM_TIMEOUT_S, min(candidates))

    def admit_retrieval(self, n: int = 1) -> tuple[bool, str]:
        """检索调用准入（query_* 外部源）：耗尽即拒，返回可读原因。"""
        with self._lock:
            if self.retrieval_calls is not None and self._retrieval + n > self.retrieval_calls:
                return False, (
                    f"检索预算耗尽 retrieval_calls（{self._retrieval}/{self.retrieval_calls}）："
                    "改用 query_kb / read_chunk 复用已登记证据，或收敛到 answer_question"
                )
            self._retrieval += n
        reasons = self.hard_exhausted()
        if reasons:
            return False, "预算耗尽：" + ",".join(reasons)
        return True, ""

    def admit_tool(self) -> tuple[bool, str]:
        """任意工具调用准入（总工具数上限）。"""
        with self._lock:
            if self.tool_calls is not None and self._tools + 1 > self.tool_calls:
                return False, (
                    f"工具调用预算耗尽 tool_calls（{self._tools}/{self.tool_calls}）"
                )
            self._tools += 1
        return True, ""

    def admit_retry(self) -> bool:
        """重试准入：重试也吃预算（audit §3.3「新预算同时用于重试与子任务」）。"""
        with self._lock:
            if self.retries is not None and self._retries + 1 > self.retries:
                return False
            self._retries += 1
        return not [r for r in self.hard_exhausted() if r != "tool_calls"]

    def record_usage(self, usage: dict[str, Any] | None, *, context_chars: int = 0) -> int:
        """累计 token。usage 缺失/不完整 → 按上下文长度估算并单独计数（不按零计费）。"""
        total = 0
        if usage:
            for key in ("total_tokens",):
                v = usage.get(key)
                if isinstance(v, int) and v > 0:
                    total = v
            if not total:
                p = usage.get("prompt_tokens")
                c = usage.get("completion_tokens")
                if isinstance(p, int) or isinstance(c, int):
                    total = (p if isinstance(p, int) else 0) + (c if isinstance(c, int) else 0)
        estimated = 0
        if not total:
            estimated = max(1, context_chars // _CHARS_PER_TOKEN)
            total = estimated
        with self._lock:
            self._tokens += total
            self._tokens_estimated += estimated
        return total

    def record_duplicate_retrieval(self, n: int = 1) -> None:
        """重复检索计数（同 run 同请求去重命中）：成本可见，便于评估「重复资料」指标。"""
        with self._lock:
            self._duplicates += n

    # ---------------- 投影 ----------------

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return BudgetSnapshot(
                started_at=self._started_at,
                deadline=self.deadline(),
                seconds_used=self.seconds_used(),
                seconds_limit=self.seconds_limit(),
                seconds_remaining=self.remaining_seconds(),
                llm_calls=self._llm_calls,
                tokens_used=self._tokens,
                tokens_limit=self.tokens,
                tokens_estimated=self._tokens_estimated,
                retrieval_calls=self._retrieval,
                retrieval_limit=self.retrieval_calls,
                tool_calls=self._tools,
                tool_limit=self.tool_calls,
                retries_used=self._retries,
                retries_limit=self.retries,
                duplicate_retrievals=self._duplicates,
                exhausted=tuple(self.exhausted()),
                reserve_seconds=self.reserve_seconds,
            )

    def describe(self) -> str:
        """一行人类可读状态（摘要/UI 等待原因用）。"""
        s = self.snapshot()
        parts = [f"墙钟 {s.seconds_used:.0f}s"]
        if s.seconds_limit is not None:
            parts[-1] += f"/{s.seconds_limit:.0f}s"
        parts.append(f"LLM {s.llm_calls} 次")
        parts.append(f"tokens {s.tokens_used}" + ("（含估算）" if s.tokens_estimated else ""))
        if s.tokens_limit:
            parts[-1] += f"/{s.tokens_limit}"
        parts.append(f"检索 {s.retrieval_calls}" + (f"/{s.retrieval_limit}"
                                                   if s.retrieval_limit else ""))
        parts.append(f"工具 {s.tool_calls}" + (f"/{s.tool_limit}" if s.tool_limit else ""))
        if s.retries_used:
            parts.append(f"重试 {s.retries_used}")
        if s.exhausted:
            parts.append("已耗尽：" + ",".join(s.exhausted))
        return "；".join(parts)


def budget_from_plan(plan_payload: dict | None, *, mode_defaults: dict[str, Any] | None = None,
                     now: Callable[[], datetime] | None = None) -> RunBudget:
    """从冻结计划的 budgets 建预算（§7.7 模式预算表 → 真实闸）。

    计划缺失时回落 mode_defaults；两者都缺 → 不设限（兼容旧行为）。
    """
    budgets = (plan_payload or {}).get("budgets") or {}
    merged = {**(mode_defaults or {}), **{k: v for k, v in budgets.items() if v is not None}}
    kwargs: dict[str, Any] = {}
    if merged.get("wall_clock_minutes"):
        kwargs["wall_clock_minutes"] = float(merged["wall_clock_minutes"])
    if merged.get("retrieval_calls"):
        kwargs["retrieval_calls"] = int(merged["retrieval_calls"])
    if merged.get("tool_calls"):
        kwargs["tool_calls"] = int(merged["tool_calls"])
    if merged.get("tokens"):
        kwargs["tokens"] = int(merged["tokens"])
    if merged.get("retries"):
        kwargs["retries"] = int(merged["retries"])
    if merged.get("synthesis_reserve_minutes") is not None:
        kwargs["synthesis_reserve_minutes"] = float(merged["synthesis_reserve_minutes"])
    if merged.get("llm_timeout_cap"):
        kwargs["llm_timeout_cap"] = float(merged["llm_timeout_cap"])
    if now is not None:
        kwargs["now"] = now
    return RunBudget(**kwargs)


#: 模式 → 合成预留与单请求 timeout 上限（audit §3.3：预算预留末段做 partial 合成）
MODE_RESERVE_MINUTES: dict[str, float] = {
    "standard": 2.0,
    "deep": 6.0,
    "refresh": 1.5,
    "targeted": 1.5,
}
MODE_LLM_TIMEOUT_CAP: dict[str, float] = {
    "standard": 180.0,
    "deep": 240.0,
    "refresh": 120.0,
    "targeted": 120.0,
}


def budget_for_mode(mode: str, plan_payload: dict | None = None) -> RunBudget:
    """模式默认值 + 计划覆盖 → RunBudget。"""
    defaults = {
        "synthesis_reserve_minutes": MODE_RESERVE_MINUTES.get(mode, 2.0),
        "llm_timeout_cap": MODE_LLM_TIMEOUT_CAP.get(mode, 180.0),
    }
    return budget_from_plan(plan_payload, mode_defaults=defaults)


__all__ = ["RunBudget", "BudgetSnapshot", "budget_from_plan", "budget_for_mode",
           "MODE_RESERVE_MINUTES", "MODE_LLM_TIMEOUT_CAP"]
