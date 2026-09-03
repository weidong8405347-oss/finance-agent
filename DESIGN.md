# Finance Agent 设计文档

> 定位：从零开始的股票投资 agent 系统。四个关键环节闭环：**Deep Research → Profile 更新 → 投资建议 → 评估**。
> 设计输入：`model_agent.md`（通用 agent 框架 + 投资领域风险清单）、deepseek-harness（插件架构与 UI 交互范式）、auto-deep-research V1.0（已验证的调研流程与数据源实现，作为重写时的参考资产）。
> 本文档是第一份也是唯一一份顶层设计；后续所有模块设计文档不得与本文档的「硬约束」冲突。

---

## 0. 一句话总纲

> Agent 的难点不在循环，在脚手架。
> 本项目的脚手架有四根支柱：**双时态知识库**（事实带「何时为真/何时可知」）、**时间锁数据网关**（评估模式只允许消费 ≤T 可知的信息）、**事件溯源**（append-only 真相源，UI/prompt/报告都是投影）、**评估权力分离**（改进者、评估者、晋升者三方独立）。
> 其中前两根是为「评估不穿越」服务的——这是本系统区别于普通研究 agent 的生死线。

---

## 1. 系统定位

### 1.1 四个 Step 的闭环

```text
┌─────────────────────────────────────────────────────────────────┐
│                        用户 / 定时触发                            │
└──────────────────────────────┬──────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ S1 Deep Research（可多轮迭代）                                    │
│    gap 分析 → 研究问题 → 采集 → 证据验证 → 结论                   │
│    「研究同一个标的可以跑 N 轮，每轮基于档案找缺口」                │
└──────────────────────────────┬──────────────────────────────────┘
                               ▼ 结论+证据
┌─────────────────────────────────────────────────────────────────┐
│ S2 Profile 更新（知识库写入）                                     │
│    股票/行业档案：双时态事实表 + 方法论规则                        │
│    「每次研究完，档案变厚；旧版本永不删除」                         │
└──────────────────────────────┬──────────────────────────────────┘
                               ▼ 档案 as_of 投影
┌─────────────────────────────────────────────────────────────────┐
│ S3 投资建议                                                       │
│    输出 DecisionCard：结论 + 证据链 + 失效条件 + 仓位建议          │
│    风险评审 hook 必达，不可跳过                                   │
└──────────────────────────────┬──────────────────────────────────┘
                               ▼ 决策卡 + 档案快照哈希
┌─────────────────────────────────────────────────────────────────┐
│ S4 评估                                                           │
│    历史时间点回放：as_of(T) 档案 → 决策 → 前瞻收益                 │
│    配对基线对比 + 穿越检测 + 防过拟合统计                          │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 两种运行模式（贯穿全系统的根本二分）

| | 生产模式（live） | 评估模式（eval @ T） |
| --- | --- | --- |
| 目的 | 产生当下可用的研究与决策 | 回答「这套系统在历史上是否有效」 |
| 数据网关 | 全部数据源可用，记录 available_at | **时间锁**：只允许 available_at ≤ T 的记录 |
| 知识库 | 读写最新版本 | 只读 as_of(T) 投影 + 写入隔离的 eval 命名空间 |
| 网络搜索 | 可用 | **默认禁用**（无 PIT 保证），仅放行白名单归档源 |
| 输出 | 当前决策卡 | 历史决策卡 + 前瞻收益对账 |

模式是**进程级开关**，由 Harness 在 run 创建时写入 run manifest，运行中不可切换。eval run 的一切产物（含其产生的档案版本）写入独立的 `{eval_run_id}` 命名空间，永不回流生产知识库——这是防「评估污染生产」的单向阀。

### 1.3 不做清单

| 不做 | 理由 |
| --- | --- |
| 不做实盘下单 | v1 范围外；决策卡是给人看的建议。未来要做也必须走「模拟盘→小资金→扩大」晋升闸 |
| 不做高频/日内 | 研究与评估的粒度是「日/周/月」，数据源的 PIT 精度也支撑不了更高频 |
| 不做全市场自动扫描选股 | 标的池由用户给定或调研主题产出；无边界扫描会让评估口径失控 |
| 不让模型自由访问网络 | 所有数据采集走 DataGateway，没有例外通道；这是时间锁能成立的前提 |
| 不在评估模式提供通用 web 搜索 | 搜索结果页是「今天的快照」，无 PIT 保证，是穿越的最大漏洞 |
| 不用 LLM 的宿主记忆做事实来源 | 模型只分析上下文里的证据；无证据的事实性断言会被校验 hook 拦截（见 4.3） |

---

## 2. 设计原则（继承 model_agent.md，落到本项目）

| # | 原则 | 本项目落点 |
| --- | --- | --- |
| 1 | Agent 是循环，Harness 是脚手架 | loop 保持极简；工程量投向 EventStore / DataGateway / 评估器 |
| 2 | 稳定 Kernel + 可配置 Step Pipeline | S1-S4 是四个 step pipeline，内部环节（gap 分析/采集/写作/评审）全是插件 |
| 3 | 真相源与投影分离 | append-only 事件日志是唯一真相源；prompt 上下文、UI、报告、as_of 档案都是投影 |
| 4 | Hook 必达 + Tool 弹性 | 证据校验/数字保护/风险评审/穿越检测 = hook（模型不可跳过）；搜数据/跑回测 = tool（模型自选） |
| 5 | 单写者 | 知识库只有 ProfileWriter 一个写入者；并行研究子代理只交「候选结论」，由它合并落库 |
| 6 | Fail-closed | 数据源无法给出 available_at → 评估模式拒用；沙箱不可用 → 不执行生成代码；证据不足 → 决策卡允许输出「不行动」 |
| 7 | 权力分离 | 研究/改进 agent 无权改评估代码与评估数据；评估器独立运行；晋升（策略进生产关注池）走人工 Gate |
| 8 | 数字保护 | 股价、财务数字只截断不改写；报告中的受保护数字必须与证据原文逐字一致，校验失败即弃用该结论 |
| 9 | 证据绑定 + 晋升门槛 | 每条事实绑证据（source + 原文摘录 + retrieved_at + available_at）；L3 方法论只存规则不存数字，晋升需 ≥N 次重复 |
| 10 | 系统的品格由它拒绝的东西定义 | 见 1.3 不做清单 |

投资领域特有的高危项（model_agent.md §6.2 的本项目对策）：

| 风险 | 本项目硬约束 |
| --- | --- |
| 未来数据泄漏（穿越） | 双时态知识库 + 时间锁网关 + prompt 穿越审计，三层防线（第 6 章） |
| 幸存者偏差 | 股票池快照含退市/摘牌历史；数据源审计第一条 |
| 回测过拟合 | 样本外 holdout 只允许有限次查询；报告 deflated Sharpe 与 generalization gap |
| 成本假设造假 | 交易成本模型是强制 hook，参数进 run manifest 审计 |
| 事后归因幻觉 | 每个决策绑当时的证据链与档案快照哈希，归因只消费这些绑定 |
| 市场环境漂移 | 评估按市场状态（趋势/震荡/波动率区间）分层报告 |

---

## 3. 总体架构

### 3.1 分层总图

```text
┌─────────────────────────── UI（React Web）───────────────────────────┐
│ Sessions 聊天时间线 │ Knowledge 档案+时光机 │ Decisions │ Evaluations │
│ 纯投影：每个字段可回指 event id；命令经 API 进入 Harness               │
└───────────────────────────────┬───────────────────────────────────────┘
                                │ REST + SSE（只读投影下行 / 命令上行）
