Agent 通用设计框架

> 定位：这是一份领域无关的 agent 设计框架，从生产级「建模调优 agent」的完整设计过程中提炼。
> 使用方法：先读设计原则（为什么这样设计），再看架构框架（怎么组织），然后看评估体系（怎么证明有效），最后用「迁移指南」落地到你的领域。
> 当前落地目标：股票投资 agent。第 6 部分给出领域映射与投资特有的风险清单。

***
0. 一句话总纲

> Agent 的难点不在循环，在脚手架。
> 循环（loop）30 行代码就能跑通；真正决定一个 agent 好不好用的，是循环外面的脚手架——状态、记忆、工具、沙箱、扩展、评估、自改进。

这套框架回答一个核心问题：怎么设计一个 agent，让它既能稳定干活，又能持续变好，还不会在学习过程中骗自己。

***
第一部分 · 设计原则

十条原则，每条都是「做了会怎样」的因果陈述，不是口号。

1. Agent 是循环，Harness 是脚手架

LLM → reasoning model → agent → harness 是五层递进。Agent 是围绕模型的控制循环（observe → inspect → choose → act）；harness 是循环外围的脚手架（状态机、记忆、调度、恢复、评估）。90% 的工程量在 harness。

→ 推论：别把循环写复杂，把脚手架写扎实。

2. 稳定 Kernel + 可配置 Step Pipeline

loop Kernel 只定义生命周期、事件顺序、安全不变式，永不膨胀；一切优化（通用/专属/策略）都通过 Plugin 在固定 extension points 上注入，组合由配置（profile）决定。

→ 推论：新增优化 = 写 plugin 或改 plugin-config，默认不修改主干代码。一个系统的演进速度，取决于「加东西要动多少主干」。

3. 真相源与投影分离

append-only 事件日志（L0）是唯一真相源；一切视图——给模型的 prompt 上下文、给用户的 UI、给评估的报告——都是真相源的投影，可裁剪、可重建、不可反向污染真相源。

→ 推论：「模型可见 = 已记录」是铁律，反过来不成立（记录 ≠ 模型可见）。存储全量、消费剪裁。

4. Hook 必达 + Tool 弹性

必须发生的逻辑（诊断、审批、校验、日志）挂生命周期 hook——模型不可跳过；可自主选择的逻辑（用哪个策略、调哪个工具）做成 tool——模型按上下文决定调谁。

→ 推论：把「必须发生」交给模型自觉 = 把可靠性交给运气。

5. 单写者

共享的指针/状态（最优解、best-so-far、当前配置）只允许一个写入者。并发竞争在写入权层面消解，而不是靠锁。

→ 推论：并行子代理只上报候选，永不直接移动主线指针；合并由主线单写者原子完成。

6. Fail-closed

能力缺失时默认拒绝，而非降级放行。沙箱不可用即拒绝执行；评测平台做不到隔离即退化为「只出建议不执行」；capability manifest 缺字段 = 该能力不存在。

→ 推论：安全默认值的方向永远是「拒绝」。要放行，必须显式证明能力。

7. 权力分离（防作弊的地基）

改进者（Improver）、评估者（Evaluator）、晋升者（Promotion Gate）必须是三个独立权力主体：

Improver 可写候选、可读公开报告；
Evaluator 独立账号/容器运行私有评测，不向 Improver 暴露 case 级细节；
Promotion Gate 只验证签名 artifact，不运行候选代码、不允许 Improver 写入。

→ 推论：让 agent 同时改实现、改评测规则、改晋升标准 = 它一定会学会钻空子。完整性是硬门禁，不进加权总分。

8. 数字保护

数值只可截断，不可改写。语义可摘要，数字必须原样。任何摘要/压缩/展示环节的受保护字段（指标值、超参、round 号），必须与原文逐字一致，校验失败即弃用。

→ 推论：工具版本必须随每次实验入库，否则「换了实现结果不同」无法归因。

9. 证据绑定 + 晋升门槛

知识入库（长期记忆/方法论）必须满足三条硬规则：①每条结论绑证据（event id / 历史行引用）；②只存规则不存数字（具体数值留在结构化历史）；③晋升有门槛（同一模式重复 ≥N 次或触发关键事件才晋升，单次偶然不晋升）。

