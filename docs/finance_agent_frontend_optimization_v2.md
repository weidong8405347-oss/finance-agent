# Finance Agent 最新前端优化方案

> 适用对象：股票档案 / 行业档案 / Deep Research Dossier  
> 当前评审基于：`dossier-ai-for-science-2026-09-12.html`  
> 优化目标：从“较好的 AI 行业研究档案”进一步升级为 **AI-native Buy-side / Sell-side Equity Research Terminal**

---

# 1. 当前版本总体评价

当前版本相较此前已经有明显进步，主要体现在：

- 已建立较统一的中性色视觉体系；
- 使用 `ink / accent / positive / warning / risk` 等语义色；
- 主正文层级明显改善；
- 已开始使用 Key Metrics、Why Now、关键瓶颈、候选公司、What Changed 等专业投研模块；
- Evidence 已开始从正文中后置；
- 产业链不再只是长文本，开始具备图形化关系表达；
- 页面已经从明显的“Agent Debug / Research Log”逐步转向专业 Research Dossier。

但当前仍大约处于：

> **专业投研终端的 60–65 分阶段**

接下来最大的提升空间，已经不再是圆角、阴影、卡片样式，而是：

```text
1. Investment View 是否真的在表达投资结论
2. 数据是否足够完整
3. 是否能做横向比较
4. 是否有 Expectations / Revisions
5. 是否具备估值能力
6. 图表是否围绕投资问题服务
7. Research Run 是否真正与 Dossier 解耦
```

---

# 2. 当前最重要的 P0 问题

---

## 2.1 Investment View 的内容仍然是 Research Run Summary

目前页面虽然已经有：

```text
Investment View
```

但其实际内容仍接近：

```text
本轮核心交付是 value_chain 与 demand_supply 两字段补缺……
```

这仍然是：

```text
Research Run Summary
```

而不是：

```text
Durable Investment View
```

---

## 正确做法

必须把：

```text
research_run.summary
```

与：

```text
dossier.investment_view
```

彻底拆开。

---

## 行业 Investment View 应该回答

```text
这个行业当前最重要的投资判断是什么？
```

例如 AI for Science：

```text
AI 已显著降低药物发现与临床前阶段的时间和成本，
但产业价值验证的主要瓶颈正在向 Phase II/III 临床有效性迁移。

当前最值得关注的是：
拥有专有数据、模型、自动化实验闭环和 BD 商业化能力的平台型公司。

应谨慎看待：
仅具 AI 标签、但缺乏临床验证和持续商业化能力的主题型公司。
```

---

## 推荐约束

```text
Investment View：
80–140 中文字

最多：
3 条核心 Thesis
```

---

# 3. 首页数据层仍然不完整

当前 Schema 中最关键的行业数据仍存在缺失，例如：

```text
market_size
growth_rate
capacity_supply
```

因此虽然页面已经有漂亮的 KPI Strip，但首页容易展示成：

```text
$6M
18 months
100x
```

这些属于有价值的“行业效率指标”，但并不能回答：

```text
这个行业有多大？
增长有多快？
当前渗透率多低？
未来空间有多大？
```

---

# 4. 行业首页 KPI 应重新分层

建议首页 6 个指标按照以下优先级选择。

---

## 一级指标：行业规模与空间

优先展示：

```text
Market Size
CAGR
AI Penetration
Addressable R&D Spend
```

---

## 二级指标：产业效率

展示：

```text
Discovery Cost
Discovery Timeline
Pricing Dispersion
Funding
```

---

## 三级指标：商业与临床兑现

展示：

```text
Clinical Assets
Approval Count
Milestone Revenue
Phase II / III Success
```

---

## 推荐 AI4S 首页 KPI

```text
Market Size
AI Penetration
R&D Addressable Spend
Discovery Cost
Clinical Assets
Approved AI Drugs
```

而：

```text
18 months
100x pricing spread
```

可以进入第二层。

---

# 5. 下一阶段不要“多加图”，而要增加高价值投资图

图表数量不是目标。

目标是：

> **每张图必须回答一个明确的投资问题。**

---

# 6. 推荐优先新增的 8 类图表

