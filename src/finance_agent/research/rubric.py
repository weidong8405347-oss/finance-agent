"""research-rubric：LLM-as-judge 软反馈插件。

定位（D4）：advisory——评分只做软反馈进下一轮 brief，永不参与硬判定，
解析失败静默降级（失败无害）。
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field, ValidationError

from ..llm.base import LLM

RUBRIC_PROMPT = """\
你是研究质量评审（rubric judge）。对刚结束的一轮研究打分（1-5）：
- completeness: 缺口覆盖进展
- evidence_quality: 证据是否一手、可追溯
- counter_evidence: 是否主动寻找了反对证据
- coherence: 结论与证据的一致性
并列出最重要的待补缺口 gaps（字符串数组）。
只输出 JSON：{"completeness": int, "evidence_quality": int, "counter_evidence": int,
"coherence": int, "gaps": [str], "notes": str}
"""


class RubricScore(BaseModel):
    completeness: int = Field(ge=1, le=5)
    evidence_quality: int = Field(ge=1, le=5)
    counter_evidence: int = Field(ge=1, le=5)
    coherence: int = Field(ge=1, le=5)
    gaps: list[str] = Field(default_factory=list)
    notes: str = ""


class RubricJudge:
    def __init__(self, llm: LLM):
        self._llm = llm

    def judge(self, round_digest: str) -> RubricScore | None:
        """返回评分；解析失败返回 None（advisory：失败无害）。"""
        reply = self._llm.complete(
            [
                {"role": "system", "content": RUBRIC_PROMPT},
                {"role": "user", "content": round_digest},
            ],
            tools=[],
        )
        return parse_rubric(reply.content)


def parse_rubric(text: str) -> RubricScore | None:
    """从模型输出提取 JSON（容忍前后杂文本）；失败返回 None。"""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return RubricScore(**json.loads(text[start : end + 1]))
    except (json.JSONDecodeError, ValidationError, TypeError):
        return None
