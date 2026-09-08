"""CalculationService：有输入引用的确定性计算（设计 §8.1/§8.3）。

与旧 calc 工具（裸数字入参）的区别：
- 入参是 InputRef（observation/fact/calculation/assumption），observation 引用
  会从 MetricStore 解析并核对值——图上计算值可完整重算与回指；
- 公式注册表版本化（formula_id@version），输出记录 input_hash 幂等；
- 全部 Decimal；四舍五入只在展示末端；失败不返回貌似有效的 0——
  status=failed/not_meaningful + 明确原因（零分母、WACC≤终值增速、无根…）；
- 计算即事件：calculation/completed 携带完整 payload。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..eventstore.events import CALCULATION_COMPLETED, Event
from ..eventstore.store import EventStore
from ..knowledge.metric_store import MetricStore

logger = logging.getLogger("finance_agent.research.calculations")

CalculationStatus = Literal["ok", "failed", "not_meaningful"]


class InputRef(BaseModel):
    """一个计算输入的引用：kind=observation/fact/calculation 时 ref_id 必填。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["observation", "fact", "calculation", "assumption", "market_data"]
    label: str
    value: str | None = None  # 十进制字符串；observation/fact 引用由服务端解析覆盖
    ref_id: str | None = None
    unit: str = ""
    currency: str | None = None

    def resolved_value(self) -> Decimal:
        if self.value is None:
            raise CalculationError(f"输入 {self.label!r} 无值（引用未解析成功）")
        try:
            return Decimal(self.value)
        except InvalidOperation as e:
            raise CalculationError(f"输入 {self.label!r} 的值不可解析为 Decimal: {self.value!r}") from e


class CalculationError(Exception):
    """计算输入/公式错误（fail-loud，不产出貌似有效的结果）。"""


class FormulaOutcome(BaseModel):
    result: str | None = None
    unit: str = ""
    currency: str | None = None
    status: CalculationStatus = "ok"
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)  # 敏感性表/中间量


class CalculationResult(BaseModel):
    calculation_id: str
    entity_kind: str
    entity_id: str
    formula_id: str
    formula_version: int
    input_refs: list[InputRef]
    assumptions: dict[str, str] = Field(default_factory=dict)
    result: str | None
    unit: str
    currency: str | None = None
    status: CalculationStatus
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    input_hash: str
    created_at: datetime
    run_id: str | None = None
    namespace: str = "prod"

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v).replace(",", ""))
    except InvalidOperation as e:
        raise CalculationError(f"非法数值: {v!r}") from e


def _ratio(num: Decimal, den: Decimal, *, label: str) -> tuple[Decimal | None, str | None]:
    if den == 0:
        return None, f"{label}: 分母为零 → 不可计算（N/M）"
    if den < 0:
        return None, f"{label}: 分母为负 → 常规比率不适用（N/M）"
    return num / den, None


# ---------------- 公式目录（版本化；§8.1 首批 + §8.3 Reverse DCF） ----------------

FormulaFn = Callable[[dict[str, Decimal], dict[str, str]], FormulaOutcome]


def _f_unit_conversion(i: dict[str, Decimal], a: dict[str, str]) -> FormulaOutcome:
    from ..knowledge.normalization import NormalizationStep, apply_step

    value = i["value"]
    steps = json.loads(a.get("steps", "[]"))
    for s in steps:
        value = apply_step(value, NormalizationStep.model_validate(s))
    return FormulaOutcome(result=str(value), unit=a.get("target_unit", ""))


def _f_yoy_growth(i: dict[str, Decimal], a: dict[str, str]) -> FormulaOutcome:
    cur, prior = i["current"], i["prior"]
    if prior <= 0:
        return FormulaOutcome(
            status="not_meaningful",
            error=f"基期为 {'零' if prior == 0 else '负'}（{prior}）→ 常规同比不适用（N/M），不自动显示",
        )
    g = (cur - prior) / prior
    return FormulaOutcome(result=str(g), unit="ratio")


