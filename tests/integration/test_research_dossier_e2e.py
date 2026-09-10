"""端到端闭环验收（设计 §3.2 流程 3 / §12 M2 完成条件 / 附录 A）：

/research 命令（真实 CommandRunner + 真实 steps + 真实存储，LLM 仅边界替身）
→ 冻结研究计划 → typed 观测/论断写入 → 充分度评估 → 结构化报告产物（validated）
→ report.md 同源渲染 → 档案快照发布（dossier/published）→ 页面读模型可回源。

同时验收：运行完成（command completed）与产物状态（validated/sufficient）分离、
Sessions 流可见问题进度（桥接事件）、补研后档案模块被点亮（前后 diff）。
"""

import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path

from finance_agent.commands.registry import parse_command
from finance_agent.commands.runner import CommandRequest, CommandRunner
from finance_agent.commands.steps import StepDeps
from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.dossier.projector import DossierProjector
from finance_agent.dossier.service import DossierService
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.adapters.fixture import FixtureAdapter
from finance_agent.gateway.gateway import DataGateway
from finance_agent.gateway.models import DataRecord, SourceCapability
from finance_agent.harness.approvals import ApprovalService
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.models import PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.research.calculations import CalculationService

NOW = datetime.now(UTC)
DOC_TEXT = (
    "Bloom Energy FY2024 10-K excerpt. Total revenue 1500 million for fiscal 2024. "
    "Firm backlog of 300 million announced. Acceptance terms: customer sign-off required."
)
# focus 用非数值表述：数值题（含「订单/收入」等关键词）编译为 expects_typed_evidence，
# answered 需 obs-/calc- 引用（基线发现 F2 的门禁，专测见 test_baseline_findings_round2）；
# 本用例验证管道全链路，题目用定性 focus 保持脚本可静态化（obs id 运行期才生成）
FOCUS = "验收条款与客户签核"
TARGETED_QID = "targeted-" + hashlib.sha256(f"BE:{FOCUS}".encode()).hexdigest()[:8]


def tc(i: int, name: str, args: dict) -> ToolCall:
    return ToolCall(call_id=f"c{i}", name=name, arguments=args)


