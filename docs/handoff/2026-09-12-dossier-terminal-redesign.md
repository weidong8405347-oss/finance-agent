# Knowledge 档案页大改版交接（2026-09-12）

> 依据：`docs/finance_agent_final_dossier_optimization_plan.md`（Research Result Viewer → AI-native Equity Research Terminal）。
> 范围：前端展示层整体重构（`frontend/`），**零后端改动**——所需数据快照 API 已全部提供。
> 验证：90/90 前端测试通过；`tsc -b` 干净；`build` + `build:export` 成功；离线导出 HTML 用真实快照回归验证（撕页 + 引用抽屉在 file:// 下正常）。

## 已完成的方案落地

| 方案条目 | 落地 |
| --- | --- |
| §2 L0-L3 渐进披露 | Overview 撕页（L0/L1）→ 深读章节（L2）→ 研究与来源（L3 审计区） |
| §3 Investor/Audit 双模式 | 头部「审计视图」开关（`?view=audit` 深链 + localStorage 记忆）；投资者视图 0 个运行时字段（快照/namespace/run/配方/as_of 全部只进审计） |
| §6-§8 Overview 重做 | `overview.tsx`：Investment View（15px/1.85、≤76ch）、Key Metrics（26px tabular-nums、缺口如实标注）、Why Now、Key Bottleneck 漏斗、Value Capture、Best Public Expressions（≤5 + 查看全部）、Catalysts（≤3）、Thesis Breakers（≤3）、Open Questions、What Changed、Key Debate、核心依据 |
| §9 密度硬规则 | KPI ≤6 / 公司 ≤5 / 催化 ≤3 / 反证 ≤3 / 头部徽标 ≤3；正文区 ≤2 列；长文本默认折叠（LongText maxPx） |
| §10 删除/后置 | Research Objective/问题进展/检索预算/raw ID/limitations 全出首屏；审计视图在 Overview 底部渲染 `AuditSummaryPanel`（目标/进展/可信度分层/限制） |
| §11 What Changed | 「本轮进展」→ ↑/!/+ 语义图标 + 模块标签 + 折叠（`classifyChange` 纯函数） |
| §13-§15 Thesis 对象 | `thesis.tsx`：论点卡带 事实/推论/假设/分析 分层 chip、支持/反证计数条、未决条目、[n] 引用；superseded 论点折叠保留审计轨迹 |
| §16/§17 Evidence 后置 | 正文 raw `ev-*`/`obs-*`/`claim-*` 全部消失 → `[n]` 编号引用（页面级稳定编号 `buildCitationIndex`）；点击开 SourceDrawer（原文/出处/PIT 分级），raw ID 只在抽屉底部 |
| §48.3 | KPI 卡与正文一律 [n]，不再出现 `ev-vc-futures` 之类 |
| §19 候选矩阵 | 默认 Top 8（tier 序）+「查看全部 N 家」；阶段中文化；引用 [n] 化；13px 表格 |
| §21/§22 产业链 | 节点/层名去重；**瓶颈红标可分辨规则**（全部瓶颈=没有瓶颈，红色不再失效）；价值流文本内嵌 ID → [n] 芯片 |
| §38-§43 视觉系统 | `tailwind.config.js` 设计令牌（ink/line/paper/accent/pos/warn/risk）+ `index.css` 三种 surface（`dos-card`/`dos-panel`）；90% 中性色，颜色只表达语义；1px 边框、8px 圆角、无重阴影 |
| §40 Typography | 正文 14-15px、表格 13px、元信息 12px；核心信息无 10px；金融数字 `tabular-nums` |
| §50 Mobile | Hero 单列堆叠、顶栏横向滚动、KPI 2 列、抽屉全屏（390px 视口截图验证） |

## 文件清单

- 新增：`frontend/src/features/dossier/{overview.tsx, thesis.tsx, citations.tsx}` + `__tests__/overview.test.ts`（21 个纯函数用例）
- 重写：`pages/StockDossierPage.tsx`（Hero/双模式/分组导航）、`features/dossier/components.tsx`（审计面板/抽屉/补研/导出）
- 改造：`modules.tsx`（view 传递、EvidenceChips 双模式、候选池折叠、全量版式）、`viz.tsx`（瓶颈可分辨规则）、`KnowledgePage.tsx`（令牌化）、`App.tsx`（顶栏滚动）、`tailwind.config.js` / `index.css`（令牌）
- 移除：`ThesisPanel`/`KeyMetricBar`/`ClaimCard`/`RefChip`（被 overview/thesis/citations 取代）

## 诚实性纪律（继承现有红线，未放松）

- 缺口如实显示（KPI 缺口行、模块 missing），不以零/旧估计补位；
- 质量圆点只映射服务端 `credibility.level`（sufficient=4/partial=2/blocked=1），不造分数；
- 图表仍只画 typed 观测；混单位/不可比 → 只给表；
- 文本剥离 raw ID 是纯展示层变换（`splitTextRefs` 保留 refs 进引用芯片），不改数据；
- 时间旅行/历史视图/新版本提醒/补研/冲突裁决/导出行为全部保留（审计视图可见可操作）。

## 后续（方案 P1+，本次未做，需后端数据模型）

- Investment Object Schema 后端化（§12/§34）、Expectations Layer（§25/§26：consensus/revision 数据接入）、
  What Changed Engine（§32：快照 diff 驱动，当前用 summary.key_changes 表达）、
  候选四象限散点（§20：需要可比的 validation/moat 坐标数据）、Profit Pool Sankey（§22：需要定量价值流证据）。
- 行业 ExecutiveSummary 的 stage/why_now/value_capture/thesis_breakers 字段当前数据为空——补研配方应补这些产出，Overview 已具备渲染能力（有数据即显示）。
