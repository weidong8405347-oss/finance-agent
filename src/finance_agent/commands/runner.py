"""CommandRunner：command 的派发与 step pipeline 编排。

事件序（全部落会话流，UI 投影数据源）：
  command/run → [approval/asked|waived] → (step_agent/start → step_agent/progress*
  → step_agent/end)* → command/done(outcome)

- step agent 跑在独立 child run（run/created 带 parent_run_id）；
- 子流的轮次级摘要桥接为父流 step_agent/progress（D5）；
- 完成后经 wake 回调唤醒主 agent 汇报（Q1 异步唤醒）；
- 取消：stop_command 置 cancel flag，step 在轮次边界安全停下（Q6）。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from ..eventstore.events import (
    APPROVAL_WAIVED,
    COMMAND_DONE,
    COMMAND_RUN,
    DECISION_CARD,
    RESEARCH_ROUND_END,
    STEP_AGENT_END,
    STEP_AGENT_PROGRESS,
    STEP_AGENT_START,
    Event,
)
from ..eventstore.store import StoredEvent
from .registry import COMMANDS, ParsedCommand
from .steps import STEP_TITLES, STEPS, StepContext, StepDeps, StepResult

logger = logging.getLogger("finance_agent.commands")

#: 子流里桥接到父流进度的事件类型 → 摘要函数
_BRIDGE_TYPES = {RESEARCH_ROUND_END, DECISION_CARD, "eval/report", "research/error", "decision/error"}

WakeFn = Callable[[str, str], None]  # (session_run_id, content) —— command/done 唤醒主 agent


@dataclass
class CommandRequest:
    session_run_id: str
    parsed: ParsedCommand
    waiver_basis: str | None = None  # 自然语言入口：用户原话摘录（approval/waived 依据）


class CommandRunner:
    def __init__(
        self,
        deps: StepDeps,
        *,
        wake: WakeFn | None = None,
        approval_timeout_s: float = 600.0,
    ):
        self._deps = deps
        self._wake = wake
        self._approval_timeout = approval_timeout_s
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._active_by_session: dict[str, list[str]] = {}

    def set_wake(self, wake: WakeFn) -> None:
        """接线 command/done → 主 agent 唤醒（装配期环依赖的晚绑定点）。"""
        self._wake = wake

    # ---------------- 派发 ----------------

    def start(self, req: CommandRequest) -> str:
        """登记 command/run 并后台执行；返回 command_id（全部校验走事件，不同步抛错）。"""
        command_id = f"cmd-{uuid.uuid4().hex[:8]}"
        cancel = threading.Event()
        with self._lock:
            self._cancel[command_id] = cancel
            self._active_by_session.setdefault(req.session_run_id, []).append(command_id)
        threading.Thread(target=self._run, args=(command_id, req, cancel), daemon=True).start()
        return command_id

    def cancel(self, session_run_id: str, command_id: str | None = None) -> str | None:
        """停止该会话最近一个活跃 command（或指定 id）；返回被停的 command_id。"""
        with self._lock:
            active = [
                cid
                for cid in self._active_by_session.get(session_run_id, [])
                if cid in self._cancel
            ]
            target = command_id if command_id in self._cancel else (active[-1] if active else None)
            if target is None:
                return None
            self._cancel[target].set()
            return target

    # ---------------- 执行 ----------------

    def _run(self, command_id: str, req: CommandRequest, cancel: threading.Event) -> None:
        events = self._deps.events
        sid = req.session_run_id
        p = req.parsed
        events.append(
            Event(
                run_id=sid,
                type=COMMAND_RUN,
                payload={
                    "command_id": command_id,
                    "name": p.name,
                    "args": {"ticker": p.ticker, "objective": p.objective, "config": p.config},
                    "raw_input": p.raw_input,
                },
            )
        )
        outcome, summary = "error", "内部错误"
        try:
            outcome, summary = self._execute(command_id, req, cancel)
        except Exception as e:  # 三通道之事件通道；日志由调用侧 subscribe 镜像
            logger.exception("command %s (%s) failed", command_id, p.name)
            outcome, summary = "error", str(e)
        events.append(
            Event(
                run_id=sid,
                type=COMMAND_DONE,
                payload={"command_id": command_id, "outcome": outcome, "summary": summary},
            )
        )
        with self._lock:
            self._cancel.pop(command_id, None)
            act = self._active_by_session.get(sid, [])
            if command_id in act:
                act.remove(command_id)
        # Q1：执行过的终态唤醒主 agent 汇报（usage/unknown/needs_config/rejected/cancelled 不唤醒）
        if self._wake is not None and outcome in ("completed", "blocked", "error"):
            self._wake(sid, f"[command 完成] /{p.name} → {outcome}：{summary}")

    def _execute(self, command_id: str, req: CommandRequest, cancel: threading.Event) -> tuple[str, str]:
        deps, sid, p = self._deps, req.session_run_id, req.parsed
        spec = COMMANDS.get(p.name)
        if spec is None:
            known = "、".join(f"/{n}" for n in COMMANDS)
            return "unknown", f"未知命令 /{p.name}。可用命令：{known}"
        if p.name == "evaluate":
            if not p.config:
                configs = self._list_eval_configs()
                listing = "、".join(configs) if configs else "（evals/mandates/ 下暂无配置）"
                return "needs_config", f"请指定评估配置：/evaluate <配置名>。可用：{listing}"
        elif not p.ticker:
            return "usage_error", f"缺少标的。用法：{spec.usage}"

        # 审批闸（默认强制；豁免当次有效：--no-approval flag 或主 agent 带原话依据）
        if spec.needs_approval:
            if p.no_approval or req.waiver_basis:
                basis = "--no-approval" if p.no_approval else f"用户原话：{req.waiver_basis}"
                deps.events.append(
                    Event(run_id=sid, type=APPROVAL_WAIVED,
                          payload={"op": f"/{p.name}", "basis": basis, "command_id": command_id})
                )
            else:
                detail = {"op": f"/{p.name}", "config": p.config, "command_id": command_id}
                approval_id = deps.approvals.request(sid, detail)
                if not deps.approvals.wait(approval_id, timeout=self._approval_timeout):
                    return "rejected", "审批被拒绝或超时，未执行"

        step_summaries: list[str] = []
        for i, step_name in enumerate(spec.steps, 1):
            if cancel.is_set():
                return "cancelled", "已被用户停止"
            child_run_id = f"{sid}--{command_id}-{i}-{step_name}"  # 跨 command 唯一
            deps.events.append(
                Event(
                    run_id=sid,
                    type=STEP_AGENT_START,
                    payload={
                        "command_id": command_id,
                        "child_run_id": child_run_id,
                        "step": step_name,
                        "title": STEP_TITLES.get(step_name, step_name),
                        "index": i,
                        "total": len(spec.steps),
                    },
                )
            )
            ctx = StepContext(
                command_id=command_id,
                session_run_id=sid,
                child_run_id=child_run_id,
                ticker=p.ticker,
                objective=p.objective,
                config=p.config,
                should_cancel=cancel.is_set,
            )
            result = self._run_step(step_name, ctx)
            deps.events.append(
                Event(
                    run_id=sid,
                    type=STEP_AGENT_END,
                    payload={
                        "command_id": command_id,
                        "child_run_id": child_run_id,
                        "step": step_name,
                        "status": result.status,
                        "summary": result.summary,
                    },
                )
            )
            step_summaries.append(f"{STEP_TITLES.get(step_name, step_name)}：{result.summary}")
            if result.status == "cancelled":
                return "cancelled", "已被用户停止"
            if result.status in ("error", "blocked"):
                return result.status, "；".join(step_summaries)
        return "completed", "；".join(step_summaries)

    def _run_step(self, step_name: str, ctx: StepContext) -> StepResult:
        deps = self._deps
        # 子流摘要桥接（D5）：轮次级事件 → 父流 step_agent/progress
        def bridge(e: StoredEvent) -> None:
            if e.run_id != ctx.child_run_id or e.type not in _BRIDGE_TYPES:
                return
            deps.events.append(
                Event(
                    run_id=ctx.session_run_id,
                    type=STEP_AGENT_PROGRESS,
                    payload={
                        "child_run_id": ctx.child_run_id,
                        "step": step_name,
                        "summary": _progress_summary(e),
                    },
                )
            )

        deps.events.subscribe(bridge)
        try:
            return STEPS[step_name](deps, ctx)
        except Exception as e:
            logger.exception("step %s failed (child run %s)", step_name, ctx.child_run_id)
            deps.events.append(
                Event(
                    run_id=ctx.child_run_id,
                    type=f"{step_name}/error",
                    payload={"reason": str(e)},
                )
            )
            return StepResult(status="error", summary=f"{type(e).__name__}: {e}")
        finally:
            deps.events.unsubscribe(bridge)

    def _list_eval_configs(self) -> list[str]:
        mandates = self._deps.evals_dir / "mandates"
        if not mandates.exists():
            return []
        return sorted(p.stem for p in mandates.glob("*.json"))


def _progress_summary(e: StoredEvent) -> str:
    p = e.payload
    if e.type == RESEARCH_ROUND_END:
        return (
            f"第 {p.get('round')} 轮研究完成 · 完整度 "
            f"{float(p.get('completeness_before', 0)):.0%} → {float(p.get('completeness_after', 0)):.0%}"
        )
    if e.type == DECISION_CARD:
        return f"决策卡出具：{str(p.get('action', '')).upper()}（{p.get('card_id')}）"
    if e.type in ("research/error", "decision/error"):
        return f"出错：{p.get('reason', '')}"
    if e.type == "eval/report":
        return f"评估报告：verdict={p.get('verdict', '?')}"
    return json.dumps(p, ensure_ascii=False)[:120]
