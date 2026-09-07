"""Step agents：command pipeline 的执行单元（各带 kernel loop + tools + hooks）。

纪律（redesign §3.3）：
- 每个 step 跑在独立 child run（run/created 带 parent_run_id），上下文互相隔离；
- step 间数据接力经 KB / DecisionStore / 结构化摘要，不经共享对话；
- 失败三通道：子流 error 事件 + 父流 step_agent/end(status=error) + 日志（runner 负责）。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_futs
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..decision.loop import DecisionLoop
from ..decision.service import DecisionService
from ..eventstore.events import (
    CONTEXT_INJECT,
    REPORT_PUBLISHED,
    RUN_CREATED,
    Event,
)
from ..eventstore.store import EventStore
from ..gateway.gateway import DataGateway
from ..gateway.tools import make_gateway_tool
from ..harness.approvals import ApprovalService
from ..harness.manifest import RunManifest, RunMode
from ..knowledge.gaps import GapAnalyzer
from ..knowledge.models import Fact
from ..knowledge.store import BitemporalStore
from ..knowledge.writer import ProfileWriter
from ..llm.base import LLM
from ..loop.kernel import AgentKernel
from ..research.calc import calc_tool
from ..research.evidence_desk import ChunkStore
from ..research.loop import ResearchLoop
from ..research.playbooks import load_playbook
from ..research.prompts import GROUNDING_CONTRACT
from ..research.tools import make_research_tools

logger = logging.getLogger("finance_agent.steps")

# S2 thesis 修订的契约（证据绑定纪律不变：thesis 也必须绑证据）
_PROFILE_CONTRACT = """\
你是档案修订员。基于档案事实（query_kb 可查，含每条事实的证据 id）修订该标的的投资论点。
纪律：论点只能建立在档案事实之上；propose_thesis 必须绑定支撑它的证据 id。
"""

PROFILE_TOOL_SCHEMAS: dict[str, dict] = {
    "query_kb": {
        "name": "query_kb",
        "description": "查询当前标的档案投影（含每条事实的证据 id）",
        "parameters": {"type": "object", "properties": {}},
    },
    "propose_thesis": {
        "name": "propose_thesis",
        "description": "提交修订后的投资论点（必须绑定支撑证据 id）",
        "parameters": {
            "type": "object",
            "properties": {
                "thesis": {"type": "string", "description": "修订后的投资论点全文"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["thesis", "evidence_ids"],
        },
    },
}


@dataclass
class StepContext:
    command_id: str
    session_run_id: str  # 父（会话）流
    child_run_id: str  # 本 step 的子 run
    ticker: str
    objective: str
    config: str
    should_cancel: Callable[[], bool]
    entity_kind: str = "stock"  # industry:<slug> 标的形态 → industry
    # 研究升级参数（§7.1）：命令解析层 → manifest → ResearchPlan
    depth: str = "standard"  # standard/deep/refresh/targeted
    focus: str = ""
    base_snapshot: str = ""


@dataclass
class StepResult:
    status: str  # completed | error | blocked | cancelled
    summary: str


@dataclass
class StepDeps:
    """step agent 的共享依赖（由装配层注入；测试用假件）。每个字段都是真实接缝。"""

    events: EventStore
    kb: BitemporalStore
    writer: ProfileWriter
    gateway: DataGateway
    decisions: DecisionService
    llm_for: Callable[[str], LLM]  # role → LLM（tool schemas 已在路由层绑定）
    approvals: ApprovalService
    evals_dir: Path
    reports_dir: Path
    knowledge_dir: Path  # 档案磁盘投影（HTML 存档）；必填——测试绝不许写进仓库工作树
    judge_llm: LLM | None = None
    eval_runner: Callable[..., dict[str, Any]] | None = None  # (config_name, child_run_id) → summary dict
    fetch_document: Callable[[str], str] | None = None  # 文档正文抓取（生产研究用；eval 回放经
                                                       # ReplayEngine 自带）
    max_rounds: int | None = None  # None → 动态预算（P3 §4.2：0%→5 轮/>50%→3 轮/仅刷新→1 轮）
    max_steps_per_round: int = 16
    # ---- 档案升级（knowledge-dossier-research-redesign §12.1）：typed 观测/计算/快照 ----
    metrics: Any | None = None  # MetricStore（缺省 = 新链路不启用，旧管线行为不变）
    metric_writer: Any | None = None  # TypedMetricWriter
    calculations: Any | None = None  # CalculationService
    dossier_service: Any | None = None  # DossierService（synthesize 后发布快照）
    research_plan_enabled: bool = True  # 灰度开关（§11.3）
    #: 维度并行 worker 池工厂（P3 §4.4）：n → n 个 flash LLM；None → 单模型串行
    worker_llm_for: Callable[[int], list[LLM]] | None = None
    completeness_target: float = 0.8
    decide_max_attempts: int = 2  # Q7：硬门禁打回的有界重试


def _open_child(deps: StepDeps, ctx: StepContext, step: str) -> RunManifest:
    """子 run 落 run/created（parent_run_id 关联；sessions 列表据此排除子 run）。"""
    deps.events.append(
        Event(
            run_id=ctx.child_run_id,
            type=RUN_CREATED,
            payload={
                "parent_run_id": ctx.session_run_id,
                "kind": "step_agent",
                "step": step,
                "command_id": ctx.command_id,
            },
        )
    )
    return RunManifest(run_id=ctx.child_run_id, mode=RunMode.LIVE)


def _cancelled(ctx: StepContext) -> StepResult:
    return StepResult(status="cancelled", summary="已被用户停止")


# ---------------- S1 研究 ----------------


def step_research(deps: StepDeps, ctx: StepContext) -> StepResult:
    if ctx.should_cancel():
        return _cancelled(ctx)
    manifest = _open_child(deps, ctx, "research")
    # 问题驱动研究（§7）：冻结 ResearchPlan 后，终止由问题覆盖+字段覆盖+预算共同决定；
    # 已有 100% 档案遇到新目标仍创建计划（只复用有效证据，不宣告「无需研究」）
    plan_id = _prepare_research_plan(deps, ctx)
    loop = ResearchLoop(
        store=deps.kb,
        events=deps.events,
        writer=deps.writer,
        gateway=deps.gateway,
        llm=deps.llm_for("research"),
        manifest=manifest,
        max_rounds=deps.max_rounds,
        completeness_target=deps.completeness_target,
        gateway_sources=deps.gateway.source_ids(),
        max_steps_per_round=deps.max_steps_per_round,
        judge_llm=deps.judge_llm,
        should_stop=ctx.should_cancel,
        fetch_document=deps.fetch_document,
        worker_llms=deps.worker_llm_for(4) if deps.worker_llm_for else None,
        plan_id=plan_id,
        metrics=deps.metrics,
        metric_writer=deps.metric_writer,
        calculations=deps.calculations,
    )
    reports = loop.run(
        ctx.entity_kind, ctx.ticker, ctx.objective or f"深度研究 {ctx.ticker}"
    )
    if loop.stop_reason == "cancelled":
        return _cancelled(ctx)
    gaps = GapAnalyzer(deps.kb).analyze(ctx.entity_kind, ctx.ticker, datetime.now(UTC))

    # 轮次摘要落盘（钻取用）；report/published 卡片由 synthesize step 负责（Q3）
    _write_research_artifact(deps, ctx, reports, loop.stop_reason or "", gaps.completeness)
    if loop.stop_reason == "stalled":
        diag = loop.stall_diagnostic or {}
        missing = diag.get("missing_fields") or []
        sugg = "；".join(diag.get("suggestions") or [])
        last = reports[-1]
        total_written = sum(len(r.facts_written) for r in reports)
        # 粒度区分：整轮零产出（本轮研究一无所获）→ blocked 拦停管道，
        # 不再让后续 step 对空档案空烧 token；已有进展后的末轮停滞 = 自然收敛，
        # 研究产出有效，管道继续（摘要留痕停滞原因）。
        if total_written == 0:
            summary = (
                f"研究停滞（stalled）：{len(reports)} 轮后完整度 "
                f"{last.completeness_before:.0%} → {last.completeness_after:.0%}，"
                f"缺口 {len(missing)} 字段"
                f"（{'、'.join(missing[:5])}{'…' if len(missing) > 5 else ''}）"
                + (f"；建议：{sugg}" if sugg else "")
            )
            return StepResult(status="blocked", summary=summary)
        summary = (
            f"研究 {len(reports)} 轮（stalled，累计写入 {total_written} 字段后停滞）："
            f"完整度 {last.completeness_before:.0%} → {last.completeness_after:.0%}"
            + (f"；残余缺口 {len(missing)} 字段；建议：{sugg}" if sugg else "")
        )
        return StepResult(status="completed", summary=summary)
    if not reports:
        summary = f"档案完整度已达标（{gaps.completeness:.0%}）且无待回答问题，无需新一轮研究"
    else:
        last = reports[-1]
        summary = (
            f"研究 {len(reports)} 轮（{loop.stop_reason}）："
            f"完整度 {last.completeness_before:.0%} → {last.completeness_after:.0%}，"
            f"写入 {len(last.facts_written)} 字段"
        )
        typed_out = (
            len(last.observations_written) + len(last.claims_written) + len(last.calculations_done)
        )
        if typed_out:
            summary += f"，typed 产出 {typed_out} 项（观测/论断/计算）"
    # 研究充分度（§7.6）：字段完整 ≠ 研究充分——评估结果进摘要（RunStatus 与产物状态分离）
    if loop.assessment is not None:
        a = loop.assessment
        cov = a.question_coverage
        summary += (
            f"；研究充分度 {a.verdict}（问题覆盖 {cov.answered}/{cov.applicable}，"
            f"硬门禁{'通过' if a.hard_gate_passed else '未过'}）"
        )
        if a.gaps:
            summary += f"；缺口：{'；'.join(a.gaps[:3])}"
    return StepResult(status="completed", summary=summary)


def _prepare_research_plan(deps: StepDeps, ctx: StepContext) -> str | None:
    """创建并冻结 ResearchPlan（§7.1/§7.5）：配方识别 + 问题模板 + 缺口提级。

    新存储未装配或开关关闭 → None（旧管线行为不变，§11.3 灰度）。"""
    if deps.metrics is None or not deps.research_plan_enabled:
        return None
    from ..research.plan import build_plan, load_recipe, select_recipe

    try:
        gaps = GapAnalyzer(deps.kb).analyze(ctx.entity_kind, ctx.ticker, datetime.now(UTC))
        hints = ""
        view = deps.kb.view(ctx.entity_kind, ctx.ticker, datetime.now(UTC))
        for field in ("business_model", "moat", "peers", "value_chain"):
            rec = view.get(field)
            if rec is not None and isinstance(rec.value, str):
                hints += " " + rec.value
        recipe_id, basis = select_recipe(
            ctx.entity_kind, hint_text=hints[:2000],
            explicit=(
                ctx.focus
                if ctx.focus in ("general", "industrial_equipment", "biotech", "industry")
                else None
            ),
        )
        recipe = load_recipe(recipe_id)
        mode = ctx.depth if ctx.depth in ("standard", "deep", "refresh", "targeted") else "standard"
        if ctx.focus and mode == "standard":
            mode = "targeted"  # 显式 focus 默认升级为 targeted（一个问题簇）
        plan = build_plan(
            entity_kind=ctx.entity_kind,
            entity_id=ctx.ticker,
            objective=ctx.objective or f"深度研究 {ctx.ticker}",
            mode=mode,
            recipe=recipe,
            focus=ctx.focus,
            missing_fields=list(gaps.missing),
            stale_fields=list(gaps.stale),
            base_snapshot_id=ctx.base_snapshot or None,
            run_id=ctx.child_run_id,
        )
        payload = plan.model_dump(mode="json")
        logger.info("研究计划冻结 %s（%s，配方 %s：%s）", plan.plan_id, mode, recipe_id, basis)
        return deps.metrics.save_plan(plan_id=plan.plan_id, namespace="prod", payload=payload)
    except Exception as e:
        # 计划创建失败不阻断旧管线（降级可见：事件+日志），但必须留痕
        logger.warning("研究计划创建失败（降级为字段驱动研究）：%s", e, exc_info=True)
        deps.events.append(
            Event(run_id=ctx.child_run_id, type="research/error",
                  payload={"reason": f"研究计划创建失败（降级）: {type(e).__name__}: {e}"})
        )
        return None


def _write_research_artifact(
    deps: StepDeps, ctx: StepContext, reports: list, stop_reason: str, completeness: float
) -> Path:
    """研究长文落磁盘 artifact（事件只带摘要+指针，ResearchFoldCard 的数据源）。"""
    out_dir = deps.reports_dir / ctx.child_run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "research.md"
    lines = [f"# {ctx.ticker} 深度研究报告\n", f"- stop_reason: {stop_reason}",
             f"- 最终完整度: {completeness:.0%}\n"]
    for r in reports:
        lines.append(f"## 第 {r.round} 轮\n")
        lines.append(f"- 完整度: {r.completeness_before:.0%} → {r.completeness_after:.0%}")
        lines.append(f"- 写入字段: {', '.join(r.facts_written) or '无'}")
        if r.rejected:
            lines.append(f"- 被拒: {json.dumps(r.rejected, ensure_ascii=False)}")
        if r.missing_after:
            lines.append(f"- 仍缺: {', '.join(r.missing_after)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------- 报告合成（CIO 综合，Q3） ----------------

_SYNTHESIZE_CONTRACT = """\
你是 CIO（首席投资官）。基于档案事实写一份结构化研究报告（Markdown）。
纪律：
1. 用 query_kb 读档案（每条事实含证据 id）；用 read_evidence 核对证据原文。
2. 每个事实性断言后紧跟证据锚点，形如 [ev-xxx]；数字必须与档案中的值逐字一致。
3. 缺证据/未知的维度明确写「未知」——宁可留白，不可编造。
4. 结构：## 摘要（3-5 句判断性结论）/ ## 业务与模式 / ## 财务质量 / ## 护城河 /
   ## 风险与反方 / ## 估值锚点 / ## 未知与缺口。
