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


def build_plan_brief(plan_payload: dict, *, assigned_question_ids: list[str] | None = None) -> str:
    """冻结研究计划的问题队列投影（§7.3）：每轮 brief 附带，模型按问题推进。

    assigned_question_ids 给定时只投影本 worker 被分配的问题（调度器已下发，
    worker 不自行推导归属）；全量投影（串行路径）传 None。
    """
    questions = plan_payload.get("questions", [])
    if assigned_question_ids is not None:
        wanted = set(assigned_question_ids)
        questions = [q for q in questions if q.get("question_id") in wanted]
    lines = [
        "本轮研究计划（已冻结，范围不可扩展；目标不是补齐字段而是回答这些问题）："
    ]
    for q in questions:
        lines.append(
            f"- [{q['question_id']}]（{q['priority']}/{q['status']}）{q['text']}"
        )
        if q.get("why"):
            lines.append(f"  为何影响判断：{q['why']}")
        if q.get("acceptance"):
            lines.append(f"  完成条件：{q['acceptance']}")
        if q.get("conclusion"):
            lines.append(f"  当前结论：{q['conclusion']}")
        if q.get("unresolved"):
            lines.append(f"  未解决项：{'；'.join(q['unresolved'])}")
    lines.append(
        "推进纪律：结构化数值用 propose_metric（原文值+期间+证据）；分析结论用 propose_claim；"
        "可重算关系用 calculate_metric；每完成一个问题立即 answer_question"
        "（answered 需结论+可解析引用；找不到数据标 unavailable 并记录尝试，"
        "不能以模型猜测完成事实采集）。"
    )
    return "\n".join(lines)


#: plan 模式专职提示词（audit §3.1）：有冻结计划时，交付物是「问题的答案」，
#: 旧字段补全只作为兼容副产物。事故形态：worker 没有问题 → 自由采集 + 刷旧字段，
#: 5 轮 576 次工具调用、0/9 问题推进。
PLAN_MODE_CONTRACT = """\
本轮是「问题驱动研究」，不是「档案字段补全」。交付物是下列问题的答案，逐题推进：
1. 证据：先用 query_* / read_* 拿到原文，再 register_evidence 登记逐字摘录；
   数字必须能在摘录里逐字定位（带规模词与表头，裸数字会被拒）。
2. 分析：把证据整理成 propose_metric（结构化数值）/ propose_claim（结论句），
   每条都要写清 question_id 归属。
3. 反证：主动找削弱结论的证据；找不到反证要在 limitations 里写明「未检索到反证」。
4. 提交：每题完成立即 answer_question(question_id, status, conclusion, support_refs)；
   查不到就标 unavailable 并记 attempts，不许留空拖到下一轮。
旧档案字段（propose_fact）只在回答问题的顺带产出时写；不要为了刷字段完整度而
消耗本轮预算——字段 100% 不等于研究充分。
"""


def build_round_brief(
    entity_kind: str,
    entity_id: str,
    objective: str,
    gaps: GapReport,
    round_no: int,
    judge_feedback: str | None = None,
    plan_payload: dict | None = None,
    typed_tools: bool = False,
    assigned_question_ids: list[str] | None = None,
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
    if gaps.weak:
        # 弱字段回流（准入闭环）：字段在但质检未过（内容过短/缺数值锚点/仅 C 级证据）→
        # 列出「字段（原因）」引导下轮用更可靠的证据重写替换旧值。软引导，不列入
        # 「只研究缺口字段」的硬约束，也不阻塞收敛。
        weak_line = "、".join(
            f"{field}（{'；'.join(issues)}）" for field, issues in gaps.weak.items()
        )
        parts.append(f"待改进字段（已有值但未过质检，优先重写替换）：{weak_line}")
    if gaps.optional_missing:
        parts.append("可选维度（有能力就补）：" + ", ".join(gaps.optional_missing))
    if gaps.conflicts:
        parts.append("存在冲突待裁决：" + ", ".join(gaps.conflicts))
    if judge_feedback:
        parts.append("上一轮评审反馈（软反馈，供参考）：" + judge_feedback)
    tools_line = "可用工具：register_evidence / propose_fact / query_kb / 数据源查询工具。"
    if typed_tools:
        tools_line = (
            "可用工具：register_evidence / propose_fact / propose_metric / propose_claim / "
            "answer_question / calculate_metric / query_kb / 数据源查询工具。"
        )
    parts.append(tools_line)
    if plan_payload:
        parts.append(PLAN_MODE_CONTRACT)
        parts.append(build_plan_brief(plan_payload, assigned_question_ids=assigned_question_ids))
    return "\n".join(parts)
