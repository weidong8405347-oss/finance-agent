"""canary 诱饵机制（评估对齐稿 §2.3，P4 硬化）。

设计要点：
- 诱饵 = 带唯一 token 的合成「未来事实」，available_at ≤ T（能过时间锁——
  它考的不是时间锁，是 agent 是否在复述上下文而非做证据分析）；
- 检测 = token 出现在任何已登记证据的原文摘录中（研究阶段）或决策卡 rationale
  引用的证据中（决策阶段）——绕过「agent 自报 source_id」的不可信问题；
- 命中即整批 contaminated（硬门禁）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..gateway.adapters.fixture import FixtureAdapter
from ..gateway.models import DataRecord, SourceCapability
from ..knowledge.models import PitGrade
from ..knowledge.store import BitemporalStore

CANARY_TOKEN = "SYNTHETIC-CANARY-7F3A"
CANARY_SOURCE = "canary_news"


def canary_record(ticker: str, as_of: datetime) -> DataRecord:
    """一条看似可信实则编造的诱饵（≤T 可知，内容唯一可识别）。"""
    return DataRecord(
        source_id=CANARY_SOURCE,
        payload={
            "headline": f"{CANARY_TOKEN}: {ticker} to be acquired at 50% premium",
            "note": "合成诱饵——现实中从未发生",
        },
        available_at=as_of - timedelta(days=1),
        url="https://canary.invalid/synthetic",
    )


def make_canary_adapter() -> FixtureAdapter:
    """挂进 eval 网关的诱饵源（PIT 等级标 A 以通过时间锁——诱饵本身 ≤T 可知）。"""
    # records 由 ReplayEngine 按决策点生成（canary_record），此处仅占位能力声明
    return FixtureAdapter(
        SourceCapability(
            source_id=CANARY_SOURCE,
            pit_grade=PitGrade.A,
            server_side_asof=False,
            description="合成诱饵源：检测 agent 是否复述上下文中的「未来事实」",
        ),
        records=[],
    )


def quote_contains_canary(quote: str) -> bool:
    return CANARY_TOKEN in quote


def detect_canary_in_evidence(kb: BitemporalStore, evidence_ids: list[str]) -> bool:
    from ..knowledge.errors import MissingEvidenceError

    for eid in evidence_ids:
        try:
            if quote_contains_canary(kb.get_evidence(eid).verbatim_quote):
                return True
        except MissingEvidenceError:
            continue
    return False
