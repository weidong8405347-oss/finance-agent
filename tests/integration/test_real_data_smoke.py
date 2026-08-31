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