def _f_cagr(i: dict[str, Decimal], a: dict[str, str]) -> FormulaOutcome:
    begin, end, years = i["begin"], i["end"], i["years"]
    if years <= 0:
        return FormulaOutcome(status="failed", error="years 必须为正")
    if begin <= 0 or end <= 0:
        return FormulaOutcome(
            status="not_meaningful", error="CAGR 要求起止值均为正（负基数增长不适用）"
        )
    # Decimal 无幂运算：用 ln/exp 经 float 收敛后回到 Decimal（记录精度警示）
    rate = (float(end) / float(begin)) ** (1.0 / float(years)) - 1.0
    return FormulaOutcome(
        result=str(Decimal(repr(rate))), unit="ratio",
        warnings=["CAGR 经浮点收敛（Decimal 无幂运算），展示前按需舍入"],
    )


def _f_margin(i: dict[str, Decimal], a: dict[str, str]) -> FormulaOutcome:
    kind = a.get("margin_kind", "net")
    numerator = i[_MARGIN_NUMERATORS[kind]]  # 惰性选择（_resolve_inputs 已验存在性）
    revenue = i["revenue"]
    r, err = _ratio(numerator, revenue, label=f"{kind}_margin")
    if r is None:
        return FormulaOutcome(status="not_meaningful", error=err)
    return FormulaOutcome(result=str(r), unit="ratio")


def _f_fcf_from_cfo(i: dict[str, Decimal], a: dict[str, str]) -> FormulaOutcome:
    fcf = i["cfo"] - i["capex"]
    return FormulaOutcome(
        result=str(fcf), unit=a.get("unit", ""),
        warnings=["CFO−Capex ≠ FCFF（未调整税/非现金/营运资本口径差异）；不得直接搭配 WACC 折现"],
        extra={"cfo": str(i["cfo"]), "capex": str(i["capex"])},
    )


def _f_net_debt(i: dict[str, Decimal], a: dict[str, str]) -> FormulaOutcome:
    # 缺失输入不允许默认为零（§8.1）：debt 与 cash 都必须显式给出
    return FormulaOutcome(
        result=str(i["total_debt"] - i["cash"]), unit=a.get("unit", ""),
        extra={"total_debt": str(i["total_debt"]), "cash": str(i["cash"])},
    )


def _f_enterprise_value(i: dict[str, Decimal], a: dict[str, str]) -> FormulaOutcome:
    return FormulaOutcome(result=str(i["market_cap"] + i["net_debt"]), unit=a.get("unit", ""))


def _f_share_dilution(i: dict[str, Decimal], a: dict[str, str]) -> FormulaOutcome:
    cur, prior = i["current_shares"], i["prior_shares"]
    if prior <= 0:
        return FormulaOutcome(status="not_meaningful", error="基期股数必须为正")
    return FormulaOutcome(result=str((cur - prior) / prior), unit="ratio")


def _f_guidance_delta(i: dict[str, Decimal], a: dict[str, str]) -> FormulaOutcome:
    """实际 vs 指引区间：落点 + 上下界差距；中点比较必须显式标注（§8.2）。"""
    actual, lo, hi = i["actual"], i["guidance_low"], i["guidance_high"]
    if lo > hi:
        return FormulaOutcome(status="failed", error="指引下界大于上界")
    mid = (lo + hi) / 2
    if actual > hi:
        landing = "beat"
    elif actual < lo:
        landing = "miss"
    else:
        landing = "in_line"
    return FormulaOutcome(
        result=str(actual - mid), unit=a.get("unit", ""),
        extra={
            "landing": landing,
            "vs_low": str(actual - lo),
            "vs_high": str(actual - hi),
            "vs_mid": str(actual - mid),
            "midpoint_note": "result 为与区间中点的差；中点比较是简化口径（已标注）",
        },
    )


def _f_ttm_sum(i: dict[str, Decimal], a: dict[str, str]) -> FormulaOutcome:
    """TTM = 四个连续同口径季度求和（连续性由 CalculationService.ttm_* 校验）。"""
    quarters = [i["q1"], i["q2"], i["q3"], i["q4"]]
    return FormulaOutcome(
        result=str(sum(quarters)), unit=a.get("unit", ""),
        extra={"quarters": [str(q) for q in quarters]},
    )


# ---- Reverse DCF（§8.3）：FCFF 模型，反向求解隐含增长 ----

_DCF_REQUIRED_ASSUMPTIONS = ("wacc", "terminal_g", "tax_rate", "years", "target_ev")