"""

_SYNTHESIZE_CONTRACT_V2 = """\
你是 CIO（首席投资官）。基于档案事实、typed 观测与研究论断，提交一份结构化研究报告。
工作流：
1. query_kb 读旧字段档案；query_observations 读结构化指标观测；query_claims 读研究论断；
   read_evidence 核对证据原文。
2. 用 submit_report_document 提交结构化报告（固定 block 类型）：
   - heading/paragraph：每章「结论—证据—推导—限制」，结论尽量短；
   - claim：引用已登记 claim_id（不要重写论断文本）；
   - metric_table/chart_ref：数值单元格给 observation_id（服务端以引用值为准，
     禁止自由改写数字）；图表只用 registry 内的 chart_id（metric_line/segment_bar/
   peer_bar/sensitivity_table）；
   - assumption_table：估值/情景假设 + calculation_ref；
   - source_ref：证据/文档引用；gap_notice：缺口显式声明。
3. 段落里引用证据写 [ev-xxx]；引用关键数值写 {{metric:<observation_id>}}（服务端插值）。
   未登记的 id 会被验证拒绝——不要编造引用。
4. 章节顺序：执行摘要 → 变化与核心问题 → 商业与行业 → 财务与 KPI → 预期 →
   估值假设 → 竞争与管理层 → 风险与反方 → 催化/监测 → 分歧与缺口 → 来源/方法。
   数据不足的章节用 gap_notice 明确降级，不以空图宣称完成。
