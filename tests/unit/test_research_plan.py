"""研究计划与充分度评估验收（设计 §7/§13.1「研究」组）。

关键场景：档案 100% + 新目标仍建计划、模式预算、问题状态机门禁、
硬门禁失败不能被 rubric 覆盖、找不到数据产 partial、unavailable 需尝试记录。
"""

from datetime import UTC, datetime

import pytest

from finance_agent.research.assessment import assess, coverage_of
from finance_agent.research.plan import (
    MODE_BUDGETS,
    Budgets,
    ResearchPlan,
    ResearchQuestion,
    build_plan,
    list_recipes,
    load_recipe,
    select_recipe,
)

NOW = datetime(2025, 6, 1, tzinfo=UTC)


def q(qid, status="unanswered", priority="high", **kw):
    return ResearchQuestion(question_id=qid, text=f"问题 {qid}", priority=priority,
                            status=status, **kw)  # type: ignore[arg-type]


class TestRecipes:
    def test_first_batch_recipes_present(self):
        recipes = list_recipes()
        assert {"general", "industrial_equipment", "biotech", "industry"} <= set(recipes)

    def test_recipe_contract(self):
        r = load_recipe("industrial_equipment")
        keys = {k.key for k in r.kpis}
        assert {"orders", "firm_backlog", "capacity", "deliveries"} <= keys  # 行业 KPI
        assert r.models["reverse_dcf"]["applicable_when"] == "profitable_and_fcf_modelable"
        qids = {x.id for x in r.core_questions}
        assert "order-to-revenue" in qids  # 订单不直接等于收入

    def test_biotech_disables_generic_dcf(self):
        r = load_recipe("biotech")
        assert r.models["reverse_dcf"]["applicable_when"] == "disabled_for_pre_revenue"

    def test_unknown_recipe_fails_loud(self):
        with pytest.raises(FileNotFoundError):
            load_recipe("crypto_memecoin")

    def test_selection_uncertain_falls_back_general(self):
        rid, basis = select_recipe("stock", hint_text="a consumer brand")
        assert rid == "general" and "通用" in basis
        rid2, basis2 = select_recipe("stock", hint_text="fuel cell energy equipment")
        assert rid2 == "industrial_equipment" and "detection_hint" in basis2
        rid3, _ = select_recipe("stock", hint_text="", explicit="biotech")
        assert rid3 == "biotech"  # 显式选择优先（可更改、有来源）


class TestPlanBuilding:
    def test_mode_budgets_match_design_table(self):
        assert MODE_BUDGETS["standard"] == {
            "max_parallel_workers": 4, "retrieval_calls": 30,
            "wall_clock_minutes": 15, "max_rounds": 3,
        }
        assert MODE_BUDGETS["deep"]["retrieval_calls"] == 80
        assert MODE_BUDGETS["refresh"]["max_rounds"] == 2
        assert MODE_BUDGETS["targeted"]["retrieval_calls"] == 12

    def test_full_profile_with_new_objective_still_plans(self):
        """已有 100% 档案遇到新目标仍创建计划（不宣告「无需研究」，§7.1）。"""
        recipe = load_recipe("general")
        plan = build_plan(
            entity_kind="stock", entity_id="BE",
            objective="评估数据中心订单转化为收入的确定性",
            mode="standard", recipe=recipe,
            missing_fields=[], stale_fields=[],  # 档案满
        )
        assert plan.questions and plan.status == "active"
        assert all(qq.status == "unanswered" for qq in plan.questions)

    def test_deep_mode_question_range(self):
        recipe = load_recipe("general")
        plan = build_plan(entity_kind="stock", entity_id="X", objective="深研",
                          mode="deep", recipe=recipe)
        assert len(plan.questions) >= 12  # deep 12–18 个问题
        assert plan.budgets.max_rounds == 5

    def test_gap_fields_raise_priority(self):
        recipe = load_recipe("general")
        plan = build_plan(entity_kind="stock", entity_id="X", objective="研究",
                          mode="standard", recipe=recipe, missing_fields=["cash_flow"])
        fq = plan.question("financial-quality")
        assert fq is not None and fq.priority == "high"

    def test_targeted_focus_single_cluster(self):
        recipe = load_recipe("general")
        plan = build_plan(entity_kind="stock", entity_id="X", objective="backlog",
                          mode="targeted", recipe=recipe, focus="订单转化")
        assert 1 <= len(plan.questions) <= 4
        assert plan.questions[0].priority == "high"

    def test_plan_hash_stable_and_scope_frozen(self):
        recipe = load_recipe("general")
        p1 = build_plan(entity_kind="stock", entity_id="X", objective="研究",
                        mode="standard", recipe=recipe, now=NOW)
        p2 = build_plan(entity_kind="stock", entity_id="X", objective="研究",
                        mode="standard", recipe=recipe, now=NOW)
        assert p1.plan_hash() == p2.plan_hash()
        assert p1.frozen is True


