"""Step agents：command pipeline 的执行单元（各带 kernel loop + tools + hooks）。

纪律（redesign §3.3）：
- 每个 step 跑在独立 child run（run/created 带 parent_run_id），上下文互相隔离；
- step 间数据接力经 KB / DecisionStore / 结构化摘要，不经共享对话；
- 失败三通道：子流 error 事件 + 父流 step_agent/end(status=error) + 日志（runner 负责）。
"""

from __future__ import annotations

import contextlib
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
from ..research.context_tools import make_context_tools
from ..research.evidence_desk import ChunkStore
from ..research.loop import ResearchLoop
from ..research.playbooks import load_playbook
from ..research.prompts import GROUNDING_CONTRACT
from ..research.tools import make_research_tools

logger = logging.getLogger("finance_agent.steps")

# S2 档案更新的契约（tools-plugins 方案 §9.1：从「重写 thesis」扩展为整合与更新）
_PROFILE_CONTRACT = """\
你是档案整合员（不只是 thesis 重写员）。基于冻结基线与本轮研究产出修订该标的的投资论点。
工作流：
1. get_research_context 读取上下文：问题结论、typed 观测/论断/计算、字段投影与开放冲突；
2. list_conflicts 检查开放冲突；有则先 adjudicate_conflict 裁决（必须给 rationale；
   不得留着冲突值写无条件结论）；
3. read_evidence 核对支撑论点的关键证据原文；
4. propose_thesis 提交：论点与已裁决的观测/论断一致，绑定支撑证据 id；
   证据不足或未裁决的点在 limitations 里明确写出。
纪律：论点只能建立在档案事实/typed 数据之上；validated 仅表示引用校验过，
内容是否支持结论由你对照原文把关。
"""

