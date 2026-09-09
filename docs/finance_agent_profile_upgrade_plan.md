# Finance Agent 股票 / 行业档案升级方案

> 目标：将当前偏“研究日志 / Evidence Viewer”的股票与行业档案，升级为更接近 **Koyfin × Fiscal.ai × AlphaSense × Sell-side Equity Research** 的专业投研产品。
>
> 核心原则：不要只优化 UI，而要同时升级 **研究模板、知识 Schema、Investment Objects、结构化金融数据、可视化语法和页面信息架构**。

---

# 第一部分：相关资源链接与推荐理由

## 1. FinSight

- GitHub：<https://github.com/RUC-NLPIR/FinSight>
- ACL 2026 论文：<https://aclanthology.org/2026.acl-long.265/>

### 推荐级别

**S 级，优先研究。**

### 为什么推荐

FinSight 与当前 Finance Agent 面临的问题高度相似：普通 Deep Research 系统通常能够搜集大量信息，但最终产物容易出现：

- 金融数据处理不充分；
- 分析链条不够深入；
- 报告结构不够专业；
- 图表和数据叙事能力较弱；
- 最终结果更像“搜索结果总结”，而不是专业 Equity Research Report。

FinSight 的关键价值不在于某个 UI，而在于完整研究流水线：

```text
Data Collection
    ↓
Chain of Analysis
    ↓
Financial / Industry Analysis
    ↓
Chart Generation
    ↓
Visual Critique / Refinement
    ↓
Report Synthesis
    ↓
Professional Rendering
```

### 建议重点拆解

1. Company Report Outline
2. Industry Report Outline
3. Chain-of-Analysis 结构
4. Data Collector / Deep Search Agent
5. Data Analyzer
6. Report Generator
7. 图表生成机制
8. 图表质量 Critique
9. Citation / Evidence tracing
10. Research → Report 的分层架构

### 对当前项目最有价值的启发

不要采用：

```text
Web Search → Markdown → 页面
```

建议升级为：

```text
Search
→ Evidence
→ Normalized Facts
→ Analysis Objects
→ Investment Objects
→ Visualization Objects
→ React Components
```

---

## 2. Anthropic Financial Services

- GitHub：<https://github.com/anthropics/financial-services>

### 推荐级别

**S 级，建议直接吸收其 Skills / SOP 思路。**

### 为什么推荐

它最有价值的地方不是代码，而是已经把专业金融研究任务拆成了比较成熟的任务体系。

主要包括：

- Equity Research
- Market Research
- Earnings Review
- Financial Analysis
- Model Building

其中 Equity Research 类任务可进一步拆成：

- earnings-analysis
- earnings-preview
- initiating-coverage
- model-update
- sector-overview
- thesis-tracker
- catalyst-calendar
- idea-generation

### 建议重点借鉴

#### Industry / Sector Research

不要只回答：

> 这个行业是什么？

而应系统回答：

1. 市场规模多大？
2. 增速是多少？
3. 行业结构是什么？
4. Value Chain 怎么分？
5. Profit Pool 在哪里？
6. 核心增长驱动是什么？
7. Why Now？
8. 产业瓶颈在哪里？
9. 哪些公司真正定义该行业？
10. 哪些公司只是“概念相关”？
11. 行业最重要的 3–5 个 KPI 是什么？
12. 最好的上市公司表达方式是什么？

### 特别值得引入的一条规则

**每个行业必须定义自己的 Industry-defining Metrics。**

例如：

```text
SaaS
→ ARR / NRR / CAC Payback / Rule of 40

Marketplace
→ GMV / Take Rate / Buyer-Seller Growth

Semiconductor
→ ASP / Utilization / Yield / Inventory / Capacity

Industrials
→ Backlog / Book-to-Bill / Utilization

Biotech
→ Pipeline Stage / Probability of Success / Cash Runway
```

不要让所有行业统一只看：

```text
Revenue / PE / Market Share
```

---

## 3. OpenBB

- GitHub：<https://github.com/OpenBB-finance/OpenBB>
- 官网：<https://openbb.co/>

### 推荐级别

**A 级，建议作为结构化金融数据层。**

### 为什么推荐

当前 Deep Research 如果大量依赖：

