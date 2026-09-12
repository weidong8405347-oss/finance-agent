# Finance Agent 股票 / 行业研究档案最终优化方案

> 适用范围：股票档案、行业档案、Deep Research 结果页、长期维护型 Research Dossier  
> 目标产品形态：**AI-native Equity Research Terminal**  
> 参考方向：Bloomberg Intelligence / AlphaSense / Tegus / Koyfin / TIKR / Fiscal.ai 等专业投研产品的信息架构与阅读体验  
> 核心原则：**先重构信息层级和数据模型，再做视觉设计；先回答投资问题，再展示研究过程。**

---

# 0. 最终结论

当前 Finance Agent 的核心问题，并不是“研究内容不够多”，而是：

> **研究过程信息、投资判断信息、底层证据、候选标的、风险、进展、元数据全部处于同一视觉层级。**

结果就是：

- 内容很多，但用户无法快速形成投资判断；
- 卡片很多，但真正的重点反而不突出；
- 可视化很多，但图表之间没有明确的信息任务；
- Evidence 很强，但过早暴露导致页面像 Debug / Audit Console；
- Research Run 的过程状态被直接当作 Dossier 内容展示；
- 页面像“AI 研究流水线结果”，不像“专业股票研究终端”。

最终需要完成的转型是：

```text
Research Result Viewer
        ↓
AI-native Equity Research Terminal
```

整体升级优先级应为：

```text
1. Research Run 与 Durable Dossier 分离
2. 重构 Company / Industry Dossier Schema
3. 建立 Investment Objects
4. 重做 Overview / Snapshot
5. Evidence 全部后置为 Audit Layer
6. 建立固定 Visualization Grammar
7. 重构 Candidate / Peer Comparison
8. 增加 Expectations / Revision Layer
9. 建立 What Changed / Thesis Tracking
10. 最后统一视觉设计与 Design Tokens
```

---

# 1. 当前页面的根本问题

## 1.1 信息层级失效

当前页面同时展示：

- 研究目标；
- 当前 Research Run 进度；
- thesis；
- why now；
- key bottleneck；
- value capture；
- candidate pool；
- included / watchlist / excluded；
- 最大分歧；
- 本轮进展；
- 关键依据；
- 最大反证；
- Evidence ID；
- Observation ID；
- Research Sufficiency；
- 未核验项；
- 检索预算耗尽；
- 催化剂；
- 时间线；
- 财务数据；
- 标的列表。

这些信息本身并非无价值。

问题是：

> **它们不应该同时处于默认展开状态。**

专业投研产品的关键能力，不是展示最多的信息，而是：

> **以最小认知成本，将正确的信息在正确的时间呈现给用户。**

---

## 1.2 Research Run 与 Dossier 混在一起

目前类似：

```text
本轮核心交付是 value_chain 与 demand_supply 两字段补缺
部分证据仍无逐字原文
关键问题 3/7
检索预算耗尽
```

这些属于：

```text
Research Run
```

而不是：

```text
Industry Dossier
```

Dossier 应回答：

```text
这个行业目前处于什么阶段？
为什么值得关注？
利润池在哪里？
最重要的瓶颈是什么？
最好的上市公司表达是什么？
哪些事件会改变判断？
```

Research Run 则负责回答：

```text
这次研究新增了什么 Evidence？
哪些问题尚未解决？
哪些来源失败？
哪些 Claim 被升级？
```

两者必须彻底分离。

---

## 1.3 所有元素都被设计成“重点”

当前常见结构：

```text
Card
 ├── Badge
 ├── Colored background
 ├── Border
 ├── Sub-card
 ├── Badge
 └── Expand button
```

再叠加：

- 红；
- 黄；
- 蓝；
- 绿；
- 紫；
- 多种边框；
- 多种阴影；
- 多种 Badge。

最终效果是：

> **所有东西都被强调 = 没有任何东西真正被强调。**

---

## 1.4 长文本与三列布局冲突

三列非常适合：

```text
Revenue
EPS
FCF
```

不适合：

```text
本轮进展
关键依据
最大反证
```

长文本放入三列以后：

- 每列宽度太小；
- 中文每行字符太少；
- 阅读视线不断左右跳；
- 用户难以判断先看哪一列。

原则：

> **三列用于 KPI，不用于复杂观点。**

---

## 1.5 字号过小

大量使用：

```text
10px
11px
text-xs
```

并承载真实研究结论。

结果会天然呈现为：

> 后台系统 / Debug Console

而非：

> Professional Research Terminal

中文正文尤其应该避免 10–11px。

---

## 1.6 Evidence 系统暴露过度

当前页面直接出现：