PROFILE_TOOL_SCHEMAS: dict[str, dict] = {
    "query_kb": {
        "name": "query_kb",
        "description": "查询当前标的档案投影（含每条事实的证据 id）",
        "parameters": {"type": "object", "properties": {}},
    },
    "propose_thesis": {
        "name": "propose_thesis",
        "description": (
            "提交修订后的投资论点（必须绑定支撑证据 id）。服务端同时保存为："
            "旧 thesis Fact（兼容投影）+ 带证据与前提的分析论断 Claim（新读侧入口）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "thesis": {"type": "string", "description": "修订后的投资论点全文"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "limitations": {"type": "array", "items": {"type": "string"},
                                "description": "证据不足/未裁决/待验证的限制点"},
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
    #: 分页抓取（Document Read v2）：f(url) -> gateway.fetch.FetchedDocument；
    #: 缺省时研究工具退回旧纯文本抓取（兼容 eval 回放与旧装配）
    fetch_document_paged: Callable[[str], Any] | None = None
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
    #: 单条检索记录正文上限（audit §3.3 按需 evidence bundle）；超出部分凭
    #: read_chunk(chunk_id) 取回——工具响应不再无条件灌满上下文
    max_record_chars: int | None = 6000


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
        fetch_document_paged=deps.fetch_document_paged,
        worker_llms=deps.worker_llm_for(4) if deps.worker_llm_for else None,
        plan_id=plan_id,
        metrics=deps.metrics,
        metric_writer=deps.metric_writer,
        calculations=deps.calculations,
        max_record_chars=deps.max_record_chars,
    )
    reports = loop.run(
        ctx.entity_kind, ctx.ticker, ctx.objective or f"深度研究 {ctx.ticker}"
    )
    if loop.stop_reason == "cancelled":
        # 取消也要保留已验证成果（audit §3.9）：部分成果冻结为 partial 产物 + 发快照
        _publish_partial_artifact(deps, ctx, loop, datetime.now(UTC))
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
        # typed 产出同样是有效进展（§7.7）：观测/论断/问题推进过 → 不拦停管道，
        # 否则「有价值分析但没写旧字段」会被误判为一无所获
        total_typed = sum(
            len(r.observations_written) + len(r.claims_written) + len(r.questions_advanced)
            for r in reports
        )
        # 粒度区分：整轮零产出（本轮研究一无所获）→ blocked 拦停管道，
        # 不再让后续 step 对空档案空烧 token；已有进展后的末轮停滞 = 自然收敛，
        # 研究产出有效，管道继续（摘要留痕停滞原因）。
        if total_written == 0 and total_typed == 0:
            summary = (
                f"研究停滞（stalled）：{len(reports)} 轮后完整度 "
                f"{last.completeness_before:.0%} → {last.completeness_after:.0%}，"
                f"缺口 {len(missing)} 字段"
                f"（{'、'.join(missing[:5])}{'…' if len(missing) > 5 else ''}）"
                + (f"；建议：{sugg}" if sugg else "")
            )
            _publish_partial_artifact(deps, ctx, loop, datetime.now(UTC))
            return StepResult(status="blocked", summary=summary)
        summary = (
            f"研究 {len(reports)} 轮（stalled，累计写入 {total_written} 字段"
            f" + {total_typed} 项 typed 产出后停滞）："
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
    # 预算/轮数终止：已验证成果走部分发布（audit §3.3/§3.9）——可读地交付 partial
    if loop.stop_reason == "budget":
        _publish_partial_artifact(deps, ctx, loop, datetime.now(UTC))
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
    # 预算与「执行结束 ≠ 成果可用」（audit §2/§3.3）：投入可见、停止原因可归因
    summary += _budget_summary(loop)
    return StepResult(status="completed", summary=summary)


def _publish_partial_artifact(
    deps: StepDeps, ctx: StepContext, loop: ResearchLoop, now: datetime
) -> str | None:
    """终止路径的部分成果发布（audit §3.9）：取消/超时/停滞不等最终合成。

    确定性构造（不调 LLM）：已回答/争议/不可得的问题逐题成段（结论 + 引用），
    未完成的题目用 gap_notice 显式标出；产物 status=draft、sufficiency 取评估结果，
    并发布档案快照（页面可读到部分成果）。返回 artifact_id。
    """
    if deps.metrics is None:
        return None
    checkpoint = getattr(loop, "partial_checkpoint", None)
    answered = (checkpoint or {}).get("answered") or []
    claims = deps.metrics.claims_as_of(
        ctx.entity_kind, ctx.ticker, now, statuses=("draft", "validated")
    )
    observations = deps.metrics.observations_as_of(ctx.entity_kind, ctx.ticker, now)
    if not (answered or claims or observations or (checkpoint or {}).get("facts")):
        return None  # 一无所获：不发空产物（不拿 draft 冒充成果）
    from ..research.artifacts import (
        ArtifactValidator,
        GapNoticeBlock,
        HeadingBlock,
        ParagraphBlock,
        ReportDocument,
        ResearchArtifact,
        SourceRefBlock,
        render_markdown,
    )

    blocks: list[Any] = [HeadingBlock(level=1, text="部分成果（研究未结束）")]
    stop = loop.stop_reason or "unknown"
    exhausted = getattr(loop, "budget_exhausted", []) or []
    blocks.append(ParagraphBlock(
        text=(
            f"本产物在研究终止时冻结（停止原因：{stop}"
            + (f"，耗尽维度：{'、'.join(exhausted)}" if exhausted else "")
            + "）。以下内容只包含已通过服务端校验的成果；未完成题目在下方缺口区显式列出。"
        )
    ))
    refs: list[str] = []
    for item in answered:
        qid = str(item.get("question_id") or "")
        status = str(item.get("status") or "")
        conclusion = str(item.get("conclusion") or "")
        blocks.append(HeadingBlock(level=2, text=f"[{qid}]（{status}）"))
        blocks.append(ParagraphBlock(text=conclusion or "（无结论正文）"))
        for ref in [*(item.get("support_refs") or []), *(item.get("counter_refs") or [])]:
            if str(ref) not in refs:
                refs.append(str(ref))
        unresolved = item.get("unresolved") or []
        if unresolved:
            blocks.append(ParagraphBlock(text="未解决：" + "；".join(str(u) for u in unresolved)))
    plan = (loop.plan_payload or {})
    open_questions = [
        q for q in plan.get("questions", []) or []
        if q.get("status") not in ("answered", "not_applicable", "disputed", "unavailable")
    ]
    if open_questions:
        blocks.append(GapNoticeBlock(
            module="research_sources",
            message=(
                f"{len(open_questions)} 道问题本轮未完成："
                + "、".join(str(q.get("question_id")) for q in open_questions[:8])
                + ("…" if len(open_questions) > 8 else "")
            ),
        ))
    ev_refs = [r for r in refs if r.startswith("ev-")]
    if ev_refs:
        blocks.append(SourceRefBlock(refs=ev_refs[:20], note="部分成果引用"))
    doc = ReportDocument(
        title=f"{ctx.ticker} 部分研究成果", entity_kind=ctx.entity_kind,
        entity_id=ctx.ticker, blocks=blocks,
        limitations=[
            f"研究以 {stop} 终止，本产物为部分成果（不是最终报告）",
            *((loop.budget_snapshot or {}).get("exhausted") and
              ["预算耗尽维度：" + "、".join((loop.budget_snapshot or {}).get("exhausted"))] or []),
        ],
    ).with_id()
    assessment = getattr(loop, "assessment", None)
    sufficiency = str(getattr(assessment, "verdict", "") or "partial")
    if sufficiency not in ("sufficient", "partial", "blocked"):
        sufficiency = "partial"
    artifact = ResearchArtifact(
        entity_kind=ctx.entity_kind, entity_id=ctx.ticker, title=doc.title,
        report_document=doc,
        claim_ids=[c["claim_id"] for c in claims if c.get("status") == "validated"],
        calculation_ids=list((checkpoint or {}).get("calculations") or []),
        plan_id=(plan or {}).get("plan_id"),
        status="draft", sufficiency=sufficiency,  # type: ignore[arg-type]
        created_at=now, evidence_cutoff=now, run_id=ctx.child_run_id,
    ).with_id()
    artifact.validation_issues = ArtifactValidator(
        kb=deps.kb, metric_store=deps.metrics).validate(artifact)
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
            "title": doc.title, "status": "draft", "sufficiency": sufficiency,
            "partial": True, "stop_reason": stop,
            "answered_questions": [a.get("question_id") for a in answered],
            "open_questions": [q.get("question_id") for q in open_questions],
            "claim_ids": artifact.claim_ids,
            "calculation_ids": artifact.calculation_ids,
        },
    ))
    path = _write_text_artifact(deps, ctx, "partial-report.md", artifact.markdown)
    _publish_report(
        deps, ctx, doc.title,
        f"部分成果（{stop} 终止）：{len(answered)} 题已完成，{len(open_questions)} 题未完成",
        path, [f"artifact: draft/{sufficiency}", "partial: true"],
        extra={"research_artifact_id": artifact.artifact_id, "artifact_status": "draft",
               "artifact_sufficiency": sufficiency, "partial": True},
    )
    # 快照发布：页面能读到部分成果（失败不阻断，service 内部已三通道可见）
    if deps.dossier_service is not None:
        with contextlib.suppress(Exception):
            deps.dossier_service.open(ctx.entity_kind, ctx.ticker, run_id=ctx.session_run_id)
    logger.info(
        "部分成果已冻结 %s（%s 终止，%d 题完成）",
        artifact.artifact_id, stop, len(answered),
    )
    return artifact.artifact_id