```text
Web Search → 网页 → LLM Extract Number
```

容易出现：

- Fiscal Period 不一致；
- 单位不一致；
- Currency 不一致；
- GAAP / Adjusted 混淆；
- TTM / FY / Quarterly 混淆；
- 同一指标多来源口径不同；
- LLM 数字抽取错误。

更合理的体系是：

```text
Structured Financial Data First
Research Second
```

OpenBB 可以承担：

- Price
- Financial Statements
- Valuation
- Estimates
- Macro
- Market Data
- 部分基本面数据

### 建议定位

OpenBB 不负责回答投资问题。

它应该成为 Finance Agent 的：

> **Financial Facts Infrastructure**

---

## 4. EdgarTools

- GitHub：<https://github.com/dgunning/edgartools>
- 文档：<https://edgartools.readthedocs.io/>

### 推荐级别

**A 级，美国股票研究强烈推荐。**

### 为什么推荐

对于美国上市公司：

```text
10-K
10-Q
8-K
SEC Filing
XBRL
```

不应该主要依赖 LLM 自己解析 HTML。

EdgarTools 可以把 Filing 转换成相对确定性的结构化对象。

### 特别适合当前项目

你已经有：

```text
Evidence
Source
Support Ref
Validation
Counter Evidence
```

EdgarTools 可以进一步让 Evidence 保存：

```yaml
metric:
  name: revenue
  value: 12.8
  unit: billion_usd
  period: FY2026
  fiscal_end: 2026-12-31

source:
  type: sec_filing
  form: 10-K
  filing_date: 2027-02-15
  section: financial_statements
  locator: ...
```

这比：

> “来源：网页文章”

可靠得多。

---

## 5. TradingView Lightweight Charts

- GitHub：<https://github.com/tradingview/lightweight-charts>
- 文档：<https://tradingview.github.io/lightweight-charts/>

### 推荐级别

**A 级，股价图建议直接使用。**

### 为什么推荐

适合构建：

- Stock Price
- Candlestick
- Volume
- Event Marker
- Earnings Marker
- Buyback
- Insider Transaction
- Catalyst Timeline 与价格联动

相比自己用通用 chart library 画 K 线，它更专业。

### Agent / Coding Skill

可以研究官方仓库提供的 Agent Skill：

```bash
npx skills add https://github.com/tradingview/lightweight-charts
```

---

## 6. Apache ECharts

- GitHub：<https://github.com/apache/echarts>
- 官网：<https://echarts.apache.org/>

### 推荐级别

**A 级，建议作为主 Research Visualization Engine。**

### 为什么推荐

非常适合金融研究中复杂图表：

- Line
- Bar
- Scatter
- Heatmap
- Treemap
- Sankey
- Graph
- Waterfall
- Multi-axis
- Rich Tooltip

建议：

```text
Stock Price
→ TradingView Lightweight Charts

Research / Fundamental Charts
→ ECharts
```

---

## 7. shadcn/ui

- 官网：<https://ui.shadcn.com/>
- GitHub：<https://github.com/shadcn-ui/ui>

### 推荐级别

**A 级。**

### 为什么推荐

不要继续大量自研：

- Card
- Tabs
- Badge
- Dropdown
- Dialog
- Sheet
- Tooltip
- Table Shell
- Skeleton
- Button

shadcn/ui 非常适合做专业金融工具，因为：

- 控制权高；
- 不像传统 UI Framework 那么重；
- Tailwind 体系容易统一 Design Token；
- 适合高信息密度页面。

---

## 8. TanStack Table

- 官网：<https://tanstack.com/table/latest>
- GitHub：<https://github.com/TanStack/table>

### 推荐级别

**A 级。**

### 为什么推荐

特别适合：

- Peer Comparison
- Financial Statement
- Estimate Table
- KPI Table
- Valuation Table
- Screening Result
- Earnings History

支持：

- Sorting
- Filtering
- Grouping
- Aggregation
- Pinning
- Resize
- Custom Rendering

比普通 HTML Table 更适合专业投研产品。

---

## 9. AG Grid

- 官网：<https://www.ag-grid.com/>

### 推荐级别

**B+ 级，重型表格场景使用。**