```text
ev-vc-futures
obs-xxxx
claim-xxxx
calc-xxxx
```

这对数据库和开发者有意义。

对投资者没有意义。

专业产品应该做到：

> **用户能随时审计，但不需要一直看到审计信息。**

---

# 2. 重新定义产品信息架构

建立四层 Progressive Disclosure：

```text
L0  Snapshot / Decision View
    30 秒形成投资认知

L1  Investment Analysis
    理解核心 Thesis、利润池、竞争与风险

L2  Deep Dive
    财务、技术、竞争、估值、KPI、详细公司比较

L3  Audit / Evidence
    Source / Quote / Observation / Calculation / Validation
```

默认用户只看到：

```text
L0 + 部分 L1
```

需要时再进入：

```text
L2 / L3
```

---

# 3. Investor Mode 与 Audit Mode

这是整个产品最重要的 UX 分层之一。

## Investor Mode

默认模式。

目标：

> 帮助用户快速形成投资判断。

显示：

- Investment View；
- Why Now；
- Key Metrics；
- Value Chain；
- Best Public Expressions；
- Financial Quality；
- Expectations；
- Catalysts；
- Risks；
- Thesis Breakers；
- What Changed。

---

## Audit Mode

二级入口。

目标：

> 帮助用户核验系统是否可信。

显示：

- Evidence；
- Original Quote；
- Source URL；
- Filing；
- Observation；
- Calculation；
- Counter Evidence；
- Validation State；
- Research Sufficiency；
- Data Gap；
- Research Run History。

---

# 4. 行业档案最终导航

建议固定为：

```text
Overview
Market
Value Chain
Technology
Competition
Companies
Catalysts & Risks
Timeline
────────────────
Research
Sources
```

---

## Overview

回答：

```text
这个行业是什么？
为什么现在重要？
产业阶段是什么？
利润在哪？
最大瓶颈是什么？
最值得跟踪哪些公司？
什么会证伪？
```

---

## Market

回答：

```text
市场有多大？
增长有多快？
渗透率多少？
需求来自哪里？
供给是否过剩？
区域结构如何？
```

---

## Value Chain

回答：

```text
谁提供什么价值？
钱流向哪里？
利润池在哪？
谁拥有议价权？
```

---

## Technology

回答：

```text
主要技术路线是什么？
哪条成熟？
哪条可能胜出？
关键成本曲线是什么？
```

---

## Competition

回答：

```text
竞争格局如何？
市场结构如何？
谁拥有差异化壁垒？
```

---

## Companies

回答：

```text
最好的上市公司表达是什么？
谁是纯正标的？
谁只是主题相关？
谁被淘汰？
```

---

## Catalysts & Risks

回答：

```text
未来 6–36 个月哪些事件会推动股价 / 改变 Thesis？
什么是最大的 downside？
```

---

# 5. 股票档案最终导航

建议固定为：

```text
Overview
Business
Financials
KPIs
Estimates
Valuation
Peers
Thesis
Catalysts & Risks
Timeline
────────────────
Filings
Sources
Research
```

---

# 6. Overview 的核心原则

Overview 不是研究报告。

Overview 是：

> **Tear Sheet + Investment Committee Summary**

首屏必须在 10–15 秒内帮助用户回答：

1. What is it？
2. Why now？
3. What matters？
4. Where is the money？
5. Who wins？
6. What breaks the thesis？

除此之外的内容默认不应该进入首屏。

---

# 7. 行业 Overview 最终布局

以 AI for Science 为例：

```text
┌──────────────────────────────────────────────────────────────┐
│ AI FOR SCIENCE                            Updated Sep 2026    │
│ Early Commercialization · Research Quality ●●●○ · 24 names  │
└──────────────────────────────────────────────────────────────┘


INVESTMENT VIEW
───────────────────────────────────────────────────────────────

AI 正显著降低 Discovery / Preclinical 的时间与成本，
但真正决定产业价值的瓶颈正在向 Clinical Validation 迁移。

Bullish on:
数据闭环 + 模型 + 自动化实验 + 商业化能力的平台型公司

Cautious on:
仅具 AI 标签、缺乏临床或商业验证的主题型公司


WHY NOW                              KEY BOTTLENECK

Big Pharma adoption ↑               Discovery
Discovery cost ↓                         ↓
Capital inflow ↑                    Preclinical
AI penetration still low                ↓
                                    Clinical Validation ← Bottleneck


KEY METRICS

~$6M           18 months         100×             0
Discovery      Discovery         Pricing          AI drugs
cost           cycle             dispersion       approved


VALUE CHAIN / PROFIT POOL
───────────────────────────────────────────────────────────────

Compute → Models → Discovery Platform → Lab Automation
                               ↓
                    Clinical Validation
                               ↓
                      Commercialization


BEST PUBLIC EXPRESSIONS
───────────────────────────────────────────────────────────────

Company        Position        Validation        Risk        Status
Insilico       End-to-end      High              Medium      Core
XtalPi         Model+Robot     Med/High          Medium      Core
SDGR           Sci Software    High              Low/Med     Core
RXRX           Data flywheel   Medium            High        Watch

                                              View all 24 →


CATALYSTS                            THESIS BREAKERS

2026  First clinical milestones      Phase II efficacy weak
2027  Milestone conversion           Upfront-heavy revenue
2029  Rentosertib Phase III          No AI drug approval


WHAT CHANGED SINCE LAST UPDATE
───────────────────────────────────────────────────────────────

↑ Evidence strengthened
! Clinical Phase II conflict remains unresolved
+ New company added to watchlist
```

