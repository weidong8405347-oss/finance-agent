"""研究环节的 prompt 契约。

Grounding 纪律（DESIGN.md §4.3 防线 3）：模型是证据的分析者，不是事实的来源。
"""

from __future__ import annotations

from ..knowledge.gaps import GapReport

GROUNDING_CONTRACT = """\
你是一名投资研究分析师。纪律（违反即被拒绝）：
1. 证据只能来自你实际检索到的内容：query_* 工具返回的记录自带 chunk_id；
   需要 filing 正文时用 read_edgar_filing(chunk_id, query=关键词) 抓出原文窗口。
2. 登记证据用 register_evidence(chunk_id, verbatim_quote)：quote 必须是该 chunk
   的逐字原文（服务端校验子串，不符即拒）；来源与可知时刻由系统推导，不得自报。
   禁止凭你的记忆写入任何事实或数字——没读到原文就不要写。
3. 数字必须与证据原文逐字一致，不允许换算或约估。
4. 本轮只研究下方列出的缺口字段；找不到可靠证据就保持缺失，不要编造。
"""


def build_round_brief(
    entity_kind: str,
    entity_id: str,
    objective: str,
    gaps: GapReport,
    round_no: int,
    judge_feedback: str | None = None,
) -> str:
    parts = [
        f"研究目标：{objective}",
        f"实体：{entity_kind}:{entity_id}（第 {round_no} 轮）",
        f"当前完整度：{gaps.completeness:.0%}",
    ]
    if gaps.missing:
        parts.append("缺失字段：" + ", ".join(gaps.missing))
    if gaps.stale:
        parts.append("待更新（陈旧）字段：" + ", ".join(gaps.stale))
    if gaps.conflicts:
        parts.append("存在冲突待裁决：" + ", ".join(gaps.conflicts))
    if judge_feedback:
        parts.append("上一轮评审反馈（软反馈，供参考）：" + judge_feedback)
    parts.append("可用工具：register_evidence / propose_fact / query_kb / 数据源查询工具。")
    return "\n".join(parts)
