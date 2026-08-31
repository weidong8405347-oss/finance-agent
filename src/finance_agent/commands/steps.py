"""Step agents：command pipeline 的执行单元（各带 kernel loop + tools + hooks）。

纪律（redesign §3.3）：
- 每个 step 跑在独立 child run（run/created 带 parent_run_id），上下文互相隔离；
- step 间数据接力经 KB / DecisionStore / 结构化摘要，不经共享对话；
- 失败三通道：子流 error 事件 + 父流 step_agent/end(status=error) + 日志（runner 负责）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
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
from ..harness.approvals import ApprovalService
from ..harness.manifest import RunManifest, RunMode
from ..knowledge.gaps import GapAnalyzer
from ..knowledge.models import Fact
from ..knowledge.store import BitemporalStore
from ..knowledge.writer import ProfileWriter
from ..llm.base import LLM
from ..loop.kernel import AgentKernel
from ..research.loop import ResearchLoop

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
    fetch_document: Callable[[str], str] | None = None  # 文档正文抓取（live 研究用；eval 禁用）
    max_rounds: int = 3
    max_steps_per_round: int = 16
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
    )
    reports = loop.run(
        ctx.entity_kind, ctx.ticker, ctx.objective or f"深度研究 {ctx.ticker}"
    )
    if loop.stop_reason == "cancelled":
        return _cancelled(ctx)
    gaps = GapAnalyzer(deps.kb).analyze(ctx.entity_kind, ctx.ticker, datetime.now(UTC))

    # 轮次摘要落盘（钻取用）；report/published 卡片由 synthesize step 负责（Q3）
    _write_research_artifact(deps, ctx, reports, loop.stop_reason or "", gaps.completeness)
    if not reports:
        summary = f"档案完整度已达标（{gaps.completeness:.0%}），无需新一轮研究"
    else:
        last = reports[-1]
        summary = (
            f"研究 {len(reports)} 轮（{loop.stop_reason}）："
            f"完整度 {last.completeness_before:.0%} → {last.completeness_after:.0%}，"
            f"写入 {len(last.facts_written)} 字段"
        )
    return StepResult(status="completed", summary=summary)


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


def step_synthesize(deps: StepDeps, ctx: StepContext) -> StepResult:
    """研究轮收敛后的报告合成：档案 + 证据 → 结构化研报（ResearchFoldCard 的内容源）。"""
    if ctx.should_cancel():
        return _cancelled(ctx)
    now = datetime.now(UTC)
    view = deps.kb.view(ctx.entity_kind, ctx.ticker, now)
    manifest = _open_child(deps, ctx, "synthesize")

    report_title = f"{ctx.ticker} 研究报告"
    if not view:
        summary = "档案为空，无内容可合成（先跑研究）"
        artifact = _write_text_artifact(deps, ctx, "report.md", f"# {report_title}\n\n{summary}\n")
        _publish_report(deps, ctx, report_title, summary, artifact, ["evidence-gap"])
        return StepResult(status="completed", summary=summary)

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

    deps.events.append(
        Event(
            run_id=ctx.child_run_id,
            type=CONTEXT_INJECT,
            payload={"role": "system", "content": _SYNTHESIZE_CONTRACT},
        )
    )
    kernel = AgentKernel(
        store=deps.events,
        llm=deps.llm_for("research"),
        manifest=manifest,
        tools={"query_kb": query_kb, "read_evidence": read_evidence},
        max_steps=8,
    )
    report_md = kernel.run_turn(f"为 {ctx.entity_kind}:{ctx.ticker} 写研究报告。")
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
    deps: StepDeps, ctx: StepContext, title: str, summary: str, artifact: Path, flags: list[str]
) -> None:
    deps.events.append(
        Event(
            run_id=ctx.session_run_id,
            type=REPORT_PUBLISHED,
            payload={
                "child_run_id": ctx.child_run_id,
                "kind": "research_report",
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
        card_id = loop.run(ctx.entity_kind, ctx.ticker)
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


STEPS: dict[str, Callable[[StepDeps, StepContext], StepResult]] = {
    "research": step_research,
    "profile_update": step_profile_update,
    "synthesize": step_synthesize,
    "decide": step_decide,
    "process_eval": step_process_eval,
    "evaluate": step_evaluate,
}

STEP_TITLES: dict[str, str] = {
    "research": "S1 研究",
    "profile_update": "S2 档案更新",
    "synthesize": "报告合成",
    "decide": "S3 决策",
    "process_eval": "过程评估",
    "evaluate": "S4 效果评估",
}

#: step agent 能力清单（GET /api/capabilities 的数据源；新增能力 = 在这里登记）
STEP_MANIFEST: dict[str, dict[str, Any]] = {
    "research": {
        "title": "S1 研究（轮次制：gap 分析 → 采集 → 证据验证 → 落库）",
        "model_role": "research",
        "tools": ["query_kb", "read_edgar_filing", "register_evidence", "propose_fact"],
        "plugins": ["gap 分析（完整度+新鲜度驱动）", "rubric 评审（judge）", "停滞/预算收敛"],
        "hooks": ["evidence-binding（服务端子串校验）", "numeric-guard", "ProfileWriter 单写者"],
        "budget": {"max_rounds": 3, "max_steps_per_round": 16},
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