class TestCoverageAndAssessment:
    def make_plan(self, questions):
        return ResearchPlan(
            plan_id="plan-t", entity_kind="stock", entity_id="BE", objective="t",
            questions=questions, budgets=Budgets(), created_at=NOW,
        )

    def test_unavailable_not_counted_as_answered(self):
        plan = self.make_plan([
            q("a", "answered", conclusion="…", support_refs=["ev-1"]),
            q("b", "unavailable", unresolved=["未披露"], attempts=["查了 10-K"]),
            q("c", "disputed", unresolved=["两源矛盾"], attempts=["交叉验证"]),
            q("d", "unanswered"),
        ])
        cov = coverage_of(plan)
        assert cov.answered == 1 and cov.applicable == 4
        assert cov.coverage == pytest.approx(0.25)
        assert any("d" in v for v in cov.violations)  # high 优先级未回答 = 违例

    def test_disputed_without_attempts_is_violation(self):
        plan = self.make_plan([q("a", "disputed")])  # 无原因与尝试记录
        cov = coverage_of(plan)
        assert cov.violations and "无原因/尝试记录" in cov.violations[0]

    def test_not_applicable_excluded_from_denominator(self):
        plan = self.make_plan([
            q("a", "answered", conclusion="…", support_refs=["ev-1"]),
            q("b", "not_applicable"),
        ])
        cov = coverage_of(plan)
        assert cov.applicable == 1 and cov.coverage == 1.0

    def test_integrity_failure_blocks_regardless_of_coverage(self):
        """硬门禁由代码运行：rubric/覆盖率再高也不能覆盖引用失败（§7.6）。"""
        from finance_agent.research.artifacts import ValidationIssue

        plan = self.make_plan([
            q("a", "answered", conclusion="…", support_refs=["ev-1"]),
        ])
        issues = [ValidationIssue(code="unresolved_evidence", ref="ev-x", message="引用不可解析")]
        a = assess(plan, claims=[], observations=[], calculations=[],
                   validation_issues=issues, stop_reason="converged", now=NOW)
        assert a.hard_gate_passed is False
        assert a.verdict == "blocked"

    def test_partial_when_coverage_below_target_with_outputs(self):
        plan = self.make_plan([
            q("a", "answered", conclusion="…", support_refs=["ev-1"]),
            q("b", "unanswered"),
            q("c", "unanswered"),
            q("d", "unanswered"),
            q("e", "unanswered"),
        ])
        claims = [{"claim_id": "c1", "kind": "inference", "status": "draft",
                   "support_refs": ["ev-1"], "counter_refs": []}]
        a = assess(plan, claims=claims, observations=[], calculations=[],
                   stop_reason="budget", now=NOW)
        # 覆盖不足但有有效成果 → partial，不叫「充分完成」
        assert a.verdict == "partial"
        assert any("budget" in n for n in a.notes)  # 预算终止不是基本面结论

    def test_sufficient_requires_coverage_and_clean_gate(self):
        qs = [q(f"q{i}", "answered", conclusion="…", support_refs=["ev-1"]) for i in range(5)]
        plan = self.make_plan(qs)
        a = assess(plan, claims=[], observations=[], calculations=[],
                   stop_reason="converged", now=NOW)
        assert a.verdict == "sufficient" and a.hard_gate_passed

    def test_open_conflicts_downgrade_unconditional_conclusion(self):
        qs = [q(f"q{i}", "answered", conclusion="…", support_refs=["ev-1"]) for i in range(5)]
        plan = self.make_plan(qs)
        a = assess(plan, claims=[], observations=[], calculations=[],
                   open_conflicts=2, stop_reason="converged", now=NOW)
        assert a.verdict == "partial"  # 重大矛盾未裁决 → 不输出无条件结论
        assert any("冲突" in n for n in a.notes)

    def test_blocked_when_nothing_produced(self):
        plan = self.make_plan([q("a", "unanswered")])
        a = assess(plan, claims=[], observations=[], calculations=[],
                   stop_reason="stalled", now=NOW)
        assert a.verdict == "blocked"