→ 推论：没有这三条，「知识库」会退化成散文摘要，无法查询、无法复现、无法信任。

10. 系统的品格由它拒绝的东西定义

明确「不做什么」和「做什么」一样重要。一个好系统敢于拒绝：不做它不擅长的事、不为了兼容而污染核心、不为了灵活而放弃边界。

→ 推论：写设计文档时，「不做清单」的每一条都要有理由。

***
第二部分 · 通用架构框架

2.1 分层架构总图

┌──────────────────────── UI / CLI ────────────────────────┐
│ 只读视图（状态面板/审批对话/报告）；命令进入 Harness       │
└────────────────────────────┬──────────────────────────────┘
                             │ commands
┌────────────────────────────▼──────────────────────────────┐
│ Harness（调度层）                                          │
│ 状态机 / turn+iteration / Inbox / 审批 / 取消 / 恢复       │
│ best 单写者 / 外部 signal / EventStream                    │
└───────┬────────────┬──────────────┬──────────────┬────────┘
        │            │              │              │
        ▼            ▼              ▼              ▼
   AgentLoop    PluginRuntime   Memory         ExecutorGateway
   稳定Kernel   Step Pipeline   L0/L1/L2/L3    执行环境 Adapter
        │        ├─ToolRuntime      │           (本地/远程/沙箱)
        │        └─Hook/Prompt/     │
        │           Policy          │
        └────────────┴── canonical events ────────┘
                             │
                 ┌───────────▼───────────┐
                 │ EventStore（真相源）    │  append-only + 快照
                 └───────────┬───────────┘
                             │ projection
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
   UI 投影              报告投影              评估投影
   (可观测)             (可读交付)            (效果证明)

Module 职责速查：

Module	Interface	隐藏的实现	深度来源
AgentLoop（Kernel）	messages、step descriptor、模型、signal、event sink	LLM 流、tool 往返、停止判定、固定 extension point 顺序	Kernel 稳定，优化逻辑不进主干
PluginRuntime	compile(profile) / execute(step, input) / dispose	插件发现、适用性匹配、依赖排序、超时、错误隔离、贡献合并	step 扩展复杂度集中且可消融
Harness	submit/steer/cancel/resume/status	状态迁移、iteration、审批、signal 路由	全部调度复杂度集中
ExecutorGateway	submit/status/cancel；结构化 signal	远程调用、重试、轮询 fallback、隔离执行	真实 seam（至少 2 个 adapter）
EventStore	append/read/snapshot	JSONL/SQLite、sequence、原子写	真相源与存储细节隔离
DomainHistory（L2）	commitIteration/queryBest/queryRelevant	结构化表、索引、版本迁移	领域历史可查询性集中
SelfImprovementController	start/pause/resume/stop/status	诊断、候选变异、隔离 workspace、评估循环、回滚	自我迭代复杂度不污染业务 loop
	2.2 时间单位与双状态轴

时间单位（四级，逐级嵌套）：

step：一次模型调用 + 它触发的一批工具调用；
turn：从一次外部输入/结果回流，到 agent 产出下一次行动方案的一组 step；
iteration：一个 turn + 一次领域执行（如训练/回测）+ 结果回流 + commit；
task/run：从任务输入到最终交付的完整运行。

两个正交的状态轴必须分离：

HarnessStatus（运行状态）：idle / running / waiting_external / maintenance / blocked / error / completed / cancelled
DomainStage（业务阶段）：由领域定义（如 explore → exploit → finalize）

混用这两个轴是常见的架构病——业务阶段流转不该影响运行状态机的判断。

2.3 插件体系

Plugin 是组合/分发单位；hook、tool、prompt、policy、validator、evaluator 是 Plugin 的贡献类型。

每个 Plugin 有 manifest（id、semver、apiVersion、engineRange、scope、appliesTo、hooks、dependencies、before/after、conflicts、capabilities、configSchema、failurePolicy）。

关键机制：

