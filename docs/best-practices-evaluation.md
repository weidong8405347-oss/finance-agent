# 业界最佳实践调研：AI 投资决策与评估

> 调研目的：为 finance-agent 的评估体系（DESIGN.md 第 6 章）与知识库设计寻找业界已验证的做法。
> 调研日期：2026-08-29。结论已吸收进 DESIGN.md，本文档留存证据与细节。

---

## 0. 一页结论

1. **LLM 回测存在系统性的"利润幻觉"（Profit Mirage）**：arXiv 2510.07920 对 FinMem/FinAgent/QuantAgent/FinCON/TradingAgents 五个代表性系统重测发现，**在模型训练截止日之后，所有系统的夏普比率衰减 51%–62%，总收益衰减 50%–72%**——即使这些系统声称做了"逐日只用 ≤T 数据"的模拟。纯数据隔离不够，因为模型的参数记忆本身就是穿越通道。
2. **业界公认的防污染第一手法：以模型知识截止日划分评估区**（Lopez-Lira & Tang, arXiv 2304.07619：只用 cutoff 之后的新闻做评估）。
3. **可量化的污染检测手段已成熟**：记忆探测 QA（FinLeak-Bench）、反事实扰动敏感度（PC/CI/IDS 指标）、微调前后对比。
4. **PIT（point-in-time）数据是量化行业成熟实践**：每行数据带 filing date + 保留原始值/重述值 + 服务端 as_of 查询 + 无幸存者偏差股票池。我们的双时态知识库设计与之一致，可直接对齐字段模型。
5. **知识库/记忆架构的代表作**：FinMem（分层记忆 + 衰减率）、FundaPod（独立性优先的多 persona + 证据溯源存储 + 知识图谱第二大脑）。
6. **统计纪律**：deflated Sharpe ratio（Bailey & López de Prado）校正多重检验偏差；InvestorBench 用 5 次重复取 median。
7. **多 agent 决策组织形式有争议但证据倾向明确**：独立分析 + 事后综合（FundaPod，防信息级联/群思）优于多轮辩论收敛；但多 agent 交叉验证在防泄漏上表现最好——两者可兼得（见 §4）。

---

## 1. 核心威胁的量化证据：Profit Mirage / FinLeak-Bench / FactFin

**来源**：*Profit Mirage: Revisiting Information Leakage in LLM-based Financial Agents*（ByteDance/SCUT，arXiv 2510.07920，2025-10）

这是与本项目评估设计最相关的一篇论文。它用四个实验系统性量化了 LLM 金融 agent 的信息泄漏：

### 1.1 截止日前后衰减实验

以 GPT-4o（训练截止 2023-10）为 backbone，选市场收益相近的两个区间（2021 Q2-Q3 vs 2024 Q3-Q4，均约 +13%）隔离市场因素：

| 系统 | 类型 | 截止日后夏普衰减 |
| --- | --- | --- |
| FinMem | 单 agent + 分层记忆 | 最严重（TR 衰减 71.85%） |
| FinAgent / QuantAgent | 单 agent + 工具 | 中等 |
| FinCON / TradingAgents | 多 agent 协作验证 | 最小但仍 51–62% |

> **关键教训 A**：TradingAgents 自称"逐日决策、无未来数据"，仍衰减 55.68%。**数据层面的 PIT 隔离无法堵住参数记忆泄漏**——这直接验证了我们三层防线设计（数据层 + 知识库层 + 模型层）的必要性，且模型层防线必须有可量化的检测手段，不能只靠 prompt 自觉。
>
> **关键教训 B**：多 agent 协作/交叉验证是已观测到的最有效泄漏缓解结构（FinCON、TradingAgents 泄漏最低），但不能消除。

### 1.2 反事实扰动实验（可直接移植的检测方法）

对输入做扰动：修改/删除关键事件（财报、监管）、把价格序列替换为历史均值或随机游走、篡改技术指标与基本面因子。若模型真在分析输入，预测应显著改变；实测最差模型 **82.13% 的预测保持不变**——在背答案。

三个可移植指标：

