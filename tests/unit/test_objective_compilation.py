"""目标编译验收（audit §3.4 P1）。

事故形态：用户问「究竟哪些公司是真的在形成技术护城河以及有比较大可能能够取得
商业爆发」，standard/deep 直接套配方 core/extended 问题，objective 只是存储字段——
九个题目主要是产业链、供需、政策、周期，答完它们也证明不了回答了用户的问题。

判据：用户目标对应明确的高优先级问题、候选比较产物与首屏结论；
背景问题完成不能代替目标完成。
"""

from datetime import UTC, datetime

from finance_agent.research.objective import (
    BREAKOUT_DIMENSION,
    OBJECTIVE_DIMENSIONS,
    build_objective_questions,
    objective_dimension_keys,
    wants_company_comparison,
)
from finance_agent.research.plan import build_plan, load_recipe

NOW = datetime(2024, 6, 1, tzinfo=UTC)

#: 本次事故的真实命令目标
INCIDENT_OBJECTIVE = "究竟哪些公司是真的在形成技术护城河以及有比较大可能能够取得商业爆发"


class TestObjectiveDetection:
    def test_incident_objective_requires_company_comparison(self):
        wanted, basis = wants_company_comparison(INCIDENT_OBJECTIVE, "industry")
        assert wanted
        assert "护城河" in basis or "商业爆发" in basis

    def test_english_objectives_detected(self):
        assert wants_company_comparison(
            "Which companies have a real moat in AI for science?", "industry")[0]
        assert wants_company_comparison("best positioned players and leaders", "industry")[0]

    def test_pure_background_objective_keeps_recipe_path(self):
        """只问行业背景 → 不强行加公司比较题（不扩大范围）。"""
        assert not wants_company_comparison("这个行业整体规模有多大、增速如何", "industry")[0]

    def test_stock_entity_needs_strong_signal(self):
        assert wants_company_comparison("这家公司护城河到底有多深", "stock")[0]
        # 单公司实体上的弱信号（候选/龙头）不强加比较交付，沿用配方问题
        assert not wants_company_comparison("它的同行候选有哪些", "stock")[0]

    def test_five_dimensions_are_all_present(self):
        keys = {d.key for d in OBJECTIVE_DIMENSIONS}
        assert keys == {"technology_moat", "commercial_proof", "sustainability",
                        "counter_evidence", "investability"}
        assert BREAKOUT_DIMENSION.key == "commercial_breakout"


class TestObjectiveQuestions:
    def test_questions_embed_the_user_objective_verbatim(self):
        qs = build_objective_questions(INCIDENT_OBJECTIVE, "industry")
        assert len(qs) >= 5
        for q in qs:
            assert INCIDENT_OBJECTIVE in q.text, "问题必须携带用户目标原文（可追溯）"
            assert q.priority == "high"
            assert q.acceptance

    def test_breakout_question_forbids_fabricated_probability(self):
        qs = build_objective_questions(INCIDENT_OBJECTIVE, "industry")
        breakout = [q for q in qs if "commercial_breakout" in q.id]
        assert breakout, "目标含「商业爆发」时必须追加爆发条件问题"
        acc = breakout[0].acceptance
        assert "概率百分比" in acc and "评分" in acc
        assert "阶段" in acc and "触发条件" in acc

    def test_no_breakout_question_without_signal(self):
        qs = build_objective_questions("哪些公司有护城河", "industry")
        assert not [q for q in qs if "commercial_breakout" in q.id]

    def test_investability_separates_listed_from_private(self):
        qs = build_objective_questions(INCIDENT_OBJECTIVE, "industry")
        inv = next(q for q in qs if "investability" in q.id)
        assert "上市状态" in inv.text and "未上市" in inv.text
        assert "待核实" in inv.acceptance

    def test_modules_route_to_industry_groups(self):
        qs = build_objective_questions(INCIDENT_OBJECTIVE, "industry")
        modules = {q.module for q in qs}
        assert modules <= {"candidate_pool", "key_kpi", "catalysts_risks"}
        # 公司实体走另一套模块（不套用行业口径）
        stock_qs = build_objective_questions("这家公司护城河有多深", "stock")
        assert {q.module for q in stock_qs} <= {"business_engine", "financial_quality",
                                                "risks", "peers"}

    def test_ids_are_deterministic(self):
        a = [q.id for q in build_objective_questions(INCIDENT_OBJECTIVE, "industry")]
        b = [q.id for q in build_objective_questions(INCIDENT_OBJECTIVE, "industry")]
        assert a == b
        assert objective_dimension_keys(INCIDENT_OBJECTIVE, "industry")


class TestPlanCompilation:
    def _plan(self, objective, *, mode="deep", kind="industry", recipe="industry"):
        return build_plan(
            entity_kind=kind, entity_id="ai-for-science", objective=objective,
            mode=mode, recipe=load_recipe(recipe), now=NOW, run_id="r1",
        )

    def test_deep_industry_plan_starts_with_objective_questions(self):
        plan = self._plan(INCIDENT_OBJECTIVE)
        ids = [q.question_id for q in plan.questions]
        objective_ids = plan.scope["objective_question_ids"]
        assert objective_ids, "目标未被编译成问题（objective 只是存储字段）"
        # 目标题排在最前（背景题让位）
        assert ids[:len(objective_ids)] == objective_ids
        assert all(plan.question(q).priority == "high" for q in objective_ids)
        assert "行业" in plan.scope["objective_decomposition"] or \
               "信号" in plan.scope["objective_decomposition"]

    def test_acceptance_states_background_cannot_substitute(self):
        plan = self._plan(INCIDENT_OBJECTIVE)
        assert "背景题完成不能代替目标完成" in plan.acceptance
        assert "用户目标必须被直接回答" in plan.acceptance

    def test_standard_mode_truncation_keeps_objective_questions(self):
        plan = self._plan(INCIDENT_OBJECTIVE, mode="standard")
        objective_ids = set(plan.scope["objective_question_ids"])
        present = {q.question_id for q in plan.questions}
        assert objective_ids <= present, "截断把目标题裁掉了（背景题应先让位）"
        assert len(plan.questions) <= 10

    def test_recipe_questions_still_present_as_background(self):
        plan = self._plan(INCIDENT_OBJECTIVE)
        ids = {q.question_id for q in plan.questions}
        assert "value-chain" in ids and "candidate-pool" in ids

    def test_background_only_objective_leaves_recipe_plan_unchanged(self):
        plan = self._plan("这个行业整体规模有多大、增速如何")
        assert plan.scope["objective_question_ids"] == []
        assert {q.question_id for q in plan.questions} >= {"value-chain", "demand-supply"}

    def test_targeted_mode_not_affected(self):
        plan = build_plan(
            entity_kind="industry", entity_id="ai-for-science", objective=INCIDENT_OBJECTIVE,
            mode="targeted", recipe=load_recipe("industry"), focus="护城河", now=NOW,
        )
        assert plan.scope["objective_question_ids"] == []
        assert plan.questions[0].question_id.startswith("targeted-")