5. 不发布买卖评级、仓位与目标价区间（那是 /decide 的域，D1 边界）。
"""


def step_synthesize(deps: StepDeps, ctx: StepContext) -> StepResult:
    """研究轮收敛后的报告合成：档案 + 证据 + typed 观测/论断 → 结构化研报。

    v2（新存储装配时）：LLM 产候选 ReportDocument block 结构，服务端验证引用后
    渲染 Markdown（report.md、档案概览、Sessions 摘要同源，§7.8），冻结为
    ResearchArtifact；产物状态（draft/validated）与研究充分度（sufficiency）分离，
    运行完成（completed）不再被误读为「可用研报」。
    """
    if ctx.should_cancel():
        return _cancelled(ctx)
    now = datetime.now(UTC)
    view = deps.kb.view(ctx.entity_kind, ctx.ticker, now)
    manifest = _open_child(deps, ctx, "synthesize")

    report_title = f"{ctx.ticker} 研究报告"
    if not view and deps.metrics is None:
        summary = "档案为空，无内容可合成（先跑研究）"
        artifact = _write_text_artifact(deps, ctx, "report.md", f"# {report_title}\n\n{summary}\n")
        _publish_report(deps, ctx, report_title, summary, artifact, ["evidence-gap"])
        return StepResult(status="completed", summary=summary)
    if not view:
        # 空档案 + 新链路：仍发布缺口说明，但产物 sufficiency=blocked（状态与内容质量分离）
        return _synthesize_empty_v2(deps, ctx, report_title, now)

    def query_kb(_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": json.dumps(
                {
                    f: {"value": r.value, "evidence_ids": r.evidence_ids, "conflict": r.conflict_flag}
                    for f, r in view.items()
                },
                ensure_ascii=False,
                default=str,
            ),
            "provenance": [],
        }

    def read_evidence(args: dict[str, Any]) -> dict[str, Any]:
        try:
            ev = deps.kb.get_evidence(str(args["evidence_id"]))
        except Exception as e:
            return {"content": f"error: {e}", "provenance": []}
        return {
            "content": json.dumps(
                {
                    "evidence_id": ev.evidence_id,
                    "source_id": ev.source_id,
                    "url": ev.url,
                    "verbatim_quote": ev.verbatim_quote,
                    "available_at": ev.available_at.isoformat() if ev.available_at else None,
                },
                ensure_ascii=False,
            ),
            "provenance": [
                {
                    "source_id": ev.source_id,
                    "available_at": ev.available_at.isoformat() if ev.available_at else None,
                    "pit_grade": ev.pit_grade.value,
                }
            ],
        }

    tools: dict[str, Any] = {"query_kb": query_kb, "read_evidence": read_evidence}
    submitted: dict[str, Any] | None = None
    contract = _SYNTHESIZE_CONTRACT
    if deps.metrics is not None:
        contract = _SYNTHESIZE_CONTRACT_V2

        def query_observations(_args: dict[str, Any]) -> dict[str, Any]:
            obs = deps.metrics.observations_as_of(ctx.entity_kind, ctx.ticker, datetime.now(UTC))
            return {"content": json.dumps([
                {"observation_id": o.observation_id, "metric_key": o.metric_key,
                 "value": o.value, "unit": o.unit, "currency": o.currency,
                 "period": o.period.fiscal_label or o.period.end.isoformat(),
                 "frequency": o.period.frequency, "nature": o.nature, "basis": o.basis,
                 "dimensions": o.dimensions, "status": o.status,
                 "evidence_refs": o.evidence_refs}
                for o in obs
            ][:200], ensure_ascii=False), "provenance": []}

        def query_claims(_args: dict[str, Any]) -> dict[str, Any]:
            claims = deps.metrics.claims_as_of(
                ctx.entity_kind, ctx.ticker, datetime.now(UTC),
                statuses=("draft", "validated"),
            )
            return {"content": json.dumps(claims[:100], ensure_ascii=False), "provenance": []}

        def submit_report_document(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal submitted
            submitted = args
            return {"content": json.dumps({"accepted": True, "blocks": len(args.get("blocks") or [])},
                                          ensure_ascii=False), "provenance": []}

        tools.update({
            "query_observations": query_observations,
            "query_claims": query_claims,
            "submit_report_document": submit_report_document,
        })

    deps.events.append(
        Event(
            run_id=ctx.child_run_id,
            type=CONTEXT_INJECT,
            payload={"role": "system", "content": contract},
        )
    )
    kernel = AgentKernel(
        store=deps.events,
        llm=deps.llm_for("research"),
        manifest=manifest,
        tools=tools,
        max_steps=12 if deps.metrics is not None else 8,
    )
    report_md = kernel.run_turn(f"为 {ctx.entity_kind}:{ctx.ticker} 写研究报告。")

    if deps.metrics is not None:
        return _finalize_artifact_v2(
            deps, ctx, report_title, view, report_md, submitted, now
        )

    artifact = _write_text_artifact(deps, ctx, "report.md", report_md or "（空报告）")
    # 摘要 = 报告首个「## 摘要」节的文本（取不到就首段）
    summary = _extract_summary(report_md) or "报告已生成"
    gaps = GapAnalyzer(deps.kb).analyze(ctx.entity_kind, ctx.ticker, now)
    flags: list[str] = []
    if gaps.missing:
        flags.append(f"evidence-gap: {len(gaps.missing)} 项缺口")
    if gaps.conflicts:
        flags.append(f"conflict: {len(gaps.conflicts)} 项待裁决")
    _publish_report(deps, ctx, report_title, summary, artifact, flags)
    return StepResult(status="completed", summary=summary)


# ---------------- v2 报告产物（ReportDocument/ResearchArtifact，§7.8） ----------------

SYNTHESIZE_TOOL_SCHEMAS: dict[str, dict] = {
    "query_observations": {
        "name": "query_observations",
        "description": "查询当前实体的 typed 指标观测（结构化数值，带 observation_id/期间/口径/证据）",
        "parameters": {"type": "object", "properties": {}},
    },
    "query_claims": {
        "name": "query_claims",
        "description": "查询当前实体的研究论断（claim_id/statement/kind/status/支持反方引用）",
        "parameters": {"type": "object", "properties": {}},
    },
    "submit_report_document": {
        "name": "submit_report_document",
        "description": (
            "提交结构化研究报告（固定 block 类型，服务端验证引用后冻结）。"
            "blocks 每项带 type：heading{level,text} / paragraph{text} / claim{claim_id,note} / "
            "metric_table{title,columns,rows[[{label,value,observation_id,unit,note}]]} / "
            "chart_ref{chart_id,title,metric_refs,caption}（chart_id 限：metric_line/segment_bar/"
            "peer_bar/sensitivity_table）/ comparison{title,items} / "
            "assumption_table{title,assumptions,calculation_ref} / source_ref{refs,note} / "
            "gap_notice{module,message}。段落内证据锚点 [ev-xxx]，数值插值 {{metric:<observation_id>}}。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "blocks": {"type": "array", "items": {"type": "object"}},
                "limitations": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["blocks"],
        },
    },
}


def _latest_assessment(deps: StepDeps, entity: str) -> dict[str, Any] | None:
    """最新 research/assessment 事件投影（研究 step 与 synthesize step 跨子 run 接力）。"""
    try:
        rows = deps.events._conn.execute(  # noqa: SLF001 - 投影层只读
            "SELECT payload FROM events WHERE type = 'research/assessment'"
            " AND json_extract(payload, '$.entity') = ? ORDER BY seq DESC LIMIT 1",
            (entity,),
        ).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    payload = rows[0][0]
    return json.loads(payload) if isinstance(payload, str) else payload


def _synthesize_empty_v2(
    deps: StepDeps, ctx: StepContext, report_title: str, now: datetime
) -> StepResult:
    """空档案 + 新链路：发布缺口说明产物（sufficiency=blocked，status=draft）。"""
    from ..research.artifacts import (
        GapNoticeBlock,
        HeadingBlock,
        ReportDocument,
        ResearchArtifact,
    )

    summary = "档案为空，无内容可合成（先跑研究）；产物已标记 sufficiency=blocked"
    doc = ReportDocument(
        title=report_title, entity_kind=ctx.entity_kind, entity_id=ctx.ticker,
        blocks=[
            HeadingBlock(level=1, text="缺口说明"),
            GapNoticeBlock(module="research_sources", message=summary),
        ],
    ).with_id()
    artifact = ResearchArtifact(
        entity_kind=ctx.entity_kind, entity_id=ctx.ticker, title=report_title,
        report_document=doc, status="draft", sufficiency="blocked",
        created_at=now, run_id=ctx.child_run_id,
        markdown=f"# {report_title}\n\n{summary}\n",
    ).with_id()
    deps.metrics.save_artifact(
        artifact_id=artifact.artifact_id, namespace="prod", payload=artifact.model_dump(mode="json")
    )
    deps.events.append(Event(
        run_id=ctx.child_run_id, type="research/artifact_created",
        payload={"artifact_id": artifact.artifact_id, "title": report_title,
                 "status": "draft", "sufficiency": "blocked",
                 "entity": f"{ctx.entity_kind}:{ctx.ticker}"},
    ))
    path = _write_text_artifact(deps, ctx, "report.md", artifact.markdown)
    _publish_report(deps, ctx, report_title, summary, path, ["evidence-gap", "sufficiency: blocked"])
    return StepResult(status="completed", summary=summary)


def _finalize_artifact_v2(
    deps: StepDeps,
    ctx: StepContext,
    report_title: str,
    view: dict[str, Any],
    report_md: str | None,
    submitted: dict[str, Any] | None,
    now: datetime,
) -> StepResult:
    """验证→冻结→发布：ReportDocument → ResearchArtifact → report.md → 档案快照。

    同源纪律（§7.8）：report.md 由冻结产物渲染，不从 LLM 自由文本直接落盘；
    验证硬 issue（引用不可解析）存在时产物只能是 draft（不得 validated）。
    """
    from ..research.artifacts import (
        ArtifactValidator,
        ReportDocument,
        ResearchArtifact,
        document_from_markdown,
        render_markdown,
    )

    parse_error: str | None = None
    doc: Any = None
    if submitted is not None:
        try:
            doc = ReportDocument.model_validate({
                "title": submitted.get("title") or report_title,
                "entity_kind": ctx.entity_kind,
                "entity_id": ctx.ticker,
                "blocks": submitted.get("blocks") or [],
                "limitations": submitted.get("limitations") or [],
            }).with_id()
        except Exception as e:  # 结构非法 → 降级解析 Markdown（可见，不静默）
            parse_error = f"{type(e).__name__}: {e}"
            logger.warning("submit_report_document 结构非法（降级 Markdown 解析）：%s", e)
    if doc is None:
        doc = document_from_markdown(
            report_md or "", title=report_title,
            entity_kind=ctx.entity_kind, entity_id=ctx.ticker,
        )
        if parse_error:
            doc.limitations.append(f"结构化提交解析失败，降级自 Markdown：{parse_error[:200]}")

    assessment = _latest_assessment(deps, f"{ctx.entity_kind}:{ctx.ticker}")
    sufficiency = (assessment or {}).get("verdict") or "partial"
    if sufficiency not in ("sufficient", "partial", "blocked"):
        sufficiency = "partial"
    plans = deps.metrics.plans_for(ctx.entity_kind, ctx.ticker, limit=1)
    plan_id = plans[0].get("plan_id") if plans else None
    claims = deps.metrics.claims_as_of(
        ctx.entity_kind, ctx.ticker, now, statuses=("draft", "validated")
    )
    artifact = ResearchArtifact(
        entity_kind=ctx.entity_kind, entity_id=ctx.ticker, title=report_title,
        report_document=doc,
        claim_ids=[c["claim_id"] for c in claims],
        calculation_ids=[],
        plan_id=plan_id,
        status="draft",
        sufficiency=sufficiency,  # type: ignore[arg-type]
        created_at=now,
        evidence_cutoff=now,
        run_id=ctx.child_run_id,
    ).with_id()
    # 服务端验证（integrity 硬门禁的执行点）
    issues = ArtifactValidator(kb=deps.kb, metric_store=deps.metrics).validate(artifact)
    artifact.validation_issues = issues
    hard_failed = any(i.hard for i in issues)
    artifact.status = "draft" if (hard_failed or sufficiency == "blocked") else "validated"
    artifact.markdown = render_markdown(artifact, store=deps.metrics, kb=deps.kb)
    deps.metrics.save_artifact(
        artifact_id=artifact.artifact_id, namespace="prod",
        payload=artifact.model_dump(mode="json"),
    )
    deps.events.append(Event(
        run_id=ctx.child_run_id, type="research/artifact_created",
        payload={
            "artifact_id": artifact.artifact_id,
            "entity": f"{ctx.entity_kind}:{ctx.ticker}",
            "title": report_title,
            "status": artifact.status,
            "sufficiency": artifact.sufficiency,
            "plan_id": plan_id,
            "claim_ids": artifact.claim_ids,
            "validation_issues": [i.model_dump(mode="json") for i in issues[:20]],
        },
    ))
    if hard_failed:
        logger.warning(
            "研究产物 %s 硬校验未过（%d 项），保持 draft：%s",
            artifact.artifact_id, len([i for i in issues if i.hard]),
            [i.message for i in issues if i.hard][:5],
        )
    # report.md 与页面同源：由冻结产物渲染（不再直接落 LLM 自由文本）
    path = _write_text_artifact(deps, ctx, "report.md", artifact.markdown)
    summary = _extract_summary(artifact.markdown) or "报告已生成"
    gaps = GapAnalyzer(deps.kb).analyze(ctx.entity_kind, ctx.ticker, now)
    flags: list[str] = []
    if gaps.missing:
        flags.append(f"evidence-gap: {len(gaps.missing)} 项缺口")
    if gaps.conflicts:
        flags.append(f"conflict: {len(gaps.conflicts)} 项待裁决")
    flags.append(f"artifact: {artifact.status}/{artifact.sufficiency}")
    _publish_report(deps, ctx, report_title, summary, path, flags)

    # 档案快照发布（dossier/published 事件由 service 落；失败不阻断报告发布，
    # 但必须可见：service 内部落 dossier/publish_failed + 日志）
    snapshot_id = None
    if deps.dossier_service is not None:
        try:
            snap, _created = deps.dossier_service.open(
                ctx.entity_kind, ctx.ticker, run_id=ctx.session_run_id
            )
            snapshot_id = snap["context"]["snapshot_id"]
        except Exception as e:
            logger.error("档案快照发布失败 %s:%s: %s", ctx.entity_kind, ctx.ticker, e, exc_info=True)
    result_summary = (
        f"{summary}（产物 {artifact.status}/充分度 {artifact.sufficiency}"
        + (f"，快照 {snapshot_id}" if snapshot_id else "")
        + (f"，硬校验 {len([i for i in issues if i.hard])} 项未过" if hard_failed else "")
        + "）"
    )
    return StepResult(status="completed", summary=result_summary)


def _write_text_artifact(deps: StepDeps, ctx: StepContext, name: str, content: str) -> Path:
    out_dir = deps.reports_dir / ctx.child_run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(content, encoding="utf-8")
    return path


def _extract_summary(report_md: str) -> str:
    """取「## 摘要」节正文；取不到则取首个非标题行。"""
    lines = report_md.splitlines()
    in_summary = False
    buf: list[str] = []
    for ln in lines:
        if ln.strip().startswith("## 摘要"):
            in_summary = True
            continue
        if in_summary and ln.strip().startswith("## "):
            break
        if in_summary and ln.strip():
            buf.append(ln.strip())
    if not buf:
        buf = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")][:2]
    return " ".join(buf)[:300]


def _publish_report(
    deps: StepDeps, ctx: StepContext, title: str, summary: str, artifact: Path, flags: list[str],
    *, kind: str = "research_report",
) -> None:
    deps.events.append(
        Event(
            run_id=ctx.session_run_id,
            type=REPORT_PUBLISHED,
            payload={
                "child_run_id": ctx.child_run_id,
                "kind": kind,
                "title": title,
                "summary": summary,
                "artifact_path": str(artifact),
                "artifact_ref": f"{ctx.child_run_id}/{artifact.name}",
                "quality_flags": flags,
                "command_id": ctx.command_id,
            },
        )
    )