Step 描述符：{ stepId, kind, role, domainStage, iteration, turn, step }——插件按 kind/role/stage 匹配；
Extension points（冻结的生命周期点）：prepare / beforeModel / afterModel / beforeTools / afterTools / evaluate / error / finalize，外加 harness 级的 afterSignal / beforeCommit / afterCommit；
Hook typed contract：每个 extension point 声明 input / contribution / sideEffects / mayTerminate——这是「自改进自动变异 plugin」的类型前提；
组合规则：依赖 DAG + before/after + 稳定 tie-breaker（禁止依赖文件加载顺序）；多插件贡献用显式 reducer（禁止 last-write-wins）；
PluginGraph 冻结：profile 编译成 canonical JSON + hash，写入 run/benchmark manifest；运行中冻结，变更必须新开 run；
预算常量：每 step 插件数上限 + 累计延迟预算，编译期 fail-fast 防膨胀。

策略归属裁决（重要）：领域的可调策略（搜索策略、预算分配、路由阈值）整体归入 builtin plugin，可调参数进 plugin-config。不要形成「既有 policy 目录又有 plugin」的双家——否则这些高频调优对象会退化为「改主干」，违背 plugin-first 目标。

2.4 记忆分层

层	存什么	格式	更新频率	生命周期
L0 原始事件流	全部 step/工具/外部执行的原始事件	append-only JSONL/事件表	每事件即时	永久
L1 会话级摘要	当前会话的工作记忆（蒸馏后的关键信息）	半结构化文本	每 step/turn 蒸馏	会话级
L2 结构化历史	每次迭代/尝试的结构化记录（参数、结果、增量、血缘）	结构化表（含 source 扩展位）	每 iteration commit	跨会话、全量保留
L3 元知识	跨任务方法论（规则 + 证据引用）	结构化规则（不带具体数字）	晋升制（≥N 重复）	跨任务、最长寿
	恢复顺序铁律：先状态后记忆。 状态快照决定「现在能/需要继续做什么」（恢复游标、运行中的 job、best 锚点），记忆决定「用什么知识做」——顺序错了会重复提交任务或丢上下文。

2.5 执行边界

Agent 本地零不可信执行。所有不可信代码（模型生成的、第三方的）都在隔离执行环境运行；Agent 本地只做「读 + 思考 + 生成方案文本」。

Agent 本地：读数据画像 / 查知识库 / 思考 / 生成方案      → 无沙箱需求
执行环境：跑不可信代码 / 领域执行 / 评估               → 隔离 + 资源闸门

执行环境按可信度选档（container → gVisor → microVM），能力清单（capability manifest）驱动 agent 决定下发哪些工具——平台能力不够，工具不下发。

2.6 自改进循环（双循环分离）

Task Loop（业务环）        优化领域对象：方案 → 执行 → 信号 → 诊断 → 下一轮
Self-Improvement Loop（元环）优化 Agent 自身：评估 → 诊断 → 改候选 → 隔离验证 → 晋升/回滚

两者共享真相源与评估 artifact，但状态机、权限、预算、停止条件独立。自我改进不得在正在运行的任务上热修改 prompt/plugin/code——每个候选形成独立不可变 revision，在新 run/隔离 workspace 中评估。

自主模式三档（权限计算：effective = min(prompt 请求, 任务 policy, 部署 policy)，prompt 只能降权不能提权）：

模式	介入	适用
guided	每次关键变更确认	初期、未知变更
milestone	只在扩权/高预算/私有评测/晋升时确认	默认生产
autonomous	预授权范围内无人值守；超范围自动停止/降级	夜间迭代
	***
第三部分 · 评估体系

3.1 三条证据链（不可互相替代）

证据链	回答	不能证明
单元/契约测试	Module 按 Interface 正确工作	Agent 真的找到更好方案
兼容链（UI/投影）	产物能被正确展示	优化质量优于 baseline
Benchmark	同任务同预算下效果/效率/稳定性提升	每个 Module 无 bug
	> 任何「效果提升」结论必须附 Benchmark comparison artifact；只说测试全绿 ≠ 效果提升。

3.2 分级测试

