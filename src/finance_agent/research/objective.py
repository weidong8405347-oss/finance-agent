"""目标编译：把用户 objective 编译成公司级研究交付（audit §3.4 P1）。

事故形态（live-a2cce641）：用户问「究竟哪些公司是真的在形成技术护城河以及有
比较大可能能够取得商业爆发」，standard/deep 却直接套配方 core/extended 问题，
objective 只是存储字段——九个题目主要是产业链、供需、政策、周期，答完它们也
证明不了回答了用户的问题。

本模块把目标**确定性**编译成高优先级问题（不经 LLM，可回放）：

- 先建立目标相关的公司比较问题，再补行业背景（背景问题完成不能代替目标完成）；
- 每个候选公司至少回答：技术壁垒 / 商业兑现 / 可持续性 / 反证 / 可投资范围；
- 「商业爆发概率」没有可校准数据时，用证据支持的阶段与条件表达，不自动制造
  百分比或总分；
- 未上市公司可作技术参照，但不混入可交易候选（上市状态/市场/证券关系单列）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .plan import RecipeQuestion

#: 目标里出现这些信号 → 需要公司级比较交付（而不是只做行业背景）
_COMPANY_COMPARISON_SIGNALS: tuple[str, ...] = (
    "哪些公司", "哪家公司", "哪些企业", "哪家企业", "哪些标的", "什么公司", "谁是",
    "公司比较", "对比公司", "候选", "标的池", "龙头", "受益股", "受益公司", "产业链公司",
    "护城河", "壁垒", "竞争优势", "商业爆发", "爆发", "放量", "拐点", "兑现",
    "which companies", "which company", "who are the", "moat", "competitive edge",
    "best positioned", "leaders", "winner", "pick", "stocks to", "commercial breakout",
)

#: 「商业爆发/兑现」类信号 → 追加一题（阶段与条件表达，不造概率）
_BREAKOUT_SIGNALS: tuple[str, ...] = (
    "商业爆发", "爆发", "放量", "拐点", "兑现", "起量", "规模化", "breakout", "inflection",
    "commercialize", "scale up",
)


@dataclass(frozen=True)
class ObjectiveDimension:
    """目标的一个分析维度 → 一个高优先级研究问题模板。"""

    key: str
    label: str
    #: 问题文本模板（{objective} 占位）
    template: str
    acceptance: str
    #: 行业实体的落点模块 / 股票实体的落点模块
    industry_module: str
    stock_module: str
    evidence_types: tuple[str, ...] = ("filing", "news", "industry_report")
    why: str = ""


#: 五个必备维度（audit §3.4）：技术壁垒 / 商业兑现 / 可持续性 / 反证 / 可投资范围
OBJECTIVE_DIMENSIONS: tuple[ObjectiveDimension, ...] = (
    ObjectiveDimension(
        key="technology_moat", label="技术壁垒",
        template=(
            "围绕研究目标「{objective}」：哪些公司在技术壁垒上有可核验证据？"
            "逐家给出：独占数据或数据飞轮、模型与实验闭环、外部同行验证、"
            "现有替代方案、复制难度与所需时间/资本。"
        ),
        acceptance=(
            "每家候选至少一条一手证据（论文/专利/客户验证/产品指标）；"
            "无证据的优势不得写入；复制难度必须说明依据而不是形容词"
        ),
        industry_module="candidate_pool", stock_module="business_engine",
        evidence_types=("publication", "filing", "industry_report", "news"),
        why="护城河判断决定「谁真的在形成壁垒」，是目标的第一交付",
    ),
    ObjectiveDimension(
        key="commercial_proof", label="商业兑现",
        template=(
            "围绕研究目标「{objective}」：这些公司的商业兑现到了什么程度？"
            "收入、已收款、首付款、潜在里程碑、合同上限必须分开列；"
            "增长来自什么（新签/复购/扩单/并表），可持续部分占多少。"
        ),
        acceptance=(
            "每项金额带期间、币种、口径与观测引用；潜在总额不得记成已收首付款；"
            "未披露的项明确留空，不用推测填补"
        ),
        industry_module="key_kpi", stock_module="financial_quality",
        evidence_types=("filing", "ir_page", "news"),
        why="技术优势必须落到可核验的商业数字，否则只是叙事",
    ),
    ObjectiveDimension(
        key="sustainability", label="可持续性",
        template=(
            "围绕研究目标「{objective}」：这些公司的优势可持续吗？"
            "复购/扩单证据、交付与产能能力、客户集中度、毛利与现金消耗、"
            "关键人/关键许可依赖。"
        ),
        acceptance=(
            "每项给出证据或明确「未披露」；客户集中度与现金消耗若可得必须给数值；"
            "不因为找不到数据就默认可持续"
        ),
        industry_module="candidate_pool", stock_module="financial_quality",
        evidence_types=("filing", "transcript", "news"),
        why="一次性订单与结构性需求要分开，否则爆发判断站不住",
    ),
    ObjectiveDimension(
        key="counter_evidence", label="反证",
        template=(
            "围绕研究目标「{objective}」：哪些证据削弱上述公司的优势？"
            "什么事件会证伪当前判断（技术替代、客户流失、监管、竞品数据）？"
            "下一次可验证的时间点是什么？"
        ),
        acceptance=(
            "每家候选至少一条 steelman 反方证据或明确「未检索到反证」；"
            "证伪条件必须可观察、可定时；不得只列通用风险套话"
        ),
        industry_module="catalysts_risks", stock_module="risks",
        evidence_types=("news", "short_report", "publication", "industry_report"),
        why="没有反证的优势判断不可投资",
    ),
    ObjectiveDimension(
        key="investability", label="可投资范围",
        template=(
            "围绕研究目标「{objective}」：这些公司里哪些是可交易标的？"
            "逐家给出上市状态、市场与代码、证券与公司的对应关系（母子公司/ADR/借壳）、"
            "流通与解禁约束。未上市公司单列为技术参照，不混入可交易候选。"
        ),
        acceptance=(
            "上市/未上市分开；每个可交易候选给出市场与证券标识及其与经营主体的关系；"
            "关系不明确时标「待核实」，不得默认同一主体"
        ),
        industry_module="candidate_pool", stock_module="peers",
        evidence_types=("filing", "exchange_listing", "news"),
        why="研究结论要能落到可执行的投资范围，否则用户无法使用",
    ),
)

#: 商业爆发/兑现维度：无可校准数据时用阶段与条件表达（不造百分比与总分）
BREAKOUT_DIMENSION = ObjectiveDimension(
    key="commercial_breakout", label="商业爆发条件",
    template=(
        "围绕研究目标「{objective}」：哪些公司更可能实现商业爆发？"
        "用**证据支持的阶段与触发条件**表达：当前所处阶段（验证/首单/复购/规模化）、"
        "距离爆发还差哪些可观察条件、每个条件的验证时点与来源。"
    ),
    acceptance=(
        "不得输出无校准依据的概率百分比或综合评分；"
        "每家给出阶段判定 + 触发条件 + 下次可验证时点；"
        "阶段判定必须绑定已登记证据或观测"
    ),
    industry_module="candidate_pool", stock_module="business_engine",
    evidence_types=("filing", "transcript", "news", "industry_report"),
    why="用户问的是「爆发可能性」，但可校准数据不存在时只能给阶段与条件",
)


def wants_company_comparison(objective: str, entity_kind: str) -> tuple[bool, str]:
    """目标是否要求公司级比较交付（确定性关键词判定，命中依据可回放）。"""
    text = (objective or "").strip()
    if not text:
        return False, "objective 为空"
    low = text.lower()
    hits = [s for s in _COMPANY_COMPARISON_SIGNALS if s.lower() in low]
    if not hits:
        return False, "未命中公司比较信号"
    if entity_kind == "industry":
        return True, f"行业实体 + 公司比较信号 {hits[:4]}"
    # 单公司实体：命中「护城河/爆发/兑现」类信号时同样需要结构化比较交付
    strong = [s for s in hits if s in ("护城河", "壁垒", "商业爆发", "爆发", "兑现", "拐点",
                                       "moat", "breakout", "inflection")]
    if strong:
        return True, f"公司实体 + 强信号 {strong[:4]}"
    return False, f"公司实体且仅命中弱信号 {hits[:4]}（沿用配方问题）"


def build_objective_questions(
    objective: str,
    entity_kind: str,
    *,
    include_breakout: bool | None = None,
    limit: int = 6,
) -> list[RecipeQuestion]:
    """目标 → 高优先级问题模板（确定性；id 由目标哈希派生，可幂等对照）。"""
    digest = hashlib.sha256(f"{entity_kind}:{objective}".encode()).hexdigest()[:6]
    low = (objective or "").lower()
    if include_breakout is None:
        include_breakout = any(s.lower() in low for s in _BREAKOUT_SIGNALS)
    dims = list(OBJECTIVE_DIMENSIONS)
    if include_breakout:
        dims.insert(1, BREAKOUT_DIMENSION)
    out: list[RecipeQuestion] = []
    for dim in dims[:limit]:
        module = dim.industry_module if entity_kind == "industry" else dim.stock_module
        out.append(RecipeQuestion(
            id=f"objective-{dim.key}-{digest}",
            text=dim.template.format(objective=objective.strip()),
            why=dim.why or f"用户目标维度：{dim.label}",
            priority="high",
            evidence_types=list(dim.evidence_types),
            acceptance=dim.acceptance,
            module=module,
        ))
    return out


def objective_dimension_keys(objective: str, entity_kind: str) -> list[str]:
    """本次目标编译覆盖了哪些维度（进 plan.scope，供验收与页面显示）。"""
    wanted, basis = wants_company_comparison(objective, entity_kind)
    if not wanted:
        return []
    low = (objective or "").lower()
    keys = [d.key for d in OBJECTIVE_DIMENSIONS]
    if any(s.lower() in low for s in _BREAKOUT_SIGNALS):
        keys.insert(1, BREAKOUT_DIMENSION.key)
    del basis
    return keys


def objective_headline(objective: str) -> str:
    """首屏结论必须回答的那句话（投影层用，不改写目标本身）。"""
    text = re.sub(r"\s+", " ", (objective or "").strip())
    return text[:180]


__all__ = [
    "ObjectiveDimension", "OBJECTIVE_DIMENSIONS", "BREAKOUT_DIMENSION",
    "wants_company_comparison", "build_objective_questions",
    "objective_dimension_keys", "objective_headline",
]
