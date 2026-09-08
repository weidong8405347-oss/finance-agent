"""数值标准化：「保留原文 + 显式转换」的换算血缘（设计 §6.2）。

纪律：
- 原文 `1.2 billion` 保留字符串与单位，再登记 `scale_by_power_of_ten` 转换为
  `1200000000 USD`——转换链可逐步重算（recompute_lineage），失败 fail-loud；
- 不把转换后的数字送进旧 numeric-guard 并放宽门禁：typed 入口在这里逐叶校验；
- 公式注册表带版本（formula_id@version），重算时锁定当时的公式语义；
- 金额用 Decimal（Decimal(str) 防浮点污染），四舍五入只在展示末端。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .metrics import MetricObservation


class NormalizationError(Exception):
    """换算链不可重算 / 公式未知 / 数值非法（fail-closed）。"""


#: 单位词 → 10 的幂（英中常用规模词）
SCALE_WORDS: dict[str, int] = {
    "thousand": 3, "k": 3, "千": 3,
    "million": 6, "mn": 6, "m": 6, "百万": 6,
    "billion": 9, "bn": 9, "b": 9, "亿": 8,  # 中文「亿」= 10^8（不是 9）
    "trillion": 12, "t": 12, "万亿": 12, "兆": 12,
}

_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


class NormalizationStep(BaseModel):
    """一步显式换算：formula_id@version + 全字符串参数（Decimal 可重算）。"""

    model_config = ConfigDict(extra="forbid")

    formula_id: str
    version: int = 1
    params: dict[str, str] = Field(default_factory=dict)


# ---------------- 公式注册表（版本化；设计 §8.1 受控目录的换算子集） ----------------


def _f_scale_by_power_of_ten(value: Decimal, params: dict[str, str]) -> Decimal:
    exp = Decimal(params["exponent"])
    if exp != exp.to_integral_value():
        raise NormalizationError(f"scale_by_power_of_ten 的 exponent 必须是整数（收到 {params['exponent']}）")
    return value * (Decimal(10) ** int(exp))


def _f_unit_word_scale(value: Decimal, params: dict[str, str]) -> Decimal:
    word = params["word"].strip().lower()
    if word not in SCALE_WORDS:
        raise NormalizationError(f"未知规模单位词 {word!r}（可用：{sorted(SCALE_WORDS)}）")
    return value * (Decimal(10) ** SCALE_WORDS[word])


def _f_fx_convert(value: Decimal, params: dict[str, str]) -> Decimal:
    """跨币换算：必须显式引用汇率、换算日与汇率来源（设计 §6.4.5）。"""
    for key in ("rate", "from_currency", "to_currency", "rate_as_of"):
        if not params.get(key):
            raise NormalizationError(f"fx_convert 缺参数 {key}（汇率/币种/换算日必须显式）")
    if not params.get("rate_evidence_ref"):
        raise NormalizationError("fx_convert 必须携带 rate_evidence_ref（汇率也要可回源）")
    rate = _dec(params["rate"])
    if rate <= 0:
        raise NormalizationError(f"fx_convert 汇率必须为正（收到 {rate}）")
    return value * rate


def _f_percent_to_ratio(value: Decimal, params: dict[str, str]) -> Decimal:
    del params
    return value / Decimal(100)


def _f_ratio_to_percent(value: Decimal, params: dict[str, str]) -> Decimal:
    del params
    return value * Decimal(100)


def _f_negate(value: Decimal, params: dict[str, str]) -> Decimal:
    del params
    return -value


#: formula_id → (version, fn)。重算按登记时的 version 取语义；未知 formula fail-loud。
FORMULAS: dict[str, tuple[int, Any]] = {
    "scale_by_power_of_ten": (1, _f_scale_by_power_of_ten),
    "unit_word_scale": (1, _f_unit_word_scale),
    "fx_convert": (1, _f_fx_convert),
    "percent_to_ratio": (1, _f_percent_to_ratio),
    "ratio_to_percent": (1, _f_ratio_to_percent),
    "negate": (1, _f_negate),
}


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v).replace(",", ""))
    except InvalidOperation as e:
        raise NormalizationError(f"非法数值: {v!r}") from e


def apply_step(value: Decimal, step: NormalizationStep) -> Decimal:
    entry = FORMULAS.get(step.formula_id)
    if entry is None:
        raise NormalizationError(f"未知换算公式 {step.formula_id!r}（注册表：{sorted(FORMULAS)}）")
    version, fn = entry
    if step.version != version:
        raise NormalizationError(
            f"公式 {step.formula_id} 版本不匹配：登记 v{step.version}，当前注册 v{version}"
        )
    return fn(value, step.params)


def _year_like(text: str) -> bool:
    """年份形态：1900–2100 的 standalone 整数（「2026 revenue 1.2 billion」不取 2026）。"""
    try:
        d = Decimal(text)
    except InvalidOperation:
        return False
    return d == d.to_integral_value() and 1900 <= int(d) <= 2100


def _select_number_match(value_text: str) -> re.Match[str]:
    """选定值文本中「真正是指标值」的数字匹配（parse 与量级绑定共用，保证一致）：

    1. 规模词紧邻的数字优先（'2026 revenue 1.2 billion' → 1.2；'Q4 revenue 300 million' → 300）；
    2. 否则排除年份形态后取首个数字（'revenue 100 in 2026' → 100）；
    3. 全是年份形态才退回首个数字。
    """
    cleaned = value_text.strip().replace(",", "")
    matches = list(_NUM_RE.finditer(cleaned))
    if not matches:
        raise NormalizationError(f"原文值文本中没有可解析数字: {value_text!r}")
    word = detect_scale_word(value_text, "")
    if word is not None:
        pattern = re.compile(
            rf"(-?\d[\d,]*\.?\d*)\s*{re.escape(word)}s?(?![a-z])", re.IGNORECASE
        )
        m = pattern.search(cleaned)
        if m is not None:
            return m  # group(1) 是数字
    non_year = [m for m in matches if not _year_like(m.group(0))]
    return non_year[0] if non_year else matches[0]


def parse_raw_number(value_text: str) -> Decimal:
    """原文值文本 → Decimal 尾数（千分位容忍；规模词剥离交给 unit_word_scale；
    会计括号负数保留符号：'( 1,234 )' → -1234，财报亏损不得记成盈利）。"""
    cleaned = value_text.strip().replace(",", "")
    m = _select_number_match(value_text)
    # 规模词模式捕获组 1 才是数字；裸数字模式用 group(0)
    value = _dec(m.group(m.lastindex or 0))
    before = cleaned[: m.start(0)].rstrip()
    after = cleaned[m.end(0):].lstrip()
    if before.endswith("(") and after.startswith(")"):
        return -abs(value)  # 会计负数：( 1,234 ) / ( 1,234 million )
    return value


def detect_scale_word(value_text: str, unit_text: str) -> str | None:
    """从原文值/单位文本中识别规模词（million/billion/亿…，含复数）；无则 None。"""
    for text in (value_text, unit_text):
        low = text.strip().lower()
        for word in sorted(SCALE_WORDS, key=len, reverse=True):
            if re.search(rf"(?<![a-z]){re.escape(word)}s?(?![a-z])", low):
                return word
    return None


def normalize_raw(
    value_text: str,
    unit_text: str = "",
    *,
    extra_steps: list[NormalizationStep] | None = None,
) -> tuple[str, list[NormalizationStep]]:
    """便捷入口：原文 → (标准化十进制字符串, 换算链)。

    规模词自动登记 unit_word_scale；extra_steps（如 fx_convert）依序追加。
    """
    mantissa = parse_raw_number(value_text)
    steps: list[NormalizationStep] = []
    word = detect_scale_word(value_text, unit_text)
    value = mantissa
    if word is not None:
        step = NormalizationStep(
            formula_id="unit_word_scale", version=1, params={"word": word}
        )
        value = apply_step(value, step)
        steps.append(step)
    for step in extra_steps or []:
        value = apply_step(value, step)
        steps.append(step)
    return format_decimal(value), steps


def format_decimal(value: Decimal) -> str:
    """Decimal → 无损字符串（去尾零；整数不带小数点）。"""
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal(1)))
    return str(normalized)


# ---------------- 量级绑定（review #1：数值量级必须绑定原文证据） ----------------

_SCALE_WORD_PATTERN = "|".join(
    re.escape(w) for w in sorted(SCALE_WORDS, key=len, reverse=True)
)

#: 规模词等价类（'bn'≡'billion'；'亿'=10^8 单独一类，不与 billion 混同）
_SCALE_EQUIV: list[frozenset[str]] = [
    frozenset({"thousand", "k", "千"}),
    frozenset({"million", "m", "mn", "百万"}),
    frozenset({"billion", "b", "bn"}),
    frozenset({"trillion", "t", "万亿", "兆"}),
    frozenset({"亿"}),
]


def _scale_class(word: str) -> frozenset[str]:
    w = word.lower().rstrip("s")
    return next((c for c in _SCALE_EQUIV if w in c), frozenset({w}))


def _mantissa_core(value_text: str) -> str:
    """量级绑定用的尾数数字串：与 parse_raw_number 同一选择逻辑（同一命中点）。"""
    m = _select_number_match(value_text)
    return m.group(m.lastindex or 0)


def assert_magnitude_bound(
    value_text: str, unit_text: str, quotes: list[str], *,
    unit_context: list[str] | tuple[str, ...] = (),
) -> None:
    """量级一致性门禁：登记值的有效量级（尾数+规模词）必须在证据中可定位。

    堵住两类量级事故（review #1）：
    - 改规模词：证据「1500 million」，提交 value_text='1500 billion' → 拒；
    - 丢规模词：证据「1500 million」，提交 value_text='1500' 且 unit_text 无规模词
      → 拒（摘录中该数字后跟规模词，登记值必须携带同等价类）。

    三种定位强度：
    - 规模词写在 value_text 里 → 严格相邻（数字后紧跟同等价类词，bn≡billion）；
    - 规模词仅由 unit_text 声明（财报表头 'In millions' 模式）→ 同摘录共现
      （数字与同等价类词均出现）；摘录里数字后跟随了不同等价类词仍拒；
    - 规模词只存在于表头/单位上下文（unit_context，来自 locator 的 header/unit 字段）
      → 接受（audit §3.2：财务证据绑定 document→page/table→row/column 后，
      表头声明的量级与正文数字分开存放是正常形态）。
    无法定位 → NormalizationError（fail-closed，宁拒勿猜）。
    """
    core = _mantissa_core(value_text)
    word_in_value = detect_scale_word(value_text, "")
    word = word_in_value or detect_scale_word("", unit_text)
    for quote in quotes:
        qn = quote.replace(",", "")  # 千分位归一（'1,500' → '1500'）
        if core not in qn:
            continue
        for m in re.finditer(re.escape(core), qn):
            tail = qn[m.end():]
            following = re.match(rf"\s*({_SCALE_WORD_PATTERN})s?(?![a-z])", tail, re.IGNORECASE)
            if word_in_value is not None:
                # 严格：数字后必须紧跟同等价类规模词（允许复数）
                if following and _scale_class(following.group(1)) == _scale_class(word_in_value):
                    return
            elif word is not None:
                # 表头模式：同摘录内共现同等价类词，且数字后未跟随其他等价类词
                if following and _scale_class(following.group(1)) != _scale_class(word):
                    continue
                if re.search(rf"(?<![a-z]){re.escape(word)}s?(?![a-z])", qn, re.IGNORECASE):
                    return
                # 表头/单位上下文声明了同等价类规模词（locator.header / locator.unit）
                for ctx in unit_context:
                    if ctx and re.search(
                        rf"(?<![a-z]){re.escape(word)}s?(?![a-z])", str(ctx), re.IGNORECASE
                    ):
                        return
            else:
                # 登记值无规模词：摘录里该数字后不得跟规模词（否则量级丢失）
                if not following:
                    return
    declared = f"{core} {word}" if word else core
    raise NormalizationError(
        f"量级未绑定证据：登记值 {declared!r} 无法在任何摘录中定位"
        "（数字+规模词必须与原文一致；不允许改写或省略量级）"
    )


# ---------------- 证据上下文准入（audit §3.2 修复方案 3/4） ----------------

#: 金额类值类型：裸数字摘录不足以证明标准化金额
_MONEY_KINDS = frozenset({"currency_amount", "price"})
#: locator 中可证明「表头/量级上下文」的键
_LOCATOR_CONTEXT_KEYS = ("table", "page", "document", "row", "column", "section", "header")


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


#: 候选值数字：排除期间/表单标签型数字（Q3、H1、FY24、10-K、v2）——
#: 它们不是「可选的值」，计入多数字会让正常摘录被误拒
_CANDIDATE_NUM = re.compile(
    r"(?<![A-Za-z0-9])"          # 前面不紧跟着字母/数字（Q3 → 不算）
    r"(-?\d[\d,]*\.?\d*)"
    r"(?![A-Za-z0-9])"           # 后面不紧跟字母/数字（10-K 的 K、v2 不算）
)
#: 规模词直接连写时（'100million'）仍算候选值：允许尾随规模词
_CANDIDATE_NUM_SCALED = re.compile(
    r"(?<![A-Za-z0-9])(-?\d[\d,]*\.?\d*)\s?"
    rf"(?=({'|'.join(re.escape(w) for w in sorted(SCALE_WORDS, key=len, reverse=True))})s?(?![a-z]))",
    re.IGNORECASE,
)


def _quote_numbers(quote: str) -> list[str]:
    """摘录里的**候选值**数字（去年份、去期间/表单标签、去千分位）。"""
    cleaned = quote.replace(",", "")
    out = [
        m.group(1) for m in _CANDIDATE_NUM.finditer(cleaned)
        if not _year_like(m.group(1))
    ]
    out += [m.group(1) for m in _CANDIDATE_NUM_SCALED.finditer(cleaned)
            if not _year_like(m.group(1))]
    return out


def assert_value_context(
    value_text: str,
    unit_text: str,
    quotes: list[str],
    *,
    value_span: str | None = None,
    locator: dict[str, str] | None = None,
    value_kind: str = "currency_amount",
) -> None:
    """数字的证据上下文准入（audit §3.2）。

    堆住本次三类真实事故：
    - `106,303 27,456` 这种多数字摘录被当成单一金额（无表头、无规模词）；
    - `$73.7 million, or 37%` 里猜错要哪个数（金额被存成 ratio）；
    - `193,518 81,864` 裁剪成裸数字后无法检验表头单位。

    规则（fail-closed，宁拒勿猜）：
    1. 摘录含 ≥2 个非年份数字 → 必须显式给 value_span（cell/span 定位），不许猜；
    2. value_span 必须是摘录的逐字子串，且包含登记值的尾数；
    3. 金额/单价类：必须有规模上下文——规模词（value_text/unit_text）或
       locator 里的表头/页/表定位；两者都无 → 拒（裸数字不得变成可靠金额）。
    """
    if not quotes:
        return
    core = _mantissa_core(value_text)
    core_plain = core.replace(",", "")
    located = [q for q in quotes if core_plain in q.replace(",", "")]
    if not located:
        # 尾数定位不到摘录：量级绑定门禁（assert_magnitude_bound）会报错，不重复判罪
        return

    span = (value_span or "").strip()
    if span:
        span_n = _normalize_ws(span)
        if not any(span_n in _normalize_ws(q) for q in located):
            raise NormalizationError(
                f"value_span {span!r} 不是任何证据摘录的逐字子串（不允许改写或拼接）"
            )
        if core_plain not in span_n.replace(",", ""):
            raise NormalizationError(
                f"value_span {span!r} 不包含登记值的尾数 {core_plain}"
                "（请给出包含该数字的原文片段）"
            )
        context_text = span
    else:
        context_text = " ".join(located)

    distinct = sorted(set(_quote_numbers(context_text)))
    if len(distinct) >= 2 and not span:
        raise NormalizationError(
            f"摘录含多个数字 {distinct}，必须显式给出 value_span（cell/span 定位）——"
            "不允许从如 '$73.7 million, or 37%' 这样的句子里猜要哪个数"
        )

    if value_kind in _MONEY_KINDS:
        has_scale_word = detect_scale_word(value_text, unit_text) is not None
        has_locator = bool(locator) and any(
            str(locator.get(k) or "").strip() for k in _LOCATOR_CONTEXT_KEYS
        )
        if not has_scale_word and not has_locator:
            raise NormalizationError(
                f"金额类数值缺规模上下文：原文 {value_text!r} 无规模词"
                f"（million/billion/百万/亿…），unit_text={unit_text!r} 未声明量表，"
                "且无 locator（document/page/table/row/column）——裸数字摘录不能直接"
                "变成可靠金额"
            )


def recompute_lineage(obs: MetricObservation) -> Decimal:
    """重算观测的换算血缘：raw.value_text + normalization 步骤 → 必须等于 obs.value。

    calculated/consensus/model_estimate 可以没有 raw（来源是公式/快照/产物），
    此时只要求 value 本身是合法十进制字符串。
    """
    if obs.value is None:
        raise NormalizationError(f"{obs.metric_key}: status={obs.status} 无值可重算")
    target = _dec(obs.value)
    if obs.raw is None:
        return target
    value = parse_raw_number(obs.raw.value_text)
    steps = [NormalizationStep.model_validate(s) for s in obs.normalization]
    for step in steps:
        value = apply_step(value, step)
    if value != target:
        raise NormalizationError(
            f"{obs.metric_key}: 换算链重算得 {format_decimal(value)}，与登记值 "
            f"{obs.value} 不一致（原文 {obs.raw.value_text!r}）"
        )
    return target


# ---------------- 逐叶数值校验（typed 入口；设计 §2.2「数字校验覆盖不足」） ----------------

_NUMERIC_STR_RE = re.compile(r"^-?\d[\d,]*\.?\d*$")


def assert_typed_leaves(payload: Any, *, path: str = "$") -> None:
    """嵌套结构逐叶校验：数值只能以「可解析为 Decimal 的字符串」出现。

    - 裸 int/float/bool 叶子一律拒绝（旧 guard 只查顶层，字符串/嵌套漏检的整改）；
    - 看起来像数字的字符串必须真能解析（"1.2 billion" 这类带规模词文本属于
      raw.value_text 的职责，不允许混进 assumptions/dimensions 的数值位）。
    """
    if isinstance(payload, dict):
        for k, v in payload.items():
            assert_typed_leaves(v, path=f"{path}.{k}")
        return
    if isinstance(payload, (list, tuple)):
        for i, v in enumerate(payload):
            assert_typed_leaves(v, path=f"{path}[{i}]")
        return
    if isinstance(payload, (bool, int, float)):
        raise NormalizationError(
            f"{path}: 数值叶子必须是十进制字符串（收到 {type(payload).__name__}）"
        )
    if isinstance(payload, str):
        s = payload.strip()
        if _NUMERIC_STR_RE.match(s) and any(c.isdigit() for c in s):
            try:
                Decimal(s.replace(",", ""))
            except InvalidOperation as e:
                raise NormalizationError(f"{path}: 数值字符串不可解析 {payload!r}") from e
        return
    if payload is None:
        return
    raise NormalizationError(f"{path}: 不允许的叶子类型 {type(payload).__name__}")
