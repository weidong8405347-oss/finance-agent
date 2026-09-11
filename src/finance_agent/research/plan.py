"""ResearchPlan：问题驱动的研究计划（设计 §7.1/§7.3/§7.5/§7.7）。

从「补齐档案字段」升级为「本轮固定要回答哪些问题、如何验收、花多少预算」：
- 计划创建即冻结（questions/acceptance/budgets 不再随轮次漂移）；
- 已有 100% 档案遇到新目标仍创建计划——只复用有效证据，不宣告「无需研究」；
- 问题状态机：unanswered → gathering → answered / disputed / unavailable / not_applicable；
- 预算按模式配置（§7.7 表），行业配方提供问题模板与 KPI 定义（recipes/*.yaml）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("finance_agent.research.plan")

ResearchMode = Literal["standard", "deep", "refresh", "targeted"]
QuestionStatus = Literal[
    "unanswered", "gathering", "answered", "disputed", "unavailable", "not_applicable"
]
QuestionPriority = Literal["high", "medium", "low"]

#: §7.7 预算表：最大并行 worker / 检索调用预算 / wall-clock 上限（分钟）/ 最大轮数
MODE_BUDGETS: dict[ResearchMode, dict[str, int]] = {
    "standard": {"max_parallel_workers": 4, "retrieval_calls": 30, "wall_clock_minutes": 15, "max_rounds": 3},
    "deep": {"max_parallel_workers": 4, "retrieval_calls": 80, "wall_clock_minutes": 40, "max_rounds": 5},
    "refresh": {"max_parallel_workers": 2, "retrieval_calls": 15, "wall_clock_minutes": 10, "max_rounds": 2},
    # targeted 检索预算 12→20（基线发现 F9：文档工具时代 fetch/search 也扣检索预算，
    # NVDA 题实测 12/12 打满；墙钟不变，成本由 RunBudget 继续封顶）
    "targeted": {"max_parallel_workers": 2, "retrieval_calls": 20, "wall_clock_minutes": 10, "max_rounds": 2},
}

#: 模式 → 问题规模指引（§7.1）
MODE_QUESTION_RANGE: dict[ResearchMode, tuple[int, int]] = {
    "standard": (6, 10),
    "deep": (12, 18),
    "refresh": (2, 6),
    "targeted": (1, 4),
}


class SubQuestion(BaseModel):
    """内部子问题/待查线索（方案 §8.1：冻结目标，允许内部研究路径演进）。

    硬约束：子问题只追加到所属问题条目下，**不扩大投资范围或预算**；
    带触发证据与退出条件（可审计的研究路径，不是自由发挥）。
    """

    model_config = ConfigDict(extra="forbid")

    sub_id: str
    parent_question_id: str
    text: str
    trigger_evidence: list[str] = Field(default_factory=list)  # 触发本子问题的证据/发现
    priority: QuestionPriority = "medium"
    exit_condition: str = ""  # 查到什么算完（防止无限发散）
    status: Literal["open", "answered", "dropped"] = "open"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResearchQuestion(BaseModel):
    """研究问题的最小结构（§7.3）。"""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    text: str
    why: str = ""  # 为何影响判断
    priority: QuestionPriority = "medium"
    evidence_types: list[str] = Field(default_factory=list)  # filing/ir_page/transcript/news...
    status: QuestionStatus = "unanswered"
    conclusion: str | None = None  # 当前结论（answered/disputed 时必填语义由验收检查）
    support_refs: list[str] = Field(default_factory=list)  # evidence/observation/claim id
    counter_refs: list[str] = Field(default_factory=list)  # 反证引用
    computable_checks: list[str] = Field(default_factory=list)  # 可计算检验（formula_id 提示）
    unresolved: list[str] = Field(default_factory=list)  # 未解决项
    acceptance: str = ""  # 完成条件
    cost: int = 0  # 已花费检索调用数（调度启发式）
    module: str = ""  # 关联档案模块（business_engine/financials/...）
    attempts: list[str] = Field(default_factory=list)  # disputed/unavailable 的尝试记录
    #: 内部子问题（§8.1）：版本化追加；不改预算与范围，调度器不单独分发
    sub_questions: list[SubQuestion] = Field(default_factory=list)
    #: 数值型问题（基线发现 F2）：answered 时 support_refs 必须含 typed 依据
    #: （obs-/calc-）——关键数字必须沉淀进指标库（可重算/可画图/受门禁），
    #: 不得只留在答案文本里。编译期冻结（模块/关键词判定），工具层执行。
    expects_typed_evidence: bool = False


class Budgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_parallel_workers: int = 4
    retrieval_calls: int = 30
    wall_clock_minutes: int = 15
    max_rounds: int = 3
    #: 验收目标：适用关键问题 answered 覆盖率（§7.6）
    question_coverage_target: float = 0.8


class ResearchPlan(BaseModel):
    """一轮研究的冻结契约：回答什么、如何验收、花多少。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    entity_kind: Literal["stock", "industry"]
    entity_id: str
    objective: str
    mode: ResearchMode = "standard"
    recipe_id: str = "general"
    recipe_version: str = "1"
    questions: list[ResearchQuestion] = Field(default_factory=list)
    acceptance: str = ""
    budgets: Budgets = Field(default_factory=Budgets)
    scope: dict[str, Any] = Field(default_factory=dict)  # focus/modules/periods 限定
    base_snapshot_id: str | None = None  # targeted/refresh 的对照基线
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    #: 问题状态最后一次更新时刻（存储层维护）：历史投影据此判断状态是否可分辨（review #10）
    updated_at: datetime | None = None
    status: Literal["active", "completed", "abandoned"] = "active"
    run_id: str | None = None
    frozen: bool = True

    def plan_hash(self) -> str:
        canon = json.dumps(
            {
                "entity": f"{self.entity_kind}:{self.entity_id}",
                "objective": self.objective,
                "mode": self.mode,
                "recipe": f"{self.recipe_id}@{self.recipe_version}",
                "questions": sorted(q.question_id for q in self.questions),
            },
            ensure_ascii=False, sort_keys=True,
        )
        return "plan-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]

    def applicable_key_questions(self) -> list[ResearchQuestion]:
        """适用关键问题（§7.6 覆盖率分母）：high 优先级且非 not_applicable。"""
        return [q for q in self.questions if q.priority == "high" and q.status != "not_applicable"]

    def question(self, question_id: str) -> ResearchQuestion | None:
        return next((q for q in self.questions if q.question_id == question_id), None)


