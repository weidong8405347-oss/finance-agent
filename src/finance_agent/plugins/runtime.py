"""运行期绑定（tools-plugins 方案 §6.2，P1-C 执行闭环收口）。

此前的缝隙（交付复核）：ToolExecutor 只被测试调用；生产 kernel 仍直接执行
run 装配的 handler——编译集只用于 adapter 注册/能力页/manifest 冻结，
「声明的能力面」与「实际执行面」可能不一致且不可归因。

本模块收口（绑定不改变 handler 语义，只统一执行纪律）：
- 执行面 = 编译声明 ∩ run 装配 handler：bound_undeclared（装配了 handler 但
  编译集未声明 = 装配缺陷信号）与 declared_unbound（声明了但本 step 未装配）
  都显式可见、进事件，不静默；
- 所有模型可见调用经 ToolExecutor：参数校验（schema required/顶层类型）、
  超时、可重试错误有界重试（重试吃预算）、错误码封装（不包装成空结果）、
  plugins/tool_trace 事件（plugin_id 归属到声明插件）；
- 冻结实际能力集合：plugins/runtime_bound 事件按 run 幂等落库，与
  plugins/manifest_frozen（编译声明）配对——声明了什么与实际跑了什么都可归因。

边界与权衡：
- 宿主内建强约束（时间准入/证据绑定/单写者/快照隔离）仍在 handler 内部，
  executor 只是统一执行纪律的薄层（方案 §6.2：插件不能声明关闭宿主约束）；
- executor 线程池为进程级共享（有界 32 worker）：超时只取消等待、不杀在飞
  handler（Python 限制，与 executor.py 注释一致）；无注册表的旧装配/回放
  路径不经过本模块（行为不变）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from ..eventstore.events import Event
from ..eventstore.store import EventStore
from .contracts import ToolDefinition
from .executor import ToolExecutor
from .registry import CompiledSet

logger = logging.getLogger("finance_agent.plugins.runtime")

#: 实际执行面冻结事件（与 plugins/manifest_frozen 配对：声明 vs 实际运行）
PLUGINS_RUNTIME_BOUND = "plugins/runtime_bound"

#: 写性质工具（trace rw 标注；宿主受控 writer 纪律不变——标注不是授权）
_WRITE_TOOLS = frozenset({
    "register_evidence", "propose_fact", "resolve_conflict", "propose_metric",
    "propose_claim", "answer_question", "calculate_metric", "submit_question_result",
    "track_sub_question", "adjudicate_conflict", "propose_thesis",
    "commit_profile_update", "submit_report_document", "submit_structures",
    "verify_claim", "propose_candidates", "submit_card",
})

#: 慢工具超时上限（秒）：内含 LLM/网络抓取/惰性解析的调用必须宽于默认 120s
#: （LLM 默认读超时 180s —— verify_claim 含内容核验调用，留足余量）
DEFAULT_TIMEOUT_OVERRIDES: dict[str, float] = {
    "verify_claim": 300.0,
    "fetch_document": 200.0,
    "read_document": 200.0,
    "read_edgar_filing": 200.0,
    "search_document": 150.0,
}
_DEFAULT_TIMEOUT_S = 120.0

#: 进程级共享执行池（有界；避免每个 run/step 建池导致 serve 模式线程泄漏）
_SHARED_POOL: ThreadPoolExecutor | None = None


def _shared_pool() -> ThreadPoolExecutor:
    global _SHARED_POOL
    if _SHARED_POOL is None:
        _SHARED_POOL = ThreadPoolExecutor(
            max_workers=32, thread_name_prefix="plugin-rt")
    return _SHARED_POOL


@dataclass
class RuntimeBinding:
    """一次绑定的结果：tools 直接喂 kernel；三个清单进审计事件与日志。"""

    tools: dict[str, Callable[[dict[str, Any]], dict[str, Any]]]
    bound: list[str]               # 编译声明 ∩ run 装配（经 executor 的正式执行面）
    declared_unbound: list[str]    # 编译声明了但本 step 未装配 handler（能力可见性）
    bound_undeclared: list[str]    # 装配了 handler 但编译集未声明（装配缺陷信号）


class RuntimeBinder:
    """把 run 装配的 handler 绑进插件编译集（一个编译场景一个 binder）。

    用法：kernel 装配处 `binder.bind(tools, run_id=...)` 的返回值替换原 dict；
    同名重复绑定（同一 run 多轮装配）只在首次落 runtime_bound 事件。
    """

    def __init__(
        self, compiled: CompiledSet, *, events: EventStore | None = None,
        budget: Any | None = None, max_retries: int = 1,
        timeout_overrides: dict[str, float] | None = None,
    ):
        self._compiled = compiled
        self._events = events
        self._budget = budget
        self._max_retries = max_retries
        self._timeouts = {**DEFAULT_TIMEOUT_OVERRIDES, **(timeout_overrides or {})}
        self._frozen_runs: set[str] = set()

    def bind(
        self, handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]], *,
        run_id: str,
    ) -> RuntimeBinding:
        declared = self._compiled.declared_schemas
        owners = self._compiled.tool_owners
        executor = ToolExecutor(
            events=self._events, budget=self._budget, run_id=run_id,
            max_retries=self._max_retries, pool=_shared_pool(),
        )
        tools: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        bound: list[str] = []
        undeclared: list[str] = []
        for name, handler in handlers.items():
            schema = declared.get(name)
            plugin_id = owners.get(name, "")
            if schema is None:
                # 装配缺陷信号：不拦执行（旧行为兼容），但必须进事件与日志
                undeclared.append(name)
                schema = {"name": name, "parameters": {"type": "object"}}
            else:
                bound.append(name)
            tool_def = ToolDefinition(
                name=name, schema_=schema, handler=handler,
                plugin_id=plugin_id or "host.unbound",
                rw="write" if name in _WRITE_TOOLS else "read",
                timeout_s=self._timeouts.get(name, _DEFAULT_TIMEOUT_S),
            )
            tools[name] = _wrap(executor, tool_def)
        declared_unbound = sorted(set(declared) - set(handlers))
        if undeclared:
            logger.warning(
                "装配了编译集未声明的工具（stage=%s run=%s）：%s",
                self._compiled.stage, run_id, sorted(undeclared),
            )
        self._freeze_once(
            run_id, bound=bound, undeclared=undeclared,
            declared_unbound=declared_unbound, total=len(handlers),
        )
        return RuntimeBinding(
            tools=tools, bound=sorted(bound),
            declared_unbound=declared_unbound, bound_undeclared=sorted(undeclared),
        )

    def _freeze_once(
        self, run_id: str, *, bound: list[str], undeclared: list[str],
        declared_unbound: list[str], total: int,
    ) -> None:
        """实际执行面冻结（run 级幂等）：绑定清单 + 编译集哈希锚点。"""
        if self._events is None or run_id in self._frozen_runs:
            return
        self._frozen_runs.add(run_id)
        self._events.append(Event(
            run_id=run_id,
            type=PLUGINS_RUNTIME_BOUND,
            payload={
                "stage": self._compiled.stage,
                "config_hash": self._compiled.config_hash,
                "tools_total": total,
                "bound": sorted(bound),
                "bound_undeclared": sorted(undeclared),
                "declared_unbound": declared_unbound,
            },
        ))


def _wrap(
    executor: ToolExecutor, tool_def: ToolDefinition,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """handler → executor 执行闭包（每个工具独立 ToolDefinition，避免循环闭包串名）。"""

    def fn(args: dict[str, Any]) -> dict[str, Any]:
        return executor.execute(tool_def, args if isinstance(args, dict) else {})

    return fn


__all__ = [
    "PLUGINS_RUNTIME_BOUND", "RuntimeBinder", "RuntimeBinding",
    "DEFAULT_TIMEOUT_OVERRIDES",
]
