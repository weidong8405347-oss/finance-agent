"""CommandRunner：command 的派发与 step pipeline 编排。

事件序（全部落会话流，UI 投影数据源）：
  command/run → [approval/asked|waived] → (step_agent/start → step_agent/progress*
  → step_agent/end)* → command/done(outcome)

- step agent 跑在独立 child run（run/created 带 parent_run_id）；
- 子流的轮次级摘要桥接为父流 step_agent/progress（D5）；
- 完成后经 wake 回调唤醒主 agent 汇报（Q1 异步唤醒）；
- 取消：stop_command 置 cancel flag，step 在轮次边界安全停下（Q6）；
- 改向：steer 落 context/inject 到当前 step 的子 run（kernel 下一次模型调用重投影即见），
  后续每个 step 启动时继承该 command 的全部改向（Q6 后置项）。
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
    CONTEXT_INJECT,
    DECISION_CARD,
    FACT_ASSERTED,
    FACT_CONFLICT,
    RESEARCH_ROUND_END,
    STEER_REQUESTED,
    STEP_AGENT_END,
    STEP_AGENT_PROGRESS,
    STEP_AGENT_START,
    Event,
)
from ..eventstore.store import StoredEvent
from ..gateway.adapters.prices import PRICE_SOURCE_ORDER
from .registry import COMMANDS, ParsedCommand, parse_target
from .steps import STEP_TITLES, STEPS, StepContext, StepDeps, StepResult

logger = logging.getLogger("finance_agent.commands")

#: 子流里桥接到父流进度的事件类型（过程透明：工具调用 + 知识库写入 + 决策出具）
_BRIDGE_TYPES = {
    RESEARCH_ROUND_END,
    DECISION_CARD,
    FACT_ASSERTED,
    FACT_CONFLICT,
    "eval/report",
    "research/error",
    "decision/error",
    "tool/call",
}

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
        self._steers: dict[str, list[str]] = {}  # command_id → 累计改向（后续 step 启动时继承）
        self._current_child: dict[str, str] = {}  # command_id → 当前正在跑的 child run

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

    def steer(
        self, session_run_id: str, message: str, command_id: str | None = None
    ) -> list[dict]:
        """向运行中的 command 注入改向指令（Q6 后置项，stop 的对偶）。

        - 默认注入该会话**全部**活跃 command（改向是对当前工作的方向修正，Q9 并行研究都应遵循）；
          指定 command_id 则只注入它；
        - 当前 step 的子 run 立即落 context/inject（白名单类型——kernel 每次模型调用
          从 store 重投影，下一次调用即见）；
        - 同时登记到该 command 的改向累计，后续每个 step 启动时继承注入；
        - 会话流落 steer/requested（UI 可见）；父流落 step_agent/progress（进度行）；
        - 返回注入结果列表（空 = 无活跃 command）。
        """
        message = message.strip()
        with self._lock:
            active = [cid for cid in self._active_by_session.get(session_run_id, []) if cid in self._cancel]
            targets = active if command_id is None else ([command_id] if command_id in active else [])
            snapshot = {cid: self._current_child.get(cid) for cid in targets}
            for cid in targets:
                self._steers.setdefault(cid, []).append(message)
        results = []
        for cid in targets:
            child = snapshot[cid]
            if child is not None:
                self._inject_steer(session_run_id, cid, child, message)
                results.append({"command_id": cid, "child_run_id": child, "delivered": True})
            else:
                # step 切换窗口或审批等待：登记后由下一个 step 启动时继承
                results.append({"command_id": cid, "child_run_id": None, "delivered": False})
            self._deps.events.append(
                Event(
                    run_id=session_run_id,
                    type=STEER_REQUESTED,
                    payload={
                        "command_id": cid,
                        "child_run_id": child,
                        "message": message,
                        "delivered": child is not None,
                    },
                )
            )
        return results

    def _inject_steer(self, session_run_id: str, command_id: str, child_run_id: str, message: str) -> None:
        """把改向注入子 run（模型可见）+ 父流进度行（过程透明）。"""
        self._deps.events.append(
            Event(
                run_id=child_run_id,
                type=CONTEXT_INJECT,
                payload={"role": "user", "content": f"[用户改方向] {message}"},
            )
        )
        self._deps.events.append(
            Event(
                run_id=session_run_id,
                type=STEP_AGENT_PROGRESS,
                payload={
                    "child_run_id": child_run_id,
                    "step": "steer",
                    "summary": f"🧭 用户改方向：{message[:80]}",
                },
            )
        )

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
            self._current_child.pop(command_id, None)
            steers = list(self._steers.pop(command_id, []))
            act = self._active_by_session.get(sid, [])
            if command_id in act:
                act.remove(command_id)
        # Q1：执行过的终态唤醒主 agent 汇报（usage/unknown/needs_config/rejected/cancelled 不唤醒）
        if self._wake is not None and outcome in ("completed", "blocked", "error"):
            note = ""
            if steers:  # 改向要进汇报上下文（主 agent 醒来能解释研究方向的变化）
                note = f"（用户中途改向 ×{len(steers)}：最新「{steers[-1][:40]}」）"
            self._wake(sid, f"[command 完成] /{p.name} → {outcome}：{summary}{note}")

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
            entity_kind, entity_id = parse_target(p.ticker) if p.ticker else ("stock", "")
            ctx = StepContext(
                command_id=command_id,
                session_run_id=sid,
                child_run_id=child_run_id,
                ticker=entity_id,
                objective=p.objective,
                config=p.config,
                should_cancel=cancel.is_set,
                entity_kind=entity_kind,
            )
            # 继承改向：该 command 此前的全部 steer 注入本 step 子 run（模型可见）。
            # 当前 step 运行中到达的 steer 由 steer() 直接注入，不在此重复。
            with self._lock:
                inherited = list(self._steers.get(command_id, []))
                self._current_child[command_id] = child_run_id
            for msg in inherited:
                deps.events.append(
                    Event(
                        run_id=child_run_id,
                        type=CONTEXT_INJECT,
                        payload={"role": "user", "content": f"[用户改方向] {msg}"},
                    )
                )
            try:
                result = self._run_step(step_name, ctx)
            finally:
                with self._lock:
                    self._current_child.pop(command_id, None)
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
        if p.ticker:
            kind, eid = parse_target(p.ticker)
            self._maybe_archive_profile(sid, kind, eid)
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

    def _maybe_archive_profile(self, sid: str, kind: str, entity_id: str) -> None:
        """S2 后档案有变化 → 生成版本化 HTML 存档（profile/archived 事件进父流）。"""
        from ..knowledge.render import maybe_archive

        deps = self._deps
        prices: list[dict] | None = None
        if kind == "stock":
            try:  # 行情不可得（反爬/限流/依赖缺失）不阻断存档——图表缺省即可
                from datetime import date, timedelta

                end = date.today()
                recs = deps.gateway.query_any(
                    PRICE_SOURCE_ORDER,
                    {"ticker": entity_id,
                     "start": (end - timedelta(days=365)).isoformat(), "end": end.isoformat()},
                )
                prices = [r.payload for r in recs]
            except Exception:
                prices = None
        try:
            path = maybe_archive(deps.kb, deps.knowledge_dir, kind, entity_id, prices=prices)
        except Exception as e:
            logger.warning("profile archive failed for %s:%s: %s", kind, entity_id, e)
            return
        if path is not None:
            deps.events.append(
                Event(
                    run_id=sid,
                    type="profile/archived",
                    payload={"entity": f"{kind}:{entity_id}", "archive": path.name},
                )
            )

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
    if e.type == "tool/call":
        args = json.dumps(p.get("arguments", {}), ensure_ascii=False)
        return f"调用 {p.get('name')}({args[:80]})"
    if e.type == FACT_ASSERTED:
        # 知识库写入可见性：哪个字段、什么版本、几条证据
        return (
            f"✎ 档案写入 {p.get('field')}（v{p.get('version')}，"
            f"证据 {len(p.get('evidence_ids') or [])} 条）"
        )
    if e.type == FACT_CONFLICT:
        return f"⚠ 档案冲突：{p.get('field')} 产生竞争版本，待裁决"
    if e.type in ("research/error", "decision/error"):
        return f"出错：{p.get('reason', '')}"
    if e.type == "eval/report":
        return f"评估报告：verdict={p.get('verdict', '?')}"
    return json.dumps(p, ensure_ascii=False)[:120]