def _budget_summary(loop: ResearchLoop) -> str:
    """一行预算/停止原因摘要（audit §3.3）。

    旧摘要只报末轮「12 字段、9 项 typed」，用户无法判断投入与成果；这里把五轮累计的
    墙钟/模型调用/检索/token 与停止维度一起给出，并区分「执行结束」与「成果可用」。
    """
    snap = getattr(loop, "budget_snapshot", None)
    if not snap:
        return ""
    parts = [
        f"耗时 {snap.get('seconds_used', 0):.0f}s"
        + (f"/{snap['seconds_limit']:.0f}s" if snap.get("seconds_limit") else ""),
        f"模型调用 {snap.get('llm_calls', 0)} 次",
        f"检索 {snap.get('retrieval_calls', 0)}"
        + (f"/{snap['retrieval_limit']}" if snap.get("retrieval_limit") else "")
        + (f"（重复命中 {snap['duplicate_retrievals']}）" if snap.get("duplicate_retrievals") else ""),
    ]
    tokens = snap.get("tokens_used", 0)
    if tokens:
        parts.append(
            f"tokens {tokens}"
            + (f"（含估算 {snap['tokens_estimated']}）" if snap.get("tokens_estimated") else "")
        )
    if snap.get("timed_out_groups"):
        parts.append(f"慢组超时未等待：{'、'.join(snap['timed_out_groups'])}")
    if loop.budget_exhausted:
        parts.append(f"停止维度：{'、'.join(loop.budget_exhausted)}")
    text = "；预算 " + "，".join(parts)
    assessment = getattr(loop, "assessment", None)
    verdict = getattr(assessment, "verdict", None)
    if verdict and verdict != "sufficient":
        text += (
            f"；注意：命令执行已结束，但成果仍为部分可用（充分度 {verdict}）"
            "——界面不得把「执行结束」当作「结论可用」"
        )
    return text


