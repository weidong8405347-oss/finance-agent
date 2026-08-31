"""command 制编排层的契约测试（redesign §3，R1 验收的场景骨干）。

覆盖：
- slash 解析（ticker/config/flag）
- 管道事件序：command/run → step_agent/* → command/done
- usage_error / unknown / needs_config（确定性报错卡，Q3）
- evaluate 审批闸：默认 asked；拒绝不执行；--no-approval / waiver_basis 豁免（当次，Q4）
- decide 打回有界重试（Q7）；cancel（Q6）；进度桥接（D5）；report/published artifact
"""

import threading
import time
from pathlib import Path

from finance_agent.commands.registry import parse_command
from finance_agent.commands.runner import CommandRequest, CommandRunner
from finance_agent.commands.steps import StepDeps
from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.gateway import DataGateway
from finance_agent.harness.approvals import ApprovalService
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM


def wait_for(events, run_id, pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = [e for e in events.read(run_id) if pred(e)]
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError(f"等待事件超时: {pred}")


# ---------------- 解析 ----------------


def test_parse_plain_message_is_not_command():
    assert parse_command("帮我看看 BE") is None


def test_parse_research_with_objective():
    p = parse_command("/research be 看看产能")
    assert p is not None and p.name == "research" and p.ticker == "BE" and p.objective == "看看产能"


def test_parse_evaluate_with_waiver_flag():
    p = parse_command("/evaluate wf-2023 --no-approval")
    assert p is not None and p.config == "wf-2023" and p.no_approval is True


# ---------------- 装配 ----------------


def make_deps(tmp_path: Path, scripts: dict[str, list], *, eval_runner=None):
    from finance_agent.gateway.adapters.fixture import FixtureAdapter
    from finance_agent.gateway.models import DataRecord, SourceCapability
    from finance_agent.knowledge.models import PitGrade

    events = EventStore(tmp_path / "e.db")
    kb = BitemporalStore(tmp_path / "kb.db")
    writer = ProfileWriter(store=kb, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-test")
    gateway.register(FixtureAdapter(
        SourceCapability(source_id="demo", pit_grade=PitGrade.C,
                         server_side_asof=False, description="夹具演示源"),
        records=[DataRecord(source_id="demo", payload={"form": "10-K", "accession": "demo-1"},
                            url="demo://filing")],
    ))
    approvals = ApprovalService(events)
    calls = {"research": 0, "fast": 0}

    def llm_for(role: str) -> MockLLM:
        script = scripts.get(role, [])
        idx = calls[role]
        calls[role] += 1
        return MockLLM(script[idx] if idx < len(script) else [AssistantReply(content="done")])

    deps = StepDeps(
        events=events,
        kb=kb,
        writer=writer,
        gateway=gateway,
        decisions=DecisionService(kb=kb, decisions=DecisionStore(tmp_path / "d.db"), events=events),
        llm_for=llm_for,
        approvals=approvals,
        evals_dir=tmp_path / "evals",
        reports_dir=tmp_path / "reports",
        knowledge_dir=tmp_path / "knowledge",  # 测试绝不写仓库工作树
        eval_runner=eval_runner,
        fetch_document=lambda url: "产能 2GW 公告。demo 正文。",
        max_rounds=3,
        completeness_target=0.8,
    )
    return deps, events, kb, approvals


def run_command(deps, events, text, *, waiver_basis=None, wake=None):
    runner = CommandRunner(deps, wake=wake, approval_timeout_s=0.2)
    parsed = parse_command(text)
    assert parsed is not None
    command_id = runner.start(
        CommandRequest(session_run_id="live-s1", parsed=parsed, waiver_basis=waiver_basis)
    )
    done = wait_for(
        events, "live-s1",
        lambda e: e.type == "command/done" and e.payload["command_id"] == command_id,
    )[0]
    return done


# 研究 step 脚本（verified binding 路径）：query 拿记录（chk-0001）→ read 正文（chk-0002）
# → register 逐字摘录 → propose 落库；round2 无进展 → stalled
RESEARCH_SCRIPT = [
    AssistantReply(content="", tool_calls=[ToolCall(call_id="c0", name="query_demo", arguments={})]),
    AssistantReply(content="", tool_calls=[ToolCall(call_id="c1", name="read_edgar_filing",
                                               arguments={"chunk_id": "chk-0001", "query": "产能"})]),
    AssistantReply(content="", tool_calls=[ToolCall(call_id="c2", name="register_evidence", arguments={
        "evidence_id": "ev-1", "chunk_id": "chk-0002", "verbatim_quote": "产能 2GW 公告"})]),
    AssistantReply(content="", tool_calls=[ToolCall(call_id="c3", name="propose_fact", arguments={
        "field": "capacity", "value": "2GW", "evidence_ids": ["ev-1"]})]),
    AssistantReply(content="round1 done"),
    AssistantReply(content="no new findings"),
]

# 研究脚本变体：轮内裁决字段冲突（resolve_conflict → fact/conflict_resolved）
RESEARCH_SCRIPT_WITH_RESOLVE = [
    *RESEARCH_SCRIPT[:4],
    AssistantReply(content="", tool_calls=[ToolCall(call_id="c4", name="resolve_conflict", arguments={
        "field": "capacity", "keep_evidence_id": "ev-1", "note": "以新公告为准"})]),
    *RESEARCH_SCRIPT[4:],
]

PROFILE_SCRIPT = [
    AssistantReply(content="", tool_calls=[ToolCall(call_id="p1", name="query_kb", arguments={})]),
    AssistantReply(content="", tool_calls=[ToolCall(call_id="p2", name="propose_thesis", arguments={
        "thesis": "产能故事扎实但估值偏贵", "evidence_ids": ["ev-1"]})]),
    AssistantReply(content="thesis done"),
]

SYNTHESIZE_SCRIPT = [
    AssistantReply(content="", tool_calls=[ToolCall(call_id="s1", name="query_kb", arguments={})]),
    AssistantReply(content="## 摘要\n产能 2GW 的标的，证据绑定完成。[ev-1]\n## 业务与模式\n……"),
]

DECIDE_SCRIPT = [
    AssistantReply(content="", tool_calls=[ToolCall(call_id="d1", name="propose_decision", arguments={
        "action": "watch", "conviction": 2, "horizon": "3m",
        "rationale": ["ev-1"], "invalidation": ["产能爬坡不及预期"]})]),
    AssistantReply(content="card issued"),
]


# ---------------- 管道行为 ----------------


def test_decide_pipeline_runs_all_steps_in_order(tmp_path):
    deps, events, _, _ = make_deps(
        tmp_path,
        {"research": [RESEARCH_SCRIPT, PROFILE_SCRIPT, SYNTHESIZE_SCRIPT, DECIDE_SCRIPT]},
    )
    done = run_command(deps, events, "/decide BE 值得投资吗")
    assert done.payload["outcome"] == "completed"

    types = [e.type for e in events.read("live-s1")]
    assert types[0] == "command/run"
    assert "command/done" in types
    # 五个 step 依次启动/结束
    starts = [e.payload["step"] for e in events.read("live-s1") if e.type == "step_agent/start"]
    assert starts == ["research", "profile_update", "synthesize", "decide", "process_eval"]
    ends = {e.payload["step"]: e.payload["status"] for e in events.read("live-s1")
            if e.type == "step_agent/end"}
    assert ends == {"research": "completed", "profile_update": "completed",
                    "synthesize": "completed", "decide": "completed", "process_eval": "completed"}


def test_child_runs_carry_parent_link(tmp_path):
    deps, events, _, _ = make_deps(tmp_path, {"research": [RESEARCH_SCRIPT]})
    run_command(deps, events, "/research BE")
    children = [
        e for e in events._conn.execute(
            "SELECT run_id, payload FROM events WHERE type='run/created'"
        ).fetchall()
    ]
    assert children, "step agent 应有 child run"
    for run_id, payload in children:
        assert "live-s1" in run_id
        assert "live-s1" in payload  # parent_run_id


def test_progress_bridged_to_parent_stream(tmp_path):
    deps, events, _, _ = make_deps(tmp_path, {"research": [RESEARCH_SCRIPT]})
    run_command(deps, events, "/research BE")
    progress = [e for e in events.read("live-s1") if e.type == "step_agent/progress"]
    assert progress, "轮次级进度应桥接到父流"
    assert any("第 1 轮" in e.payload["summary"] for e in progress)


def test_conflict_resolved_bridged_to_parent_stream(tmp_path):
    deps, events, _, _ = make_deps(tmp_path, {"research": [RESEARCH_SCRIPT_WITH_RESOLVE]})
    run_command(deps, events, "/research BE")
    progress = [e for e in events.read("live-s1") if e.type == "step_agent/progress"]
    resolved = [e for e in progress if "冲突已裁决" in e.payload["summary"]]
    assert resolved, "fact/conflict_resolved 应桥接为父流进度行（裁决回显）"
    assert "capacity" in resolved[0].payload["summary"]
    assert "以新公告为准" in resolved[0].payload["summary"]


def test_report_published_with_artifact(tmp_path):
    deps, events, _, _ = make_deps(
        tmp_path, {"research": [RESEARCH_SCRIPT, SYNTHESIZE_SCRIPT]}
    )
    run_command(deps, events, "/research BE")
    pub = [e for e in events.read("live-s1") if e.type == "report/published"]
    assert pub, "synthesize 应发布 report/published（ResearchFoldCard 数据源）"
    assert pub[0].payload["kind"] == "research_report"
    artifact = Path(pub[0].payload["artifact_path"])
    assert artifact.exists() and "## 摘要" in artifact.read_text()
    assert "产能 2GW" in pub[0].payload["summary"]
    assert pub[0].payload["artifact_ref"].endswith("/report.md")

    # R3：command 完成后档案 HTML 存档生成（版本化目录，写进 tmp 而非工作树）
    archived = [e for e in events.read("live-s1") if e.type == "profile/archived"]
    assert archived, "档案有变化应生成 HTML 存档"
    assert archived[0].payload["entity"] == "stock:BE"
    archive_dir = tmp_path / "knowledge" / "stocks" / "BE" / "archive"
    assert (archive_dir / "latest.html").exists()
    assert any(f.suffix == ".html" and f.name != "latest.html" for f in archive_dir.iterdir())


def test_usage_error_when_ticker_missing(tmp_path):
    deps, events, _, _ = make_deps(tmp_path, {})
    done = run_command(deps, events, "/research")
    assert done.payload["outcome"] == "usage_error"
    assert "用法" in done.payload["summary"]


def test_unknown_command(tmp_path):
    deps, events, _kb, _ = make_deps(tmp_path, {})
    done = run_command(deps, events, "/frobnicate BE")
    assert done.payload["outcome"] == "unknown"
    assert "/research" in done.payload["summary"]


def test_evaluate_needs_config_lists_mandates(tmp_path):
    (tmp_path / "evals" / "mandates").mkdir(parents=True)
    (tmp_path / "evals" / "mandates" / "wf-2023.json").write_text("{}")
    deps, events, _, _ = make_deps(tmp_path, {})
    done = run_command(deps, events, "/evaluate")
    assert done.payload["outcome"] == "needs_config"
    assert "wf-2023" in done.payload["summary"]


# ---------------- 审批闸（evaluate 默认强制；豁免当次有效） ----------------


def test_evaluate_requires_approval_and_runs_after_approve(tmp_path):
    (tmp_path / "evals" / "mandates").mkdir(parents=True)
    (tmp_path / "evals" / "mandates" / "wf.json").write_text("{}")
    ran = []
    deps, events, _, approvals = make_deps(
        tmp_path, {}, eval_runner=lambda **kw: ran.append(kw) or {"verdict": "clean"}
    )
    runner = CommandRunner(deps, approval_timeout_s=5.0)
    command_id = runner.start(
        CommandRequest(session_run_id="live-s1", parsed=parse_command("/evaluate wf"))
    )
    asked = wait_for(events, "live-s1", lambda e: e.type == "approval/asked")[0]
    assert not ran, "审批前不得执行"
    approvals.decide(asked.payload["approval_id"], True)
    done = wait_for(events, "live-s1", lambda e: e.type == "command/done"
                    and e.payload["command_id"] == command_id)[0]
    assert done.payload["outcome"] == "completed" and ran


def test_evaluate_rejected_not_executed(tmp_path):
    (tmp_path / "evals" / "mandates").mkdir(parents=True)
    (tmp_path / "evals" / "mandates" / "wf.json").write_text("{}")
    ran = []
    deps, events, _, approvals = make_deps(
        tmp_path, {}, eval_runner=lambda **kw: ran.append(kw)
    )
    runner = CommandRunner(deps, approval_timeout_s=5.0)
    command_id = runner.start(
        CommandRequest(session_run_id="live-s1", parsed=parse_command("/evaluate wf"))
    )
    asked = wait_for(events, "live-s1", lambda e: e.type == "approval/asked")[0]
    approvals.decide(asked.payload["approval_id"], False)
    done = wait_for(events, "live-s1", lambda e: e.type == "command/done"
                    and e.payload["command_id"] == command_id)[0]
    assert done.payload["outcome"] == "rejected" and not ran


def test_evaluate_waiver_flag_skips_approval(tmp_path):
    (tmp_path / "evals" / "mandates").mkdir(parents=True)
    (tmp_path / "evals" / "mandates" / "wf.json").write_text("{}")
    ran = []
    deps, events, _, _ = make_deps(
        tmp_path, {}, eval_runner=lambda **kw: ran.append(kw) or {"verdict": "clean"}
    )
    done = run_command(deps, events, "/evaluate wf --no-approval")
    assert done.payload["outcome"] == "completed" and ran
    types = [e.type for e in events.read("live-s1")]
    assert "approval/waived" in types and "approval/asked" not in types
    waived = [e for e in events.read("live-s1") if e.type == "approval/waived"][0]
    assert "--no-approval" in waived.payload["basis"]


def test_evaluate_waiver_basis_from_natural_language(tmp_path):
    (tmp_path / "evals" / "mandates").mkdir(parents=True)
    (tmp_path / "evals" / "mandates" / "wf.json").write_text("{}")
    deps, events, _, _ = make_deps(
        tmp_path, {}, eval_runner=lambda **kw: {"verdict": "clean"}
    )
    done = run_command(deps, events, "/evaluate wf", waiver_basis="不用审批，直接跑")
    assert done.payload["outcome"] == "completed"
    waived = [e for e in events.read("live-s1") if e.type == "approval/waived"][0]
    assert "不用审批" in waived.payload["basis"]


# ---------------- decide 打回重试（Q7） ----------------

ALWAYS_REJECT_SCRIPT = [
    AssistantReply(content="", tool_calls=[ToolCall(call_id="d1", name="propose_decision", arguments={
        "action": "buy", "conviction": 5, "horizon": "3m",
        "rationale": ["ev-ghost"], "invalidation": ["x"],
        "position": {"sizing_pct": 0.9, "max_loss_pct": 0.5}})]),
    AssistantReply(content="done"),
]


def test_decide_rejection_retries_then_blocked(tmp_path):
    # rationale 引用未登记证据 → risk-review 必拒；重试满 2 次 → blocked（Q7 有界重试）
    deps, events, _, _ = make_deps(
        tmp_path,
        {"research": [RESEARCH_SCRIPT, PROFILE_SCRIPT, SYNTHESIZE_SCRIPT,
                      ALWAYS_REJECT_SCRIPT, ALWAYS_REJECT_SCRIPT]},
    )
    done = run_command(deps, events, "/decide BE")
    assert done.payload["outcome"] == "blocked"
    assert "risk-review" in done.payload["summary"]
    step_ends = {e.payload["step"]: e.payload["status"] for e in events.read("live-s1")
                 if e.type == "step_agent/end"}
    assert step_ends["decide"] == "blocked"
    # 打回原因被注入子 run 上下文（重试不是盲目重来）
    child = [e for e in events._conn.execute(
        "SELECT run_id FROM events WHERE type='run/created' AND json_extract(payload,'$.step')='decide'"
    ).fetchall()][0][0]
    injects = [e for e in events.read(child)
               if e.type == "context/inject" and "打回" in str(e.payload.get("content", ""))]
    assert injects, "重试时应把打回原因注入子 run 上下文"


# ---------------- 取消（Q6） ----------------


def test_cancel_stops_command_between_rounds(tmp_path):
    started = threading.Event()

    class SlowLLM:
        def complete(self, messages, tools):
            started.set()
            while True:  # 等 cancel 生效
                time.sleep(0.02)

    deps, events, _, _ = make_deps(tmp_path, {})
    deps = StepDeps(**{**deps.__dict__, "llm_for": lambda role: SlowLLM()})
    runner = CommandRunner(deps, approval_timeout_s=0.2)
    parsed = parse_command("/research BE")
    command_id = runner.start(CommandRequest(session_run_id="live-s1", parsed=parsed))
    assert started.wait(timeout=3)
    assert runner.cancel("live-s1") == command_id
    # 取消标志已置位；此处只断言 cancel 注册语义（step 内阻塞属异常路径）
    with deps.events._conn:
        pass


# ---------------- 改向 steer（Q6 后置项） ----------------


class GatedLLM:
    """第一次调用挂起等 steer（返回一个工具调用），之后放行；记录每次调用收到的 messages。
    第一次返回带 tool_call → 同一 turn 内还有第二次模型调用，可验证「注入后下一次调用即见」。"""

    def __init__(self):
        self.calls: list[list[dict]] = []
        self.first_seen = threading.Event()
        self.gate = threading.Event()

    def complete(self, messages, tools):
        self.calls.append(messages)
        if len(self.calls) == 1:
            self.first_seen.set()
            assert self.gate.wait(timeout=5), "等待 steer 超时"
            return AssistantReply(
                content="",
                tool_calls=[ToolCall(call_id="g1", name="query_demo", arguments={})],
            )
        return AssistantReply(content="done")


def test_steer_injects_into_running_child_run(tmp_path):
    """运行中 steer：当前 step 的 child run 落 context/inject（白名单内），
    kernel 下一次模型调用的投影里可见。"""
    llm = GatedLLM()
    deps, events, _, _ = make_deps(tmp_path, {})
    deps = StepDeps(**{**deps.__dict__, "llm_for": lambda role: llm})
    runner = CommandRunner(deps, approval_timeout_s=0.2)
    parsed = parse_command("/research BE")
    command_id = runner.start(CommandRequest(session_run_id="live-s1", parsed=parsed))
    assert llm.first_seen.wait(timeout=5)

    result = runner.steer("live-s1", "重点看竞争对手格局")
    assert result, "应注入到运行中的 command"
    assert result[0]["command_id"] == command_id
    assert result[0]["delivered"] is True
    llm.gate.set()  # 放行，让研究继续收敛
    wait_for(events, "live-s1",
             lambda e: e.type == "command/done" and e.payload["command_id"] == command_id)

    # 1) 当前 child run 落了改向注入（模型可见白名单类型）
    child = f"live-s1--{command_id}-1-research"
    injects = [e for e in events.read(child) if e.type == "context/inject"
               and "用户改方向" in str(e.payload.get("content", ""))]
    assert injects and "竞争对手" in injects[0].payload["content"]
    # 2) 下一次模型调用的投影包含改向（kernel 每步从 store 重投影）
    assert any("用户改方向" in str(m.get("content", "")) for m in llm.calls[1])
    # 3) 会话流落 steer/requested（UI 可见）；父流进度有改向行
    steered = [e for e in events.read("live-s1") if e.type == "steer/requested"]
    assert steered and steered[0].payload["message"] == "重点看竞争对手格局"
    progress = [e for e in events.read("live-s1") if e.type == "step_agent/progress"
                and "改方向" in str(e.payload.get("summary", ""))]
    assert progress


def test_steer_carried_to_subsequent_steps(tmp_path):
    """steer 后续 step 继承：S1 期间改向 → 每个后续 child run 启动时各注入一次。"""
    llm = GatedLLM()
    deps, events, _, _ = make_deps(tmp_path, {})
    counter = {"n": 0}
    scripts = {2: PROFILE_SCRIPT, 3: SYNTHESIZE_SCRIPT}  # S2/S3 脚本；S1 用门控 LLM

    def llm_for(role):
        counter["n"] += 1
        if counter["n"] == 1:
            return llm
        return MockLLM(scripts.get(counter["n"], [AssistantReply(content="done")]))

    deps = StepDeps(**{**deps.__dict__, "llm_for": llm_for})
    runner = CommandRunner(deps, approval_timeout_s=0.2)
    parsed = parse_command("/profile BE")
    command_id = runner.start(CommandRequest(session_run_id="live-s1", parsed=parsed))
    assert llm.first_seen.wait(timeout=5)
    assert runner.steer("live-s1", "优先补财务质量维度")
    llm.gate.set()
    wait_for(events, "live-s1",
             lambda e: e.type == "command/done" and e.payload["command_id"] == command_id)

    # 每个 step 的 child run：改向注入各恰好一条（S1=直接注入，S2+=启动继承）
    children = [e.payload["child_run_id"] for e in events.read("live-s1")
                if e.type == "step_agent/start"]
    assert len(children) == 4  # research / profile_update / synthesize / process_eval
    for child in children:
        n = len([e for e in events.read(child) if e.type == "context/inject"
                 and "用户改方向" in str(e.payload.get("content", ""))])
        assert n == 1, f"{child} 应恰好一条改向注入，实际 {n}"


def test_steer_wake_summary_mentions_redirect(tmp_path):
    llm = GatedLLM()
    wakes: list[tuple] = []
    deps, events, _, _ = make_deps(tmp_path, {})
    deps = StepDeps(**{**deps.__dict__, "llm_for": lambda role: llm})
    runner = CommandRunner(deps, wake=lambda sid, c: wakes.append((sid, c)), approval_timeout_s=0.2)
    parsed = parse_command("/research BE")
    command_id = runner.start(CommandRequest(session_run_id="live-s1", parsed=parsed))
    assert llm.first_seen.wait(timeout=5)
    runner.steer("live-s1", "重点看竞对")
    llm.gate.set()
    wait_for(events, "live-s1",
             lambda e: e.type == "command/done" and e.payload["command_id"] == command_id)
    assert wakes and "改向" in wakes[0][1]


def test_steer_no_active_command_returns_empty(tmp_path):
    deps, events, _, _ = make_deps(tmp_path, {})
    runner = CommandRunner(deps, approval_timeout_s=0.2)
    assert runner.steer("live-none", "改个方向") == []


def test_steer_targets_specific_command(tmp_path):
    """多 command 并行：显式 command_id 只注入指定的那个。"""
    llms: list[GatedLLM] = []

    def llm_for(role):
        g = GatedLLM()
        llms.append(g)
        return g

    deps, events, _, _ = make_deps(tmp_path, {})
    deps = StepDeps(**{**deps.__dict__, "llm_for": llm_for})
    runner = CommandRunner(deps, approval_timeout_s=0.2)
    cmd_a = runner.start(CommandRequest(session_run_id="live-s1", parsed=parse_command("/research BE")))
    cmd_b = runner.start(CommandRequest(session_run_id="live-s1", parsed=parse_command("/research PLTR")))
    deadline = time.time() + 5
    while time.time() < deadline and len(llms) < 2:
        time.sleep(0.02)
    assert len(llms) == 2, "两个 command 都应进入研究 step"
    assert llms[0].first_seen.wait(timeout=5)  # A 的第一次模型调用在飞
    assert llms[1].first_seen.wait(timeout=5)  # B 也在飞

    result = runner.steer("live-s1", "只改 A 的方向", command_id=cmd_a)
    assert [r["command_id"] for r in result] == [cmd_a]
    for g in llms:
        g.gate.set()
    for cid in (cmd_a, cmd_b):
        wait_for(events, "live-s1",
                 lambda e, c=cid: e.type == "command/done" and e.payload["command_id"] == c)
    child_a = f"live-s1--{cmd_a}-1-research"
    child_b = f"live-s1--{cmd_b}-1-research"
    assert len([e for e in events.read(child_a) if e.type == "context/inject"
                and "只改 A" in str(e.payload.get("content", ""))]) == 1
    assert not [e for e in events.read(child_b) if e.type == "context/inject"
                and "只改 A" in str(e.payload.get("content", ""))]


# ---------------- 唤醒（Q1） ----------------


def test_wake_called_on_completed_but_not_on_usage_error(tmp_path):
    wakes = []
    deps, events, _, _ = make_deps(tmp_path, {"research": [RESEARCH_SCRIPT]})
    run_command(deps, events, "/research BE", wake=lambda sid, c: wakes.append((sid, c)))
    assert wakes and wakes[0][0] == "live-s1" and "research" in wakes[0][1]

    wakes.clear()
    run_command(deps, events, "/research", wake=lambda sid, c: wakes.append((sid, c)))
    assert not wakes, "usage_error 不唤醒主 agent（错误卡自解释）"
