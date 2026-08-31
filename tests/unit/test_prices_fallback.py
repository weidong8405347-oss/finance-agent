"""行情源可用性容错（2026-08-31 真实首跑暴露的缺口）。

背景：stooq CSV 端点已全局启用 JS PoW 反爬（换网络也不通，不绕过）；
yfinance 经代理可用，但其 1.7 返回 (Price, Ticker) 双层列，adapter 骨架
从未真跑过 → 解析崩溃。且图表存档与 eval 对账都硬编码 stooq 优先、无回退。

契约：
- YFinancePricesAdapter 兼容 yfinance 1.7 双层列（PIT 语义不变：available_at = 交易日+1d）
- DataGateway.query_any：按序回退（空结果/异常/未注册 → 下一源）
- PriceBook.from_gateway：多源回退装配；require=True 时数据全缺 → 显式失败（铁律 4）
- 档案图表：stooq 空结果时回退 yfinance，HTML 存档仍有价格图
"""

from datetime import UTC, datetime

import pytest
from test_commands import (
    RESEARCH_SCRIPT,
    SYNTHESIZE_SCRIPT,
    make_deps,
    run_command,
)

from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.adapters.fixture import FixtureAdapter
from finance_agent.gateway.gateway import DataGateway
from finance_agent.gateway.models import DataRecord, SourceCapability
from finance_agent.knowledge.models import PitGrade


def _gw(tmp_path) -> DataGateway:
    events = EventStore(tmp_path / "e.db")
    return DataGateway(mode="live", events=events, run_id="live-test")


def _price_cap(source_id: str) -> SourceCapability:
    return SourceCapability(
        source_id=source_id, pit_grade=PitGrade.A,
        server_side_asof=True, description="夹具行情源",
    )


def _bar(source_id: str, ticker: str, day: str, close: float) -> DataRecord:
    return DataRecord(
        source_id=source_id,
        payload={"ticker": ticker, "date": day, "open": close, "high": close,
                 "low": close, "close": close, "volume": 1000},
        available_at=datetime.fromisoformat(day).replace(tzinfo=UTC),
    )


# ---------------- yfinance 1.7 双层列解析 ----------------

def test_yfinance_adapter_parses_v17_multiindex_frame_and_respects_as_of(monkeypatch):
    """yfinance 1.7 的 yf.download 返回 (Price, Ticker) 双层列 DataFrame——adapter 必须兼容。"""
    pd = pytest.importorskip("pandas")  # 仅 data extra 环境可跑（核心测试不依赖 pandas）

    idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
    cols = pd.MultiIndex.from_product(
        [["Adj Close", "Close", "High", "Low", "Open", "Volume"], ["BE"]],
        names=["Price", "Ticker"],
    )
    df = pd.DataFrame(
        [[10.5, 10.5, 11, 9, 10, 1000], [11.0, 11.0, 11.5, 10, 10.5, 1200]],
        index=idx, columns=cols,
    )
    monkeypatch.setattr("yfinance.download", lambda *a, **kw: df)

    from finance_agent.gateway.adapters.prices import YFinancePricesAdapter

    adapter = YFinancePricesAdapter()
    recs = adapter.query({"ticker": "BE"})
    assert len(recs) == 2
    assert recs[0].payload["close"] == 10.5 and recs[0].payload["date"] == "2024-01-02"
    assert recs[0].available_at == datetime(2024, 1, 3, tzinfo=UTC)  # 交易日 +1d
    assert recs[0].payload["volume"] == 1000
    # as_of 过滤：1 月 3 日 0 点（严格小于首条 available_at）→ 空
    cut = adapter.query({"ticker": "BE"}, as_of=datetime(2024, 1, 3, tzinfo=UTC))
    assert len(cut) == 1


def test_yfinance_adapter_handles_empty_download(monkeypatch):
    """下载失败（限流/无数据）返回空 DataFrame → 空列表，不崩溃。"""
    pd = pytest.importorskip("pandas")
    monkeypatch.setattr("yfinance.download", lambda *a, **kw: pd.DataFrame())
    from finance_agent.gateway.adapters.prices import YFinancePricesAdapter

    assert YFinancePricesAdapter().query({"ticker": "BE"}) == []


# ---------------- DataGateway.query_any：按序回退 ----------------

# ---------------- stooq 可用性漂移降级 ----------------

