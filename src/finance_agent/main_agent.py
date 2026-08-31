"""主 agent：对话的唯一入口（redesign §3.4 契约 + Q1-Q9 裁决落点）。

- 无状态重建（D1）：每个 turn 的上下文从 EventStore 投影重建；本对象不持有对话状态。
- 宽松调用 + decide 闸（Q2）：research/profile 判断即调；decide 必须先摆摘要经用户确认。
- 异步唤醒（Q1）：run_command 立即返回；command/done 由 ChatService 注入唤醒新 turn。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .commands.registry import COMMANDS, ParsedCommand, catalog
from .commands.runner import CommandRequest, CommandRunner
from .eventstore.events import CONTEXT_INJECT, Event
from .eventstore.store import EventStore
from .gateway.gateway import DataGateway
from .gateway.tools import make_gateway_tool
from .harness.manifest import RunManifest, RunMode
from .knowledge.gaps import GapAnalyzer
from .knowledge.store import BitemporalStore
from .llm.base import LLM
from .loop.kernel import AgentKernel

MAIN_CONTRACT = """\
你是 finance-agent 的主 agent，一个投研对话伙伴。纪律：
1. 标的识别：用户提到公司（中文名/英文名/代码/别名/描述）时，先确定标的代码。
   能确定就直接说明（如「BE = Bloom Energy，NYSE」）；不能确定就追问，绝不猜。
2. 自主调用：判断需要研究/建档就直接用 run_command 调用 research 或 profile，
   调用时用一句话预告将发生什么；不做二次确认。
3. decide 闸：只有用户明确要投资建议时才考虑 decide；且调用前必须把最新研究结论
   摘要摆出来问「要出决策卡吗」，用户说要才调。研究完成 ≠ 自动出决策。
4. evaluate 高成本：说明成本与配置摘要，走审批卡；用户明确说「不用审批/直接跑」时，
   把用户原话摘录进 waiver_basis 一并调用（豁免当次有效）。
5. 多标的：对比/批量类输入 → 每个标的各自独立 run_command（可并行发起），
   全部完成后由你综合对比；command 只接受单标的。
6. 先查档案：调用 command 前先 query_kb 看完整度与新鲜度，把现状告诉用户。
7. 一切事实性断言引用证据 id；没有证据就说「我不知道」。
8. 用户要看档案 → 调 show_profile；要停任务 → stop_command；command 运行中用户想改研究方向
   → steer_command（把新方向注入正在跑的 step，后续 step 也会遵循）。
9. 长任务启动时用一句话告知接下来会发生什么；command 完成后你会收到
   「[command 完成]」系统消息，届时向用户汇报结论摘要。