层	内容	触发	目的
B0 确定性合成基准	已知最优的领域任务	每个 PR	快速定位回归（秒~分钟）
B1 轻量真实链	真实执行链、小预算	改核心后	证明真实链路能跑通（分钟级）
B2 正式效果 benchmark	代表性真实场景、完整预算、candidate vs baseline 配对	release / 关键改动	证明效果、收敛、稳定、成本（小时~天）
B3 兼容链	产物 → 投影 → UI 同构	改行为后	证明产出与消费端兼容
	B0/B1 是开发反馈环；B2 是效果门禁；B3 是协议门禁。B1 成功不能替代 B2。

3.3 公平对比协议

candidate 与 baseline 必须锁定：数据版本/切分/seed 集/任务规格/模型 provider+id/预算/超时/capability manifest/工具版本。

配对 seed：同一 seed 下 baseline 与 candidate 一一比较；
正式评估每场景 n≥5，报告 mean/median/std/p10 与 paired delta；
优先 paired bootstrap 或 Wilcoxon；多指标比较控制多重检验；
authoritative result 必须由权威 id 读取，禁止用最后一行或 UI 推断；
baseline 锁定 commit、prompt version、工具版本、模型、manifest 版本。

3.4 迁移性验证（「类似场景可用」的唯一证据形式）

单一真实场景证明不了可迁移。B2 必须定义任务族矩阵并满足迁移指标：

矩阵维度（示例）：

维度	取值	目的
任务类型	领域内不同子类	覆盖产品定位范围
数据规模	小/中/大	验证预算与稳定性
数据特征	均衡/不均衡、平稳/漂移	验证鲁棒性
	迁移指标：

指标	通过标准
跨场景提升一致性	N 个场景中显著为正的比例 ≥ 80%，且关键场景必须为正
跨场景方差	paired uplift 标准差不超过阈值的均值倍数（写入 manifest）
Leave-one-scenario-out 泛化差	N-1 训策略、留 1 验证，留出场景 uplift ≥ 0
分布偏移稳定性	时间偏移/噪声注入下退化幅度在容差内
	> 只有满足迁移指标，才允许宣称「类似场景可用」。B0 合成 + 单场景 B2 都不足以支撑该结论。

3.5 防作弊（自闭环的生命线）

威胁模型（Improver 可能怎么「提高分数」）：读/记忆私有 case 与 Judge、按 fixture id 硬编码、修改评测规则或 baseline、降低预算、吞掉失败、伪造 metric、优化 proxy 而损害 authoritative、反复查询 holdout 过拟合、改日志让 UI「看起来更好」。

防御结构：

权力分离（见原则 7）；
评测集分层：public dev（可反馈）/ shadow（有限聚合）/ private promotion holdout（冻结后有限次、不返回 case 细节）/ rotating audit（周期轮换）；
受保护路径 + diff policy：评测、baseline、integrity、authority、投影代码禁止自动修改；新增依赖/联网默认人工批准；
Anti-gaming 检查：contamination 扫描、special-case 检测、协议一致性、可复现、budget 对账、perturbation、generalization gap、独立 evaluator、canary/honeypot；
完整性是硬约束：发现作弊/污染直接 quarantine，业务分数再高也不晋升。

3.6 指标体系

维度	指标
最终效果	authoritative score、相对 baseline uplift、normalized gap、约束满足率
Anytime 效果	cost-to-threshold（主）、best-so-far AUC（辅）、budget-normalized gain（辅）、time-to-threshold
探索质量	unique config ratio、参数覆盖率、重复率、invalid proposal rate
稳定性	completion rate、std/p10、failure recovery rate、跨 seed 方差
成本	LLM tokens、执行成本、wall time
系统健康	tool 成功率、重复 job、resume 成功率、协议通过率
插件效果	attach 成功率、per-step 成功/失败、边际效果、交互效应、额外成本
自改进	cycle success、accepted rate、attempts-to-green、false promotion（目标 0）、rollback rate
可读性	step 报告完整率、归因覆盖率、知识证据绑定率
	***
第四部分 · 可读性与可观测

4.1 每 step 可读记录（StepReport）

每个 step 必须落一条结构化可读记录，让人 10 秒读懂「这步干了什么、干得怎么样」：

type StepReport = {
  stepId: string;
  goal: string;             // 本步目标
  evidenceUsed: string[];   // 引用的证据 id
  decision: string;         // 关键决定
  toolsCalled: string[];    // 实际调用
  outcome: "ok" | "blocked" | "failed" | "skipped";
  cost: { tokens: number; durationMs: number; execCost?: number };
  qualityFlags: string[];   // "low-confidence" / "fallback-used" / "budget-blocked"
};

