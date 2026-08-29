"""真实数据源冒烟测试（默认跳过；FINANCE_AGENT_NETWORK=1 时运行）。

验证 EDGAR adapter 的 PIT 语义对真实数据成立：
filingDate ≤ as_of 的过滤在真实 API 上行为正确（reporting lag 真实存在）。
"""

import os
from datetime import UTC, datetime

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