# ---------------- S2 档案更新（thesis 修订） ----------------


def step_profile_update(deps: StepDeps, ctx: StepContext) -> StepResult:
    if ctx.should_cancel():
        return _cancelled(ctx)
    now = datetime.now(UTC)
    profile = deps.kb.view(ctx.entity_kind, ctx.ticker, now)
    if not profile:
        return StepResult(status="completed", summary="档案为空，跳过 thesis 修订")

    manifest = _open_child(deps, ctx, "profile_update")
    outcome: dict[str, str] = {}

    def query_kb(_args: dict[str, Any]) -> dict[str, Any]:
        view = deps.kb.view(ctx.entity_kind, ctx.ticker, datetime.now(UTC))
        return {
            "content": json.dumps(
                {
                    f: {
                        "value": r.value,
                        "evidence_ids": r.evidence_ids,
                        "knowledge_time": r.knowledge_time.isoformat(),
                    }
                    for f, r in view.items()
                },
                ensure_ascii=False,
                default=str,
            ),
            "provenance": [
                {"source_id": "kb", "available_at": r.knowledge_time.isoformat(), "pit_grade": "A"}
                for r in view.values()
            ],
        }

    def propose_thesis(args: dict[str, Any]) -> dict[str, Any]:
        try:
            fact_id = deps.writer.write_fact(
                Fact(
                    entity_kind=ctx.entity_kind,  # type: ignore[arg-type]
                    entity_id=ctx.ticker,
                    field="thesis",
                    value=args["thesis"],
                    knowledge_time=datetime.now(UTC),
                    evidence_ids=list(args["evidence_ids"]),
                    run_id=ctx.child_run_id,
                ),
                run=manifest,
            )
        except Exception as e:
            return {"content": f"rejected: {e}", "provenance": []}
        outcome["fact_id"] = fact_id
        return {"content": json.dumps({"fact_id": fact_id}), "provenance": []}

    deps.events.append(
        Event(
            run_id=ctx.child_run_id,
            type=CONTEXT_INJECT,
            payload={"role": "system", "content": _PROFILE_CONTRACT},
        )
    )
    kernel = AgentKernel(
        store=deps.events,
        llm=deps.llm_for("research"),
        manifest=manifest,
        tools={"query_kb": query_kb, "propose_thesis": propose_thesis},
        max_steps=6,
    )
    kernel.run_turn(
        f"请基于 {ctx.entity_kind}:{ctx.ticker} 的当前档案修订投资论点（thesis）。"
        "先 query_kb 查看全部事实，再 propose_thesis 提交（绑定支撑证据 id）。"
    )
    if "fact_id" in outcome:
        return StepResult(status="completed", summary=f"thesis 已修订（{outcome['fact_id']}）")
    return StepResult(status="completed", summary="模型未提交 thesis 修订（保持现状）")


# ---------------- S3 决策 ----------------


def step_decide(deps: StepDeps, ctx: StepContext) -> StepResult:
    manifest = _open_child(deps, ctx, "decide")
    rejection: str | None = None
    for attempt in range(1, deps.decide_max_attempts + 1):
        if ctx.should_cancel():
            return _cancelled(ctx)
        if rejection:  # Q7：打回原因进上下文，有界重试
            deps.events.append(
                Event(
                    run_id=ctx.child_run_id,
                    type=CONTEXT_INJECT,
                    payload={
                        "role": "user",
                        "content": (
                            f"上次出卡被 risk-review 打回：{rejection}。请修正后重试（第 {attempt} 次）。"
                        ),
                    },
                )
            )
        loop = DecisionLoop(
            kb=deps.kb,
            events=deps.events,
            decision_service=deps.decisions,
            llm=deps.llm_for("research"),
            manifest=manifest,
        )
        # P4：投资委员会记录（任一 /industry 漏斗产出）注入决策上下文——取最新一份
        committee_note: str | None = None
        committee_files = sorted(deps.reports_dir.glob(f"*/committee_{ctx.ticker}.md"))
        if committee_files:
            try:
                committee_note = committee_files[-1].read_text(encoding="utf-8")[:4000]
            except OSError:
                committee_note = None
        card_id = loop.run(ctx.entity_kind, ctx.ticker, context_note=committee_note)
        if loop.last_outcome == "issued":
            return StepResult(status="completed", summary=f"决策卡已出具：{card_id}")
        if loop.last_outcome == "declined":
            return StepResult(
                status="completed",
                summary="模型评估后选择不出卡（证据不足/观望）——合法结论",
            )
        rejection = loop.last_rejection or "risk-review 拒绝"
    return StepResult(
        status="blocked",
        summary=f"决策卡被 risk-review 连续打回 {deps.decide_max_attempts} 次：{rejection}",
    )


# ---------------- 过程评估（软反馈聚合） ----------------


def step_process_eval(deps: StepDeps, ctx: StepContext) -> StepResult:
    _open_child(deps, ctx, "process_eval")
    gaps = GapAnalyzer(deps.kb).analyze(ctx.entity_kind, ctx.ticker, datetime.now(UTC))
    deps.events.append(
        Event(
            run_id=ctx.child_run_id,
            type="process_eval/report",
            payload={
                "entity": f"{ctx.entity_kind}:{ctx.ticker}",
                "completeness": gaps.completeness,
                "missing": list(gaps.missing),
                "stale": list(gaps.stale),
                "conflicts": list(gaps.conflicts),
            },
        )
    )
    return StepResult(
        status="completed",
        summary=(
            f"过程评估：完整度 {gaps.completeness:.0%}，"
            f"缺口 {len(gaps.missing)} 项，冲突 {len(gaps.conflicts)} 项"
        ),
    )


# ---------------- S4 独立效果评估 ----------------


def step_evaluate(deps: StepDeps, ctx: StepContext) -> StepResult:
    _open_child(deps, ctx, "evaluate")
    if deps.eval_runner is None:
        return StepResult(status="error", summary="评估执行器未装配（eval_runner 缺失）")
    if ctx.should_cancel():
        return _cancelled(ctx)
    result = deps.eval_runner(config_name=ctx.config, child_run_id=ctx.child_run_id)
    verdict = result.get("verdict", "?")
    return StepResult(
        status="completed",
        summary=f"评估完成：verdict={verdict}（配置 {ctx.config}）",
    )


# ---------------- /industry 行业调研漏斗（P3 §4.1：F1-F5） ----------------


def theme_slug(theme: str) -> str:
    """主题 → 行业实体 id（确定性 slug，中英混排保留中文）。"""
    import re

    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", theme.strip().lower()).strip("-")
    return s or "industry"


def _valid_pool(value: Any) -> list[dict]:
    """标的池读侧校验：必须是 list[dict 且带 ticker]（防写侧类型腐化流入 F3）。"""
    if not isinstance(value, list):
        return []
    return [c for c in value if isinstance(c, dict) and c.get("ticker")]


def _objective_markets(objective: str) -> set[str]:
    """主题中的市场范围词 → 市场码集合；无 → 空集（不过滤）。"""
    markets: set[str] = set()
    if "美股" in objective or "美国" in objective:
        markets.add("US")
    if "港股" in objective or "香港" in objective:
        markets.add("HK")
    if "A股" in objective or "沪深" in objective:
        markets.add("CN")
    return markets


def _industry_loop(
    deps: StepDeps, ctx: StepContext, playbook_name: str, *, max_rounds: int | None = None
) -> ResearchLoop:
    """F1/F2 共用：行业实体的 ResearchLoop（gap 驱动 + 证据纪律不变）。"""
    playbook, ver = load_playbook(playbook_name)
    deps.events.append(
        Event(run_id=ctx.child_run_id, type="research/playbook",
              payload={"name": playbook_name, "version": ver})
    )
    manifest = _open_child(deps, ctx, playbook_name)
    loop = ResearchLoop(
        store=deps.kb,
        events=deps.events,
        writer=deps.writer,
        gateway=deps.gateway,
        llm=deps.llm_for("research"),
        manifest=manifest,
        max_rounds=max_rounds if max_rounds is not None else deps.max_rounds,
        completeness_target=deps.completeness_target,
        gateway_sources=deps.gateway.source_ids(),
        judge_llm=deps.judge_llm,
        should_stop=ctx.should_cancel,
        fetch_document=deps.fetch_document,
        worker_llms=deps.worker_llm_for(4) if deps.worker_llm_for else None,
    )
    loop.run(
        "industry", ctx.ticker,
        f"调研主题：{ctx.objective}\n\n{playbook}",
    )
    return loop


def step_industry_map(deps: StepDeps, ctx: StepContext) -> StepResult:
    """F1 赛道地图：子赛道拆解 → industry 档案（sub_sectors + 五必填字段）。"""
    if ctx.should_cancel():
        return _cancelled(ctx)
    loop = _industry_loop(deps, ctx, "industry_map")
    if loop.stop_reason == "cancelled":
        return _cancelled(ctx)
    gaps = GapAnalyzer(deps.kb).analyze("industry", ctx.ticker, datetime.now(UTC))
    if loop.stop_reason == "stalled" and not gaps.completeness:
        diag = (loop.stall_diagnostic or {}).get("suggestions") or []
        return StepResult(status="blocked",
                          summary=f"赛道地图停滞：完整度 0%（{'；'.join(diag)}）")
    return StepResult(
        status="completed",
        summary=f"赛道地图完成（{loop.stop_reason}）：行业档案完整度 {gaps.completeness:.0%}",
    )


