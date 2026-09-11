"""报告依赖闭包与提交即校验（audit §3.9 P1）。

事故形态（live-a2cce641，事件 seq 196202）：最终产物校验发现
`claim-a4654b90f0d9` 引用了不存在的 `fact-commercial-breakout`，触发
`unresolved_claim_ref` 硬失败——该 claim 早已是 draft，但旧代码把当前实体所有
draft/validated claim 一并挂到报告，错误草稿也进了正式报告的依赖集合；
同时 `calculation_ids=[]`、`snapshot_refs=[]`，submit_report_document 先回
accepted、最后才校验，失去同轮修复机会。

判据：只纳入报告实际引用且经校验的依赖闭包；未通过的草稿留在缺口区（可见、
可修正后重验），不靠关闭校验通过；计算与快照依赖不丢；提交当轮就能拿到可修复错误。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from finance_agent.commands.steps import (
    StepContext,
    StepDeps,
    _report_dependencies,
    _verified_claim_ids,
    step_synthesize,
)
from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.dossier.projector import DossierProjector
from finance_agent.dossier.service import DossierService
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.gateway import DataGateway
from finance_agent.harness.approvals import ApprovalService
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.research.calculations import CalculationService

NOW = datetime(2024, 6, 1, tzinfo=UTC)


def tc(i: int, name: str, args: dict) -> ToolCall:
    return ToolCall(call_id=f"c{i}", name=name, arguments=args)


def make_env(tmp_path: Path, script: list[AssistantReply], *, with_dossier: bool = True):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "metrics.db")
    events = EventStore(tmp_path / "events.db")
    decisions = DecisionStore(tmp_path / "decisions.db")
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    calcs = CalculationService(metrics, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-synth")
    kb.add_evidence(Evidence(
        evidence_id="ev-1", source_id="demo", verbatim_quote="revenue 1500 million FY2024",
        retrieved_at=NOW, available_at=NOW, pit_grade=PitGrade.A,
    ))
    kb.assert_fact(Fact(
        entity_kind="industry", entity_id="ai-for-science", field="market_size",
        value="市场规模 1500 million（FY2024）", knowledge_time=NOW, evidence_ids=["ev-1"],
    ))
    dossier = None
    if with_dossier:
        projector = DossierProjector(kb=kb, metrics=metrics, decisions=decisions)
        dossier = DossierService(kb=kb, metrics=metrics, projector=projector,
                                 events=events, decisions=decisions)
    deps = StepDeps(
        events=events, kb=kb, writer=ProfileWriter(store=kb, events=events),
        gateway=gateway,
        decisions=DecisionService(kb=kb, decisions=decisions, events=events),
        llm_for=lambda role: MockLLM(list(script)),
        approvals=ApprovalService(events),
        evals_dir=tmp_path / "evals", reports_dir=tmp_path / "reports",
        knowledge_dir=tmp_path / "knowledge",
        metrics=metrics, metric_writer=mw, calculations=calcs, dossier_service=dossier,
    )
    ctx = StepContext(
        command_id="cmd-1", session_run_id="sess-1", child_run_id="child-synth",
        ticker="ai-for-science", objective="哪些公司在形成护城河", config="",
        should_cancel=lambda: False, entity_kind="industry", depth="deep",
    )
    return deps, ctx, kb, metrics, events


def seed_claims(metrics, *, good: str = "claim-good", bad: str = "claim-bad"):
    """一条引用可解析的论断 + 一条引用不存在事实的草稿（事故形态）。"""
    metrics.save_claim(claim_id=good, namespace="prod", payload={
        "claim_id": good, "entity_kind": "industry", "entity_id": "ai-for-science",
        "kind": "inference", "status": "validated", "statement": "平台已形成数据壁垒",
        "created_at": NOW.isoformat(), "support_refs": ["ev-1"], "counter_refs": [],
    })
    metrics.save_claim(claim_id=bad, namespace="prod", payload={
        "claim_id": bad, "entity_kind": "industry", "entity_id": "ai-for-science",
        "kind": "inference", "status": "draft",
        "statement": "公司已实现商业爆发",  # 事故里正是这类无据草稿进了正式报告
        "created_at": NOW.isoformat(), "support_refs": ["fact-commercial-breakout"],
        "counter_refs": [],
    })


def seed_calculation(metrics, *, calc_id="calc-1"):
    metrics.save_calculation(
        calculation_id=calc_id, namespace="prod", entity_kind="industry",
        entity_id="ai-for-science", formula_id="yoy_growth", formula_version=1,
        status="ok", result="0.18", unit="ratio", input_hash="h1", created_at=NOW,
        run_id="r1", payload={"calculation_id": calc_id, "formula_id": "yoy_growth"},
    )


BLOCKS = [
    {"type": "heading", "level": 1, "text": "研究结论"},
    {"type": "claim", "claim_id": "claim-good", "note": "数据壁垒"},
    {"type": "claim", "claim_id": "claim-bad", "note": "商业爆发（草稿）"},
    {"type": "metric_table", "title": "关键指标", "columns": ["指标", "值"],
     "rows": [[{"label": "市场规模", "value": "1500000000", "observation_id": "obs-1",
               "unit": "USD"}]]},
    {"type": "assumption_table", "title": "增速假设", "assumptions": {"g": "0.18"},
     "calculation_ref": "calc-1"},
    {"type": "source_ref", "refs": ["ev-1"], "note": "FY2024 披露"},
]


def submit_script(blocks=None, *, title="行业研究报告"):
    return [
        AssistantReply(content="", tool_calls=[tc(0, "query_claims", {})]),
        AssistantReply(content="", tool_calls=[tc(1, "query_calculations", {})]),
        AssistantReply(content="", tool_calls=[tc(2, "submit_report_document", {
            "title": title, "blocks": blocks if blocks is not None else BLOCKS,
            "limitations": ["部分问题未完成"],
        })]),
        AssistantReply(content="report submitted"),
    ]


class TestDependencyHelpers:
    def test_dependencies_come_from_blocks_only(self, tmp_path):
        deps, ctx, kb, metrics, events = make_env(tmp_path, submit_script())
        seed_claims(metrics)
        seed_calculation(metrics)
        from finance_agent.research.artifacts import ReportDocument

        doc = ReportDocument.model_validate({
            "title": "t", "entity_kind": "industry", "entity_id": "ai-for-science",
            "blocks": BLOCKS,
        })
        found = _report_dependencies(doc, metrics)
        assert found["claims"] == ["claim-good", "claim-bad"]
        assert found["calculations"] == ["calc-1"]
        assert found["observations"] == ["obs-1"]

    def test_broken_claim_is_excluded_with_reason(self, tmp_path):
        deps, ctx, kb, metrics, events = make_env(tmp_path, submit_script())
        seed_claims(metrics)
        ok, broken = _verified_claim_ids(deps, ["claim-good", "claim-bad", "claim-ghost"])
        assert ok == ["claim-good"]
        reasons = {b["claim_id"]: b["reason"] for b in broken}
        assert "fact-commercial-breakout" in reasons["claim-bad"]
        assert "未登记" in reasons["claim-ghost"]


class TestFinalizeArtifact:
    def _run(self, tmp_path, *, blocks=None, with_dossier=True):
        script = submit_script(blocks)
        deps, ctx, kb, metrics, events = make_env(
            tmp_path, script, with_dossier=with_dossier)
        seed_claims(metrics)
        seed_calculation(metrics)
        result = step_synthesize(deps, ctx)
        artifacts = metrics.artifacts_as_of("industry", "ai-for-science", datetime.now(UTC))
        return result, artifacts[0], events, metrics

    def test_unverified_claim_excluded_and_made_visible(self, tmp_path):
        """错误草稿不进正式依赖集合，但保留在缺口区（不靠关闭校验通过）。"""
        result, art, events, metrics = self._run(tmp_path)
        assert art["claim_ids"] == ["claim-good"]
        assert "claim-bad" not in art["claim_ids"]
        gap_blocks = [b for b in art["report_document"]["blocks"]
                      if b.get("type") == "gap_notice"]
        assert gap_blocks and "claim-bad" in gap_blocks[-1]["message"]
        created = [e for e in events.read("child-synth")
                   if e.type == "research/artifact_created"][0]
        assert created.payload["excluded_claims"]
        assert created.payload["excluded_claims"][0]["claim_id"] == "claim-bad"

    def test_calculation_and_snapshot_refs_are_not_empty(self, tmp_path):
        _, art, events, metrics = self._run(tmp_path)
        assert art["calculation_ids"] == ["calc-1"], "计算引用不得丢失"
        assert art["snapshot_refs"], "快照依赖不得为空（发布闭环）"
        created = [e for e in events.read("child-synth")
                   if e.type == "research/artifact_created"][0]
        assert created.payload["referenced_observations"] == ["obs-1"]

    def test_hard_failure_from_unreferenced_draft_is_gone(self, tmp_path):
        """事故里 unresolved_claim_ref 硬失败源自「全部 claim 挂进报告」；
        现在未引用的草稿不进依赖闭包，产物不该因此被硬失败拖住。"""
        _, art, events, metrics = self._run(tmp_path)
        codes = [i["code"] for i in art["validation_issues"]]
        assert "unresolved_claim_ref" not in codes

    def test_submit_returns_fixable_errors_in_the_same_turn(self, tmp_path):
        """提交即校验：引用不可解析时当轮返回硬错与修复提示。"""
        bad_blocks = [
            {"type": "heading", "level": 1, "text": "结论"},
            {"type": "claim", "claim_id": "claim-ghost", "note": "不存在的论断"},
            {"type": "paragraph", "text": "收入 1500 million [ev-1]"},
        ]
        script = [
            AssistantReply(content="", tool_calls=[tc(0, "query_claims", {})]),
            AssistantReply(content="", tool_calls=[tc(1, "submit_report_document", {
                "title": "行业研究报告", "blocks": bad_blocks,
            })]),
            # 同轮修正重提（旧行为先回 accepted、最后才校验，修不了）
            AssistantReply(content="", tool_calls=[tc(2, "submit_report_document", {
                "title": "行业研究报告",
                "blocks": [
                    {"type": "heading", "level": 1, "text": "结论"},
                    {"type": "paragraph", "text": "收入 1500 million [ev-1]"},
                ],
            })]),
            AssistantReply(content="fixed"),
        ]
        deps, ctx, kb, metrics, events = make_env(tmp_path, script)
        seed_claims(metrics)
        step_synthesize(deps, ctx)
        results = [e for e in events.read("child-synth") if e.type == "tool/result"
                   and e.payload.get("name") == "submit_report_document"]
        first = json.loads(results[0].payload["content"])
        assert first["accepted"] is True and first["validated"] is False
        assert any(i["code"] == "unresolved_claim" for i in first["hard_issues"])
        assert "重提" in first["hint"]
        second = json.loads(results[1].payload["content"])
        assert second["validated"] is True

    def test_invalid_structure_is_rejected_immediately(self, tmp_path):
        script = [
            AssistantReply(content="", tool_calls=[tc(0, "submit_report_document", {
                "title": "t", "blocks": [{"type": "not_a_block_type"}],
            })]),
            AssistantReply(content="", tool_calls=[tc(1, "submit_report_document", {
                "title": "t",
                "blocks": [{"type": "paragraph", "text": "收入 1500 million [ev-1]"}],
            })]),
            AssistantReply(content="done"),
        ]
        deps, ctx, kb, metrics, events = make_env(tmp_path, script)
        step_synthesize(deps, ctx)
        results = [e for e in events.read("child-synth") if e.type == "tool/result"
                   and e.payload.get("name") == "submit_report_document"]
        first = json.loads(results[0].payload["content"])
        assert first["accepted"] is False and first["code"] == "invalid_structure"
        assert first["error"]

    def test_synthesis_input_carries_objective_and_plan(self, tmp_path):
        """合成输入直接带用户目标/计划/评估/计算，不再让 agent 自己摸索。"""
        deps, ctx, kb, metrics, events = make_env(tmp_path, submit_script())
        seed_claims(metrics)
        seed_calculation(metrics)
        metrics.save_plan(plan_id="plan-1", namespace="prod", payload={
            "plan_id": "plan-1", "entity_kind": "industry", "entity_id": "ai-for-science",
            "objective": ctx.objective, "mode": "deep", "recipe_id": "industry",
            "recipe_version": "1", "created_at": NOW.isoformat(), "status": "active",
            "questions": [{"question_id": "objective-technology_moat-abc123",
                           "text": "哪些公司有技术壁垒", "priority": "high",
                           "status": "unanswered", "module": "candidate_pool"}],
            "budgets": {}, "scope": {},
        })
        step_synthesize(deps, ctx)
        briefs = [e for e in events.read("child-synth") if e.type == "user/message"]
        text = " ".join(str(b.payload.get("content", "")) for b in briefs)
        assert ctx.objective in text
        assert "plan-1" in text
        assert "objective-technology_moat-abc123" in text
        assert "calc-1" in text
        assert "不自行制造百分比或总分" in text


class TestBatchVerificationAtSynthesize:
    """F15（B 组实测核验覆盖 3/106）：合成定稿前服务端批量核验报告引用的
    validated 未核验论断——不再依赖模型主动调 verify_claim。"""

    def _deps_with_judge(self, tmp_path, judge_payload):
        script = submit_script()
        deps, ctx, kb, metrics, events = make_env(tmp_path, script)
        deps.judge_llm = MockLLM([AssistantReply(content=json.dumps({
            "atomic_claims": judge_payload,
            "reasoning_review": {"premises_explicit": True, "boundary_ok": True,
                                 "alternative_explanations": []},
            "next_actions": [],
        }))])
        deps.judge_llm.model_name = "judge-model"
        seed_claims(metrics)
        seed_calculation(metrics)
        return deps, ctx, kb, metrics, events

    def test_referenced_validated_claims_batch_verified(self, tmp_path):
        deps, ctx, kb, metrics, events = self._deps_with_judge(
            tmp_path, [{"text": "数据壁垒", "verdict": "supported",
                        "supporting_refs": ["ev-1"]}])
        result = step_synthesize(deps, ctx)
        assert result.status == "completed"
        claim = metrics.get_claim("claim-good")
        assert claim["verification"]["evidence_support"] == "supported", \
            "报告引用的 validated 论断在定稿前被服务端核验（不依赖模型主动调用）"
        assert claim["verification"]["verified_by"].startswith("judge-model")
        ev = [e for e in events.read(ctx.child_run_id)
              if e.type == "research/artifact_created"][0]
        batch = ev.payload["verification_batch"]
        assert batch["attempted"] == 1 and batch["verified"] == 1

    def test_contradicted_found_at_synthesize_blocks_validated(self, tmp_path):
        """批量核验发现 contradicted → 论断降级 draft + 产物硬失败不得 validated。"""
        deps, ctx, kb, metrics, events = self._deps_with_judge(
            tmp_path, [{"text": "数据壁垒", "verdict": "contradicted",
                        "supporting_refs": [], "notes": "原文不支持"}])
        step_synthesize(deps, ctx)
        claim = metrics.get_claim("claim-good")
        assert claim["status"] == "draft", "contradicted 论断在定稿门禁前降级"
        art = metrics.artifacts_as_of("industry", "ai-for-science", datetime.now(UTC))[0]
        assert art["status"] == "draft"
        assert any(i["code"] == "contradicted_claim_ref" and i["hard"]
                   for i in art["validation_issues"])

    def test_verifier_unavailable_keeps_soft_visibility(self, tmp_path):
        """核验不可用（无 judge、脚本非 JSON）→ 论断保持 validated+unchecked，
        产物不被打回（诚实降级），但软问题 claim_content_unchecked 逐条可见。"""
        deps, ctx, kb, metrics, events = make_env(tmp_path, submit_script())
        seed_claims(metrics)
        seed_calculation(metrics)
        step_synthesize(deps, ctx)
        claim = metrics.get_claim("claim-good")
        assert claim["verification"]["evidence_support"] == "unchecked"
        art = metrics.artifacts_as_of("industry", "ai-for-science", datetime.now(UTC))[0]
        assert any(i["code"] == "claim_content_unchecked" and not i["hard"]
                   for i in art["validation_issues"])
        ev = [e for e in events.read(ctx.child_run_id)
              if e.type == "research/artifact_created"][0]
        assert ev.payload["verification_batch"]["unavailable"] >= 1
