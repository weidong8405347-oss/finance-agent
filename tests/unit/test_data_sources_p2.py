"""P2 数据源（Exa/GDELT/基本面）与 calc 工具的离线契约测试。

纪律钉住（research-capability-upgrade §4.5/§4.7）：
- PIT 分级真实：GDELT/Exa=B（逐条带时刻才成立，无日期 → available_at=None 诚实降级）；
  基本面快照=C（当前值无历史 PIT）
- 评估模式 fail-closed：C 级拒用；B 级默认拒（allow_pit_b 放行后无 available_at 的记录仍被丢弃）
- calc：Decimal 精确算术；双源 >1% 分歧告警；三情景估值数学正确
"""

import json
from datetime import UTC, datetime

import pytest

from finance_agent.gateway.adapters.exa_search import ExaSearchAdapter
from finance_agent.gateway.adapters.fundamentals import (
    AkshareHKFundamentalsAdapter,
    YFinanceFundamentalsAdapter,
)
from finance_agent.gateway.adapters.gdelt import GdeltNewsAdapter
from finance_agent.gateway.gateway import DataGateway, SourceBlockedError
from finance_agent.knowledge.models import PitGrade
from finance_agent.research.calc import calc_tool


class _FakeResp:
    def __init__(self, payload=None, text: str = ""):
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# ---------------- GDELT ----------------


class TestGdelt:
    def _adapter(self, monkeypatch, articles):
        import httpx

        captured = {}

        def fake_get(url, params=None, timeout=None):
            captured["params"] = params
            return _FakeResp({"articles": articles})

        monkeypatch.setattr(httpx, "get", fake_get)
        return GdeltNewsAdapter(), captured

    def test_parse_articles_with_pit(self, monkeypatch):
        adapter, _ = self._adapter(monkeypatch, [
            {"title": "晶泰控股发布新平台", "url": "https://x.com/1", "seendate": "20240115T080000Z",
             "domain": "x.com", "language": "Chinese", "sourceCountry": "CN"},
        ])
        recs = adapter.query({"query": "晶泰控股"})
        assert len(recs) == 1
        assert recs[0].available_at == datetime(2024, 1, 15, 8, 0, tzinfo=UTC)
        assert recs[0].payload["language"] == "Chinese"

    def test_as_of_passed_server_side(self, monkeypatch):
        adapter, captured = self._adapter(monkeypatch, [])
        as_of = datetime(2024, 3, 1, tzinfo=UTC)
        adapter.query({"query": "x"}, as_of=as_of)
        assert captured["params"]["enddatetime"] == "20240301000000"

    def test_non_json_response_degrades_to_empty(self, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(text="<html>限流</html>"))
        assert GdeltNewsAdapter().query({"query": "x"}) == []

    def test_capability_is_pit_b(self):
        assert GdeltNewsAdapter().capability().pit_grade is PitGrade.B


# ---------------- Exa（Novita 网关 passthrough 为默认通道） ----------------