def step_candidate_pool(deps: StepDeps, ctx: StepContext) -> StepResult:
    """F2 标的池：三 worker 并行挖掘取并集（召回敏感，Q4 裁决），每票绑赛道归属证据。

    不用 ResearchLoop（gap 驱动在「行业必填字段已完成」时会误判收敛）——
    这是定向采集：并行 worker 各挖一遍 → 并集去重 → 单写者落 player_landscape。
    """
    if ctx.should_cancel():
        return _cancelled(ctx)
    manifest = _open_child(deps, ctx, "candidate_pool")
    view = deps.kb.view("industry", ctx.ticker, datetime.now(UTC))
    existing = view.get("player_landscape")
    if existing is not None and _valid_pool(existing.value):
        return StepResult(
            status="completed",
            summary=f"标的池已存在（{len(_valid_pool(existing.value))} 只），跳过重复挖掘",
        )

    playbook, ver = load_playbook("candidate_pool")
    deps.events.append(
        Event(run_id=ctx.child_run_id, type="research/playbook",
              payload={"name": "candidate_pool", "version": ver})
    )
    workers = deps.worker_llm_for(3) if deps.worker_llm_for else [deps.llm_for("research")]
    collected: list[dict] = []
    lock = threading.Lock()
    propose_stats = {"accepted": 0, "rejected": 0}  # 空收诊断素材
    chunk_store = ChunkStore()  # 共享台账（锁保护），跨 worker 证据统一可引

    def make_tools() -> dict[str, Any]:
        tools, _tracker = make_research_tools(
            store=deps.kb, writer=deps.writer, manifest=manifest,
            entity_kind="industry", entity_id=ctx.ticker,
            chunk_store=chunk_store, fetch_document=deps.fetch_document,
        )
        for source_id in deps.gateway.source_ids():
            tools[f"query_{source_id}"] = make_gateway_tool(deps.gateway, source_id, chunk_store)

        def propose_candidates(args: dict[str, Any]) -> dict[str, Any]:
            cands = args.get("candidates") or []
            if not isinstance(cands, list):
                return {"content": "rejected: candidates 必须是 list", "provenance": []}
            accepted = []
            for c in cands:
                if not isinstance(c, dict) or not c.get("ticker"):
                    continue
                ev_ids = [str(e) for e in c.get("evidence_ids") or []]
                if not ev_ids:
                    continue  # 每票必须绑归属证据（堵记忆列票）
                ok = True
                for e in ev_ids:
                    try:
                        deps.kb.get_evidence(e)
                    except KeyError:
                        ok = False
                if ok:
                    accepted.append({**c, "ticker": _normalize_pool_ticker(str(c["ticker"])),
                                     "evidence_ids": ev_ids})
            with lock:
                collected.extend(accepted)
                propose_stats["accepted"] += len(accepted)
                propose_stats["rejected"] += len(cands) - len(accepted)
            return {"content": json.dumps({"accepted": len(accepted),
                                           "rejected": len(cands) - len(accepted)},
                                          ensure_ascii=False), "provenance": []}

        tools["propose_candidates"] = propose_candidates
        return tools

    def dig(idx: int, llm: LLM) -> None:
        run_id = f"{ctx.child_run_id}--dig{idx}"
        deps.events.append(
            Event(run_id=run_id, type=RUN_CREATED,
                  payload={"parent_run_id": ctx.child_run_id, "kind": "pool_digger",
                           "worker": idx})
        )
        deps.events.append(
            Event(run_id=run_id, type=CONTEXT_INJECT,
                  payload={"role": "system", "content": GROUNDING_CONTRACT})
        )
        brief = (
            f"挖掘赛道「{ctx.objective}」的上市标的池（你是第 {idx + 1} 号挖掘 worker，"
            f"与其他 worker 并行、结果取并集）。\n\n{playbook}\n\n"
            "流程：query_web_search / query_web_search_tavily 多角度搜索（中英文）→ "
            "register_evidence 登记 → propose_candidates 提交（每票带 evidence_ids）。\n"
            "生产纪律（防囤证据空转）：找到一批候选就立即 propose_candidates 提交（每批 3-8 只），"
            "禁止攒到最后；无发布时间的网页证据仍可用（系统自动按 C 级处理，能登记）。"
        )
        try:
            AgentKernel(
                store=deps.events, llm=llm,
                manifest=RunManifest(run_id=run_id, mode=RunMode.LIVE),
                tools=make_tools(), max_steps=14,
            ).run_turn(brief)
        except Exception as e:  # worker 失败隔离：其余 worker 继续
            logger.warning("标的池挖掘 worker %d 失败：%s", idx, e)

    with ThreadPoolExecutor(max_workers=len(workers)) as pool_exec:
        list(pool_exec.map(lambda iw: dig(*iw), enumerate(workers)))

    # 并集去重（按 ticker；证据 id 合并）
    merged: dict[str, dict] = {}
    for c in collected:
        key = c["ticker"]
        if key in merged:
            merged[key]["evidence_ids"] = list(dict.fromkeys(
                [*merged[key]["evidence_ids"], *c["evidence_ids"]]
            ))
        else:
            merged[key] = dict(c)
    if not merged:
        return StepResult(
            status="blocked",
            summary=f"标的池为空：挖掘 worker 提交 {propose_stats['accepted']} 只被接受 /"
                    f" {propose_stats['rejected']} 只被拒（证据校验）。"
                    "若提交数为 0 = worker 囤证据未提交（纪律问题）；"
                    "被拒多 = 证据未先登记（流程问题）——均需重跑或换源",
        )
    all_ev = sorted({e for c in merged.values() for e in c["evidence_ids"]})
    writer_llm_manifest = manifest
    fact = Fact(
        entity_kind="industry", entity_id=ctx.ticker, field="player_landscape",
        value=list(merged.values()), event_time=None,
        knowledge_time=datetime.now(UTC),  # C 级为主的采集：可知时刻=检索时刻
        evidence_ids=all_ev, run_id=writer_llm_manifest.run_id,
    )
    deps.writer.write_fact(fact, run=writer_llm_manifest)
    return StepResult(status="completed",
                      summary=f"标的池完成：{len(merged)} 只候选（{len(workers)} 路并集，"
                              f"均绑归属证据）")


# ---------------- P4 投资判断层（§5.1：F1.5 thesis + 投资委员会） ----------------


def step_thesis(deps: StepDeps, ctx: StepContext) -> StepResult:
    """F1.5 产业判断备忘录（thesis-first 的锚，对齐范本：先产业判断后筛票）。

    强模型 + 定向研究预算（不是纯综合——范本级 thesis 需要产业级素材）。
    写行业档案 `thesis` 字段（版本化判断，绑证据；推翻必须留痕）。
    """
    if ctx.should_cancel():
        return _cancelled(ctx)
    manifest = _open_child(deps, ctx, "thesis")
    playbook, ver = load_playbook("thesis")
    deps.events.append(
        Event(run_id=ctx.child_run_id, type="research/playbook",
              payload={"name": "thesis", "version": ver})
    )
    view = deps.kb.view("industry", ctx.ticker, datetime.now(UTC))
    f1_digest = {f: str(r.value)[:400] for f, r in view.items()}

    chunk_store = ChunkStore()
    tools, tracker = make_research_tools(
        store=deps.kb, writer=deps.writer, manifest=manifest,
        entity_kind="industry", entity_id=ctx.ticker,
        chunk_store=chunk_store, fetch_document=deps.fetch_document,
    )
    for source_id in deps.gateway.source_ids():
        tools[f"query_{source_id}"] = make_gateway_tool(deps.gateway, source_id, chunk_store)
    tools["calc"] = calc_tool
    deps.events.append(
        Event(run_id=ctx.child_run_id, type=CONTEXT_INJECT,
              payload={"role": "system", "content": GROUNDING_CONTRACT})
    )
    brief = (
        f"为赛道「{ctx.objective}」产出产业判断备忘录（thesis）。\n\n{playbook}\n\n"
        f"F1 赛道地图现状（可核查的起点，不是论据上限）：\n"
        f"{json.dumps(f1_digest, ensure_ascii=False, default=str)[:4000]}\n\n"
        "先补定向研究（web 搜索产业分析/技术瓶颈证据），再把备忘录写入档案："
        "propose_fact(field='thesis', value=<备忘录全文 markdown>, evidence_ids=[...])。"
    )
    AgentKernel(
        store=deps.events, llm=deps.llm_for("research"), manifest=manifest,
        tools=tools, max_steps=16,
    ).run_turn(brief)

    view = deps.kb.view("industry", ctx.ticker, datetime.now(UTC))
    if "thesis" not in view:
        return StepResult(
            status="blocked",
            summary="thesis 未产出（备忘录是后续筛选与排序的锚，缺了不往下走）；"
                    f"被拒 {len(tracker.rejected)} 次",
        )
    return StepResult(status="completed", summary="产业判断备忘录已落档案（thesis 字段，绑证据）")


#: 委员会视角（对齐 ai-berkshire 四大师 + 空头 + CIO）
_COMMITTEE_ROLES: tuple[tuple[str, str], ...] = (
    ("business", "商业模式视角（段永平式：十年后这门生意还在吗）"),
    ("financial", "财务估值视角（巴菲特式：报表质量、现金流、现在价格在下注什么）"),
    ("industry", "行业竞争视角（芒格式：格局/对手/替代威胁，护城河比看起来深还是浅）"),
    ("risk_mgmt", "风险与管理层视角（李录式：治理、诚信、资本配置、什么让它归零）"),
    ("bear", "空头视角（最强反方论证 + 证伪条件；找不出强反方论证本身要写明）"),
)