def _compound(base: Decimal, rate: Decimal, t: int) -> Decimal:
    """Decimal 复利：base × (1+rate)^t，整数幂精确展开（t ≤ 40，性能可接受）。"""
    if t > 40:
        raise CalculationError(f"复利期数 {t} 超出受控范围（≤40）")
    factor = Decimal(1)
    r = 1 + rate
    for _ in range(t):
        factor *= r
    return base * factor


def _fcff(rev: Decimal, prev_rev: Decimal, a: dict[str, Decimal]) -> Decimal:
    """FCFF_t = EBIT_t×(1−tax) + D&A_t − Capex_t − ΔNWC_t（§8.3 模型定义）。"""
    ebit = rev * a["ebit_margin"]
    da = rev * a["da_ratio"]
    capex = rev * a["capex_ratio"]
    dnwc = (rev - prev_rev) * a["nwc_ratio"]
    return ebit * (1 - a["tax_rate"]) + da - capex - dnwc


def _dcf_ev(revenue0: Decimal, g: Decimal, a: dict[str, Decimal]) -> Decimal:
    """给定增长 g 的 EV_model（驱动因子形式：margin/再投资比例固定于假设）。"""
    wacc, tg = a["wacc"], a["terminal_g"]
    n = int(a["years"])
    if wacc <= tg:
        raise CalculationError(f"WACC({wacc}) ≤ 终值增速({tg}) → 永续增长终值发散，模型禁用")
    ev = Decimal(0)
    prev_rev = revenue0
    for t in range(1, n + 1):
        rev = _compound(revenue0, g, t)
        ev += _fcff(rev, prev_rev, a) / _compound(Decimal(1), wacc, t)
        prev_rev = rev
    # 终值：TV_N = FCFF_(N+1) / (WACC − g_terminal)，FCFF_(N+1) 以终值增速外推
    rev_n1 = prev_rev * (1 + tg)
    tv = _fcff(rev_n1, prev_rev, a) / (wacc - tg)
    return ev + tv / _compound(Decimal(1), wacc, n)