┌───────────────────────────────▼───────────────────────────────────────┐
│ Harness（调度层）                                                      │
│ run/turn/step/iteration 状态机 │ 双状态轴 │ 审批 │ 预算 │ 恢复         │
│ run manifest（模式/模型/数据版本/成本参数/评估配置哈希，创建后冻结）    │
└───────┬──────────────┬──────────────┬──────────────┬─────────────────┘
        ▼              ▼              ▼              ▼
   AgentLoop     Step Pipelines   ProfileWriter   Evaluator
   稳定 Kernel   S1/S2/S3/S4      知识库单写者     独立权力主体
        │        插件化环节              │              │
        │   （gap分析/采集/写作/          │              │
        │     校验/评审/回测…）           │              │
        └──────────────┴──────────────┴──────────────┘
                       │ canonical events（append-only）
        ┌──────────────▼──────────────┐
        │ EventStore（L0 真相源）       │  SQLite/JSONL + 快照
        └──────────────┬──────────────┘
                       │ projection
   ┌───────────┬───────┴───────┬─────────────┬──────────────┐
   ▼           ▼               ▼             ▼              ▼
prompt 上下文  UI 投影      报告投影      as_of 档案投影   评估投影
                                │
        ┌───────────────────────┴────────────────────────┐
        │ DataGateway（数据统一入口 + 时间锁）              │
        │ EDGAR/HKEX/cninfo/行情/新闻归档/… 各 adapter      │
        │ 声明 PIT 能力等级；评估模式 fail-closed           │
        └─────────────────────────────────────────────────┘