| 指标 | 定义 | 判读 |
| --- | --- | --- |
| PC（Prediction Consistency） | 扰动后预测不变的比例 | 越高 = 越依赖记忆 |
| CI（Confidence Invariance） | 置信度对扰动的稳定性 | 接近 1 = 对输入不敏感 |
| IDS（Input Dependency Score） | 扰动前后预测分布的 KL 散度 | 越高 = 越真在用输入 |

### 1.3 记忆探测审计（FinLeak-Bench）

2000 条历史问答对（2022-01 ~ 2023-06），四类问题：特定日收盘价、趋势预测、事件影响、当日市场表现。GPT-4o/Claude/Grok-3 准确率 85%–93%（趋势题 90.23%）——**主流模型对历史行情有接近百科级的记忆，评估设计必须默认污染存在**。

### 1.4 微调注入实验

往开源模型（Qwen2.5-7B / Llama-3.1-8B）微调注入金融数据：分布内准确率 51.61%→72.16%，但未见过数据的泛化能力下降 21.53%。**微调增益本质是记忆，不是技能**——对本项目含义：不要指望通过微调提升评估有效性，反而要警惕。

### 1.5 FactFin 的缓解架构（值得借鉴的方向）

让 LLM 做**策略生成者**而非**直接决策者**：LLM 输出可执行策略代码（输入=当前市场状态的结构化特征），配合 RAG 把新闻因子化（情绪分数/主题分布，减少"背诵现成结论"的通道）、MCTS 策略进化、反事实模拟器（优化目标显式包含最小化 PC/CI、最大化 IDS）。

> 对本项目的映射：我们的 S3 决策卡强制 rationale=证据引用，正是"分析者而非事实来源"的约束；反事实模拟器可作为评估期的进阶检测手段（P4 硬化阶段）。

---

## 2. 评估基准与协议实践

### 2.1 Lopez-Lira & Tang（arXiv 2304.07619）——截止日划分法

"Can ChatGPT Forecast Stock Price Movements?" 的方法论核心：**只使用模型训练截止日之后发布的新闻头条做评估**，从源头排除参数记忆。GPT-4 对初始市场反应的命中率约 90%，对后续漂移（小盘股、负面新闻更显著）有预测力；且随 LLM 普及，策略收益下降（市场效率提升）。

> 这已成为 LLM 金融评估的"诚实基线"做法：**评估区间必须在 backbone 的知识截止日之后**。

### 2.2 InvestorBench（arXiv 2412.18174）——目前最完整的 LLM 投资决策基准

- **Agent 结构**：Brain(LLM) + Perception + Profile + Memory + Action；记忆为 FinMem 式分层长期记忆（不同层不同衰减率）+ 工作记忆（观察/摘要/即时与扩展反思）。
- **两阶段协议**：**warm-up 段**（给定方向，建立记忆库）→ **评估段**（禁未来价格，逐日决策）。按日期切分 train/test。
- **指标**：CR / SR / AV / MDD，CR 与 SR 为主；基线 = Buy & Hold（单资产）/ 等权组合（组合任务）；**5 次重复取 median**。
- **13 个模型的发现**：闭源模型显著优于开源；**金融领域微调模型没有表现出决策优势**（训练目标偏报告分析而非决策）；参数量与决策质量正相关；震荡混合市况下闭源模型优势扩大。

> **关键教训 C**：InvestorBench 的股票评估窗口（2020-07 ~ 2021-05）在被测模型的训练数据之内——按 §1 的结论，其绝对分数全部被污染，只有模型间相对比较有意义。**我们自己的评估配置必须显式登记每个 backbone 的 cutoff，并分区报告。**

### 2.3 TradingAgents（arXiv 2412.20138）——多角色交易公司结构

- 角色：基本面/情绪/新闻/技术四类分析师 → 多空研究员辩论 → 交易员 → 风险管理团队（激进/中性/保守三视角）→ 基金经理批准。
- 通信协议：**结构化文档为主、自然语言只用于辩论**（避免长对话的"电话效应"）。
- 模拟：逐日推进，声称只用 ≤T 数据。
- 模型分工：deep-thinking 模型做分析/决策，quick-thinking 模型做数据检索/摘要——与我们 config 按角色分配 provider 的做法一致。

