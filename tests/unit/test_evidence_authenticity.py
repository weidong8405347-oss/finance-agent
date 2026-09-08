"""来源真实性验收（audit §3.5 P1 + §5 验收用例 3）。

事故形态（live-a2cce641）：证据基准保存为 JSON 序列化字符串，核验却用原文子串
匹配——换行、引号被转义后，模型复制真实正文也可能被拒。本次 44 次拒绝中，事件
重放至少能确认 9 次 quote 是解码正文的逐字子串，不能全部归因于模型编造。

判据：换行/引号/中文标点不误拒；改写与不在所选内容的数字仍然拒绝。
"""

from datetime import UTC, datetime

import pytest

from finance_agent.knowledge.models import PitGrade
from finance_agent.research.evidence_desk import (
    ChunkStore,
    EvidenceVerificationError,
    split_spans,
    verify_and_build,
)

NOW = datetime(2024, 6, 1, tzinfo=UTC)

DOC = (
    "营业收入\n"
    "2024 财年，公司实现营业收入 1,234 百万元，同比增长 18%。\n"
    "管理层称：“我们相信 AI for Science 平台已形成技术壁垒”。\n"
    "表 3：分部收入（单位：百万元）\n"
    "| 分部 | 2024 | 2023 |\n"
    "| AI for Science | 193,518 | 81,864 |\n"
)


def _store(text: str = DOC, *, quality: str = "ok", locator: dict | None = None) -> ChunkStore:
    store = ChunkStore()
    store.add(source_id="web_search", text=text, url="https://x.com/a",
              available_at=NOW, pit_grade=PitGrade.B, quality=quality,
              locator=locator or {"page": "12", "table": "3"})
    return store


class TestNoFalseRejection:
    """真实摘录不得被误拒（转写/编码差异不是编造）。"""

    def test_newline_in_quote_is_accepted(self):
        store = _store()
        ev = verify_and_build(store, chunk_id="chk-0001",
                              verbatim_quote="营业收入\n2024 财年，公司实现营业收入 1,234 百万元")
        assert "1,234 百万元" in ev.verbatim_quote

    def test_json_escaped_newline_from_transport_is_accepted(self):
        """模型从 json.dumps 的工具响应里复制正文 → 带 \\n 转义，仍须接受。"""
        store = _store()
        ev = verify_and_build(
            store, chunk_id="chk-0001",
            verbatim_quote="营业收入\\n2024 财年，公司实现营业收入 1,234 百万元",
        )
        assert ev.verbatim_quote.startswith("营业收入")

    def test_escaped_quotes_are_accepted(self):
        store = _store()
        ev = verify_and_build(store, chunk_id="chk-0001",
                              verbatim_quote='管理层称：\\"我们相信 AI for Science 平台已形成技术壁垒\\"')
        assert "技术壁垒" in ev.verbatim_quote

    def test_typographic_and_fullwidth_punctuation_are_accepted(self):
        """弯引号/直引号、全角/半角标点折叠后比对（同形不同码不算改写）。"""
        store = _store()
        ev = verify_and_build(
            store, chunk_id="chk-0001",
            verbatim_quote='管理层称："我们相信 AI for Science 平台已形成技术壁垒"。',
        )
        assert "技术壁垒" in ev.verbatim_quote
        # 全角逗号/冒号 → 半角
        ev2 = verify_and_build(store, chunk_id="chk-0001",
                               verbatim_quote="2024 财年,公司实现营业收入 1,234 百万元")
        assert "1,234" in ev2.verbatim_quote

    def test_whitespace_collapse_is_accepted(self):
        store = _store()
        ev = verify_and_build(store, chunk_id="chk-0001",
                              verbatim_quote="表 3：分部收入（单位：百万元）    | 分部 | 2024 | 2023 |")
        assert "193,518" not in ev.verbatim_quote  # 只取到表头（下一段另有 span）