def _f_reverse_dcf(i: dict[str, Decimal], a_raw: dict[str, str]) -> FormulaOutcome:
    """反向求解：固定 margin/税率/再投资/WACC/终值假设，只求一个变量 g，
    使 EV_model(g) 匹配经调整的市场 EV——「价格隐含的增长率」。

    模式：
    - driver（默认）：ebit_margin/da_ratio/capex_ratio/nwc_ratio 驱动 FCFF；
    - fcf_margin：FCFF = 收入 × fcf_margin（简化，必须标注为「FCFF 收入占比假设」）。
    """
    required = {k: a_raw.get(k) for k in _DCF_REQUIRED_ASSUMPTIONS}
    missing = [k for k, v in required.items() if v in (None, "")]
    if missing:
        return FormulaOutcome(status="failed", error=f"reverse_dcf 缺假设: {missing}")
    try:
        a = {k: _dec(v) for k, v in a_raw.items() if k not in ("mode", "unit", "currency")}
    except CalculationError as e:
        return FormulaOutcome(status="failed", error=str(e))
    mode = a_raw.get("mode", "driver")
    revenue0 = i["revenue_0"]
    net_debt = i.get("net_debt")
    target_ev = a["target_ev"]
    warnings: list[str] = []
    if revenue0 <= 0:
        return FormulaOutcome(status="failed", error="revenue_0 必须为正（亏损公司请用适用性检查禁用本模型）")
    if a["wacc"] <= a["terminal_g"]:
        return FormulaOutcome(
            status="failed",
            error=f"WACC({a['wacc']}) ≤ 终值增速({a['terminal_g']}) → 永续终值发散，禁用（§8.3）",
        )
    if mode == "fcf_margin":
        if "fcf_margin" not in a:
            return FormulaOutcome(status="failed", error="fcf_margin 模式必须给出 fcf_margin 假设")
        warnings.append("简化模式：FCFF 按「收入占比假设」计算，与报表 CFO−Capex 存在口径差别（已标注）")
        folded = (
            a["fcf_margin"] / (1 - a["tax_rate"]) if a["tax_rate"] != 1 else a["fcf_margin"]
        )
        a.update({
            "ebit_margin": folded,
            "da_ratio": Decimal(0), "capex_ratio": Decimal(0), "nwc_ratio": Decimal(0),
        })
        # fcf_margin 模式下 FCFF_t = rev_t × fcf_margin：用 ebit×(1−tax) 表达
        warnings.append("fcf_margin 模式经 ebit_margin = fcf_margin/(1−tax_rate) 折算（税盾已中性化）")
    else:
        for k in ("ebit_margin", "da_ratio", "capex_ratio", "nwc_ratio"):
            if k not in a:
                return FormulaOutcome(status="failed", error=f"driver 模式缺假设 {k}")

    def ev_of(g: Decimal) -> Decimal:
        return _dcf_ev(revenue0, g, a)

    # 有界二分求解（§8.3：记录求解区间、误差与收敛状态）
    g_lo, g_hi = _dec(a_raw.get("g_min", "-0.2")), _dec(a_raw.get("g_max", "0.6"))
    tol = _dec(a_raw.get("tolerance", "0.0001"))
    try:
        f_lo, f_hi = ev_of(g_lo), ev_of(g_hi)
    except (CalculationError, InvalidOperation, OverflowError) as e:
        return FormulaOutcome(status="failed", error=f"EV 函数在边界不可计算: {e}")
    if not (f_lo.is_finite() and f_hi.is_finite()):
        return FormulaOutcome(status="failed", error="EV 函数在求解区间出现非有限数")
    if f_lo > target_ev or f_hi < target_ev:
        return FormulaOutcome(
            status="failed",
            error=(
                f"目标 EV {target_ev} 不在可解区间：EV(g={g_lo})={f_lo.quantize(Decimal(1))}，"
                f"EV(g={g_hi})={f_hi.quantize(Decimal(1))}——扩大 g_min/g_max 或检查输入"
            ),
            extra={"ev_at_g_min": str(f_lo), "ev_at_g_max": str(f_hi)},
        )
    lo, hi = g_lo, g_hi
    g_mid = lo
    for _ in range(200):
        g_mid = (lo + hi) / 2
        f_mid = ev_of(g_mid)
        if abs(f_mid - target_ev) <= abs(target_ev) * tol:
            break
        if f_mid < target_ev:
            lo = g_mid
        else:
            hi = g_mid
    else:
        warnings.append(f"二分未在 200 次内收敛到容差 {tol}（当前区间 [{lo}, {hi}]）")
    implied_ev = ev_of(g_mid)
    equity = implied_ev - net_debt if net_debt is not None else None
    if net_debt is None:
        warnings.append("未提供 net_debt 引用 → 不输出 Equity_model（净债务未知不默认为零）")
    else:
        warnings.append("Equity_model 仅扣除 net_debt；优先股/少数股东/非经营资产未纳入（需要时显式登记）")
    extra: dict[str, Any] = {
        "solve_interval": [str(g_lo), str(g_hi)],
        "final_bracket": [str(lo), str(hi)],
        "tolerance": str(tol),
        "ev_at_solution": str(implied_ev),
        "target_ev": str(target_ev),
        "residual": str(implied_ev - target_ev),
        "equity_model": str(equity) if equity is not None else None,
        "mode": mode,
    }
    return FormulaOutcome(
        result=str(g_mid), unit="ratio", currency=a_raw.get("currency"),
        warnings=warnings, extra=extra,
    )