### 适合什么场景

未来如果希望做到类似：

- Bloomberg
- Capital IQ
- FactSet

这种极高数据密度表格，可以使用 AG Grid。

当前第一阶段不一定需要。

---

## 10. React Flow

- 官网：<https://reactflow.dev/>
- GitHub：<https://github.com/xyflow/xyflow>

### 推荐级别

**A 级，行业 / 公司生态图强烈推荐。**

### 适合展示

```text
Company
→ Product
→ Customer
→ Supplier
→ Competitor
→ Technology
```

以及：

```text
Industry Value Chain

Upstream
→ Midstream
→ Downstream
```

还可以进一步扩展为：

```text
NVIDIA
├── TSMC
├── SK Hynix
├── Micron
├── Supermicro
├── Arista
├── Microsoft
├── Meta
└── CoreWeave
```

这类 Industry Knowledge Graph。

---

## 11. Fiscal.ai

- 官网：<https://fiscal.ai/>

### 推荐级别

**产品设计参考 A+。**

### 建议重点学习

不是复制数据，而是学习：

#### Company Snapshot

一页回答：

- 公司是什么；
- 最新财务；
- Valuation；
- Price Action；
- Key News；
- 核心指标。

然后再进入：

> Deep Research

### 最大启发

**Snapshot 和 Deep Research 必须分开。**

---

## 12. Koyfin

- 官网：<https://www.koyfin.com/>

### 推荐级别

**产品信息架构参考 A+。**

### 值得学习

Koyfin 非常擅长：

> 高信息密度 + 快速建立公司认知。

重点观察：

- Company Snapshot
- Estimates
- Valuation
- Price Performance
- Capital Structure
- Analyst Expectations
- Peer Comparison

---

## 13. TIKR

- 官网：<https://www.tikr.com/>

### 推荐级别

**Expectations / Estimates 模块重点参考。**

### 特别值得借鉴

不要只做：

```text
Revenue Growth
EPS Growth
```

应该做：

```text
Actual
vs
Consensus
vs
Previous Consensus
vs
Revision
```

例如：

| Metric | FY26E | FY27E | 3M Ago | Revision |
|---|---:|---:|---:|---:|
| Revenue | | | | |
| EPS | | | | |
| EBITDA | | | | |
| FCF | | | | |

股票投资真正重要的是：

> **Future Fundamental − Current Market Expectation**

而不仅仅是：

> 公司是不是好公司。

---

## 14. Quartr

- 官网：<https://quartr.com/>
- API：<https://quartr.com/products/quartr-api>

### 推荐级别

**有数据预算时 A 级。**

### 适合补充

- Earnings Call
- Transcript
- Investor Presentation
- Filing
- IR Event
- Management Commentary

特别适合后续建设：

```text
Management Language Change
Guidance Change
Narrative Change
Earnings Call Intelligence
```

---

# 第二部分：Finance Agent 具体升级与优化方案

---

# 1. 首先重新定义产品：Research Log → Investment Research Terminal

当前 Finance Agent 的研究能力并不算弱。

真正的问题是：

> Research Pipeline 的内部状态，直接成为了用户页面。

例如用户看到大量：

```text
research_goal
gathering
validated
support_refs
candidate_pool
stalled
typed observations
research completeness
```

这些字段对 Agent Developer 很重要。

但投资者真正需要的是：

```text
这个行业是否值得投资？
现在为什么值得关注？
行业利润在哪里？
谁会赢？
市场目前在预期什么？
什么事情会改变我的观点？
```

因此建议把产品拆成两个模式：

## Investor Mode

默认。

负责：

> 快速完成投资判断。

## Audit Mode

二级页面。

负责：

> Evidence / Source / Claim / Calculation / Validation。

---

# 2. 重新设计 Company Dossier Schema

建议 Company Profile 顶层固定为：

```text
Company Dossier

01 Investment Snapshot
02 Business Model
03 Revenue / Product Mix
04 Industry Position
05 Competitive Landscape
06 Economic Moat
07 Financial Quality
08 Key KPI
09 Earnings & Expectations
10 Valuation
11 Management & Capital Allocation
12 Catalysts
13 Risks
14 Bull / Base / Bear
15 Thesis Breakers
16 Timeline / What Changed
17 Evidence
```