| 图表 | 推荐形式 | 回答的问题 | 优先级 |
|---|---|---|---|
| 市场规模 + 渗透率 | Area + Line | 行业多大？当前走到哪？ | P0 |
| 临床阶段漏斗 | Funnel / Stage | 最大瓶颈在哪里？ | P0 |
| Moat × Commercial Validation | Bubble Scatter | 谁是真正的好公司？ | P0 |
| Valuation × Growth | Bubble Scatter | 好公司是不是好股票？ | P0 |
| 行业融资趋势 | Stacked Bar / Area | 资本在加速还是退潮？ | P1 |
| Revenue Quality Mix | 100% Stacked Bar | 收入质量如何？ | P1 |
| Catalyst Timeline | Timeline | 下一次重估什么时候？ | P1 |
| Technology Roadmap | Roadmap / Matrix | 哪条技术路线最成熟？ | P1 |

---

# 7. P0 可视化 1：市场规模 + 渗透率

当前 AI4S 最大的投资逻辑之一是：

```text
Pharma R&D TAM 很大
但 AI Drug Discovery penetration 极低
```

非常适合使用两层视觉。

---

## 图一：TAM vs Current AI Spend

示意：

```text
Global Pharma R&D Spend
$194B
████████████████████████████████████████

AI Drug Discovery Market
$2.9B
█

Penetration ≈ 1.5%
```

---

## 图二：Historical + Forecast

```text
2022
2023
2024
2025
2026
2027E
2030E
```

展示：

```text
AI Drug Discovery Market Size
+
Penetration Rate
```

---

## 价值

用户一眼看到：

> **大市场 + 低渗透 + 高成长**

比一段 Why Now 更有冲击力。

---

# 8. P0 可视化 2：Clinical Validation Funnel

这是 AI4S 行业最应该拥有的一张核心图。

当前已有的数据包括：

```text
173–200+ clinical programs
Phase I
Phase II
Phase III
Approval = 0
```

建议设计：

```text
DISCOVERY
████████████████████

PRECLINICAL
██████████████

PHASE I
█████████
94 assets
80–90%

PHASE II
█████
56 assets
~40% vs 68% ⚠

PHASE III
██
15 assets

APPROVED
0
↑
THESIS BREAKER
```

---

## 特殊交互

Phase II：

```text
CONTESTED
```

点击：

显示：

```text
Source A → 68%
Source B → ~40%
```

而不是强行给一个统一值。

---

# 9. P0 可视化 3：Moat × Commercial Validation

当前 Candidate Pool 已经结构化，但仍以表格为主。

建议增加：

## Investment Landscape

```text
Y = Commercial Validation
X = Technology / Moat
Bubble Size = Market Cap / Funding
Bubble Fill = Candidate Tier
Bubble Border = Valuation Attractiveness
```

示意：

```text
                  HIGH VALIDATION

              SDGR          Insilico

                          XtalPi

────────────────────────────────────→ MOAT

                      RXRX

                  LOW VALIDATION
```

---

# 10. Candidate Score 建议

为了支持 Scatter，需要补充：

```text
moat_score
commercial_validation_score
financial_quality_score
valuation_score
evidence_confidence
```

建议范围：

```text
0–100
```

但必须是：

```text
规则驱动 + Evidence 可追溯
```

不能让 LLM 自由打分。

---

# 11. Score 必须拆解

例如：

```yaml
moat_score: 82

components:
  proprietary_data: 90
  technology: 85
  switching_cost: 60
  scale: 80

confidence: 0.76

evidence:
  - ...
```

用户点击 82：

可以看到计算逻辑。

---

# 12. P0 可视化 4：Valuation × Growth

这是当前产品从“产业研究”进入“股票投资”的关键缺口。

建议加入：

```text
X = NTM EV/Sales
Y = Forward Revenue Growth
Bubble Size = Market Cap
Color = Candidate Tier
```

用户可以一眼判断：

```text
High growth + reasonable valuation
vs
High growth + extreme valuation
```

---

# 13. Candidate Pool 需要从“表格”升级为“表格 + 地图”

推荐同时保留：

## View 1：Table

适合精确对比。

## View 2：Landscape

适合快速判断。

顶部：

```text
[Table] [Landscape]
```

---

# 14. 当前 Peer Comparison 数据仍然过弱

目前核心字段主要集中在：