class TestExa:
    def _adapter(self, monkeypatch, results, **kw):
        import httpx

        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["body"] = json
            captured["headers"] = headers
            return _FakeResp({"results": results})

        monkeypatch.setattr(httpx, "post", fake_post)
        return ExaSearchAdapter(novita_api_key="sk-novita-test", **kw), captured

    def test_default_channel_is_novita_gateway_with_bearer(self, monkeypatch):
        """默认通道：Novita 的 Exa passthrough 端点 + `Authorization: Bearer`。"""
        adapter, captured = self._adapter(monkeypatch, [])
        assert adapter.channel == "novita"
        adapter.query({"query": "x"})
        assert captured["url"] == "https://api.novita.ai/v3/exa/search"
        assert captured["headers"]["Authorization"] == "Bearer sk-novita-test"
        assert "x-api-key" not in captured["headers"]
        assert captured["headers"]["Content-Type"] == "application/json"

    def test_exa_key_falls_back_to_direct_endpoint(self, monkeypatch):
        """仅有 EXA_API_KEY（旧配置）：回退直连 api.exa.ai + x-api-key，PIT 语义不变。"""
        import httpx

        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResp({"results": [
                {"title": "t", "url": "https://x.com/a", "publishedDate": "2024-02-01T10:00:00Z"},
            ]})

        monkeypatch.setattr(httpx, "post", fake_post)
        adapter = ExaSearchAdapter(api_key="sk-exa-direct", env={})
        assert adapter.channel == "exa"
        recs = adapter.query({"query": "x"})
        assert captured["url"] == "https://api.exa.ai/search"
        assert captured["headers"]["x-api-key"] == "sk-exa-direct"
        assert recs[0].available_at == datetime(2024, 2, 1, 10, 0, tzinfo=UTC)

    def test_novita_key_wins_over_exa_key(self, monkeypatch):
        """两 key 并存时 Novita 优先（通道唯一、可预测，不会双发请求）。"""
        adapter, captured = self._adapter(monkeypatch, [], api_key="sk-exa-direct")
        assert adapter.channel == "novita"
        adapter.query({"query": "x"})
        assert captured["url"] == "https://api.novita.ai/v3/exa/search"

    def test_env_key_resolution_prefers_novita(self):
        assert ExaSearchAdapter(env={"NOVITA_API_KEY": "sk-n"}).channel == "novita"
        assert ExaSearchAdapter(env={"EXA_API_KEY": "sk-e"}).channel == "exa"
        assert ExaSearchAdapter(env={"NOVITA_API_KEY": "sk-n", "EXA_API_KEY": "sk-e"}).channel == "novita"
        assert ExaSearchAdapter(env={}).channel is None

    def test_capability_declares_active_channel(self):
        cap = ExaSearchAdapter(novita_api_key="sk-n").capability()
        assert cap.source_id == "web_search" and cap.pit_grade is PitGrade.B
        assert cap.server_side_asof is True and "novita" in cap.description

    def test_published_date_becomes_available_at(self, monkeypatch):
        adapter, _ = self._adapter(monkeypatch, [
            {"title": "AI4S", "url": "https://x.com/a", "publishedDate": "2024-02-01T10:00:00Z",
             "text": "body", "author": "analyst"},
        ])
        recs = adapter.query({"query": "ai for science"})
        assert recs[0].available_at == datetime(2024, 2, 1, 10, 0, tzinfo=UTC)
        assert recs[0].payload["text"] == "body"

    def test_missing_published_date_degrades_honestly(self, monkeypatch):
        """无 publishedDate 的条目 available_at=None：评估模式必被网关丢弃（诚实降级）。"""
        adapter, _ = self._adapter(monkeypatch, [{"title": "x", "url": "https://x.com/a"}])
        recs = adapter.query({"query": "x"})
        assert recs[0].available_at is None

    def test_as_of_server_side_filter(self, monkeypatch):
        adapter, captured = self._adapter(monkeypatch, [])
        as_of = datetime(2024, 1, 1, tzinfo=UTC)
        adapter.query({"query": "x"}, as_of=as_of)
        assert captured["body"]["endPublishedDate"] == as_of.isoformat()

    def test_missing_key_fail_closed(self):
        """两个 key 都没配 → 未 configured（装配层不注册）；硬调 query 则 fail-loud。"""
        adapter = ExaSearchAdapter(env={})
        assert adapter.configured is False
        assert adapter.healthcheck() == {
            "ok": False, "detail": "未配置 NOVITA_API_KEY / EXA_API_KEY"}
        with pytest.raises(RuntimeError, match="NOVITA_API_KEY / EXA_API_KEY"):
            adapter.query({"query": "x"})


# ---------------- 基本面快照 ----------------