---

# 3. 重新设计 Industry Dossier Schema

建议行业 Profile 固定为：

```text
Industry Dossier

01 Industry Snapshot
02 Why Now
03 TAM / Growth
04 Industry Structure
05 Value Chain
06 Profit Pool
07 Supply / Demand
08 Technology Routes
09 Industry KPI
10 Competitive Landscape
11 Winners / Losers
12 Public Equity Candidate Pool
13 Valuation / Expectations
14 Catalysts
15 Risks
16 Counter Evidence
17 Scenario Analysis
18 Timeline
19 Evidence
```

---

# 4. 首页变成 Tear Sheet，而不是长报告

## Company 首页建议

第一屏控制在 **8–12 秒建立认知**。

### Header

```text
Company
Ticker
Price
Market Cap
Industry
Last Updated
```

### 第一屏

左侧：

```text
One-line Business Description

3 Investment Theses

3 Key KPIs

Revenue Trend
Margin Trend
FCF Trend
```

右侧：

```text
Investment Score

Bull / Base / Bear

Valuation

Expected Return

Catalysts

Kill Criteria
```

---

# 5. Industry 首页也应该 Tear Sheet 化

例如 AI for Science：

不要第一屏展示：

```text
2/5 已回答
partial
typed metric missing
research stalled
```

而应该展示：

```text
AI for Science

Why Now
AI penetration only ~x%
Big Pharma adoption accelerating
Clinical validation becoming bottleneck

Industry Stage
Early Commercialization

Value Capture
Platform / Data / Automation

Key Bottleneck
Discovery → Clinical Validation

Top Public Expressions
Company A
Company B
Company C

What Changes the Thesis
First AI drug approval
Phase III validation
Milestone revenue conversion
```

---

# 6. 从“文章”升级为 Investment Objects

这是整个架构中最关键的一步。

不要让 Agent 最终输出：

```text
一大段 Markdown
```

而应该输出结构化 Investment Objects。

建议定义：

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

---

# 7. Thesis Object 示例

```yaml
type: thesis

title: >
  AI drug discovery bottleneck is shifting
  from discovery to clinical validation

stance: neutral

importance: 0.92

confidence: 0.78

supports:
  - evidence_127
  - evidence_311

contradicts:
  - evidence_428

monitor:
  - first_ai_drug_approval
  - rentosertib_phase3
  - isomorphic_first_clinical_entry

visualization:
  type: stage_funnel
```

这样同一个 Thesis 可以被：

- 首页；
- Deep Research；
- Timeline；
- Risk；
- Industry；
- Company；
- Chat Agent；

同时复用。

---

# 8. 建立 Visualization Objects

LLM 不应该负责写 HTML。

LLM 应该负责判断：

> 什么信息应该用什么视觉表达。

建议 Schema：

```yaml
visualization:
  type: line_chart

  metric:
    - revenue
    - gross_margin

  period:
    start: FY2022
    end: FY2026

  annotation:
    - event_id_001
```

React Renderer 再决定具体画法。

---

# 9. 建立固定 Visualization Grammar

建议至少实现以下组件。

| Research Object | Visualization |
|---|---|
| Market Size | Historical + Forecast |
| Growth | Line / Bar |
| Value Chain | Flow Graph |
| Profit Pool | Sankey |
| Market Share | Ranked Bar |
| Competition | 2×2 Matrix |
| Peer Comparison | Scatter |
| Revenue Mix | Stacked Bar |
| Margin | Line |
| KPI | Small Multiples |
| Valuation | Historical Percentile |
| Estimate Revision | Revision Line |
| Catalysts | Timeline |
| Bull/Base/Bear | Scenario Matrix |
| Risk | Probability × Impact |
| Technology Route | Roadmap |
| Evidence | Provenance Drawer |

---

# 10. 不要滥用 Radar Chart

专业投研中更常用：

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

Radar Chart 看起来“高级”，但：

- 不容易比较；
- 不适合精确数值；
- 信息密度低。

只在少数定性评分场景使用。

---

# 11. 单独建设 Expectation Layer

这是当前 Finance Agent 很值得增强的一层。

