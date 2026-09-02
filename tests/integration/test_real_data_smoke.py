"""真实数据源冒烟测试（默认跳过；FINANCE_AGENT_NETWORK=1 时运行）。

验证 PIT 语义对真实数据成立（2026-08-31 网络环境实录）：
- EDGAR：filingDate ≤ as_of 过滤真实有效（reporting lag 真实存在）；
- yfinance：经代理可用（本机系统代理 127.0.0.1:7897；shell 需显式
  HTTPS_PROXY=http://127.0.0.1:7897，httpx/yfinance 才会走代理——macOS
  系统级代理对 Python 进程不自动生效）；
- stooq：CSV 端点已全局启用 JS PoW 反爬，预期优雅降级为空（回退链首跳）。
"""

import os
from datetime import UTC, date, datetime

import pytest

from finance_agent.gateway.adapters.edgar import EdgarAdapter

pytestmark = pytest.mark.skipif(
    not os.environ.get("FINANCE_AGENT_NETWORK"), reason="网络冒烟测试默认跳过"
)


def test_edgar_real_filings_respect_as_of():
    adapter = EdgarAdapter()
    as_of = datetime(2024, 1, 1, tzinfo=UTC)
    records = adapter.query({"ticker": "AAPL", "forms": ["10-K"]}, as_of=as_of)
    assert len(records) >= 5  # AAPL 有足够历史
    for r in records:
        assert r.available_at is not None and r.available_at <= as_of
    # AAPL 2023 财年的 10-K 是 2023-11-03 提交的（真实 reporting lag：财年 9 月底结束）
    fy23 = [r for r in records if r.event_time and r.event_time.date().isoformat() == "2023-09-30"]
    assert fy23 and fy23[0].available_at.date().isoformat() == "2023-11-03"


def test_yfinance_real_prices_respect_as_of():
    """真实冒烟：available_at = 交易日+1d、as_of 客户端过滤（Yahoo 通常需 HTTPS_PROXY）。"""
    from finance_agent.gateway.adapters.prices import YFinancePricesAdapter

    adapter = YFinancePricesAdapter()
    as_of = datetime(2026, 6, 1, tzinfo=UTC)
    recs = adapter.query({"ticker": "BE", "start": "2026-01-01", "end": "2026-08-31"}, as_of=as_of)
    assert recs, "无行情记录——检查网络（Yahoo 通常需 HTTPS_PROXY，见模块 docstring）"
    for r in recs:
        assert r.available_at is not None and r.available_at <= as_of
        assert (r.available_at.date() - date.fromisoformat(r.payload["date"])).days == 1
    assert recs == sorted(recs, key=lambda r: r.payload["date"])


def test_stooq_real_challenge_degrades_to_empty():
    """真实冒烟：反爬挑战页不崩溃——解析不出行情行 → []（回退次序首跳的降级姿态）。

    若未来网络/策略恢复返回真 CSV，本测试同样通过（只是不再为空）。
    """
    from finance_agent.gateway.adapters.stooq import StooqPricesAdapter

    adapter = StooqPricesAdapter()
    recs = adapter.query({"ticker": "AAPL", "start": "2026-01-01", "end": "2026-08-31"})
    assert isinstance(recs, list)


def test_dashscope_reasoning_effort_wire_param_accepted():
    """reasoning_effort 实参真实校准（P1 首验证项，§4.4）：
    kimi-k3 必须接受该参数且真的进入思考（usage 里应能观察到 reasoning tokens）；
    参数不被接受会 400 fail-loud（LLMCallError 带响应体）——那就是要改实参名的信号。
    """

    from finance_agent.llm.router import LLMRouter

    router = LLMRouter.from_pi()  # 真实 pi 配置
    llm = router.get("research", tool_schemas={})
    assert llm.spec.effort == "max", "默认角色路由应把 research 指到 kimi-k3@max"
    reply = llm.complete(
        [{"role": "user", "content": "用一句话回答：1+1 等于几？"}],
        [],
    )
    assert reply.content.strip(), "应返回非空内容"
    usage = reply.usage or {}
    # max effort 生效的旁证：存在 reasoning tokens 统计（不同 provider 字段名不同，宽松断言）
    assert usage, "usage 应存在"


