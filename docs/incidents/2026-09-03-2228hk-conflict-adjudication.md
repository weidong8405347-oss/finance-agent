# 2026-09-03 · 2228.HK 合并后冲突裁决建议（待人工确认）

## 背景

`merge-entity 02228.HK → 2228.HK` 恢复重放后，两条研究线的平行断言叠加，浮现
**6 个字段、21 条冲突版本**（见 `2026-09-03-knowledge-verify-gate.md` 遗留项）。
本文档按统一口径给出每字段的 keep 建议。

**已执行（2026-09-03，用户确认「入库」）**：六字段逐个走
`POST /api/knowledge/stock/2228.HK/resolve`，审计事件（6 条 `fact/conflict_resolved`
+ 6 条补写 `fact/asserted`）全部落 `kb-stock-2228.HK` 审计 run。终态：
冲突清零（23 个标记：21 遗留 + 2 个补写瞬时冲突随印随清），质量分 0.35 → **0.825**，
实体 issues 只剩 peers 缺失（真缺口，归下轮研究）与 valuation 仅 C 级证据（归弱字段回流）。

## 裁决口径（按优先级）

1. **证据 PIT 等级**：A（hkex_news，HKEX 公告直采，有 available_at）> B（有
   available_at 的媒体/通稿）> C（web 快照，无 PIT 保证）。
2. **knowledge_time 双时态干净度**：事实可知时点 = 公告/报道日（如 2026-03-25）
   优于恢复重放时戳（2026-09-02，重放把 kt 抬到了今天，PIT 语义受损）。
3. **来源质量**：HKEX 公告 > 公司通稿（PRNewswire 为晶泰官方发布渠道）>
   财经媒体（每经/新浪/21财经）> 券商/warrants 门户（jpmhk、中金在线转述）。
4. **值形态**：散文/结构化 dict 优于序列化 JSON 字符串（写侧门禁上线前的旧形态；
   且 resolve 对非最新版本会经 ProfileWriter 补写同值新版本——**keep JSON 字符串
   旧值会被准入闸拒绝**，见「注意事项 ①」）。

数值层面：6 个字段的冲突绝大多数是**平行重复断言**（数值一致、证据链不同），
非真分歧；唯一实质分歧在 `risks` 的 2026H1 净亏损口径（见该字段说明）。

## 建议清单

| 字段 | 冲突版本 | 建议 keep | 核心理由 |
| --- | --- | --- | --- |
| revenue_fy | v2/v3/v4/v5/v8 | `fact-1dad38b0dad9`（v7） | **全字段唯一 A 级证据**（hkex_news，HKEX 2025 全年业绩公告直采，available_at=2026-03-25）；kt=公告当日（双时态最干净）；值是结构化 dict（amount 802,623,000 CNY，机器可算）；数值与全部 5 条冲突版本一致（802,623 千元，无真分歧） |
| net_income_fy | v2/v3/v4/v7 | `fact-f159796965ad`（v7） | 唯一 B 级证据（21财经，available_at=2026-03-26）；kt=公告次日（非恢复时戳）；口径正确（年内利润 1.35 亿 + 经调整 2.58 亿双口径齐备）。备选 `fact-11834281dc1a`（精确千元 134,579 + HKEX 公告 URL 直证，但 C 级且 kt 为恢复时戳）。**不建议** `fact-eabe009a948d`：虽 A 级但值是经调整净利 2.58 亿——口径错位，net_income_fy 应记年内利润 |
| cash_flow | v3/v4 | `fact-0400919327ef`（v6） | B 级（PRNewswire 公司通稿，2026 中期业绩官方发布渠道，available_at=2026-08-19）；结构化 dict（amount 8,671,200,000 as_of 2026-06-30）；取最新时点余额（86.71 亿 > v1/v2 的 70.69 亿 2025 年末口径）。v3 同数但值是 JSON 字符串（keep 会被门禁拒）；v4 同数但自认「未单独登记证据」 |
| valuation | v6/v7 | `fact-bc9482252abd`（v9） | 冲突实为**不同日期快照**（7-14 ~ 9-02，非数据矛盾）；v9 是最新快照（2026-09-02 当日 Yahoo 31.242B @ 7.26 × 43.03 亿股，服务端验算通过）且 kt 与快照同日（双时态自洽）；dict 结构化。v6 kt=07-17 却引 08-21 快照（时点错位）、v7 混两个日期快照；v4（07-15 月报表口径）留作历史版本自然演进 |
| business_model | v7 | `fact-e5130672e60f`（v3） | B 级（PRNewswire 公司通稿 2026-08-19）；最新完整散文叙事（双板块 + 2026H1 AI4S 占比 49% + 平台化跃迁）；演进型字段取新叙述。v7（02228 旧链）是 2025-04 时点旧描述（仅 XtalFold 授权，无 DoveTree/GPCR 演进）。**A 级的 `fact-843c3a2c427b`（HKEX 2026-06-09 公告直证）内容最好但值是序列化 JSON 字符串——keep 会触发注意事项 ①**；建议后续以它为素材走弱字段回流重写成散文 |
| risks | v2/v3/v4/v5/v6/v8/v9 | `fact-df039eb80bd9`（v8） | **唯一双 B 级证据**（新浪财经 + PRNewswire 通稿，均 available_at）；kt=中期公告次日（2026-08-20，干净）；结构化 3 点风险列表（BD 首付款依赖 / 研发侵蚀 / 股价波动）。**当前投影 v6 是 6 版本里最弱的**（jpmhk warrants 门户、单 C 级），且净亏损 2.51 亿 / 上年同期纯利 8,279.5 万是少数派口径——多数版本与全部 B 级证据一致为净亏 2.25 亿 / 上年同期 7,560.9 万；裁决即纠正投影 |