---

# 8. 股票 Overview 最终布局

建议：

```text
NVIDIA                                      NVDA
$xxx     +x.x%     Market Cap $x.xxT

AI compute platform leader


INVESTMENT VIEW
───────────────────────────────────────────────────────────────

① Data Center demand remains structurally strong
② Networking expands wallet share
③ Supply / margin remain key constraints


KEY METRICS
───────────────────────────────────────────────────────────────

Revenue       EPS       Gross Margin       FCF
$xxxB         $xx       xx%                $xxB


PRICE VS EXPECTATIONS
───────────────────────────────────────────────────────────────

Stock Price + FY+1 EPS Consensus


VALUATION
───────────────────────────────────────────────────────────────

Current P/E
5Y percentile
Peer median
Implied growth


BULL / BASE / BEAR
───────────────────────────────────────────────────────────────

Bull           Base           Bear
$xxx           $xxx           $xxx


CATALYSTS                           THESIS BREAKERS
```

---

# 9. 首页内容密度硬规则

建议直接写入产品 PRD。

## Overview 默认上限

```text
核心 Thesis          ≤ 3
Key Metrics          4–6
Top Companies        ≤ 5
Catalysts            ≤ 3
Risks                ≤ 3
Thesis Breakers      ≤ 3
Header Badges        ≤ 3
```

---

## 长度控制

```text
单段正文：
≤ 100–140 中文字

Thesis Summary：
≤ 2 行

Card 内默认列表：
≤ 5 条

长文本：
默认折叠或进入 Detail
```

---

## 布局控制

```text
正文区域默认最多 2 列

3–4 列：
只用于 KPI / Numeric Summary

禁止：
三列长文本
```

---

# 10. 删除 / 后置的内容

以下内容不要出现在 Investor Mode 首屏：

```text
Research Objective
Question Progress
retrieval_calls
Research Budget
gathering
stalled
raw Claim ID
Evidence ID
Observation ID
Calculation ID
namespace
run_id
schema_version
```

它们进入：

```text
Research / Audit
```

---

# 11. 将“本轮进展”改成 What Changed

这是非常重要的产品变化。

不要显示：

```text
本轮补齐 value_chain
新增 5 个 evidence
某字段 gathering → partial
```

改为：

```text
SINCE LAST UPDATE

↑ Thesis confidence increased
  New primary evidence supports discovery cost reduction.

↑ Commercial validation
  Company X announced a new milestone.

! Risk increased
  Phase II efficacy remains inconsistent across sources.

+ Universe changed
  Company Y moved from Watchlist → Core.
```

用户关心的是：

> **研究更新对投资判断造成了什么变化。**

不是：

> Agent 做了哪些内部操作。

---

# 12. Investment Objects：新的核心数据模型

不要让前端继续直接渲染大段 Markdown。

建议建立：

```text
InvestmentObject
├── fact
├── metric
├── thesis
├── moat
├── catalyst
├── risk
├── expectation
├── valuation
├── competitor
├── scenario
├── timeline_event
├── company
├── industry
└── evidence
```

前端只消费这些对象。

---

# 13. Thesis Object

示例：

```yaml
type: thesis

id: clinical-validation-bottleneck

title:
  Clinical validation is becoming
  the industry's primary bottleneck

summary:
  Discovery economics improved,
  but efficacy remains unproven.

importance: 0.95
confidence: 0.72

direction: neutral

support_count: 7
counter_count: 3
unresolved_count: 2

supports:
  - evidence_001
  - evidence_002

contradicts:
  - evidence_101

monitor:
  - first_ai_drug_approval
  - rentosertib_phase3
  - isomorphic_first_clinical

related_companies:
  - Insilico
  - XtalPi
  - Recursion

visualization:
  type: stage_funnel
```

