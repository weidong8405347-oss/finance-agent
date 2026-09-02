"""CommandRegistry：四个领域 command 的目录与 slash 解析（确定性，无模型参与）。

纪律（redesign §3.2/Q3）：slash = 程序员式入口——缺参报 usage_error 给用法，不猜不追问；
自然语言入口由主 agent 负责理解与补全（run_command 工具走同一派发管线）。

管道组成（复用四个 step + 独立评估）：
  /research  = research → process_eval
  /profile   = research → profile_update → process_eval
  /decide    = research → profile_update → decide → process_eval
  /evaluate  = evaluate（S4 独立效果评估；默认强制审批，--no-approval 当次豁免）
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CommandSpec:
    name: str
    summary: str
    usage: str
    steps: tuple[str, ...]
    needs_approval: bool = False  # True = 默认强制审批（可用 --no-approval 当次豁免）


COMMANDS: dict[str, CommandSpec] = {
    "research": CommandSpec(
        name="research",
        summary="S1 深度研究（轮次制）→ 报告合成 + 过程评估",
        usage="/research <标的> [研究目标]（标的可写 industry:<slug> 研究行业）",
        steps=("research", "synthesize", "process_eval"),
    ),
    "profile": CommandSpec(
        name="profile",
        summary="S1 研究 → S2 档案更新 → 报告合成 + 过程评估",
        usage="/profile <标的>",
        steps=("research", "profile_update", "synthesize", "process_eval"),
    ),
    "decide": CommandSpec(
        name="decide",
        summary="S1 → S2 → 报告合成 → S3 决策卡（risk-review 硬门禁）+ 过程评估",
        usage="/decide <标的>",
        steps=("research", "profile_update", "synthesize", "decide", "process_eval"),
    ),
    "evaluate": CommandSpec(
        name="evaluate",
        summary="S4 独立效果评估（eval 隔离环境；默认需审批）",
        usage="/evaluate <配置名> [--no-approval]",
        steps=("evaluate",),
        needs_approval=True,
    ),
    "industry": CommandSpec(
        name="industry",
        summary="行业调研漏斗：F1 赛道地图 → F1.5 产业判断备忘录 → F2 标的池 → "
                "F3 粗调研+人工闸口 → F4 深研 → F4.5 投资委员会 → F5 排序报告",
        usage="/industry <主题>（如 /industry AI for Science）",
        steps=("industry_map", "thesis", "candidate_pool", "screen", "deep_dive",
               "committee", "rank_report"),
    ),
}


@dataclass(frozen=True)
class ParsedCommand:
    """一次 slash 输入的解析结果。未知命令 name 原样保留，由 runner 报 unknown。"""

    name: str
    raw_input: str
    ticker: str = ""
    objective: str = ""
    config: str = ""
    no_approval: bool = False
    extra: dict[str, str] = field(default_factory=dict)


def parse_target(raw: str) -> tuple[str, str]:
    """标的解析：'industry:<slug>' → 行业；否则股票（代码大写归一）。"""
    if raw.lower().startswith("industry:"):
        return "industry", raw.split(":", 1)[1].strip().lower()
    return "stock", raw.upper()


def parse_command(text: str) -> ParsedCommand | None:
    """text 以 / 开头 → 解析为 ParsedCommand；否则 None（走主 agent 对话）。"""
    if not text.startswith("/"):
        return None
    tokens = text[1:].split()
    if not tokens:
        return ParsedCommand(name="", raw_input=text)
    name = tokens[0].lower()
    rest = tokens[1:]
    no_approval = False
    positional: list[str] = []
    for tok in rest:
        if tok == "--no-approval":
            no_approval = True
        else:
            positional.append(tok)
    ticker, config, objective = "", "", ""
    if name == "evaluate":
        config = positional[0] if positional else ""
    elif name == "industry":
        # 主题是自然语言整段（含空格），不走 ticker 大写归一
        objective = " ".join(positional).strip()
    elif positional:
        ticker = positional[0].upper()
        objective = " ".join(positional[1:]).strip()
    return ParsedCommand(
        name=name, raw_input=text, ticker=ticker, objective=objective,
        config=config, no_approval=no_approval,
    )


def catalog() -> list[dict[str, str | bool]]:
    """command 目录（composer 补全 / 主 agent prompt / GET /api/commands 共用）。"""
    return [
        {
            "name": spec.name,
            "summary": spec.summary,
            "usage": spec.usage,
            "needs_approval": spec.needs_approval,
        }
        for spec in COMMANDS.values()
    ]