def step_committee(deps: StepDeps, ctx: StepContext) -> StepResult:
    """投资委员会：每票四视角 + 空头 + CIO 双强综合（kimi-k3@max 与 GLM-5.3@max 分担）。

    产出是判断 → 不落 KB，落报告 artifact（F5 排序报告与 /decide 决策卡消费）。
    """
    if ctx.should_cancel():
        return _cancelled(ctx)
    _open_child(deps, ctx, "committee")
    approved = _latest_screen_result(deps, ctx)
    if not approved:
        return StepResult(status="blocked", summary="缺少深研名单（screen_result 事件缺失）")
    playbook, ver = load_playbook("committee")
    scoring, sver = load_playbook("scoring")
    deps.events.append(
        Event(run_id=ctx.child_run_id, type="research/playbook",
              payload={"name": "committee", "version": ver})
    )
    deps.events.append(
        Event(run_id=ctx.child_run_id, type="research/playbook",
              payload={"name": "scoring", "version": sver})
    )

    view = deps.kb.view("industry", ctx.ticker, datetime.now(UTC))
    thesis_text = str(view["thesis"].value) if "thesis" in view else "（无 thesis——本轮按默认权重与常识判断）"

    strong_a = deps.llm_for("research")       # kimi-k3@max
    strong_b = deps.llm_for("research-alt")   # GLM-5.3@max
    out_paths: list[str] = []
    for ticker in approved:
        if ctx.should_cancel():
            return _cancelled(ctx)
        profile = deps.kb.view("stock", ticker, datetime.now(UTC))
        profile_md = json.dumps(
            {f: {"value": r.value, "evidence_ids": r.evidence_ids} for f, r in profile.items()},
            ensure_ascii=False, default=str,
        )[:9000]

        def read_evidence(args: dict[str, Any]) -> dict[str, Any]:
            try:
                ev = deps.kb.get_evidence(str(args["evidence_id"]))
            except Exception as e:
                return {"content": f"error: {e}", "provenance": []}
            return {"content": json.dumps({"verbatim_quote": ev.verbatim_quote,
                                           "source_id": ev.source_id, "url": ev.url},
                                          ensure_ascii=False), "provenance": []}

        notes: dict[str, str] = {}

        def run_perspective(role_key: str, role_desc: str, llm: LLM,
                            ticker: str = ticker, profile_md: str = profile_md,
                            notes: dict = notes) -> None:
            run_id = f"{ctx.child_run_id}--{ticker}-{role_key}"
            deps.events.append(
                Event(run_id=run_id, type=RUN_CREATED,
                      payload={"parent_run_id": ctx.child_run_id, "kind": "committee",
                               "ticker": ticker, "role": role_key})
            )
            brief = (
                f"以「{role_desc}」分析 {ticker}（赛道：{ctx.objective}）。\n\n"
                f"{playbook}\n\n评分 rubric：\n{scoring}\n\n"
                f"赛道 thesis：\n{thesis_text[:2000]}\n\n"
                f"该票档案（逐字段含证据 id）：\n{profile_md}\n\n"
                "产出该视角的独立分析（800 字内）：明确倾向 + 四维打分（逐分绑证据锚点）"
                "+ 一个最强论据 + 一个最大疑问。可用 read_evidence 核对证据原文、calc 算估值。"
            )
            try:
                notes[role_key] = AgentKernel(
                    store=deps.events, llm=llm,
                    manifest=RunManifest(run_id=run_id, mode=RunMode.LIVE),
                    tools={"read_evidence": read_evidence, "calc": calc_tool},
                    max_steps=8,
                ).run_turn(brief)
                if not (notes[role_key] or "").strip():
                    # 空产出也是失败（步数耗尽/模型未产出）——失败可见性：
                    # 日志 + artifact 明示，不允许静默出空白章节（2026-09-02 实测）
                    logger.warning("委员会 %s 视角 %s 产出为空（步数耗尽或模型未产出）", ticker, role_key)
                    notes[role_key] = "（该视角产出为空：步数耗尽或模型未产出，CIO 综合时降权）"
            except Exception as e:  # 单视角失败隔离：其余视角继续，CIO 拿到残缺但有标注的输入
                logger.warning("委员会 %s 视角 %s 失败：%s", ticker, role_key, e)
                notes[role_key] = f"（该视角产出失败：{type(e).__name__}）"

        with ThreadPoolExecutor(max_workers=5) as pool:
            futs = []
            for i, (role_key, role_desc) in enumerate(_COMMITTEE_ROLES):
                llm = strong_a if i % 2 == 0 else strong_b  # 双强分担（交叉验证在 CIO 综合体现）
                futs.append(pool.submit(run_perspective, role_key, role_desc, llm))
            for f in futs:
                f.result()

        # CIO 综合（强模型 A）
        cio_run = f"{ctx.child_run_id}--{ticker}-cio"
        cio_brief = (
            f"你是 CIO。综合 {ticker} 的投资委员会各视角（赛道：{ctx.objective}）。\n\n"
            + "\n\n".join(f"### {k}\n{v}" for k, v in notes.items())
            + "\n\n产出 CIO 综合（500 字内）：视角间真实分歧 + 裁决 + 双方共认的硬事实 + "
              "明确倾向（偏多/偏空/五五开）+ 它凭什么赢过同赛道其他票。"
        )
        try:
            notes["cio"] = AgentKernel(
                store=deps.events, llm=deps.llm_for("research"),
                manifest=RunManifest(run_id=cio_run, mode=RunMode.LIVE),
                tools={}, max_steps=2,
            ).run_turn(cio_brief)
            if not (notes["cio"] or "").strip():
                logger.warning("委员会 %s CIO 综合产出为空", ticker)
                notes["cio"] = "（CIO 综合产出为空：模型未产出，请直接读各视角记录）"
        except Exception as e:
            notes["cio"] = f"（CIO 综合失败：{type(e).__name__}）"

        artifact_md = f"# {ticker} 投资委员会记录\n\n" + "\n\n".join(
            f"## {role}\n{notes[role]}" for role, _ in _COMMITTEE_ROLES
        ) + f"\n\n## CIO 综合\n{notes['cio']}"
        out_paths.append(
            str(_write_text_artifact(deps, ctx, f"committee_{ticker}.md", artifact_md))
        )
        deps.events.append(
            Event(run_id=ctx.session_run_id, type="industry/committee_note",
                  payload={"ticker": ticker, "cio": notes["cio"][:200]})
        )
    return StepResult(status="completed",
                      summary=f"投资委员会完成 {len(approved)} 票（四视角+空头+CIO 综合，记录落 artifacts）")


#: F3 闸口迭代上限（打回重呈次数）
_SCREEN_MAX_ITERATIONS = 3
#: 粗筛单批总时限（卡片并行；超时的票收「调研超时」卡，不堵批）
_SCREEN_BATCH_TIMEOUT_S = 720.0
#: F3 闸口等待超时（用户要读表，给足时间）
_SCREEN_GATE_TIMEOUT_S = 1800.0


def _screen_one_candidate(deps: StepDeps, ctx: StepContext, cand: dict, llm: LLM,
                          chunk_store: ChunkStore, feedback: str | None) -> dict:
    """F3a 单票粗调研卡：worker LLM 一轮轻量调研 + submit_card 结构化提交。"""
    ticker = str(cand.get("ticker") or "").upper()
    # 每票独立 child run（并行 kernel 的 context 隔离；chunk 台账共享）
    card_run_id = f"{ctx.child_run_id}--{ticker}"
    manifest = RunManifest(run_id=card_run_id, mode=RunMode.LIVE)
    deps.events.append(
        Event(run_id=card_run_id, type=RUN_CREATED,
              payload={"parent_run_id": ctx.child_run_id, "kind": "screen_card",
                       "ticker": ticker})
    )
    tools, _tracker = make_research_tools(
        store=deps.kb, writer=deps.writer,
        manifest=manifest,
        entity_kind="stock", entity_id=ticker,
        chunk_store=chunk_store,
        fetch_document=deps.fetch_document,
    )
    for source_id in deps.gateway.source_ids():
        tools[f"query_{source_id}"] = make_gateway_tool(deps.gateway, source_id, chunk_store)
    tools["calc"] = calc_tool
    card: dict[str, Any] = {}

    def submit_card(args: dict[str, Any]) -> dict[str, Any]:
        # 空卡拒收（2026-09-01 实测 flash 空参调用 submit_card({}) 占位）
        if not args.get("one_liner") or not args.get("reason"):
            return {"content": "rejected: one_liner 与 reason 必填且非空；"
                               "先完成调研再提交卡片", "provenance": []}
        ev_ids = [str(e) for e in args.get("evidence_ids") or []]
        if not ev_ids:
            return {"content": "rejected: 至少绑 1 条已登记证据", "provenance": []}
        missing_ev = []
        for e in ev_ids:
            try:
                deps.kb.get_evidence(e)
            except KeyError:
                missing_ev.append(e)
        if missing_ev:
            return {"content": f"rejected: 未登记的证据 id：{missing_ev}", "provenance": []}
        card.update({
            "ticker": ticker,
            "one_liner": str(args.get("one_liner") or ""),
            "key_metrics": args.get("key_metrics") or {},
            "highlights": args.get("highlights") or [],
            "risks": args.get("risks") or [],
            "richness": str(args.get("richness") or "C"),
            "recommend": bool(args.get("recommend")),
            "reason": str(args.get("reason") or ""),
            "evidence_ids": ev_ids,
        })
        return {"content": "ok", "provenance": []}

    tools["submit_card"] = submit_card
    deps.events.append(
        Event(run_id=card_run_id, type=CONTEXT_INJECT,
              payload={"role": "system", "content": GROUNDING_CONTRACT})
    )
    playbook, _ = load_playbook("screen")
    fb = f"\n\n上一轮打回反馈（必须回应）：{feedback}" if feedback else ""
    pool_ev = cand.get("evidence_ids") or []
    brief = (
        f"粗调研这只股票并提交调研卡（submit_card）。\n"
        f"候选：{json.dumps(cand, ensure_ascii=False)}\n"
        f"主题赛道：{ctx.objective}\n\n{playbook}{fb}\n\n"
        f"该候选的赛道归属证据已存在：{pool_ev}——可直接绑进卡片，新发现再登记新证据。\n"
        "固定流程（总步数有限，别超支）：\n"
        "① query_fundamentals / query_fundamentals_hk 拿市值与收入（1-2 次调用）\n"
        "② query_web_search 补亮点与风险（≤2 次）\n"
        "③ register_evidence 登记新证据（≤3 条；引用已有证据不扣）\n"
        "④ submit_card 提交。\n"
        "调研中确认的硬事实用 propose_fact 落档案，字段名用 schema 名（valuation/"
        "revenue_fy/net_income_fy/cash_flow/business_model/moat/risks），"
        "不要自造中文字段名（后续深研按 schema 缺口驱动，自造字段等于白写）。"
    )
    kernel = AgentKernel(
        store=deps.events,
        llm=llm,
        manifest=manifest,
        tools=tools,
        max_steps=14,  # 8 步实测不够（要检索+登记+写事实+提交卡）
    )
    try:
        kernel.run_turn(brief)
    except Exception as e:
        # 卡片级失败隔离（失败可见性：单票挂了不拖死整批，原因进卡片）
        logger.warning("粗调研卡 %s 失败：%s", ticker, e)
        return {"ticker": ticker, "recommend": False,
                "reason": f"调研中断：{type(e).__name__}: {str(e)[:80]}",
                "richness": "C", "one_liner": "", "key_metrics": {}, "highlights": [],
                "risks": [], "evidence_ids": [], "failed": True}
    if not card:
        return {"ticker": ticker, "recommend": False, "reason": "调研未产出卡片（模型未提交）",
                "richness": "C", "one_liner": "", "key_metrics": {}, "highlights": [],
                "risks": [], "evidence_ids": [], "failed": True}
    return card


