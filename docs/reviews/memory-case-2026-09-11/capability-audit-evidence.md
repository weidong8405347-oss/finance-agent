# Research / Profile 投资研究能力审查证据

审查日期：2026-09-11。范围为本机工作区当前代码、既有生产数据库、现有测试与纯函数复现；新存储行业现场研究由主任务独立运行，本文件不冒充其运行结果。未修改代码或生产数据库。仓库根目录没有 `.codegraph/`，按 AGENTS 指令跳过 CodeGraph。工作区原有未提交修改保留。

## 总体判断

项目具备值得保留的可追溯研究底座：问题计划、原文证据、数值语义与计算血缘、历史快照、冲突处理、结构化档案和失败提示均有实际实现。但“有工具/有页面”还不能推出“能稳定交付投资级研究”。目前最明显的断层是：投资必要问题没有全部成为硬性验收；多市场与周期行业建模不足；结构化产出过于依赖模型主动调用；研究报告图表仍是文字占位；历史错误数据没有因为写入门禁升级而自动失效。

对存储投资而言，系统最需要证明的是“能把 DRAM/NAND/HBM 的供需、量价、产品组合、成本、Capex、现金流、周期估值连起来，并在统一证券/币种/期间口径下比较可投资标的”，而非多生成几段行业叙事。

## 四维审查

| 维度 | 已有能力 | 核心不足 | 专业投资研究的可验收交付 |
|---|---|---|---|
| 专业度 | 双时态、来源原文、typed 观测、可重算、冲突显式化；研究充分度与基础校验分开 | 基础校验可接受无研究意义的正文；来源一手分类不覆盖公司 IR/DART；关键段落数字仅软检查；存量坏数据仍可被展示 | 投资结论逐条带事实/假设/推导/反证；买入结论必须经过财务、估值、预期、反证门禁；旧数据迁移与隔离 |
| 投资分析覆盖 | 商业模式、财务、竞争、风险、管理层、反证有配方；有行业漏斗、计算、委员会 | standard 不含估值/预期差问题；deep 的估值/预期差仍可未答但 sufficient；缺存储周期专用模型与稳定 consensus 数据源 | 统一截止日的多公司横比、至少一套可重算情景、估值敏感性、催化时间表、领先指标及失效条件 |
| 可视化 | 档案页有 ECharts、小倍数、产业链图、分层比较、时间线、证据抽屉 | 研究报告 chart_ref 只渲染成文字；跨公司比较工作台一次一个原始指标；币种/期间不可比时没有可执行的标准化路径 | 行业供需与价格周期、产品结构、现金流/Capex、同行估值、情景敏感性六类图；每图可回到数据与证据 |
| 公司深度 | 有分部、管理层、技术、假设等模板；Profile 有依赖图、prepare/commit、快照失效 | 通用模板不强制 DRAM/NAND/HBM 分部经济模型；5FY+8Q 只是文字目标；既有 BNTX 产出只沉淀收入，利润、现金与估值缺失 | 每公司先建历史数据底稿，再形成量价成本预测；研究更新需说明新事实改变哪个假设及估值 |

## 关键发现与代码证据

### P0-1：报告发布的“基础校验”不能保证报告有研究内容

既有生产最新 `ai-for-science` 报告状态为 validated/partial，正文只有“main_basis 需为列表、credibility 需为字典。修正后重提。”，是内部修复提示，并非研报。原样导出见 [历史报告第 9 行](audit-original-artifact-1e94edd47bee.md:9)。

当前代码仍存在相同的路径风险：自由 Markdown 降级成普通 paragraph（`research/artifacts.py:611`），验证器只要求至少有 block（`:334`），合成发布直接按“无 hard issue 且 sufficiency 非 blocked”升为 validated（`commands/steps.py:1411`）。降级解析函数注释称必须 draft，但发布分支没有落实这个条件。纯组件探针把上述内部提示提交给当前 `ArtifactValidator`，返回 `[]`。

建议：将“结构未提交”“内部工具错误/修复文本”“无实际回答”“所有内容为缺口”区分为运行产物与可阅读研究报告；成功发布至少需要一个投资相关问题的有效结论和证据，降级结果保留 draft。验收时直接复用上述真实坏样本，要求不能被标为可用报告。

