"""SEC 结构化披露验收（tools-plugins 方案 §5.2/§7.1，PR#5 sec-financial-facts）。

判据：
- 公开时刻优先 acceptanceDateTime（分钟精度），缺失保守取 filingDate UTC 日末
  （不得随意当作零点公开——零点是穿越方向）；
- 历史遍历：submissions `recent` 之外的分段索引按需拉取；单段失败降级可见，
  不假装没有旧 filing；
- companyfacts XBRL：原始 tag/unit/期间/filing 版本（accn）+ 原文链接完整，
  过滤（tags/forms/units/period）与 as_of 服务端过滤有效，limit 截断可预期；
- 响应形状异常 fail-loud（错误不被包装成空结果）；进程内缓存不重复拉数 MB 全量；
- 离线测试：monkeypatch httpx.get，不打真实网络。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from finance_agent.gateway.adapters.edgar import EdgarAdapter, acceptance_or_eod
from finance_agent.gateway.adapters.edgar_facts import EdgarFactsAdapter

T = lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC)  # noqa: E731


@pytest.fixture(autouse=True)
def no_throttle_sleep(monkeypatch):
    monkeypatch.setattr("finance_agent.gateway.adapters.edgar._MIN_INTERVAL_S", 0.0)


class FakeResp:
    def __init__(self, data=None, *, error: Exception | None = None):
        self._data = data
        self._error = error

    def json(self):
        return self._data

    def raise_for_status(self):
        if self._error is not None:
            raise self._error


SUBMISSIONS = {
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-23-000106", "0000320193-23-000077"],
            "filingDate": ["2023-11-03", "2023-08-04"],
            "reportDate": ["2023-09-30", "2023-06-30"],
            # 第二条无 acceptance（真实 JSON 里是空串/缺失列，两种都要兼容）
            "acceptanceDateTime": ["2023-11-03T06:01:58-05:00", ""],
            "form": ["10-K", "10-Q"],
            "primaryDocument": ["aapl-20230930.htm", "aapl-20230630.htm"],
        },
        "files": [
            {"name": "AAPL-submissions-001.json",
             "filingFrom": "2015-01-01", "filingTo": "2020-12-31"},
        ],
    },
}

SEGMENT = {
    "accessionNumber": ["0000320193-19-000045"],
    "filingDate": ["2019-10-31"],
    "reportDate": ["2019-09-28"],
    # 受理后次日才算 filingDate（17:30  cutoff 规则）：acceptance 必须优先于 filingDate
    "acceptanceDateTime": ["2019-10-30T18:12:36-04:00"],
    "form": ["10-K"],
    "primaryDocument": ["aapl-20190928.htm"],
}

COMPANYFACTS = {
    "entityName": "Apple Inc.",
    "cik": "0000320193",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "label": "Revenue from Contract with Customer",
                "units": {"USD": [
                    {"start": "2022-09-25", "end": "2023-09-30", "val": 383285000000,
                     "accn": "0000320193-23-000106", "fy": 2023, "fp": "FY",
                     "form": "10-K", "filed": "2023-11-03",
                     "accepted": "2023-11-03T06:01:58-05:00", "frame": "CY2023"},
                    {"start": "2021-09-26", "end": "2022-09-24", "val": 394328000000,
                     "accn": "0000320193-22-000108", "fy": 2022, "fp": "FY",
                     "form": "10-K", "filed": "2022-10-28",
                     "accepted": "2022-10-28T06:04:28-04:00"},
                    {"start": "2023-04-02", "end": "2023-07-01", "val": 81797000000,
                     "accn": "0000320193-23-000077", "fy": 2023, "fp": "Q3",
                     "form": "10-Q", "filed": "2023-08-04"},  # 无 accepted
                ]},
            },
            "NetIncomeLoss": {
                "label": "Net Income Loss",
                "units": {"USD": [
                    {"start": "2022-09-25", "end": "2023-09-30", "val": 96995000000,
                     "accn": "0000320193-23-000106", "fy": 2023, "fp": "FY",
                     "form": "10-K", "filed": "2023-11-03",
                     "accepted": "2023-11-03T06:01:58-05:00"},
                ]},
            },
        },
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "label": "Shares Outstanding",
                "units": {"shares": [
                    {"end": "2023-10-27", "val": 15552979000,
                     "accn": "0000320193-23-000106", "form": "10-K",
                     "filed": "2023-11-03", "accepted": "2023-11-03T06:01:58-05:00"},
                ]},
            },
        },
    },
}

TICKER_MAP = {"0": {"ticker": "AAPL", "cik_str": 320193, "title": "Apple Inc."}}


def install_router(monkeypatch, *, calls: list[str], segment_error: Exception | None = None,
                   facts_payload=None):
    import httpx

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append(str(url))
        if "companyfacts" in str(url):
            return FakeResp(COMPANYFACTS if facts_payload is None else facts_payload)
        if "company_tickers" in str(url):
            return FakeResp(TICKER_MAP)
        if str(url).endswith("AAPL-submissions-001.json"):
            if segment_error is not None:
                return FakeResp(error=segment_error)
            return FakeResp(SEGMENT)
        if "submissions/CIK" in str(url):
            return FakeResp(SUBMISSIONS)
        raise AssertionError(f"未预期的请求: {url}")

    monkeypatch.setattr(httpx, "get", fake_get)


# ---------------- 公开时刻语义 ----------------


class TestAcceptanceSemantics:
    def test_acceptance_preferred_and_utc_normalized(self):
        assert acceptance_or_eod("2023-11-03", "2023-11-03T06:01:58-05:00") == \
            T("2023-11-03T11:01:58+00:00")

    def test_missing_acceptance_conservative_eod(self):
        """只有日精度 filingDate：保守取 UTC 日末（零点比真实公开早，是穿越方向）。"""
        assert acceptance_or_eod("2023-08-04", None) == T("2023-08-04T23:59:59+00:00")
        assert acceptance_or_eod("2023-08-04", "") == T("2023-08-04T23:59:59+00:00")

    def test_bad_acceptance_falls_back(self):
        assert acceptance_or_eod("2023-08-04", "not-a-time") == \
            T("2023-08-04T23:59:59+00:00")


# ---------------- submissions：acceptance + 历史分段 ----------------


class TestSubmissions:
    def test_available_at_uses_acceptance(self, monkeypatch):
        calls: list[str] = []
        install_router(monkeypatch, calls=calls)
        recs = EdgarAdapter().query({"ticker": "AAPL"})
        assert len(recs) == 2
        by_form = {r.payload["form"]: r for r in recs}
        assert by_form["10-K"].available_at == T("2023-11-03T11:01:58+00:00")
        # 无 acceptance 的 10-Q：保守日末，且 payload 透明保留两个日期
        assert by_form["10-Q"].available_at == T("2023-08-04T23:59:59+00:00")
        assert by_form["10-Q"].payload["filingDate"] == "2023-08-04"
        assert by_form["10-Q"].payload["acceptanceDateTime"] in ("", None)
        assert by_form["10-K"].event_time == T("2023-09-30T00:00:00+00:00")

    def test_as_of_filter_uses_acceptance_time(self, monkeypatch):
        calls: list[str] = []
        install_router(monkeypatch, calls=calls)
        # as_of 在受理时刻之前 → 该 filing 当时不可知
        recs = EdgarAdapter().query({"ticker": "AAPL"}, as_of=T("2023-11-03T10:00:00+00:00"))
        assert [r.payload["form"] for r in recs] == ["10-Q"]

    def test_history_segments_merged_on_demand(self, monkeypatch):
        calls: list[str] = []
        install_router(monkeypatch, calls=calls)
        adapter = EdgarAdapter()
        # 默认不拉历史（旧行为不变）
        recs = adapter.query({"ticker": "AAPL"})
        assert not any("AAPL-submissions-001" in c for c in calls)
        assert len(recs) == 2
        recs = adapter.query({"ticker": "AAPL", "include_history": True})
        assert any("AAPL-submissions-001" in c for c in calls)
        assert len(recs) == 3
        old = next(r for r in recs if r.payload["form"] == "10-K"
                   and r.payload["filingDate"] == "2019-10-31")
        # acceptance（10-30 22:12 UTC）优先于 filingDate（10-31）
        assert old.available_at == T("2019-10-30T22:12:36+00:00")
        assert old.url.endswith("/320193/000032019319000045/aapl-20190928.htm")

    def test_history_segment_skipped_when_before_as_of(self, monkeypatch):
        calls: list[str] = []
        install_router(monkeypatch, calls=calls)
        # as_of 早于分段起点 → 整段越界，不必拉
        EdgarAdapter().query({"ticker": "AAPL", "include_history": True},
                             as_of=T("2010-01-01T00:00:00+00:00"))
        assert not any("AAPL-submissions-001" in c for c in calls)

    def test_history_segment_failure_visible_not_silent(self, monkeypatch, caplog):
        import httpx

        calls: list[str] = []
        install_router(monkeypatch, calls=calls,
                       segment_error=httpx.HTTPError("403 rate limited"))
        adapter = EdgarAdapter()
        with caplog.at_level("WARNING"):
            recs = adapter.query({"ticker": "AAPL", "include_history": True})
        # recent 结果不受拖累；失败降级可见（属性 + 日志，不假装没有旧 filing）
        assert len(recs) == 2
        assert adapter.last_history_errors and "403" in adapter.last_history_errors[0]["error"]
        assert any("历史分段" in r.message for r in caplog.records)


# ---------------- companyfacts XBRL ----------------


class TestCompanyFacts:
    def test_records_carry_full_provenance(self, monkeypatch):
        calls: list[str] = []
        install_router(monkeypatch, calls=calls)
        recs = EdgarFactsAdapter().query({"cik": "320193"})
        assert len(recs) == 5
        rev = next(r for r in recs if r.payload["tag"] == "Revenues"
                   and r.payload["fiscal_label"] == "FY2023 FY")
        p = rev.payload
        assert p["taxonomy"] == "us-gaap" and p["unit"] == "USD"
        assert p["val"] == 383285000000 and p["value_text"] == "383285000000"
        assert p["start"] == "2022-09-25" and p["end"] == "2023-09-30"
        assert p["accn"] == "0000320193-23-000106" and p["form"] == "10-K"
        assert p["label"] == "Revenue from Contract with Customer"
        assert rev.available_at == T("2023-11-03T11:01:58+00:00")
        assert rev.event_time == T("2023-09-30T00:00:00+00:00")
        assert rev.url == "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/"

    def test_no_accepted_conservative_eod(self, monkeypatch):
        calls: list[str] = []
        install_router(monkeypatch, calls=calls)
        recs = EdgarFactsAdapter().query({"cik": "320193", "tags": ["Revenues"],
                                          "forms": ["10-Q"]})
        assert len(recs) == 1
        assert recs[0].available_at == T("2023-08-04T23:59:59+00:00")

    def test_filters_and_sort_and_limit(self, monkeypatch):
        calls: list[str] = []
        install_router(monkeypatch, calls=calls)
        adapter = EdgarFactsAdapter()
        by_tag = adapter.query({"cik": "320193", "tags": ["Revenues"]})
        assert len(by_tag) == 3
        by_unit = adapter.query({"cik": "320193", "units": ["shares"]})
        assert len(by_unit) == 1 and by_unit[0].payload["taxonomy"] == "dei"
        by_period = adapter.query({"cik": "320193", "tags": ["Revenues"],
                                   "period_end": "2022-12-31"})
        assert len(by_period) == 1 and by_period[0].payload["fy"] == 2022
        # 排序：最近披露在前；limit 截断可预期
        limited = adapter.query({"cik": "320193", "limit": 2})
        assert len(limited) == 2
        assert limited[0].available_at >= limited[1].available_at
        assert limited[0].payload["filed"] == "2023-11-03"

    def test_as_of_server_side_filter(self, monkeypatch):
        calls: list[str] = []
        install_router(monkeypatch, calls=calls)
        recs = EdgarFactsAdapter().query({"cik": "320193"},
                                         as_of=T("2023-01-01T00:00:00+00:00"))
        assert len(recs) == 1
        assert recs[0].payload["fy"] == 2022  # 2023 年的版本当时不可知

    def test_ticker_resolution_via_official_map(self, monkeypatch):
        calls: list[str] = []
        install_router(monkeypatch, calls=calls)
        recs = EdgarFactsAdapter().query({"ticker": "AAPL", "tags": ["NetIncomeLoss"]})
        assert len(recs) == 1
        assert any("company_tickers" in c for c in calls)

    def test_bad_shape_fail_loud(self, monkeypatch):
        calls: list[str] = []
        install_router(monkeypatch, calls=calls, facts_payload={"unexpected": True})
        with pytest.raises(ValueError, match="形状异常"):
            EdgarFactsAdapter().query({"cik": "320193"})

    def test_companyfacts_cached_per_cik(self, monkeypatch):
        calls: list[str] = []
        install_router(monkeypatch, calls=calls)
        adapter = EdgarFactsAdapter()
        adapter.query({"cik": "320193"})
        adapter.query({"cik": "320193", "tags": ["Revenues"]})
        assert sum(1 for c in calls if "companyfacts" in c) == 1, \
            "数 MB 全量必须进程内缓存复用"

    def test_capability_is_pit_a(self):
        cap = EdgarFactsAdapter().capability()
        assert cap.source_id == "edgar_facts"
        assert cap.pit_grade.value == "A"
        assert cap.server_side_asof is True
