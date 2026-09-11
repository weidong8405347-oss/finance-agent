"""哨兵基线 A 发现的第二轮整改回归（docs/sentinel-baseline-A-2026-09-10.md §4 F1-F9）。

F1 focus 编译：standard/deep 的 --focus 编译为高优先专门问题（基线事故：BE
   --focus=订单口径 的计划仍是标准 12 题，哨兵核心场景没被研究）；
F2 数值题 typed 依据：expects_typed_evidence 编译期冻结，answered 无 obs-/calc-
   引用即拒（基线事故：BE financial 组 0 次 propose_metric，数字全留在文本里）；
F3 unit↔currency 一致性（基线事故：2228 观测 unit=USD + currency=CNY 并存）；
F5 交题欠账提醒（基线事故：2228 deep 只交 3/12）；
F6 行业级 typed 示例进 brief（基线事故：行业 typed 观测为 0）；
F7 披露域细化一手归类（基线事故：sec.gov 直拉新闻稿被归 secondary）；
F8 反证沉淀纪律（基线事故：BE 反证答案完整但 counter_evidence_claims=0）；
F9 targeted 检索预算 12→20（基线实测打满）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from finance_agent.knowledge.metric_spec import check_observation
from finance_agent.research.plan import (
    MODE_BUDGETS,
    Recipe,
    RecipeQuestion,
    build_plan,
    text_expects_typed,
)
from finance_agent.research.prompts import build_plan_brief

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def recipe() -> Recipe:
    return Recipe(
        id="test-recipe", version="1", entity_kind="stock",
        core_questions=[
            RecipeQuestion(id="financial-quality", text="利润是否兑现为现金？",
                           priority="high", module="financial_quality",
                           computable_checks=["fcf_from_cfo"]),
            RecipeQuestion(id="business-model", text="谁付钱、为什么付钱？",
                           priority="high", module="business_engine"),
            RecipeQuestion(id="counter-evidence", text="最强的反对理由是什么？",
                           priority="high", module="risks"),
        ],
        extended_questions=[
            RecipeQuestion(id="valuation-assumptions", text="当前价格隐含什么假设？",
                           priority="medium", module="valuation"),
        ],
    )


# ---------------- F1 focus 编译 ----------------


class TestFocusCompilation:
    def test_deep_focus_becomes_dedicated_question(self):
        plan = build_plan(
            entity_kind="stock", entity_id="BE", objective="深度研究 BE",
            mode="deep", recipe=recipe(), focus="订单口径与收入确认", now=NOW,
        )
        first = plan.questions[0]
        assert first.question_id.startswith("focus-")
        assert first.text == "订单口径与收入确认"
        assert first.priority == "high"
        assert plan.scope["focus_question_id"] == first.question_id
        assert "focus 题" in plan.acceptance

    def test_standard_focus_also_compiled(self):
        plan = build_plan(
            entity_kind="stock", entity_id="BE", objective="研究 BE",
            mode="standard", recipe=recipe(), focus="管理层兑现记录", now=NOW,
        )
        assert any(q.question_id.startswith("focus-") for q in plan.questions)

    def test_focus_deterministic_id(self):
        kw = dict(entity_kind="stock", entity_id="BE", objective="o", mode="deep",
                  recipe=recipe(), focus="订单口径", now=NOW)
        a = build_plan(**kw)
        b = build_plan(**kw)
        assert a.questions[0].question_id == b.questions[0].question_id

    def test_no_focus_no_question(self):
        plan = build_plan(
            entity_kind="stock", entity_id="BE", objective="深度研究 BE",
            mode="deep", recipe=recipe(), now=NOW,
        )
        assert not any(q.question_id.startswith("focus-") for q in plan.questions)
        assert plan.scope["focus_question_id"] is None

    def test_targeted_mode_unchanged(self):
        plan = build_plan(
            entity_kind="stock", entity_id="BE", objective="o",
            mode="targeted", recipe=recipe(), focus="订单口径与收入确认", now=NOW,
        )
        assert plan.questions[0].question_id.startswith("targeted-")
        assert not any(q.question_id.startswith("focus-") for q in plan.questions)

    def test_focus_survives_truncation(self):
        """focus 题排最前且 high——问题数超上限截断时不被背景题挤掉。"""
        big = Recipe(
            id="big", version="1",
            core_questions=[
                RecipeQuestion(id=f"q{i}", text=f"问题{i}", priority="medium", module="risks")
                for i in range(30)
            ],
        )
        plan = build_plan(
            entity_kind="stock", entity_id="BE", objective="o",
            mode="standard", recipe=big, focus="订单口径", now=NOW,
        )
        assert plan.questions[0].question_id.startswith("focus-")


# ---------------- F2 数值题 typed 依据 ----------------


class TestExpectsTypedCompilation:
    def test_numeric_module_question_flagged(self):
        plan = build_plan(entity_kind="stock", entity_id="BE", objective="o",
                          mode="deep", recipe=recipe(), now=NOW)
        by_id = {q.question_id: q for q in plan.questions}
        assert by_id["financial-quality"].expects_typed_evidence is True
        assert by_id["valuation-assumptions"].expects_typed_evidence is True
        assert by_id["business-model"].expects_typed_evidence is False

    def test_focus_text_heuristic(self):
        assert text_expects_typed("订单口径与收入确认") is True
        assert text_expects_typed("最近两个季度公司指引与实际披露值对比") is True
        assert text_expects_typed("revenue recognition policy") is True
        assert text_expects_typed("护城河与竞争格局") is False
        assert text_expects_typed("管理层背景") is False

    def test_targeted_numeric_focus_flagged(self):
        plan = build_plan(entity_kind="stock", entity_id="NVDA", objective="o",
                          mode="targeted", recipe=recipe(),
                          focus="最近两个季度收入指引与实际值", now=NOW)
        assert plan.questions[0].expects_typed_evidence is True


class TestAnswerQuestionTypedGate:
    """answered 门禁：数值题必须带 obs-/calc- 引用（工具层硬规则）。"""

    @pytest.fixture()
    def env(self, tmp_path):
        from finance_agent.eventstore.store import EventStore
        from finance_agent.harness.manifest import RunManifest, RunMode
        from finance_agent.knowledge.metric_store import MetricStore
        from finance_agent.knowledge.metric_writer import TypedMetricWriter
        from finance_agent.knowledge.models import Evidence, PitGrade
        from finance_agent.knowledge.store import BitemporalStore
        from finance_agent.knowledge.writer import ProfileWriter
        from finance_agent.research.evidence_desk import ChunkStore
        from finance_agent.research.tools import make_research_tools

        kb = BitemporalStore(tmp_path / "kb.db")
        metrics = MetricStore(tmp_path / "m.db")
        events = EventStore(tmp_path / "e.db")
        writer = ProfileWriter(store=kb, events=events)
        mw = TypedMetricWriter(store=metrics, kb=kb, events=events)
        kb.add_evidence(Evidence(
            evidence_id="ev-num", source_id="edgar", verbatim_quote="revenue 100 million",
            retrieved_at=NOW, available_at=NOW, pit_grade=PitGrade.A,
        ))
        # 一条可引用的 typed 观测
        from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation
        oid, _ = metrics.assert_observation(ReportedObservation(
            entity_kind="stock", entity_id="BE", metric_key="revenue",
            period=MetricPeriod(start=datetime(2025, 1, 1, tzinfo=UTC).date(),
                                end=datetime(2025, 12, 31, tzinfo=UTC).date(),
                                frequency="FY", fiscal_label="FY2025"),
            value="100000000", unit="USD", currency="USD",
            raw=RawValue(value_text="100 million", quote_ref="ev-num"),
            evidence_refs=["ev-num"], knowledge_time=NOW, retrieved_at=NOW, created_at=NOW,
            pit_grade=PitGrade.A,
        ))
        plan = build_plan(entity_kind="stock", entity_id="BE", objective="o",
                          mode="targeted", recipe=recipe(), focus="收入口径确认", now=NOW)
        metrics.save_plan(plan_id=plan.plan_id, namespace="prod",
                          payload=plan.model_dump(mode="json"))

        def make_tools(plan_id):
            return make_research_tools(
                store=kb, writer=writer,
                manifest=RunManifest(run_id="live-g", mode=RunMode.LIVE),
                entity_kind="stock", entity_id="BE", chunk_store=ChunkStore(),
                events=events, metrics=metrics, metric_writer=mw, plan_id=plan_id,
            )

        tools, tracker = make_tools(plan.plan_id)
        return {"tools": tools, "tracker": tracker, "plan": plan, "oid": oid,
                "metrics": metrics, "make_tools": make_tools}

    def test_answered_without_typed_ref_rejected(self, env):
        tools, tracker, plan = env["tools"], env["tracker"], env["plan"]
        qid = plan.questions[0].question_id
        assert plan.questions[0].expects_typed_evidence
        out = tools["answer_question"]({
            "question_id": qid, "status": "answered",
            "conclusion": "收入 100 million", "support_refs": ["ev-num"],
        })
        assert out["content"].startswith("rejected:")
        assert "obs-/calc-" in out["content"] and "propose_metric" in out["content"]
        assert tracker.answer_rejections

    def test_answered_with_observation_ref_accepted(self, env):
        tools, plan, oid = env["tools"], env["plan"], env["oid"]
        out = tools["answer_question"]({
            "question_id": plan.questions[0].question_id, "status": "answered",
            "conclusion": "收入 100 million（FY2025）", "support_refs": ["ev-num", oid],
        })
        assert not out["content"].startswith("rejected:"), out["content"]

    def test_unavailable_escape_hatch_intact(self, env):
        """数字确实无法结构化：disputed/unavailable 出口不被 typed 门禁堵死。"""
        tools, plan = env["tools"], env["plan"]
        out = tools["answer_question"]({
            "question_id": plan.questions[0].question_id, "status": "unavailable",
            "unresolved": ["年报扫描件乱码，无法结构化"],
            "attempts": ["读 3 份 PDF 均 garbled"],
        })
        assert not out["content"].startswith("rejected:"), out["content"]

    def test_non_numeric_question_unchanged(self, env):
        """非数值题行为不变：ev- 引用即可 answered（门禁面不扩大）。"""
        plan2 = build_plan(entity_kind="stock", entity_id="BE", objective="o",
                           mode="targeted", recipe=recipe(),
                           focus="护城河与竞争格局", now=NOW)
        assert plan2.questions[0].expects_typed_evidence is False
        env["metrics"].save_plan(plan_id=plan2.plan_id, namespace="prod",
                                 payload=plan2.model_dump(mode="json"))
        tools2, _ = env["make_tools"](plan2.plan_id)
        out = tools2["answer_question"]({
            "question_id": plan2.questions[0].question_id, "status": "answered",
            "conclusion": "护城河来自专利与转换成本", "support_refs": ["ev-num"],
        })
        assert not out["content"].startswith("rejected:"), out["content"]


# ---------------- F3 unit↔currency 一致性 ----------------


class TestUnitCurrencyConsistency:
    def test_conflict_rejected(self):
        try:
            check_observation(
                metric_key="revenue", unit="USD", currency="CNY",
                subject_kind="stock", frequency="FY", dimensions={},
                value="802623", quotes=["收入 802,623 千元"],
                locator={"page": "6"},
            )
            raise AssertionError("unit=USD + currency=CNY 必须被拒")
        except Exception as e:
            assert "冲突" in str(e) and "unit_text" in str(e)

    def test_consistent_passes(self):
        check_observation(
            metric_key="revenue", unit="CNY", currency="CNY",
            subject_kind="stock", frequency="FY", dimensions={},
            value="802623000", quotes=["收入 802,623 千元"],
            locator={"page": "6"},
        )


# ---------------- F5/F6/F8 brief 投影 ----------------


class TestBriefProjections:
    def _payload(self, entity_kind="stock", statuses=("unanswered",)):
        plan = build_plan(entity_kind=entity_kind,
                          entity_id="ai4s" if entity_kind == "industry" else "BE",
                          objective="o", mode="deep",
                          recipe=recipe() if entity_kind == "stock" else Recipe(
                              id="ind", version="1", entity_kind="industry",
                              core_questions=[RecipeQuestion(
                                  id="market-scale", text="赛道规模多大？",
                                  priority="high", module="market_size")],
                          ),
                          now=NOW)
        payload = plan.model_dump(mode="json")
        for q in payload["questions"]:
            q["status"] = statuses[0]
        return payload

    def test_round2_shows_submission_debt(self):
        brief = build_plan_brief(self._payload(), round_no=2)
        assert "未交答案" in brief and "交题优先于新检索" in brief

    def test_round1_no_debt_note(self):
        assert "未交答案" not in build_plan_brief(self._payload(), round_no=1)

    def test_answered_questions_no_debt_note(self):
        brief = build_plan_brief(self._payload(statuses=("answered",)), round_no=3)
        assert "未交答案" not in brief

    def test_typed_marker_on_numeric_questions(self):
        brief = build_plan_brief(self._payload())
        assert "数值题" in brief and "obs-/calc-" in brief

    def test_industry_typed_examples(self):
        brief = build_plan_brief(self._payload(entity_kind="industry"))
        assert "market_size" in brief and "growth_rate" in brief and "capacity_supply" in brief
        assert "RMB thousands" in brief  # 反例警示也在

    def test_stock_brief_has_no_industry_examples(self):
        assert "capacity_supply" not in build_plan_brief(self._payload())

    def test_counter_evidence_discipline(self):
        brief = build_plan_brief(self._payload())
        assert "counter_refs" in brief and "propose_claim" in brief


# ---------------- F7 披露域细化 ----------------


class TestIssuerDomainClassification:
    def _obs(self, refs):
        from dataclasses import dataclass, field

        @dataclass
        class _O:
            nature: str = "reported"
            pit_grade: str = "B"
            evidence_refs: list = field(default_factory=list)
            raw: object | None = None
            value: str | None = None
            metric_key: str = "revenue"

        return _O(evidence_refs=refs)

    def test_sec_gov_direct_fetch_is_first_party(self):
        from finance_agent.research.assessment import classify_observation_source

        bucket = classify_observation_source(
            self._obs(["ev-1"]), {"ev-1": "web_fetch"},
            {"ev-1": "https://www.sec.gov/Archives/edgar/data/1045810/x/q1fy27pr.htm"},
        )
        assert bucket == "first_party"

    def test_hkexnews_domain_first_party(self):
        from finance_agent.research.assessment import classify_observation_source

        bucket = classify_observation_source(
            self._obs(["ev-1"]), {"ev-1": "web_fetch"},
            {"ev-1": "https://www1.hkexnews.hk/listedco/x.pdf"},
        )
        assert bucket == "first_party"

    def test_non_issuer_domain_not_upgraded(self):
        from finance_agent.research.assessment import classify_observation_source

        bucket = classify_observation_source(
            self._obs(["ev-1"]), {"ev-1": "web_fetch"},
            {"ev-1": "https://news.example.com/nvda"},
        )
        assert bucket == "secondary"

    def test_media_source_not_upgraded_by_url(self):
        """媒体源转载的 sec.gov 链接不升档（转载族归并属 SearchBroker 后续）。"""
        from finance_agent.research.assessment import classify_observation_source

        bucket = classify_observation_source(
            self._obs(["ev-1"]), {"ev-1": "web_search"},
            {"ev-1": "https://www.sec.gov/Archives/x.htm"},
        )
        assert bucket == "secondary"


# ---------------- F9 预算 ----------------


def test_targeted_retrieval_budget_raised():
    assert MODE_BUDGETS["targeted"]["retrieval_calls"] == 20
    assert MODE_BUDGETS["targeted"]["wall_clock_minutes"] == 10  # 墙钟不变