"""

MAIN_AGENT_TOOL_SCHEMAS: dict[str, dict] = {
    "query_kb": {
        "name": "query_kb",
        "description": "查询实体档案（股票/行业）当前投影：全部字段、完整度、证据绑定",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_kind": {"type": "string", "enum": ["stock", "industry"]},
                "entity_id": {"type": "string", "description": "标的代码（如 BE、600519）或行业 slug"},
            },
            "required": ["entity_kind", "entity_id"],
        },
    },
    "run_command": {
        "name": "run_command",
        "description": (
            "启动一个领域 command（子 agent pipeline，异步执行，完成后你会收到通知）。"
            "research=深度研究；profile=研究+档案更新；decide=全链路出决策卡（先经用户确认）；"
            "evaluate=独立效果评估（默认审批）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": list(COMMANDS)},
                "ticker": {"type": "string", "description": "单标的代码（research/profile/decide 必填）"},
                "objective": {"type": "string", "description": "研究目标（可选）"},
                "config": {"type": "string", "description": "评估配置名（evaluate 必填）"},
                "waiver_basis": {
                    "type": "string",
                    "description": "仅 evaluate：用户明确豁免审批的原话摘录（当次有效）",
                },
            },
            "required": ["name"],
        },
    },
    "stop_command": {
        "name": "stop_command",
        "description": "应用户要求中断本会话正在运行的 command（轮次边界安全停止，已落库结果保留）",
        "parameters": {
            "type": "object",
            "properties": {"command_id": {"type": "string", "description": "可选；缺省停最近一个"}},
        },
    },
    "steer_command": {
        "name": "steer_command",
        "description": (
            "把用户新的研究方向注入本会话正在运行的 command（当前 step 下一次模型调用即见，"
            "后续 step 启动时继承）。command 运行中用户说「换个重点/别看 X 了/优先看 Y」时用它。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "改向指令（用户意图的一句话转述）"},
                "command_id": {"type": "string", "description": "可选；缺省注入全部运行中的 command"},
            },
            "required": ["message"],
        },
    },
    "show_profile": {
        "name": "show_profile",
        "description": "在对话流中内联展示实体档案卡（完整度/关键事实/thesis/冲突）",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_kind": {"type": "string", "enum": ["stock", "industry"]},
                "entity_id": {"type": "string"},
            },
            "required": ["entity_kind", "entity_id"],
        },
    },
}


class MainAgent:
    """会话级主 agent。一个会话一个实例；上下文无状态（永远从 EventStore 投影）。"""

    def __init__(
        self,
        *,
        run_id: str,
        events: EventStore,
        kb: BitemporalStore,
        gateway: DataGateway,
        llm: LLM,
        commands: CommandRunner,
        max_steps: int = 8,
    ):
        self._run_id = run_id
        self._events = events
        self._kb = kb
        self._gateway = gateway
        self._llm = llm
        self._commands = commands
        self._max_steps = max_steps

    def ensure_contract(self) -> None:
        """会话首个模型可见事件 = system 契约（投影顺序即模型所见顺序）。"""
        if not self._events.read(self._run_id, types={CONTEXT_INJECT}):
            self._events.append(
                Event(
                    run_id=self._run_id,
                    type=CONTEXT_INJECT,
                    payload={"role": "system", "content": MAIN_CONTRACT},
                )
            )

    def run_turn(self) -> str:
        """认领一个 turn（输入已落库：user/message 或 context/inject 唤醒）。"""
        self.ensure_contract()
        kernel = AgentKernel(
            store=self._events,
            llm=self._llm,
            manifest=RunManifest(run_id=self._run_id, mode=RunMode.LIVE),
            tools=self._tools(),
            max_steps=self._max_steps,
        )
        return kernel.run_turn(None)

    # ---------------- 工具面 ----------------

    def _tools(self) -> dict[str, Any]:
        tools: dict[str, Any] = {
            "query_kb": self._query_kb,
            "run_command": self._run_command,
            "stop_command": self._stop_command,
            "steer_command": self._steer_command,
            "show_profile": self._show_profile,
        }
        for source_id in self._gateway.source_ids():
            tools[f"query_{source_id}"] = make_gateway_tool(self._gateway, source_id)
        return tools

    def _query_kb(self, args: dict[str, Any]) -> dict[str, Any]:
        kind, eid = args.get("entity_kind"), args.get("entity_id")
        if not kind or not eid:
            return {
                "content": "error: query_kb 需要两个参数——"
                'entity_kind（"stock" 或 "industry"）与 entity_id（标的代码，如 "BE"）。'
                '示例：{"entity_kind": "stock", "entity_id": "BE"}',
                "provenance": [],
            }
        now = datetime.now(UTC)
        view = self._kb.view(kind, eid, now)
        gaps = GapAnalyzer(self._kb).analyze(kind, eid, now)
        return {
            "content": json.dumps(
                {
                    "entity": f"{kind}:{eid}",
                    "completeness": gaps.completeness,
                    "missing": list(gaps.missing),
                    "stale": list(gaps.stale),
                    "facts": {
                        f: {"value": r.value, "knowledge_time": r.knowledge_time.isoformat()}
                        for f, r in view.items()
                    },
                },
                ensure_ascii=False,
                default=str,
            ),
            "provenance": [
                {"source_id": "kb", "available_at": r.knowledge_time.isoformat(), "pit_grade": "A"}
                for r in view.values()
            ],
        }

    def _run_command(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", ""))
        spec = COMMANDS.get(name)
        if spec is None:
            return {"content": f"error: 未知 command {name}。可用：{list(COMMANDS)}", "provenance": []}
        ticker = str(args.get("ticker") or "").upper()
        config = str(args.get("config") or "")
        if name == "evaluate":
            if not config:
                return {
                    "content": "error: evaluate 需要 config（evals/mandates/ 下的配置名）",
                    "provenance": [],
                }
        elif not ticker:
            return {"content": f"error: 缺少标的。用法：{spec.usage}", "provenance": []}
        parsed = ParsedCommand(
            name=name,
            raw_input=f"/{name} {ticker or config}".strip(),
            ticker=ticker,
            objective=str(args.get("objective") or ""),
            config=config,
            no_approval=False,
        )
        command_id = self._commands.start(
            CommandRequest(
                session_run_id=self._run_id,
                parsed=parsed,
                waiver_basis=args.get("waiver_basis") or None,
            )
        )
        return {
            "content": json.dumps(
                {
                    "started": True,
                    "command_id": command_id,
                    "note": "command 已异步启动；完成后你会收到 [command 完成] 通知，届时向用户汇报。",
                },
                ensure_ascii=False,
            ),
            "provenance": [],
        }

    def _stop_command(self, args: dict[str, Any]) -> dict[str, Any]:
        stopped = self._commands.cancel(self._run_id, args.get("command_id") or None)
        if stopped is None:
            return {"content": "本会话没有正在运行的 command。", "provenance": []}
        return {"content": f"已请求停止 {stopped}（将在当前轮次边界安全停下）。", "provenance": []}

    def _steer_command(self, args: dict[str, Any]) -> dict[str, Any]:
        message = str(args.get("message") or "").strip()
        if not message:
            return {
                "content": "error: steer_command 需要 message（改向指令的一句话转述）。",
                "provenance": [],
            }
        steered = self._commands.steer(self._run_id, message, args.get("command_id") or None)
        if not steered:
            return {
                "content": "本会话没有正在运行的 command，无法注入改向（可先 run_command 启动）。",
                "provenance": [],
            }
        targets = "、".join(
            f"{r['command_id']}（{'已注入当前 step' if r['delivered'] else '将于下一 step 生效'}）"
            for r in steered
        )
        return {
            "content": f"已注入改向指令到 {len(steered)} 个运行中的 command：{targets}。",
            "provenance": [],
        }

    def _show_profile(self, args: dict[str, Any]) -> dict[str, Any]:
        """内联 ProfileCard 的数据源：结构化档案摘要（UI 按工具名特化渲染）。"""
        kind, eid = args.get("entity_kind"), args.get("entity_id")
        if not kind or not eid:
            return {
                "content": "error: show_profile 需要 entity_kind 与 entity_id 两个参数",
                "provenance": [],
            }
        now = datetime.now(UTC)
        view = self._kb.view(kind, eid, now)
        gaps = GapAnalyzer(self._kb).analyze(kind, eid, now)
        if not view:
            return {"content": f"{kind}:{eid} 暂无档案。", "provenance": []}
        card = {
            "entity": f"{kind}:{eid}",
            "completeness": gaps.completeness,
            "conflicts": list(gaps.conflicts),
            "fact_count": len(view),
            "thesis": (view.get("thesis").value if "thesis" in view else None),
            "facts": {
                f: {"value": r.value, "version": r.version, "conflict": r.conflict_flag}
                for f, r in sorted(view.items())
            },
        }
        return {
            "content": json.dumps(card, ensure_ascii=False, default=str),
            "provenance": [
                {"source_id": "kb", "available_at": r.knowledge_time.isoformat(), "pit_grade": "A"}
                for r in view.values()
            ],
        }


def command_catalog_prompt() -> str:
    """command 目录的 prompt 文本（主 agent 契约的补充说明）。"""
    return "\n".join(f"- /{c['name']}：{c['summary']}（用法 {c['usage']}）" for c in catalog())