原文对话记录「想了什么」，StepReport 回答「这步做得对不对」——两者互补。

4.2 统一归因（StepAttribution）

type StepAttribution = {
  stepId: string;
  failureCategory: string;      // 实现错/优化败/资源瓶颈/数据问题/未知
  rootCause: string;            // 一句话根因
  affectedStep: string;
  pluginId?: string;
  evidenceRefs: string[];       // 证据引用
  recoverable: boolean;
  suggestedNextAction: string;  // 下一步建议
};

correlationId 只给连线能力，不给归因结论。没有统一归因 schema，「哪类 step 最常出问题」就无法统计。

4.3 知识沉淀硬规则

见原则 9。知识库的价值 = 可查询 + 可复现 + 可信任，三者都依赖硬规则，而非模型自觉。

4.4 UI 是投影，不拥有状态

UI 读到的每个字段都必须能回指真相源（L0 event / L2 行）。UI 一致性用快照测试守护——面板字段值 vs 真相源快照做断言，防止「显示与真值脱节」。

***
第五部分 · 设计决策速查表

决策点	选择	借自	拒绝
loop 内核	纯库 + 稳定 extension points	pi（纯函数 loop）、dsh（waterfall）	codex 巨型 core
真相源	append-only event log	dsh SessionEvent	就地改 messages 数组
插件	manifest + typed hook contract + profile + graph hash	dsh 可逆注册、codex 版本信任、pi 目录发现	全局 class registry
沙箱	agent 零执行 + 执行下沉 + capability manifest fail-closed	codex 分级沙箱、dsh fail-closed seam	pi 裸跑
调度	双状态轴 + 外部 signal + 幂等恢复	harness Request/Accept、herdr resume/dedupe	状态与业务阶段混用
记忆	L0-L3 分层 + 证据绑定 + 晋升门槛	（提炼自课程设计）	全文摘要当知识库
评估	三条证据链 + 分级 + 配对对比 + 迁移矩阵 + 防作弊	AlgoBenchmark + 研究界 anti-gaming 实践	单一分数、单场景下结论
自改进	双循环分离 + 三档自主 + 权力分离	METR/研究界 evaluation integrity	选手兼任裁判
	***
第六部分 · 迁移指南：股票投资 Agent

> 把上面的通用框架映射到股票投资领域。重点是第 6.2 节——投资领域有一组别处没有的风险，迁移时最容易翻车。

6.1 领域映射表

通用概念	股票投资 agent 落地
任务目标	策略研究/调优：选股、择时、仓位、风控参数
领域执行（iteration 的「训练」）	回测 job（历史数据回放）/ 模拟盘 job / 实盘 job
领域信号（回流）	回测结果：收益、夏普、最大回撤、胜率、换手率、各行业暴露
领域指标（authoritative result）	风险调整后收益（夏普/卡玛/索提诺）、最大回撤、超额收益
候选方案（plugin 产出）	策略配置：因子组合、权重、再平衡周期、止损规则、仓位规则
数据画像（stable prefix 输入）	行情数据概况、因子库 schema、可用数据源清单
诊断 hook（必达）	回撤诊断、归因分析（收益分解到因子/行业/择时）、交易成本分析
知识库（L3）	「某类因子在某类行情下失效」「高换手策略对滑点敏感」等投资方法论
ExecutorGateway	回测引擎 adapter（如 backtrader/qlib/自研）+ 模拟盘 + 实盘交易接口
沙箱	回测/策略代码在隔离环境跑；实盘下单是最高危操作（独立审批）
	6.2 投资特有的关键风险（迁移时最要小心的坑）

通用框架的每条原则在投资领域都有一个「对应的高危版本」，这些不是可选项，是生死线：