```

### 3.2 技术选型（新项目，刻意保守）

| 层 | 选择 | 理由 |
| --- | --- | --- |
| 语言 | Python 3.11+ | V1.0 数据源/调研资产可移植；金融数据生态在 Python |
| Agent 编排 | 自研轻量 loop + 插件管线，**不引 LangGraph** | V1.0 经验：LangGraph 的图抽象对「step pipeline + hook」模型是过重且绕路的；model_agent.md 与 dsh 都用「稳定 kernel + 事件 hook」，30 行循环 + 扎实脚手架 |
| 存储 | SQLite（事件表 + 双时态事实表 + 决策表）+ JSONL 快照导出 | 单机自用足够；SQLite 事务保证 append 原子性；JSONL 便于审计与 diff |
| LLM 接入 | 沿用 V1.0 的 LLMRouter 多 provider 模式（.env 三件套） | 已验证；按角色分配 provider |
| 后端 API | FastAPI + SSE | 与 V1.0 dashboard 一致，前端对接经验可复用 |
| 前端 | React + Vite + Tailwind + shadcn 风格组件 | 与 V1.0 frontend 同栈，组件可移植 |
| 回测 | 自研轻量事件驱动回测器（日频） | 需求是「决策卡 → 前瞻收益对账」，不是策略框架；自研可把 PIT 约束焊进引擎内部，外部框架（backtrader 等）的 data feed 模型反而要额外防穿越 |
| 测试 | pytest + 前端 vitest | 沿用 |

### 3.3 双状态轴

两个正交状态轴严格分离（混用是架构病）：

- **RunStatus**（运行状态）：`idle / running / waiting_approval / waiting_external / blocked / error / completed / cancelled`
- **PipelineStage**（业务阶段）：`research / profile_update / decide / evaluate`，外加阶段内进度（如 research 的第几轮 iteration）

UI 同时展示两轴；审批、重试、取消只作用于 RunStatus。

### 3.4 事件溯源核心

- 一切 run 内事实（用户指令、模型 step、工具调用与结果、hook 裁决、档案写入、决策卡、评估对账）都是事件，append-only 入 EventStore。
- **「模型可见 = 已记录」是铁律**：prompt 上下文必须从日志投影重建，运行时不变量校验这一点（借鉴 dsh 的 deriveMessages 模型）。
- 反向不成立：记录 ≠ 模型可见。穿越检测 hook 的职责之一就是审计「投影给模型的内容是否越过了 T」。
- 事件携带 `correlation_id`（run/turn/step 链）与领域字段；StepReport（每步可读摘要）与 StepAttribution（失败归因）作为结构化字段随事件落库——让人 10 秒读懂「这步干了什么、干得怎么样」。

---

## 4. 核心机制设计

### 4.1 双时态知识库（地基中的地基）

每条事实（Fact）携带两个时间戳：

| 字段 | 含义 | 例子 |
| --- | --- | --- |
| `event_time` | 事实在世界中何时为真 | 财报所属期截止日 2024-12-31 |
| `knowledge_time` | 系统何时可以知道它 | 财报发布日 2025-03-15（= 数据源的 available_at） |

**评估与决策只用 `knowledge_time`**：2025-03-01 的决策点看不到 3 月 15 才发布的年报，哪怕 event_time 是去年。这是防穿越的第一性原理——「当时能知道什么」由 knowledge_time 决定。

写入纪律：

- **append-only**：更新 = 新增一个版本（`supersedes` 指向前版本），旧版本永不修改、永不删除。as_of(T) = 过滤 `knowledge_time ≤ T` 后取每字段最新版本。
- **证据绑定**（原则 9）：每条事实必须绑 ≥1 条 Evidence：`{ source_id, source_url, verbatim_quote, retrieved_at, available_at, pit_grade }`。无证据的事实禁止入库（schema 层面 forbid）。
- **冲突不覆盖**：新证据与现存事实冲突时，产生竞争版本 + `conflict_flag`，由下一轮研究或人工裁决，绝不静默覆盖。
- **数字保护**（原则 8）：数值字段原样存储，verbatim_quote 必须逐字包含该数值；展示/摘要环节做逐字校验。

两类实体档案（Entity Profile）：

```text
knowledge/
  stocks/<TICKER>/
    meta.yaml            # 实体元信息（市场/行业/状态含退市标记）
    facts/               # 双时态事实表投影（财务、估值、股东、业务…）
    thesis.md            # 当前投资论点（绑定证据 id 的版本化文档）
    timeline.md          # 关键事件时间线（投影产物）
  industries/<SLUG>/
    meta.yaml
    facts/               # 行业规模、增速、格局、政策…
    chain.md             # 产业链拆解（投影产物）
  methodology/           # L3 元知识：规则+证据引用，无具体数字
