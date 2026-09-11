"""消融开关（tools-plugins 方案 §10.2 插件消融 + B 组 F14 诊断运行）。

纪律：
- **仅评估/试点用途**（哨兵运行器、消融驱动脚本经环境变量开启）；
  生产默认全量开启（空集合 = 无消融），能力页/API 不暴露；
- 开关只切执行路径，不删除数据、不改历史事件；每次运行把生效的消融集合
  写进运行环境存档（结果可归因：哪个组件被关了）；
- 组件清单对应方案 §10.2 的消融项 + F14 诊断项。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: 消融组件 → 环境变量（值 1/true/yes 生效）
_FLAG_ENV: dict[str, str] = {
    # 状态卡语义压缩回流（F14 诊断：状态卡是否挤压采集步数）
    "state_card": "FA_ABLATE_STATE_CARD",
    # 内容级核验（verify_claim + S2 核验 + 合成批量核验）
    "verifier": "FA_ABLATE_VERIFIER",
    # SearchBroker 双源代理（search_sources 工具）
    "broker": "FA_ABLATE_BROKER",
    # 统一知识读取（S1 worker 的 get_research_context/query_*/read_evidence 等）
    "knowledge_context": "FA_ABLATE_KNOWLEDGE_CONTEXT",
    # 第二搜索源（Tavily；验证双源合并的净收益）
    "second_search": "FA_ABLATE_SECOND_SEARCH",
}

#: 合法消融组件（拼错的开关名 fail-loud，不静默忽略——消融运行必须可归因）
ABLATION_COMPONENTS = frozenset(_FLAG_ENV)

_TRUE = {"1", "true", "yes", "on"}


def ablation_flags(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """环境变量 → 生效的消融组件集合（frozenset；空 = 生产默认全量开启）。"""
    environ = os.environ if env is None else env
    return frozenset(
        flag for flag, var in _FLAG_ENV.items()
        if str(environ.get(var) or "").strip().lower() in _TRUE
    )


def ablation_notes(flags: frozenset[str]) -> dict[str, str]:
    """运行环境存档用：组件 → 关闭的能力（一句话）。"""
    notes = {
        "state_card": "状态卡语义压缩回流关闭（research/context_compressed 不再产生）",
        "verifier": "内容级核验关闭（verify_claim 工具/合成批量核验不装配）",
        "broker": "SearchBroker 双源代理关闭（search_sources 不装配）",
        "knowledge_context": "S1 统一知识读取关闭（worker 只走检索/文档/写入工具）",
        "second_search": "第二搜索源（Tavily）不注册进网关",
    }
    return {f: notes[f] for f in sorted(flags)}


__all__ = ["ABLATION_COMPONENTS", "ablation_flags", "ablation_notes"]