# ---------------- 行业配方（playbooks/research/recipes/*.yaml，§7.5） ----------------


class KpiSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    unit_hint: str = ""
    required: bool = False
    definition: str = ""


class RecipeQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    why: str = ""
    priority: QuestionPriority = "medium"
    evidence_types: list[str] = Field(default_factory=list)
    acceptance: str = ""
    module: str = ""
    computable_checks: list[str] = Field(default_factory=list)


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str = "1"
    name: str = ""
    entity_kind: Literal["stock", "industry"] = "stock"
    modules: list[str] = Field(default_factory=list)
    kpis: list[KpiSpec] = Field(default_factory=list)
    core_questions: list[RecipeQuestion] = Field(default_factory=list)
    extended_questions: list[RecipeQuestion] = Field(default_factory=list)
    models: dict[str, Any] = Field(default_factory=dict)  # 模型适用条件
    freshness: dict[str, int] = Field(default_factory=dict)  # 模块 → 目标天数
    detection_hints: list[str] = Field(default_factory=list)  # 行业识别关键词（GICS/业务描述）


_RECIPES_DIR = Path(__file__).resolve().parents[3] / "playbooks" / "research" / "recipes"


def load_recipe(recipe_id: str, *, recipes_dir: str | Path | None = None) -> Recipe:
    """加载配方；文件缺失/损坏 fail-loud（配方是硬契约的一部分，不静默兜底）。"""
    path = Path(recipes_dir or _RECIPES_DIR) / f"{recipe_id}.yaml"
    if not path.is_file():
        available = sorted(p.stem for p in Path(recipes_dir or _RECIPES_DIR).glob("*.yaml"))
        raise FileNotFoundError(f"未知研究配方 {recipe_id!r}（可用：{available}）")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Recipe.model_validate(data)


def list_recipes(*, recipes_dir: str | Path | None = None) -> list[str]:
    return sorted(p.stem for p in Path(recipes_dir or _RECIPES_DIR).glob("*.yaml"))