def _f_sensitivity_grid(i: dict[str, Decimal], a_raw: dict[str, str]) -> FormulaOutcome:
    """二维敏感性表（§8.3 初版）：增长×WACC 或 WACC×终值增长。所有格子继承同一输入。"""
    axis_x = [ _dec(x) for x in a_raw.get("axis_x", "").split(",") if x.strip() ]
    axis_y = [ _dec(y) for y in a_raw.get("axis_y", "").split(",") if y.strip() ]
    vary_x = a_raw.get("vary_x", "g")
    vary_y = a_raw.get("vary_y", "wacc")
    if not axis_x or not axis_y:
        return FormulaOutcome(status="failed", error="sensitivity_grid 需要 axis_x/axis_y（逗号分隔十进制）")
    if vary_x not in ("g", "wacc", "terminal_g", "ebit_margin") or vary_y not in (
        "g", "wacc", "terminal_g", "ebit_margin"
    ):
        return FormulaOutcome(status="failed", error="可变轴仅支持 g/wacc/terminal_g/ebit_margin")
    try:
        base = {k: _dec(v) for k, v in a_raw.items()
                if k not in ("axis_x", "axis_y", "vary_x", "vary_y", "unit", "currency", "mode")}
    except CalculationError as e:
        return FormulaOutcome(status="failed", error=str(e))
    revenue0 = i["revenue_0"]
    cells: list[dict[str, Any]] = []
    failed = 0
    for y in axis_y:
        for x in axis_x:
            params = dict(base)
            params[vary_x] = x
            params[vary_y] = y
            if params["wacc"] <= params["terminal_g"]:
                cells.append({"x": str(x), "y": str(y), "ev": None, "error": "WACC ≤ terminal_g"})
                failed += 1
                continue
            try:
                ev = _dcf_ev(revenue0, params.get("g", base.get("g", Decimal(0))), params)
                cells.append({"x": str(x), "y": str(y), "ev": str(ev.quantize(Decimal(1)))})
            except (CalculationError, InvalidOperation, OverflowError) as e:
                cells.append({"x": str(x), "y": str(y), "ev": None, "error": str(e)})
                failed += 1
    return FormulaOutcome(
        result=None, unit=a_raw.get("unit", ""),
        status="ok" if failed < len(cells) else "failed",
        error="全部格子不可计算" if failed == len(cells) else None,
        extra={"vary_x": vary_x, "vary_y": vary_y, "cells": cells},
    )


#: 公式注册表：formula_id → (version, fn, required_inputs)
FORMULA_REGISTRY: dict[str, tuple[int, FormulaFn, tuple[str, ...]]] = {
    "unit_conversion": (1, _f_unit_conversion, ("value",)),
    "yoy_growth": (1, _f_yoy_growth, ("current", "prior")),
    "cagr": (1, _f_cagr, ("begin", "end", "years")),
    "margin": (1, _f_margin, ("revenue",)),
    "fcf_from_cfo": (1, _f_fcf_from_cfo, ("cfo", "capex")),
    "net_debt": (1, _f_net_debt, ("total_debt", "cash")),
    "enterprise_value": (1, _f_enterprise_value, ("market_cap", "net_debt")),
    "share_dilution": (1, _f_share_dilution, ("current_shares", "prior_shares")),
    "guidance_delta": (1, _f_guidance_delta, ("actual", "guidance_low", "guidance_high")),
    "ttm_sum": (1, _f_ttm_sum, ("q1", "q2", "q3", "q4")),
    "reverse_dcf": (1, _f_reverse_dcf, ("revenue_0",)),
    "sensitivity_grid": (1, _f_sensitivity_grid, ("revenue_0",)),
}

#: margin 公式按 margin_kind 需要额外分子输入
_MARGIN_NUMERATORS = {"gross": "gross_profit", "operating": "operating_income", "net": "net_income"}


