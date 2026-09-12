"""What Changed 引擎（升级方案 §11/§32）：快照级结构化 diff → 投资语义变化日志。

纪律：
- 纯函数、确定性：只比较两个冻结快照 payload，不调用 LLM、不读文本情绪；
- 投资语义分类：thesis（结论/论断）/ risk（反证与证伪）/ metric（关键指标与预期）/
  company_status（候选分层变动）/ catalyst（验证时间线）/ module（模块状态迁移）；
- 图标语义（§11）：↑ 进展/改善 · ! 风险/恶化/冲突 · + 新增（候选/催化/论断）；
- 数字保护：指标值逐字引用快照里的十进制字符串（只搬移，不改写不换算）；
- 产出上限 6 条（§9 密度规则），按类别优先级截断；base=None（首次发布）→ 空列表，
  前端回退到研究轮次的 key_changes。
"""

from __future__ import annotations

from typing import Any

#: 变化条目的类别（前端按类归组展示；kind 是稳定契约词，不做中文硬译依赖）
ChangeKind = (
    "thesis", "risk", "metric", "company_status", "catalyst", "module",
)

#: 类别优先级（数字越小越靠前；§11 示例顺序：thesis → risk → metric → company → catalyst）
_KIND_PRIORITY = {
    "thesis": 0, "risk": 1, "metric": 2, "company_status": 3, "catalyst": 4, "module": 5,
}

#: 每类条目上限（洪水闸门：一次大更不应淹没首屏；company_status 给 4——
#: 一次候选评审常见 2 升级 + 1 降级 + 1 移除，截断会丢「移除」信号）
_KIND_CAP = {"thesis": 3, "risk": 2, "metric": 3, "company_status": 4, "catalyst": 3, "module": 2}

#: 输出总上限（§9 密度硬规则）
TOTAL_CAP = 6

_TIER_LABEL = {
    "included": "入选", "watchlist": "观察", "needs_review": "待核实", "excluded": "淘汰",
}
#: tier 升级方向（数字小 = 更核心）：向 included 移动 = 升级（↑），远离 = 降级（!）
_TIER_RANK = {"included": 0, "watchlist": 1, "needs_review": 2, "excluded": 3}

#: 模块状态迁移的图标：就绪/恢复 = ↑；退化/冲突/陈旧 = !；其余（partial 间移动）= ↑
_STATUS_ICON = {
    "ready": "up", "partial": "up", "missing": "risk", "stale": "risk",
    "conflicted": "risk", "not_applicable": "risk", "unavailable_at_as_of": "risk",
}
_STATUS_LABEL = {
    "ready": "就绪", "partial": "部分", "missing": "缺口", "stale": "陈旧",
    "conflicted": "冲突", "not_applicable": "不适用", "unavailable_at_as_of": "当时不可知",
}


def _entry(icon: str, kind: str, tag: str, text: str, refs: list[str] | None = None) -> dict[str, Any]:
    return {"icon": icon, "kind": kind, "tag": tag, "text": text, "refs": refs or []}


def _claims_map(snap: dict[str, Any]) -> dict[str, dict[str, Any]]:
    inputs = snap.get("inputs") or {}
    return {str(c.get("claim_id")): c for c in inputs.get("claims") or [] if c.get("claim_id")}


def _ev_refs(refs: list[str] | None) -> list[str]:
    return [r for r in (refs or []) if str(r).startswith("ev-")][:4]