股票不是：

> 好公司 vs 坏公司

而是：

> **Fundamental Reality vs Market Expectation**

建议建设：

```text
Expectation Layer
```

---

## 关键字段

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

Management Guidance Revision
```

---

## 推荐核心图

### Price vs EPS Revision

```text
Stock Price
vs
FY+1 EPS Consensus
```

### Revenue Estimate Revision

```text
3M Ago
→ Current
```

### Earnings Surprise

```text
Actual
vs
Consensus
```

这通常比普通新闻摘要更有投资意义。

---

# 12. Company Profile 增加 KPI Engine

不能所有公司都用同一套 KPI。

应该让 Agent 根据：

```text
industry
business model
revenue model
```

自动选择：

```text
3–8 个最重要 KPI
```

---

## 示例

### NVIDIA

```text
Data Center Revenue
Gaming Revenue
Gross Margin
Networking Revenue
Inventory
Hyperscaler Capex
GPU Lead Time
```

### SaaS

```text
ARR
NRR
CAC Payback
RPO
FCF Margin
Rule of 40
```

### AI Drug Discovery

```text
Pipeline Stage
Number of PCCs
Clinical Assets
BD Upfront
Milestone Potential
Cash Runway
R&D Burn
```

---

# 13. 建设 Business Quality / Moat Framework

建议 Moat 不只是一段文章。

拆成固定对象：

```text
Moat

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
evidence:
confidence:
trend:
```

---

# 14. 建设 Catalyst Engine

Catalyst 不应该是普通 Bullet List。

Schema：

```yaml
catalyst:
  title:
  date:
  probability:
  expected_impact:
  current_expectation:
  bull_case:
  bear_case:
  evidence:
```

然后前端：

```text
Catalyst Timeline
```

---

# 15. 建设 Thesis Breaker / Kill Criteria

这是专业投资研究很重要、普通 AI Research 很少做的一层。

每个 Thesis 必须对应：

```text
What would prove me wrong?
```

例如：

```text
Thesis:
AI drug discovery improves economics

Kill Criteria:
Phase II / III success does not exceed industry baseline
```

这样可以避免：

> Agent 不断寻找支持自己原结论的材料。

---

# 16. Counter Evidence 不要只做文本

你现在已经有 Counter Evidence，这是一个优势。

建议升级为：

```text
Thesis
├── Supporting Evidence
├── Counter Evidence
├── Unresolved Conflict
└── What Would Resolve It
```

UI：

```text
Bull Evidence     6
Bear Evidence     4
Unresolved        2
```

点击之后再展开来源。

---

# 17. Evidence System 保留，但放到第二层

不要删除现在已有：

```text
Evidence
Validation
Source
Claim
Counter Evidence
```

这是很有价值的资产。

但是默认页面只展示：

> AI clinical validation remains unproven. [3]

用户 Hover：

```text
3 Sources
2 Primary Sources
Updated Aug 2026
Confidence 78%
```

点击才进入：

```text
Exact Quote
Source
Date
Location
Calculation
Evidence ID
Counter Evidence
Validation State
```

即：

```text
Investor Mode
→ Conclusion

Audit Mode
→ Proof
```

---

# 18. Structured Financial Data First

以后数据获取优先级建议：

```text
Level 1
Structured Financial API

Level 2
Official Filing

Level 3
Company IR

Level 4
Transcript

Level 5
High-quality Research

Level 6
Web Search
```

而不是：

```text
所有东西都先 Web Search
```

---

# 19. 建立 Financial Fact Schema

例如：

```yaml
metric:
  name: revenue

value: 12.8

unit: USD_B

period:
  type: fiscal_year
  value: FY2026

accounting:
  standard: GAAP

source:
  type: SEC
  document: 10-K

confidence: 1.0
```

然后所有页面只读取统一 Fact Store。

---

# 20. 建立 Source Hierarchy

建议定义：

```text
Tier 1
SEC / HKEX / Exchange / Official Filing

Tier 2
Company IR / Earnings Call

Tier 3
Government / Regulator / Academic

Tier 4
Reuters / Bloomberg / FT

Tier 5
Sell-side / Industry Research

Tier 6
Secondary Media

