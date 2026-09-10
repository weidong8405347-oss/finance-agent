"""P2-B profile.consolidator 验收（tools-plugins 方案 §9.1/§9.3）。

判据（方案 §11 P2-B 交付/验收）：
- 依赖图 document→observation→calculation→claim→module 复用现有引用关系；
- prepare 是确定性只读预览：待合并/重复/冲突/失效依赖/预期 diff + change_set 身份；
- commit 幂等（重跑返回首次结果，不重复落失效记录）；
- expected_base_hash 不符 → 拒绝（基线过期不拿旧基线盖新数据）；
- 依赖失效走追加记录（claim_invalidations，按 invalidated_at 时态合并）：
  旧快照/历史投影保持不变；
- S2 真实装配：prepare → 裁决 → thesis → commit 全链工具可用。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from finance_agent.dossier.consolidator import (
    ConsolidationError,
    ProfileConsolidator,
)
from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metric_writer import TypedMetricWriter
from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter

NOW = datetime.now(UTC)
T0 = datetime(2024, 3, 1, tzinfo=UTC)
T1 = datetime(2024, 6, 1, tzinfo=UTC)
FY2023 = MetricPeriod(start=date(2023, 1, 1), end=date(2023, 12, 31),
                      frequency="FY", fiscal_label="FY2023")
RUN = RunManifest(run_id="r-cons", mode=RunMode.LIVE)


@pytest.fixture()
def env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    metrics = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
    kb.add_evidence(Evidence(
        evidence_id="ev-1", source_id="edgar", verbatim_quote="backlog 300 million USD",
        retrieved_at=NOW, available_at=T0, pit_grade=PitGrade.A,
    ))
    obs_id, _ = metrics.assert_observation(ReportedObservation(
        entity_kind="stock", entity_id="BE", metric_key="backlog", period=FY2023,
        value="300000000", unit="USD", currency="USD",
        raw=RawValue(value_text="300 million", unit_text="USD", quote_ref="ev-1"),
        evidence_refs=["ev-1"], knowledge_time=T0, source_available_at=T0,
        retrieved_at=T1, created_at=T1, pit_grade=PitGrade.A,
    ))
    # 依赖 obs 的论断（失效传播对象）
    metrics.save_claim(claim_id="claim-dep", namespace="prod", payload={
        "claim_id": "claim-dep", "entity_kind": "stock", "entity_id": "BE",
        "statement": "在手订单支撑增长", "kind": "inference", "status": "validated",
        "question_id": "q1", "support_refs": [obs_id], "counter_refs": [],
        "limitations": [], "created_at": T1.isoformat(), "namespace": "prod",
    })
    # 计划（module 映射）
    metrics.save_plan(plan_id="plan-c", namespace="prod", payload={
        "plan_id": "plan-c", "entity_kind": "stock", "entity_id": "BE",
        "objective": "o", "mode": "targeted", "created_at": T0.isoformat(),
        "recipe_id": "general", "recipe_version": "v1",
        "questions": [{"question_id": "q1", "text": "订单？", "priority": "high",
                       "status": "answered", "module": "business_engine"}],
        "budgets": {"question_coverage_target": 0.8},
    })
    kb.assert_fact(Fact(entity_kind="stock", entity_id="BE", field="business_model",
                        value="燃料电池系统", knowledge_time=T0, evidence_ids=["ev-1"]))
    cons = ProfileConsolidator(kb=kb, metrics=metrics, events=events)
    return kb, metrics, events, writer, mw, cons, obs_id


def fake_snapshot(metrics, *, snapshot_id="snap-base", obs_ids=(), claim_ids=(),
                  data_hash="hash-base"):
    """最小合法快照 payload（save_snapshot 契约：entity/context/data_hash/inputs）。"""
    metrics.save_snapshot(snapshot_id=snapshot_id, namespace="prod", payload={
        "entity": {"kind": "stock", "id": "BE"},
        "context": {"mode": "frozen", "namespace": "prod",
                    "as_of": T1.isoformat(), "snapshot_id": snapshot_id,
                    "generated_at": T1.isoformat(), "projector_version": "test"},
        "data_hash": data_hash,
        "inputs": {
            "fact_ids": {}, "observation_ids": list(obs_ids),
            "claims": [{"claim_id": c} for c in claim_ids],
            "artifacts": [], "resolution_ids": [], "calculation_ids": [],
        },
    })


# ---------------- 依赖图 ----------------


class TestDependencyGraph:
    def test_edges_cover_full_chain(self, env):
        kb, metrics, events, writer, mw, cons, obs_id = env
        g = cons.dependency_graph("stock", "BE")
        kinds = {(e["from"], e["to"], e["kind"]) for e in g["edges"]}
        assert ("ev-1", obs_id, "evidence→observation") in kinds
        assert ("claim-dep", "module:business_engine", "claim→module") in kinds
        assert (obs_id, "claim-dep", "ref→claim") in kinds
        assert obs_id in g["nodes"]["observations"]


# ---------------- prepare：确定性只读预览 ----------------


class TestPrepare:
    def test_no_base_everything_new(self, env):
        kb, metrics, events, writer, mw, cons, obs_id = env
        out = cons.prepare_update("stock", "BE")
        assert out["base_snapshot"] is None
        assert out["merges"]["new_observations"] == [obs_id]
        assert "claim-dep" in out["merges"]["new_claims"]
        assert out["change_set_id"].startswith("chg-")
        assert out["expected_base_hash"]
        # prepare 不写任何状态
        assert metrics.invalidations_as_of("stock", "BE", NOW) == []

    def test_base_snapshot_scopes_merges(self, env):
        kb, metrics, events, writer, mw, cons, obs_id = env
        fake_snapshot(metrics, obs_ids=[obs_id], claim_ids=["claim-dep"])
        out = cons.prepare_update("stock", "BE")
        assert out["base_snapshot"]["snapshot_id"] == "snap-base"
        assert out["merges"]["new_observations"] == []
        assert out["merges"]["new_claims"] == []
        assert out["expected_diff"]["base_data_hash"] == "hash-base"

    def test_invalidated_observation_flags_dependents(self, env):
        """已作废观测的下游论断 = 待重审（§9.3 依赖失效的输入）。"""
        kb, metrics, events, writer, mw, cons, obs_id = env
        mw.revise_observation(obs_id, action="invalidated",
                              reason="口径错误（演示）", run=RUN)
        out = cons.prepare_update("stock", "BE")
        deps_ = out["invalidated_dependencies"]
        assert obs_id in deps_["observations"]
        stale = {d["ref"]: d for d in deps_["stale_dependents"]}
        assert "claim-dep" in stale
        assert stale["claim-dep"]["via_observation"] == obs_id
        assert stale["claim-dep"]["already_invalidated"] is False

    def test_candidate_refs_classified(self, env):
        kb, metrics, events, writer, mw, cons, obs_id = env
        out = cons.prepare_update("stock", "BE",
                                  candidate_refs=["ev-1", "ev-ghost", obs_id])
        assert out["candidates"]["resolved"] == ["ev-1", obs_id]
        assert out["candidates"]["unresolved"] == ["ev-ghost"]

    def test_unknown_base_snapshot_rejected(self, env):
        kb, metrics, events, writer, mw, cons, obs_id = env
        with pytest.raises(ConsolidationError, match="基线快照不存在"):
            cons.prepare_update("stock", "BE", base_snapshot_id="snap-ghost")


# ---------------- commit：幂等 + 基线校验 + 时态失效 ----------------


class TestCommit:
    def _prepare(self, env):
        kb, metrics, events, writer, mw, cons, obs_id = env
        return cons, metrics, events, obs_id

    def test_commit_requires_note_and_valid_change_set(self, env):
        cons, metrics, events, obs_id = self._prepare(env)
        out = cons.prepare_update("stock", "BE")
        with pytest.raises(ConsolidationError, match="note"):
            cons.commit_update("stock", "BE", change_set_id=out["change_set_id"],
                               expected_base_hash=out["expected_base_hash"], note="")
        with pytest.raises(ConsolidationError, match="change_set_id"):
            cons.commit_update("stock", "BE", change_set_id="",
                               expected_base_hash=out["expected_base_hash"], note="n")

    def test_stale_base_hash_rejected(self, env):
        """prepare 之后有并发研究写入（新观测）→ expected_base_hash 失效，
        拒绝提交（不拿旧基线盖新数据）；而 S2 自身写 thesis/裁决不触发过期。"""
        cons, metrics, events, obs_id = self._prepare(env)
        out = cons.prepare_update("stock", "BE")
        metrics.assert_observation(ReportedObservation(
            entity_kind="stock", entity_id="BE", metric_key="capex", period=FY2023,
            value="50000000", unit="USD", currency="USD",
            raw=RawValue(value_text="50 million", unit_text="USD", quote_ref="ev-1"),
            evidence_refs=["ev-1"], knowledge_time=T1, source_available_at=T1,
            retrieved_at=NOW, created_at=NOW, pit_grade=PitGrade.A,
        ))
        with pytest.raises(ConsolidationError, match="基线已变化"):
            cons.commit_update("stock", "BE", change_set_id=out["change_set_id"],
                               expected_base_hash=out["expected_base_hash"],
                               note="整合提交", manifest=RUN)

    def test_own_thesis_write_does_not_stale_base(self, env):
        """正常工作流 prepare → 写 thesis Fact/Claim → commit 不被自己触发过期。"""
        kb, metrics, events, writer, mw, cons, obs_id = env
        out = cons.prepare_update("stock", "BE")
        writer.write_fact(Fact(
            entity_kind="stock", entity_id="BE", field="thesis",
            value="整合后的论点", knowledge_time=datetime.now(UTC),
            evidence_ids=["ev-1"], run_id=RUN.run_id,
        ), run=RUN)
        committed = cons.commit_update(
            "stock", "BE", change_set_id=out["change_set_id"],
            expected_base_hash=out["expected_base_hash"],
            note="thesis 修订后提交", manifest=RUN,
        )
        assert committed["committed"] is True

    def test_commit_idempotent_replay(self, env):
        cons, metrics, events, obs_id = self._prepare(env)
        out = cons.prepare_update("stock", "BE")
        first = cons.commit_update(
            "stock", "BE", change_set_id=out["change_set_id"],
            expected_base_hash=out["expected_base_hash"],
            note="订单口径核实，thesis 更新", manifest=RUN,
            invalidate_claims=[{"claim_id": "claim-dep",
                                "reason": "依赖观测口径待复核",
                                "source_refs": [obs_id]}],
        )
        assert first["committed"] is True
        assert first["invalidations"][0]["claim_id"] == "claim-dep"
        replay = cons.commit_update(
            "stock", "BE", change_set_id=out["change_set_id"],
            expected_base_hash=out["expected_base_hash"], note="重放",
            invalidate_claims=[{"claim_id": "claim-dep", "reason": "重复"}],
        )
        assert replay["idempotent_replay"] is True
        # 失效记录只落一次（重放不重复追加）
        invalidations = metrics.invalidations_as_of("stock", "BE", datetime.now(UTC))
        assert len(invalidations) == 1
        evs = [e for e in events.read("r-cons") if e.type == "profile/update_committed"]
        assert len(evs) == 1 and evs[0].payload["note"] == "订单口径核实，thesis 更新"
        inval_evs = [e for e in events.read("r-cons") if e.type == "profile/claim_invalidated"]
        assert len(inval_evs) == 1

    def test_invalidation_is_temporal_old_view_unaffected(self, env):
        """失效按 invalidated_at 时态合并：早于失效时刻的历史视图看不到它（旧快照不变）。"""
        cons, metrics, events, obs_id = self._prepare(env)
        out = cons.prepare_update("stock", "BE")
        cons.commit_update(
            "stock", "BE", change_set_id=out["change_set_id"],
            expected_base_hash=out["expected_base_hash"], note="n",
            manifest=RUN,
            invalidate_claims=[{"claim_id": "claim-dep", "reason": "依赖已作废"}],
        )
        before = metrics.invalidations_as_of("stock", "BE", T0)
        after = metrics.invalidations_as_of("stock", "BE", datetime.now(UTC))
        assert before == [], "T0 时点（失效发生前）不得看到失效记录"
        assert len(after) == 1 and after[0]["reason"] == "依赖已作废"
        # 论断本身未被原位改写（append-only：payload 冻结历史不动）
        claim = metrics.get_claim("claim-dep")
        assert claim["status"] == "validated"

    def test_invalidate_cross_entity_rejected(self, env):
        cons, metrics, events, obs_id = self._prepare(env)
        metrics.save_claim(claim_id="claim-other", namespace="prod", payload={
            "claim_id": "claim-other", "entity_kind": "stock", "entity_id": "NVDA",
            "statement": "别家论断", "kind": "analysis", "status": "draft",
            "support_refs": [], "counter_refs": [], "limitations": [],
            "created_at": NOW.isoformat(), "namespace": "prod",
        })
        out = cons.prepare_update("stock", "BE")
        with pytest.raises(ConsolidationError, match="跨上下文失效拒绝"):
            cons.commit_update(
                "stock", "BE", change_set_id=out["change_set_id"],
                expected_base_hash=out["expected_base_hash"], note="n", manifest=RUN,
                invalidate_claims=[{"claim_id": "claim-other", "reason": "x"}],
            )

    def test_invalidate_requires_reason(self, env):
        cons, metrics, events, obs_id = self._prepare(env)
        out = cons.prepare_update("stock", "BE")
        with pytest.raises(ConsolidationError, match="reason"):
            cons.commit_update(
                "stock", "BE", change_set_id=out["change_set_id"],
                expected_base_hash=out["expected_base_hash"], note="n", manifest=RUN,
                invalidate_claims=[{"claim_id": "claim-dep"}],
            )


# ---------------- S2 真实装配 ----------------


class TestS2ConsolidationWiring:
    def test_profile_update_step_exposes_consolidator_tools(self, env, tmp_path):
        from finance_agent.commands.steps import StepContext, StepDeps, step_profile_update
        from finance_agent.decision.service import DecisionService
        from finance_agent.decision.store import DecisionStore
        from finance_agent.gateway.gateway import DataGateway
        from finance_agent.harness.approvals import ApprovalService
        from finance_agent.llm.base import AssistantReply, ToolCall
        from finance_agent.llm.mock import MockLLM

        kb, metrics, events, writer, mw, cons, obs_id = env
        script = [
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c0", name="prepare_profile_update", arguments={})]),
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c1", name="propose_thesis", arguments={
                    "thesis": "订单口径已核实，维持看好但需复核依赖。",
                    "evidence_ids": ["ev-1"],
                    "limitations": ["claim-dep 依赖观测待复核"],
                })]),
            # 关键论断核验（S2 与 S1 共用 verify_claim；惰性 LLM 拿到的是非 JSON
            # 脚本回复 → 内容审查不可用，诚实降级为只做硬检查）
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c1b", name="verify_claim",
                         arguments={"claim_id": "claim-dep"})]),
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c2", name="commit_profile_update", arguments={
                    "change_set_id": "__FILL__", "expected_base_hash": "__FILL__",
                    "note": "整合：thesis 修订 + 依赖论断标记待复核",
                    "invalidate_claims": [{"claim_id": "claim-dep",
                                           "reason": "依赖观测口径待复核"}],
                })]),
            AssistantReply(content="done"),
        ]
        # 用真实 prepare 结果填 commit 参数（模型在真实运行中读 prepare 响应获得）
        preview = cons.prepare_update("stock", "BE")
        script[3].tool_calls[0].arguments["change_set_id"] = preview["change_set_id"]
        script[3].tool_calls[0].arguments["expected_base_hash"] = preview[
            "expected_base_hash"]

        deps = StepDeps(
            events=events, kb=kb, writer=writer,
            gateway=DataGateway(mode="live", events=events, run_id="live-s2c"),
            decisions=DecisionService(kb=kb, decisions=DecisionStore(tmp_path / "d.db"),
                                      events=events),
            llm_for=lambda role: MockLLM(script),
            approvals=ApprovalService(events),
            evals_dir=tmp_path / "evals", reports_dir=tmp_path / "reports",
            knowledge_dir=tmp_path / "knowledge",
            metrics=metrics, metric_writer=mw,
        )
        ctx = StepContext(
            command_id="cmd-c", session_run_id="live-s2c",
            child_run_id="live-s2c--cmd-c-2", ticker="BE", objective="", config="",
            should_cancel=lambda: False, entity_kind="stock",
        )
        result = step_profile_update(deps, ctx)
        assert result.status == "completed"
        assert "thesis 已修订" in result.summary
        assert preview["change_set_id"] in result.summary  # 整合提交进摘要

        names = [e.payload["name"] for e in events.read(ctx.child_run_id)
                 if e.type == "tool/result"]
        assert "prepare_profile_update" in names and "commit_profile_update" in names
        assert "verify_claim" in names, "S2 与 S1 共用内容级核验工具"
        verified = metrics.get_claim("claim-dep")["verification"]
        assert verified["references_valid"] is True
        assert verified["evidence_support"] == "unchecked", \
            "内容审查不可用时诚实降级（不冒充已核验）"
        # 核验只改 verification 不改状态：claim 仍在，commit 基线哈希不受影响
        assert metrics.get_claim("claim-dep")["status"] == "validated"
        # commit 真实落库：提交台账 + 失效记录 + 事件
        committed = metrics.get_profile_update_commit(preview["change_set_id"])
        assert committed and committed["note"].startswith("整合：")
        assert len(metrics.invalidations_as_of("stock", "BE", datetime.now(UTC))) == 1
        assert [e for e in events.read(ctx.child_run_id)
                if e.type == "profile/update_committed"]

    def test_commit_rejection_visible_to_model(self, env, tmp_path):
        """基线过期的拒绝必须回到模型上下文（可修正重试），不静默失败。"""
        from finance_agent.commands.steps import StepContext, StepDeps, step_profile_update
        from finance_agent.decision.service import DecisionService
        from finance_agent.decision.store import DecisionStore
        from finance_agent.gateway.gateway import DataGateway
        from finance_agent.harness.approvals import ApprovalService
        from finance_agent.llm.base import AssistantReply, ToolCall
        from finance_agent.llm.mock import MockLLM

        kb, metrics, events, writer, mw, cons, obs_id = env
        preview = cons.prepare_update("stock", "BE")
        script = [
            AssistantReply(content="", tool_calls=[
                ToolCall(call_id="c0", name="commit_profile_update", arguments={
                    "change_set_id": preview["change_set_id"],
                    "expected_base_hash": "stale-hash-000",
                    "note": "拿旧基线提交",
                })]),
            AssistantReply(content="done"),
        ]
        deps = StepDeps(
            events=events, kb=kb, writer=writer,
            gateway=DataGateway(mode="live", events=events, run_id="live-s2r"),
            decisions=DecisionService(kb=kb, decisions=DecisionStore(tmp_path / "d2.db"),
                                      events=events),
            llm_for=lambda role: MockLLM(script),
            approvals=ApprovalService(events),
            evals_dir=tmp_path / "evals", reports_dir=tmp_path / "reports",
            knowledge_dir=tmp_path / "knowledge",
            metrics=metrics, metric_writer=mw,
        )
        ctx = StepContext(
            command_id="cmd-r", session_run_id="live-s2r",
            child_run_id="live-s2r--cmd-r-2", ticker="BE", objective="", config="",
            should_cancel=lambda: False, entity_kind="stock",
        )
        result = step_profile_update(deps, ctx)
        assert result.status == "completed"  # 拒绝不熔断 step（模型可见并可修正）
        tool_results = [e for e in events.read(ctx.child_run_id)
                        if e.type == "tool/result"
                        and e.payload.get("name") == "commit_profile_update"]
        assert tool_results
        assert tool_results[0].payload["content"].startswith("rejected:")
        assert "基线已变化" in tool_results[0].payload["content"]
        assert metrics.get_profile_update_commit(preview["change_set_id"]) is None