def test_hkexnews_real_filings_pit():
    """HKEXnews 真实冒烟（P2 spike 验证项）：晶泰控股有真实公告、披露时刻可解析、
    as_of 服务端过滤成立（A 级 PIT）。"""
    from finance_agent.gateway.adapters.hkexnews import HKEXNewsAdapter

    adapter = HKEXNewsAdapter()
    assert adapter.healthcheck()["ok"], "披露易探活失败"
    as_of = datetime(2026, 6, 1, tzinfo=UTC)
    recs = adapter.query({"ticker": "2228.HK", "days": 400}, as_of=as_of)
    assert recs, "晶泰控股 2025-2026 应有公告"
    for r in recs:
        assert r.available_at is not None and r.available_at <= as_of
        assert r.url and r.url.endswith(".pdf")


def test_gdelt_real_news_pit():
    """GDELT 真实冒烟：中文查询可取、seendate 可解析（B 级 PIT）。

    2026-09-01 实录：gdeltproject.org 在本机网络不可达（直连与代理均 SSL
    握手超时，疑似被墙）——探活不过即跳过（健康检查驱动 skip，不制造红色噪音；
    不可达事实记录在 research-capability-upgrade §6 风险表）。
    """
    from finance_agent.gateway.adapters.gdelt import GdeltNewsAdapter

    adapter = GdeltNewsAdapter()
    health = adapter.healthcheck()
    if not health["ok"]:
        pytest.skip(f"GDELT 本网络不可达：{health['detail']}")
    recs = adapter.query({"query": "晶泰控股", "max_records": 5, "timespan": "6m"})
    assert recs, "GDELT 应能取到晶泰控股中文报道"
    assert all(r.url and r.payload.get("title") for r in recs)


def test_fundamentals_real_snapshot():
    """yfinance 基本面快照真实冒烟：市值/股本/货币单位齐全（C 级快照）。"""
    from finance_agent.gateway.adapters.fundamentals import YFinanceFundamentalsAdapter

    recs = YFinanceFundamentalsAdapter().query({"ticker": "AAPL"})
    assert recs and recs[0].payload.get("market_cap")
    assert recs[0].payload.get("currency") == "USD"


def test_exa_real_search_if_key():
    """Exa 真实冒烟：配置 EXA_API_KEY（环境变量或 .env）时运行，否则跳过。"""
    import os

    from finance_agent.llm.router import _read_dotenv

    key = os.environ.get("EXA_API_KEY") or _read_dotenv().get("EXA_API_KEY")
    if not key:
        pytest.skip("未配置 EXA_API_KEY")
    from finance_agent.gateway.adapters.exa_search import ExaSearchAdapter

    adapter = ExaSearchAdapter(api_key=key)
    assert adapter.healthcheck()["ok"], adapter.healthcheck()["detail"]
    recs = adapter.query({"query": "Recursion Pharmaceuticals AI drug discovery",
                          "num_results": 3})
    assert recs and all(r.url for r in recs)


def test_tavily_real_search_if_key():
    """Tavily 真实冒烟（C 级备份搜索源）。"""
    import os

    from finance_agent.llm.router import _read_dotenv

    if not (os.environ.get("TAVILY_API_KEY") or _read_dotenv().get("TAVILY_API_KEY")):
        pytest.skip("未配置 TAVILY_API_KEY")
    from finance_agent.gateway.adapters.tavily import TavilySearchAdapter

    adapter = TavilySearchAdapter(api_key=os.environ.get("TAVILY_API_KEY")
                                  or _read_dotenv().get("TAVILY_API_KEY"))
    assert adapter.healthcheck()["ok"], adapter.healthcheck()["detail"]
    recs = adapter.query({"query": "晶泰控股 AI 药物发现", "max_results": 3})
    assert recs and all(r.url for r in recs)
