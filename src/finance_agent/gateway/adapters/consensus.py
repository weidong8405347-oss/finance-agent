"""分析师一致预期 adapter（C 级 PIT，升级方案 §25/§26 Expectations Layer）。

数据源：yfinance 的分析师估计族（earnings_estimate / revenue_estimate /
eps_trend / eps_revisions / growth_estimates）——当前快照值。

PIT 语义（诚实声明，与 fundamentals adapter 同级）：
- 一致预期是「当前快照」，无历史回溯保证 → C 级（available_at=None；
  生产模式可用，评估模式 fail-closed 禁用）；
- eps_trend 的 7/30/60/90 天前值是 yfinance 当前提供的对比快照，
  不是可审计的历史库——revision 分析只在生产模式使用；
- 严肃历史口径以监管披露（A 级）为准；本源用于「市场当前预期是什么」。

产出记录：一条 DataRecord，payload 含 estimates/eps_trend/eps_revisions/
growth_estimates 四个表（原样数值，不换算）；空结果 = 无覆盖（不编造）。
"""

from __future__ import annotations

from datetime import datetime  # noqa: F401 - query 签名的 as_of 形参用
from typing import Any

from ...knowledge.models import PitGrade
from ..models import DataRecord, SourceCapability

#: 期间键 → 可读标签（0q=本季度，+1y=下一财年；模型据此登记 metric 的 period）
PERIOD_LABELS = {
    "0q": "本季度", "+1q": "下一季度", "0y": "本财年", "+1y": "下一财年", "+5y": "未来5年",
    "-5y": "过去5年",
}


def _frame_records(df: Any, *, fields: list[str]) -> list[dict[str, Any]]:
    """yfinance 估计 DataFrame → 记录列表（期间 × 字段；原样数值不换算）。

    索引为期间键（0q/+1q/0y/+1y）；缺列跳过；NaN 转 None（不落脏值）。
    """
    if df is None or getattr(df, "empty", True):
        return []
    out: list[dict[str, Any]] = []
    for period, row in df.iterrows():
        rec: dict[str, Any] = {
            "period": str(period),
            "period_label": PERIOD_LABELS.get(str(period), str(period)),
        }
        for f in fields:
            if f not in df.columns:
                continue
            v = row[f]
            try:
                # NaN/inf → None（不编造 0）
                if v is None or v != v or v in (float("inf"), float("-inf")):
                    rec[f] = None
                else:
                    rec[f] = float(v) if isinstance(v, float) else v
            except (TypeError, ValueError):
                rec[f] = None
        out.append(rec)
    return out


class YFinanceConsensusAdapter:
    """美股为主的一致预期快照（yfinance；港股/其他市场无覆盖 → 空结果）。"""

    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="consensus_yf",
            pit_grade=PitGrade.C,
            server_side_asof=False,
            description="yfinance 分析师一致预期快照（EPS/收入估计 + 修订方向 + 增长预期）；"
                        "当前值无 PIT → C 级，评估模式禁用",
        )

    def healthcheck(self) -> dict:
        try:
            recs = self.query({"ticker": "AAPL"})
            if recs and recs[0].payload.get("earnings_estimate"):
                return {"ok": True, "detail": "yfinance 一致预期可取"}
            return {"ok": False, "detail": "空结果或缺 earnings_estimate"}
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:  # noqa: ARG002 - C 级源无 as_of 语义
        import yfinance as yf  # lazy

        ticker = str(request["ticker"])
        t = yf.Ticker(ticker)
        # 四个估计族分别容错：一族失败不拖死其余（部分覆盖优于全丢）
        payload: dict[str, Any] = {"ticker": ticker}
        errors: dict[str, str] = {}
        for attr, fields in (
            ("earnings_estimate", ["avg", "low", "high", "year_ago_eps",
                                   "number_of_analysts", "growth"]),
            ("revenue_estimate", ["avg", "low", "high", "year_ago_revenue",
                                  "number_of_analysts", "sales_growth"]),
            ("eps_trend", ["current", "7days_ago", "30days_ago", "60days_ago", "90days_ago"]),
            ("eps_revisions", ["up_last_7days", "up_last_30days",
                               "down_last_7days", "down_last_30days", "down_last_90days"]),
            ("growth_estimates", ["stock", "industry", "sector", "index"]),
        ):
            try:
                payload[attr] = _frame_records(getattr(t, attr, None), fields=fields)
            except Exception as e:  # noqa: BLE001 - 单族失败降级可见
                payload[attr] = []
                errors[attr] = f"{type(e).__name__}: {e}"
        if errors:
            payload["partial_errors"] = errors
        if not any(payload[attr] for attr in
                   ("earnings_estimate", "revenue_estimate", "eps_trend")):
            return []  # 无覆盖/无效 ticker → 空结果（不编造一致预期）
        return [
            DataRecord(
                source_id="consensus_yf",
                payload=payload,
                available_at=None,  # C 级快照：无 PIT 保证（评估模式网关拒用）
                url=f"https://finance.yahoo.com/quote/{ticker}/analysis",
            )
        ]


class FixtureConsensusAdapter:
    """测试/演示夹具（与 fixture.py 同纪律）：固定 payload，不访问网络。"""

    def __init__(self, payload: dict[str, Any] | None = None):
        self._payload = payload or {}

    def capability(self) -> SourceCapability:
        return SourceCapability(
            source_id="consensus_fixture",
            pit_grade=PitGrade.C,
            server_side_asof=False,
            description="一致预期夹具（测试用）",
        )

    def healthcheck(self) -> dict:
        return {"ok": True, "detail": "fixture"}

    def query(self, request: dict, as_of: datetime | None = None) -> list[DataRecord]:  # noqa: ARG002
        if not self._payload:
            return []
        return [
            DataRecord(
                source_id="consensus_fixture",
                payload={**self._payload, "ticker": str(request.get("ticker", ""))},
                available_at=None,
                url="fixture://consensus",
            )
        ]


__all__ = ["YFinanceConsensusAdapter", "FixtureConsensusAdapter", "PERIOD_LABELS"]
