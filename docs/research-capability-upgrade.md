# 调研能力提升设计（流程 / 模型 / 数据 三层升级）

> 来源：2026-09  grill-me 会话（AI for Science 赛道调研事故驱动）。
> 状态：**P1、P2 已完成**（2026-09，252 测试全绿）；P3-P5 待开工。
> （grill-me 七轮裁决全部落稿，见 §2 裁决记录）。
> 约束：DESIGN.md 硬约束全部不动——时间锁数据网关、fail-closed、证据绑定、
> 数字保护、权力分离、双时态 KB。本稿所有扩展都在既有纪律**之内**做加法。
> 参考资产：[ai-berkshire](https://github.com/xbtlin/ai-berkshire)（四角色并行、
> financial_rigor、漏斗淘汰留痕、信息丰富度评级、搜索权限预检五个机制已吸收）。

---

## 1. 背景与根因诊断

### 1.1 事故现场

用户提问：「深度调研 AI for Science 赛道有哪些股票值得投资，从市值/未来空间/
技术护城河/市场份额/财报现金流/人才密度/CEO 战略眼光七个维度挖掘未来三年
10 倍成长的美股或港股」。系统行为与结果：

1. 主 agent **凭参数记忆**直接列出 RXRX/SDGR/TEM/EXAI/ABCL/2228.HK——
   「属于该赛道」的分类断言零证据绑定，绕过了全部 grounding 校验
   （发生在 command 之外，校验管不到）；
2. 逐票发起 `/profile`，某票因研究停滞（stalled）结束，**档案完整度 0%**；
3. `[command 完成]` 通知未带 ticker/command_id，用户无法定位失败标的
   （`commands/runner.py` 唤醒消息缺字段，已定位）。

### 1.2 根因分层

| 层 | 根因 | 证据 |
| --- | --- | --- |
| 流程（主矛盾） | 行业级问题没有对应流程：commands 全部单标的，漏斗（赛道拆解→标的池→粗筛→深研→排序）五个环节一个都不存在 | `commands/registry.py`；主合同第 5 条「多标的各自独立 run_command」 |
| 流程 | 标的池凭模型记忆产出，无证据绑定 | 事故现场 ① |
| 流程 | 无粗筛层，直接对 6 票发起全量深研，成本与价值倒挂 | 主 agent 行为 |
| 流程 | stalled = 死路：无换策略/换源/降级阶梯；主 agent 纪律「两次 stalled 永久停手」 | `research/loop.py` `stop_reason="stalled"`；`main_agent.py` 纪律 10 |
| 数据 | 研究工具面只有 EDGAR + stooq 行情 + canary_news，**无 web 搜索**；七个调研维度仅「财报及现金流（美股）」可被支撑；2228.HK 走 HKEX 不在 SEC | `gateway/tools.py` GATEWAY_TOOL_SCHEMAS |
| 模型 | router 请求体不下发 `reasoning_effort`——kimi-k3/GLM-5.3 的 max 档从未启用；所有步骤共用强模型烧 token | `llm/router.py` `complete()` body 仅 model/messages/tools |
| 可观测 | 通知缺上下文；stalled 无诊断输出 | `commands/runner.py:224` |

**关键判断**：stalled 0% 是 fail-closed 在正确工作（查不到 → 拒写 → 一轮零写入 →
停滞收敛）——纪律本身是对的，病根是「无米 + 无流程」。模型/prompt 不是瓶颈。

---

## 2. 裁决记录（grill-me 七轮）

| # | 议题 | 裁决 |
| --- | --- | --- |
| Q1 | 瓶颈定位 | 用户裁决：**流程 > 数据源 > prompt**；loop 机制（轮次/单 agent/步数上限）一并修 |
| Q2 | 行业漏斗形态 | 新增 `/industry` command，F1-F5 五步；**F3 人工闸口保留**，且粗筛必须具备真调研能力：产出调研卡 + 判断依据；闸口**可打回**，打回反馈落事件、迭代优化粗筛结果 |
| Q3 | stalled 处理 | **四级升级阶梯**（换策略→换源/记缺口→降级产出→上报人工）；stalled **必须产出缺口诊断卡**（对齐失败可见性三通道） |
| Q4 | loop 重构 | **维度并行制**：每轮 = gap 分析 → 4 维度组并行 researcher → 汇总写回；预算动态化（0% 档案 5 轮 / >50% 3 轮 / 仅刷新 1 轮）；worker 用**三 flash 齐上**（deepseek-v4-flash-0731 + GLM-5.3-Flash + qwen3.8-flash），预算放开、并行 6-8 路 |
| Q5 | schema 映射 | 7 维度映射见 §4.6；新增字段一律先进 optional；**档案只存事实，判断（含 10 倍潜力评级）只在 F5 报告** |
| Q5b | 模型层 | 效果优先：顶层 kimi-k3@max / GLM-5.3@max（实测超 pa/gpt-5.6-sol）；provider **不用 OpenRouter**，做成**前端可自配**；关键步骤（F5/决策卡）**双强模型交叉** + 多视角互相 review（ai-berkshire 模式） |
| Q6 | 数据源预算 | **免费组合先行**：Exa（免费额）+ yfinance + akshare + GDELT + HKEXnews 爬取；付费源实测卡点上补，第一候选 FMP |
| Q7 | prompt 形态 | **playbook 文件化**（`playbooks/*.md`，git 版本管理，run manifest 记版本哈希）；grounding 等硬纪律留代码内置不许改 |

---

## 3. 总览：一张图

```
用户：「调研 AI for Science 赛道…」
  │
  ▼  /industry <主题>（新 command）
F1 赛道地图 ──► F2 标的池 ──► F3 粗调研+筛选 ⟲人工闸口 ──► F4 深研 fan-out ──► F5 排序对比
 (industry 档案)  (双通道+证据)   (调研卡/打回迭代)      (4维度并行×3flash)   (双强对抗)
                                    │打回
                                    └── 反馈落事件 → 扩池/补调研/换维度 → 重呈
```

底座升级（支撑全部环节）：数据源四新源、calc 工具、模型三级制、playbook 文件化、
可观测性三修复。

---

## 4. 详设

### 4.1 行业调研漏斗 `/industry <主题>`

新 command，五步，每步产物落事件可审计。

| 步 | 动作 | 产出 | 关键纪律 |
| --- | --- | --- | --- |
| F1 赛道地图 | web 搜索拆解子赛道；赛道断言绑证据 | industry 实体档案（复用 INDUSTRY_SCHEMA） | 子赛道归属必须引证据 |
| F2 标的池 | **双通道**：①数据通道——市值锚定∪活跃榜∪涨幅榜（A∪B∪C，来自基本面/行情 adapter）；②语义通道——三 flash 各自挖掘取并集 | 标的池事件：每票带市场/交易所/「属于该赛道」证据 id；未上市候选单列 | 堵记忆列票；「沾边股」（行业占比 <30%）标注非纯正标的 |
| F3 粗调研 + 人工闸口 | 每票一张**粗调研卡**（业务一句话/赛道归属证据/市值·收入·增速·现金/初步亮点/初步风险/信息丰富度 A-B-C 评级/判断依据全部引证据 id）→ 呈交推荐深研名单 + 不推荐名单**各带淘汰理由** | 筛分表 + 调研卡全文 + 「本轮改了什么」diff（迭代轮） | **打回回环**：用户反馈落事件 → 扩池/补调研/换维度 → 重呈，直到确认；复用 ApprovalService 做闸口 |
| F4 深研 fan-out | 每只入选票独立 profile 子 run，并行 + 失败隔离（一票 stalled 不阻塞其余） | 各票档案更新 | 子 run 结果就是现有 research/profile 管道，不重写 |
| F5 排序对比 | 7 维度对比矩阵 + 候选排序；每格强制引证据 id，数据不足标「未知」；**双强模型交叉 + 四视角对抗**（§4.4） | 排序报告（非决策卡） | 「10 倍潜力评级」是判断，只存在这里，不进档案 |

事件词汇新增：`industry/funnel_start`、`industry/screen_proposed`（闸口呈交）、
`industry/screen_feedback`（打回）、`industry/funnel_done`。

### 4.2 研究 loop 重构（维度并行制）

- **每轮** = gap 分析 → 缺失字段按维度分组 fan-out → 汇总写回 → 过程评估。
- **4 维度组**（对齐 ai-berkshire 四角色，同时是 F4/P4 对抗分析的视角分工）：
  财务组（revenue_fy/net_income_fy/cash_flow/valuation）·
  商业模式组（business_model/moat）·
  行业竞争组（peers/market_share/future_space）·
  风险与管理层组（risks/management/catalysts/counter_evidence）。
- **每组独立 context + 独立步数预算 12 步**，组间隔离（财务与定性思维链不互相稀释）。
- **预算动态化**：完整度 0% → 5 轮；>50% → 3 轮；仅刷新陈旧 → 1 轮。
- **写入纪律不变**：并行 researcher 只产提案；全部提案过 quote 逐字校验 +
  ProfileWriter 单写者硬门禁（内部串行化）；同字段竞争走现有 conflict_flag。
- 收敛/停滞判定不变（完整度达标且无 stale 收敛；一轮零写入触发 §4.3 阶梯）。

### 4.3 stalled 四级升级阶梯 + 缺口诊断卡

| 级 | 动作 | 触发 |
| --- | --- | --- |
| L1 换策略重试 | 同 run 内自动：换查询词、缩小缺口子集、改抓 filing 其他章节 | 一轮零写入 |
| L2 换源 / 记缺口 | 多源间自动切换；需要的源不存在 → 落 `source/gap` 事件（数据源需求自动发现：系统明示「缺 HKEX 源」而非默默死） | L1 仍零写入 |
| L3 降级产出 | stalled 必产**缺口诊断卡**：缺哪些字段、试过哪些查询、各源分别返回什么、建议替代方向/替代标的 | L2 无解 |
| L4 上报人工 | 主 agent 汇报数据边界，用户决定换标的/放弃/等源接入 | L3 之后 |

失败可见性三通道对齐：诊断卡 = 事件落库（`research/stall_diagnostic`）+
日志 + 用户可见（UI 卡 + command 完成通知摘要）。

### 4.4 模型层：三级制 + provider 自配

**角色分级（效果优先，成本其次）：**

| 角色 | 模型 | effort | 职责 |
| --- | --- | --- | --- |
| `research` | kimi-k3（主）/ GLM-5.3（备） | **max** | 主 agent、F3b 筛分呈交、F5 排序综合、决策卡 |
| `research-worker`（新） | deepseek-v4-flash-0731 + GLM-5.3-Flash + qwen3.8-flash **三家齐上** | 默认 | 维度并行 researcher、F3a 粗调研卡、F2 语义挖掘、filing 阅读；预算放开，并行 6-8 路 |
| `fast` | GLM-5.3 | 默认档 | rubric judge、评估基线 |

pa/gpt-5.6-sol 降为评估基线对照组（横向比较模型质量用）。

**三 flash 用法分场景：**

| 场景 | 用法 |
| --- | --- |
| F2 标的池挖掘 | **冗余并集**：三家各挖一遍取并集（召回敏感，知识偏差互补防漏票） |
| F4 维度深研 | **分工为主**：4 维度组分配三家（吞吐最大化）；单家限流自动 failover |
| 关键数字字段 | **双跑取一致**：两家独立提同一数字，一致收、不一致走 conflict_flag |

**router 改造（P1）：**
1. `ProviderSpec` 增加 `effort` 字段，`complete()`/`stream_complete()` 请求体下发
   `reasoning_effort`（OpenAI 兼容协议；dashscope 兼容模式实参名以实测校准为准——
   P1 首个验证项）；
2. timeout 从全局 `FINANCE_AGENT_LLM_TIMEOUT` 改为**按角色覆盖**（max effort 显著拉长
   首 token 延迟，research 角色需上调）；
3. role_map 从 finance-agent 自有配置文件读取（见下）。

**provider 前端自配（不用 OpenRouter）：**
- finance-agent 自有配置 `data_dir/llm-providers.json`：providers（name/base_url/
  api_key/models）+ role_map + 每角色 effort/timeout；缺省回落 pi `~/.pi/agent/models.json`（现状行为保留）；
- P1 先落后端文件读写 + 热生效（无需重启）；
- P5 落前端配置页（列表/新增/编辑/测活按钮）。
- 观测：事件 payload 记录每条提案出自哪个模型，**按模型分桶统计提案拒绝率**
  （`tracker.rejected` 已有），某家 flash 掉队一眼可见，据此调分工权重。

**双强交叉 + 四视角对抗（P4）：**
- F5/决策卡：kimi-k3@max 与 GLM-5.3@max 各出一版，分歧处标注给你看；
- 四视角（商业模式=段永平 / 财务估值=巴菲特 / 行业竞争=芒格 / 风险与管理层=李录）
  由两强模型分担，产出真实张力而非平衡废话；
- 关键论断 claim 级证伪（3 agent、2/3 票证伪才剔除）——P4 可选增强。

### 4.5 数据源扩展（gateway adapter 模式不变）

全部走现有 adapter 协议（`capability()` 声明 PIT 等级 + `query(request, as_of)`），
ChunkStore 证据校验、评估模式双重过滤纪律不动。

| 新源 | 覆盖 | PIT 目标级 | 成本 | 备注 |
| --- | --- | --- | --- | --- |
| **Exa** web 搜索 | 定性维度（护城河/CEO/人才/份额/TAM） | B（逐条带 publishedDate；无日期条目诚实降级无 PIT） | 免费额起步，按量 ~$5/千次 | ✅ 2026-09-01 已配 key 并实测：中文查询返回新浪/医药魔方等有效中文源 |
| **Tavily** web 搜索 | 同上，Exa 的备份/并集源 | C（无逐条发布时间保证） | 1000 credits/月免费 | ✅ 同日接入并实测通过；双源并集提召回 |
| **yfinance + akshare** | 市值/股本/报表粗数据；akshare 补港股 | C（快照，生产可用、评估禁用） | 免费 | F2 数据通道 + F3a 粗调研够用 |
| **GDELT** | 全球新闻含中文媒体 | B（发布时间戳） | 免费 | catalysts/risks 维度 |
| **HKEXnews 披露易** | 港股公告/财报原文 | A（披露日期） | 免费 | **无官方 API 需爬取 → P2 先 spike** |
| （备选付费）FMP | 美股+港股基本面一把抓 | B/C | ~$19/月 | 实测卡点再上；有 official MCP |

评估模式合规：C 级源在评估模式 fail-closed 为既有机制，需**补测试**——
新源注册后评估 run 越界记录被丢弃且落 `leakage_attempt` 事件。

### 4.6 schema 扩展 + 事实/判断分层

STOCK_SCHEMA.optional 新增（不进完整度、不进硬门禁；跑 2-3 个真实调研后
视证据可得性再决定是否晋升 required）：

| 字段 | 承载维度 |
| --- | --- |
| `future_space` | 未来空间（TAM/赛道增速事实，link industry 实体） |
| `market_share` | 现有市场份额 |
| `talent_density` | 人才密度 |

其余映射：市值 → `valuation` 值结构升级为 `{market_cap, pe, ps, as_of}`；
CEO 战略眼光 → 现有 `management`；护城河/财报现金流/peers 已有。
INDUSTRY_SCHEMA.optional 新增 `sub_sectors`、`player_landscape` 支撑 F1/F2。

**事实/判断分层**：档案（双时态 KB）只存可绑证据的事实；「10 倍潜力评级」
等判断只存在于 F5 报告，可随评估口径演进重算，永不回写档案。

### 4.7 calc 工具与数字保护升级

新增服务端 `calc` 工具（ai-berkshire financial_rigor 的对口物）：
- Python `Decimal` 精确计算，**禁止模型心算**进 prompt 纪律；
- 市值验算：`price × shares` 对比上报值，偏差 >1% 告警；
- 关键数字双源交叉：两源不一致 >1% → 自动 conflict_flag（现有冲突机制承载）；
- 三情景估值辅助（乐观/中性/悲观），供 F5 与决策卡使用。

### 4.8 playbook 文件化

`playbooks/*.md` 独立文件：F1 赛道地图、F3a 粗调研卡、四视角对抗、F5 综合各一份；
run 启动时加载，manifest 记录所用版本哈希（可复现性不受影响）。
**硬纪律留代码**（GROUNDING_CONTRACT、证据绑定、数字保护）不进 playbook、不许改。

### 4.9 可观测性三修复

1. command 完成通知带 `ticker` + `command_id` + `run_id`（`runner.py:224`，一行级修复）；
2. stalled 缺口诊断卡（§4.3 L3）；
3. **command 启动前 gateway 预检**：各 adapter 探活 + API key 有效，不过则拒启动并
   明示哪个源挂了（对齐 ai-berkshire 的 WebSearch 预检纪律——防后台 agent 静默退化）。

---

## 5. 实施分期（依赖序）

| 期 | 内容 | 解锁 | 验收 |
| --- | --- | --- | --- |
| **P1 速效修复** ✅ | 通知带 ticker/command_id（wake 消息，2026-09 落地）；stall 诊断卡（`research/stall_diagnostic` 事件 + 零产出 stalled→blocked + 建议规则）；gateway 预检（`gateway/preflight` 事件 + 关键组拦截/降级）；router effort 下发 + 分角色 timeout；`llm-providers.json` 后端（读 + 热生效，写 P5 进 UI） | 立即可感 | dashscope 实参校准冒烟（FINANCE_AGENT_NETWORK=1 已实测通过）；预检拒启动/降级测试 |
| **P2 数据层** ✅ | Exa（B）/ GDELT（B）/ HKEXnews（A，spike 一次通过直接转 adapter）/ yfinance+akshare 基本面快照（C）四 adapter + calc 工具（Decimal 验算/双源交叉/三情景）；预检并行化 + 300s TTL | 漏斗有米 | 每源离线契约测试 + 评估模式 fail-closed 回归 + 真实冒烟（HKEX/基本面实测通过；GDELT 本机不可达见 §6；Exa 待 key） |
| **P3 漏斗与 loop** ✅ | `/industry` F1-F5 全通（闸口复用 ApprovalService，打回带 comment 迭代重呈 ≤3 轮）；维度并行 loop（4 组 × 12 步，无池时旧式单 kernel 兼容）；动态预算（0%→5/>50%→3/仅刷新→1）；schema optional 五字段；playbooks/*.md 五份 + 版本哈希落事件 | 事故场景端到端跑通 | 测试锁定：漏斗 e2e/打回迭代/无反馈 blocked/并行组隔离/失败隔离/动态预算/兜底加载 |
| P3 已知简化 | F2 语义通道三 worker 并集已做；「数据通道市值锚定」靠 fundamentals 逐票查询，无全市场 screener API（后续评估 FMP screener）；F5 单强模型（双强交叉在 P4） | — | — |

### P3 真实验收实录（2026-09-01/02，`/industry AI for Science 美股港股` 真跑四轮）

端到端结果：F1 行业档案 100% → F2 标的池 39 只（3 路并集、每票绑证据；发现 EXAI 已被收购退市——
纯记忆列票抓不到这种事）→ F3 粗调研卡 + 闸口（打回迭代实测生效）→ F4 六票深研至 88-100% →
F5 排序报告（逐格证据针 + calc 验算 + ABSI 三源市值 42.3% 分歧走 conflict + 未知维度留白）。

实测发现并修复的缺陷（全部进测试锁定）：

| 缺陷 | 修法 |
| --- | --- |
| flash worker 囤证据空转（124 次登记 0 写入） | 生产纪律进 brief/playbook + group_end 记 evidence_registered |
| 行业字段无维度组（全落 misc 单组丢失并行） | 按实体类型分表（stock/industry 两组映射） |
| 无日期 B 级记录被证据校验误拒 | 逐条有效等级降级（源级 B + 本条无 available_at → 本条 C） |
| 自定义工具无 schema 被空参调用 | submit_card/propose_candidates 登记 TOOL_SCHEMAS |
| 模型输出坏 JSON 烧死整步 | router 参数容错解析 → __parse_error__ 工具反馈 |
| LLM 流式中途断连烧死整步 | stream 重试解除 started 限制 + 卡片级失败隔离 + 批级总时限 |
| LLM 调用挂死无 wall-clock 上限 | F3 批级 720s 超时 + 超时卡可见 |
| F5 步数耗尽出空报告 | max_steps 8→20 + 节奏纪律 + 空报告 blocked（不再静默出壳） |
| 卡片/stock 字段自造中文名（F4 读不到） | brief 强制 schema 字段名 |
| 打回无反馈通道 | 审批 decide 增 comment（服务/API/前端三层） |
| **P4 投资判断层**（2026-09-02 范本对齐重构，裁决：评级/仓位归 /decide 域；评分权重为行业可调参考值；PDF 抽取进范围） | F1.5 thesis 备忘录（带研究预算）；评分体系（Bottleneck/护城河/估值/成长，默认 40/25/20/15 可行业调整，分值强制绑证据 + rubric 锚定）；估值工具链（EV/Sales 等 + 预期分析）；投资委员会（四视角+空头+CIO 双强交叉）；人才/CEO 深挖 playbook；HKEXnews PDF 正文抽取（pypdf）；报告结构对齐范本（thesis 先行/淘汰逻辑/估值快照/证伪条件） | 报告质量上限 | 对照 docs/samples/ 四份范本逐项覆盖；评级/仓位/价格区间只进 /decide 决策卡 |
| **P5 UI** | provider 配置前端页；漏斗进度可视化 | 自服务 | 前端 vitest + 手测 |

## 5.1 P4 补充裁决（2026-09-02 grill）

| # | 议题 | 裁决 |
| --- | --- | --- |
| D1 | 评级/仓位/价格区间归属 | **只在 /decide 决策卡**（research/profile 报告不出；F5 报告保留排序+判断性评级依据+估值快照+证伪条件） |
| D2 | 评分权重 | 默认值 = 瓶颈 40/护城河 25/成长 20/估值 15（范本半导体逻辑）；**行业可调**——F1.5 thesis 备忘录可携带权重调整及理由；分值必须逐条绑证据（防「看起来科学的虚构」） |
| D3 | 港股 PDF | 进 P4：HKEXnews 披露全是 PDF，fetch_document 加 PDF 抽取（pypdf），否则港股估值永远缺净债务/股本一手数据 |

## 6. 风险与待验证

| 项 | 处理 |
| --- | --- |
| ~~HKEXnews 爬取可行性~~ | ✅ 2026-09-01 spike 一次通过：两步协议（prefix.do 解析 stockId → titleSearchServlet.do 查公告），**日期必须 YYYYMMDD 紧凑格式 + rowRange 必填**（否则静默空 200）；坑已写进 adapter docstring |
| GDELT 本机网络不可达 | 2026-09-01 实录：直连与代理均 SSL 握手超时（疑似被墙）。adapter 保留（网络环境差异），预检会标降级；新闻维度短期由 Exa 兼任，或后续评估 Bing/Polygon |
| akshare/东财港股快照本机不可用 | 2026-09-01 实录：requests 走 macOS 系统代理（env 变量清除无效，需 `NO_PROXY='*'`）；直连后东财 push2 接口仍断连。adapter 保留 + 探活如实报降级；港股净值短期可由 HKEXnews 披露 + 行情价推算，或付费上 FMP |
| Exa 对港股/中文源覆盖 | P2 实测（2228.HK 用例），不足则 Tavily/Brave 补位 |
| flash 模型提案质量 | 上线后按模型分桶观测拒绝率；过高则调 brief 复杂度，**不调** grounding 纪律 |
| dashscope `reasoning_effort` 实参名 | ✅ P1 实测通过（FINANCE_AGENT_NETWORK 冒烟：kimi-k3 接受 max 档，无 400） |
| 三 flash 并行成本失控 | worker 层预算放开≠无审计：每 run token 用量落事件，周报复核 |