## 执行记录（2026-09-03）

| 字段 | keep（裁决依据版本） | 落版 fact_id（补写同值新版本） | 清除标记 |
| --- | --- | --- | --- |
| revenue_fy | fact-1dad38b0dad9 | fact-f3d4550e7ef2（v11） | 6 |
| net_income_fy | fact-f159796965ad | fact-b982584a393e（v10） | 5 |
| cash_flow | fact-0400919327ef | fact-7809d4ee8013（v9） | 2 |
| valuation | fact-bc9482252abd | fact-d7bfef69567e（v10，人工落版） | 2 |
| business_model | fact-e5130672e60f | fact-060d625806de（v9） | 1 |
| risks | fact-df039eb80bd9 | fact-c60183695bfb（v12） | 7 |

- valuation 特例：keep 版本是 version 链尾，resolve 端点不触发补写；但合并重放造成
  version 序与 knowledge_time 序错位（v4 的 kt 更晚），若不落版则投影仍显示 v4（07-15
  快照）。按 store `resolve_conflict` 的设计约定（「若 keep 的不是最新版本，调用方应再写
  一条同值新事实落最新版」）经 ProfileWriter 补写同值事实（同一审计 run、全门禁、
  `fact/asserted` 留痕），裁决值落到当前投影。
- 裁决后弱字段（下一轮研究素材，走 weak 回流）：valuation 仅 C 级证据（待行情/月报表
  双源核实）；peers 缺失（真缺口）。

## 注意事项

1. **resolve × 写侧门禁交互（已根治，后续提交）**：resolve 对非「当前投影」版本会经
   ProfileWriter 补写同值新版本，值若为门禁上线前的序列化 JSON 字符串旧形态
   （revenue_fy v1/v4、net_income_fy v3、cash_flow v3、valuation v3/v4、
   business_model v2/v4/v5、risks v2/v3/v4）会被 `assert_value_admissible` 拒写。
   端点现返回 **409 + 出路提示**（改选版本或先以合规形态重写），不再 500；
   豁免通道不开——准入闸 fail-closed 对裁决入口同样成立。补写触发判据同步从
   「版本链尾」改为「当前投影」，合并重放的 version/kt 序错位不再需要人工补落版
   （本表 valuation 一例即该错位的实测）。
2. valuation 为时点快照字段（max_age 180d）：裁决只清冲突，不冻结估值——
   下一轮研究会自然刷新。
3. 21 条冲突版本裁决后全部保留在版本链中（append-only），仅清除冲突标记并落
   `fact/conflict_resolved` 审计事件；被否版本不删除。