def select_recipe(
    entity_kind: str,
    *,
    hint_text: str = "",
    explicit: str | None = None,
    recipes_dir: str | Path | None = None,
) -> tuple[str, str]:
    """行业识别（§7.5）：显式指定优先；否则按关键词提示匹配；不确定用通用。

    返回 (recipe_id, basis)——basis 说明选择依据（来源可见、可更改）。
    """
    if explicit:
        load_recipe(explicit, recipes_dir=recipes_dir)  # 不存在 fail-loud
        return explicit, "explicit"
    if entity_kind == "industry":
        try:
            load_recipe("industry", recipes_dir=recipes_dir)
            return "industry", "entity_kind=industry"
        except FileNotFoundError:
            return "general", "entity_kind=industry（无行业配方，回落通用）"
    low = hint_text.lower()
    if low:
        for rid in list_recipes(recipes_dir=recipes_dir):
            if rid == "general":
                continue
            try:
                recipe = load_recipe(rid, recipes_dir=recipes_dir)
            except Exception:
                continue
            if any(h.lower() in low for h in recipe.detection_hints):
                return rid, f"detection_hint 命中（{rid}）"
    return "general", "不确定时使用通用模板（不硬套行业 KPI）"


# ---------------- 计划构建 ----------------


def _to_question(rq: RecipeQuestion) -> ResearchQuestion:
    return ResearchQuestion(
        question_id=rq.id,
        text=rq.text,
        why=rq.why,
        priority=rq.priority,
        evidence_types=list(rq.evidence_types),
        acceptance=rq.acceptance,
        module=rq.module,
        computable_checks=list(rq.computable_checks),
        expects_typed_evidence=(rq.module in NUMERIC_MODULES or bool(rq.computable_checks)),
    )


#: 数值密集模块（基线发现 F2）：这些模块的问题 answered 必须有 typed 依据
NUMERIC_MODULES = frozenset({
    "financials", "financial_quality", "revenue_segments", "valuation", "expectations",
})

#: 自定义问题（targeted/focus）的数值题启发式：编译期判定后冻结进计划，
#: 不在运行期对模型自报做推断
_TYPED_TEXT_HINTS = re.compile(
    r"(收入|营收|利润|现金流|订单|在手|backlog|金额|规模|增速|增长|市值|估值|单价|"
    r"出货量|产能|装机|份额|毛利|净利|burn|revenue|margin|cash|sales|growth|"
    r"valuation|market cap|guidance|指引)",
    re.IGNORECASE,
)


def text_expects_typed(text: str) -> bool:
    """自定义问题文本的数值题判定（确定性规则，可回放）。"""
    return bool(_TYPED_TEXT_HINTS.search(text or ""))