class TestFundamentals:
    def test_yfinance_payload_mapping(self, monkeypatch):
        import yfinance as yf

        class _T:
            def __init__(self, ticker):
                self.info = {"longName": "Bloom Energy", "marketCap": 2.1e10,
                             "sharesOutstanding": 2.3e8, "currency": "USD",
                             "trailingPE": None}  # None 字段跳过

        monkeypatch.setattr(yf, "Ticker", _T)
        recs = YFinanceFundamentalsAdapter().query({"ticker": "BE"})
        p = recs[0].payload
        assert p["name"] == "Bloom Energy" and p["market_cap"] == 2.1e10
        assert "pe_ttm" not in p  # None 不进 payload
        assert recs[0].available_at is None  # C 级快照

    def test_yfinance_empty_info_returns_empty(self, monkeypatch):
        import yfinance as yf

        monkeypatch.setattr(yf, "Ticker", lambda t: type("_T", (), {"info": {}})())
        assert YFinanceFundamentalsAdapter().query({"ticker": "NOPE"}) == []

    def test_hk_code_normalization(self):
        assert AkshareHKFundamentalsAdapter._normalize_code("2228.HK") == "02228"
        assert AkshareHKFundamentalsAdapter._normalize_code("2228") == "02228"
        assert AkshareHKFundamentalsAdapter._normalize_code("00700") == "00700"
        with pytest.raises(ValueError, match="数字"):
            AkshareHKFundamentalsAdapter._normalize_code("AAPL")

    def test_capability_is_pit_c(self):
        assert YFinanceFundamentalsAdapter().capability().pit_grade is PitGrade.C
        assert AkshareHKFundamentalsAdapter().capability().pit_grade is PitGrade.C


# ---------------- 评估模式 fail-closed 回归（新源不破坏时间锁） ----------------


class TestEvalModeCompliance:
    def _eval_gateway(self, **kw):
        return DataGateway(mode="eval", eval_as_of=datetime(2024, 1, 1, tzinfo=UTC), **kw)

    def test_c_grade_fundamentals_blocked_in_eval(self):
        gw = self._eval_gateway()
        gw.register(YFinanceFundamentalsAdapter())
        with pytest.raises(SourceBlockedError, match="C 级"):
            gw.query("fundamentals", {"ticker": "BE"})

    def test_b_grade_blocked_by_default_in_eval(self):
        gw = self._eval_gateway()
        gw.register(ExaSearchAdapter(api_key="sk-t"))
        with pytest.raises(SourceBlockedError, match="B 级"):
            gw.query("web_search", {"query": "x"})

    def test_b_grade_allowed_but_undated_records_dropped(self, monkeypatch):
        """allow_pit_b 放行后：无 available_at 的记录仍被网关逐条丢弃（leakage 审计）。"""
        import httpx

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp({"results": [
            {"title": "有日期", "url": "https://x.com/1", "publishedDate": "2023-12-01T00:00:00Z"},
            {"title": "无日期", "url": "https://x.com/2"},
            {"title": "未来", "url": "https://x.com/3", "publishedDate": "2024-06-01T00:00:00Z"},
        ]}))
        from finance_agent.eventstore.store import EventStore

        gw = self._eval_gateway(allow_pit_b=True, events=EventStore(":memory:"), run_id="eval-t")
        gw.register(ExaSearchAdapter(api_key="sk-t"))
        recs = gw.query("web_search", {"query": "x"})
        assert [r.payload["title"] for r in recs] == ["有日期"]


# ---------------- calc 工具 ----------------