def _prepare_research_plan(
    deps: StepDeps, ctx: StepContext, *,
    focus_override: str | None = None,
    mode_override: str | None = None,
    objective_override: str | None = None,
    entity_kind_override: str | None = None,
    entity_id_override: str | None = None,
) -> str | None:
    """创建并冻结 ResearchPlan（§7.1/§7.5）：配方识别 + 问题模板 + 缺口提级。

    新存储未装配或开关关闭 → None（旧管线行为不变，§11.3 灰度）。

    override 参数供 F1–F5 行业漏斗复用同一服务（audit §6 相邻风险：同一行业从
    不同入口不得得到不同质量）：漏斗每步用自己的 focus/objective 建 targeted 计划，
    而不是回到无计划、无 typed 工具的旧路径。
    """
    if deps.metrics is None or not deps.research_plan_enabled:
        return None
    from ..research.plan import build_plan, load_recipe, select_recipe

    entity_kind = entity_kind_override or ctx.entity_kind
    entity_id = entity_id_override or ctx.ticker
    focus = ctx.focus if focus_override is None else focus_override
    try:
        gaps = GapAnalyzer(deps.kb).analyze(entity_kind, entity_id, datetime.now(UTC))
        hints = ""
        view = deps.kb.view(entity_kind, entity_id, datetime.now(UTC))
        for field in ("business_model", "moat", "peers", "value_chain"):
            rec = view.get(field)
            if rec is not None and isinstance(rec.value, str):
                hints += " " + rec.value
        recipe_id, basis = select_recipe(
            entity_kind, hint_text=hints[:2000],
            explicit=(
                focus
                if focus in ("general", "industrial_equipment", "biotech", "industry")
                else None
            ),
        )
        recipe = load_recipe(recipe_id)
        mode = ctx.depth if ctx.depth in ("standard", "deep", "refresh", "targeted") else "standard"
        if focus and mode == "standard":
            mode = "targeted"  # 显式 focus 默认升级为 targeted（一个问题簇）
        if mode_override in ("standard", "deep", "refresh", "targeted"):
            mode = mode_override
        objective = objective_override or ctx.objective or f"深度研究 {entity_id}"
        plan = build_plan(
            entity_kind=entity_kind,
            entity_id=entity_id,
            objective=objective,
            mode=mode,
            recipe=recipe,
            focus=focus,
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
1. get_research_context 一次取回研究上下文（问题结论/字段投影/观测与论断概要）；
   query_kb 读旧字段档案；query_observations 读结构化指标观测（支持过滤与游标，
   total > returned 时用 cursor 继续）；query_claims 读研究论断；
   read_evidence 核对证据原文（refs 可批量）。
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
        # 旧字段为空不等于无内容：typed 观测/论断也是研究产出（只有两者全空才走缺口说明）
        has_typed = False
        if deps.metrics is not None:
            has_typed = bool(
                deps.metrics.observations_as_of(ctx.entity_kind, ctx.ticker, now)
                or deps.metrics.claims_as_of(ctx.entity_kind, ctx.ticker, now)
            )
        if not has_typed:
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

    # 统一知识读取（tools-plugins 方案 §5.3）：与 S1/S2 共用同一模块与契约；
    # 合成阶段只读（不装配裁决），as_of 固定在本 step 开始时刻（报告内不漂移）；
    # 限额沿用旧上限（观测 200/论断 100/计算 50），但截断不再静默：返回 total+游标。
    context_tools = make_context_tools(
        kb=deps.kb, metrics=deps.metrics, entity_kind=ctx.entity_kind, entity_id=ctx.ticker,
        namespace="prod", as_of=now, writer=deps.writer, manifest=manifest,
        events=deps.events, read_only=True,
        default_limits={"query_observations": 200, "query_claims": 100,
                        "query_calculations": 50},
    )

    tools: dict[str, Any] = {"query_kb": query_kb, **context_tools}
    submitted: dict[str, Any] | None = None
    submitted_structures: dict[str, Any] = {}
    contract = _SYNTHESIZE_CONTRACT
    if deps.metrics is not None:
        contract = _SYNTHESIZE_CONTRACT_V2

        def submit_report_document(args: dict[str, Any]) -> dict[str, Any]:
            """提交即校验（audit §3.9）：同一轮内返回可修复错误，不失掉修复机会。

            旧行为：先回 accepted，最后才在 _finalize_artifact_v2 里校验——硬错只能
            把产物降为 draft（本次事故：unresolved_claim_ref 硬失败，报告仍发布）。
            """
            nonlocal submitted
            from ..research.artifacts import (
                ArtifactValidator,
                ReportDocument,
                ResearchArtifact,
            )

            try:
                candidate = ReportDocument.model_validate({
                    "title": args.get("title") or report_title,
                    "entity_kind": ctx.entity_kind,
                    "entity_id": ctx.ticker,
                    "blocks": args.get("blocks") or [],
                    "limitations": args.get("limitations") or [],
                }).with_id()
            except Exception as e:  # 结构非法 → 当轮回报，模型可修正重提
                return {"content": json.dumps({
                    "accepted": False, "code": "invalid_structure",
                    "error": f"{type(e).__name__}: {e}"[:800],
                    "hint": "blocks 每项必须带合法 type 与该类型必填字段",
                }, ensure_ascii=False), "provenance": []}
            probe = ResearchArtifact(
                entity_kind=ctx.entity_kind, entity_id=ctx.ticker,
                title=candidate.title, report_document=candidate,
                claim_ids=_dedupe(_report_dependencies(candidate, deps.metrics)["claims"]),
                calculation_ids=_report_dependencies(candidate, deps.metrics)["calculations"],
                status="draft", sufficiency="partial", created_at=now,
                run_id=ctx.child_run_id,
            ).with_id()
            issues = ArtifactValidator(kb=deps.kb, metric_store=deps.metrics).validate(probe)
            hard = [i for i in issues if i.hard]
            submitted = args
            if hard:
                return {"content": json.dumps({
                    "accepted": True, "validated": False,
                    "hard_issues": [i.model_dump(mode="json") for i in hard[:10]],
                    "soft_issues": [i.model_dump(mode="json")
                                    for i in issues if not i.hard][:10],
                    "hint": (
                        "硬校验未过：修正不可解析的引用、剔除不采用的论断、或给裸数值"
                        "补 observation_id 后重提；不修正则产物只能是 draft"
                    ),
                }, ensure_ascii=False), "provenance": []}
            return {"content": json.dumps({
                "accepted": True, "validated": True,
                "blocks": len(candidate.blocks),
                "soft_issues": [i.model_dump(mode="json") for i in issues][:10],
            }, ensure_ascii=False), "provenance": []}

        def submit_structures(args: dict[str, Any]) -> dict[str, Any]:
            """结构化产物提交即校验 + 按 kind 部分接受（audit §3.7 + 归一层）。

            服务端先做确定性形状归一（同义词映射/描述搬移/单元素包装，逐条留痕），
            再逐 kind 解析与验证：合法 kind 立即冻结（跨调用累计合并），非法 kind 单独
            返回字段级原因——一个 kind 的形状漂移不再拖死整批（事故：5-kind 提交因
            bottleneck 字符串 6 连拒，12 步预算烧光，产物冻结 structures=[]）。
            """
            nonlocal submitted_structures
            from ..dossier.structures import (
                parse_structures_partial,
                structures_payload,
                validate_structures,
            )
            from ..research.artifacts import ref_resolvable

            raw = args.get("structures")
            if not isinstance(raw, dict) or not raw:
                return {"content": json.dumps({
                    "accepted": [], "rejected": {"_": "structures 必须是非空对象（kind → 结构体）"},
                }, ensure_ascii=False), "provenance": []}

            def _resolvable(ref: str) -> bool:
                return ref_resolvable(
                    deps.kb, deps.metrics, ref, namespace="prod",
                    entity_kind=ctx.entity_kind, entity_id=ctx.ticker,
                )

            parsed, failures, repairs = parse_structures_partial(raw)
            # 语义验证按 kind 归组（issue 前缀即 kind）：有问题的 kind 整体拒绝，其余照常接受
            for issue in validate_structures(parsed, resolvable=_resolvable):
                kind = issue.split(":", 1)[0].strip()
                failures.setdefault(kind, "")
                failures[kind] = (failures[kind] + "；" + issue if failures[kind] else issue)[:800]
                parsed.pop(kind, None)
            accepted_now = structures_payload(parsed)
            submitted_structures.update(accepted_now)  # 累计合并：重提只需修非法 kind
            resp: dict[str, Any] = {
                "accepted": sorted(submitted_structures),
                "accepted_now": sorted(accepted_now),
                "rejected": failures,
            }
            if repairs:
                resp["repairs"] = repairs[:20]  # 归一留痕：哪些形状被确定性修复
            if failures:
                resp["hint"] = (
                    "只修被拒 kind 后重提（已接受的 kind 已冻结，不必重交）：引用必须可解析，"
                    "淘汰/不可比必须给原因，flow_known=False 不得给 flow_value；"
                    "bottleneck 用布尔（描述写 note），relation 用 supplies/competes/substitutes/"
                    "depends_on/enables/value_flow，status 用 occurred/expected/unknown，"
                    "layers 用与 node.layer 相同的 key（显示名放 layer_labels）"
                )
            return {"content": json.dumps(resp, ensure_ascii=False), "provenance": []}

        tools.update({
            "submit_structures": submit_structures,
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
        # 16 步（原 12）：结构提交改按 kind 部分接受后重试压力已降，但仍需余量：
        # 读证据 + 提交结构（可修重提）+ 提交报告文档三条链各占几步（事故：12 步烧光在 6 连拒）
        max_steps=16 if deps.metrics is not None else 8,
    )
    report_md = kernel.run_turn(_synthesize_brief(deps, ctx, view, now))

    if deps.metrics is not None:
        return _finalize_artifact_v2(
            deps, ctx, report_title, view, report_md, submitted, now,
            structures=submitted_structures,
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
    # query_observations / query_claims / query_calculations / read_evidence 已迁至
    # 共享上下文工具（research/context_tools.CONTEXT_TOOL_SCHEMAS，S1/S2/合成同一契约），
    # 由 cli._all_tool_schemas 统一合并；本表只留合成阶段专有的提交类工具。
    "submit_structures": {
        "name": "submit_structures",
        "description": (
            "提交结构化产物（服务端确定性归一 + 按 kind 校验：合法 kind 立即冻结并累计合并，"
            "非法 kind 单独返回字段级原因，只需修被拒 kind 重提）。"
            "structures 的键限定五类："
            "industry_map{nodes[{node_id,label,layer,company_refs,bottleneck(布尔，瓶颈描述写 note),"
            "note,evidence_refs}],edges[{source,target,relation(supplies/competes/substitutes/"
            "depends_on/enables/value_flow),flow_known,flow_value,note,evidence_refs}],"
            "layers(与 node.layer 同一组 key：upstream/midstream/downstream/platform/application/"
            "infrastructure/demand),layer_labels(key→显示名),routes[{route,maturity,companies}],"
            "bottlenecks,value_flow_note} / "
            "candidate_assessment{objective,criteria,candidates[{entity_id,name,listing_status(listed/"
            "private/subsidiary/unknown),market,security_relation,tier(included/watchlist/excluded/"
            "needs_review),technology_stage,commercial_stage,moat_evidence,commercial_evidence,"
            "sustainability_evidence,counter_evidence,reason,next_validation,evidence_refs,investable}],"
            "stage_definitions} / "
            "comparison_matrix{title,columns[{id,label,period,unit}],rows[{label,cells(以列 id 为键的 "
            "dict，不是数组),observation_ids(同样以列 id 为键),comparable,incomparable_reason}],"
            "chartable(全部行可比才 true)} / "
            "validation_timeline{items[{event,window_start,window_end,status(occurred/expected/unknown),"
            "trigger_condition,affected_judgment,company_refs,evidence_refs}]} / "
            "executive_summary{objective,answer,stage(行业阶段一句话),why_now(为什么现在，列表),"
            "value_capture(价值捕获在哪),thesis_breakers(证伪条件，列表),tiers(分层→公司名列表),"
            "main_basis(列表),biggest_disagreement,limitations,question_progress,refs,"
            "credibility(分层→说明的 dict)}。"
            "常见中文同义词会被归一（支撑→enables、pending→expected、入选→included、上游→upstream 等）"
            "并在响应 repairs 里留痕，但请尽量直接给规范值。"
            "硬纪律：引用必须可解析；淘汰与不可比必须给原因；无流量数据时 "
            "flow_known=false 且不填 flow_value；无可校准依据时不给概率百分比或总分。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "structures": {
                    "type": "object",
                    "description": "kind → 结构体（可只提交已备好的那几类）",
                },
            },
            "required": ["structures"],
        },
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
    _publish_report(deps, ctx, report_title, summary, path,
                    ["evidence-gap", "sufficiency: blocked"], extra={
                        "research_artifact_id": artifact.artifact_id,
                        "artifact_status": "draft",
                        "artifact_sufficiency": "blocked",
                    })
    return StepResult(status="completed", summary=summary)


def _synthesize_brief(
    deps: StepDeps, ctx: StepContext, view: dict[str, Any], now: datetime
) -> str:
    """合成输入直接带目标/完整计划/评估/计算/问题证据包（audit §3.9）。

    旧实现只给一句「写研究报告」，合成 agent 得自己查 KB/指标/claim 再逐条读证据，
    12 步预算大量耗在检索上，且看不到用户目标与验收条件。
    """
    parts = [f"为 {ctx.entity_kind}:{ctx.ticker} 写研究报告。"]
    if ctx.objective:
        parts.append(f"用户研究目标（报告必须直接回答它）：{ctx.objective}")
    if deps.metrics is None:
        return "\n".join(parts)

    plans = deps.metrics.plans_for(ctx.entity_kind, ctx.ticker, limit=1)
    plan = plans[0] if plans else None
    if plan:
        parts.append(
            f"冻结研究计划 {plan.get('plan_id')}（mode={plan.get('mode')}，"
            f"配方 {plan.get('recipe_id')}@{plan.get('recipe_version')}）："
        )
        for q in (plan.get("questions") or [])[:40]:
            status = q.get("status")
            line = f"- [{q.get('question_id')}]（{q.get('priority')}/{status}）{q.get('text')}"
            if q.get("conclusion"):
                line += f"\n  当前结论：{q['conclusion']}"
            if q.get("support_refs"):
                line += f"\n  支持引用：{q['support_refs']}"
            if q.get("counter_refs"):
                line += f"\n  反方引用：{q['counter_refs']}"
            if q.get("unresolved"):
                line += f"\n  未解决：{q['unresolved']}"
            parts.append(line)
    assessment = _latest_assessment(deps, f"{ctx.entity_kind}:{ctx.ticker}")
    if assessment:
        cov = assessment.get("question_coverage") or {}
        parts.append(
            f"研究充分度评估：verdict={assessment.get('verdict')}，"
            f"问题覆盖 {cov.get('answered')}/{cov.get('applicable')}，"
            f"硬门禁{'通过' if assessment.get('hard_gate_passed') else '未过'}"
            + (f"；缺口：{assessment.get('gaps')}" if assessment.get("gaps") else "")
        )
    try:
        calc_ids = deps.metrics.list_calculation_ids(
            ctx.entity_kind, ctx.ticker, now, namespace="prod"
        )
    except Exception:  # noqa: BLE001
        calc_ids = []
    if calc_ids:
        parts.append(f"已登记计算（用 calculation_ref 引用，不重算）：{calc_ids[:20]}")
    parts.append(
        "写作纪律：① 首屏必须回答用户目标（候选分层/主要依据/最大分歧/限制），"
        "背景叙述放后面；② 关键数字用 metric_table/chart_ref 带 observation_id，"
        "不用裸值；③ 论断用 claim block 带 claim_id，引用不可解析的论断不要采用；"
        "④ 未完成的题目用 gap_notice 显式标出，不得用推测补齐；"
        "⑤ 无可校准数据时用证据支持的阶段与条件表达，不自行制造百分比或总分；"
        "⑥ 先调 submit_structures 提交结构化产物（行业实体至少交 industry_map + "
        "candidate_assessment + executive_summary，executive_summary 尽量给 tear-sheet 字段 "
        "stage/why_now/value_capture/thesis_breakers；有验证节点时交 "
        "validation_timeline；同口径数据齐时交 comparison_matrix）——页面靠这些"
        "结构渲染关系图与公司矩阵，不靠长文本；按 kind 提交即校验：合法 kind 立即冻结，"
        "非法 kind 按返回原因只修该 kind 重提；"
        "⑦ 最后调 submit_report_document（提交即校验，硬错会当轮返回可修原因）。"
    )
    del view
    return "\n".join(parts)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def _report_dependencies(doc: Any, metrics: Any) -> dict[str, list[str]]:
    """报告**实际引用**的依赖闭包（audit §3.9）。

    旧实现把当前实体全部 draft/validated claim 一并挂到报告（本次事故：
    错误草稿 claim-a4654b90f0d9 引用不存在的 fact-commercial-breakout，
    触发 unresolved_claim_ref 硬失败），且 calculation_ids 固定为空。
    """
    claims: list[str] = []
    calcs: list[str] = []
    observations: list[str] = []
    for block in getattr(doc, "blocks", []) or []:
        btype = getattr(block, "type", "")
        if btype == "claim" and getattr(block, "claim_id", None):
            claims.append(block.claim_id)
        elif btype == "metric_table":
            for row in getattr(block, "rows", []) or []:
                for cell in row:
                    if getattr(cell, "observation_id", None):
                        observations.append(cell.observation_id)
        elif btype == "comparison":
            for item in getattr(block, "items", []) or []:
                if getattr(item, "observation_id", None):
                    observations.append(item.observation_id)
        elif btype == "chart_ref":
            observations.extend(getattr(block, "metric_refs", []) or [])
        elif btype == "assumption_table" and getattr(block, "calculation_ref", None):
            calcs.append(block.calculation_ref)
    # 观测的派生计算也纳入（计算引用不丢）
    for oid in _dedupe(observations):
        try:
            obs = metrics.get_observation(oid)
        except Exception:  # noqa: BLE001 - 不可解析的引用由验证器报硬错
            obs = None
        if obs is not None and getattr(obs, "calculation_ref", None):
            calcs.append(obs.calculation_ref)
    return {
        "claims": _dedupe(claims),
        "calculations": _dedupe(calcs),
        "observations": _dedupe(observations),
    }


def _verified_claim_ids(
    deps: StepDeps, claim_ids: list[str], *, namespace: str = "prod",
) -> tuple[list[str], list[dict[str, Any]]]:
    """只纳入引用可解析的论断；未通过的进缺口区（不靠关闭校验通过）。"""
    from ..research.artifacts import ref_resolvable

    ok: list[str] = []
    broken: list[dict[str, Any]] = []
    for cid in claim_ids:
        claim = deps.metrics.get_claim(cid)
        if claim is None:
            broken.append({"claim_id": cid, "reason": "claim 未登记"})
            continue
        refs = [*(claim.get("support_refs") or []), *(claim.get("counter_refs") or [])]
        bad = [
            r for r in refs
            if not ref_resolvable(
                deps.kb, deps.metrics, r, namespace=namespace,
                entity_kind=claim.get("entity_kind"), entity_id=claim.get("entity_id"),
            )
        ]
        if bad:
            broken.append({"claim_id": cid, "reason": f"引用不可解析: {bad}",
                           "status": claim.get("status")})
        else:
            ok.append(cid)
    return ok, broken


def _finalize_artifact_v2(
    deps: StepDeps,
    ctx: StepContext,
    report_title: str,
    view: dict[str, Any],
    report_md: str | None,
    submitted: dict[str, Any] | None,
    now: datetime,
    structures: dict[str, Any] | None = None,
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
    # 依赖闭包（audit §3.9）：只纳入报告实际引用且经校验的论断/计算，
    # 未通过的草稿保留在缺口区（可见、可修正后重验），不靠关闭校验通过。
    referenced = _report_dependencies(doc, deps.metrics)
    ok_claims, broken_claims = _verified_claim_ids(deps, referenced["claims"])
    if broken_claims:
        from ..research.artifacts import GapNoticeBlock

        # 剔除不采用的论断 block（audit §3.9）：已判定不可用的草稿不能既留在正文里
        # 又指望校验放行——剔除 + 缺口区留痕 + 重新校验，不靠关闭校验通过。
        broken_ids = {str(b["claim_id"]) for b in broken_claims}
        kept, dropped = [], []
        for block in doc.blocks:
            if getattr(block, "type", "") == "claim" and getattr(block, "claim_id", "") in broken_ids:
                dropped.append(str(block.claim_id))
                continue
            kept.append(block)
        doc.blocks = kept
        if dropped:
            doc.limitations.append(
                f"剔除未通过引用校验的论断（保留在缺口区，修正后可重新纳入）：{dropped}"
            )
        doc.blocks.append(GapNoticeBlock(
            module="research_sources",
            message=(
                f"{len(broken_claims)} 条被引用的论断未通过引用校验，未纳入本报告依赖："
                + "；".join(
                    f"{b['claim_id']}（{b['reason']}）" for b in broken_claims[:5]
                )
            ),
        ))
        logger.warning(
            "报告依赖闭包剔除 %d 条论断：%s", len(broken_claims),
            [b["claim_id"] for b in broken_claims],
        )
    artifact = ResearchArtifact(
        entity_kind=ctx.entity_kind, entity_id=ctx.ticker, title=report_title,
        report_document=doc,
        claim_ids=ok_claims,
        calculation_ids=referenced["calculations"],
        structures=dict(structures or {}),
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
            "calculation_ids": artifact.calculation_ids,
            "excluded_claims": broken_claims,
            "structures": sorted((structures or {}).keys()),
            "referenced_observations": referenced["observations"],
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

    # 档案快照发布（dossier/published 事件由 service 落；失败不阻断报告发布，
    # 但必须可见：service 内部落 dossier/publish_failed + 日志）
    snapshot_id = None
    if deps.dossier_service is not None:
        try:
            snap, _created = deps.dossier_service.open(
                ctx.entity_kind, ctx.ticker, run_id=ctx.session_run_id
            )
            snapshot_id = snap["context"]["snapshot_id"]
            # 快照依赖进产物（audit §3.9：artifact 自身的计算与快照依赖不得为空）
            artifact.snapshot_refs = [snapshot_id]
            deps.metrics.save_artifact(
                artifact_id=artifact.artifact_id, namespace="prod",
                payload=artifact.model_dump(mode="json"),
            )
        except Exception as e:
            logger.error("档案快照发布失败 %s:%s: %s", ctx.entity_kind, ctx.ticker, e, exc_info=True)
    _publish_report(deps, ctx, report_title, summary, path, flags, extra={
        "research_artifact_id": artifact.artifact_id,
        "artifact_status": artifact.status,
        "artifact_sufficiency": artifact.sufficiency,
        "dossier_snapshot_id": snapshot_id,
    })
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
    *, kind: str = "research_report", extra: dict[str, Any] | None = None,
) -> None:
    """Sessions 报告卡事件。沿用 report/published，新链路增加 artifact/snapshot 引用
    （§6.5：Sessions 卡片可深链到冻结研报与档案快照，显示真实产物状态）。"""
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
                "entity": f"{ctx.entity_kind}:{ctx.ticker}",
                **(extra or {}),
            },
        )
    )


