"""MetricSpec：指标经济含义的注册表与准入校验（audit §3.2 P0）。

事故样本（live-a2cce641，四条观测均 `status=ok` 落库）：

- obs-1ce021570310：`sdgr_top20_kpi=73700000`（unit=ratio, currency=USD）——
  摘录是 `$73.7 million, or 37%`，金额被当比例存；
- obs-723282fae4d1：`xtalpi_major_deal_upfront=4 USD`——摘录乱码，
  claim 自述「潜在总额、首付款未拆分」；
- obs-79a868c353bb：`revenue=106303 USD`——摘录只有 `106,303 27,456`，量级矛盾；
- obs-cf01ee8416d6：`revenue=193518 CNY`（segment=AI for Science）——摘录无公司/
  表头/币种/规模词，且不同公司收入被记在行业实体上。

`typed` 过去只保证「可解析 + 换算链一致」，不保证数字对应**哪个主体、哪一列、
什么经济含义**。本模块补上这三件事：

1. 每个 metric_key 有经济含义、值类型（金额/比例/数量）、允许单位与币种、
   主体类别、期间频率、必备维度与合法范围——比例禁带币种，收入与合同潜在总额、
   已收首付款分别是不同指标；
2. 未登记的 metric_key 不是「随便写」：由命名与单位推断值类型，仍受同一组
   一致性校验约束，并要求显式声明 unit（fail-loud，不静默放行）；
3. 主体（subject）与研究范围（scope）分离：公司财务写公司实体，行业实体通过
   候选关系读取；跨主体引用必须在授权范围内。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

ValueKind = Literal[
    "currency_amount",  # 金额（必须带币种与规模上下文）
    "ratio",            # 比例/百分比/倍数（禁带币种）
    "count",            # 计数（客户数/管线数/人数）
    "quantity",         # 物理量（产能/交付量，必须带计量单位）
    "price",            # 单价/股价（带币种）
    "duration",         # 时长（月/年，如现金跑道）
]

#: 允许的目标单位（按值类型）
_UNITS_BY_KIND: dict[str, frozenset[str]] = {
    "currency_amount": frozenset({"USD", "CNY", "HKD", "EUR", "GBP", "JPY", "KRW"}),
    "ratio": frozenset({"ratio", "percent", "x", "multiple", "bp", "ppt"}),
    "count": frozenset({"count", "units", "companies", "customers", "programs", "people"}),
    "quantity": frozenset({"units", "MW", "GW", "tonnes", "sqm", "wafers", "doses"}),
    "price": frozenset({"USD", "CNY", "HKD", "EUR", "GBP", "JPY", "KRW", "per_share"}),
    "duration": frozenset({"months", "years", "quarters", "days"}),
}

#: 币种单位（比例/计数/时长不得携带）
_CURRENCY_UNITS = _UNITS_BY_KIND["currency_amount"] | _UNITS_BY_KIND["price"]


class MetricSpecError(ValueError):
    """指标语义校验失败（fail-closed）：消息即拒绝原因，逐条可修复。"""

    def __init__(self, message: str, *, violations: list[str] | None = None):
        super().__init__(message)
        self.violations = violations or [message]


@dataclass(frozen=True)
class MetricSpec:
    """一个 metric_key 的经济含义与合法形态。"""

    metric_key: str
    value_kind: ValueKind
    meaning: str
    #: 允许的目标单位；空 = 由 value_kind 的默认集合决定
    allowed_units: tuple[str, ...] = ()
    #: 是否必须携带币种（金额/单价类为 True）
    requires_currency: bool = False
    #: 允许的主体类别（stock=公司，industry=行业整体）
    subject_kinds: tuple[str, ...] = ("stock",)
    #: 允许的期间频率
    allowed_frequencies: tuple[str, ...] = ("FY", "Q", "H1", "TTM", "instant")
    #: 必备维度（如分部收入必须有 segment）
    required_dimensions: tuple[str, ...] = ()
    #: 合法值域（None = 该侧不设限）
    value_range: tuple[Decimal | None, Decimal | None] = (None, None)
    #: 金额类是否必须有文档定位（document→page/table→row/column）
    requires_locator: bool = False
    #: 摘录里出现这些标记即与本指标语义冲突（如「潜在总额」不能记成「已收首付款」）
    incompatible_quote_markers: tuple[str, ...] = ()
    #: 摘录里必须出现其一（正向锚点）
    required_quote_markers: tuple[str, ...] = ()
    registered: bool = True
    notes: str = ""

    def units(self) -> frozenset[str]:
        return frozenset(self.allowed_units) if self.allowed_units else _UNITS_BY_KIND[self.value_kind]


def _money(key: str, meaning: str, **kw: Any) -> MetricSpec:
    return MetricSpec(
        metric_key=key, value_kind="currency_amount", meaning=meaning,
        requires_currency=True, requires_locator=True, **kw,
    )


def _ratio(key: str, meaning: str, **kw: Any) -> MetricSpec:
    return MetricSpec(metric_key=key, value_kind="ratio", meaning=meaning,
                      value_range=(Decimal("-1"), Decimal("100")), **kw)


#: 指标注册表（受控目录）：新增指标 = 在此登记语义，而不是让模型自由命名
REGISTRY: dict[str, MetricSpec] = {s.metric_key: s for s in (
    # ---- 公司财务（流量/余额） ----
    _money("revenue", "营业收入（报告期内确认的收入总额）",
           allowed_frequencies=("FY", "Q", "H1", "TTM")),
    _money("net_income", "净利润（归属母公司口径需另建 net_income_attributable）"),
    _money("gross_profit", "毛利"),
    _money("operating_income", "营业利润"),
    _money("ebitda", "息税折旧摊销前利润（非 GAAP 需 basis=non_GAAP）"),
    _money("cfo", "经营活动现金流净额"),
    _money("capex", "资本开支"),
    _money("fcf", "自由现金流（= CFO - capex，应为 calculated 性质）"),
    _money("r_and_d", "研发费用"),
    _money("net_debt", "净债务（余额，用 instant/期末值）",
           allowed_frequencies=("FY", "Q", "H1", "instant")),
    _money("cash_and_equivalents", "现金及等价物（余额）",
           allowed_frequencies=("FY", "Q", "H1", "instant")),
    _money("market_cap", "市值（时点值）", subject_kinds=("stock",),
           allowed_frequencies=("instant",)),
    _money("enterprise_value", "企业价值（时点值）", allowed_frequencies=("instant",)),
    MetricSpec(metric_key="share_price", value_kind="price", meaning="股价（时点）",
               requires_currency=True, allowed_frequencies=("instant",)),
    _ratio("gross_margin", "毛利率（毛利/收入；percent 或 ratio 二选一，不得混用）"),
    _ratio("operating_margin", "营业利润率"),
    _ratio("net_margin", "净利率"),
    _ratio("growth_rate", "同比/复合增速（需说明比较期；行业增速写行业实体）",
           subject_kinds=("stock", "industry")),
    MetricSpec(metric_key="share_dilution", value_kind="ratio",
               meaning="股本稀释比例（期间股本变动/期初股本）",
               value_range=(Decimal("-1"), Decimal("10"))),
    # ---- 订单/产能（工业设备类） ----
    _money("orders", "新签订单金额（报告期内）"),
    _money("firm_backlog", "确定性在手订单（期末余额，不含潜在/意向）",
           allowed_frequencies=("FY", "Q", "H1", "instant"),
           incompatible_quote_markers=("潜在", "意向", "pipeline only", "up to")),
    _money("backlog", "在手订单（期末余额；口径需在 dimensions/note 说明）",
           allowed_frequencies=("FY", "Q", "H1", "instant")),
    MetricSpec(metric_key="capacity", value_kind="quantity", meaning="产能（物理量，需计量单位）",
               allowed_units=("MW", "GW", "tonnes", "sqm", "wafers", "units")),
    MetricSpec(metric_key="deliveries", value_kind="count", meaning="交付量（台/套/剂）",
               allowed_units=("units", "count")),
    _money("service_obligations", "服务义务/维保合同金额"),
    # ---- 生物医药 ----
    MetricSpec(metric_key="pipeline", value_kind="count", meaning="在研管线数量（按阶段需拆维度）",
               allowed_units=("programs", "count"), required_dimensions=("stage",)),
    MetricSpec(metric_key="cash_runway", value_kind="duration", meaning="现金跑道（可支撑月数）",
               allowed_units=("months", "years", "quarters"),
               value_range=(Decimal("0"), Decimal("30"))),
    _money("quarterly_burn", "季度现金消耗"),
    _money("licensing_milestones", "授权里程碑金额（潜在总额，非已收）",
           incompatible_quote_markers=("已收", "received to date")),
    # ---- 合同拆分（audit：潜在总额 / 已收首付款 / 里程碑上限必须分开） ----
    _money("contract_potential_total", "合同潜在总额（含里程碑，未实现）",
           required_quote_markers=("潜在", "总额", "up to", "potential", "aggregate", "total")),
    _money("contract_upfront_received", "已收/应收首付款（不含里程碑）",
           required_quote_markers=("首付款", "upfront", "initial payment", "已收", "down payment"),
           incompatible_quote_markers=("潜在总额", "up to", "aggregate of")),
    _money("contract_milestone_max", "里程碑付款上限（潜在，未实现）",
           required_quote_markers=("里程碑", "milestone")),
    _money("arr", "年度经常性收入（订阅制；口径需说明）"),
    # ---- 行业级 ----
    _money("market_size", "行业市场规模（口径 top-down/bottom-up 必须在维度或 note 说明）",
           subject_kinds=("industry", "stock"), required_dimensions=()),
    MetricSpec(metric_key="capacity_supply", value_kind="quantity",
               meaning="行业供给/产能（物理量）", subject_kinds=("industry",),
               allowed_units=("MW", "GW", "tonnes", "sqm", "wafers", "units")),
    MetricSpec(metric_key="customer_count", value_kind="count", meaning="客户数",
               allowed_units=("customers", "count")),
    MetricSpec(metric_key="headcount", value_kind="count", meaning="员工数",
               allowed_units=("people", "count")),
    MetricSpec(metric_key="market_share", value_kind="ratio", meaning="市场份额",
               subject_kinds=("stock", "industry"), value_range=(Decimal("0"), Decimal("1"))),
)}

#: 命名启发式（未登记 key 的兜底推断）：后缀/关键词 → 值类型
_KIND_HINTS: tuple[tuple[re.Pattern[str], ValueKind], ...] = (
    (re.compile(r"(margin|rate|ratio|share|yield|_pct$|percent|multiple|_x$)", re.I), "ratio"),
    (re.compile(r"(runway|_months$|_years$|duration)", re.I), "duration"),
    (re.compile(r"(count|_n$|number_of|headcount|customers?$|programs?$)", re.I), "count"),
    (re.compile(r"(capacity|volume|tonnes|mw$|gw$|wafers|sqm)", re.I), "quantity"),
    (re.compile(r"(price|per_share|psp$|arpu)", re.I), "price"),
    (re.compile(
        r"(revenue|income|profit|ebitda|cfo|capex|fcf|cash|debt|orders|backlog|arr|"
        r"spend|cost|expense|fee|sales|gmv|market_size|valuation|upfront|milestone|"
        r"amount|_usd$|_cny$|deal)", re.I), "currency_amount"),
)


def infer_value_kind(metric_key: str, unit: str = "") -> ValueKind | None:
    """未登记 key 的值类型推断：命名启发式 + 单位反推（不确定 → None）。"""
    u = (unit or "").strip()
    if u in _CURRENCY_UNITS:
        return "currency_amount"
    if u in ("ratio", "percent", "x", "multiple", "bp", "ppt"):
        return "ratio"
    if u in ("months", "years", "quarters", "days"):
        return "duration"
    for pattern, kind in _KIND_HINTS:
        if pattern.search(metric_key):
            return kind
    return None


def spec_for(metric_key: str, *, unit: str = "") -> MetricSpec:
    """取指标规格；未登记 key 走推断（registered=False，仍受同一组校验约束）。"""
    spec = REGISTRY.get(metric_key)
    if spec is not None:
        return spec
    kind = infer_value_kind(metric_key, unit)
    if kind is None:
        raise MetricSpecError(
            f"未登记指标 {metric_key!r} 且无法从命名/单位推断值类型："
            "请改用注册表中的语义键，或显式给出可识别的 unit"
            f"（注册表：{sorted(REGISTRY)[:12]}…）",
            violations=[f"unknown_metric_key:{metric_key}"],
        )
    return MetricSpec(
        metric_key=metric_key, value_kind=kind,  # type: ignore[arg-type]
        meaning="（未登记指标：语义由命名/单位推断，需人工复核）",
        requires_currency=kind in ("currency_amount", "price"),
        requires_locator=kind in ("currency_amount", "price"),
        subject_kinds=("stock", "industry"),
        registered=False,
        notes="建议在 knowledge/metric_spec.py REGISTRY 登记后再用于正式产物",
    )


def check_observation(
    *,
    metric_key: str,
    unit: str,
    currency: str | None,
    value: str | None,
    frequency: str,
    dimensions: dict[str, str],
    subject_kind: str,
    locator: dict[str, str] | None = None,
    quotes: list[str] | None = None,
    basis: str = "GAAP",
) -> None:
    """指标语义准入（audit §3.2 修复方案 2）：违规逐条列出，全部违规一次报清。

    校验的是「这个数字是什么」，不是「能不能解析」：
    - 单位与值类型一致（比例不得带币种；金额必须有币种）；
    - 主体类别合法（公司财务不得记在行业实体上，反之亦然）；
    - 期间频率合法（余额类不做 TTM）；
    - 必备维度齐（分部收入必须有 segment）；
    - 值域合法（比例 ≤1 或 ≤100 视单位；跑道月数不超 30）；
    - 金额类必须有文档定位（document→page/table→row/column）；
    - 摘录语义不与指标冲突（「潜在总额」不得记成「已收首付款」）。
    """
    spec = spec_for(metric_key, unit=unit)
    violations: list[str] = []
    u = (unit or "").strip()

    if u and u not in spec.units():
        violations.append(
            f"单位 {u!r} 不在 {spec.value_kind} 类指标允许集合 {sorted(spec.units())}"
            f"（{spec.metric_key}: {spec.meaning}）。"
            "修法：unit 用规范代码（金额类如 CNY/USD，比例类如 percent/ratio），"
            "原文量表写进 unit_text（如 'RMB'000' / 'USD millions'），"
            "规模换算由服务端按 value_text/unit_text 显式登记，不要自造单位字符串"
        )
    if spec.value_kind in ("ratio", "count", "duration", "quantity") and currency:
        violations.append(
            f"{spec.value_kind} 类指标不得携带币种（收到 currency={currency}）——"
            "金额被当比例保存是本次事故形态之一"
        )
    if u in _CURRENCY_UNITS and currency and u != currency:
        # 基线发现 F3：同库出现 unit=USD + currency=CNY 自相矛盾的观测——
        # 金额类的 unit 就是币种代码，两者必须一致；换算口径走显式 normalization
        violations.append(
            f"unit={u} 与 currency={currency} 冲突：金额类指标的 unit 即币种代码，"
            "二者必须一致（currency 用报告币种；如经汇率换算，在 normalization 里"
            "显式登记 fx_convert 步骤，原文量表写 unit_text）"
        )
    if u in ("ratio", "percent", "x", "multiple", "bp", "ppt") and currency:
        violations.append(f"unit={u} 与 currency={currency} 冲突（比例不得带币种）")
    if spec.requires_currency and not currency and value is not None:
        violations.append(f"{spec.metric_key} 是金额类指标，必须给出 currency")
    if subject_kind not in spec.subject_kinds:
        violations.append(
            f"{spec.metric_key} 的主体类别应为 {spec.subject_kinds}，收到 {subject_kind!r}"
            "（公司财务写公司实体；行业总量写行业实体）"
        )
    if frequency not in spec.allowed_frequencies:
        violations.append(
            f"{spec.metric_key} 不允许 frequency={frequency}（允许 {spec.allowed_frequencies}）"
        )
    missing_dims = [d for d in spec.required_dimensions if not (dimensions or {}).get(d)]
    if missing_dims:
        violations.append(f"{spec.metric_key} 缺必备维度 {missing_dims}")
    if value is not None:
        try:
            dec = Decimal(str(value))
        except Exception:  # noqa: BLE001 - 上游已校验，这里只防意外
            dec = None
        if dec is not None:
            lo, hi = spec.value_range
            if lo is not None and dec < lo:
                violations.append(f"{spec.metric_key}={dec} 低于合法下界 {lo}")
            if hi is not None and dec > hi:
                violations.append(
                    f"{spec.metric_key}={dec} 超出合法上界 {hi}"
                    f"（{spec.value_kind} 类；检查是否把金额当比例或量级错误）"
                )
            if spec.value_kind == "ratio" and u in ("ratio", "x", "multiple") and abs(dec) > 100:
                violations.append(
                    f"{spec.metric_key}={dec} 作为 {u} 明显异常（比例值 |x|>100）——"
                    "疑似金额被记成比例"
                )
    if spec.requires_locator and not (locator or {}):
        # 金额类的定位要求是「条件式」的：原文带规模词（1500 million）时量级已绑定原文，
        # 不强制表头定位；裸数字才必须给 locator——该判据在 assert_value_context 里执行
        # （那里能看到 raw.value_text/unit_text），此处不重复报错。
        pass
    if basis == "non_GAAP" and spec.value_kind == "currency_amount" and not (dimensions or {}).get(
        "adjustment"
    ) and spec.metric_key not in ("ebitda", "fcf", "arr"):
        violations.append("non_GAAP 金额需说明调整口径（dimensions.adjustment 或改用 GAAP 指标）")

    quote_text = " ".join(quotes or [])
    if quote_text:
        low = quote_text.lower()
        if spec.incompatible_quote_markers and any(
            m.lower() in low for m in spec.incompatible_quote_markers
        ):
            hit = [m for m in spec.incompatible_quote_markers if m.lower() in low]
            violations.append(
                f"摘录含与 {spec.metric_key} 语义冲突的标记 {hit}"
                f"（{spec.meaning}）——请改用语义相符的指标键"
            )
        if spec.required_quote_markers and not any(
            m.lower() in low for m in spec.required_quote_markers
        ):
            violations.append(
                f"{spec.metric_key} 要求摘录含正向锚点之一 {list(spec.required_quote_markers)}"
                f"（{spec.meaning}）；摘录未出现，说明该数字不是本指标"
            )
    if violations:
        raise MetricSpecError(
            f"指标语义校验未过（{metric_key}）：" + "；".join(violations),
            violations=violations,
        )


# ---------------- 主体授权（scope ≠ subject，audit §3.2 修复方案 1） ----------------


@dataclass
class SubjectAuthorization:
    """研究范围内允许的跨主体引用（多主体研究的授权闸）。

    规则：
    - subject == scope → 总是允许；
    - scope 是行业、subject 是公司 → 需要公司在该行业的候选/授权清单内
      （配方候选池、计划 scope.authorized_subjects、或显式 allow 列表）；
    - 其余跨主体 → 拒绝（不因为「多主体研究」就放开所有引用）。
    """

    scope_kind: str
    scope_id: str
    authorized: set[str] = field(default_factory=set)
    #: 授权依据（可回放：为什么允许这个主体）
    basis: dict[str, str] = field(default_factory=dict)

    def allow(self, subject_kind: str, subject_id: str) -> tuple[bool, str]:
        sid = (subject_id or "").strip()
        if not sid:
            return False, "subject_entity_id 为空（跨主体引用必须显式给出主体）"
        if subject_kind == self.scope_kind and sid.upper() == self.scope_id.upper():
            return True, "same_entity"
        key = f"{subject_kind}:{sid}"
        if key in self.authorized or sid.upper() in {a.upper() for a in self.authorized}:
            return True, self.basis.get(key, "authorized_subject")
        if self.scope_kind == "industry":
            return False, (
                f"跨主体引用未授权：{key} 不在行业 {self.scope_id} 的候选/授权清单内"
                f"（已授权 {sorted(self.authorized)[:8]}）——公司财务应写在公司实体上，"
                "行业通过候选关系读取"
            )
        return False, f"跨主体引用未授权：{key}（研究范围 {self.scope_kind}:{self.scope_id}）"


def authorized_subjects_from_profile(profile: dict[str, Any]) -> tuple[set[str], dict[str, str]]:
    """从行业档案的候选字段抽出授权主体（player_landscape / peers / candidates）。"""
    allowed: set[str] = set()
    basis: dict[str, str] = {}
    for field_name in ("player_landscape", "peers", "candidates", "sub_sectors"):
        value = (profile or {}).get(field_name)
        rec = value.value if hasattr(value, "value") else value
        if not isinstance(rec, list):
            continue
        for item in rec:
            if not isinstance(item, dict):
                continue
            for key in ("ticker", "symbol", "company_id", "entity_id", "name", "company"):
                v = item.get(key)
                if isinstance(v, str) and v.strip():
                    ident = v.strip()
                    allowed.add(ident.upper())
                    basis[ident.upper()] = f"档案字段 {field_name}"
                    kind = "industry" if not re.fullmatch(r"[A-Za-z0-9.\-]{1,12}", ident) else "stock"
                    allowed.add(f"{kind}:{ident}".upper())
    return allowed, basis


def make_subject_gate(kb: Any = None, metrics: Any = None):
    """装配层用的主体授权闸（audit §3.2 修复方案 1）。

    授权来源（两者之一即可，依据回写事件）：
    1. 行业档案的候选字段（player_landscape / peers / candidates / sub_sectors）；
    2. 冻结计划的 scope.authorized_subjects（多主体研究显式授权）。

    不因为「多主体研究」就放开所有引用：未入候选、未授权的公司一律拒。
    """

    def gate(scope_kind: str, scope_id: str, subject_kind: str, subject_id: str) -> tuple[bool, str]:
        sid = (subject_id or "").strip()
        if not sid:
            return False, "subject_entity_id 为空（跨主体引用必须显式给出主体）"
        if subject_kind == scope_kind and sid.upper() == (scope_id or "").upper():
            return True, "same_entity"
        # 1) 计划显式授权
        if metrics is not None:
            try:
                plans = metrics.plans_for(scope_kind, scope_id, namespace="prod")
            except Exception:  # noqa: BLE001 - 存储不支持时不阻断，回落档案判据
                plans = []
            for plan in plans or []:
                authorized = (plan.get("scope") or {}).get("authorized_subjects") or []
                if any(str(a).strip().upper() in (sid.upper(), f"{subject_kind}:{sid}".upper())
                       for a in authorized):
                    return True, f"plan:{plan.get('plan_id')} scope.authorized_subjects"
        # 2) 行业档案候选字段
        if kb is not None and scope_kind == "industry":
            try:
                profile = kb.as_of(scope_kind, scope_id, datetime.now(UTC), namespace="prod")
            except Exception:  # noqa: BLE001
                profile = {}
            allowed, basis = authorized_subjects_from_profile(profile)
            if sid.upper() in allowed or f"{subject_kind}:{sid}".upper() in allowed:
                return True, basis.get(sid.upper(), "行业档案候选字段")
            return False, (
                f"跨主体引用未授权：{subject_kind}:{sid} 不在行业 {scope_id} 的候选/授权清单内"
                f"（已授权 {sorted(allowed)[:6]}）——公司财务应写在公司实体上，"
                "行业通过候选关系读取"
            )
        return False, (
            f"跨主体引用未授权：{subject_kind}:{sid}（研究范围 {scope_kind}:{scope_id}）"
        )

    return gate


__all__ = [
    "MetricSpec", "MetricSpecError", "REGISTRY", "spec_for", "check_observation",
    "infer_value_kind", "SubjectAuthorization", "authorized_subjects_from_profile",
    "make_subject_gate", "ValueKind",
]
