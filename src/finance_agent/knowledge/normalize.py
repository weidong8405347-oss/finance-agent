"""实体 ID 归一化：同一标的只允许有一个档案（验收事故：2228.HK 与 02228.HK 并存）。

规则（幂等，已规范的输入原样返回）：
- 港股 ``<数字>.HK``：去前导零后左补到 4 位（02228.HK → 2228.HK；700.HK → 0700.HK；
  5 位及以上保持去零后的数字，不截断）；
- A 股 ``<数字>.SZ/.SH/.SS``：去前导零后左补到 6 位；
- 美股：纯大写；
- 行业：小写、去首尾空白（slug 生成在 commands/steps.py 的 theme_slug，风格保持一致）。

落点：命令解析（parse_target）、研究工具写入口（propose_fact）、单写者
（ProfileWriter，兜底所有路径）三层都过 normalize——纵深防御，入口再多也不会分叉。
"""

from __future__ import annotations

import re

_HK = re.compile(r"^(?P<num>\d{1,5})\.HK$", re.IGNORECASE)
_CN = re.compile(r"^(?P<num>\d{1,6})\.(?P<ex>SZ|SH|SS)$", re.IGNORECASE)


def normalize_stock_id(raw: str) -> str:
    """股票代码 → 规范形（大写、交易所后缀大写、数字段去前导零后补齐）。"""
    s = raw.strip().upper()
    m = _HK.match(s)
    if m:
        return f"{m['num'].lstrip('0').zfill(4)}.HK"
    m = _CN.match(s)
    if m:
        return f"{m['num'].lstrip('0').zfill(6)}.{m['ex'].upper()}"
    return s


def normalize_entity_id(entity_kind: str, raw: str) -> str:
    """按实体类型归一；未知类型走股票规则（向后兼容历史命令路径）。"""
    if entity_kind == "industry":
        return raw.strip().lower()
    return normalize_stock_id(raw)
