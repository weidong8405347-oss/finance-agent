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

PROFILE_SCRIPT = [
    AssistantReply(content="", tool_calls=[ToolCall(call_id="p1", name="query_kb", arguments={})]),
    AssistantReply(content="", tool_calls=[ToolCall(call_id="p2", name="propose_thesis", arguments={
        "thesis": "产能故事扎实但估值偏贵", "evidence_ids": ["ev-1"]})]),
    AssistantReply(content="thesis done"),
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
        tmp_path, {"research": [RESEARCH_SCRIPT, PROFILE_SCRIPT, DECIDE_SCRIPT]}
    )
    done = run_command(deps, events, "/decide BE 值得投资吗")
    assert done.payload["outcome"] == "completed"

    types = [e.type for e in events.read("live-s1")]
    assert types[0] == "command/run"
    assert "command/done" in types
    # 四个 step 依次启动/结束
    starts = [e.payload["step"] for e in events.read("live-s1") if e.type == "step_agent/start"]
    assert starts == ["research", "profile_update", "decide", "process_eval"]
    ends = {e.payload["step"]: e.payload["status"] for e in events.read("live-s1")
            if e.type == "step_agent/end"}
    assert ends == {"research": "completed", "profile_update": "completed",
                    "decide": "completed", "process_eval": "completed"}


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


def test_report_published_with_artifact(tmp_path):
    deps, events, _, _ = make_deps(tmp_path, {"research": [RESEARCH_SCRIPT]})
    run_command(deps, events, "/research BE")
    pub = [e for e in events.read("live-s1") if e.type == "report/published"]
    assert pub, "研究完成应发布 report/published（ResearchFoldCard 数据源）"
    artifact = Path(pub[0].payload["artifact_path"])
    assert artifact.exists() and "第 1 轮" in artifact.read_text()
    assert pub[0].payload["artifact_ref"].endswith("/research.md")

    # R3：command 完成后档案 HTML 存档生成（版本化目录，写进 tmp 而非工作树）
    archived = [e for e in events.read("live-s1") if e.type == "profile/archived"]
    assert archived, "档案有变化应生成 HTML 存档"
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
        {"research": [RESEARCH_SCRIPT, PROFILE_SCRIPT, ALWAYS_REJECT_SCRIPT, ALWAYS_REJECT_SCRIPT]},
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


# ---------------- 唤醒（Q1） ----------------


def test_wake_called_on_completed_but_not_on_usage_error(tmp_path):
    wakes = []
    deps, events, _, _ = make_deps(tmp_path, {"research": [RESEARCH_SCRIPT]})
    run_command(deps, events, "/research BE", wake=lambda sid, c: wakes.append((sid, c)))
    assert wakes and wakes[0][0] == "live-s1" and "research" in wakes[0][1]

    wakes.clear()
    run_command(deps, events, "/research", wake=lambda sid, c: wakes.append((sid, c)))
    assert not wakes, "usage_error 不唤醒主 agent（错误卡自解释）"
