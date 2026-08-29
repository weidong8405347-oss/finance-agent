"""成本模型：强制 hook（DESIGN.md §6.3 协议层——手续费/滑点不可关闭）。

参数进 run manifest 审计；回测引擎内不可绕过。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CostModel(BaseModel, frozen=True):
    commission_bps: float = Field(default=5.0, ge=0)  # 单边佣金（万分之一 = 1bp）
    slippage_bps: float = Field(default=10.0, ge=0)  # 单边滑点

    def round_trip_cost_rate(self) -> float:
        """双边成本率（买 + 卖）。"""
        return 2.0 * (self.commission_bps + self.slippage_bps) / 1e4

    def net_return(self, gross: float, *, sizing_pct: float) -> float:
        """仓位加权净收益：毛利先扣双边成本，再乘仓位。"""
        return (gross - self.round_trip_cost_rate()) * sizing_pct
