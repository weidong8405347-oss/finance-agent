# Knowledge 档案升级实施状态对照

> 对照 [knowledge-dossier-research-redesign.md](knowledge-dossier-research-redesign.md) v1.0 的落地清单。
> 更新：2026-09-07 · 分支 `feat/knowledge-dossier-research-upgrade`。
> 状态口径：**已落地** = 有代码与测试；**部分** = 契约已落地但依赖数据/后续阶段；**未做** = 本期未实施（如实标注，不宣称 ready）。

## 里程碑完成度

| 阶段 | 设计要求 | 状态 |
| --- | --- | --- |
| M0 契约和样本 | Dossier/Observation/Claim/Plan schema、对照样本、迁移 dry-run | **已落地**（样本见 `docs/samples/dossier/`，dry-run 见 `scripts/migrate_dossier.py inventory`，27 生产实体盘点通过） |
| M1 可阅读档案闭环 | 兼容 projector、typed 读取/换算血缘、裁决时态投影、snapshot API、列表/详情、来源抽屉、基础财务图 | **已落地**（真实 BE 数据冒烟通过；旧 KB → 新页面可读、无 archive 也可用） |
| M2 深研闭环 | ResearchPlan、问题终止、typed 写入、CalculationRun、分章 ReportDocument、验证、partial 发布 | **已落地**（E2E 集成测试：`/research --depth=targeted` → 计划 → 观测/论断 → 评估 sufficient → 产物 validated → report.md 同源 → 快照发布 + 前后 diff） |
| M3 预期与行业增强 | 指引 vs 实际、Reverse DCF、敏感性、同业、首批行业模板、管理层兑现 | **部分**：Reverse DCF（FCFF 反向求解 + 二维敏感性 + 假设实验面板/情景保存）与首批配方（通用/工业设备/生物科技/行业）已落地；**guidance/consensus 数据采集与管理层兑现时间线依赖数据源接入**，模块按契约正确降级（missing + 原因），未以空图宣称完成 |
| M4 迁移及稳定 | 历史裁决批量回填、影子迁移、HTML 导出、性能/可访问性/恢复/回滚 | **部分**：影子迁移与对账 manifest（27/27 成功）、JSON/Markdown 导出、幂等发布/断点恢复已落地；**历史裁决批量回填与 HTML 导出未做**（HTML 待组件契约稳定后加入，设计 §11.4 即为第一期之后） |

## 设计章节 → 代码落点