def test_stooq_http_error_degrades_to_empty(monkeypatch):
    """HTTP 层故障（404/限流/网络）→ 空结果不抛错（回退链首跳的降级姿态）。"""
    import httpx

    from finance_agent.gateway.adapters.stooq import StooqPricesAdapter

    req = httpx.Request("GET", "https://stooq.com/q/d/l/")

    def raise_http_error(*a, **kw):
        raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404))

    monkeypatch.setattr(httpx, "get", raise_http_error)
    assert StooqPricesAdapter().query({"ticker": "AAPL"}) == []


def test_query_any_falls_back_on_empty_then_error(tmp_path):
    g = _gw(tmp_path)
    g.register(FixtureAdapter(_price_cap("prices_stooq"), records=[]))  # 首选空（反爬现状）
    boom = FixtureAdapter(_price_cap("prices_err"), records=[])

    def raise_always(request, as_of):  # 中间源抛错 → 继续回退
        raise RuntimeError("boom")

    boom.records = raise_always
    g.register(boom)
    g.register(FixtureAdapter(
        _price_cap("prices"),
        records=[_bar("prices", "BE", "2026-08-20", 100.0)],
    ))

    from finance_agent.gateway.adapters.prices import PRICE_SOURCE_ORDER

    recs = g.query_any(("prices_stooq", "prices_err", "prices"), {"ticker": "BE"})
    assert len(recs) == 1 and recs[0].source_id == "prices"
    # 常规次序（stooq 空 → yfinance）
    assert g.query_any(PRICE_SOURCE_ORDER, {"ticker": "BE"})[0].source_id == "prices"


def test_query_any_all_unavailable_returns_empty(tmp_path):
    """全部源为空/未注册 → []（调用方对空结果自行降级，不抛错）。"""
    g = _gw(tmp_path)
    g.register(FixtureAdapter(_price_cap("prices_stooq"), records=[]))
    assert g.query_any(("prices_stooq", "prices"), {"ticker": "BE"}) == []
    assert g.query_any(("prices",), {"ticker": "BE"}) == []  # 未注册 = 能力不存在


# ---------------- PriceBook.from_gateway：eval 对账数据装配 ----------------

def test_price_book_from_gateway_falls_back_and_requires(tmp_path):
    from finance_agent.evaluation.prices import PriceBook, PriceDataUnavailableError

    g = _gw(tmp_path)
    g.register(FixtureAdapter(_price_cap("prices_stooq"), records=[]))
    be_bars = [
        _bar("prices", "BE", "2026-01-06", 100.0),
        _bar("prices", "BE", "2026-04-06", 110.0),
    ]
    g.register(FixtureAdapter(
        _price_cap("prices"),
        records=lambda request, as_of: be_bars if request.get("ticker") == "BE" else [],
    ))

    book = PriceBook.from_gateway(g, ["BE"])
    fwd = book.forward_return("BE", __import__("datetime").date(2026, 1, 5), horizon_months=3)
    assert fwd is not None and fwd[2] == pytest.approx(0.10)  # 110/100 - 1

    # require=True：任一标的行情全缺 → 显式失败（eval 预算不该被静默烧掉）
    with pytest.raises(PriceDataUnavailableError, match="AAPL"):
        PriceBook.from_gateway(g, ["BE", "AAPL"], require=True)


# ---------------- 档案图表：stooq 空结果回退 yfinance ----------------

def test_archive_chart_falls_back_when_stooq_empty(tmp_path):
    """stooq（首选源）空结果 → 回退 yfinance → HTML 存档仍含价格图。"""
    deps, events, _, _ = make_deps(tmp_path, {"research": [RESEARCH_SCRIPT, SYNTHESIZE_SCRIPT]})
    deps.gateway.register(FixtureAdapter(_price_cap("prices_stooq"), records=[]))
    deps.gateway.register(FixtureAdapter(
        _price_cap("prices"),
        records=[_bar("prices", "BE", "2026-08-20", 100.0),
                 _bar("prices", "BE", "2026-08-21", 101.0)],
    ))

    run_command(deps, events, "/research BE")
    archive_dir = tmp_path / "knowledge" / "stocks" / "BE" / "archive"
    html = (archive_dir / "latest.html").read_text()
    assert "<svg" in html, "行情回退生效后，存档应渲染价格图"