```

档案是 EventStore 的**投影**，可从事件流重建；磁盘文件只是缓存与人工阅读界面。

### 4.2 时间锁数据网关（DataGateway）

所有数据采集**必须**经过 DataGateway，无任何例外通道（包括 LLM tool）。每个数据源 adapter 声明 PIT 能力等级：

| 等级 | 含义 | 评估模式 | 例子 |
| --- | --- | --- | --- |
| A | 每条记录有可靠 available_at，可按 T 精确过滤 | 放行 | EDGAR（filing date）、HKEX/cninfo（披露日期）、行情日线（交易日）、Reddit（created_utc） |
| B | 有发布时间但内容可能被编辑/快照是当前态 | 显式配置放行 + 标记 | Substack 文章、新闻归档 |
| C | 无 PIT 保证 | **fail-closed 拒用** | 通用 web 搜索（Tavily）、雪球/股吧当前页、一致预期当前值 |

网关行为：

- **数据源接入前的 PIT 验收清单**（对齐量化行业 PIT 数据验收惯例）：①每行记录有真实「公开时刻」戳（filing/publish date，非 period-end）；②原始值与后续修订值分开存，带 restated 标记（重述不得静默覆盖——FY 截止日与 10-K 提交日之间平均隔 ~2 个月，按 period-end 就当可知 = 白送两个月未来）；③支持服务端 as_of 查询（过滤在数据源侧完成，不靠客户端自觉）；④股票池含退市/摘牌公司（基本面 PIT 与无幸存者偏差的价格池是两件事，分别验收）；⑤数据质量状态透明（flag 而非静默丢弃）。
- 生产模式：全量可用，每条返回记录强制附带 `available_at`（adapter 给不出 → 记录标记 `pit_grade=C` 且评估模式永不可用）。
- 评估模式 @ T：双重过滤——adapter 层按 T 查询 + 网关层校验每条返回 `available_at ≤ T`，越界记录直接丢弃并记 `leakage_attempt` 事件（审计线索）。
- 已知数据缺口（诚实清单）：**分析师一致预期的历史快照**、部分新闻的精确发布时间、已编辑内容的原文。这些维度在评估中要么降级为「不用」，要么只用 A 级替代源——宁可信息少，不可穿越。

### 4.3 LLM 参数知识污染（最隐蔽的穿越通道）

即使数据与知识库完美隔离，**模型本身的训练数据包含未来**（它「知道」NVDA 后来涨了）。这不是理论风险，是已被量化的业界事实（调研详见 docs/best-practices-evaluation.md §1）：

> arXiv 2510.07920（Profit Mirage / FinLeak-Bench）对 FinMem、FinAgent、QuantAgent、FinCON、TradingAgents 五个代表性系统重测：在 backbone 训练截止日**之后**的区间，全部系统夏普衰减 51%–62%、总收益衰减 50%–72%——包括自称「逐日只用 ≤T 数据」的 TradingAgents。主流模型对历史行情问答（收盘价/趋势/事件影响）的准确率 85%–93%，接近百科级记忆。

**结论：数据层面的 PIT 隔离堵不住参数记忆泄漏，模型层防线必须自带可量化的检测手段。** 四道缓解 + 两类度量：

1. **Grounding 纪律**：S1/S3 的 prompt 契约要求一切事实性断言必须引用上下文中的证据 id；决策卡的 rationale 字段只接受证据引用列表。让 LLM 做「证据的分析者」而非「事实的来源」（与 FactFin 把 LLM 降级为策略生成者的思路一致）。
2. **Claim 校验 hook（必达）**：解析模型输出中的事实性断言，核对每个断言绑定的证据 id 是否存在于当次 PIT 上下文；无绑定或绑定越界的断言 → 弃用并记录。
3. **诱饵 canary（周期性）**：在评估上下文中注入合成的「未来事实」（与真实历史相反的诱饵，如编造 T 之后才会发生的并购公告）。若决策卡引用了诱饵 → 该次评估判污染，整批结果作废并告警。
4. **记忆探测（memorization probe）**：为每个 backbone 维护一份 FinLeak-Bench 式探测集——对评估标的池直接提问「T 日收盘价 / T 后走势 / T 后事件影响」（不提供任何证据上下文），准确率即该模型的参数污染基线。换 backbone / 换评估区间时重跑。探测分数写进评估报告，用于校准结论置信度（记忆越强的模型，pre-cutoff 区间的结果越不可信）。
5. **LLM-only 基线（度量 A）**：每个评估时间点同时跑一个「无档案、无 PIT 证据、只有通用 prompt」的对照组。诚实指标 = **系统决策相对 LLM-only 基线的超额表现**。若两者无差异，说明知识库没产生增量（或增量被参数知识淹没），结论必须如实报告。
6. **反事实扰动测试（度量 B，P4 硬化实现）**：对 PIT 证据做扰动（修改关键数字、删除关键事件、价格序列替换为随机游走），测三个指标——PC（扰动后决策不变比例，越高越糟）、CI（置信度不变性，越接近 1 越糟）、IDS（决策分布 KL 散度，越高越好）。决策不随证据变化而变化 = 在背答案。Profit Mirage 实测最差系统 PC=82.13%，多 agent 交叉验证结构 PC 最低（但仍在 0.69 以上）。

### 4.4 插件管线与 hook/tool 分工

四个 step 各是一条 pipeline，内部环节是插件；组合由 profile 配置决定，新增优化 = 写插件，不动主干。

**Hook（必达，模型不可跳过）**：

| hook | 挂载点 | 职责 |
| --- | --- | --- |
| evidence-binding | S2 写入前 / S3 输出前 | 每条结论绑证据，否则拒绝落库/输出 |
| numeric-guard | 任何报告/决策卡产出前 | 受保护数字与证据原文逐字一致 |
| leakage-audit | 每次模型调用前 | 审计投影给模型的上下文是否含 knowledge_time > T 的内容（评估模式） |
| risk-review | S3 决策卡定稿前 | 失效条件/仓位上限/流动性检查，不过不出卡 |
| cost-model | S4 回测引擎内 | 手续费+滑点强制计入，参数进 run manifest |
| step-report | 每个 step 结束 | 落 StepReport（目标/证据/决定/成本/质量旗标） |

**Tool（模型按上下文自选）**：`query_price_history` / `query_filings` / `query_news_archive` / `query_kb` / `request_research_round` / `run_backtest` / `compute_forward_returns` / `compare_baselines`。工具的可用集由模式决定（评估模式下 SearchWeb 类工具不下发——能力清单驱动，平台给不出 PIT 保证，工具就不存在）。

---

## 5. 四大 Step 详细设计

### 5.1 S1 Deep Research（可迭代的研究循环）

继承 V1.0 已验证的编排思路（产业链拆解 → 标的映射 → 基本面 → 情报 → CIO 综合），但重构为**轮次制迭代**：

```text
一轮研究 = gap 分析 → 研究问题清单 → 采集（经 DataGateway）→ 证据验证 → 结论候选
        → [evidence-binding hook] → ProfileWriter 合并落库 → 轮次报告