### 2.4 ai-hedge-fund（virattt，开源工程参考）

- "Fund 即一等公民"：策略/风控/再平衡周期写成声明式 **mandate YAML**，可回测、可模拟盘；alpha model（Buffett/Graham/Lynch 等 persona 信号）可插拔、可单独回测。
- 工程件：`backtesting/engine.py`（逐日推进）、`event_study/`（事件研究）、`risk/limits.py`、`validation/`。
- **可借鉴**：评估配置 = 声明式 mandate 文件 + 冻结哈希，与我们 run manifest 设计天然契合。

---

## 3. 知识库 / 记忆架构实践

### 3.1 FinMem（arXiv 2311.13743）——分层记忆 + 衰减

Profiling（人设与风险偏好）+ Memory + Decision 三模块。长期记忆分层、各层不同衰减率（越深越持久），检索 top-K 入 prompt；即时/扩展两种反思。**与本项目的差异**：FinMem 的记忆是向量库 + 衰减淘汰（会遗忘、不可回溯）；我们需要的是 append-only + 版本化 + as_of 可重建（评估要求精确回溯，不能靠衰减概率）。

### 3.2 FundaPod（arXiv 2605.27864）——机构级基本面研究 agent，与本项目形态最接近

多 persona（价值投资者/宏观策略师…）研究平台，五个设计原则全部与本项目共振：

| FundaPod 原则 | 要点 | 我们的对应 |
| --- | --- | --- |
| DP1 人类中心增强 | 产出供 PM 检视/质疑/选择性采纳的 memo，不自动行动 | S3 决策卡 = 给人看的建议，不做实盘 |
| DP2 **先独立后综合** | persona 隔离推理，事后由 PM/master agent 裁决；引用证据：多 agent 辩论的收益主要来自独立输出集成，多轮辩论会固化错误（信息级联 Bikhchandani 1992；Smit et al. 2024） | 见下方设计修正 |
| DP3 **溯源是一等存储** | claim 链接到 content-addressed 证据工件，跨版本保留 lineage | 我们的 evidence-binding hook + kb_snapshot_id |
| DP4 确定性工作与判断工作分离 | 数据摄取/KPI 抽取 = 确定性 skill；写作/主题探索 = agent skill；声明式 needs/produces 契约 | hook（必达确定性）vs tool（模型自选）分工 |
| DP5 沿独立轴可扩展 | 数据源/skill/persona/workflow 四个扩展点共用元数据契约 | 插件管线 |

知识图谱"第二大脑"连接 ticker/memo/persona/theme/证据，支持跨公司模式浮现与覆盖缺口识别（= 我们 S1 gap 分析的图版）。

> **设计修正（已吸收进 DESIGN.md）**：S3 决策环节从"多空辩论"改为 **independence-preserving**：多个分析视角（基本面/估值/风险/反方）**相互隔离**独立产出判断，由 CIO 综合者事后裁决；辩论只作为"证据采集义务"（反方必须找反对证据），不做多轮观点收敛。理由：信息级联证据 + Profit Mirage 显示协作验证泄漏最低 → 独立产出 + 交叉验证 + 事后综合，两者兼得。

---

## 4. PIT 数据工程实践（量化行业成熟做法）

**来源**：Tradevo Data 的 PIT 指南 + Sharadar/Compustat 产品形态。

非 PIT 基本面数据的两个坑，正好对应我们双时态模型的两个时间戳：

| 坑 | 机制 | 我们的字段 |
| --- | --- | --- |
| **报告滞后**（reporting lag） | 财年 12-31 结束，10-K 可能次年 2-3 月才提交；假设 1-1 可知 = 白送两个月未来 | `event_time`（财年截止）vs `knowledge_time`（= filing date） |
| **重述**（restatement） | 普通数据库用新值静默覆盖历史；模型学到当时不存在的"修正后"数据 | `supersedes` 版本链 + `restated` 标记，旧版本永不删除 |