| 设计章节 | 落点 | 状态 |
| --- | --- | --- |
| §6.1 数据对象（8 张逻辑表） | `knowledge/metric_store.py`（source_documents / metric_observations / calculation_runs / research_plans / research_claims / research_artifacts / dossier_snapshots / conflict_resolutions，独立 `data/metrics.db`） | 已落地 |
| §6.2 核心数值类型（判别联合） | `knowledge/metrics.py`（reported/calculated/guidance/consensus/model_estimate 各自强制来源义务；TTM 仅流量指标；value 十进制字符串，缺失≠0） | 已落地 |
| §6.2 标准化「保留原文+显式转换」 | `knowledge/normalization.py`（版本化公式注册表、`recompute_lineage` 逐步重算、逐叶数值校验堵嵌套/字符串漏检、亿=10^8 等中英规模词、年份歧义消解） | 已落地 |
| §6.4 期间/单位/口径规则 | 语义键版本化、重述竞争版本、裁决时态化（`resolved_at`，历史不借用未来裁决）、`ttm_from_observations` 连续性/口径门禁 | 已落地 |
| §6.5 事件投影 | `metric/asserted`（完整 payload）等新事件（`eventstore/events.py`）；写入 = 索引 + 事件；崩溃恢复依赖幂等（semantic_hash / input_hash / data_hash） | 已落地（「先事件后投影 + offset 记录」的完整事件溯源重建未做，当前为双写幂等） |
| §6.6 快照一致性 | `DossierContext` 冻结、live 首开固定服务端时刻、live 身份哈希不含 as_of（数据不变复用冻结快照）、三时间区分、`kb_snapshot_id` 兼容保留 | 已落地 |
| §7.1-7.3 研究模式/流程/问题结构 | `research/plan.py`（四模式预算表、问题状态机、targeted 确定性 id）；`commands/registry.py` 解析 `--depth/--focus/--refresh` 进 manifest | 已落地 |
| §7.4-7.5 深度配方/行业模板 | `playbooks/research/recipes/{general,industrial_equipment,biotech,industry}.yaml`；配方识别给来源、不确定回落通用 | 已落地 |
| §7.6 完成与质量门禁 | `research/assessment.py`（integrity 硬门禁代码运行、覆盖率 unavailable 不计已回答、冲突未裁决不出无条件结论、verdict 三态）；rubric 仅 advisory 不进门禁 | 已落地 |
| §7.7 预算并发恢复 | 计划预算接管轮次（`loop._effective_budget`）；typed 产出计入进展（防 stalled 误判）；问题级 checkpoint（问题状态存计划 payload） | 已落地（wall-clock 上限与 per-provider 限速未单独实施，检索预算经 max_rounds×max_steps 间接约束） |
| §7.8 报告产物 | `research/artifacts.py`（固定 block 类型、`{{metric:id}}` 插值、`[ev-xxx]` 未知 id 硬失败、report.md/概览/摘要同源渲染） | 已落地 |
| §8.1 计算服务 | `research/calculations.py`（input_refs 解析与漂移拒绝、公式注册表 12 个、Decimal、N/M 语义、幂等 input_hash） | 已落地（ROIC 等待口径明确后加，符合设计） |
| §8.2 预期与管理层兑现 | `guidance_delta` 公式（区间落点 + 中点标注）与 Expectations 模块契约 | 部分（兑现时间线/股价反应窗口依赖数据源，未做） |
| §8.3 Reverse DCF | FCFF 模型、有界二分、求解区间/残差/收敛状态、WACC≤终值增速禁用、fcf_margin 简化标注、二维敏感性 | 已落地 |
| §8.4-8.5 /decide 边界与滑块 | 预览不写事实、情景保存=模型 artifact（assumption_hash 409 防慢响应覆盖）、页面明示「假设实验/归 /decide」 | 已落地（前端本地预览模式未做，全部走后端确定性计算） |
| §9 数据源策略 | EDGAR/HKEX 等沿用现有 adapter；本次**未新增数据源**（设计明示不为设计文档安装依赖/购买数据源） | 未做（按计划） |
| §10.1 API v2 | `api/dossier.py` 全表（entities/dossier/snapshot/modules/evidence/documents/series/compare/artifacts/plans/changes/research requests 幂等/valuations preview+scenarios/exports+jobs；404/409/422/200+reasons 语义） | 已落地（`GET /dossiers/{sid}/documents/{did}` 依赖文档目录数据，契约已就绪；202 快照构建 job 未做——当前投影为同步毫秒级） |
| §10.2 前端结构 | `app/route.ts`、`features/dossier/{types,api,charts,components,modules,legacy}`、`pages/{Knowledge,StockDossier,ResearchReport,Compare}Page` | 已落地 |
| §10.3 SSE 补研闭环 | 问题/评估/产物/快照事件桥接进 Sessions 流（`runner._BRIDGE_TYPES` + 摘要）；补研幂等 key | 已落地（dossier 页的实时 published 推送未做，用「刷新（检查新快照）」按钮替代；SSE cursor 去重复用现有机制） |
| §11.1 迁移 | `scripts/migrate_dossier.py`（inventory dry-run / shadow 对账 / apply-typed 保守映射：显式币种+日期才转、规模词不猜量级、幂等 checkpoint、迁移值标 PIT-B） | 已落地（旧报告 [ev-id] 批量重链与 thesis→claim 批量迁移未做，读侧已兼容 legacy_analysis 投影） |
| §11.3 发布与回滚 | 新链路装配缺省安全：`StepDeps.metrics=None` 时旧管线行为不变（等价 `typed_metrics_enabled=false`）；`research_plan_enabled` 开关已有；additive schema、回滚不删新表 | 部分（`dossier_ui_enabled` 前端灰度开关未做——新页面默认可用，旧「数据与审计」与 HTML 存档入口保留即回滚路径） |
| §11.4 导出 | JSON + Markdown（绑定 data_hash/as_of/情景差异注明；历史导出默认不附加新情景） | 已落地（HTML/PDF 按计划后置） |
| §13.1 正确性测试 | 数值语义/财务期间/来源/历史与评估/模型/研究/发布恢复/前端/兼容迁移 9 组 | 已落地（135 个新用例；浏览器 E2E 与截图验收未做，见下） |
| §13.2 对照样本评估 | 冻结样本 3 个已备（附录 A.1） | 部分（新旧管线盲评对照需真实研究运行，属实施后评估任务） |

## 已知边界与后续（如实清单）

1. **数据源未扩展**：guidance/consensus/电话会/A 股结构化披露依赖 §9.1 的 adapter 验证任务；相关模块当前按契约降级（missing + 原因），这是设计要求的行为，不是缺陷掩盖。
2. **事件溯源重建**：`metric/asserted` 事件带完整 payload 可重建观测索引，但「先事件后投影 + offset」的严格顺序与全量重放工具未实施；当前一致性靠幂等键（semantic_hash/input_hash/data_hash）。
3. **历史裁决批量回填**（M4）与 HTML 导出：未做。旧 fact 冲突的时态裁决投影仅新观测通道具备；旧通道保持原 `resolve_conflict` 行为（新页面历史视图中旧字段裁决入口已设只读）。
4. **浏览器 E2E**：前端逻辑经 vitest 覆盖（路由/数值边界），三档视口的截图验收（§13.3）未跑真实浏览器。
5. **性能目标**（p95 <300/500ms、数万观测）：本地 27 实体规模下即时响应；设计要求的规模压测未做。
6. **wall-clock/检索调用硬预算**：以轮次×步数近似，未按 §7.7 表实施计时器与 per-provider 限速。

## 验证入口

```bash
uv run pytest tests                        # 454 passed, 9 skipped（含 135 个新用例）
cd frontend && npm test && npm run build   # 37 passed；主包 206KB + echarts 懒加载 chunk
uv run python scripts/migrate_dossier.py --data-dir data inventory   # 迁移盘点（只读）
uv run python -m finance_agent serve       # → #/knowledge 列表 → 实体档案 → 来源抽屉 → 补研
```