Tier 7
Blog / Community
```

Agent Research 时：

```text
Primary Source > Secondary Source
```

---

# 21. 数据和分析严格分层

建议数据库中分开：

```text
Fact
Observation
Inference
Thesis
Opinion
```

例如：

```text
Revenue = $10B
→ Fact

Revenue +40%
→ Observation

Growth accelerating
→ Inference

Growth will remain >30%
→ Thesis
```

前端用不同标识。

---

# 22. Page Architecture 推荐

Company 页面：

```text
Overview
Financials
KPI
Estimates
Valuation
Competition
Moat
Catalysts
Risks
Timeline
Evidence
```

Industry 页面：

```text
Overview
Market
Value Chain
Technology
Competition
Companies
Economics
Catalysts
Risks
Timeline
Evidence
```

---

# 23. 推荐前端技术栈

```text
Next.js

Tailwind CSS

shadcn/ui

TanStack Table

ECharts

TradingView Lightweight Charts

React Flow
```

职责：

```text
shadcn
→ UI shell

TanStack Table
→ Financial Table

ECharts
→ Fundamental Visualization

TradingView
→ Stock Price

React Flow
→ Value Chain / Ecosystem
```

---

# 24. 不建议 LLM 直接生成最终 HTML

避免：

```text
Prompt
→ LLM HTML
→ Browser
```

问题：

- 页面不一致；
- 视觉漂移；
- 难维护；
- 无法保证数据格式；
- 同一个指标不同页面画法不同；
- Mobile 难适配。

推荐：

```text
Research Agent

↓ Structured Output

Investment Objects

↓ Schema

Visualization Objects

↓ Deterministic Rendering

React Components
```

---

# 25. 建立 Research → UI 中间层

建议：

```text
Raw Evidence
↓
Fact Extraction
↓
Fact Store
↓
Analysis
↓
Investment Objects
↓
Page Composition
↓
Visualization Objects
↓
UI
```

Research Agent 和前端不要直接耦合。

---

# 26. AI for Science 当前页面如何具体改

当前 AI4S 内容本身已经有不少有价值信息。

但应该重新组织。

---

## 当前

大量展示：

```text
candidate-pool
support refs
validation
research status
typed metrics
```

---

## 建议首页

```text
AI FOR SCIENCE

Industry Stage
Early Commercialization

WHY NOW

• Big Pharma adoption accelerating
• AI penetration remains low
• Funding concentrated in platform leaders

KEY BOTTLENECK

Discovery
   ↓
Preclinical
   ↓
Clinical Validation ← current bottleneck
   ↓
Approval

VALUE CAPTURE

Infrastructure
→ Platform
→ Application

TOP INVESTABLE THEMES

AI Drug Discovery
Scientific Simulation
Lab Automation
Scientific Compute

KEY PUBLIC COMPANIES

Insilico
XtalPi
Schrödinger
Recursion
...

KEY INDUSTRY KPI

First AI Drug Approval
Phase II / III Success
BD Revenue Conversion
Pipeline Advancement

THESIS BREAKERS

Clinical efficacy fails to improve
Milestone revenue does not convert
AI economics remain dependent on upfront payments
```

---

# 27. Value Chain 应该从文字变成图

不要写：

```text
上游公司 A/B/C
中游公司 D/E/F
下游公司 G/H
```

而应该产生：

```text
Compute
↓
Scientific Models
↓
Simulation
↓
Drug Discovery
↓
Lab Automation
↓
Clinical Validation
↓
Commercialization
```

每个节点：

```text
Market Size
Growth
Margin
Bargaining Power
Moat
Representative Companies
Investability
```

---

# 28. 公司对比不要只用表格

可以建设：

```text
Moat vs Valuation
```

Scatter：

```text
X = Valuation
Y = Growth
Bubble = Market Cap
```

或者：

```text
X = Commercial Validation
Y = Technology Moat
```

这样很容易看到：

```text
Good Company
vs
Good Stock
```

---

# 29. 加入 Change Detection

Profile 不应该是静态百科。

每次刷新需要回答：

> What changed?

建议：

```text
Since Last Update