### P0-2：投资必要问题没有成为充分度的硬性条件

`playbooks/research/recipes/general.yaml:111` 把预期差和估值放在 extended，且均为 medium。`research/plan.py:353` 的 standard 只加载 core；deep 才加载 extended。现有纯函数实测，普通 MU standard 计划为 8 题，不含估值与预期差。

`research/assessment.py:439` 在适用题覆盖达到 80% 时允许 sufficient；analysis-depth 与 model-reproducibility 只是统计。当前组件探针中，deep 12 题答完另外 10 题，仅估值和预期差未答，结果 sufficient（83.3%），即使无计算、无反证引用。这个探针有意绕过工具层，不表示伪造回答可以通过实测；它证明充分度层缺少“估值/预期/反证不可被覆盖率抵消”的投资特定门禁。

建议：区分“资料研究充分”与“支持投资决策充分”。用户问“谁更值得投资”时，将最新财务、当前价格和证券口径、前瞻假设、估值及反证设为必答；缺输入可以给观察名单，不能给无条件排序或“已完成投资研究”。

### P0-3：存量币种/单位错误仍在用户产物里，写入侧修复不等于数据修复

生产 BNTX 的 5 条收入观测均 `unit=USD`、`currency=EUR`，原文为欧元 millions。报告插值以 `unit` 输出（`research/artifacts.py:649`），因此正文错误标 USD；表格引用取归一化值，却沿用单元格的 `EUR M` 单位（`:702`），出现 `2869900000 EUR M`。见 [原报告第 17 行](audit-original-artifact-0f85988fa67c.md:17) 及 [第 22 行](audit-original-artifact-0f85988fa67c.md:22)。

当前写入侧已明确拦 `unit != currency`（`knowledge/metric_spec.py:276`），因此不能把历史错误简单归因于当前写入仍允许。但 `data/metrics.db` 与冻结报告仍保留上述错误，读侧与旧报告尚未统一失效/更正。

建议：对存量做只读质量扫描，再追加 correction/invalidations 与 superseding artifact；保留原快照历史，但当前默认视图显示更正版。表格单元格的数值、币种、scale 应来自同一 observation，渲染器做统一缩写，不能使用 LLM 自写单位尾缀。验收需确认美元、欧元、韩元、日元、百分比、千/百万/十亿以及区间都一致。

### P1-1：存储行业会落到通用配方，缺行业经济模型

实际配方目录只有 `biotech/general/industrial_equipment/industry`。输入 `Micron MU DRAM NAND HBM semiconductor 存储`，`select_recipe` 返回 general。通用 KPI 主要是 revenue/gross_margin/CFO/Capex/net_debt/dilution（`general.yaml:20`），没有存储生产与销售模型。

当前受控公式有同比、复合增长、利润率、FCF、净债务、EV、TTM、反向 DCF 和敏感性（`research/calculations.py:409`）；DCF 是收入 × 固定 EBIT/折旧/Capex 比率（`:237`）。这适合作为框架，但无法自然表示 DRAM 与 NAND 的量价背离、HBM 对传统 DRAM 产能挤出、良率爬坡、不同代际产品与周期谷底利润。

建议新增 memory 配方和模型：DRAM/NAND/HBM/企业级 SSD 分层；bit shipment、ASP、bit cost、wafer starts、yield、HBM capacity/qualification、库存天数、Capex、折旧、客户和产品组合。按“收入=bit 出货×ASP”与“毛利=收入−bit 出货×单位成本”建三情景，再桥接 Capex→FCF。跨周期以正常化利润/FCF、P/B–ROE 和重置成本进行交叉验证，避免只看峰值 P/E。

### P1-2：多市场与预期数据能力不对称，影响 MU/SKHY/SNDK/三星/铠侠横比

内建源集中于 SEC（`plugins/builtin.py:92`）、HKEX（`:110`）、web、Yahoo/Stooq 与美港供应商快照（`:147`）；没有 DART/EDINET 专用 adapter，也没有独立 consensus/revision 源。不能因此断言韩国企业完全不可研究：例如主任务已独立核验 SK hynix 的 Nasdaq ADS `SKHY`，有 SEC 路径；这里的缺口是原股、ADS、公司主体、币种与当地披露之间的桥接。

