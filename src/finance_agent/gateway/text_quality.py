"""抓取文本可读性判定（audit §3.2/§3.5）。

事故样本：`obs-723282fae4d1` 的摘录与 raw 是乱码，却仍以 `status=ok` 写进了
指标库（xtalpi_major_deal_upfront=4 USD）。PIT 等级只说明「时间来源性质」，
表达不了文本可读性与数字可靠性——质量必须单独标记，并在指标入口拦住。

判定是确定性的规则（不依赖模型、不依赖网络）：
- garbled：抽取结果是编码噪声（替换字符/控制字符占比高、可读字符占比过低、
  拉丁文本元音比例异常）——不许进指标库；
- needs_ocr：PDF 有页但抽不出文字（扫描件）——需要 OCR 或人工核对；
- partial：能读但结构受损（丢空格、行断裂、过短）——可作线索，不可作金额依据；
- ok：可正常引用。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: 可读字符：字母/数字/CJK/常用标点
_READABLE = re.compile(r"[\w\u4e00-\u9fff\u3040-\u30ff.,%$€£()\-–—:;/'\"&+ ]", re.UNICODE)
_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")
_LATIN = re.compile(r"[A-Za-z]")
_VOWELS = re.compile(r"[AEIOUaeiou]")
_WORDS = re.compile(r"[A-Za-z]{2,}")
_REPLACEMENT = "\ufffd"

#: PDF 每页平均抽出字符数低于此值 → 扫描件（needs_ocr）
_MIN_CHARS_PER_PAGE = 20


@dataclass(frozen=True)
class TextQuality:
    """抓取质量标记（进 chunk/Evidence，指标门禁据此拒写）。"""

    quality: str  # ok / partial / garbled / needs_ocr
    reasons: tuple[str, ...] = ()
    #: 0..1 可读性打分（诊断/排序用；门禁只看 quality）
    score: float = 1.0

    @property
    def usable_for_metrics(self) -> bool:
        """能否作为结构化数值的来源（garbled/needs_ocr 一律不行）。"""
        return self.quality in ("ok", "partial")

    def as_payload(self) -> dict:
        return {"quality": self.quality, "reasons": list(self.reasons),
                "score": round(self.score, 3)}


def assess_text_quality(text: str, *, pages: int | None = None) -> TextQuality:
    """纯文本 → 质量标记（确定性规则，fail-safe：噪声判 garbled，不判 ok）。"""
    if not text or not text.strip():
        if pages:
            return TextQuality("needs_ocr", ("pdf_no_text",), 0.0)
        return TextQuality("garbled", ("empty_text",), 0.0)

    stripped = text.strip()
    total = len(stripped)
    reasons: list[str] = []

    replacement_ratio = stripped.count(_REPLACEMENT) / total
    if replacement_ratio > 0.002:
        reasons.append(f"replacement_chars={replacement_ratio:.3%}")

    control = sum(
        1 for ch in stripped
        if unicodedata.category(ch)[0] == "C" and ch not in "\n\r\t"
    ) / total
    if control > 0.01:
        reasons.append(f"control_chars={control:.3%}")

    readable = len(_READABLE.findall(stripped)) / total
    if readable < 0.6:
        reasons.append(f"readable_ratio={readable:.2f}")

    cjk = len(_CJK.findall(stripped))
    latin = len(_LATIN.findall(stripped))
    score = readable
    if latin and not cjk:
        vowels = len(_VOWELS.findall(stripped)) / latin
        if vowels < 0.15:
            reasons.append(f"vowel_ratio={vowels:.2f}")
        words = _WORDS.findall(stripped)
        if words:
            avg_word = sum(len(w) for w in words) / len(words)
            if avg_word > 22:
                reasons.append(f"avg_word_len={avg_word:.0f}（抽取丢空格）")
                score = min(score, 0.6)
    digits = sum(1 for ch in stripped if ch.isdigit())
    if digits and total and digits / total > 0.5:
        reasons.append(f"digit_ratio={digits / total:.2f}（表格碎屑，缺表头语义）")
        score = min(score, 0.5)

    if pages is not None and total / max(1, pages) < _MIN_CHARS_PER_PAGE:
        reasons.append(f"chars_per_page={total / max(1, pages):.0f}")
        return TextQuality("needs_ocr", tuple(reasons), min(score, 0.2))

    hard = [r for r in reasons if r.startswith(("replacement_chars", "control_chars",
                                                "readable_ratio", "vowel_ratio"))]
    if hard:
        return TextQuality("garbled", tuple(reasons), min(score, 0.2))
    if reasons:
        return TextQuality("partial", tuple(reasons), score)
    return TextQuality("ok", (), score)


__all__ = ["TextQuality", "assess_text_quality"]