def step_screen(deps: StepDeps, ctx: StepContext) -> StepResult:
    """F3 粗调研 + 人工闸口回环：调研卡 → 筛分表 → 闸口（打回带反馈迭代重呈）。"""
    if ctx.should_cancel():
        return _cancelled(ctx)
    _open_child(deps, ctx, "screen")
    view = deps.kb.view("industry", ctx.ticker, datetime.now(UTC))
    pool_rec = view.get("player_landscape")
    pool = _valid_pool(pool_rec.value if pool_rec else None)
    listed = [c for c in pool if c.get("listed", True)]
    # 市场范围过滤：主题里明示市场（美股/港股/A股）时，不在范围的候选不进粗筛
    # （省卡位，避免对范围外标的浪费调研）
    scope_markets = _objective_markets(ctx.objective)
    if scope_markets:
        listed = [c for c in listed if str(c.get("market", "")).upper() in scope_markets]
    if not listed:
        return StepResult(status="blocked",
                          summary="标的池无符合市场范围的上市候选，无法粗筛")

    n_workers = min(len(listed), 6)
    feedback: str | None = None
    for iteration in range(1, _SCREEN_MAX_ITERATIONS + 1):
        if ctx.should_cancel():
            return _cancelled(ctx)
        # worker 每轮重建（迭代重呈需要新鲜实例；生产侧 router.get 本来就每次新建）
        if deps.worker_llm_for:
            workers = deps.worker_llm_for(n_workers)
        else:
            # 无 worker 池时每票独立实例（MockLLM 等脚本化 LLM 一次性消耗，不可共享）
            workers = [deps.llm_for("research") for _ in range(n_workers)]
        chunk_store = ChunkStore()
        cards: list[dict] = []
        # 总时限 + 收集（2026-09-01 实测：某 worker 的 LLM 流式连接半开悬挂，
        # f.result() 无限等待拖死整批）——超时票收成「调研超时」卡（可见），
        # 悬挂线程泄漏但步不堵（cancel_futures 取消未启动的）。
        pool_exec = ThreadPoolExecutor(max_workers=min(len(listed), 6))
        futs = {
            pool_exec.submit(
                _screen_one_candidate, deps, ctx, cand,
                workers[i % len(workers)], chunk_store, feedback,
            ): str(cand.get("ticker") or "?")
            for i, cand in enumerate(listed)
        }
        done_futs, not_done = wait_futs(futs, timeout=_SCREEN_BATCH_TIMEOUT_S)
        for f in done_futs:
            cards.append(f.result())
        for f in not_done:
            cards.append({"ticker": futs[f], "recommend": False,
                          "reason": f"调研超时（{_SCREEN_BATCH_TIMEOUT_S:.0f}s 未返回）",
                          "richness": "C", "one_liner": "", "key_metrics": {},
                          "highlights": [], "risks": [], "evidence_ids": [], "failed": True})
        pool_exec.shutdown(wait=False, cancel_futures=True)
        # 按标的池顺序呈现（闸口阅读体验稳定），不按完成乱序
        order = {str(c.get("ticker")): i for i, c in enumerate(listed)}
        cards.sort(key=lambda c: order.get(c["ticker"], 999))
        for c in cards:
            deps.events.append(
                Event(run_id=ctx.session_run_id, type="industry/card",
                      payload={"ticker": c["ticker"],
                               "recommend": c.get("recommend"),
                               "reason": str(c.get("reason", ""))[:120]})
            )

        recommended = [c for c in cards if c.get("recommend")][:6]
        table = _render_screen_table(cards, recommended)
        approval_id = deps.approvals.request(
            ctx.session_run_id,
            {
                "op": "industry_screen",
                "theme": ctx.objective,
                "round": iteration,
                "table": table,
                "recommended": [c["ticker"] for c in recommended],
                "feedback_addressed": feedback,
                "prompt": "确认深研名单（批准=进入深研；拒绝可带打回理由，将迭代重呈）",
            },
        )
        deps.approvals.wait(approval_id, timeout=_SCREEN_GATE_TIMEOUT_S)
        approved, comment = deps.approvals.outcome(approval_id)
        if approved:
            deps.events.append(
                Event(run_id=ctx.session_run_id, type="industry/screen_result",
                      payload={"command_id": ctx.command_id,
                               "approved_tickers": [c["ticker"] for c in recommended],
                               "round": iteration, "table": table})
            )
            return StepResult(
                status="completed",
                summary=f"粗筛闸口通过（第 {iteration} 轮呈交）：深研 {len(recommended)} 只"
                        f"（{'、'.join(c['ticker'] for c in recommended)}）",
            )
        if comment:
            feedback = comment  # 打回反馈 → 下一轮迭代重呈
            continue
        return StepResult(status="blocked",
                          summary="粗筛被打回（未附反馈）。请在审批卡填写打回理由，或终止本 command")
    return StepResult(status="blocked",
                      summary=f"粗筛闸口迭代 {_SCREEN_MAX_ITERATIONS} 轮仍未通过；请直接说明筛选标准")


def _render_screen_table(cards: list[dict], recommended: list[dict]) -> str:
    """筛分表（闸口阅读用）：推荐/淘汰各带理由，证据可回溯。"""
    rec_ids = {c["ticker"] for c in recommended}
    lines = [
        "| 标的 | 业务一句话 | 关键指标 | 亮点 | 风险 | 丰富度 | 结论 |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in cards:
        km = c.get("key_metrics") or {}
        km_s = "；".join(f"{k}={v}" for k, v in list(km.items())[:3]) or "未知"
        verdict = "✅ 深研" if c["ticker"] in rec_ids else "✕ 淘汰"
        reason = c.get("reason", "")
        lines.append(
            f"| {c['ticker']} | {c.get('one_liner', '')[:40]} | {km_s[:40]} | "
            f"{'；'.join(c.get('highlights') or [])[:40]} | {'；'.join(c.get('risks') or [])[:40]} | "
            f"{c.get('richness', '?')} | {verdict}：{reason[:40]} |"
        )
    return "\n".join(lines)


def step_deep_dive(deps: StepDeps, ctx: StepContext) -> StepResult:
    """F4 深研 fan-out：入选票各自独立 ResearchLoop（并行 + 失败隔离）。"""
    if ctx.should_cancel():
        return _cancelled(ctx)
    _open_child(deps, ctx, "deep_dive")
    approved = _latest_screen_result(deps, ctx)
    if not approved:
        return StepResult(status="blocked", summary="缺少闸口通过的深研名单（screen_result 事件缺失）")

    results: dict[str, str] = {}

    def dive(ticker: str) -> None:
        manifest = RunManifest(
            run_id=f"{ctx.child_run_id}--{ticker}", mode=RunMode.LIVE,
        )
        deps.events.append(
            Event(run_id=manifest.run_id, type=RUN_CREATED,
                  payload={"parent_run_id": ctx.child_run_id, "kind": "deep_dive",
                           "ticker": ticker})
        )
        try:
            loop = ResearchLoop(
                store=deps.kb, events=deps.events, writer=deps.writer, gateway=deps.gateway,
                llm=deps.llm_for("research"), manifest=manifest,
                max_rounds=deps.max_rounds,
                completeness_target=deps.completeness_target,
                gateway_sources=deps.gateway.source_ids(),
                judge_llm=deps.judge_llm,
                should_stop=ctx.should_cancel,
                fetch_document=deps.fetch_document,
                worker_llms=deps.worker_llm_for(4) if deps.worker_llm_for else None,
            )
            loop.run("stock", ticker, f"深度研究 {ticker}（赛道：{ctx.objective}）")
            if loop.stop_reason == "stalled":
                sugg = ((loop.stall_diagnostic or {}).get("suggestions") or [""])[0]
                results[ticker] = f"stalled（{sugg[:60]}）"
            else:
                results[ticker] = loop.stop_reason or "done"
        except Exception as e:  # 失败隔离：一票挂了不阻塞其余
            logger.exception("deep dive failed for %s", ticker)
            results[ticker] = f"error: {type(e).__name__}"

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(dive, approved))

    gaps_line = []
    for t in approved:
        g = GapAnalyzer(deps.kb).analyze("stock", t, datetime.now(UTC))
        gaps_line.append(f"{t} {g.completeness:.0%}（{results[t]}）")
    return StepResult(status="completed",
                      summary=f"深研完成 {len(approved)} 只：" + "；".join(gaps_line))


def _normalize_pool_ticker(raw: str) -> str:
    """标的池代码归一化（P4 验收残留修复）：港股数字码统一 5 位零填充
    （2228.HK ≡ 02228.HK）——双胞胎代码会把同一公司拆成两条档案、两张调研卡。

    口径依据：HKEX 官方 5 位制；hkexnews/fundamentals adapter 内部同口径（zfill(5)）。
    """
    t = raw.strip().upper()
    if t.endswith(".HK"):
        code = t[:-3]
        if code.isdigit():
            return f"{code.zfill(5)}.HK"
    return t


#: F5 注入的逐票预算（字符）。截断必须按票隔离——全局截断会把名单尾部整票静默丢掉
#: （2026-09-02 P4 验收实测：profiles[:8000] 使 RLAY/ABCL 缺席报告，
#: 模型据残缺输入编造「漏斗 8→4」叙事；committee_notes[:6000] 只剩第一票）。
_RANK_REPORT_PER_TICKER_PROFILE_CHARS = 12000
_RANK_REPORT_PER_TICKER_NOTES_CHARS = 12000


