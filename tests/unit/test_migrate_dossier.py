"""迁移脚本验收（设计 §11.1/§13.1「兼容迁移」组）：dry-run 只读、
高置信映射保守（不猜单位/期间/币种）、幂等与断点恢复、影子快照对账。
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from migrate_dossier import classify_fact, main  # noqa: E402

from finance_agent.knowledge.models import Evidence, Fact, PitGrade  # noqa: E402
from finance_agent.knowledge.store import BitemporalStore  # noqa: E402

NOW = datetime.now(UTC)


@pytest.fixture()
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    kb = BitemporalStore(d / "kb.db")
    kb.add_evidence(Evidence(
        evidence_id="ev-m1", source_id="stooq", verbatim_quote="close 119.51 on 2026-03-30",
        retrieved_at=NOW, available_at=NOW, pit_grade=PitGrade.A,
    ))
    kb.assert_fact(Fact(
        entity_kind="stock", entity_id="BE", field="market_cap_snapshot",
        value={"close": 119.51, "currency": "USD", "trading_date": "2026-03-30"},
        knowledge_time=NOW, evidence_ids=["ev-m1"],
    ))
    kb.add_evidence(Evidence(
        evidence_id="ev-m2", source_id="edgar", verbatim_quote="revenue fy2025 1,364",
        retrieved_at=NOW, available_at=NOW, pit_grade=PitGrade.A,
    ))
    kb.assert_fact(Fact(
        entity_kind="stock", entity_id="BE", field="revenue_fy2025",
        value="1,364", knowledge_time=NOW, evidence_ids=["ev-m2"],
    ))
    kb.assert_fact(Fact(
        entity_kind="stock", entity_id="BE", field="business_model",
        value="sells fuel cell systems and services with long-term agreements",
        knowledge_time=NOW, evidence_ids=["ev-m2"],
    ))
    kb.close()
    return d


class TestClassify:
    def test_structured_snapshot_high_confidence(self):
        c = classify_fact("market_cap_snapshot",
                          {"close": 119.51, "currency": "USD", "trading_date": "2026-03-30"})
        assert c["confidence"] == "high" and c["currency"] == "USD"
        assert c["frequency"] == "instant" and c["period_end"] == "2026-03-30"

    def test_accounting_parens_negative(self):
        c = classify_fact("market_cap_2026-07-03", "( 1,234 ) 美元")
        assert c["confidence"] == "high" and c["value"] == "-1234" and c["currency"] == "USD"

    def test_period_ambiguous_goes_to_review_not_guess(self):
        """期间可由字段名推断但财年口径/单位不确定 → review（不批量猜测迁移）。"""
        c = classify_fact("revenue_fy2025", "1,364")
        assert c["confidence"] == "review"
        assert "不自动映射" in c["reason"]

    def test_no_currency_no_mapping(self):
        c = classify_fact("market_cap_2026-07-03", "5299")  # 无币种提示
        assert c["confidence"] == "review" and "不猜货币" in c["reason"]

    def test_hk_dollar_not_misread_as_usd(self):
        """review #31：HK$ 必须先于裸 $ 匹配，不得标成 USD 进入自动迁移。"""
        c = classify_fact("market_cap_2026-07-03", "HK$5,299")
        assert c["confidence"] == "high" and c["currency"] == "HKD"
        c2 = classify_fact("market_cap_2026-07-03", "$5,299")
        assert c2["currency"] == "USD"  # 裸 $ 默认美元（已排除 HK$/US$ 先行）
        c3 = classify_fact("市值（2026-07-03）", "US$5,299")
        assert c3["currency"] == "USD"

    def test_prose_stays_legacy(self):
        c = classify_fact("business_model", "sells fuel cells")
        assert c["confidence"] == "text"


class TestCommands:
    def test_inventory_dry_run_readonly(self, data_dir):
        before = _fact_state(data_dir)
        rc = main(["--data-dir", str(data_dir), "inventory"])
        assert rc == 0
        assert _fact_state(data_dir) == before  # 盘点不写 kb（行数/版本链不变）
        reports = list((data_dir / "migration").glob("inventory-*.json"))
        assert reports
        data = json.loads(reports[0].read_text())
        be = data["entities"][0]
        assert be["entity"] == "stock:BE"
        assert len(be["typed_candidates_high"]) == 1  # market_cap_snapshot
        assert len(be["typed_candidates_review"]) == 1  # revenue_fy2025
        assert "business_model" in be["legacy_text_fields"]

    def test_apply_typed_dry_run_then_apply_idempotent(self, data_dir):
        rc = main(["--data-dir", str(data_dir), "apply-typed"])
        assert rc == 0  # dry-run 默认
        from finance_agent.knowledge.metric_store import MetricStore
        m = MetricStore(data_dir / "metrics.db")
        assert m.observations_as_of("stock", "BE", datetime.now(UTC)) == []
        m.close()
        rc = main(["--data-dir", str(data_dir), "apply-typed", "--apply"])
        assert rc == 0
        m = MetricStore(data_dir / "metrics.db")
        obs = m.observations_as_of("stock", "BE", datetime.now(UTC))
        assert len(obs) == 1
        assert obs[0].metric_key == "market_cap" and obs[0].currency == "USD"
        assert obs[0].pit_grade.value == "B"  # 迁移值不冒充 A 级原始披露定位
        assert "legacy_field" in obs[0].dimensions  # 回指旧字段（可追溯）
        m.close()
        # 断点恢复：重跑 --apply 幂等（checkpoint 跳过，不产生新版本）
        rc = main(["--data-dir", str(data_dir), "apply-typed", "--apply"])
        assert rc == 0
        m = MetricStore(data_dir / "metrics.db")
        obs2 = m.observations_as_of("stock", "BE", datetime.now(UTC))
        assert len(obs2) == 1
        assert m.conflicted_semantic_hashes("stock", "BE") == []
        m.close()

    def test_shadow_generates_reconciliation_manifest(self, data_dir):
        rc = main(["--data-dir", str(data_dir), "shadow"])
        assert rc == 0
        manifests = list((data_dir / "migration").glob("shadow-*/manifest.json"))
        assert manifests
        m = json.loads(manifests[0].read_text())
        entry = m["snapshots"][0]
        assert entry["entity"] == "stock:BE"
        assert entry["source_fact_ids"]  # 对账：来源 fact_id 记录在案
        assert entry["module_status"]["business_engine"] == "partial"  # legacy 文本可读
        assert m["projector_version"] == "1"

    def test_never_merges_entities_or_deletes_versions(self, data_dir):
        main(["--data-dir", str(data_dir), "apply-typed", "--apply"])
        main(["--data-dir", str(data_dir), "shadow"])
        kb = BitemporalStore(data_dir / "kb.db")
        history = kb.history("stock", "BE", "revenue_fy2025")
        assert len(history) == 1  # 旧版本链原样保留
        view = kb.view("stock", "BE", datetime.now(UTC))
        assert set(view) == {"market_cap_snapshot", "revenue_fy2025", "business_model"}
        kb.close()


def _fact_state(d: Path) -> tuple:
    """旧库状态指纹（WAL 下文件大小不可靠）：行数 + 版本总和。"""
    kb = BitemporalStore(d / "kb.db")
    try:
        rows = kb._conn.execute(  # noqa: SLF001
            "SELECT COUNT(*), COALESCE(SUM(version), 0) FROM facts"
        ).fetchone()
        ev = kb._conn.execute("SELECT COUNT(*) FROM evidence").fetchone()  # noqa: SLF001
        return (rows[0], rows[1], ev[0])
    finally:
        kb.close()
