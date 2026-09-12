"""L1 失败可见性（RCA 规矩 1：错误路径必须三通道齐全——事件 + 日志 + 用户可见状态）。

重设计后（command 制编排）的失败路径：
- step agent 失败 → 子流 {step}/error 事件 + 父流 step_agent/end(error) + command/done(error)
  + 日志（runner logger.exception + 镜像）+ 会话状态 error（API 投影）
- 审批超时 → fail-closed rejected → 会话状态 cancelled（可见但不算错误）
"""

import logging
import time

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.cli import build_orchestrator
from finance_agent.logging_setup import mirror_events_to_logging, setup_logging


def make_client(tmp_path, *, monkeypatch=None, dead_provider=False, approval_timeout_s=None):
    if dead_provider:
        assert monkeypatch is not None
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")  # 端口 9 必拒连
        monkeypatch.setenv("OPENAI_MODEL", "gpt-x")
        monkeypatch.setattr("finance_agent.llm.router._read_dotenv", lambda *a: {})
        # 钉住 provider 解析接缝：只看 env（隔离本机真实 pi 配置，防误打付费 API）
        from finance_agent.llm.router import LLMRouter

        monkeypatch.setattr("finance_agent.cli._router", lambda data_dir=None: LLMRouter.from_env())

    orch = build_orchestrator(tmp_path)  # 真实装配（与 serve 同一条路径）
    # 预检探活属网络边界（工程约定 2 的可注入接缝）——本组测试主题是失败可见性，
    # 不是数据源健康；实例级 stub 为全通过，防离线环境干扰断言。
    orch["command_runner"]._deps.gateway.preflight = lambda **kw: {}  # noqa: SLF001
    if approval_timeout_s is not None:
        orch["command_runner"]._approval_timeout = approval_timeout_s
    app = create_app(
        kb=orch["kb"], events=orch["events"], decisions=orch["decisions"].decisions,
        evals_dir=orch["evals_dir"], chat_service=orch["chat_service"],
        command_runner=orch["command_runner"], approvals=orch["approvals"],
    )
    return TestClient(app), orch["events"]


def wait_event(events, run_id, pred, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = [e for e in events.read(run_id) if pred(e)]
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError("等待事件超时")


def test_mid_run_failure_three_channels(tmp_path, caplog, monkeypatch):
    """provider 配了但端点不可达 → 研究零推进 → blocked（拦停+停滞诊断）三通道可见。

    重设计后的研究 step 对单点失败容错（维度组失败记 WARNING 后继续、零推进触发
    停滞诊断）——死 provider 在 command 层的可见形态是 blocked（带诊断与建议），
    不是裸 error；会话层随后续 follow-up turn 撞死 provider 记 turn/error → error。
    （历史备注：本测试曾依赖 `_router` 零参 stub 的 TypeError 偶然通过——stub 签名
    修正为 `lambda data_dir=None` 后，此处断言的是真实死 provider 路径。）
    """
    monkeypatch.setenv("FINANCE_AGENT_LLM_RETRY_ATTEMPTS", "1")
    client, events = make_client(tmp_path, monkeypatch=monkeypatch, dead_provider=True)
    logger = setup_logging("finance_agent_test_l1")
    mirror_events_to_logging(events, logger)

    with caplog.at_level(logging.WARNING, logger="finance_agent.research"), \
            caplog.at_level(logging.ERROR, logger="finance_agent.commands"):
        resp = client.post("/api/chat", json={"message": "/research AAA"})
    run_id = resp.json()["run_id"]

    done = wait_event(events, run_id, lambda e: e.type == "command/done")[0]
    # 通道 1a：父流 command/done(blocked) + 停滞摘要（含缺口与建议）
    assert done.payload["outcome"] == "blocked"
    assert "停滞" in done.payload["summary"]
    # 通道 1b：子流 step blocked + 停滞诊断事件（真实原因：缺口字段 + 修复建议）
    child = [e for e in events.read(run_id) if e.type == "step_agent/end"
             and e.payload["status"] == "blocked"][0]
    child_run = child.payload["child_run_id"]
    diag = [e for e in events.read(child_run) if e.type == "research/stall_diagnostic"][0]
    assert diag.payload["missing_fields"] and diag.payload["suggestions"]
    # 通道 2：日志（维度组失败/零推进 WARNING 留痕，运维可巡）
    assert any("维度组" in r.getMessage() or "零推进" in r.getMessage()
               for r in caplog.records)
    # 通道 3：用户可见状态——follow-up turn 撞死 provider → turn/error → 会话 error
    wait_event(events, run_id, lambda e: e.type == "turn/error")
    sessions = {s["run_id"]: s for s in client.get("/api/sessions").json()}
    assert sessions[run_id]["status"] == "error"
    assert sessions[run_id]["status_detail"]


def test_approval_timeout_fails_closed_visibly(tmp_path):
    """审批超时（fail-closed 按拒绝处理）：command/done(rejected) + 状态 cancelled。

    注：approval 闸在 runner 层、先于一切 step——超时后 eval_runner 不会被触达，
    这本身就是 fail-closed 的证明（无需探针）。
    """
    (tmp_path / "evals" / "mandates").mkdir(parents=True)
    (tmp_path / "evals" / "mandates" / "wf.json").write_text("{}")
    client, events = make_client(tmp_path, approval_timeout_s=0.2)
    resp = client.post("/api/chat", json={"message": "/evaluate wf"})
    run_id = resp.json()["run_id"]

    done = wait_event(events, run_id, lambda e: e.type == "command/done")[0]
    assert done.payload["outcome"] == "rejected"
    sessions = {s["run_id"]: s for s in client.get("/api/sessions").json()}
    assert sessions[run_id]["status"] == "cancelled"