```

- **gap 分析**（每轮起点，决定「这轮研究什么」）：对照 profile schema 计算完整度（哪些必填维度空缺）与新鲜度（哪些事实 knowledge_time 距今超阈），叠加上轮遗留的 `open_questions` 与冲突标记，产出本轮研究问题。这让「多次迭代不断完善」成为机制而非口号。
- **反方挑战**：核心投资假设必须主动采集反对证据（替代解释、历史失败类比、证伪条件）。注意定位：反方是**证据采集义务**，不是多轮观点辩论——调研证据（FundaPod/arXiv 2605.27864 引 Bikhchandani 信息级联与 Smit et al. 2024）表明多 agent 辩论的收益主要来自独立输出集成，多轮辩论反而固化错误（群思）。
- **独立视角 + 事后综合**：多个分析视角（基本面/行业/估值/反方）相互**隔离**独立产出（不见彼此的结论），由综合者（CIO 角色）事后裁决分歧。同时 Profit Mirage 的实测显示多 agent 交叉验证是泄漏最低的结构——独立产出 + 交叉验证 + 事后综合，防群思与防泄漏兼得。
- **一手来源优先**：监管披露 > 公司原始材料 > 独立专业来源 > 专家观点 > 社区社媒；社媒只做线索入口，不做关键财务事实的唯一证据。
- **收敛条件**：完整度 ≥ 阈 且 无高危冲突 且 预算未耗尽；或人工 steer 终止。
- 每轮结束产出 IterationReport（本轮新增/修订事实数、证据数、解决与新增的 open questions、成本）。

### 5.2 S2 Profile 更新（知识库写入）

- **单写者**：只有 ProfileWriter 能写知识库。并行研究子代理产出的是「候选结论 + 证据」，由 ProfileWriter 原子合并。
- **verify 准入闸（2026-09-03 整改，对应验收事故「完整度 100% 但点进去没内容」）**：
  - **写侧硬门禁**（ProfileWriter 落库前，fail-closed）：空值/占位符（「待补充」之类整体占位）/序列化 JSON 字符串/结构化字段类型违例 → 拒写并落 `hook/verdict`（verify-gate）事件。品格由它拒绝的东西定义：残次品不进知识库。
  - **读侧质量投影**（`verify_entity`，纯投影无状态）：每字段软检查（内容过短/缺数值锚点/仅 C 级证据/开放冲突/陈旧）→ per-field status、`quality_score`（0-1）与实体 status（verified/draft）。UI 列表与 HTML 存档 header 的状态 pill 同源——「完整度」（schema 覆盖度）与「质量分」并排展示，前者不再单独撒谎。
  - **实体 ID 归一**（`normalize_entity_id`，三层纵深）：命令解析/研究工具/单写者都归一（HK 去前导零补 4 位，02228.HK→2228.HK；CN 补 6 位；US 大写）——同一标的只允许一个档案。
  - **存档惰性物化**：HTML 存档是 KB 的纯投影（内容寻址幂等），读路径缺档即同步重渲染——研究走非 command 路径不再导致详情页 404。
- 写入即事件：`fact.asserted / fact.superseded / fact.conflict_raised / thesis.revised`，全部入 L0。
- **L3 方法论晋升**：跨标的/跨行业反复出现 ≥N 次的模式（如「高换手策略在该行业滑点敏感」），由晋升流程写入 methodology/，只存规则与证据引用，不存具体数字。单次偶然不晋升。
- 每次写入后更新 `kb_snapshot_id`（内容寻址哈希）——决策卡与评估都引用它，保证「这个决策是基于哪一版知识」可复现。

### 5.3 S3 投资建议（DecisionCard）

```yaml
DecisionCard:
  card_id: uuid
  created_at: 2025-03-01T09:00:00Z        # 决策时刻（评估模式 = T）
  mode: live | eval
  kb_snapshot_id: sha256:…                # 决策依据的知识库版本
  subject: { kind: stock, id: AAPL }
  action: buy | hold | sell | avoid | watch
  conviction: 1-5
  horizon: 3m | 6m | 12m
  rationale: [evidence_id, …]             # 只接受证据引用，拒绝自由文本事实
  thesis_points: […]                      # 引用 thesis.md 版本
  invalidation:                           # 失效条件（必达 risk-review 检查项）
    - "若季度营收增速连续两季 < X%，论点失效"
  position: { sizing: pct_of_portfolio, max_loss_budget: … }
  quality_flags: [low-confidence, evidence-gap, …]
```

- 允许输出 `avoid`/`watch`——「不行动」是合法且重要的决策，fail-closed 精神在投资端的体现。
- 决策卡本身是不可变事件；事后修改 = 新卡 + supersedes。
- risk-review hook 必达：无失效条件、无仓位上限、证据链不完整 → 不出卡。

### 5.4 S4 评估

见第 6 章（本章是系统设计重心，单独成章）。

---

## 6. 评估体系（重点设计）

> **评估 = 两个完全隔离的部分**（详细对齐稿见 [docs/evaluation-design.md](docs/evaluation-design.md)，以那份为准）：
> - **过程评估**（生产侧）：嵌在 S1-S3 生产循环内，评研究结论/档案/决策卡的质量，反馈驱动下一轮 loop；用最新数据，产出真实投资意见。
> - **效果评估**（模拟侧）：独立评估环境，真实历史数据 + 时间锁回放，答「系统历史上是否有效」；eval 命名空间与生产数据完全隔离，永不回流。
> 两部分的所有评估方法都以 EvaluatorPlugin 插件实现（scope=process/effect/both，硬门禁 or 软反馈在 manifest 声明）。

### 6.1 评估什么——三层对象

| 层 | 对象 | 指标 |
| --- | --- | --- |
| 研究质量 | S1/S2 产出的档案 | 完整度、事实准确率（抽样人工/交叉源核对）、证据绑定率、冲突解决率 |
| 决策质量 | S3 决策卡 | 前瞻收益、超额收益、夏普/卡玛、最大回撤、胜率、相对各基线的 uplift |
| 系统健康 | 全链路 | 穿越检测通过率（必须 100%）、canary 拦截率、成本（token/数据源调用/wall time）、resume 成功率 |

**穿越检测通过率是硬门禁**：不是 100% 时，业务指标再好，本次评估结果整体作废。完整性不进加权总分（原则 7）。

### 6.2 反穿越：三层防线 + 审计

```text
防线 1（数据层）：时间锁 DataGateway——评估模式只放行 A/B 级源，逐条校验 available_at ≤ T
防线 2（知识库层）：双时态 as_of 投影——eval run 只读 knowledge_time ≤ T 的档案版本，
                  且写入隔离命名空间，不回流生产库