业界 PIT 数据集验收清单（我们 DataGateway 的 A 级源评审可直接采用）：

1. 每行有真实 filing-date 戳（非 period-end）；
2. 原始值与最新值分开存，带 restated flag；
3. 服务端 as_of 查询（`first_filed <= as_of` 过滤在服务端做，不靠客户端自觉）；
4. 无幸存者偏差：股票池含退市公司（注意：基本面 PIT 与无幸存者偏差的**价格/股票池**文件是两件事，都要查）；
5. QA 状态透明（flag 而非静默丢弃）。

数据源选项（评估用基本面）：DIY EDGAR（免费，XBRL 解析+修正案去重+重述处理是数周工程）/ Sharadar SF1（datekey 结构 PIT，零售价位，含无幸存者偏差股票池）/ Tiingo（as-reported 维度）/ Compustat-LSEG（机构级，1987 起，$10k+/yr，WRDS）。

> **建议**：v1 评估从 DIY EDGAR（A 级，filing date 精确）+ 行情日线起步；Sharadar 作为升级选项登记进数据源 PIT 评级表。

---

## 5. 统计纪律

- **Deflated Sharpe Ratio**（Bailey, Borwein, López de Prado & Zhu, 2014）：校正两类业绩虚高——多重检验下的选择偏差 + 收益非正态（偏度/峰度）。迭代研究本质是在 dev 区间上反复试错的"多重检验"，每迭代一次显著性门槛抬高一截。配套：PSR（Probabilistic SR）。
- **重复运行取 median**（InvestorBench：5 epochs 取中位轨迹）。
- **两阶段时间切分**（warm-up 建记忆 / evaluation 禁未来，InvestorBench & FinMem 共同做法）。

---

## 6. 对 DESIGN.md 的吸收清单

| # | 调研发现 | 吸收位置 |
| --- | --- | --- |
| 1 | 截止日前后分区报告（污染区/诚实区） | §6.3 评估协议 + run manifest 登记 backbone cutoff |
| 2 | 记忆探测（FinLeak-Bench 式 QA）作为模型污染校准 | §4.3 模型层第 5 条 |
| 3 | 反事实扰动 PC/CI/IDS 指标 | §4.3 + §6.4 指标表（P4 硬化实现） |
| 4 | 多 agent：独立产出 + 交叉验证 + 事后综合（非辩论收敛） | §5.1 反方挑战定位 + §5.3 决策环节 |
| 5 | warm-up / evaluation 两阶段回放 | §6.3 时点回放协议 |
| 6 | PIT 数据验收清单 + reporting lag/restatement 双坑 | §4.2 数据源评审清单 |
| 7 | DSR/PSR + median-of-N | §6.3/§6.4（原已有，注明出处与公式参数） |
| 8 | 评估配置 = 声明式 mandate 文件 | §6.5 run manifest 细化 |
| 9 | FinMem 式衰减记忆不适用评估回溯 | §4.1 注释（为什么不用向量库+遗忘） |

## 附：核心参考清单

| 文献/系统 | 一句话 |
| --- | --- |
| arXiv 2510.07920 Profit Mirage / FinLeak-Bench / FactFin | LLM 回测泄漏的四维量化证据与反事实缓解框架（最重要） |
| arXiv 2304.07619 Lopez-Lira & Tang | 截止日后纯净评估法 |
| arXiv 2412.18174 InvestorBench | 13 模型投资决策基准：warm-up/eval 两阶段、median-of-5、B&H 基线 |
| arXiv 2311.13743 FinMem | 分层记忆 + 反思的交易 agent |
| arXiv 2412.20138 TradingAgents | 交易公司式多角色结构；结构化通信协议 |
| arXiv 2605.27864 FundaPod | 机构级基本面研究 agent：独立性优先、溯源存储、KG 第二大脑 |
| Bailey & López de Prado, Deflated Sharpe Ratio | 多重检验与非正态校正的夏普显著性 |
| Tradevo/Sharadar/Compustat PIT 实践 | PIT 数据验收五清单 |
| virattt/ai-hedge-fund | mandate YAML + 可插拔 alpha model 的开源工程参考 |