def wait_for(events, run_id, pred, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = [e for e in events.read(run_id) if pred(e)]
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError(f"等待事件超时: run={run_id} pred={pred}")


#: research step 脚本：检索 → 证据 → typed 观测 ×2 → 计算 → 论断 → 回答问题
RESEARCH_SCRIPT = [
    AssistantReply(content="", tool_calls=[tc(0, "query_demo", {})]),
    AssistantReply(content="", tool_calls=[tc(1, "read_edgar_filing",
                                              {"chunk_id": "chk-0001", "query": "backlog"})]),
    AssistantReply(content="", tool_calls=[tc(2, "register_evidence", {
        "chunk_id": "chk-0002", "evidence_id": "ev-e2e-1",
        "verbatim_quote": "Total revenue 1500 million for fiscal 2024"})]),
    AssistantReply(content="", tool_calls=[tc(3, "register_evidence", {
        "chunk_id": "chk-0002", "evidence_id": "ev-e2e-2",
        "verbatim_quote": "Firm backlog of 300 million announced"})]),
    AssistantReply(content="", tool_calls=[tc(4, "propose_metric", {
        "metric_key": "revenue", "value_text": "1500 million", "unit": "USD",
        "currency": "USD",
        "period": {"start": "2024-01-01", "end": "2024-12-31",
                   "frequency": "FY", "fiscal_label": "FY2024"},
        "evidence_ids": ["ev-e2e-1"],
    })]),
    AssistantReply(content="", tool_calls=[tc(5, "propose_metric", {
        "metric_key": "firm_backlog", "value_text": "300 million", "unit": "USD",
        "currency": "USD",
        "period": {"start": "2024-01-01", "end": "2024-12-31",
                   "frequency": "FY", "fiscal_label": "FY2024"},
        "evidence_ids": ["ev-e2e-2"],
    })]),
    AssistantReply(content="", tool_calls=[tc(6, "propose_claim", {
        "statement": "FY2024 收入 15 亿美元，firm backlog 3 亿美元，订单有验收条款约束",
        "kind": "fact_summary", "question_id": TARGETED_QID,
        "support_refs": ["ev-e2e-1", "ev-e2e-2"],
        "limitations": ["转化率未披露，无法量化订单→收入时滞"],
    })]),
    AssistantReply(content="", tool_calls=[tc(7, "answer_question", {
        "question_id": TARGETED_QID, "status": "answered",
        "conclusion": "订单与 backlog 有披露支持；验收条款是客户签核，转化率未披露（明确无法量化）",
        "support_refs": ["ev-e2e-1", "ev-e2e-2"],
    })]),
    AssistantReply(content="research round done"),
    AssistantReply(content="no further findings"),  # round2 无进展 → stalled（typed 产出有效）
]

#: synthesize step 脚本：结构化提交（固定 block；引用全部可解析）
SYNTHESIZE_SCRIPT = [
    AssistantReply(content="", tool_calls=[tc(0, "query_claims", {})]),
    AssistantReply(content="", tool_calls=[tc(1, "submit_report_document", {
        "title": "BE 研究报告（订单转化专项）",
        "blocks": [
            {"type": "heading", "level": 1, "text": "执行摘要"},
            {"type": "paragraph",
             "text": "FY2024 收入与 firm backlog 均有披露支持 [ev-e2e-1] [ev-e2e-2]，"
                     "订单转化受验收条款约束。"},
            {"type": "heading", "level": 1, "text": "分歧与缺口"},
            {"type": "gap_notice", "module": "expectations",
             "message": "转化率与 consensus 未披露——本模块降级，不虚构。"},
            {"type": "source_ref", "refs": ["ev-e2e-1", "ev-e2e-2"],
             "note": "FY2024 10-K 摘录"},
        ],
        "limitations": ["targeted 专项研究，不覆盖全部十模块"],
    })]),
    AssistantReply(content="report submitted"),
]


def make_env(tmp_path: Path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "metrics.db")
    events = EventStore(tmp_path / "events.db")
    decisions = DecisionStore(tmp_path / "decisions.db")
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    calcs = CalculationService(metrics, events=events)
    projector = DossierProjector(kb=kb, metrics=metrics, decisions=decisions)
    dossier = DossierService(kb=kb, metrics=metrics, projector=projector,
                             events=events, decisions=decisions)
    gateway = DataGateway(mode="live", events=events, run_id="live-e2e")
    gateway.register(FixtureAdapter(
        SourceCapability(source_id="demo", pit_grade=PitGrade.A,
                         server_side_asof=False, description="夹具披露源"),
        records=[DataRecord(source_id="demo", payload={"form": "10-K", "title": "FY2024"},
                            url="demo://be-10k", available_at=NOW)],
    ))
    calls: dict[str, int] = {}
    scripts = {"research": [RESEARCH_SCRIPT, SYNTHESIZE_SCRIPT]}

    def llm_for(role: str) -> MockLLM:
        script = scripts.get(role, [])
        idx = calls.get(role, 0)
        calls[role] = idx + 1
        return MockLLM(script[idx] if idx < len(script) else [AssistantReply(content="done")])

    deps = StepDeps(
        events=events, kb=kb, writer=ProfileWriter(store=kb, events=events),
        gateway=gateway,
        decisions=DecisionService(kb=kb, decisions=decisions, events=events),
        llm_for=llm_for, approvals=ApprovalService(events),
        evals_dir=tmp_path / "evals", reports_dir=tmp_path / "reports",
        knowledge_dir=tmp_path / "knowledge",
        metrics=metrics, metric_writer=mw, calculations=calcs, dossier_service=dossier,
        fetch_document=lambda url: DOC_TEXT,
        max_rounds=None,  # 计划预算接管（targeted → 2 轮）
    )
    return deps, events, kb, metrics, dossier


def test_research_command_full_loop(tmp_path):
    deps, events, kb, metrics, dossier = make_env(tmp_path)

    # 补研前的档案快照（前后 diff 的基线）
    snap_before, _ = dossier.open("stock", "BE")
    assert snap_before["modules"]["research_sources"]["status"] == "missing"
    assert snap_before["modules"]["key_kpi"]["status"] == "missing"

    runner = CommandRunner(deps, approval_timeout_s=0.2)
    parsed = parse_command(f"/research BE --depth=targeted --focus={FOCUS}")
    assert parsed is not None and parsed.extra.get("depth") == "targeted"
    command_id = runner.start(CommandRequest(session_run_id="sess-e2e", parsed=parsed))
    done = wait_for(
        events, "sess-e2e",
        lambda e: e.type == "command/done" and e.payload["command_id"] == command_id,
    )[0]

    # 1) 运行状态：管道完成（RunStatus 与产物状态分离验收在下面）
    assert done.payload["outcome"] == "completed", done.payload["summary"]

    # 2) 研究计划冻结 + 问题进度对 Sessions 可见（桥接事件，§10.3）
    progress = [e for e in events.read("sess-e2e") if e.type == "step_agent/progress"]
    summaries = " ".join(str(p.payload.get("summary", "")) for p in progress)
    assert "研究计划冻结" in summaries and "targeted" in summaries
    assert "问题进展" in summaries and "answered" in summaries
    assert "研究充分度" in summaries

    # 3) typed 产出落库：观测带血缘、论断 validated
    obs = metrics.observations_as_of("stock", "BE", datetime.now(UTC))
    keys = {o.metric_key for o in obs}
    assert {"revenue", "firm_backlog"} <= keys
    claims = metrics.claims_as_of("stock", "BE", datetime.now(UTC))
    assert claims and claims[0]["status"] == "validated"
    assert claims[0]["limitations"]  # 「无法量化」的诚实限制进了论断

    # 4) 评估门禁：targeted 问题 answered → sufficient（硬门禁代码运行）
    assessments = [
        e for run in ("sess-e2e",) for e in events.read(run)
        if e.type == "step_agent/progress" and "研究充分度" in str(e.payload.get("summary"))
    ]
    assert assessments and "sufficient" in str(assessments[0].payload["summary"])

    # 5) 产物冻结：validated + report.md 同源渲染（不是 LLM 自由文本直接落盘）
    artifacts = metrics.artifacts_as_of("stock", "BE", datetime.now(UTC))
    assert len(artifacts) == 1
    art = artifacts[0]
    assert art["status"] == "validated" and art["sufficiency"] == "sufficient"
    report_files = list((tmp_path / "reports").rglob("report.md"))
    assert report_files
    md = report_files[0].read_text(encoding="utf-8")
    assert "执行摘要" in md and "ev-e2e-1" in md  # block 结构与引用同源渲染
    assert "产物" in md and "validated" in md  # 状态元信息（完成≠可用 的分离展示）
    published = [e for e in events.read(_synth_run_id(events, "sess-e2e"))
                 if e.type == "research/artifact_created"]
    assert published and published[0].payload["status"] == "validated"

    # 6) 档案快照发布：dossier/published 进会话流，模块被研究点亮（前后 diff）
    pub = wait_for(events, "sess-e2e", lambda e: e.type == "dossier/published")
    assert pub and pub[0].payload["entity"] == "stock:BE"
    snap_after = dossier.get(pub[0].payload["snapshot_id"])
    assert snap_after["modules"]["research_sources"]["status"] == "ready"
    assert snap_after["modules"]["key_kpi"]["status"] in ("ready", "partial")
    km = {m["metric_key"]: m for m in snap_after["summary"]["key_metrics"]}
    # 无行业提示时配方识别回落通用模板（不硬套 KPI）→ 首屏指标栏用通用 KPI（typed，非猜数）
    assert snap_after["recipe"]["id"] == "general"
    assert km["revenue"]["value"] == "1500000000"
    assert km["revenue"]["observation_id"]
    assert snap_after["summary"]["thesis_kind"] == "claim"
    diff = dossier.changes(snap_after["context"]["snapshot_id"],
                           snap_before["context"]["snapshot_id"])
    assert "research_sources" in diff["changed_modules"]
    assert any(c["metric_key"] == "revenue" for c in diff["key_metric_changes"])

    # 7) 证据一次点击可回源：快照引用的证据在来源目录中
    assert {"ev-e2e-1", "ev-e2e-2"} <= set(snap_after["evidence_refs"])


def _synth_run_id(events, session: str) -> str:
    starts = [
        e for e in events.read(session)
        if e.type == "step_agent/start" and e.payload.get("step") == "synthesize"
    ]
    assert starts, "synthesize step 未启动"
    return starts[0].payload["child_run_id"]


def test_deep_flag_enters_manifest_and_plan(tmp_path):
    """--depth=deep 进入计划模式与预算（§7.1 参数由命令解析层处理并进 manifest）。"""
    deps, events, kb, metrics, dossier = make_env(tmp_path)
    runner = CommandRunner(deps, approval_timeout_s=0.2)
    parsed = parse_command("/research BE --depth=deep 全面理解公司")
    assert parsed is not None and parsed.extra["depth"] == "deep"
    command_id = runner.start(CommandRequest(session_run_id="sess-deep", parsed=parsed))
    wait_for(events, "sess-deep",
             lambda e: e.type == "command/done" and e.payload["command_id"] == command_id,
             timeout=20.0)
    plans = metrics.plans_for("stock", "BE")
    assert plans and plans[0]["mode"] == "deep"
    assert plans[0]["budgets"]["max_rounds"] == 5  # deep 预算表（§7.7）
    assert len(plans[0]["questions"]) >= 12  # deep 12–18 个问题
