"""供应商验收卡线束验收（方案 §7.2；离线——注入假 transport，不打真实网络）。

判据：
- 缺凭证 → 卡片 blocked，缺失项显式列出（不假装验收过）；
- 探针如实记录：字段缺失/PIT 字段空值/HTTP 错误都进卡（验收卡的用途是暴露问题）；
- 覆盖计数与 markdown 渲染可用；
- 行束不替人下结论（sign_off 待人工签署）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from vendor_acceptance import PROBES, build_card, card_to_markdown  # noqa: E402


def fake_get(payload_by_url):
    def get(url: str):
        for needle, payload in payload_by_url.items():
            if needle in url:
                return 200, payload
        return 404, {"error": "not found"}
    return get


class TestBlockedWithoutCredentials:
    def test_missing_key_blocks_visibly(self):
        card = build_card("fmp", ["AAPL"], env={})
        assert card["status"] == "blocked"
        assert "FMP_API_KEY" in card["blocked_reason"]
        assert card["probes"] == [], "缺凭证不跑探针（不假装验收过）"
        md = card_to_markdown(card)
        assert "blocked" in md and "checklist" in md


class TestProbesAreHonest:
    def test_field_gaps_and_pit_absence_recorded(self):
        payload = [
            {"symbol": "AAPL", "date": "2026-12-31", "estimatedRevenueAvg": 4.2e11,
             # 故意缺 estimatedEpsAvg + updatedDate（无修订 PIT）
            }
        ]
        get = fake_get({"analyst-estimates": payload,
                        "earning-call-transcript": [{"content": "call text",
                                                     "symbol": "AAPL",
                                                     "period": "Q3",
                                                     "date": "2025-10-30"}]})
        card = build_card("fmp", ["AAPL"], env={"FMP_API_KEY": "k"}, get=get)
        assert card["status"] == "probed"
        est = card["probes"][0]
        assert est["endpoint"] == "analyst_estimates"
        sample = est["samples"][0]
        assert "estimatedEpsAvg" in sample["expect_fields_missing"]
        assert "updatedDate" not in sample["pit_fields_present"], \
            "无修订时间戳必须可见（快照 PIT 是验收要点）"
        transcript = card["probes"][1]["samples"][0]
        assert transcript["rows"] == 1 and "date" in transcript["pit_fields_present"]

    def test_http_errors_recorded_not_wrapped(self):
        get = fake_get({})  # 全部 404
        card = build_card("fmp", ["AAPL"], env={"FMP_API_KEY": "k"}, get=get)
        sample = card["probes"][0]["samples"][0]
        assert sample["http_status"] == 404
        assert card["probes"][0]["coverage"] == "0/1"

    def test_transport_exception_recorded(self):
        def boom(url):
            raise ConnectionError("refused")
        card = build_card("fmp", ["AAPL"], env={"FMP_API_KEY": "k"}, get=boom)
        sample = card["probes"][0]["samples"][0]
        assert "ConnectionError" in sample["error"]

    def test_markdown_has_checklist_and_signoff(self):
        card = build_card("financial_datasets", ["AAPL"],
                          env={"FINANCIAL_DATASETS_API_KEY": "k"},
                          get=fake_get({"income-statements": {
                              "income_statements": [
                                  {"filing_url": "https://sec.gov/x",
                                   "report_period": "FY2025",
                                   "fiscal_period": "FY"},
                              ]}}))
        md = card_to_markdown(card)
        assert "- [ ]" in md, "checklist 逐项人工确认"
        assert "待人工签署" in md
        assert "Financial Datasets" in md


class TestRegistryShape:
    def test_probes_have_expect_and_pit_fields(self):
        for vid, v in PROBES.items():
            assert v["env_keys"] and v["endpoints"], vid
            for ep in v["endpoints"]:
                assert ep["expect_fields"] and ep["pit_fields"], (vid, ep["name"])
