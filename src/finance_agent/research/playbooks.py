"""playbook 加载器（research-capability-upgrade §4.8）。

纪律：
- playbook = 流程与方法论文本，playbooks/*.md 独立文件，git 版本管理；
- 文件缺失/损坏 → 回落代码内置默认（永不阻断 run）；
- 每次加载计算版本哈希（sha256 前 8 位），调用方落事件——run 可复现性；
- grounding 契约等硬纪律不在这里（那是 prompts.py 的代码内置，不许改）。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger("finance_agent.playbooks")

#: 内置默认（文件缺失时的兜底；保持最小可用）
_BUILTIN: dict[str, str] = {
    "industry_map": "拆解子赛道并绑证据；写 sub_sectors 与行业五字段；找不到就留白。",
    "candidate_pool": "多角度挖掘标的池；每票绑赛道归属证据；写 player_landscape；防漏检。",
    "screen": "每票粗调研卡（指标/亮点/风险/丰富度/理由）；推荐 ≤6 只；淘汰留理由。",
    "dimension_researcher": "只研究分配给你的字段；数字用 calc；找不到就留白；主动找反方证据。",
    "rank_report": "对比矩阵逐格引证据；排序理由落到驱动因子；潜力评级是判断不是事实。",
    "thesis": "产出一句话论点+瓶颈层定位+利润池排序+关键比率+反方假设+评分权重调整；先研究再判断。",
    "committee": "四视角对抗+空头证伪+CIO 综合；判断引证据；不出评级与仓位（那是 /decide 的域）。",
    "scoring": "四维打分（瓶颈/护城河/成长/估值）逐条绑证据；权重默认 40/25/20/15 可行业调整。",
}

_PLAYBOOK_DIR = Path(__file__).resolve().parents[3] / "playbooks"


def load_playbook(name: str) -> tuple[str, str]:
    """返回 (文本, 版本哈希)。文件优先，内置兜底；未知名称 → ValueError。"""
    if name not in _BUILTIN:
        raise ValueError(f"未知 playbook: {name}（可用：{sorted(_BUILTIN)}）")
    path = _PLAYBOOK_DIR / f"{name}.md"
    text = _BUILTIN[name]
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:  # 文件损坏不阻断 run——回落内置 + 日志可见
            logger.warning("playbook %s 读取失败（%s），回落内置默认", name, e)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