```text
Revenue
YoY
Note
```

这距离专业 Comps 还有较大差距。

---

# 15. 建议 Peer Comparison 至少拆成 4 个 Preset

## Financial

```text
Revenue
Revenue Growth
Gross Margin
EBITDA
FCF
Cash
Net Cash
Cash Runway
```

---

## Operating

```text
AI Revenue %
Recurring Revenue %
Customer Count
Pipeline Assets
Clinical Stage
BD Contract Value
```

---

## Valuation

```text
Market Cap
Enterprise Value
P/S
EV/Sales
EV/GP
Forward EV/Sales
```

---

## Investment

```text
Moat
Commercial Validation
Financial Quality
Valuation
Catalyst
Risk
```

---

# 16. 横向比较必须统一口径

目前可能出现：

```text
2026H1
vs
FY2025
```

以及：

```text
CNY
vs
USD
```

这种对比即使有说明，也很难专业。

推荐：

---

## 默认口径

```text
TTM
or
FY+0
```

---

## Currency

默认：

```text
Original Currency
```

可切换：

```text
USD normalized
```

---

## 会计准则

显示：

```text
GAAP
IFRS
Non-GAAP
```

不要隐式混合。

---

# 17. 当前最大的内容缺口：估值层

现在页面已经开始回答：

```text
谁技术强？
谁商业化强？
```

但还没充分回答：

```text
谁值得买？
```

这是行业 Profile 和专业股票研究之间最关键的差异。

---

# 18. 行业页面也要有 Public Equity Dashboard

当前：

```text
valuation_lab
expectations
financial_quality
peers
```

更多被放到股票页面。

建议行业页面也必须具备：

## Public Equity Dashboard

```text
Company
Market Cap
EV
Revenue
Growth
Gross Margin
Cash Runway
EV/Sales
Forward EV/Sales
```

---

# 19. 行业 Profile 推荐增加两张估值图

## Growth vs Valuation

```text
X = Forward EV/Sales
Y = Forward Growth
```

---

## Validation vs Valuation

```text
X = Valuation
Y = Commercial Validation
```

回答：

> **哪些公司是“证据弱、估值贵”？**

---

# 20. 股票 Profile：Expectations 必须成为一等公民

股票投资真正重要的不是：

```text
公司未来增长多少？
```

而是：

```text
市场现在已经预期多少？
```

因此每个股票档案必须有：

```text
Actual
Consensus
Previous Consensus
Revision
Surprise
```

---

# 21. 股票页推荐三张核心图

## 21.1 Price vs FY+1 EPS Consensus

```text
Stock Price
+
FY+1 EPS
```

回答：

```text
股价上涨来自：
盈利上修
还是估值扩张？
```

---

## 21.2 Estimate Revision

```text
3M Ago
2M Ago
1M Ago
Current
```

支持：

```text
Revenue
EPS
EBITDA
FCF
```

---

## 21.3 Earnings Surprise

```text
Q1
Q2
Q3
Q4
```

显示：

```text
Actual vs Consensus
```

---

# 22. AI4S 推荐增加 Revenue Quality Mix

这是非常重要但当前不足的一块。

AI Drug Discovery 商业模式存在：

```text
Recurring SaaS
Upfront
Milestone
Royalty
Service Revenue
```

质量差异巨大。

---

## 推荐图

100% Stacked Bar：

```text
             SaaS     Milestone    Upfront
SDGR         ████████ ██
Insilico     ██       ████         ███████
XtalPi       ███      ███          ██████
METiS        █                     █████████
```

---

## 价值

让用户一眼区分：

```text
高质量增长
vs
一次性确认增长
```

这比单看 YoY 增速更重要。

---

# 23. 行业融资趋势需要独立图表

当前已有：

```text
2024 funding
2025 funding
2026H1 funding
Isomorphic concentration
```

建议：

## Capital Flow

```text
2024       $1.54B
2025       $1.84B
2026 H1    $2.42B
```

再叠加：

```text
Top 1 concentration
Top 5 concentration
```

---

# 24. Big Pharma Deal Map

当前已有：

```text
Big Pharma
AI Platform
Upfront
Milestone
```

可以做成：

```text
Partner Network
```

推荐：