# ---------------- S2 档案更新（thesis 修订） ----------------


def step_profile_update(deps: StepDeps, ctx: StepContext) -> StepResult:
    """S2 档案更新（tools-plugins 方案 §9.1 第一步：从重写 thesis 扩展为整合与更新）。

    读取冻结基线与本轮研究产出（问题结论/观测/论断/计算）→ 检查并裁决开放冲突
    → 修订 thesis：旧 Fact 保兼容投影，同时保存为带证据与前提的分析 Claim
    （新读侧入口，dossier 投影据此取总论）。完整 consolidator（依赖失效/语义 diff）属 P2-B。
    """
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

    # 统一知识读取（方案 §5.3）：S2 与 S1/合成共用 typed 查询与证据回读；
    # 裁决入口开放（整合阶段需要消除冲突后再下结论）；as_of 固定在本 step 开始时刻。
    plan_id: str | None = None
    if deps.metrics is not None:
        plans = deps.metrics.plans_for(ctx.entity_kind, ctx.ticker, namespace="prod", limit=1)
        plan_id = str(plans[0].get("plan_id")) if plans else None
    context_tools = make_context_tools(
        kb=deps.kb, metrics=deps.metrics, entity_kind=ctx.entity_kind, entity_id=ctx.ticker,
        namespace="prod", as_of=now, plan_id=plan_id, writer=deps.writer,
        manifest=manifest, events=deps.events,
    )

    def propose_thesis(args: dict[str, Any]) -> dict[str, Any]:
        evidence_ids = [str(r) for r in (args.get("evidence_ids") or [])]
        thesis = str(args["thesis"])
        try:
            fact_id = deps.writer.write_fact(
                Fact(
                    entity_kind=ctx.entity_kind,  # type: ignore[arg-type]
                    entity_id=ctx.ticker,
                    field="thesis",
                    value=thesis,
                    knowledge_time=datetime.now(UTC),
                    evidence_ids=evidence_ids,
                    run_id=ctx.child_run_id,
                ),
                run=manifest,
            )
        except Exception as e:
            return {"content": f"rejected: {e}", "provenance": []}
        outcome["fact_id"] = fact_id
        claim_id: str | None = None
        # 事实与分析分层（方案 §9.1）：新 thesis 同时保存为带证据与前提的 Claim；
        # 旧 thesis Fact 仅作兼容投影（decision 入口依赖旧格式，先不破坏）。
        if deps.metrics is not None:
            from ..research.artifacts import ClaimVerification, ResearchClaim, ref_resolvable

            unresolved = [
                r for r in evidence_ids
                if not ref_resolvable(
                    deps.kb, deps.metrics, r, namespace="prod",
                    entity_kind=ctx.entity_kind, entity_id=ctx.ticker,
                )
            ]
            status = "validated" if evidence_ids and not unresolved else "draft"
            claim = ResearchClaim(
                entity_kind=ctx.entity_kind,  # type: ignore[arg-type]
                entity_id=ctx.ticker,
                statement=thesis, kind="analysis",
                support_refs=evidence_ids,
                limitations=[str(x) for x in (args.get("limitations") or [])],
                status=status,  # type: ignore[arg-type]
                verification=ClaimVerification(
                    references_valid=status == "validated",
                    verified_by="profile_update:references",
                ),
                evidence_cutoff=now, run_id=ctx.child_run_id, namespace="prod",
                legacy_field="thesis",
            ).with_id()
            deps.metrics.save_claim(
                claim_id=claim.claim_id, namespace="prod",
                payload=claim.model_dump(mode="json"),
            )
            claim_id = claim.claim_id
            outcome["claim_id"] = claim_id
        return {"content": json.dumps(
            {"fact_id": fact_id, "claim_id": claim_id}, ensure_ascii=False
        ), "provenance": []}

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
        tools={"query_kb": query_kb, "propose_thesis": propose_thesis, **context_tools},
        # 6 → 10 步：整合需要读上下文/核证据/裁决冲突的余量（方案 §9.1）
        max_steps=10,
    )
    kernel.run_turn(
        f"请基于本轮研究产出整合更新 {ctx.entity_kind}:{ctx.ticker} 的档案论点。\n"
        "步骤：① get_research_context 读取冻结基线与本轮产出（问题结论/观测/论断/计算）；"
        "② list_conflicts 检查开放冲突，有则先 adjudicate_conflict 裁决（给 rationale）；"
        "③ read_evidence 核对关键证据原文；④ propose_thesis 提交修订（绑定支撑证据 id，"
        "证据不足的点写进 limitations）。若上下文与档案已一致且无新证据，可不提交（保持现状）。"
    )
    if "fact_id" in outcome:
        extra = f"，claim {outcome['claim_id']}" if outcome.get("claim_id") else ""
        return StepResult(status="completed",
                          summary=f"thesis 已修订（{outcome['fact_id']}{extra}）")
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
    """F1/F2 共用：行业实体的 ResearchLoop（gap 驱动 + 证据纪律不变）。

    audit §6 相邻风险整改：旧 `/industry` 入口不装配 plan/typed/synthesize，
    同一行业从两个入口得到不同质量。这里改为复用同一研究服务：
    - 为本步建 targeted 计划（focus = 本步 playbook 目标），于是问题驱动、预算闸、
      充分度评估与 `/research` 完全同源；
    - 装配 metrics/metric_writer/calculations → propose_metric/propose_claim/
      answer_question/calculate_metric 可用，产出落同一个 typed 库，同一页面可读；
    - 计划创建失败时降级为旧字段驱动路径（不阻断漏斗）。
    """
    playbook, ver = load_playbook(playbook_name)
    deps.events.append(
        Event(run_id=ctx.child_run_id, type="research/playbook",
              payload={"name": playbook_name, "version": ver})
    )
    manifest = _open_child(deps, ctx, playbook_name)
    step_objective = f"调研主题：{ctx.objective}"
    plan_id = _prepare_research_plan(
        deps, ctx,
        focus_override=ctx.focus or playbook_name,
        mode_override="targeted",
        objective_override=step_objective,
        entity_kind_override="industry",
    )
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
        fetch_document_paged=deps.fetch_document_paged,
        worker_llms=deps.worker_llm_for(4) if deps.worker_llm_for else None,
        plan_id=plan_id,
        metrics=deps.metrics,
        metric_writer=deps.metric_writer,
        calculations=deps.calculations,
        max_record_chars=deps.max_record_chars,
    )
    loop.run(
        "industry", ctx.ticker,
        f"{step_objective}\n\n{playbook}",
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
            fetch_paged=deps.fetch_document_paged,
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
        fetch_paged=deps.fetch_document_paged,
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

        # 共享证据读取（方案 §5.3）：与 S1/S2/合成同一契约（单条 evidence_id 或批量 refs）
        shared_read = make_context_tools(
            kb=deps.kb, metrics=deps.metrics, entity_kind="stock", entity_id=ticker,
            namespace="prod", read_only=True,
        )["read_evidence"]

        notes: dict[str, str] = {}

        def run_perspective(role_key: str, role_desc: str, llm: LLM,
                            ticker: str = ticker, profile_md: str = profile_md,
                            notes: dict = notes, read_ev=shared_read) -> None:
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
                    tools={"read_evidence": read_ev, "calc": calc_tool},
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
        # 子 run 必须落 run/created（带 parent_run_id）：否则会话列表认不出它是子 run，
        # 会在 Sessions 里冒出一个「空闲、点开什么都没有」的幽灵会话（实测 6 个）
        deps.events.append(
            Event(run_id=cio_run, type=RUN_CREATED,
                  payload={"parent_run_id": ctx.child_run_id, "kind": "committee",
                           "ticker": ticker, "role": "cio"})
        )
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
        fetch_paged=deps.fetch_document_paged,
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
                fetch_document_paged=deps.fetch_document_paged,
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

    # 共享证据读取（方案 §5.3）：F5 与 S1/S2/合成/委员会同一契约
    shared_read_evidence = make_context_tools(
        kb=deps.kb, metrics=deps.metrics, entity_kind=ctx.entity_kind, entity_id=ctx.ticker,
        namespace="prod", read_only=True,
    )["read_evidence"]

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
        tools={"read_evidence": shared_read_evidence, "calc": calc_tool},
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
