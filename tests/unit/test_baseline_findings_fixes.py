"""哨兵基线 A 试跑发现的整改回归（2026-09-10，data/sentinel/baseline-A-r4）。

四个真实失败形态的修复判据：
1. ai4s 全漏斗 blocked 根因一：growth_rate 行业主体被语义闸全拒（F1 两轮零写入）
   ——行业增速是合法的行业级指标；
2. ai4s 根因二：F1 停滞判定只看字段完整度（0%）——问题已答（1/1 sufficient）仍拦停
   整个 /industry；与 step_research 同口径：有有效产出就不算一无所获；
3. 过时停滞建议（"待接入 HKEXnews/Exa"——两者早已装配）把排查引向不存在的缺口；
4. 'RMB thousands' 类自造单位连拒 4 次——拒绝必须给正向修法（规范 unit + unit_text
   承载原文量表），不只给允许集合。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from finance_agent.commands.steps import _industry_map_stall_blocked
from finance_agent.knowledge.metric_spec import check_observation
from finance_agent.research.loop import _stall_suggestions

NOW = datetime(2026, 9, 10, tzinfo=UTC)


class TestGrowthRateIndustrySubject:
    def test_industry_growth_rate_accepted(self):
        """行业增速写行业实体是合法语义（基线事故：4 连拒导致 F1 零写入 stalled）。"""
        check_observation(
            metric_key="growth_rate", unit="percent", currency=None,
            subject_kind="industry",
            frequency="FY", dimensions={}, value="35.2",
            quotes=["行业收入增速 35.2%"], locator={},
        )  # 不抛 = 通过

    def test_company_margin_on_industry_still_rejected(self):
        """放开的只有行业级增速；公司财务指标写行业实体仍然拒绝（纵深不破）。"""
        try:
            check_observation(
                metric_key="gross_margin", unit="percent", currency=None,
                subject_kind="industry",
                frequency="FY", dimensions={}, value="35",
                quotes=["gross margin 35%"], locator={},
            )
            raise AssertionError("gross_margin 行业主体应被拒")
        except Exception as e:
            assert "主体类别" in str(e)


class TestIndustryMapStallGate:
    def _gaps(self, completeness: float):
        return SimpleNamespace(completeness=completeness, missing=["market_size"])

    def _assessment(self, answered: int, observations: int = 0, claims: int = 0):
        return SimpleNamespace(
            question_coverage=SimpleNamespace(answered=answered, applicable=1),
            evidence_quality={"observations": observations,
                              "validated_claims": claims, "draft_claims": 0},
        )

    def test_answered_questions_prevent_block(self):
        """基线事故形态：字段 0% 但问题 1/1 已答 → 不得拦停漏斗。"""
        loop = SimpleNamespace(assessment=self._assessment(answered=1))
        assert _industry_map_stall_blocked(loop, self._gaps(0.0)) is False

    def test_typed_output_prevents_block(self):
        loop = SimpleNamespace(assessment=self._assessment(answered=0, observations=3))
        assert _industry_map_stall_blocked(loop, self._gaps(0.0)) is False

    def test_true_zero_output_blocks(self):
        """一无所获（无问题推进、无 typed 产出、字段 0%）→ 仍然拦停（旧防线不破）。"""
        loop = SimpleNamespace(assessment=self._assessment(answered=0))
        assert _industry_map_stall_blocked(loop, self._gaps(0.0)) is True

    def test_no_assessment_falls_back_to_completeness(self):
        loop = SimpleNamespace(assessment=None)
        assert _industry_map_stall_blocked(loop, self._gaps(0.0)) is True
        assert _industry_map_stall_blocked(loop, self._gaps(0.4)) is False


class TestStallSuggestionsCurrent:
    def test_hk_with_source_gives_garbled_advice(self):
        sugg = _stall_suggestions("2228.HK", ["moat"], ["hkex_news", "web_search"])
        joined = "；".join(sugg)
        assert "garbled" in joined and "英文版" in joined
        assert "待接入" not in joined, "hkex_news 已装配，不得再建议「待接入」"

    def test_hk_without_source_flags_assembly(self):
        sugg = _stall_suggestions("2228.HK", [], ["edgar"])
        assert any("hkex_news 源未装配" in s for s in sugg)

    def test_qualitative_with_web_search_gives_tool_advice(self):
        sugg = _stall_suggestions("BE", ["moat", "risks"], ["web_search", "edgar"])
        joined = "；".join(sugg)
        assert "read_document" in joined or "文档精读" in joined
        assert "待接入 web 搜索源" not in joined

    def test_qualitative_without_web_search_flags_keys(self):
        sugg = _stall_suggestions("BE", ["moat"], ["edgar"])
        assert any("web 搜索源未装配" in s for s in sugg)


class TestUnitRejectionHint:
    def test_bad_unit_message_carries_positive_fix(self):
        """'RMB thousands' 连拒 4 次（基线）：拒绝消息必须给正向修法示例。"""
        try:
            check_observation(
                metric_key="revenue", unit="RMB thousands", currency="CNY",
                subject_kind="stock",
                frequency="H1", dimensions={}, value="393627",
                quotes=["收入 393,627 千元"], locator={"page": "85"},
            )
            raise AssertionError("自造单位应被拒")
        except Exception as e:
            msg = str(e)
            assert "RMB thousands" in msg
            assert "unit_text" in msg and "修法" in msg, "必须给正向修法（规范 unit + unit_text）"