---

# 14. 每个 Thesis 必须绑定 Bear Case

避免系统只寻找支持原判断的 Evidence。

每个 Thesis 必须回答：

```text
What supports it?
What contradicts it?
What remains unresolved?
What would prove it wrong?
```

UI：

```text
Clinical validation is the new bottleneck

Confidence      ●●●○○

Supports        7
Contradicts     3
Unresolved      2

Main counterargument
Phase II success may not exceed historical baseline.

View evidence →
```

---

# 15. Financial Fact 与 Thesis 必须分层

建议建立：

```text
Fact
Observation
Inference
Thesis
```

示例：

```text
Revenue = $10B
→ Fact

Revenue +40%
→ Observation

Growth is accelerating
→ Inference

Growth will remain >30%
→ Thesis
```

UI 要通过标签或 Tooltip 区分。

避免：

> 事实、分析、预测在视觉上完全一致。

---

# 16. Evidence 系统：保留，但彻底后置

你的 Evidence 系统是 Finance Agent 的优势，不应该删除。

正确做法是：

> Evidence Everywhere, Evidence Visible Only on Demand.

正文：

```text
Discovery + preclinical cycle has fallen to ~18 months. [1]
```

Hover：

```text
1 Primary source
Updated Jul 2026
Confidence: High
```

点击：

右侧 Evidence Drawer。

---

# 17. Evidence Drawer

示例：

```text
18 MONTHS

Claim
AI-assisted discovery + preclinical timeline

Value
18 months

Source
Futures Korea

Published
Jul 14, 2026

Source Grade
Secondary / Industry

Original Quote
"……"

Used by
• Thesis #1
• Value Chain
• Industry KPI

Contradicting Evidence
1

Observation ID
obs-xxx

Evidence ID
ev-xxx
```

注意：

```text
Observation ID / Evidence ID
```

仅 Drawer 底部显示。

---

# 18. Research Quality 设计

不要在正文大面积展示：

```text
refs_resolvable
facts_checked
analysis_reviewed
sufficiency
```

Header 只显示：

```text
Research Quality ●●●○
```

Hover：

```text
27 facts verified
13 claims source-linked
5 unresolved gaps
2 source conflicts
```

点击：

进入完整 Research Quality 页面。

---

# 19. Candidate Pool 重构

当前最需要重构的区域之一。

不要用自然语言描述：

```text
上游有 A/B/C
中游有 D/E/F
下游有 G/H
```

改成结构化矩阵。

---

## 推荐表格

| Company | Segment | Purity | Moat | Commercial Validation | Financial Quality | Valuation | Status |
|---|---|---:|---:|---:|---:|---:|---|
| Insilico | Drug Discovery | High | 5 | 4 | 3 | — | Core |
| XtalPi | Platform | High | 5 | 4 | 3 | 3 | Core |
| SDGR | Software | High | 4 | 5 | 4 | 3 | Core |
| RXRX | Discovery | High | 4 | 3 | 1 | 2 | Watch |

默认：

```text
只展示 Top 5–8
```

点击：

```text
View all companies
```

---

# 20. Candidate 四象限

核心公司推荐提供 Scatter / Matrix：

```text
Y = Commercial Validation
X = Technology / Moat
Bubble Size = Market Cap
Bubble Border / Color = Valuation / Risk
```

目的：

> 一眼识别“好公司”和“好股票”的区别。

---

# 21. Value Chain 重构

当前文字版产业链必须升级为交互图。

示例：

```text
COMPUTE
NVIDIA / Hygon / Inspur
      ↓
SCIENTIFIC MODELS
Deep Potential / Schrödinger
      ↓
DISCOVERY PLATFORM
Insilico / Recursion / XtalPi
      ↓
LAB AUTOMATION
      ↓
CLINICAL VALIDATION       ← CURRENT BOTTLENECK
      ↓
COMMERCIALIZATION
```

---

## 每个节点应包含

```text
Market Size
Growth
Margin
Bargaining Power
Moat
Bottleneck
Representative Companies
Investability
```

点击展开右侧详情。

---

# 22. Profit Pool 与 Value Chain 分开

不要只展示：

```text
产业链有哪些公司
```

还要回答：

> **谁真正赚到钱？**

建议 Profit Pool 单独可视化：

```text
Customer Spend
   ↓
Infrastructure
   ↓
Model / Data Platform
   ↓
Application
   ↓
Commercialization
```

使用：

```text
Sankey
Stacked Bar
```

前提：

必须有足够可靠的定量证据。

如果没有：

> 使用定性 High / Medium / Low，而不要伪造精确百分比。

