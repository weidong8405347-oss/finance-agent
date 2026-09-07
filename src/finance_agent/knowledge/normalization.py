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


def parse_raw_number(value_text: str) -> Decimal:
    """原文值文本 → Decimal 尾数（千分位容忍；规模词剥离交给 unit_word_scale）。

    歧义消解（数值语义测试组 §13.1）：
    1. 规模词紧邻的数字优先（'2026 revenue 1.2 billion' → 1.2）；
    2. 否则排除年份形态后取首个数字（'revenue 100 in 2026' → 100）；
    3. 全是年份形态才退回首个数字。
    """
    cleaned = value_text.strip().replace(",", "")
    matches = [m.group(0) for m in _NUM_RE.finditer(cleaned)]
    if not matches:
        raise NormalizationError(f"原文值文本中没有可解析数字: {value_text!r}")
    word = detect_scale_word(value_text, "")
    if word is not None:
        pattern = re.compile(
            rf"(-?\d[\d,]*\.?\d*)\s*{re.escape(word)}s?(?![a-z])", re.IGNORECASE
        )
        m = pattern.search(cleaned)
        if m is not None:
            return _dec(m.group(1))
    non_year = [t for t in matches if not _year_like(t)]
    return _dec(non_year[0] if non_year else matches[0])


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
