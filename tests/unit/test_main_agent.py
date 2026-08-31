"""主 agent 契约测试（redesign §3.4 + R1 验收场景 1/3 的后端骨干）。

- BE 场景：自然语言 → 主 agent 识别标的 → run_command 启动 /research →
  command/done 唤醒 → 主 agent 汇报摘要（不自动出卡由契约+脚本共同保证）
- run_command 校验（缺标的/config → 工具内报错，不起线程）
- stop_command / show_profile
- 不变量：模型上下文只含白名单事件（command/* 编排事件不可见）
"""

import json
import time

from finance_agent.chat.service import ChatService
from finance_agent.commands.runner import CommandRunner
from finance_agent.commands.steps import StepDeps
from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.events import MODEL_VISIBLE_TYPES, Event
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.gateway import DataGateway
from finance_agent.harness.approvals import ApprovalService
from finance_agent.knowledge.models import Evidence, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.main_agent import MainAgent


def make_stack(tmp_path, scripts: list, *, real_commands=False):
    """主 agent + 依赖栈。real_commands=False 时用记录型假 CommandRunner。"""
    events = EventStore(tmp_path / "e.db")
    kb = BitemporalStore(tmp_path / "kb.db")
    writer = ProfileWriter(store=kb, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-t")
    llm = MockLLM(scripts)

    if real_commands:
        deps = StepDeps(
            events=events, kb=kb, writer=writer, gateway=gateway,
            decisions=DecisionService(kb=kb, decisions=DecisionStore(tmp_path / "d.db"), events=events),
            llm_for=lambda role: llm,
            approvals=ApprovalService(events),
            evals_dir=tmp_path / "evals",
            reports_dir=tmp_path / "reports",
        knowledge_dir=tmp_path / "knowledge",
            max_rounds=1,
        )
        runner = CommandRunner(deps)
    else:
        class FakeRunner:
            started: list = []

            def start(self, req):
                self.started.append(req)
                return "cmd-fake01"

            def cancel(self, run_id, command_id=None):
                return "cmd-fake01"

            def steer(self, run_id, message, command_id=None):
                if command_id not in (None, "cmd-fake01"):
                    return []
                return [{"command_id": "cmd-fake01", "child_run_id": "child-1", "delivered": True}]

        runner = FakeRunner()

    def make_agent(run_id: str) -> MainAgent:
        return MainAgent(
            run_id=run_id, events=events, kb=kb, gateway=gateway, llm=llm, commands=runner,
            evals_dir=tmp_path / "evals",
        )

    chat = ChatService(events=events, make_main_agent=make_agent)
    return events, kb, llm, runner, chat


def wait_event(events, run_id, pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = [e for e in events.read(run_id) if pred(e)]
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError("等待事件超时")


def run_one_tool_turn(tmp_path, tool_name, arguments):
    """脚本化主 agent：调用一个工具 → 返回该工具结果消息内容（模型可见投影）。"""
    script = [
        AssistantReply(content="", tool_calls=[ToolCall(call_id="t1", name=tool_name,
                                                    arguments=arguments)]),
        AssistantReply(content="done"),
    ]
    events, kb, llm, runner, chat = make_stack(tmp_path, script)
    run_id = "live-tool"
    chat.begin_session(run_id)
    events.append(Event(run_id=run_id, type="user/message", payload={"content": "trigger"}))
    chat.submit_message(run_id)
    wait_event(events, run_id, lambda e: e.type == "turn/end")
    tools = [m for m in events.derive_messages(run_id) if m["role"] == "tool"]
    assert tools, "工具结果应进入模型可见投影"
    return tools[0]["content"]


def test_contract_has_eval_config_tuning_guidance():
    """评估配置的对话式微调：读出 → 完整新 JSON 代码块 → 用户确认/落盘。
    主 agent 不直接改 evals/ 下的文件（评估配置变更必须留痕、经人确认）。"""
    from finance_agent.main_agent import MAIN_CONTRACT

    assert "show_eval_config" in MAIN_CONTRACT
    assert "代码块" in MAIN_CONTRACT
    assert "不直接改" in MAIN_CONTRACT


def test_show_eval_config_reads_mandate(tmp_path):
    (tmp_path / "evals" / "mandates").mkdir(parents=True)
    (tmp_path / "evals" / "mandates" / "be-q.json").write_text('{"tickers": ["BE"], "budget": 3}')
    content = run_one_tool_turn(tmp_path, "show_eval_config", {"config": "be-q"})
    assert '"tickers"' in content and "BE" in content  # 完整 JSON 读出（微调的基础）


def test_show_eval_config_unknown_lists_available(tmp_path):
    (tmp_path / "evals" / "mandates").mkdir(parents=True)
    (tmp_path / "evals" / "mandates" / "wf-2023.json").write_text("{}")
    content = run_one_tool_turn(tmp_path, "show_eval_config", {"config": "nope"})
    assert "error" in content and "wf-2023" in content  # 可操作报错：列出可用配置


def test_show_eval_config_rejects_path_traversal(tmp_path):
    (tmp_path / "evals" / "mandates").mkdir(parents=True)
    content = run_one_tool_turn(tmp_path, "show_eval_config", {"config": "../../secrets"})
    assert "error" in content and "wf" not in content


def test_contract_has_stalled_retry_discipline():
    """同一标的研究连续两次 stalled → 停手并如实汇报数据边界（prompt 层纪律）。

    背景：主 agent 曾在研究 stalled 时自主重试最多 3 次才停（观察项）。
    契约是唯一杠杆（prompt 层），文本存在性断言防回归丢失。
    """
    from finance_agent.main_agent import MAIN_CONTRACT

    assert "stalled" in MAIN_CONTRACT
    assert "连续两次" in MAIN_CONTRACT
    assert "数据边界" in MAIN_CONTRACT
    assert "不再自主" in MAIN_CONTRACT


def test_be_scenario_identify_and_launch_research(tmp_path):
    """验收场景 1：自然语言 → 识别 BE → run_command 启动研究（宽松直调）。"""
    script = [
        # turn 1：查档案 → 启动 research → 预告
        AssistantReply(content="", tool_calls=[ToolCall(call_id="a1", name="query_kb",
                                                    arguments={"entity_kind": "stock", "entity_id": "BE"})]),
        AssistantReply(content="", tool_calls=[ToolCall(call_id="a2", name="run_command",
                                                    arguments={"name": "research", "ticker": "BE",
                                                               "objective": "评估是否值得投资"})]),
        AssistantReply(content="BE = Bloom Energy（NYSE）。档案完整度 0%，我启动了深度研究，完成后汇报。"),
    ]
    events, kb, llm, runner, chat = make_stack(tmp_path, script)
    run_id = "live-be"
    from finance_agent.eventstore.events import Event
    chat.begin_session(run_id)
    events.append(Event(
        run_id=run_id, type="user/message",
        payload={"content": "我想深度研究下BE这家公司，是否值得投资"}))
    chat.submit_message(run_id)

    wait_event(events, run_id, lambda e: e.type == "turn/end")
    assert runner.started, "主 agent 应调用 run_command"
    req = runner.started[0]
    assert req.parsed.name == "research" and req.parsed.ticker == "BE"
    assert req.session_run_id == run_id

    # 模型上下文不变量：command 编排事件不进入模型可见投影
    msgs = events.derive_messages(run_id)
    assert all(set(m.keys()) >= {"role"} for m in msgs)
    assert "command/run" not in MODEL_VISIBLE_TYPES
    # 上下文里应包含：system 契约 + user 消息 + assistant + tool 结果
    roles = [m["role"] for m in msgs]
    assert roles[0] == "system" and "user" in roles and "assistant" in roles


def test_run_command_validation_errors_are_visible_to_model(tmp_path):
    """缺标的 → 工具内报错（不起线程），模型可在同一 turn 修正。"""
    script = [
        AssistantReply(content="", tool_calls=[ToolCall(call_id="a1", name="run_command",
                                                    arguments={"name": "research"})]),
        AssistantReply(content="请告诉我要研究的标的代码。"),
    ]
    events, kb, llm, runner, chat = make_stack(tmp_path, script)
    run_id = "live-v"
    from finance_agent.eventstore.events import Event
    events.append(Event(run_id=run_id, type="user/message", payload={"content": "帮我研究一下"}))
    chat.submit_message(run_id)
    wait_event(events, run_id, lambda e: e.type == "turn/end")
    assert not runner.started, "缺标的不得启动 command"
    tool_results = [e for e in events.read(run_id) if e.type == "tool/result"]
    assert any("缺少标的" in e.payload.get("content", "") for e in tool_results)


def test_stop_command_cancels_active(tmp_path):
    script = [
        AssistantReply(content="", tool_calls=[ToolCall(call_id="s1", name="stop_command",
                                                    arguments={})]),
        AssistantReply(content="已停止。"),
    ]
    events, kb, llm, runner, chat = make_stack(tmp_path, script)
    run_id = "live-stop"
    from finance_agent.eventstore.events import Event
    events.append(Event(run_id=run_id, type="user/message", payload={"content": "停下来"}))
    chat.submit_message(run_id)
    wait_event(events, run_id, lambda e: e.type == "turn/end")
    results = [e for e in events.read(run_id)
               if e.type == "tool/result" and e.payload.get("name") == "stop_command"]
    assert results and "已请求停止" in results[0].payload["content"]


def test_steer_command_tool_injects_direction(tmp_path):
    script = [
        AssistantReply(content="", tool_calls=[ToolCall(call_id="s1", name="steer_command",
                                                    arguments={"message": "重点看财务质量"})]),
        AssistantReply(content="已注入。"),
    ]
    events, kb, llm, runner, chat = make_stack(tmp_path, script)
    run_id = "live-steer"
    from finance_agent.eventstore.events import Event
    events.append(Event(run_id=run_id, type="user/message", payload={"content": "研究改个方向"}))
    chat.submit_message(run_id)
    wait_event(events, run_id, lambda e: e.type == "turn/end")
    results = [e for e in events.read(run_id)
               if e.type == "tool/result" and e.payload.get("name") == "steer_command"]
    assert results and "已注入" in results[0].payload["content"]


def test_show_profile_returns_structured_card(tmp_path):
    events, kb, llm, runner, chat = make_stack(tmp_path, [
        AssistantReply(content="", tool_calls=[ToolCall(call_id="sp", name="show_profile",
                                                    arguments={"entity_kind": "stock", "entity_id": "BE"})]),
        AssistantReply(content="档案如下。"),
    ])
    # 预置一条事实（经 writer 真实落库）
    from datetime import UTC, datetime

    from finance_agent.knowledge.models import Fact
    kb.add_evidence(Evidence(
        evidence_id="ev-1", source_id="demo", verbatim_quote="产能 2GW",
        retrieved_at=datetime.now(UTC), pit_grade=PitGrade.C))
    from finance_agent.harness.manifest import RunManifest, RunMode
    ProfileWriter(store=kb, events=events).write_fact(
        Fact(entity_kind="stock", entity_id="BE", field="capacity", value="2GW",
             knowledge_time=datetime.now(UTC), evidence_ids=["ev-1"]),
        run=RunManifest(run_id="seed", mode=RunMode.LIVE),
    )
    run_id = "live-prof"
    from finance_agent.eventstore.events import Event
    events.append(Event(run_id=run_id, type="user/message", payload={"content": "给我看看 BE 的档案"}))
    chat.submit_message(run_id)
    wait_event(events, run_id, lambda e: e.type == "turn/end")
    results = [e for e in events.read(run_id)
               if e.type == "tool/result" and e.payload.get("name") == "show_profile"]
    assert results
    card = json.loads(results[0].payload["content"])
    assert card["entity"] == "stock:BE" and card["fact_count"] == 1
    assert card["facts"]["capacity"]["value"] == "2GW"


def test_command_done_wakes_main_agent_to_report(tmp_path):
    """Q1 唤醒链：command/done → context/inject → 主 agent 新 turn 汇报。"""
    script = [
        # turn 1：启动 research
        AssistantReply(content="", tool_calls=[ToolCall(call_id="a2", name="run_command",
                                                    arguments={"name": "research", "ticker": "BE"})]),
        AssistantReply(content="研究已启动。"),
        # turn 2（被 command/done 唤醒）：汇报
        AssistantReply(content="研究完成：完整度提升到 61%。要出决策卡吗？"),
    ]
    events, kb, llm, runner, chat = make_stack(tmp_path, script)
    run_id = "live-wake"
    from finance_agent.eventstore.events import Event
    chat.begin_session(run_id)
    events.append(Event(run_id=run_id, type="user/message", payload={"content": "研究下 BE"}))
    chat.submit_message(run_id)
    wait_event(
        events, run_id,
        lambda e: e.type == "assistant/message" and "已启动" in e.payload.get("content", ""),
    )

    chat.wake(run_id, "[command 完成] /research → completed：研究 2 轮，完整度 61%")
    wait_event(events, run_id, lambda e: e.type == "assistant/message"
               and "要出决策卡吗" in e.payload.get("content", ""))
    # 唤醒注入落库且角色为 user（模型可见=已记录）
    injects = [e for e in events.read(run_id) if e.type == "context/inject"
               and str(e.payload.get("content", "")).startswith("[command 完成]")]
    assert injects and injects[0].payload["role"] == "user"
    # 主 agent 第二个 turn 的上下文包含完成通知
    last_call = llm.received[-1]
    assert any("command 完成" in m.get("content", "") for m in last_call)


def test_message_queueing_serializes_turns(tmp_path):
    """同一会话连续两条消息 → 两个 turn 串行，第二个 turn 能看到全部输入。"""
    script = [
        AssistantReply(content="回答一"),
        AssistantReply(content="回答二"),
    ]
    events, kb, llm, runner, chat = make_stack(tmp_path, script)
    run_id = "live-q"
    from finance_agent.eventstore.events import Event
    events.append(Event(run_id=run_id, type="user/message", payload={"content": "第一问"}))
    chat.submit_message(run_id)
    wait_event(events, run_id,
               lambda e: e.type == "assistant/message" and "回答一" in e.payload.get("content", ""))
    events.append(Event(run_id=run_id, type="user/message", payload={"content": "第二问"}))
    chat.submit_message(run_id)
    wait_event(events, run_id,
               lambda e: e.type == "assistant/message" and "回答二" in e.payload.get("content", ""))
    turns = [e for e in events.read(run_id) if e.type == "turn/start"]
    assert len(turns) == 2
    # 第二个 turn 的模型上下文包含两条用户消息
    assert any("第二问" in m.get("content", "") for m in llm.received[-1])


def test_turn_failure_lands_three_channels(tmp_path, caplog):
    """失败三通道：turn 抛错 → turn/error 事件（用户可见）+ 日志（运维可见）。"""
    import logging

    events = EventStore(tmp_path / "e.db")
    kb = BitemporalStore(tmp_path / "kb.db")

    class BoomLLM:
        def complete(self, messages, tools):
            raise RuntimeError("provider 500")

    runner = type("R", (), {"start": lambda s, r: "cmd-x", "cancel": lambda s, r, c=None: None})()
    chat = ChatService(
        events=events,
        make_main_agent=lambda rid: MainAgent(
            run_id=rid, events=events, kb=kb,
            gateway=DataGateway(mode="live", events=events, run_id="t"),
            llm=BoomLLM(), commands=runner,
        ),
    )
    from finance_agent.eventstore.events import Event
    events.append(Event(run_id="live-err", type="user/message", payload={"content": "hi"}))
    with caplog.at_level(logging.ERROR, logger="finance_agent.chat"):
        chat.submit_message("live-err")
        errs = wait_event(events, "live-err", lambda e: e.type == "turn/error")
    assert "provider 500" in errs[0].payload["reason"]  # 通道一：事件
    assert "provider 500" in caplog.text  # 通道二：日志（通道三 = API 状态投影，API 测试覆盖）