def _read_committee_notes(deps: StepDeps, ctx: StepContext) -> str:
    """读本 command 投资委员会产出的各票记录（reports/<committee run>/committee_*.md）。

    逐票截断（不全局截断）：每票都有自己的预算，名单尾部不会被静默丢弃。
    """
    import glob as _glob

    pattern = str(deps.reports_dir / f"{ctx.session_run_id}--{ctx.command_id}-*-committee")
    dirs = sorted(_glob.glob(pattern))
    if not dirs:
        return "（本轮无投资委员会记录）"
    parts: list[str] = []
    for f in sorted(Path(dirs[-1]).glob("committee_*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if len(text) > _RANK_REPORT_PER_TICKER_NOTES_CHARS:
            text = text[:_RANK_REPORT_PER_TICKER_NOTES_CHARS] + "\n…（本票委员会记录超长截断）"
        parts.append(text)
    return "\n\n".join(parts) or "（投资委员会记录为空）"


def _latest_screen_table(deps: StepDeps, ctx: StepContext) -> str:
    """本 command 最近一次闸口的筛分表全文（含淘汰理由）——F5 漏斗叙事的事实源。"""
    for e in reversed(deps.events.read(ctx.session_run_id, types={"industry/screen_result"})):
        if e.payload.get("command_id") == ctx.command_id:
            return str(e.payload.get("table") or "")
    return ""


def _latest_screen_result(deps: StepDeps, ctx: StepContext) -> list[str]:
    """从会话流读本 command 最近一次闸口通过名单。"""
    for e in reversed(deps.events.read(ctx.session_run_id, types={"industry/screen_result"})):
        if e.payload.get("command_id") == ctx.command_id:
            return [str(t).upper() for t in e.payload.get("approved_tickers") or []]
    return []


def step_rank_report(deps: StepDeps, ctx: StepContext) -> StepResult:
    """F5 排序对比报告：多维度矩阵（逐格证据锚点）+ 候选排序 + 潜力评级（判断只在报告）。"""
    if ctx.should_cancel():
        return _cancelled(ctx)
    manifest = _open_child(deps, ctx, "rank_report")
    approved = _latest_screen_result(deps, ctx)
    if not approved:
        return StepResult(status="blocked", summary="缺少深研名单（screen_result 事件缺失）")

    now = datetime.now(UTC)
    profiles: dict[str, Any] = {}
    for t in approved:
        view = deps.kb.view("stock", t, now)
        profiles[t] = {
            f: {"value": r.value, "evidence_ids": r.evidence_ids} for f, r in view.items()
        }
    industry_view = deps.kb.view("industry", ctx.ticker, now)
    playbook, ver = load_playbook("rank_report")
    deps.events.append(
        Event(run_id=ctx.child_run_id, type="research/playbook",
              payload={"name": "rank_report", "version": ver})
    )

    def read_evidence(args: dict[str, Any]) -> dict[str, Any]:
        try:
            ev = deps.kb.get_evidence(str(args["evidence_id"]))
        except Exception as e:
            return {"content": f"error: {e}", "provenance": []}
        return {"content": json.dumps({"evidence_id": ev.evidence_id, "source_id": ev.source_id,
                                       "url": ev.url, "verbatim_quote": ev.verbatim_quote},
                                      ensure_ascii=False), "provenance": []}

    # P4：thesis + 评分 rubric + 投资委员会记录 一并注入（判断层的全部上游产物）
    thesis_text = str(industry_view["thesis"].value) if "thesis" in industry_view else "（无）"
    scoring, sver = load_playbook("scoring")
    deps.events.append(
        Event(run_id=ctx.child_run_id, type="research/playbook",
              payload={"name": "scoring", "version": sver})
    )
    committee_notes = _read_committee_notes(deps, ctx)

    kernel = AgentKernel(
        store=deps.events,
        llm=deps.llm_for("research"),
        manifest=manifest,
        tools={"read_evidence": read_evidence, "calc": calc_tool},
        max_steps=20,  # 实测 8 步不够：核对证据会吃步数，必须留足写报告的余量
    )
    # 漏斗事实（§3 漏斗叙事的事实源——没有这些数字，模型会凭记忆编造池子规模与淘汰理由）
    pool = _valid_pool(industry_view["player_landscape"].value) \
        if "player_landscape" in industry_view else []
    screen_table = _latest_screen_table(deps, ctx)
    funnel_facts = (
        f"- 标的池（F2 双通道挖掘，均绑归属证据）：{len(pool)} 只\n"
        f"- 粗筛（F3 调研卡 + 人工闸口）：入选 {len(approved)} 只 = {'、'.join(approved)}\n"
        f"- 闸口筛分表（含每票淘汰理由）：\n{screen_table[:8000] or '（无记录）'}"
    )
    # 逐票序列化 + 逐票预算：每票档案独立成块，截断按票隔离（不全局截断丢票）
    profile_blocks: list[str] = []
    for t in approved:
        body = json.dumps(profiles.get(t) or {}, ensure_ascii=False, default=str)
        if len(body) > _RANK_REPORT_PER_TICKER_PROFILE_CHARS:
            body = body[:_RANK_REPORT_PER_TICKER_PROFILE_CHARS] + "…（本票档案超长截断）"
        profile_blocks.append(f"### {t}\n{body}")
    brief = (
        f"为赛道「{ctx.objective}」产出排序对比报告。\n\n{playbook}\n\n"
        f"## 评分 rubric\n{scoring}\n\n"
        f"## 产业判断备忘录（thesis）\n{thesis_text[:3000]}\n\n"
        "## 行业档案："
        + json.dumps(
            {f: r.value for f, r in industry_view.items() if f != "thesis"},
            ensure_ascii=False, default=str,
        )[:3000]
        + f"\n\n## 漏斗事实（以此为准，禁止凭记忆补写）：\n{funnel_facts}"
        + "\n\n## 入选标的档案（逐字段含证据 id）：\n"
        + "\n\n".join(profile_blocks)
        + f"\n\n## 投资委员会记录（四视角+空头+CIO 综合）：\n{committee_notes}"
        + f"\n\n入选名单完整性纪律：本轮入选 = {'、'.join(approved)}（共 {len(approved)} 只）。"
          "对比矩阵、估值快照、候选排序、委员会摘要必须逐票覆盖全部入选票；"
          "任何一票缺席 = 报告作废。漏斗叙事只能引用「漏斗事实」，禁止凭记忆补写。"
        + "\n\n节奏纪律：read_evidence 抽查点到为止（只核存疑条目，不逐条读）；"
          "**报告正文必须在最后一次模型调用中以完整 markdown 输出**——步数耗尽前必须落笔，"
          "宁可少核对两条证据。"
    )
    report_md = kernel.run_turn(brief)
    title = f"{ctx.objective} 赛道调研排序报告"
    if not report_md.strip():
        # 空报告 ≠ 完成（失败可见性：模型步数耗尽未落笔，明确 blocked 而非静默出空壳）
        return StepResult(status="blocked",
                          summary="排序报告为空：模型步数耗尽未产出正文（卡在证据核对阶段）——重试或调大步数预算")
    artifact = _write_text_artifact(deps, ctx, "industry_report.md", report_md)
    summary = _extract_summary(report_md) or f"排序报告已生成（{len(approved)} 只入选）"
    _publish_report(deps, ctx, title, summary, artifact, [f"candidates:{len(approved)}"],
                    kind="industry_report")
    return StepResult(status="completed", summary=summary)


STEPS: dict[str, Callable[[StepDeps, StepContext], StepResult]] = {
    "research": step_research,
    "profile_update": step_profile_update,
    "synthesize": step_synthesize,
    "decide": step_decide,
    "process_eval": step_process_eval,
    "evaluate": step_evaluate,
    "industry_map": step_industry_map,
    "thesis": step_thesis,
    "candidate_pool": step_candidate_pool,
    "screen": step_screen,
    "deep_dive": step_deep_dive,
    "committee": step_committee,
    "rank_report": step_rank_report,
}

STEP_TITLES: dict[str, str] = {
    "research": "S1 研究",
    "profile_update": "S2 档案更新",
    "synthesize": "报告合成",
    "decide": "S3 决策",
    "process_eval": "过程评估",
    "evaluate": "S4 效果评估",
    "industry_map": "F1 赛道地图",
    "thesis": "F1.5 产业判断备忘录",
    "candidate_pool": "F2 标的池",
    "screen": "F3 粗调研·人工闸口",
    "deep_dive": "F4 深研",
    "committee": "F4.5 投资委员会",
    "rank_report": "F5 排序报告",
}

#: step agent 能力清单（GET /api/capabilities 的数据源；新增能力 = 在这里登记）
STEP_MANIFEST: dict[str, dict[str, Any]] = {
    "research": {
        "title": "S1 研究（轮次制：gap 分析 → 采集 → 证据验证 → 落库）",
        "model_role": "research",
        "tools": ["query_kb", "read_edgar_filing", "register_evidence", "propose_fact"],
        "plugins": ["gap 分析（完整度+新鲜度驱动）", "rubric 评审（judge）", "停滞/预算收敛"],
        "hooks": ["evidence-binding（服务端子串校验）", "numeric-guard", "ProfileWriter 单写者"],
        "budget": {"max_rounds": "动态（0%→5/>50%→3/仅刷新→1）", "max_steps_per_group": 12},
    },
    "industry_map": {
        "title": "F1 赛道地图（子赛道拆解 → industry 档案）",
        "model_role": "research",
        "tools": ["query_* 数据源", "register_evidence", "propose_fact", "calc"],
        "plugins": ["playbook: industry_map", "gap 分析", "维度并行"],
        "hooks": ["evidence-binding", "ProfileWriter 单写者"],
    },
    "candidate_pool": {
        "title": "F2 标的池（三 worker 并集挖掘，每票绑归属证据）",
        "model_role": "research-worker ×3",
        "tools": ["query_web_search", "query_web_search_tavily", "register_evidence", "propose_candidates"],
        "plugins": ["playbook: candidate_pool", "并集去重", "worker 失败隔离"],
        "hooks": ["evidence-binding（无证据候选拒收）"],
    },
    "screen": {
        "title": "F3 粗调研 + 人工闸口（调研卡 → 筛分表 → 打回迭代）",
        "model_role": "research-worker（每票并行）",
        "tools": ["query_fundamentals*", "query_web_search*", "calc", "submit_card"],
        "plugins": ["playbook: screen", "信息丰富度评级", "闸口回环（≤3 轮）"],
        "hooks": ["证据 id 存在性校验"],
    },
    "deep_dive": {
        "title": "F4 深研 fan-out（入选票并行 ResearchLoop）",
        "model_role": "research + research-worker ×4/票",
        "tools": ["全套研究工具"],
        "plugins": ["失败隔离", "stalled 诊断卡"],
        "hooks": ["evidence-binding", "ProfileWriter 单写者"],
    },
    "rank_report": {
        "title": "F5 排序对比报告（矩阵逐格证据锚点；判断只在报告）",
        "model_role": "research（强模型；双强交叉在 P4）",
        "tools": ["read_evidence", "calc"],
        "plugins": ["playbook: rank_report"],
        "hooks": [],
    },
    "profile_update": {
        "title": "S2 档案更新（thesis 修订）",
        "model_role": "research",
        "tools": ["query_kb", "propose_thesis"],
        "plugins": ["thesis 版本化"],
        "hooks": ["evidence-binding", "ProfileWriter 单写者"],
        "budget": {"max_steps": 6},
    },
    "decide": {
        "title": "S3 决策（DecisionCard）",
        "model_role": "research",
        "tools": ["query_kb", "propose_decision"],
        "plugins": ["打回有界重试 ×2"],
        "hooks": ["risk-review（失效条件/仓位上限/证据链）", "kb_snapshot 绑定校验"],
        "budget": {"max_steps": 8, "max_attempts": 2},
    },
    "synthesize": {
        "title": "报告合成（CIO 综合 → 结构化研报）",
        "model_role": "research",
        "tools": ["query_kb", "read_evidence"],
        "plugins": ["结构化研报模板", "证据锚点内联"],
        "hooks": ["数字与档案值一致（契约级）"],
        "budget": {"max_steps": 8},
    },
    "process_eval": {
        "title": "过程评估（软反馈聚合）",
        "model_role": None,
        "tools": ["gap 分析器（确定性）"],
        "plugins": [],
        "hooks": ["coverage-check"],
        "budget": {},
    },
    "evaluate": {
        "title": "S4 效果评估（walk-forward 回放 + 基线对照）",
        "model_role": "research(+fast 基线)",
        "tools": ["ReplayEngine", "PriceBook", "LeakageAuditHook"],
        "plugins": ["LLM-only 基线", "deflated Sharpe", "holdout 预算"],
        "hooks": ["时间锁网关", "穿越审计", "审批闸（默认强制）"],
        "budget": {"holdout_budget": 10},
    },
}