class CalculationService:
    """确定性计算服务：解析输入引用 → 执行公式 → 落库 + 落事件。"""

    def __init__(self, store: MetricStore, events: EventStore | None = None):
        self._store = store
        self._events = events

    def calculate(
        self,
        *,
        entity_kind: str,
        entity_id: str,
        formula_id: str,
        inputs: list[InputRef],
        assumptions: dict[str, str] | None = None,
        run_id: str | None = None,
        namespace: str = "prod",
        now: datetime | None = None,
        as_of: datetime | None = None,
    ) -> CalculationResult:
        """确定性计算。引用纪律（review #4/#14）：

        - observation/calculation 引用必须与本次计算同命名空间、同实体，
          且（给出 as_of 时）在截止时点前可知；
        - 跨币种输入直接拒绝（需先经显式 fx_convert 换算链）；
        - ttm_sum 只接受四个连续同口径季度观测引用（堵通用入口绕过连续性检查）。
        """
        entry = FORMULA_REGISTRY.get(formula_id)
        if entry is None:
            raise CalculationError(f"未知公式 {formula_id!r}（注册表：{sorted(FORMULA_REGISTRY)}）")
        version, fn, required = entry
        assumptions = dict(assumptions or {})
        named, resolved_meta = self._resolve_inputs(
            inputs, formula_id, required, assumptions,
            namespace=namespace, entity_kind=entity_kind, entity_id=entity_id, as_of=as_of,
        )
        self._assert_currency_consistent(inputs, resolved_meta, formula_id)
        if formula_id == "ttm_sum":
            self._assert_ttm_inputs(inputs, resolved_meta)
        created = now or datetime.now(UTC)
        input_hash = self._input_hash(
            formula_id, version, inputs, resolved_meta, assumptions,
            entity_kind=entity_kind, entity_id=entity_id, namespace=namespace,
        )
        calculation_id = f"calc-{uuid.uuid4().hex[:12]}"
        try:
            outcome = fn(named, assumptions)
        except CalculationError as e:
            outcome = FormulaOutcome(status="failed", error=str(e))
        except (InvalidOperation, OverflowError, ZeroDivisionError, ArithmeticError) as e:
            outcome = FormulaOutcome(status="failed", error=f"{type(e).__name__}: {e}")
        result = CalculationResult(
            calculation_id=calculation_id,
            entity_kind=entity_kind,
            entity_id=entity_id,
            formula_id=formula_id,
            formula_version=version,
            input_refs=inputs,
            assumptions=assumptions,
            result=outcome.result,
            unit=outcome.unit or assumptions.get("unit", ""),
            currency=outcome.currency or assumptions.get("currency"),
            status=outcome.status,
            warnings=outcome.warnings,
            error=outcome.error,
            extra=outcome.extra,
            input_hash=input_hash,
            created_at=created,
            run_id=run_id,
            namespace=namespace,
        )
        stored_id = self._store.save_calculation(
            calculation_id=calculation_id, namespace=namespace,
            entity_kind=entity_kind, entity_id=entity_id,
            formula_id=formula_id, formula_version=version,
            status=outcome.status, result=outcome.result,
            unit=result.unit, input_hash=input_hash,
            created_at=created, run_id=run_id,
            payload=result.to_payload(),
        )
        result.calculation_id = stored_id
        if self._events is not None:
            self._events.append(Event(
                run_id=run_id or "calc",
                type=CALCULATION_COMPLETED,
                payload=result.to_payload(),
            ))
        if outcome.status != "ok":
            logger.warning(
                "计算 %s(%s:%s) → %s: %s", formula_id, entity_kind, entity_id,
                outcome.status, outcome.error,
            )
        return result

    def _resolve_inputs(
        self,
        inputs: list[InputRef],
        formula_id: str,
        required: tuple[str, ...],
        assumptions: dict[str, str],
        *,
        namespace: str,
        entity_kind: str,
        entity_id: str,
        as_of: datetime | None,
    ) -> tuple[dict[str, Decimal], dict[str, dict[str, Any]]]:
        """解析输入 → (数值表, 引用元数据表)。引用必须同命名空间同实体，
        as_of 给定时还必须当时可知（review #4）。"""
        named: dict[str, Decimal] = {}
        metas: dict[str, dict[str, Any]] = {}
        for ref in inputs:
            value = ref.value
            if ref.kind in ("observation", "calculation"):
                if not ref.ref_id:
                    raise CalculationError(f"{ref.label}: kind={ref.kind} 必须给出 ref_id")
                meta = self._store.get_ref_meta(ref.ref_id)
                if meta is None:
                    raise CalculationError(f"{ref.kind} 未登记: {ref.ref_id}")
                if meta["namespace"] != namespace:
                    raise CalculationError(
                        f"{ref.label}: 引用 {ref.ref_id} 属于命名空间 {meta['namespace']!r}，"
                        f"与本次计算 {namespace!r} 不符（跨命名空间引用拒绝）"
                    )
                if (meta["entity_kind"], meta["entity_id"]) != (entity_kind, entity_id):
                    raise CalculationError(
                        f"{ref.label}: 引用 {ref.ref_id} 属于 {meta['entity_kind']}:{meta['entity_id']}，"
                        f"与本次计算实体 {entity_kind}:{entity_id} 不符（跨实体引用拒绝）"
                    )
                if as_of is not None and meta.get("knowledge_time"):
                    known = datetime.fromisoformat(str(meta["knowledge_time"]))
                    if known > as_of:
                        raise CalculationError(
                            f"{ref.label}: 引用 {ref.ref_id} 在 as_of={as_of.isoformat()} 时点"
                            f"尚不可知（knowledge_time={known.isoformat()}）"
                        )
                stored = self._resolve_ref_value(ref)
                if value is not None and _dec(value) != _dec(stored):
                    raise CalculationError(
                        f"{ref.label}: 传入值 {value} 与已登记引用值 {stored} 不一致（禁止漂移）"
                    )
                value = stored
                metas[ref.label] = meta
            if value is None:
                raise CalculationError(f"{ref.label}: 无值且引用不可解析")
            named[ref.label] = _dec(value)
        missing = [k for k in required if k not in named]
        if formula_id == "margin":
            kind = assumptions.get("margin_kind", "net")
            if kind not in _MARGIN_NUMERATORS:
                raise CalculationError(f"未知 margin_kind {kind!r}（可用：{sorted(_MARGIN_NUMERATORS)}）")
            numerator = _MARGIN_NUMERATORS[kind]
            if numerator not in named:
                missing.append(numerator)
        if missing:
            raise CalculationError(f"公式 {formula_id} 缺输入: {missing}")
        return named, metas

    def _resolve_ref_value(self, ref: InputRef) -> str:
        assert ref.ref_id is not None
        if ref.kind == "observation":
            obs = self._store.get_observation(ref.ref_id)
            if obs is None or obs.value is None:
                raise CalculationError(f"observation {ref.ref_id} 无值或未登记")
            return obs.value
        calc = self._store.get_calculation(ref.ref_id)
        if calc is None or calc.result is None:
            raise CalculationError(f"calculation {ref.ref_id} 无结果或未登记")
        return calc.result

    def _assert_currency_consistent(
        self, inputs: list[InputRef], metas: dict[str, dict[str, Any]], formula_id: str
    ) -> None:
        """跨币种输入拒绝（review #14：100 USD − 100 HKD 不得返回 0）。

        币种来源：引用解析的存储记录优先，其次调用方声明的 InputRef.currency；
        unit_conversion/fx 类公式豁免（它们本身就是换算入口）。"""
        if formula_id in ("unit_conversion",):
            return
        currencies: dict[str, str] = {}
        for ref in inputs:
            cur = ref.currency
            if ref.kind == "observation" and ref.label in metas:
                obs = self._store.get_observation(ref.ref_id or "")
                if obs is not None and obs.currency:
                    cur = obs.currency
            if cur:
                currencies[ref.label] = cur
        distinct = set(currencies.values())
        if len(distinct) > 1:
            raise CalculationError(
                f"公式 {formula_id} 输入币种不一致（{currencies}）——"
                "跨币比较必须先经显式 fx_convert 换算链（登记汇率与换算日）"
            )

    def _assert_ttm_inputs(
        self, inputs: list[InputRef], metas: dict[str, dict[str, Any]]
    ) -> None:
        """通用入口的 ttm_sum 门禁（review #14）：四个输入必须是连续、同口径、
        不同版本的季度观测引用——堵住拿同一个年度值重复冒充四季度。"""
        from ..knowledge.metrics import ADDITIVE_FLOW_METRICS

        labels = ("q1", "q2", "q3", "q4")
        by_label = {r.label: r for r in inputs}
        observations = []
        for label in labels:
            ref = by_label.get(label)
            meta = metas.get(label)
            if ref is None or meta is None or ref.kind != "observation" or not ref.ref_id:
                raise CalculationError(
                    f"ttm_sum 的 {label} 必须是 kind=observation 的引用"
                    "（裸数字拼接/年度值充季度均拒绝；推荐用 ttm_from_observations 入口）"
                )
            obs = self._store.get_observation(ref.ref_id)
            if obs is None:
                raise CalculationError(f"ttm_sum {label}: observation 不可解析 {ref.ref_id}")
            observations.append(obs)
        ids = {o.observation_id for o in observations}
        if len(ids) != 4:
            raise CalculationError("ttm_sum 四个输入必须是四个不同的季度观测（重复引用拒绝）")
        keys = {o.metric_key for o in observations}
        if len(keys) != 1:
            raise CalculationError(f"ttm_sum 四个季度必须同一 metric_key（收到 {keys}）")
        if keys.pop() not in ADDITIVE_FLOW_METRICS:
            raise CalculationError("TTM 仅允许可加总流量指标")
        if any(o.period.frequency != "Q" for o in observations):
            raise CalculationError("ttm_sum 只接受 frequency=Q 的观测（年度值充季度拒绝）")
        bases = {(o.basis, o.currency, tuple(sorted(o.dimensions.items()))) for o in observations}
        if len(bases) != 1:
            raise CalculationError(f"ttm_sum 四个季度口径不一致: {bases}")
        ordered = sorted(observations, key=lambda o: o.period.end)
        for prev, nxt in zip(ordered, ordered[1:], strict=False):
            if nxt.period.start is None or prev.period.end is None:
                raise CalculationError("ttm_sum 季度缺期间起止，无法验连续性")
            gap_days = (nxt.period.start - prev.period.end).days
            if not -3 <= gap_days <= 3:
                raise CalculationError(
                    f"ttm_sum 季度不连续（{prev.period.end} → {nxt.period.start}），拒绝拼 TTM"
                )

    @staticmethod
    def _input_hash(
        formula_id: str,
        version: int,
        inputs: list[InputRef],
        metas: dict[str, dict[str, Any]],
        assumptions: dict[str, str],
        *,
        entity_kind: str,
        entity_id: str,
        namespace: str,
    ) -> str:
        """幂等键含引用身份与实体（review #13）：同数值不同实体/不同引用来源
        必须是不同计算（归属与重算血缘不错位）。"""
        canon = json.dumps(
            {
                "formula": f"{formula_id}@{version}",
                "entity": f"{namespace}:{entity_kind}:{entity_id}",
                "inputs": [
                    {"label": r.label, "kind": r.kind, "ref_id": r.ref_id,
                     "value": r.value, "unit": r.unit, "currency": r.currency}
                    for r in sorted(inputs, key=lambda x: x.label)
                ],
                "resolved": {k: {kk: str(vv) for kk, vv in sorted(v.items())}
                             for k, v in sorted(metas.items())},
                "assumptions": assumptions,
            },
            ensure_ascii=False, sort_keys=True, default=str,
        )
        return "ih-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]

    # ---- TTM 专用入口（§6.4.2：连续性/口径校验后才允许求和） ----

    def ttm_from_observations(
        self,
        *,
        entity_kind: str,
        entity_id: str,
        metric_key: str,
        as_of: datetime,
        namespace: str = "prod",
        run_id: str | None = None,
    ) -> CalculationResult:
        from ..knowledge.metrics import ADDITIVE_FLOW_METRICS

        if metric_key not in ADDITIVE_FLOW_METRICS:
            raise CalculationError(f"TTM 仅允许可加总流量指标（收到 {metric_key!r}）")
        obs_list = self._store.observations_as_of(
            entity_kind, entity_id, as_of, namespace=namespace,
            metric_key=metric_key, frequency="Q",
        )
        if len(obs_list) < 4:
            raise CalculationError(
                f"{metric_key}: as_of 可见季度观测不足 4 个（收到 {len(obs_list)}）——缺期不补零"
            )
        latest4 = sorted(obs_list, key=lambda o: o.period.end)[-4:]
        # 连续性：四个季度期间必须首尾相接（约 91±10 天一段）且同口径
        bases = {(o.basis, o.currency, tuple(sorted(o.dimensions.items()))) for o in latest4}
        if len(bases) != 1:
            raise CalculationError(f"{metric_key}: 四个季度口径不一致（basis/currency/dimensions）: {bases}")
        for prev, nxt in zip(latest4, latest4[1:], strict=False):
            gap_days = (
                (nxt.period.start - prev.period.end).days
                if nxt.period.start and prev.period.end
                else None
            )
            if gap_days is None or not (-3 <= gap_days <= 3):
                raise CalculationError(
                    f"{metric_key}: 季度不连续（{prev.period.end} → {nxt.period.start}），拒绝拼 TTM"
                )
        inputs = [
            InputRef(
                kind="observation", label=f"q{n + 1}", ref_id=o.observation_id,
                value=o.value, unit=o.unit, currency=o.currency,
            )
            for n, o in enumerate(latest4)
        ]
        return self.calculate(
            entity_kind=entity_kind, entity_id=entity_id, formula_id="ttm_sum",
            inputs=inputs,
            assumptions={
                "unit": latest4[0].unit, "currency": latest4[0].currency or "",
                "metric_key": metric_key,
                "period_end": latest4[-1].period.end.isoformat(),
            },
            run_id=run_id, namespace=namespace,
        )