def _thesis_entries(base: dict[str, Any], cur: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    bsum = base.get("summary") or {}
    csum = cur.get("summary") or {}
    b_thesis = str(bsum.get("thesis") or "").strip()
    c_thesis = str(csum.get("thesis") or "").strip()
    if c_thesis and c_thesis != b_thesis:
        icon = "new" if not b_thesis else "up"
        text = c_thesis if not b_thesis else f"核心结论更新：{c_thesis}"
        out.append(_entry(icon, "thesis", "核心结论", text, _ev_refs(csum.get("thesis_refs"))))
    for field, tag in (("stage", "阶段判断"), ("value_capture", "价值捕获")):
        b, c = str(bsum.get(field) or "").strip(), str(csum.get(field) or "").strip()
        if c and c != b:
            out.append(_entry(
                "new" if not b else "up", "thesis", tag,
                f"{tag}：{c}" if not b else f"{tag}更新：{b} → {c}",
            ))
    # 论断集 diff：新增 validated、draft→validated 升级、被替代/移除
    b_claims = _claims_map(base)
    for cid, c in _claims_map(cur).items():
        prev = b_claims.get(cid)
        if prev is None and c.get("status") == "validated":
            out.append(_entry(
                "new", "thesis", "新论断", str(c.get("statement") or ""),
                _ev_refs(c.get("support_refs")),
            ))
        elif prev is not None and prev.get("status") != "validated" \
                and c.get("status") == "validated":
            out.append(_entry(
                "up", "thesis", "论断通过校验", str(c.get("statement") or ""),
                _ev_refs(c.get("support_refs")),
            ))
    return out


def _risk_entries(base: dict[str, Any], cur: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    bsum = base.get("summary") or {}
    csum = cur.get("summary") or {}
    b_counter = str(bsum.get("counter_evidence") or "").strip()
    c_counter = str(csum.get("counter_evidence") or "").strip()
    if c_counter and c_counter != b_counter:
        out.append(_entry(
            "new" if not b_counter else "risk", "risk", "反证",
            f"最大反证：{c_counter}" if not b_counter else f"最大反证更新：{c_counter}",
            _ev_refs(csum.get("counter_refs")),
        ))
    b_breakers = {str(x) for x in bsum.get("thesis_breakers") or []}
    for br in csum.get("thesis_breakers") or []:
        if str(br) not in b_breakers:
            out.append(_entry("risk", "risk", "证伪条件", f"新增证伪条件：{br}"))
    # 论断被替代/消失 = 研究判断恶化（原来成立的结论不再成立）
    c_claims = _claims_map(cur)
    for cid, prev in _claims_map(base).items():
        now = c_claims.get(cid)
        if now is None:
            out.append(_entry("risk", "risk", "论断移除",
                              f"论断不再出现于当前快照：{prev.get('statement') or cid}"))
        elif now.get("status") == "superseded" and prev.get("status") != "superseded":
            out.append(_entry("risk", "risk", "论断被替代",
                              str(now.get("statement") or cid)))
    return out


def _metric_entries(base: dict[str, Any], cur: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    b_metrics = {
        m.get("metric_key"): m for m in (base.get("summary") or {}).get("key_metrics") or []
    }
    for m in (cur.get("summary") or {}).get("key_metrics") or []:
        key = m.get("metric_key")
        prev = b_metrics.get(key)
        label = str(m.get("label") or key)
        if prev is None:
            continue  # 配方/路由变化带来的「新卡」不是数据变化（噪声），跳过
        old_v, new_v = prev.get("value"), m.get("value")
        old_s, new_s = prev.get("status"), m.get("status")
        if old_v == new_v and old_s == new_s:
            continue
        if new_s == "conflicted":
            out.append(_entry("risk", "metric", label, f"{label} 出现开放冲突（同口径竞争值未裁决）"))
        elif new_s == "stale" and old_s != "stale":
            out.append(_entry("risk", "metric", label, f"{label} 超过新鲜度目标（数据陈旧）"))
        elif new_s == "missing" and old_s != "missing":
            out.append(_entry("risk", "metric", label, f"{label} 变为缺口"))
        elif old_s == "missing" and new_s == "ok":
            out.append(_entry("new", "metric", label, f"新增指标：{label} {new_v}"))
        elif old_v != new_v:
            out.append(_entry("up", "metric", label, f"{label}：{old_v} → {new_v}"))
    return out


def _company_entries(base: dict[str, Any], cur: dict[str, Any]) -> list[dict[str, Any]]:
    def _cands(snap: dict[str, Any]) -> dict[str, dict[str, Any]]:
        ca = (snap.get("structures") or {}).get("candidate_assessment") or {}
        return {
            str(c.get("entity_id")): c for c in ca.get("candidates") or [] if c.get("entity_id")
        }

    b_cands, c_cands = _cands(base), _cands(cur)
    out: list[dict[str, Any]] = []
    for cid, c in c_cands.items():
        name = str(c.get("name") or cid)
        tier = str(c.get("tier") or "")
        prev = b_cands.get(cid)
        if prev is None:
            out.append(_entry(
                "new", "company_status", "候选池",
                f"候选新增：{name}（{_TIER_LABEL.get(tier, tier)}）",
                _ev_refs(c.get("evidence_refs")),
            ))
            continue
        prev_tier = str(prev.get("tier") or "")
        if prev_tier and tier != prev_tier:
            rank_old = _TIER_RANK.get(prev_tier, 9)
            rank_new = _TIER_RANK.get(tier, 9)
            icon = "up" if rank_new < rank_old else "risk"
            out.append(_entry(
                icon, "company_status", "候选分层",
                f"{name}：{_TIER_LABEL.get(prev_tier, prev_tier)} → {_TIER_LABEL.get(tier, tier)}",
                _ev_refs(c.get("evidence_refs")),
            ))
    for cid, prev in b_cands.items():
        if cid not in c_cands:
            out.append(_entry("risk", "company_status", "候选池",
                              f"候选移除：{prev.get('name') or cid}"))
    return out


def _catalyst_entries(base: dict[str, Any], cur: dict[str, Any]) -> list[dict[str, Any]]:
    def _items(snap: dict[str, Any]) -> dict[str, dict[str, Any]]:
        vt = (snap.get("structures") or {}).get("validation_timeline") or {}
        return {str(i.get("event")): i for i in vt.get("items") or [] if i.get("event")}

    b_items, c_items = _items(base), _items(cur)
    out: list[dict[str, Any]] = []
    for event, item in c_items.items():
        prev = b_items.get(event)
        window = " → ".join(x for x in (item.get("window_start"), item.get("window_end")) if x)
        if prev is None:
            out.append(_entry(
                "new", "catalyst", "催化",
                f"新增验证节点：{event}" + (f"（{window}）" if window else ""),
                _ev_refs(item.get("evidence_refs")),
            ))
        elif prev.get("status") != "occurred" and item.get("status") == "occurred":
            out.append(_entry("up", "catalyst", "催化兑现", f"验证节点已发生：{event}",
                              _ev_refs(item.get("evidence_refs"))))
        elif prev.get("status") == "occurred" and item.get("status") != "occurred":
            out.append(_entry("risk", "catalyst", "催化",
                              f"已发生节点被改标为 {item.get('status')}：{event}（口径回退，需复核）"))
    for event in b_items:
        if event not in c_items:
            out.append(_entry("risk", "catalyst", "催化", f"验证节点移出时间线：{event}"))
    return out


def _module_entries(base: dict[str, Any], cur: dict[str, Any]) -> list[dict[str, Any]]:
    """模块状态迁移（状态变化才报；纯 data_ref 变化已被具体类别覆盖，不报）。"""
    out: list[dict[str, Any]] = []
    b_mods = base.get("modules") or {}
    for mod, state in (cur.get("modules") or {}).items():
        prev = b_mods.get(mod) or {}
        old_s, new_s = prev.get("status"), state.get("status")
        if not old_s or old_s == new_s:
            continue
        title = str(state.get("title") or mod)
        icon = _STATUS_ICON.get(str(new_s), "up")
        out.append(_entry(
            icon, "module", "模块",
            f"{title}：{_STATUS_LABEL.get(str(old_s), old_s)} → {_STATUS_LABEL.get(str(new_s), new_s)}",
        ))
    return out


def compute_change_log(base: dict[str, Any] | None, cur: dict[str, Any]) -> list[dict[str, Any]]:
    """上一发布快照 → 当前快照的结构化变化日志（§32）。

    base=None（首次发布）→ []（调用方回退到研究轮次的 key_changes）。
    """
    if base is None:
        return []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for kind, fn in (
        ("thesis", _thesis_entries),
        ("risk", _risk_entries),
        ("metric", _metric_entries),
        ("company_status", _company_entries),
        ("catalyst", _catalyst_entries),
        ("module", _module_entries),
    ):
        entries = fn(base, cur)
        if entries:
            grouped[kind] = entries[: _KIND_CAP[kind]]
    out: list[dict[str, Any]] = []
    for kind in sorted(grouped, key=lambda k: _KIND_PRIORITY[k]):
        out.extend(grouped[kind])
    return out[:TOTAL_CAP]


__all__ = ["compute_change_log", "TOTAL_CAP"]