---

# 23. Visualization Grammar

以后图表选择必须由“投资问题”决定。

| 投资问题 | 默认可视化 |
|---|---|
| 市场多大 | Historical + Forecast Line / Area |
| 增速 | Line |
| 份额 | Ranked Bar |
| Revenue Mix | Stacked Bar |
| Profit Pool | Sankey / Stacked Bar |
| 产业链 | Flow / Node Graph |
| Competition | 2×2 Matrix |
| Peer Comparison | Scatter |
| Financial History | Line / Bar |
| KPI | Small Multiples |
| Valuation | Historical Percentile |
| Estimate Revision | Revision Line |
| Catalyst | Timeline |
| Scenario | Bull / Base / Bear Table |
| Risk | Probability × Impact Matrix |
| Technology | Roadmap |
| Evidence | Drawer / Popover |

---

# 24. 不要滥用 Radar Chart

Radar Chart：

- 难做精确比较；
- 不适合高信息密度；
- 视觉面积容易误导；
- 机构投研使用频率并不高。

优先：

```text
Bar
Line
Scatter
Waterfall
Heatmap
Matrix
Timeline
Table
Sankey
```

---

# 25. Expectations Layer：必须补齐

这是 Finance Agent 和专业股票研究平台之间的重要差距。

股票研究不是：

```text
公司增长多少？
```

而是：

```text
市场已经预期增长多少？
```

因此必须新增：

```text
Expectation Layer
```

---

## 核心字段

```text
Revenue Consensus
EPS Consensus
EBITDA Consensus
FCF Consensus

1M Revision
3M Revision
6M Revision

Actual vs Consensus
Guidance vs Consensus
Guidance Revision
Analyst Count
Dispersion
```

---

# 26. 推荐的 Expectations 可视化

## Price vs EPS Revision

```text
Stock Price
+
FY+1 EPS Consensus
```

这是极高价值图表。

它帮助回答：

```text
股价上涨是：
盈利预期上修
还是
估值扩张？
```

---

## Revenue Revision

```text
3M Ago → Current
```

---

## Earnings Surprise

```text
Actual vs Consensus
```

---

# 27. KPI Engine

不同公司 / 行业必须拥有不同的 KPI。

不要统一：

```text
Revenue
PE
Market Share
```

---

## 示例：SaaS

```text
ARR
NRR
RPO
CAC Payback
FCF Margin
Rule of 40
```

---

## Semiconductor

```text
ASP
Utilization
Yield
Inventory Days
Capacity
Book-to-Bill
```

---

## AI Drug Discovery

```text
Clinical Assets
Pipeline Stage
PCC Count
BD Upfront
Milestone Potential
Cash Runway
R&D Burn
Phase II / III Success
```

Agent 根据：

```text
industry
business model
revenue model
```

动态选择 3–8 个 KPI。

---

# 28. Moat Framework

Moat 不应只是一段文字。

建立统一结构：

```text
Data
Technology
Scale
Network Effect
Switching Cost
Distribution
Brand
Regulation
Cost Advantage
Ecosystem
```

每一项：

```yaml
score: 1-5
confidence:
trend:
evidence:
```

---

# 29. Catalyst Engine

Catalyst Schema：

```yaml
title:
date:
probability:
impact:
current_expectation:
bull_case:
bear_case:
affected_thesis:
evidence:
```

前端：

```text
Catalyst Timeline
```

---

# 30. Thesis Breaker / Kill Criteria

每个 Thesis 必须有：

```text
What would prove me wrong?
```

例如：

```text
Thesis
AI improves drug discovery economics

Breaker
Phase II / III success does not outperform historical baseline.
```

这是买方研究与普通 AI Research 之间的重要差异。

---

# 31. Timeline

Timeline 不应该只是新闻列表。

需要只收录：

```text
Earnings
Product Launch
Acquisition
Capital Raise
CEO Change
Regulation
Clinical Data
Major Contract
Guidance Change
```

并支持：

```text
Timeline + Stock Price Overlay
```

---

# 32. What Changed Engine

Profile 必须从：

> 静态百科

升级为：

> 持续维护的投资档案。

每次 Research Run 后计算：

```text
Fact changed?
Expectation changed?
Thesis changed?
Risk changed?
Catalyst changed?
Company status changed?
```

然后输出：

```text
Since Last Update
```

---

# 33. Research Run 与 Dossier 的最终数据关系

推荐：

```text
Research Run
     ↓
New Evidence
     ↓
Fact / Observation
     ↓
Conflict Resolution
     ↓
Inference
     ↓
Investment Object Update
     ↓
Dossier Snapshot
```