```text
Lilly      → Isomorphic
Novartis   → Isomorphic
Sanofi     → AI Platform
...
```

Node Size：

```text
Total Deal Value
```

Edge Width：

```text
Upfront / Committed Value
```

不要为了炫酷使用复杂 Chord。

优先：

```text
Sankey / Network
```

---

# 25. 产业链图需要进一步“金融化”

当前产业链图已经具备：

```text
Nodes
Edges
Layers
```

方向正确。

下一步节点不要只显示：

```text
公司数
来源数
```

---

## 每个节点增加

```text
Stage
Market Attractiveness
Margin Profile
Moat
Bargaining Power
Investability
Public Companies
```

---

## 示例节点

```text
DISCOVERY PLATFORM

Stage
Scaling

Value Pool
High

Moat
High

Commercial Validation
Medium

Public Names
6

Core Names
3
```

这样才能从：

> Industry Map

升级成：

> Investment Value Chain。

---

# 26. Value Chain 与 Profit Pool 必须分开

Value Chain 回答：

```text
谁做什么？
```

Profit Pool 回答：

```text
谁赚到钱？
```

---

## Profit Pool 推荐

如果有可信数据：

```text
Sankey
```

如果没有：

使用：

```text
High
Medium
Low
```

而不是伪造：

```text
18%
45%
37%
```

---

# 27. Visualization Grammar 最终建议

| 投资问题 | 默认图表 |
|---|---|
| 市场多大 | Area / Line |
| CAGR | Line |
| Penetration | Progress / Area |
| Market Share | Ranked Bar |
| Revenue Mix | Stacked Bar |
| Revenue Quality | 100% Stacked Bar |
| Profit Pool | Sankey |
| Value Chain | Flow Graph |
| Competitive Position | 2×2 Matrix |
| Peer Comparison | Scatter |
| Valuation | Scatter / Percentile |
| Consensus Revision | Line |
| Clinical Pipeline | Funnel |
| Funding | Area / Bar |
| Catalysts | Timeline |
| Scenario | Bull/Base/Bear Table |
| Risk | Impact × Probability |
| Technology | Roadmap |
| Evidence | Drawer |

---

# 28. 不建议增加的图

减少：

```text
Radar Chart
Donut Chart
Gauge
Speedometer
3D Chart
```

原因：

```text
信息密度低
难比较
视觉噪音高
金融含义弱
```

---

# 29. 配色：当前方向正确，不建议大改

目前推荐继续坚持：

```text
Slate / Ink
Blue Accent
Green Positive
Amber Uncertainty
Red Risk
```

但下一步建议：

> **进一步减少颜色。**

---

# 30. 颜色语义重新定义

## Blue

只用于：

```text
Active
Link
Information
Selection
```

---

## Green

只用于：

```text
Positive Trend
Positive Surprise
Validated Improvement
```

---

## Amber

只用于：

```text
Uncertainty
Watch
Conflict
Stale Data
```

---

## Red

只用于：

```text
Thesis Breaker
Critical Risk
Negative Surprise
```

---

# 31. Core / Watch 不建议用红绿

当前：

```text
Core = Green
```

容易让用户理解为：

```text
Core = Buy
```

实际上：

```text
Core ≠ Bullish
```

推荐：

```text
Core
→ Ink / Blue

Watch
→ Neutral / Amber

Needs Review
→ Gray / Amber

Excluded
→ Gray

Critical Risk
→ Red
```

---

# 32. 页面需要一个 Dominant Visual

当前页面虽然更干净，但多个模块视觉权重仍比较平均。

专业页面第一屏通常应该有：

```text
1 Hero Insight
+
4 Supporting KPI
+
2 Secondary Modules
```

而不是：

```text
6 个同等权重卡片
```

---

# 33. 行业页推荐 Hero Visual

AI4S：

```text
Market Penetration
+
Clinical Funnel
```

二选一作为视觉焦点。

---

# 34. 股票页推荐 Hero Visual

```text
Price
+
Fundamental Expectations
```

例如：

```text
Stock Price
vs
FY+1 EPS Consensus
```

---

# 35. Header 继续简化

建议压缩成：

```text
AI for Science
Industry · Early Commercialization

Updated Sep 10
Research Quality ●●●○

[Research] [Compare] [Export]
```

