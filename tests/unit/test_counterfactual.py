"""反事实扰动探针（PC/CI/IDS）：决策不随证据变化 = 在背答案（FinLeak 方法移植）。"""

from datetime import UTC, datetime

from finance_agent.evaluation.counterfactual import CounterfactualProbe, ScaleField
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.store import BitemporalStore

NOW = datetime(2024, 6, 1, tzinfo=UTC)


def seed(tmp_path, revenue=100):
    kb = BitemporalStore(tmp_path / "kb.db")
    kb.add_evidence(
        Evidence(
            evidence_id="ev-1",
            source_id="edgar",
            verbatim_quote=f"Total revenue {revenue}",
            retrieved_at=NOW,
            available_at=datetime(2024, 3, 1, tzinfo=UTC),
            pit_grade=PitGrade.A,
        )
    )
    kb.assert_fact(
        Fact(
            entity_kind="stock",
            entity_id="AAA",
            field="revenue_fy",
            value=revenue,
            knowledge_time=datetime(2024, 3, 1, tzinfo=UTC),
            evidence_ids=["ev-1"],
        )
    )
    return kb


class RuleDecider:
    """按档案内容决策（真在用输入）：营收 > 50 → buy 4，否则 sell 2。"""

    def __call__(self, profile: dict) -> dict:
        revenue = profile.get("revenue_fy", {}).get("value", 0)
        if isinstance(revenue, (int, float)) and revenue > 50:
            return {"action": "buy", "conviction": 4}
        return {"action": "sell", "conviction": 2}


class MemorizedDecider:
    """无视输入（在背答案）：永远 buy 4。"""

    def __call__(self, profile: dict) -> dict:
        return {"action": "buy", "conviction": 4}


def test_counterfactual_detects_input_driven_decision(tmp_path):
    kb = seed(tmp_path)
    probe = CounterfactualProbe(
        kb, entity_kind="stock", entity_id="AAA", as_of=NOW, namespace="prod"
    )
    result = probe.run(
        RuleDecider(),
        perturbations=[ScaleField("revenue_fy", 0.1), ScaleField("revenue_fy", 0.01)],
    )
    # 两次扰动都把营收打到阈值以下 → 决策都变了
    assert result.pc == 0.0
    assert result.ci < 1.0
    assert result.ids > 0.0
    assert result.trials == 2


def test_counterfactual_detects_memorized_decision(tmp_path):
    kb = seed(tmp_path)
    probe = CounterfactualProbe(
        kb, entity_kind="stock", entity_id="AAA", as_of=NOW, namespace="prod"
    )
    result = probe.run(
        MemorizedDecider(),
        perturbations=[ScaleField("revenue_fy", 0.1), ScaleField("revenue_fy", 0.01)],
    )
    assert result.pc == 1.0  # 决策完全不变 → 高泄漏嫌疑
    assert result.ci == 1.0
    assert result.ids == 0.0