`research/assessment.py:54` 的一手披露域只有 SEC/HK/CN。组件探针：`investors.micron.com`、`news.skhynix.com`、`dart.fss.or.kr` 的 `web_fetch` 均返回 unknown。保守分类能防虚报一手，但会系统性低估合法发行人 IR/韩国披露。另 guidance 的 first_party 目前按 nature 判定（`:99`），应同步校验发行主体与官方文档。

建议：建立实体证券主数据（法人、交易所、ticker、CIK/当地代码、ADS ratio、生效日期、股数与货币）；公司 IR 域名登记基于可靠主体映射，不能仅凭域名字符串猜。同一截止日保留 actual/guidance/consensus/model 四层；没有 consensus 时，明确“与公司指引相比”，不要把新闻报道目标价当可复现一致预期。

### P1-3：跨公司比较工作台无法完成专业横比闭环

后端 `/knowledge/compare` 逐实体选择最新 observation（`api/dossier.py:280`），只在期间末、频率、币种、basis 完全一致时 comparable（`:294`）；不做财年日历化、FX 转换或共同可用期间选择。对不同财年、USD/KRW/JPY、GAAP/IFRS 的存储公司，通常只能提示不可比。

前端要求手输 `stock:...` 和 metric key（`pages/ComparePage.tsx:59`），一次比较一个指标；前端 API 契约还没有暴露后端 frequency/period_end（`features/dossier/api.ts:91`）。表格没有投资级多指标横比、正常化估值、排序假设、ADR 调整或区间情景。其强项是保守地不硬比较，不应把拒绝比较本身描述成已解决可比性。

建议：标准化任务前置，保留 reported 原值，同时生成单独的 FX/calendarized 计算观测；默认多公司矩阵（技术/产品暴露、收入、毛利、ROIC、FCF、Capex、净现金、正常化估值、前瞻假设），逐格显示口径与来源。不可标准化的指标明确排除，不强填。

### P1-4：档案可视化已实现，研究报告图表未实现

档案模块确实调用 `MetricChart/MetricSmallMultiples`（`features/dossier/modules.tsx:269`、`:338`），产业链、时间线、候选矩阵有实际 SVG/图表组件和护栏测试。不要把项目概括成“完全没有可视化”。

但 `ResearchReportPage.tsx:97` 只渲染 `artifact.markdown`，`research/artifacts.py:714` 把 chart_ref 转成“📈 图表 xxx；数据引用 xxx”的一行文字；BNTX [第 48 行](audit-original-artifact-0f85988fa67c.md:48) 是实物例子。`docs/profile-content-viz-upgrade.md:66` 也明确分部堆叠图、股价图未做；`:86` 明确预期引擎未做。

建议：报告页按 ReportDocument block 渲染，chart_ref 绑定冻结 series 与图表 registry；导出同时生成静态 SVG/PNG 与数据表，确保“在页面有图”与“交付报告有图”一致。存储案例至少交付供需/ASP趋势、产品结构、Capex-FCF、同行对比、估值敏感性、催化时间线六张有证据图，不以图表图标计数。

### P1-5：估值实验面板硬编码 USD，且没有传快照上下文

`features/dossier/modules.tsx:426` 的 preview 请求传 entity 和假设，却漏 `base_snapshot`；`:439` 硬编码 unit/currency 为 USD。后端仅在有 `base_snapshot` 时校验引用、namespace 和 as_of（`api/dossier.py:392`）。因此在韩国/日本公司或历史/eval 快照里使用该面板，会出现显示币种与当前快照上下文不一致的风险。

建议：绑定快照、货币、证券和输入引用；从最新有效数据预填 revenue/EV/净债务，所有缺失显示缺失；对手动美元情景显式标假设并要求汇率依据。以 KRW 股票和历史/eval 快照做端到端验收。

### P1-6：公司深度目前更像章节清单，缺少每章完成标准

通用配方财务质量要求“目标 5FY+8Q，或明确缺口”（`general.yaml:72`），而状态逻辑主要检查 KPI key 是否出现（`dossier/projector.py:692`），不检查序列长度、最新财报、三表勾稽、分部合计和收入/现金流预测完整性。旧 schema 的估值有效期为 180 天，peer 为 400 天（`knowledge/schema.py:29`），对高波动存储投资明显过宽；模块新鲜度有更细规则，但依然需要价格/指引/业绩事件触发更新。

