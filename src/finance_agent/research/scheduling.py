"""研究调度器：把冻结计划编译成显式 WorkItem（audit §3.1 P0）。

2026-09-08 现场事故（live-a2cce641）：`_question_groups()` 用问题 module 原名建
worker 组，`_group_plan_view()` 又把 module 映射成旧维度名再比较——两套口径互不
相交，20 个 worker 全部拿到空计划视图，5 轮 576 次工具调用、0/9 问题推进。

本模块是「组」这件事的唯一真相源：

- 建组（哪些 worker）与下发（每个 worker 拿到哪些问题）复用同一张
  `entity_kind + module → worker_group` 表；
- 调度产物是显式 `WorkItem(group, fields, question_ids, acceptance)`——worker 不再
  自行推导问题归属；
- 启动前断言：待执行问题集合被完整分配；有待执行问题时不产出空问题 worker
  （`assert_dispatch_complete`，fail-loud 而不是烧 53 分钟空转）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 维度组定义（P3 §4.2）：字段 → 专职 researcher 组。按实体类型分表——
#: 行业字段与股票字段不同集（2026-09-01 实测：行业字段全落 misc 单组，丢失并行性）
DIMENSION_GROUPS_STOCK: dict[str, tuple[str, ...]] = {
    "financial": ("revenue_fy", "net_income_fy", "cash_flow", "valuation"),
    "business": ("business_model", "moat"),
    "industry": ("peers", "market_share", "future_space"),
    # talent_density 归 risk_mgmt 组（与 management 同根：都是「人」的维度，
    # 2026-09-02 前未登记 → 轮转进随机组，挖不到专业指引 → 维度全空）
    "risk_mgmt": ("risks", "management", "talent_density", "catalysts", "counter_evidence"),
}
DIMENSION_GROUPS_INDUSTRY: dict[str, tuple[str, ...]] = {
    "market": ("market_size", "growth_rate", "future_space"),
    "landscape": ("value_chain", "competition", "sub_sectors"),
    # player_landscape 不在 F1：它是 F2 标的池挖掘的专属产出（专用工具校验+并集纪律）
    "policy_players": ("policy",),
}

#: 计划问题 module → worker 组（唯一映射；建组与下发共用，audit §3.1）。
#: 按 entity_kind 分表：行业 `key_kpi` 是供需/规模，绝不能落进股票 `financial` 组。
WORKER_GROUP_BY_MODULE: dict[str, dict[str, str]] = {
    "stock": {
        "financial_quality": "financial",
        "financials": "financial",
        "valuation": "financial",
        "valuation_lab": "financial",
        "expectations": "financial",
        "key_kpi": "financial",
        "business_engine": "business",
        "revenue_segments": "business",
        "peers": "industry",
        "management": "industry",
        "moat": "business",
        "market_share": "industry",
        "risks": "risk_mgmt",
        "catalysts": "risk_mgmt",
        "catalysts_risks": "risk_mgmt",
        "talent_density": "risk_mgmt",
        "counter_evidence": "risk_mgmt",
    },
    "industry": {
        # 行业组名与 DIMENSION_GROUPS_INDUSTRY 对齐：字段缺口路径与问题路径同名同组，
        # 两条路径产出的 worker 可互相接续（不会各建一套互不相认的组）。
        "industry_chain": "landscape",
        "investment_snapshot": "landscape",
        "key_kpi": "market",
        "market": "market",
        "policy": "policy_players",
        "catalysts_risks": "policy_players",
        # 候选池是独立交付物（公司证据矩阵），不并入 landscape 叙述组
        "candidate_pool": "candidate_pool",
        "research_sources": "research_sources",
    },
}

#: 未登记模块的兜底组名 = 模块本身（确定性；不静默丢进 misc 失去并行度）
_FALLBACK_GROUP = "misc"


class DispatchError(RuntimeError):
    """调度装配缺陷：问题未完整分配 / 产出空问题 worker（fail-loud，不空转）。"""


def worker_group(entity_kind: str, module: str) -> str:
    """问题 module → worker 组名（唯一入口；建组与过滤都走这里）。"""
    table = WORKER_GROUP_BY_MODULE.get(entity_kind) or WORKER_GROUP_BY_MODULE["stock"]
    if not module:
        return _FALLBACK_GROUP
    return table.get(module, module)


def dimension_groups(
    missing: list[str], stale: list[str], optional_missing: list[str],
    *, entity_kind: str = "stock", weak: list[str] | None = None,
) -> list[tuple[str, list[str]]]:
    """缺口字段（缺失+陈旧+可选）按维度分组；未登记进组的字段轮转分配。

    注：player_landscape 永不分组（F2 专属产出，见 DIMENSION_GROUPS_INDUSTRY 注释）。
    弱字段回流（2026-09-03 整改收尾）：weak 只挂进「因缺口已激活」的本维度组，
    让专职组顺带重写；不新建组、不轮转（weak 是引导不是缺口，不许扩大并行面）。
    无对应激活组 → 本轮不回流（serial 路径的 brief 全量投影仍可见）。
    """
    table = DIMENSION_GROUPS_INDUSTRY if entity_kind == "industry" else DIMENSION_GROUPS_STOCK
    pending = [f for f in dict.fromkeys([*missing, *stale, *optional_missing])
               if f != "player_landscape"]
    groups: list[list] = []
    assigned: set[str] = set()
    for gname, gfields in table.items():
        hit = [f for f in pending if f in gfields]
        if hit:
            groups.append([gname, hit])
            assigned.update(hit)
    rest = [f for f in pending if f not in assigned]
    for i, f in enumerate(rest):
        if groups:
            groups[i % len(groups)][1].append(f)
        else:
            groups.append(["misc", [f]])
    for f in weak or []:
        if f in assigned or f == "player_landscape":
            continue
        for g in groups:
            if f in table.get(g[0], ()):
                g[1].append(f)
                assigned.add(f)
                break
    return [(g, fs) for g, fs in groups]


#: 已终结的问题状态（不再下发、不算覆盖率分母）
CLOSED_STATUSES = frozenset({"answered", "not_applicable"})


def pending_questions(plan_payload: dict | None) -> list[dict]:
    """待执行问题（状态未终结）——调度与覆盖率共用的同一口径。"""
    if not plan_payload:
        return []
    return [
        q for q in plan_payload.get("questions", [])
        if q.get("status") not in CLOSED_STATUSES
    ]


@dataclass(frozen=True)
class WorkItem:
    """一个 worker 的本轮任务：显式字段缺口 + 显式问题 id + 显式验收条件。

    worker 不再自行推导「我该答哪些问题」（事故根因）；question_ids 是硬契约。
    """

    group: str
    fields: tuple[str, ...] = ()
    question_ids: tuple[str, ...] = ()
    #: 问题 id → 验收条件（brief 直接引用，避免 worker 自行改写完成标准）
    acceptance: dict[str, str] = field(default_factory=dict)
    #: 问题 id → module（审计/诊断用）
    modules: dict[str, str] = field(default_factory=dict)
    #: 由字段缺口折叠而来（该组原本只有字段、没有问题）
    folded_from: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "fields": list(self.fields),
            "question_ids": list(self.question_ids),
            "acceptance": dict(self.acceptance),
            "modules": dict(self.modules),
            "folded_from": list(self.folded_from),
        }


@dataclass
class Schedule:
    """一轮的调度结果：可执行 WorkItem + 装配自检信息（全部落事件，可回放）。"""

    items: list[WorkItem]
    #: 待执行但没进任何 worker 的问题 id（必须为空，否则 assert_dispatch_complete 抛错）
    unassigned: list[str] = field(default_factory=list)
    #: 无模块归属的问题（targeted 自定义问题）：对全部 worker 可见，回答幂等
    global_question_ids: list[str] = field(default_factory=list)
    #: 字段组折叠记录（可见，不静默）
    folded: list[dict[str, Any]] = field(default_factory=list)
    #: 超出 worker 上限被合并的组
    merged_over_cap: list[str] = field(default_factory=list)

    @property
    def dispatched_question_ids(self) -> list[str]:
        """实际进入执行上下文的问题 id（去重、保持顺序）。"""
        out: list[str] = []
        for item in self.items:
            for qid in item.question_ids:
                if qid not in out:
                    out.append(qid)
        return out

    def as_payload(self) -> dict[str, Any]:
        return {
            "items": [i.as_payload() for i in self.items],
            "unassigned": list(self.unassigned),
            "global_question_ids": list(self.global_question_ids),
            "folded": list(self.folded),
            "merged_over_cap": list(self.merged_over_cap),
            "dispatched_question_ids": self.dispatched_question_ids,
        }


def build_schedule(
    *,
    plan_payload: dict | None,
    entity_kind: str,
    field_groups: list[tuple[str, list[str]]],
    max_workers: int | None = None,
) -> Schedule:
    """字段缺口 × 计划问题 → WorkItem 列表（确定性，无 LLM 参与）。

    规则：
    1. 每个待执行问题按 `worker_group(entity_kind, module)` 归组；无 module 的问题
       （targeted 自定义）为全局问题，对每个 worker 都可见；
    2. 字段组与问题组按组名合并（同名 = 同一个 worker，字段与问题一起推进）；
    3. 有待执行问题时不允许空问题 worker：只带字段缺口的组，其字段折叠进问题组
       （优先同组名，否则字段最少的问题组），折叠过程记入 `Schedule.folded`；
    4. 组数超过 `max_workers` 时按问题优先级/数量排序保留，落选组的问题重分配到
       保留组（问题一个不丢），落选组名记入 `merged_over_cap`。
    """
    pending = pending_questions(plan_payload)
    by_group: dict[str, dict[str, Any]] = {}

    def _slot(group: str) -> dict[str, Any]:
        return by_group.setdefault(
            group, {"fields": [], "question_ids": [], "acceptance": {}, "modules": {}}
        )

    for group, fields in field_groups:
        slot = _slot(group)
        for f in fields:
            if f not in slot["fields"]:
                slot["fields"].append(f)

    globals_: list[str] = []
    for q in pending:
        qid = str(q.get("question_id") or "")
        if not qid:
            continue
        module = str(q.get("module") or "")
        if not module:
            globals_.append(qid)
            continue
        slot = _slot(worker_group(entity_kind, module))
        if qid not in slot["question_ids"]:
            slot["question_ids"].append(qid)
        slot["acceptance"][qid] = str(q.get("acceptance") or "")
        slot["modules"][qid] = module

    folded: list[dict[str, Any]] = []
    question_groups = [g for g, s in by_group.items() if s["question_ids"]]
    if pending and question_groups:
        # 规则 3：有待执行问题 → 空问题组的字段折叠进问题组
        for group in [g for g, s in by_group.items() if not s["question_ids"]]:
            src = by_group.pop(group)
            if not src["fields"]:
                continue
            target = group if group in question_groups else min(
                question_groups, key=lambda g: (len(by_group[g]["fields"]), g)
            )
            for f in src["fields"]:
                if f not in by_group[target]["fields"]:
                    by_group[target]["fields"].append(f)
            folded.append({"from_group": group, "into_group": target,
                           "fields": list(src["fields"])})
    elif pending and not question_groups and not globals_:
        # 计划里全是无模块问题：单组承载（不建 N 个空组）
        _slot(_FALLBACK_GROUP)

    # 全局问题对每个 worker 可见（回答幂等，重复推进无害）
    for slot in by_group.values():
        for qid in globals_:
            if qid not in slot["question_ids"]:
                slot["question_ids"].append(qid)

    items = [
        WorkItem(
            group=g,
            fields=tuple(s["fields"]),
            question_ids=tuple(s["question_ids"]),
            acceptance={qid: s["acceptance"].get(qid, "") for qid in s["question_ids"]},
            modules={qid: s["modules"].get(qid, "") for qid in s["question_ids"]},
            folded_from=tuple(f["from_group"] for f in folded if f["into_group"] == g),
        )
        for g, s in sorted(by_group.items())
    ]

    if max_workers is not None and max_workers > 0 and len(items) > max_workers:
        items, merged = _cap_items(items, max_workers)
        schedule = Schedule(items=items, global_question_ids=globals_, folded=folded,
                            merged_over_cap=merged)
    else:
        schedule = Schedule(items=items, global_question_ids=globals_, folded=folded)

    schedule.unassigned = [
        qid for qid in (str(q.get("question_id") or "") for q in pending)
        if qid and qid not in schedule.dispatched_question_ids
    ]
    return schedule


def _cap_items(items: list[WorkItem], cap: int) -> tuple[list[WorkItem], list[str]]:
    """组数超上限：保留问题最多/优先级最高的组，落选组的问题与字段并入保留组。"""
    ordered = sorted(
        items,
        key=lambda it: (-len(it.question_ids), it.group),
    )
    keep, drop = ordered[:cap], ordered[cap:]
    merged: list[WorkItem] = []
    for i, item in enumerate(keep):
        extra_q = [qid for d in drop for qid in d.question_ids if qid not in item.question_ids]
        extra_f = [f for d in drop for f in d.fields if f not in item.fields]
        # 轮转分配，避免全部压到第一个 worker
        qids = list(item.question_ids) + [q for j, q in enumerate(extra_q) if j % cap == i]
        fields = list(item.fields) + [f for j, f in enumerate(extra_f) if j % cap == i]
        acc = dict(item.acceptance)
        mods = dict(item.modules)
        for d in drop:
            for qid in d.question_ids:
                acc.setdefault(qid, d.acceptance.get(qid, ""))
                mods.setdefault(qid, d.modules.get(qid, ""))
        merged.append(WorkItem(
            group=item.group, fields=tuple(dict.fromkeys(fields)),
            question_ids=tuple(dict.fromkeys(qids)),
            acceptance={q: acc.get(q, "") for q in dict.fromkeys(qids)},
            modules={q: mods.get(q, "") for q in dict.fromkeys(qids)},
            folded_from=(*item.folded_from, *(d.group for d in drop)),
        ))
    return merged, [d.group for d in drop]


def assert_dispatch_complete(schedule: Schedule, plan_payload: dict | None) -> None:
    """启动前断言（audit §3.1）：不满足即 fail-loud，绝不带着空计划开跑。

    - 待执行问题必须全部进入某个 worker 的执行上下文；
    - 有待执行问题时不允许出现空问题 worker（事故形态：worker 自由采集、0 问题推进）。
    """
    pending = pending_questions(plan_payload)
    if not pending:
        return
    if schedule.unassigned:
        raise DispatchError(
            f"问题未完整分发：{schedule.unassigned} 没有进入任何 worker 的执行上下文"
            f"（已分发 {schedule.dispatched_question_ids}）"
        )
    empty = [it.group for it in schedule.items if not it.question_ids]
    if empty:
        raise DispatchError(
            f"存在待执行问题时不允许创建空问题 worker：{empty}"
            "（worker 没有验收条件会退化为自由采集）"
        )


def plan_view(plan_payload: dict | None, question_ids: list[str] | tuple[str, ...]) -> dict | None:
    """worker 看到的计划子集：严格按调度器下发的 question_ids 投影（不再二次映射）。

    返回 None = 本 worker 本轮没有问题（brief 不附计划段落）。
    """
    if not plan_payload or not question_ids:
        return None
    wanted = set(question_ids)
    questions = [
        q for q in plan_payload.get("questions", [])
        if q.get("question_id") in wanted and q.get("status") not in CLOSED_STATUSES
    ]
    if not questions:
        return None
    return {**plan_payload, "questions": questions}