Research Run：

> 更新 Dossier。

而不是：

> Research Run 本身就是 Dossier。

---

# 34. 推荐核心 Schema

```text
Dossier

├── Identity
├── Snapshot
├── InvestmentView
├── KeyMetrics
├── Thesis[]
├── Risks[]
├── Catalysts[]
├── Expectations
├── Valuation
├── Companies[]
├── Market
├── ValueChain
├── Competition
├── Technology
├── Timeline[]
├── ChangeLog[]
├── ResearchQuality
│
├── InvestmentObjects[]
│
├── ResearchRuns[]
│
└── EvidenceGraph
```

---

# 35. Financial Fact Schema

示例：

```yaml
metric:
  key: revenue
  label: Revenue

value: 12.8

unit: USD_B

period:
  type: fiscal_year
  value: FY2026

accounting:
  standard: GAAP

nature:
  reported

source:
  type: SEC
  document: 10-K

confidence: 1.0
```

---

# 36. Source Hierarchy

建议统一：

```text
Tier 1
Exchange / SEC / HKEX / Regulator / Official Filing

Tier 2
Company IR / Earnings Call

Tier 3
Government / Academic / Official Industry Body

Tier 4
Reuters / Bloomberg / FT

Tier 5
Sell-side / Specialist Research

Tier 6
Secondary Media

Tier 7
Blog / Community
```

Agent 默认：

```text
Primary Source > Secondary Source
```

---

# 37. 页面视觉系统

最终目标不是：

> 炫酷 AI Dashboard

而应该接近：

> **Koyfin × Linear × Institutional Research Terminal**

---

# 38. 色彩

建议：

```text
90% Neutral

Accent       Blue
Positive     Green
Warning      Amber
Risk         Red
```

原则：

> 颜色只表达语义，不表达“模块身份”。

避免：

```text
一个模块蓝
一个模块紫
一个模块绿
一个模块橙
```

---

# 39. 推荐 Design Tokens

```css
--bg-page: #F8FAFC;
--bg-surface: #FFFFFF;

--text-primary: #0F172A;
--text-secondary: #334155;
--text-muted: #64748B;

--border: #E2E8F0;

--accent: #1D4ED8;

--positive: #15803D;
--positive-bg: #F0FDF4;

--warning: #B45309;
--warning-bg: #FFFBEB;

--danger: #B91C1C;
--danger-bg: #FEF2F2;
```

不要依赖大量阴影。

---

# 40. Typography

建议：

| Level | Size |
|---|---:|
| Page Title | 28–32px |
| Major Section | 20–22px |
| Card / Subsection | 15–16px |
| Body | 14–15px |
| Table | 13px |
| Metadata | 12px |
| Label | 11–12px |
| 10px | 不用于核心信息 |

金融数字必须：

```css
font-variant-numeric: tabular-nums;
```

---

# 41. Layout

Desktop：

```text
App shell:
1440–1600px

Primary content:
900–1040px

Optional right rail:
280–320px
```

长文本阅读宽度：

```text
680–760px
```

---

# 42. Surface 规则

只保留三种 Surface：

```text
Primary Section
Secondary Panel
Drawer / Popover
```

避免：

```text
Card inside Card inside Card
```

---

# 43. Card 设计

建议：

```text
White background
1px subtle border
6–10px radius
Very light / no shadow
20–24px padding
```

专业感来自：

```text
Typography
Alignment
Whitespace
Grid
```

不是：

```text
Shadow
Gradient
Glow
```

---

# 44. 推荐前端技术栈

```text
Next.js
Tailwind CSS
shadcn/ui

TanStack Table
Apache ECharts
TradingView Lightweight Charts
React Flow
```

职责：

```text
shadcn
→ UI shell

TanStack Table
→ Financial / Peer Tables

ECharts
→ Fundamental visualization

TradingView
→ Price / Volume

React Flow
→ Value Chain / Ecosystem
```

---

# 45. 不建议 LLM 直接生成最终 HTML

避免：

```text
Prompt
→ LLM
→ Arbitrary HTML
```

问题：

- UI 漂移；
- 相同数据展示方式不一致；
- Mobile 难适配；
- 难统一视觉；
- 难测试；
- 难维护。

推荐：

```text
Research Agent
     ↓
Typed Investment Objects
     ↓
Visualization Objects
     ↓
Deterministic React Renderer
```

---

# 46. Visualization Object

示例：

```yaml
type: line_chart

metric:
  - revenue
  - gross_margin

period:
  start: FY2022
  end: FY2026

annotations:
  - event_001
```

Agent 决定：

```text
要表达什么
```

Renderer 决定：

```text
具体怎么画
```