通用原则	投资领域的对应风险	强制对策
真相源 + 投影分离	未来数据泄漏（lookahead bias）：策略「看到」了当时的未来数据，回测收益虚高	回测引擎强制「按时间点可见性」：第 t 天的决策只能用 ≤ t 的数据；这是 ExecutorGateway 的不可绕过的硬约束，不是 plugin 可选
评测集完整性	幸存者偏差（survivorship bias）：股票池只含活下来的公司	数据源必须含退市/摘牌股票的历史；基准测试数据审计第一条
私有评测 + 查询预算	回测过拟合：反复调参直到回测曲线漂亮	样本外（out-of-sample）区间 = private holdout，只允许冻结策略后有限次评估；walk-forward 验证作为 B2 的一部分
数字保护	成本假设造假：忽略滑点/手续费/冲击成本，收益虚高	交易成本模型是强制 hook（不可关闭），且参数进 manifest 审计
fail-closed	实盘风险：策略 bug 直接造成真实资金损失	实盘下单 = 最高危工具，默认拒绝，必须显式人工审批 + 资金上限 + 熔断；回测→模拟盘→实盘有不可跳过的晋升闸
证据绑定	事后归因幻觉：收益被归因到错误的原因	每笔交易/每个决策必须绑证据（当时的信号、数据、理由），归因分析只消费这些绑定
双状态轴	市场环境漂移：策略在牛市验证、在熊市失效	DomainStage 加入市场状态维度（趋势/震荡/波动率区间），评估按市场状态分层报告
	6.3 投资 agent 的具体落点

Step 类型（StepDescriptor.kind 的领域实例）：

data-profile      数据画像与质量审计（含幸存者偏差检查）
hypothesis        策略假设生成
factor-research   因子研究/检验
strategy-design   策略结构与参数设计
backtest-submit   回测提交
backtest-diagnose 回测诊断（回撤归因、成本分析）
risk-review       风险评审（高危 hook，必达）
deploy-decision   实盘/模拟盘决策（最高审批级）

工具集（tool，模型可选）：

query_market_data        查询行情/因子数据（只读）
run_backtest             提交回测 job（需审批，花算力）
analyze_drawdown         回撤归因诊断（只读）
analyze_factor_ic        因子有效性检验（只读）
compare_strategies       多策略对拍（需审批）
submit_paper_trade       模拟盘下单（高危，需审批）
submit_live_trade        实盘下单（最高危，默认拒 + 人工审批 + 熔断）

评估指标（Benchmark 指标体系的投资实例）：

效果：夏普/卡玛比率、最大回撤、超额收益、信息比率
效率：cost-to-threshold（达到目标夏普的回测次数/算力）
稳定性：分年度/分市场状态的收益分布、参数敏感性
成本：回测算力、数据查询成本、wall time
健康：未来数据泄漏检测通过率、幸存者偏差审计通过率

Benchmark 分层映射：

B0：合成行情数据（已知最优策略）——验证 loop/工具/记忆
B1：小区间真实回测（note=test 快速档）——验证真实执行链
B2：完整历史 + walk-forward + 样本外 + 多市场状态——效果门禁
B3：交易链路兼容验证（回测引擎/下单接口/Dashboard 展示）

6.4 实施顺序建议

第 1 周：真相源（回测事件 append-only）+ 投影 + 单策略串行 loop + 未来数据泄漏硬约束
第 2 周：插件管线 + 回撤诊断 hook + 交易成本强制 hook + B0 合成行情基准
第 3 周：公平回测对比协议 + 样本外 holdout + walk-forward + 风险评审 hook
第 4 周：B1 真实回测 + Dashboard 兼容 + 自改进最小闭环（guided 模式）

实盘是最后一步，且有不可跳过的晋升闸：回测（B2 通过）→ 模拟盘（运行 N 周）→ 小资金实盘 → 扩大。每一级晋升都走 Promotion Gate，agent 无权自动跨越。

***
附录 · 使用本文档的方式

新领域开球：先读第一部分原则 + 6.1 映射表，把「通用概念 → 你的领域」一一对应写清楚，再谈架构；
评审设计文档：用第三部分评估体系做 checklist——尤其问「私有评测/权力分离做了吗」「迁移指标定义了吗」；
防翻车：先读 6.2 的领域风险清单，确认每条都有「不可绕过的硬约束」，再写第一行代码。

> 框架是通用的，但每个领域都有自己的「未来数据泄漏」——找到你这个领域的那个「作弊捷径」，把它焊死，比任何精巧设计都重要。