既有 BNTX 是直接例子：5 条 typed 观测全部 revenue、只有两个年末期间；0/10 问题 answered、0 估值计算，最新快照 financial_quality/expectations/valuation_lab 均 missing，却首屏 investment_snapshot ready。报告诚实地披露缺口，这是优点；但“可投资但需验证”相对于其底稿仍过早。主库 profile_update_commits=0 只说明本次抽样未见真实整合提交，不证明整合实现失效。

建议每公司建立可审核底稿与问题完成标准：至少三年年度+八季（披露不全明确区别）；最近已公开业绩必须纳入；净利润-CFO-Capex-FCF 勾稽；收入产品结构；资本开支/折旧/库存/营运资本；管理层承诺—实际跟踪；核心客户和产品验证；核心 thesis 对估值的影响。仅缺口说明不计完成。

## 保留并加强的优势

1. `knowledge/metric_spec.py:235` 的语义门禁已经处理比例带币种、单位冲突、量表、主体和期间；这是避免金融数字灾难的必要基础。
2. `research/calculations.py:237` 正确区分 FCFF 与 CFO−Capex，并有 WACC/终值增长、缺净债务、N/M 等护栏及测试。
3. `dossier/consolidator.py:158` 的 prepare/commit、基线 hash 与依赖失效设计，比简单重写摘要更适合长期跟踪；需要真实样本证明增量可用。
4. `api/dossier.py:241` 对不同期间与币种拒绝硬比、`modules.tsx:354` 对缺 consensus 明示降级，比伪造完整度可靠。
5. `ResearchReportPage.tsx:78` 明确“通过基础校验不代表人工审计或结论一定正确”；应继续保留，并增加投资可用性分级。

## 建议验收表

| 优先级 | 工作包 | 存储案例验收标准 |
|---|---|---|
| P0 | 投资发布与历史质量门禁 | 上述两份历史坏样本不能作为当前可用报告；段落关键数字 100% 有观测/计算血缘；金额币种与 scale 零冲突 |
| P0 | 投资可用性判定 | 任一公司缺价格/股数或 ADS ratio/最新财务/前瞻假设/估值/反证时，禁止无条件买入排序；研究资料充分与投资判断充分分开 |
| P1 | Memory 配方和底稿 | MU、SK hynix、SNDK，并纳入三星/铠侠至少两家对照；8Q+3FY 关键财务；DRAM/NAND/HBM 按披露边界分开；未披露参数标 unknown |
| P1 | 周期情景模型 | 每公司 bear/base/bull 的量、价、成本、Capex 到 FCF 桥接；假设逐条有出处或标 model_estimate；敏感性可重算 |
| P1 | 证券与跨市场标准化 | 原股/ADS/法人正确映射；统一价格截止时点；FX 与财年日历化各有计算血缘；不能对齐则排除并解释 |
| P1 | 六图投研交付 | 六类图实际渲染、可导出；每个数据点可回证据；缺失不画成 0；财报数据与预测有不同样式 |
| P2 | 自动更新与成本可靠性 | 新财报后仅刷新受影响问题/指标/claims/估值；旧快照不变；连续三次真实运行记录完成率、关键缺口、时长、成本与补研次数 |

## 本次验证与附件

- 只读 SQL 摘要：主库 32 条观测、6 计划、20 claims、4 计算（均 ai-for-science 的 yoy_growth）、5 artifacts、25 snapshots；既有生产研究并不等于新存储案例实测。
- [抽样生产数据证据](audit-production-observations.json)；[当前组件探针结果](audit-component-probes.json)。探针未调用 LLM、未联网、未写生产库。
- 运行现有相关测试：`test_calculations.py`、`test_metric_semantics_gate.py`、`test_artifacts.py`、`test_profile_schema_gaps.py`、`test_profile_consolidator.py`，结果 **84 passed in 0.79s**。这支持工程契约，不支持投资分析已充分。
- 本子审查没有发起 research/profile、新增代码、重写快照、采购数据或修改用户设置。新存储案例的任务完成率、内容与画面，由主任务现场记录另附。