防线 3（模型层）：grounding 契约 + claim 校验 hook + canary 诱饵 + LLM-only 基线度量
审计层：leakage-audit hook 审计每次模型调用的输入投影；
        全部穿越尝试落 leakage_attempt 事件，评估报告必须附该日志（应为空）
```

### 6.3 评估协议（历史回放）

**基本单元：一次「时点回放」**

```text
给定决策时刻 T（如 2023-06-30）与标的池：
0. Warm-up 阶段（可省略但推荐）：用 ≤T 的数据跑 S1/S2 建立/补齐 as_of(T) 档案——
   即 InvestorBench/FinMem 的「warm-up 建记忆 → evaluation 禁未来」两阶段协议；
   warm-up 段与评估段严格按日期切分，评估段不得回流任何 >T 的信息
1. 物化知识库 as_of(T) 投影 → kb_snapshot_id
2. （可选）增量研究：若档案在 T 时点完整度不足，允许在 eval 命名空间内
   用 ≤T 数据跑 S1/S2 补研究——这本身也是被评估的能力
3. S3 生成决策卡（上下文只有 as_of(T) 投影 + ≤T 数据）
4. 冻结决策卡，用 (T, T+h] 的真实行情计算前瞻收益（h = 卡上 horizon）
5. 对账：收益归因 + 失效条件是否触发 + 成本模型计入
```

**截止日分区（诚实报告的第一前提）**：每个 backbone 模型的知识截止日登记进 run manifest。评估时间点按「T 是否晚于 backbone cutoff」分为两区：

| 区 | 含义 | 报告口径 |
| --- | --- | --- |
| 污染区（T ≤ cutoff） | 模型可能背过这段历史，数据隔离无法排除参数记忆 | 结果只作参考，必须附记忆探测分数；**不得用于任何「系统有效」的结论** |
| 诚实区（T > cutoff） | 参数记忆天然排除，只需防数据层穿越 | 主结论只能出自这里；代价是区间短、样本少 |

这是 Lopez-Lira & Tang（arXiv 2304.07619）开创并已成惯例的做法：只用 cutoff 之后的数据做评估。InvestorBench 是反面教材——其股票评估窗口（2020-07~2021-05）落在被测模型训练数据之内，绝对分数全部被污染，只剩模型间相对比较有意义。

**协议层结构**：

| 协议 | 内容 | 防什么 |
| --- | --- | --- |
| Walk-forward | 决策点序列 T1<T2<…<Tn（如季度末），严格按时间推进；Tk 的任何产物不得含 >Tk 信息 | 整体穿越 |
| 配对基线 | 每个 Tk 同池同步跑：买入持有 / 基准指数 / 等权组合 / 简单动量因子 / **LLM-only 对照** | 「赚钱只是行情好」；LLM-only 分离出知识库的真实增量 |
| 样本外 holdout | 最近 12 个月为私有 holdout：只在评估配置冻结后运行，**有查询预算上限**（如 ≤10 次），只报聚合统计，不返回逐 case 细节 | 反复调参过拟合近期 |
| 市场状态分层 | 按 trend/volatility 区间把决策点分组，分层报告 | 牛市验证熊市失效 |
| 重复与方差 | 同配置 n≥5 次重复（温度/seed 变化），取 median 轨迹（InvestorBench 惯例），同时报 mean/std/p10 | 单次运气 |

**防过拟合统计纪律**：

- 报告 **deflated Sharpe Ratio**（Bailey, Borwein, López de Prado & Zhu 2014：校正多重检验选择偏差 + 收益非正态的偏度/峰度，配套 PSR）——系统在 dev 区间上每迭代一次，「有效」的显著性门槛就高一截；
- 报告 **generalization gap**：dev 区间表现 − holdout 表现，gap 超阈 = 过拟合结论；
- 评估配置（时间点、标的池、基线、成本参数、阈值）冻结后做哈希，进 run manifest 签名；改配置 = 新一轮评估，历史轮次可查。

### 6.4 指标体系

| 维度 | 指标 |
| --- | --- |
| 效果 | 年化超额收益（vs 各基线）、夏普/卡玛、最大回撤、信息比率、胜率 |
| 决策质量 | 失效条件触发率、avoid 决策的后验正确率、conviction 校准曲线（说 5 分把握的是否真比 3 分准） |
| 知识库健康 | 事实总量/冲突率/陈旧度分布、证据绑定率（必须 100%）、as_of 重建耗时 |
| 穿越防线 | leakage_attempt 数（必须 0）、canary 引用数（必须 0）、LLM-only 增量、**记忆探测分数**（按 backbone 登记）、**反事实 PC/CI/IDS**（P4 起） |
| 效率/成本 | 每决策点 token 与数据源成本、增量研究轮次、wall time |
| 稳健性 | 重复运行方差、分市场状态表现、generalization gap、deflated Sharpe |

### 6.5 权力分离与防作弊

| 角色 | 权限 | 不能做什么 |
| --- | --- | --- |
| Improver（研究/优化 agent 本身） | 读写生产知识库、跑 dev 区间评估、读聚合评估报告 | 改评估代码/配置/holdout 数据；读 holdout 逐 case 细节 |
| Evaluator（评估器） | 独立入口运行评估；独占 holdout 查询预算计数 | 修改知识库生产命名空间 |
| Promotion Gate（人工） | 审评估报告签名 artifact，决定「策略/方法论是否进生产关注池」 | 不被 agent 自动触发 |

技术落地：评估代码、评估配置、holdout 数据路径列入 **protected paths**，agent 工具层对这些路径只读且写操作 fail-closed；**评估配置即声明式 mandate 文件**（时间点/标的池/基线/成本参数/阈值/backbone cutoff，借鉴 ai-hedge-fund 的 mandate YAML 形态），冻结后哈希进 run manifest 并签名；报告与 manifest 绑定，改配置 = 新一轮评估，历史轮次可查。

---

## 7. UI / 交互设计（参考 deepseek-harness 范式）

> 状态：✅ 已确认（2026-08-29）——UI 整体参考 deepseek-harness，不单独出对齐稿。
> 约束只有一条不可让渡：UI 是 EventStore 的纯投影，每个字段可回指 event id（§7.1/§7.3）。

### 7.1 从 dsh 借什么

| dsh 概念 | 本项目对应 |
| --- | --- |
| Session 聊天时间线（turn/step/tool card/thinking 折叠） | 一次研究/决策任务 = 一个 session，时间线渲染每轮研究的 step、工具调用卡片、hook 裁决标记 |
| 「UI 是 session/event 流的投影」，模型可见即可记录 | 同上，我们的 EventStore + SSE 投影；每个 UI 字段可回指 event id |
| 审批对话框（权限策略驱动） | 高成本/高危操作审批：发起全量研究轮、跑评估（花钱）、holdout 查询（消耗预算） |
| Settings（providers/models 页） | 沿用 V1.0 四 provider 配置界面 |
| Workspace 概念 | 「覆盖范围」页：标的池、行业池、监控列表 |
| jobs/后台任务 | 研究轮、评估回放作为后台 job，可断点恢复 |

### 7.2 信息架构（五个一级页面）

```text
┌ Sessions ─────── 聊天式任务时间线（研究/决策/评估任务的统一交互入口）
│     每 step：目标 + 工具卡片 + 证据引用 chips + 成本；turn 级折叠
│     审批以内联对话框出现（如「本轮研究预计消耗 X token，继续？」）
├ Knowledge ────── 档案浏览器（本项目差异化页面）
│     实体列表（股票/行业）→ 实体页：facts 分维度展示 + thesis + 时间线
│     ★ as_of 时光机：日期选择器，整页切换为该实体在 T 时点的档案投影，
│       事实按当时版本渲染，后来才被证伪/修订的内容以 diff 标出
├ Decisions ────── 决策卡列表与详情
│     卡片 → 证据链钻取（每条 rationale 跳到证据原文与当时档案快照）
│     事后对账视图：决策后价格走势 vs 失效条件触发情况
├ Evaluations ──── 评估 dashboard
│     评估 run 列表 → 单次评估：净值曲线（vs 各基线）、窗口矩阵
│     （时间点 × 市场状态的分层表现）、穿越审计面板（必须全绿）、
│     决策点钻取（跳到该时点的决策卡与 as_of 档案）
└ Settings ─────── providers / 数据源与 PIT 等级 / 评估配置 / 预算
```

### 7.3 关键交互细节

- **一切数字可溯源**：UI 中每个事实数字 hover 显示证据 popover（来源 + 原文摘录 + available_at）；点击跳原文。这是「UI 是投影」原则的用户可感知形态。
- **模式可见性**：eval 模式的 session 与时间线有明确的视觉标识（如顶栏 badge「EVAL @ 2023-06-30」），防止用户把历史回放当当前建议。
- **成本仪表**：session 级 token/数据源调用/wall time 实时累计（dsh 的 cost 显示思路）。
- **冲突与不确定性的一等公民展示**：档案中的 conflict_flag、决策卡的 quality_flags 不做弱化处理，用醒目但克制的样式呈现——系统的诚实度是核心卖点。

---

## 8. 数据模型与存储

### 8.1 核心表（SQLite）

```text
events            # L0：id, run_id, turn, step, type, payload(jsonb), ts, correlation_id
facts             # 双时态：entity_kind, entity_id, field, value(jsonb),
                  # event_time, knowledge_time, version, supersedes, evidence_ids, run_id