class TestRealRejection:
    """改写、编造、跨内容取数仍然必须拒（fail-closed 不放松）。"""

    def test_paraphrase_is_rejected_with_actionable_hint(self):
        store = _store()
        with pytest.raises(EvidenceVerificationError) as ei:
            verify_and_build(store, chunk_id="chk-0001",
                             verbatim_quote="公司 2024 财年收入约 12.34 亿元并保持增长")
        assert ei.value.code == "quote_mismatch"
        assert ei.value.hint and "read_chunk" in ei.value.hint

    def test_number_not_in_selected_content_is_rejected(self):
        """不在所选内容里的数字（改写量级/换数字）仍然拒。"""
        store = _store()
        with pytest.raises(EvidenceVerificationError) as ei:
            verify_and_build(store, chunk_id="chk-0001",
                             verbatim_quote="2024 财年，公司实现营业收入 1,234,000 百万元")
        assert ei.value.code == "quote_mismatch"

    def test_unknown_chunk_and_empty_quote_have_distinct_codes(self):
        store = _store()
        with pytest.raises(EvidenceVerificationError) as ei:
            verify_and_build(store, chunk_id="chk-9999", verbatim_quote="任意")
        assert ei.value.code == "unknown_chunk"
        with pytest.raises(EvidenceVerificationError) as ei:
            verify_and_build(store, chunk_id="chk-0001", verbatim_quote="   ")
        assert ei.value.code == "empty_quote"

    def test_cross_chunk_quote_is_rejected(self):
        """两段不相邻正文拼接不算逐字摘录（应分条登记或用 span）。"""
        store = _store()
        with pytest.raises(EvidenceVerificationError):
            verify_and_build(store, chunk_id="chk-0001",
                             verbatim_quote="营业收入 193,518 81,864")


class TestSpanReference:
    """span 引用：服务端填摘录，转写误差为零（audit §3.5 优先路径）。"""

    def test_spans_are_stable_and_deterministic(self):
        assert split_spans(DOC) == split_spans(DOC)
        store = _store()
        chunk = store.get("chk-0001")
        assert chunk.spans
        assert any("193,518" in s for s in chunk.spans)

    def test_register_by_span_id_yields_exact_text(self):
        store = _store()
        chunk = store.get("chk-0001")
        idx = next(i for i, s in enumerate(chunk.spans) if "193,518" in s)
        ev = verify_and_build(store, span_id=f"chk-0001#s{idx}")
        assert ev.verbatim_quote == chunk.spans[idx]
        assert ev.source_id == "web_search"
        assert ev.available_at == NOW  # 由 chunk 推导，不信模型自报
        assert ev.pit_grade is PitGrade.B

    def test_unknown_span_is_rejected_with_code(self):
        store = _store()
        with pytest.raises(EvidenceVerificationError) as ei:
            verify_and_build(store, span_id="chk-0001#s999")
        assert ei.value.code == "unknown_span"
        with pytest.raises(EvidenceVerificationError) as ei:
            verify_and_build(store, span_id="chk-9999#s0")
        assert ei.value.code == "unknown_chunk"

    def test_span_id_accepts_bare_and_prefixed_forms(self):
        store = _store()
        chunk = store.get("chk-0001")
        for form in ("chk-0001#s0", "chk-0001#0"):
            ev = verify_and_build(store, span_id=form)
            assert ev.verbatim_quote == chunk.spans[0]


class TestQualityAndLocator:
    """抓取质量与定位随证据落库（PIT 等级表达不了可读性，audit §3.2/§3.5）。"""

    def test_quality_propagates_to_evidence(self):
        store = _store(quality="garbled")
        ev = verify_and_build(store, chunk_id="chk-0001",
                              verbatim_quote="2024 财年，公司实现营业收入 1,234 百万元")
        assert ev.quality == "garbled"

    def test_locator_propagates_to_evidence(self):
        store = _store()
        ev = verify_and_build(store, chunk_id="chk-0001", verbatim_quote="表 3：分部收入")
        assert ev.locator == {"page": "12", "table": "3"}

    def test_needs_ocr_marker_survives_registration(self):
        store = _store(text="扫描件无正文 1234", quality="needs_ocr")
        ev = verify_and_build(store, chunk_id="chk-0001", verbatim_quote="扫描件无正文 1234")
        assert ev.quality == "needs_ocr"
