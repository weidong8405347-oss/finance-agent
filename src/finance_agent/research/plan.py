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
    "targeted": {"max_parallel_workers": 2, "retrieval_calls": 12, "wall_clock_minutes": 10, "max_rounds": 2},
}

#: 模式 → 问题规模指引（§7.1）
MODE_QUESTION_RANGE: dict[ResearchMode, tuple[int, int]] = {
    "standard": (6, 10),
    "deep": (12, 18),
    "refresh": (2, 6),
    "targeted": (1, 4),
}


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
    )


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
    if len(questions) > hi:
        rank = {"high": 0, "medium": 1, "low": 2}
        questions.sort(key=lambda q: rank[q.priority])
        questions = questions[:hi]
    budgets = Budgets(**MODE_BUDGETS[mode])
    if mode == "deep":
        budgets.question_coverage_target = 0.8
    return ResearchPlan(
        plan_id=f"plan-{uuid.uuid4().hex[:10]}",
        entity_kind=entity_kind,  # type: ignore[arg-type]
        entity_id=entity_id,
        objective=objective,
        mode=mode,
        recipe_id=recipe.id,
        recipe_version=recipe.version,
        questions=questions,
        acceptance=(
            f"适用关键问题 answered 覆盖 ≥{budgets.question_coverage_target:.0%}；"
            "关键数字与关键事实句引用覆盖 100%；disputed/unavailable 必须有原因与尝试记录"
        ),
        budgets=budgets,
        scope={"focus": focus, "missing_fields": sorted(missing), "stale_fields": sorted(stale)},
        base_snapshot_id=base_snapshot_id,
        created_at=now or datetime.now(UTC),
        run_id=run_id,
    )