evidence          # evidence_id, source_id, url, verbatim_quote, retrieved_at,
                  # available_at, pit_grade, raw_ref
decisions         # DecisionCard 全量字段 + kb_snapshot_id + eval_run_id(nullable)
eval_runs         # 评估配置哈希、代码 git hash、时间窗、状态、预算消耗、结论摘要
eval_results      # 逐决策点对账：card_id, T, h, forward_return, excess, 归因(jsonb)
methodology       # L3：规则、证据引用、晋升计数、状态(candidate/promoted/retired)
```

as_of(T) 档案查询 = `SELECT … WHERE knowledge_time <= T AND version = max(version per field)`，加 `(entity_id, field, knowledge_time)` 复合索引；物化投影可缓存并以 kb_snapshot_id 校验。

### 8.2 建议的仓库结构（greenfield）

```text
finance-agent/
  pyproject.toml
  config/                    # config.yaml / providers / 数据源 PIT 登记
  src/finance_agent/
    harness/                 # run 状态机、turn/step、审批、预算、恢复
    loop/                    # 稳定 agent kernel + extension points
    plugins/                 # S1-S4 各环节插件 + 全部 hook + 全部 tool
    eventstore/              # append-only + 投影 + kb_snapshot
    knowledge/               # 双时态 facts、ProfileWriter（单写者）、as_of 查询
    gateway/                 # DataGateway + 数据源 adapter（PIT 分级）
    decision/                # DecisionCard、risk-review
    evaluation/              # 回放协议、回测器、基线、统计（deflated SR 等）、审计
    llm/                     # LLMRouter（移植自 V1.0）
    api/                     # FastAPI + SSE
  frontend/                  # React + Vite + Tailwind
  knowledge/                 # 档案磁盘投影（stocks/ industries/ methodology/）
  evals/                     # 评估配置、holdout 登记、历史评估报告
  tests/                     # unit / contract / eval-harness
  docs/