Revenue Estimate ↑
Gross Margin Estimate ↑
New Contract
Insider Sell
New Competitor
Clinical Milestone
Valuation Expansion
```

然后：

```text
Impact on Thesis:
Positive / Neutral / Negative
```

---

# 30. 建设 Timeline

长期股票研究非常需要：

```text
Historical Timeline

Product Launch
Earnings
Acquisition
CEO Change
Capital Raise
Regulation
Partnership
Clinical Data
```

Timeline 可以直接关联：

```text
Stock Price
```

---

# 31. 建议建立 Investment Score，但不要黑箱

例如：

```text
Investment Score

Quality        82
Growth         91
Moat           87
Management     76
Valuation      55
Expectation    63
Catalyst       88
Risk           61
```

每一个 Score：

```text
必须可点击
必须能回溯 Evidence
```

不要让 LLM 凭感觉打一个：

```text
8.7 / 10
```

---

# 32. 研究深度分层

建议建立：

```text
L0 Snapshot

L1 Standard Research

L2 Deep Research

L3 Investment Committee
```

---

## L0

30 秒阅读。

## L1

5–10 分钟。

## L2

30–60 分钟。

## L3

完整：

```text
Thesis
Valuation
Scenario
Risk
Catalyst
Position Sizing
Kill Criteria
```

---

# 33. 建议最终 Architecture

```text
                    Finance Agent

                         │
                         ▼

                  Research Router

         ┌───────────────┼───────────────┐

         ▼               ▼               ▼

   Structured Data     Filing        Deep Research

       OpenBB        EdgarTools         Web

         │               │               │

         └───────────────┼───────────────┘

                         ▼

                    Evidence Store

                         ▼

                      Fact Store

                         ▼

                   Analysis Engine

                         ▼

                 Investment Objects

                         ▼

                Visualization Objects

                         ▼

                 Page Composition

                         ▼

       ┌─────────────────┼──────────────────┐

       ▼                 ▼                  ▼

 Company Profile    Industry Profile    Research Report

       │                 │                  │

       └─────────────────┼──────────────────┘

                         ▼

                     Audit Mode
```

---

# 34. 实施优先级

不要同时做所有事情。

---

## Phase 1：先重构 Schema

优先级：**P0**

建立：

```text
Company Dossier Schema
Industry Dossier Schema
Investment Object Schema
Financial Fact Schema
Evidence Schema
```

这是最重要的一步。

---

## Phase 2：重做首页

优先级：**P0**

实现：

```text
Company Snapshot
Industry Snapshot
```

目标：

> 用户 10 秒内理解投资逻辑。

---

## Phase 3：Visualization Grammar

优先级：**P0**

优先完成：

```text
Line
Bar
Scatter
Timeline
Value Chain
Scenario
Risk Matrix
Estimate Revision
```

---

## Phase 4：Structured Financial Data

优先级：**P1**

接入：

```text
OpenBB
EdgarTools
```

减少：

```text
LLM Web 数字抽取
```

---

## Phase 5：Expectation Engine

优先级：**P1**

建设：

```text
Consensus
Revision
Actual vs Estimate
Price vs Estimate
Guidance vs Consensus
```

---

## Phase 6：Thesis / Catalyst / Risk Engine

优先级：**P1**

把研究真正转化成：

```text
Investment Decision System
```

---

## Phase 7：Change Detection

优先级：**P2**

让 Profile 从：

> 静态数据库

升级到：

> 持续维护的投资档案。

---

# 35. 最终判断

当前项目的问题大致可以拆成：

```text
20% UI / CSS

30% Research Template

50% Data + Knowledge Schema
```

因此不要首先把大量时间放在：

```text
颜色
字号
圆角
卡片
动画
```

优先解决：

```text
1. Company / Industry Dossier Schema

2. Investment Objects

3. Visualization Grammar

4. Structured Financial Data

5. Expectations Layer

6. Snapshot UX

7. Evidence Audit Layer
```

当这几层建立以后，即使只使用：

```text
shadcn + Tailwind
```

也会比目前仅做视觉装修提升一个数量级。

最终目标不是：

> “一个能自动生成股票研究报告的 AI。”

而应该是：

> **一个能够持续构建、更新、验证和可视化 Investment Knowledge 的 AI-native Equity Research Terminal。**