---

## 不出现在 Header

```text
Snapshot ID
Namespace
Recipe
Schema Version
Run ID
Knowledge Time
```

全部放：

```text
Audit
```

---

# 36. 内容层仍需补齐的 6 大模块

当前内容优势：

```text
Technology
Value Chain
Evidence
Moat
Counter Evidence
```

但金融研究层仍偏弱。

---

## 必须补齐

| 模块 | Industry | Stock |
|---|---|---|
| Market History + Forecast | 必须 | 间接 |
| Public Comps Valuation | 必须 | 必须 |
| Consensus / Revisions | 聚合 | 必须 |
| Capital / Deal Flows | 必须 | 重要 |
| Unit Economics / Revenue Quality | 重要 | 必须 |
| Management / Capital Allocation | 次要 | 必须 |

---

# 37. 股票档案还应增加 Management 模块

建议：

```text
CEO Tenure
Capital Allocation
R&D Efficiency
M&A
Buyback
Dilution
Insider Ownership
Insider Transactions
Management Guidance Accuracy
```

---

# 38. 行业 Research 还应增加 Regulation / Policy Map

对于政策高度相关的行业：

```text
AI4S
Semiconductors
Nuclear
Biotech
Energy
```

建议有：

```text
Policy Timeline
Region
Policy Direction
Financial Impact
Affected Companies
```

---

# 39. Research Run 与 Dossier 仍需彻底解耦

最终数据流应该是：

```text
Research Run
     ↓
Evidence
     ↓
Fact / Observation
     ↓
Inference
     ↓
Investment Object
     ↓
Dossier Update
     ↓
What Changed
```

---

# 40. Dossier 不应保存“本轮做了什么”

Dossier 保存：

```text
Current Investment View
Current Thesis
Current Risks
Current Catalysts
Current KPI
Current Company Ranking
Current Expectations
```

---

# 41. Research Run 保存

```text
What was searched
What changed
What failed
What remains unresolved
What evidence was added
```

---

# 42. Schema 建议继续升级

建议：

```text
Dossier

├── Identity
├── InvestmentView
├── KeyMetrics
├── Market
├── Thesis[]
├── Risks[]
├── Catalysts[]
├── Expectations
├── Valuation
├── Companies[]
├── ValueChain
├── Competition
├── Technology
├── Timeline
├── WhatChanged[]
├── ResearchQuality
│
├── InvestmentObjects[]
├── ResearchRuns[]
└── EvidenceGraph
```

---

# 43. Investment Object 必须成为 UI 主要输入

推荐：

```text
fact
metric
thesis
risk
catalyst
expectation
valuation
company
scenario
timeline_event
```

而不是：

```text
大段 Markdown
```

---

# 44. 推荐新增 Visualization Object

例如：

```yaml
type: bubble_scatter

x:
  metric: valuation_score

y:
  metric: commercial_validation_score

bubble:
  metric: market_cap

color:
  field: candidate_tier
```

Renderer 决定：

```text
ECharts
React Flow
TradingView
```

---

# 45. UI Renderer 必须 deterministic

继续坚持：

```text
LLM
→ Typed Object
→ Renderer
```

不要：

```text
LLM
→ arbitrary HTML
```

---

# 46. P0 开发清单

建议下一轮只做 6 件事。

---

## P0-1

### Investment View 数据源重构

目标：

```text
Research Run Summary
≠
Investment View
```

---

## P0-2

### 补齐行业 typed metrics

至少：

```text
Market Size
Growth
Penetration
R&D TAM
Funding
Clinical Assets
```

---

## P0-3

### Clinical Funnel

AI4S 首页核心图。

---

## P0-4

### Moat × Commercial Validation

Candidate Landscape。

---

## P0-5

### Public Comps + Valuation

行业页增加：

```text
Growth
Margins
Cash
Valuation
```

---

## P0-6

### 股票 Expectations / Revisions

实现：

```text
Actual
Consensus
Revision
Surprise
```

---

# 47. P1 开发清单

```text
Revenue Quality Mix
Funding Trend
Deal Map
Technology Roadmap
Catalyst Timeline
Valuation × Growth
Price vs EPS
Risk Matrix
```

---

# 48. P2 开发清单