---

# 47. AI for Science 页面具体改造

现有内容可以直接重新映射。

---

## 原来的：

```text
Research progress
Candidate Pool text
Drivers text
Counter Evidence text
Credibility text
Limitations
```

---

## 新页面：

### Header

```text
AI for Science

Early Commercialization
Research Quality ●●●○
Updated Sep 2026
```

---

### Investment View

```text
AI 已经明显改善 Discovery economics，
但价值验证的主要瓶颈已经迁移到 Clinical Validation。
```

---

### Key Metrics

```text
~$6M
18 months
100×
0 approvals
```

---

### Key Debate

```text
Phase I
80–90%

Phase II
~40% vs 68%

Approved
0
```

---

### Best Public Expressions

```text
Insilico
XtalPi
Schrödinger
Recursion
```

---

### Key Catalysts

```text
2026
Isomorphic first clinical entry

2027
Milestone conversion / key pipeline events

2029
Rentosertib Phase III
```

---

### Thesis Breakers

```text
Clinical efficacy fails to outperform historical baseline

Milestone revenue fails to replace upfront revenue

No AI-discovered drug obtains approval
```

---

# 48. 对附件方案的保留与调整

附件中的以下方向应保留：

```text
L0–L3 Progressive Disclosure
Executive Hero
Candidate Matrix
Catalyst Timeline
Evidence Drawer
Institutional Palette
tabular-nums
Candidate Tiering
```

---

## 但建议调整的地方

### 48.1 不建议把 Bear Case 大红框放在首屏非常靠前的位置

风险必须突出，但不能压过 Investment View。

建议：

```text
Investment View
→ Key Metrics
→ Key Debate / Thesis Breakers
```

而不是：

```text
KPI
→ 大红框
→ 其他内容
```

红色面积过大容易让页面产生警报系统感。

---

### 48.2 不建议左 340px / 右 1fr 的长期固定布局

产业链和 Timeline 都属于重要内容。

建议：

```text
Overview:
主内容 2/3 + Side Rail 1/3

Deep Dive:
根据模块独立布局
```

不要让产业链永久被压缩为窄侧栏。

---

### 48.3 不建议在 KPI 内展示 raw evidence ID

例如：

```text
ev-vc-futures
```

应显示：

```text
[1]
```

或：

```text
Source
```

真正 ID 放 Evidence Drawer。

---

### 48.4 “价值占比 18% / 45% / 37%”等数值必须有强来源质量要求

如果只是单一二手来源：

不要将其设计成视觉上高度确定的数字。

应该：

```text
Estimated / Directional
```

或不用精确百分比。

---

### 48.5 Candidate 表格不能混合不可比财务周期

例如：

```text
2026H1
vs
FY2025
```

即使标注了，也很容易误导横向比较。

推荐默认：

```text
TTM / FY+0
```

或者每列严格同周期。

---

# 49. 实施路线

---

## Phase 0：删减

优先级：P0

先做：

```text
隐藏 Research Runtime 字段
隐藏 raw evidence IDs
隐藏 retrieval 信息
隐藏 namespace / run_id
隐藏长 Limitations
```

收益立刻可见。

---

## Phase 1：数据模型拆分

优先级：P0

完成：

```text
ResearchRun
Dossier
InvestmentObject
Evidence
FinancialFact
```

解耦。

---

## Phase 2：Overview 重做

优先级：P0

完成：

```text
Investment View
Key Metrics
Key Debate
Top Companies
Catalysts
Thesis Breakers
What Changed
```

---

## Phase 3：Evidence Drawer

优先级：P0

把：

```text
ev-*
obs-*
claim-*
calc-*
```

全部退出正文。

---

## Phase 4：Candidate / Peer Matrix

优先级：P0

完成：

```text
Core
Watch
Needs Review
Excluded
```

并提供：

```text
Table + Scatter
```

---

## Phase 5：Visualization Grammar

优先级：P1

优先实现：

```text
Line
Bar
Scatter
Timeline
Value Chain
Risk Matrix
Scenario Matrix
Revision Chart
```

---

## Phase 6：Expectations

优先级：P1

实现：

```text
Consensus
Revision
Actual vs Estimate
Guidance vs Consensus
Price vs EPS
```

---

## Phase 7：What Changed

优先级：P1

建立：

```text
Snapshot Diff
Thesis Diff
KPI Diff
Risk Diff
Catalyst Diff
```

---

## Phase 8：Deep Dive Pages

优先级：P2

完成：

```text
Market
Technology
Competition
Valuation
Financials
```

---

# 50. 推荐验收指标

## 首屏测试

用户打开行业 / 股票档案后：