```

---

## 9. 实施计划（按依赖序，每阶段可验收）

| 阶段 | 内容 | 验收标准 |
| --- | --- | --- |
| P0 地基 | EventStore + 双时态 facts 表 + DataGateway 骨架（先接 2 个 A 级源：EDGAR + 行情）+ 最小 loop | 事件回放可重建任意 run 的 prompt 上下文；as_of(T) 查询正确性单测（含构造穿越用例） |
| P1 研究闭环（生产模式） | S1 轮次制研究 + S2 ProfileWriter + evidence-binding/numeric-guard hook + Sessions/Knowledge 两页 UI | 对一个真实标的跑 3 轮迭代研究，档案完整度逐轮提升，全部事实有证据 |
| P2 决策闭环 | S3 DecisionCard + risk-review hook + Decisions 页 | 出卡被 hook 拦截的用例可复现；卡片证据链可逐条钻取 |
| P3 评估闭环 | 时间锁模式 + 回放协议 + 回测器（含成本模型）+ 配对基线 + Evaluations 页 | 穿越检测用例全绿；一份 8-12 个决策点的 walk-forward 报告，含 LLM-only 对照与 deflated Sharpe |
| P4 硬化 | canary 机制、holdout 预算、protected paths、增量研究（eval 命名空间）、as_of 时光机 UI、断点恢复 | 权力分离渗透测试（让 agent 尝试改评估配置，必须失败）；holdout 一次正式评估 |

依赖序即风险序：P0 的双时态与事件溯源错一点，后面全部返工——所以 P0 的穿越用例测试必须先写（TDD）。

---

## 10. 风险清单

| 风险 | 等级 | 缓解 |
| --- | --- | --- |
| 关键数据源 PIT 能力不达标（一致预期、精确新闻时间） | 高 | 诚实降级：评估中禁用该维度或换 A 级替代源；数据源登记表强制评级后才可接入 |
| LLM 参数知识污染无法完全消除 | 高 | 不追求消除，追求**度量**：LLM-only 基线把增量显性化；canary 抽查；结论表述保守 |
| 全量历史回放成本高（每决策点一次研究+决策 = N×token） | 中 | 增量研究模式（as_of 档案已达标则跳过 S1）；决策点密度可配；先小样本建立协议信心 |
| 双时态模型被实现走样（某个 adapter 漏传 available_at） | 高 | schema 层 forbid 无 available_at 的证据；评估模式网关逐条复核；leakage_attempt 审计 |
| 评估结论统计功效不足（决策点少、噪声大） | 中 | 如实报告置信区间与 deflated 指标；宁可结论「证据不足」不可宣称有效 |
| 范围蠕变（想做实盘、想加高频） | 中 | 不做清单；新增能力必须回答「它的穿越等价物是什么」再动工 |

---

## 附录 A · 概念映射：model_agent.md → 本项目

| 框架概念 | 本项目 |
| --- | --- |
| Harness 双状态轴 | RunStatus × PipelineStage |
| L0-L3 记忆 | EventStore / session 工作记忆 / 双时态 facts 结构化历史 / methodology 元知识 |
| ExecutorGateway + capability manifest | DataGateway + PIT 能力分级（fail-closed 下发工具） |
| 晋升门槛（知识入库） | L3 方法论 ≥N 次重复 + 证据绑定 |
| B0-B3 分级测试 | B0 合成行情（已知最优）/ B1 小区间真实回放 / B2 walk-forward+holdout 正式评估 / B3 UI-投影兼容链 |
| 三档自主模式 | guided（每次研究轮确认）→ milestone（默认）→ autonomous（仅 dev 区间夜间迭代） |

## 附录 B · 概念映射：deepseek-harness → 本项目

| dsh | 本项目 | 备注 |
| --- | --- | --- |
| Cordis 插件 + profile 组合 | 插件管线 + profile 配置 | 借思想不借实现（TS → Python） |
| SessionEvent append-only + deriveMessages | EventStore + prompt 投影 + 「模型可见=已记录」不变量 | 核心借鉴 |
| turn/step 事件流渲染 | Sessions 时间线 UI | 交互范式照搬 |
| capability seam（Service/Provider/Consumer 三角色） | DataGateway adapter + PIT 分级 | seam 思路用于数据源 |
| 审批/权限策略 | 高成本操作审批对话框 | |
| dsh web 本地 token 鉴权、不绑 LAN | FastAPI 本地部署同策略 | 自用工具的安全默认 |

## 附录 C · 从 auto-deep-research V1.0 移植/重写清单

| 资产 | 处置 |
| --- | --- |
| 数据源 adapter（edgar/hkex/cninfo/yfinance/reddit/substack/tavily） | 移植，逐个补 available_at 与 PIT 评级 |
| wiki 的 EvidenceField（value+quote+url+retrieved_at） | 移植进 evidence 表，补 available_at 字段 |
| LLMRouter 多 provider + .env 三件套 | 直接移植 |
| prompt-first ResearchPlan（澄清→确认→执行） | 思路保留，落在 Sessions 交互里 |
| LangGraph 五 agent 编排 | **重写**为轮次制插件管线（保留 prompt 与角色设计经验） |
| Dashboard FastAPI + SSE + React 栈 | 技术栈沿用，页面按 7.2 信息架构重做 |