class TestCalc:
    def test_verify_market_cap_ok(self):
        r = json.loads(calc_tool({"op": "verify_market_cap", "price": 510, "shares": 9.11e9,
                                  "reported": 4.65e12})["content"])
        assert r["ok"] is True and float(r["diff_pct"]) < 0.1

    def test_verify_market_cap_mismatch_alerts(self):
        """单位混淆（港币亿 vs 人民币亿）场景：偏差 >1% 必须告警，不许静默通过。"""
        r = json.loads(calc_tool({"op": "verify_market_cap", "price": 510, "shares": 9.11e9,
                                  "reported": 4.2e12})["content"])
        assert r["ok"] is False and float(r["diff_pct"]) > 1

    def test_decimal_precision_no_float_pollution(self):
        """0.1+0.2 类浮点尾差不许出现（Decimal(str) 路径）。"""
        r = json.loads(calc_tool({"op": "verify_market_cap", "price": 0.1, "shares": 3,
                                  "reported": 0.3})["content"])
        assert r["computed_market_cap"] == "0" or r["ok"] is True
        assert r["diff_pct"] in ("0.0000", "0")

    def test_cross_validate_detects_spread(self):
        r = json.loads(calc_tool({"op": "cross_validate", "values": [100, 103.5]})["content"])
        assert r["ok"] is False and float(r["spread_pct"]) > 1
        r2 = json.loads(calc_tool({"op": "cross_validate", "values": [100, 100.5]})["content"])
        assert r2["ok"] is True

    def test_three_scenario_math(self):
        """eps=2, 增长 20%, 3 年, PE 30 → 2×1.2³×30 = 103.68；现价 80 → 空间 +29.6%。"""
        r = json.loads(calc_tool({"op": "three_scenario", "price": 80, "eps": 2,
                                  "growth": [0.1, 0.2, 0.3], "pe": [20, 30, 40], "years": 3})["content"])
        mid = r["scenarios"][1]
        assert mid["target_price"] == "103.6800"
        assert float(mid["upside_pct"]) == pytest.approx(29.6, abs=0.01)

    def test_unknown_op_and_bad_value_fail_visibly(self):
        assert "error" in calc_tool({"op": "nope"})["content"]
        assert "error" in calc_tool({"op": "verify_market_cap", "price": "abc", "shares": 1,
                                     "reported": 1})["content"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------- HKEXnews（披露易，A 级） ----------------


class TestHKEXNews:
    def _adapter(self, monkeypatch):
        import httpx

        calls = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            if "prefix.do" in url:
                return _FakeResp(text='cb({"more":"1","stockInfo":'
                                      '[{"stockId":1000225298,"code":"02228","name":"晶泰控股"}]});')
            calls["params"] = params
            return _FakeResp({
                "result": json.dumps([
                    {"FILE_INFO": "1MB", "NEWS_ID": "12290445", "SHORT_TEXT": "中期業績",
                     "TOTAL_COUNT": "2", "DOD_WEB_PATH": "", "STOCK_NAME": "晶泰控股",
                     "TITLE": "截至2026年6月30日止六個月的中期業績公告", "FILE_TYPE": "PDF",
                     "DATE_TIME": "19/08/2026 18:09", "LONG_TEXT": "公告及通告 - [中期業績]",
                     "STOCK_CODE": "02228",
                     "FILE_LINK": "/listedco/listconews/sehk/2026/0819/2026081900920_c.pdf"},
                    {"FILE_INFO": "2MB", "NEWS_ID": "12290000", "SHORT_TEXT": "年報",
                     "TOTAL_COUNT": "2", "DOD_WEB_PATH": "", "STOCK_NAME": "晶泰控股",
                     "TITLE": "2025 年報", "FILE_TYPE": "PDF",
                     "DATE_TIME": "28/04/2026 09:00", "LONG_TEXT": "公告及通告 - [年報]",
                     "STOCK_CODE": "02228",
                     "FILE_LINK": "/listedco/listconews/sehk/2026/0428/x_c.pdf"},
                ], ensure_ascii=False),
                "recordCnt": 2, "rowRange": 100, "hasNextRow": False,
            })

        monkeypatch.setattr(httpx, "get", fake_get)
        from finance_agent.gateway.adapters.hkexnews import HKEXNewsAdapter

        return HKEXNewsAdapter(), calls

    def test_two_step_resolution_and_parse(self, monkeypatch):
        adapter, calls = self._adapter(monkeypatch)
        recs = adapter.query({"ticker": "2228.HK", "days": 400})
        assert len(recs) == 2
        r0 = recs[0]
        assert r0.payload["title"] == "截至2026年6月30日止六個月的中期業績公告"
        assert r0.payload["category"] == "公告及通告 - [中期業績]"
        # HKT 18:09 → UTC 10:09
        assert r0.available_at == datetime(2026, 8, 19, 10, 9, tzinfo=UTC)
        assert r0.url.startswith("https://www1.hkexnews.hk/listedco/")
        # spike 实测坑：日期必须 YYYYMMDD 紧凑格式 + rowRange 必填
        assert calls["params"]["fromDate"].isdigit() and len(calls["params"]["fromDate"]) == 8
        assert calls["params"]["rowRange"] == "100"
        assert calls["params"]["stockId"] == "1000225298"

    def test_capability_is_pit_a(self, monkeypatch):
        adapter, _ = self._adapter(monkeypatch)
        assert adapter.capability().pit_grade is PitGrade.A

    def test_as_of_server_side_and_double_check(self, monkeypatch):
        adapter, calls = self._adapter(monkeypatch)
        as_of = datetime(2026, 5, 1, tzinfo=UTC)
        recs = adapter.query({"ticker": "02228"}, as_of=as_of)
        assert calls["params"]["toDate"] == "20260501"  # 服务端过滤
        assert len(recs) == 1 and recs[0].payload["title"] == "2025 年報"  # 客户端双保险

    def test_rejects_non_hk_ticker(self, monkeypatch):
        adapter, _ = self._adapter(monkeypatch)
        with pytest.raises(ValueError, match="港股数字代码"):
            adapter.query({"ticker": "AAPL"})


# ---------------- Tavily（C 级备份搜索源） ----------------


class TestTavily:
    def _adapter(self, monkeypatch, results):
        import httpx

        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["body"] = json
            return _FakeResp({"results": results})

        monkeypatch.setattr(httpx, "post", fake_post)
        from finance_agent.gateway.adapters.tavily import TavilySearchAdapter

        return TavilySearchAdapter(api_key="tvly-test"), captured

    def test_parse_results_c_grade_no_pit(self, monkeypatch):
        adapter, captured = self._adapter(monkeypatch, [
            {"title": "AI4S overview", "url": "https://x.com/a", "content": "body", "score": 0.9},
        ])
        recs = adapter.query({"query": "ai for science"})
        assert len(recs) == 1
        assert recs[0].available_at is None  # C 级：无逐条发布时间
        assert recs[0].payload["text"] == "body"
        assert captured["body"]["api_key"] == "tvly-test"  # key 进请求体
        assert captured["body"]["search_depth"] == "basic"

    def test_capability_is_pit_c(self):
        from finance_agent.gateway.adapters.tavily import TavilySearchAdapter

        assert TavilySearchAdapter(api_key="k").capability().pit_grade is PitGrade.C

    def test_missing_key_fail_closed(self):
        from finance_agent.gateway.adapters.tavily import TavilySearchAdapter

        adapter = TavilySearchAdapter(env={})
        assert adapter.configured is False
        with pytest.raises(RuntimeError, match="TAVILY_API_KEY"):
            adapter.query({"query": "x"})


class TestPerRecordPitGrade:
    def test_undated_b_grade_record_chunk_downgrades_to_c(self, monkeypatch):
        """B 级源的无日期记录：chunk 有效等级降 C（否则证据校验拒绝登记——
        2026-09-01 实测：Exa 约半数结果无 publishedDate，全被误拒）。"""
        import httpx

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp({"results": [
            {"title": "无日期条目", "url": "https://x.com/2"}]}))
        from finance_agent.gateway.gateway import DataGateway
        from finance_agent.gateway.tools import make_gateway_tool
        from finance_agent.research.evidence_desk import ChunkStore, verify_and_build

        gw = DataGateway(mode="live")
        gw.register(ExaSearchAdapter(api_key="k"))
        cs = ChunkStore()
        out = make_gateway_tool(gw, "web_search", cs)({"query": "x"})
        cid = json.loads(out["content"])[0]["chunk_id"]
        chunk = cs.get(cid)
        assert chunk is not None and chunk.pit_grade is PitGrade.C
        ev = verify_and_build(cs, chunk_id=cid, verbatim_quote="无日期条目")
        assert ev.pit_grade is PitGrade.C  # 登记成功（C 级允许无 available_at）

    def test_dated_b_record_keeps_b(self, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp({"results": [
            {"title": "有日期", "url": "https://x.com/1", "publishedDate": "2024-02-01T00:00:00Z"}]}))
        from finance_agent.gateway.gateway import DataGateway
        from finance_agent.gateway.tools import make_gateway_tool
        from finance_agent.research.evidence_desk import ChunkStore

        gw = DataGateway(mode="live")
        gw.register(ExaSearchAdapter(api_key="k"))
        cs = ChunkStore()
        out = make_gateway_tool(gw, "web_search", cs)({"query": "x"})
        cid = json.loads(out["content"])[0]["chunk_id"]
        assert cs.get(cid).pit_grade is PitGrade.B