**15 秒内无需滚动，应能回答：**

```text
它是什么？
为什么重要？
核心投资逻辑是什么？
最好的投资表达是什么？
最大风险是什么？
```

---

## 信息密度

Overview：

```text
≤ 3 Thesis
≤ 6 KPI
≤ 5 Companies
≤ 3 Catalysts
≤ 3 Risks
≤ 3 Header Badges
```

---

## Typography

```text
正文 ≥ 14px
表格 ≥ 13px
核心信息不得使用 10px
```

---

## Evidence

```text
正文不出现 raw ID

100% 关键数字可点击追溯
```

---

## Research Metadata

Investor Mode：

```text
0 个 Research Runtime 字段
```

---

## Candidate Comparison

默认：

```text
同周期
同货币 / 明确原币
同会计口径或清晰标记
```

---

## Mobile

移动端：

```text
单列
KPI 横向滑动或 2×N
表格局部横向滚动
Evidence Drawer 全屏 Sheet
```

---

# 51. 最终产品目标

不要把 Finance Agent 定义为：

> 一个能生成股票研究报告的 AI。

建议定义为：

> **一个能够持续构建、更新、验证、比较和可视化 Investment Knowledge 的 AI-native Equity Research Terminal。**

它真正应该建立的竞争优势不是：

```text
一次生成更多文字
```

而是：

```text
持续研究
+
结构化投资知识
+
Expectation Tracking
+
Thesis Tracking
+
Evidence Auditability
+
专业可视化
```

---

# 52. 最终架构

```text
                       Finance Agent

                            │
                            ▼

                     Research Router

        ┌───────────────────┼───────────────────┐

        ▼                   ▼                   ▼

 Structured Data         Filings           Deep Research

        │                   │                   │
        └───────────────────┼───────────────────┘

                            ▼

                       Evidence Graph

                            ▼

                         Fact Store

                            ▼

                     Analysis Engine

                            ▼

                    Investment Objects

                            ▼

                    Dossier Projector

                            ▼

                  Visualization Objects

                            ▼

                Deterministic UI Renderer

        ┌───────────────────┼───────────────────┐

        ▼                   ▼                   ▼

 Company Profile      Industry Profile     Compare Mode

        │                   │                   │
        └───────────────────┼───────────────────┘

                            ▼

                       Audit / Sources


Research Runs
      │
      └─────> Update Evidence / Facts / Investment Objects
                         │
                         ▼
                    What Changed
```

---

# 53. 最关键的五条执行原则

如果整个项目只保留五条规范，应保留：

### 1. Research Run ≠ Dossier

研究过程不等于投资档案。

### 2. Overview 只服务“投资判断”

不是服务完整性。

### 3. Evidence 默认隐藏，但必须一键可追溯

Auditability 和 Readability 同时保留。

### 4. LLM 输出 Investment Objects，不输出任意 UI

UI 必须 deterministic。

### 5. 股票研究必须加入 Expectations Layer

没有：

```text
Actual vs Consensus
Revision
Valuation
```

就很难达到真正专业买方 / 卖方股票研究产品的水平。

---

# 54. 最终实施优先级表

| Priority | 项目 | 影响 |
|---|---|---|
| P0 | Research Run / Dossier 解耦 | ★★★★★ |
| P0 | Overview Snapshot 重做 | ★★★★★ |
| P0 | Evidence Drawer | ★★★★★ |
| P0 | 删除 raw ID / runtime 信息 | ★★★★★ |
| P0 | Candidate Matrix | ★★★★★ |
| P1 | Investment Object Schema | ★★★★★ |
| P1 | Expectations / Revisions | ★★★★★ |
| P1 | Visualization Grammar | ★★★★☆ |
| P1 | Value Chain | ★★★★☆ |
| P1 | Catalyst / Thesis Breaker | ★★★★☆ |
| P1 | What Changed | ★★★★☆ |
| P2 | Compare Mode | ★★★★☆ |
| P2 | Mobile Optimization | ★★★☆☆ |
| P2 | Research Quality Dashboard | ★★★☆☆ |

---

# 55. 一句话总结

当前 Finance Agent 的核心问题不是：

> **信息不足。**

而是：

> **信息没有按照投资决策的重要程度被组织。**

最终改造方向不是：

> 把当前 Dashboard 做得更漂亮。

而是：

> **从数据模型开始重新定义信息层级，让用户先看到 Investment View，再逐步进入分析、数据和 Evidence。**

完成这次重构以后，Finance Agent 才会真正从：

> **AI Deep Research 调试面板**

升级成：

> **专业、可持续维护、可审计的 AI Equity Research Terminal。**