```text
Compare Mode
Saved Views
Custom KPI Set
Watchlist
Mobile Optimized View
Export to PDF / Markdown
Alert / Catalyst Tracking
Portfolio Mapping
```

---

# 49. 推荐页面最终结构：行业

```text
HEADER

INVESTMENT VIEW
──────────────────────────────

HERO VISUAL
──────────────────────────────

KEY METRICS
──────────────────────────────

MARKET
──────────────────────────────

VALUE CHAIN
──────────────────────────────

CLINICAL / COMMERCIAL VALIDATION
──────────────────────────────

COMPETITIVE LANDSCAPE
──────────────────────────────

PUBLIC EQUITY COMPS
──────────────────────────────

VALUATION
──────────────────────────────

CATALYSTS / RISKS
──────────────────────────────

WHAT CHANGED
──────────────────────────────

AUDIT / SOURCES
```

---

# 50. 推荐页面最终结构：股票

```text
HEADER

INVESTMENT VIEW
──────────────────────────────

PRICE + EXPECTATIONS
──────────────────────────────

KEY KPI
──────────────────────────────

BUSINESS
──────────────────────────────

FINANCIALS
──────────────────────────────

ESTIMATES / REVISIONS
──────────────────────────────

VALUATION
──────────────────────────────

PEERS
──────────────────────────────

MOAT
──────────────────────────────

CATALYSTS / RISKS
──────────────────────────────

WHAT CHANGED
──────────────────────────────

FILINGS / SOURCES
```

---

# 51. 验收标准

---

## 首屏

15 秒内无需滚动可以回答：

```text
是什么？
为什么重要？
核心 Thesis 是什么？
最值得关注谁？
最大的风险是什么？
```

---

## Industry Profile

必须拥有：

```text
Market Size
Growth
Penetration
Value Chain
Top Companies
Valuation
Catalysts
Risks
```

---

## Stock Profile

必须拥有：

```text
Price
Financials
KPI
Consensus
Revisions
Valuation
Peers
Catalysts
Risks
```

---

## Visual

```text
每张图必须回答一个投资问题
```

禁止：

```text
为了好看而画图
```

---

## Evidence

```text
100% 关键结论可追溯

正文不显示 raw Evidence ID
```

---

## Comparison

```text
同周期
同单位
同会计口径
```

---

# 52. 最终优先级

| Priority | 优化项 | 价值 |
|---|---|---|
| P0 | Investment View 数据源拆分 | ★★★★★ |
| P0 | 补 Market / Growth / Penetration | ★★★★★ |
| P0 | Clinical Funnel | ★★★★★ |
| P0 | Candidate Scatter | ★★★★★ |
| P0 | Public Comps / Valuation | ★★★★★ |
| P0 | Expectations / Revisions | ★★★★★ |
| P1 | Revenue Quality | ★★★★☆ |
| P1 | Funding Trend | ★★★★☆ |
| P1 | Deal Map | ★★★★☆ |
| P1 | Catalyst Timeline | ★★★★☆ |
| P1 | Risk Matrix | ★★★★☆ |
| P2 | Compare Mode | ★★★★☆ |
| P2 | Watchlist / Alerts | ★★★☆☆ |
| P2 | Portfolio Mapping | ★★★☆☆ |

---

# 53. 最终判断

当前产品已经基本解决了：

```text
“页面过乱、过碎、过像调试系统”
```

的问题。

下一阶段的核心任务已经变成：

```text
如何让页面真正帮助用户完成投资判断。
```

最终应该建立完整链条：

```text
Industry Knowledge
      ↓
Financial Reality
      ↓
Market Expectation
      ↓
Valuation
      ↓
Catalyst
      ↓
Thesis Change
      ↓
Investment Decision
```

而不是继续扩展成：

```text
更多 Research Text
+
更多 Card
```

---

# 54. 一句话总结

下一阶段不要继续把重点放在：

```text
border
padding
rounded
shadow
```

而应该集中资源建设：

> **Market Data + Expectations + Valuation + Investment Visualization + Thesis Tracking。**

完成这一轮后，Finance Agent 才会从：

> **优秀的 AI 行业研究档案**

进一步升级为：

> **真正能支持买方投资决策的 AI-native Equity Research Terminal。**