def build_plan(
    *,
    entity_kind: str,
    entity_id: str,
    objective: str,
    mode: ResearchMode = "standard",
    recipe: Recipe,
    focus: str = "",
    missing_fields: list[str] | None = None,
    stale_fields: list[str] | None = None,
    base_snapshot_id: str | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
) -> ResearchPlan:
    """确定性计划构建（不经 LLM）：配方问题模板 × 模式 × 档案缺口 × focus。

    - targeted：focus/objective 生成 1 个自定义问题 + 配方里语义相近的问题；
    - refresh：陈旧字段映射的问题 + 配方 core 里标记 module 与之相关者；
    - standard：core 问题（缺口字段映射的问题优先级提升为 high）；
    - deep：core + extended。
    问题数超出模式上限时按优先级截断（high > medium > low）。
    """
    missing = set(missing_fields or [])
    stale = set(stale_fields or [])
    questions: list[ResearchQuestion] = []
    seen: set[str] = set()

    def _add(q: ResearchQuestion) -> None:
        if q.question_id in seen:
            return
        seen.add(q.question_id)
        questions.append(q)

    #: 旧 schema 字段 → 配方 module 的映射（缺口提升问题优先级用）
    field_to_module = {
        "revenue_fy": "financials", "net_income_fy": "financials", "cash_flow": "financials",
        "valuation": "valuation", "business_model": "business_engine", "moat": "peers",
        "risks": "risks", "peers": "peers", "management": "management",
        "catalysts": "catalysts", "counter_evidence": "risks",
    }
    gap_modules = {field_to_module.get(f, "") for f in (missing | stale)} - {""}

    if mode == "targeted":
        target_text = focus or objective
        # 确定性 id（实体+目标哈希）：同一目标重复建计划可幂等对照，脚本/测试可预测
        digest = hashlib.sha256(f"{entity_id}:{target_text}".encode()).hexdigest()[:8]
        _add(ResearchQuestion(
            question_id=f"targeted-{digest}",
            text=target_text,
            why="用户指定的研究目标（targeted 模式）",
            priority="high",
            acceptance="结论有直接证据支撑；无法量化时明确「无法量化」并记录尝试",
            expects_typed_evidence=text_expects_typed(target_text),
        ))
        for rq in recipe.core_questions:
            if focus and (focus.lower() in rq.text.lower() or rq.id.lower() in focus.lower()):
                _add(_to_question(rq))
    elif mode == "refresh":
        for rq in [*recipe.core_questions, *recipe.extended_questions]:
            if rq.module in gap_modules or any(
                f for f in stale if field_to_module.get(f) == rq.module
            ):
                q = _to_question(rq)
                q.priority = "high"
                q.why = (q.why + "；").rstrip("；") + "refresh：受影响模块（新披露/陈旧）"
                _add(q)
        if not questions:  # 无可映射的陈旧模块 → 至少刷新核心问题
            for rq in recipe.core_questions[:4]:
                _add(_to_question(rq))
    else:
        for rq in recipe.core_questions:
            q = _to_question(rq)
            if q.module in gap_modules and q.priority != "high":
                q.priority = "high"  # 档案缺口对应的问题提级
            _add(q)
        if mode == "deep":
            for rq in recipe.extended_questions:
                _add(_to_question(rq))

    lo, hi = MODE_QUESTION_RANGE[mode]
    # 目标编译（audit §3.4）：standard/deep 不再直接套配方问题——先把用户目标
    # 编译成公司级比较问题（高优先），再补行业背景；截断时背景题先让位。
    objective_keys: list[str] = []
    objective_basis = ""
    if mode in ("standard", "deep"):
        from .objective import build_objective_questions, wants_company_comparison

        wanted, objective_basis = wants_company_comparison(objective, entity_kind)
        if wanted:
            fresh = [
                _to_question(rq) for rq in build_objective_questions(objective, entity_kind)
                if rq.id not in seen
            ]
            for q in fresh:
                seen.add(q.question_id)
            # 目标题排在最前：截断时先让行业背景题让位（目标优先）
            questions[:0] = fresh
            objective_keys = [q.question_id for q in fresh]
    if len(questions) > hi:
        rank = {"high": 0, "medium": 1, "low": 2}
        # 稳定排序：同优先级下保留插入顺序（目标题先于背景题）
        questions.sort(key=lambda q: rank[q.priority])
        questions = questions[:hi]
    # focus 编译（基线发现 F1）：standard/deep 的 --focus 不再只存 scope——
    # 用户显式关注点编译为高优先专门问题，排在最前（截断时背景题先让位）。
    # 基线事故形态：BE --focus=订单口径与收入确认，计划仍是标准 12 题，
    # 哨兵题的核心场景根本没被研究。
    focus_key: str | None = None
    if focus and mode in ("standard", "deep"):
        f_digest = hashlib.sha256(f"{entity_id}:{focus}".encode()).hexdigest()[:8]
        fq_id = f"focus-{f_digest}"
        if fq_id not in seen:
            questions.insert(0, ResearchQuestion(
                question_id=fq_id,
                text=focus,
                why="用户指定的关注点（--focus；standard/deep 模式同样编译为专门问题）",
                priority="high",
                acceptance=("结论有直接证据支撑；涉及数值时以 typed 观测/计算为据；"
                            "无法量化时明确说明并记录尝试"),
                expects_typed_evidence=text_expects_typed(focus),
            ))
            seen.add(fq_id)
            focus_key = fq_id
    budgets = Budgets(**MODE_BUDGETS[mode])
    if mode == "deep":
        budgets.question_coverage_target = 0.8
    acceptance = (
        f"适用关键问题 answered 覆盖 ≥{budgets.question_coverage_target:.0%}；"
        "关键数字与关键事实句引用覆盖 100%；disputed/unavailable 必须有原因与尝试记录"
    )
    if objective_keys:
        acceptance = (
            f"用户目标必须被直接回答（目标题 {len(objective_keys)} 道全部有结论或明确未解决原因）；"
            "背景题完成不能代替目标完成；" + acceptance
        )
    if focus_key:
        acceptance = f"用户关注点（focus 题 {focus_key}）必须被直接回答；" + acceptance
    return ResearchPlan(
        plan_id=f"plan-{uuid.uuid4().hex[:10]}",
        entity_kind=entity_kind,  # type: ignore[arg-type]
        entity_id=entity_id,
        objective=objective,
        mode=mode,
        recipe_id=recipe.id,
        recipe_version=recipe.version,
        questions=questions,
        acceptance=acceptance,
        budgets=budgets,
        scope={
            "focus": focus, "missing_fields": sorted(missing), "stale_fields": sorted(stale),
            # 目标编译可回放（audit §3.4）：哪些题来自目标、判定依据是什么
            "objective_question_ids": objective_keys,
            "objective_decomposition": objective_basis,
            "focus_question_id": focus_key,
        },
        base_snapshot_id=base_snapshot_id,
        created_at=now or datetime.now(UTC),
        run_id=run_id,
    )
