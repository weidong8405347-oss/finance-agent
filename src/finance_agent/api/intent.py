"""对话意图路由：从自然语言消息提取标的 + 判定意图（research / decide / chat）。

确定性实现（不消耗 LLM 调用）；识别不了 → chat 追问澄清，不静默猜。
"""

from __future__ import annotations

import re

# A 股：60/68/00/30 开头的 6 位
_CN_TICKER = re.compile(r"\b(?:60|68|00|30)\d{4}\b")
# 美股：1-5 位大写字母（需滤掉常见英文词）
_US_TICKER = re.compile(r"\b[A-Z]{1,5}\b")
_US_STOPWORDS = {
    "I", "A", "OK", "AI", "ETF", "CEO", "USA", "GDP", "CPI", "IT", "IS", "IN", "ON", "AT",
    "TO", "DO", "BE", "WE", "HE", "AN", "AS", "IF", "OR", "SO", "UP", "US", "NOW", "NEW",
    "ALL", "CAN", "MAY", "NOT", "BUY", "SELL", "THE", "AND", "FOR", "ARE", "BUT", "VS",
}

_DECIDE_KEYWORDS = ("可以买", "能买", "该不该", "建议", "决策", "买入", "卖出", "值不值得", "投资")
_RESEARCH_KEYWORDS = ("研究", "调研", "分析", "怎么样", "了解", "看看", "梳理")


def extract_tickers(text: str) -> list[str]:
    """提取标的代码：A 股 6 位 + 美股大写缩写（去停用词）。顺序保持出现次序。"""
    found: list[str] = []
    for m in _CN_TICKER.finditer(text):
        found.append(m.group(0))
    for m in _US_TICKER.finditer(text):
        w = m.group(0)
        if w not in _US_STOPWORDS and w not in found:
            found.append(w)
    return found


def classify_intent(text: str) -> str:
    """decide > research > chat。"""
    if any(k in text for k in _DECIDE_KEYWORDS):
        return "decide"
    if any(k in text for k in _RESEARCH_KEYWORDS) or extract_tickers(text):
        return "research"
    return "chat"
