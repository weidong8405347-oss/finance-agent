"""行业入口一致性（audit §6 相邻风险）。

事故风险：本次实际走 `/research industry:...`；另一个 `/industry` 入口仍使用旧
F1–F5 路径，部分步骤未装配 plan/typed/synthesize——同一行业从不同入口会得到
不同质量的研究。

判据：F1 行业漏斗步骤与 `/research` 复用同一研究服务——冻结 targeted 计划、
typed 工具可用、产出落同一个 typed 库（同一页面可读）、预算与调度同源。
"""

from datetime import UTC, datetime
from pathlib import Path

from test_commands import make_deps

from finance_agent.commands.steps import StepContext, _industry_loop
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.research.calculations import CalculationService

NOW = datetime(2024, 6, 1, tzinfo=UTC)


def tc(i: int, name: str, args: dict) -> ToolCall:
    return ToolCall(call_id=f"c{i}", name=name, arguments=args)


#: F1 步骤脚本：检索 → 读正文窗口 → 登记证据 → typed 观测（与 /research 同一套工具）
F1_DOC = "行业市场规模 5000 million 美元。产能 2GW 公告。"
F1_SCRIPT = [
    AssistantReply(content="", tool_calls=[tc(0, "query_demo", {"ticker": "x"})]),
    AssistantReply(content="", tool_calls=[tc(1, "read_edgar_filing", {
        "chunk_id": "chk-0001", "query": "市场规模"})]),
    AssistantReply(content="", tool_calls=[tc(2, "register_evidence", {
        "evidence_id": "ev-f1", "chunk_id": "chk-0002",
        "verbatim_quote": "行业市场规模 5000 million 美元。"})]),
    AssistantReply(content="", tool_calls=[tc(3, "propose_metric", {
        "metric_key": "market_size", "value_text": "5000 million", "unit": "USD",
        "unit_text": "USD", "currency": "USD",
        "period": {"end": "2024-12-31", "frequency": "instant", "fiscal_label": "2024"},
        "evidence_ids": ["ev-f1"],
    })]),
    AssistantReply(content="f1 done"),
    AssistantReply(content="no further findings"),
]


def make_industry_deps(tmp_path: Path, scripts: dict[str, list]):
    """与 /research 相同的装配（metrics/metric_writer/calculations 全部就位）。"""
    deps, events, kb, approvals = make_deps(tmp_path, scripts)
    metrics = MetricStore(tmp_path / "m.db")
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    calcs = CalculationService(metrics, events=events)
    deps.metrics = metrics
    deps.metric_writer = mw
    deps.calculations = calcs
    deps.max_rounds = 1  # 单轮足够验证装配同源（脚本不必覆盖多轮）
    # 夹具正文必须含待登记的数字（否则证据子串校验 rightfully 拒登记）
    deps.fetch_document = lambda url: F1_DOC
    return deps, events, kb, metrics


def make_ctx(child_run: str = "child-f1") -> StepContext:
    return StepContext(
        command_id="cmd-ind", session_run_id="sess-ind", child_run_id=child_run,
        ticker="ai-for-science", objective="AI for Science 赛道地图", config="",
        should_cancel=lambda: False, entity_kind="industry", depth="standard",
    )


class TestIndustryEntryParity:
    def test_funnel_step_freezes_targeted_plan(self, tmp_path):
        """F1 步骤也冻结计划（旧路径完全没有 plan → 无问题驱动、无预算闸）。"""
        deps, events, kb, metrics = make_industry_deps(
            tmp_path, {"research": [F1_SCRIPT]})
        loop = _industry_loop(deps, make_ctx(), "industry_map")
        plans = metrics.plans_for("industry", "ai-for-science")
        assert plans, "F1 入口没有冻结研究计划（与 /research 不同源）"
        plan = plans[0]
        assert plan["mode"] == "targeted"
        assert plan["entity_kind"] == "industry"
        assert plan["questions"], "计划没有问题（targeted 应至少一题）"
        assert loop.plan_payload is not None
        created = [e for e in events.read("child-f1") if e.type == "research/plan_created"]
        assert created and created[0].payload["mode"] == "targeted"

    def test_typed_tools_available_and_output_lands_in_same_store(self, tmp_path):
        """typed 工具在 F1 入口可用，产出落同一个库（同一档案页可读）。"""
        deps, events, kb, metrics = make_industry_deps(
            tmp_path, {"research": [F1_SCRIPT]})
        loop = _industry_loop(deps, make_ctx(), "industry_map")
        obs = metrics.observations_as_of("industry", "ai-for-science", datetime.now(UTC))
        assert [o.metric_key for o in obs] == ["market_size"], \
            "F1 入口没有 typed 产出（旧路径只有 propose_fact）"
        assert obs[0].value == "5000000000"
        assert obs[0].evidence_refs == ["ev-f1"]
        # 调度与预算同源：research/schedule 或 research/budget 事件至少其一可回放
        types = {e.type for e in events.read("child-f1")}
        assert "research/budget" in types, f"预算闸未在 F1 入口生效：{sorted(types)}"

    def test_degrades_when_metrics_not_assembled(self, tmp_path):
        """新存储未装配 → 回落旧字段驱动路径（灰度开关，不阻断漏斗）。"""
        deps, events, kb, _ = make_deps(tmp_path, {"research": [F1_SCRIPT]})
        assert deps.metrics is None
        loop = _industry_loop(deps, make_ctx("child-legacy"), "industry_map")
        assert loop.plan_payload is None
        assert loop.stop_reason in ("converged", "stalled", "budget", None)

    def test_plan_disabled_flag_respected(self, tmp_path):
        deps, events, kb, metrics = make_industry_deps(
            tmp_path, {"research": [F1_SCRIPT]})
        deps.research_plan_enabled = False
        loop = _industry_loop(deps, make_ctx("child-noplan"), "industry_map")
        assert loop.plan_payload is None
        assert metrics.plans_for("industry", "ai-for-science") == []

    def test_context_budget_applies_to_funnel_step(self, tmp_path):
        """按需 evidence bundle（max_record_chars）在 F1 入口同样生效。"""
        deps, events, kb, metrics = make_industry_deps(
            tmp_path, {"research": [F1_SCRIPT]})
        deps.max_record_chars = 5  # 夹具记录字段短，阈值设小才能触发截断
        _industry_loop(deps, make_ctx("child-clamp"), "industry_map")
        results = [e for e in events.read("child-clamp") if e.type == "tool/result"]
        assert any("read_chunk(chunk_id=" in str(r.payload.get("content", ""))
                   for r in results), "长正文未按需截断（上下文成本未控制）"


class TestFunnelStepParityContract:
    def test_prepare_plan_overrides_do_not_mutate_ctx(self, tmp_path):
        """漏斗步骤用自己的 focus/objective 建计划，不改 ctx（各步互不污染）。"""
        from finance_agent.commands.steps import _prepare_research_plan

        deps, events, kb, metrics = make_industry_deps(
            tmp_path, {"research": [F1_SCRIPT]})
        ctx = make_ctx("child-ovr")
        plan_id = _prepare_research_plan(
            deps, ctx, focus_override="candidate_digging", mode_override="targeted",
            objective_override="标的池挖掘", entity_kind_override="industry",
        )
        assert plan_id
        assert ctx.focus == "" and ctx.objective == "AI for Science 赛道地图"
        plan = metrics.get_plan(plan_id)
        assert plan["objective"] == "标的池挖掘"
        assert plan["mode"] == "targeted"
        assert plan["scope"]["focus"] == "candidate_digging"
