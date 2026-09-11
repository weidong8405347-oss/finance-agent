# 哨兵 B 组对照报告（2026-09-11）

> 方案：[research-profile-tools-plugins-plan-2026-09-09.md](research-profile-tools-plugins-plan-2026-09-09.md) §10「对照组 B/C」的第一次正式对照。
> 运行：`scripts/run_sentinel.py --all --auto-approve-gates`，代码冻结 worktree @ `a54906e`（= A 组基线 + F1–F13 整改 + P1-C 插件层 + P2-A 核验 + P2-B consolidator）。
> **注意**：B 组启动后仓库又合入了第三/四轮整改（`d200d07` review 12 条修复、`11d5c22` P1-C 执行闭环+SearchBroker、`2441155` 来源独立性+extract_table）——**不在 B 组测量范围内**；C 组对照应冻结当前 HEAD。
> 对照基线：A 组 `data/sentinel/baseline-A-r4/`（ai4s 用 `ai4s-final/` 全链版）；A′ 参考 `baseline-A-prime/`。
> 模型与环境同 A：research=kimi-k3、fast=GLM-5.3；预检降级源一致（prices_stooq/news_gdelt/fundamentals_hk，本机网络）。
> 判读纪律：单次运行的过程/结构信号，不是质量终评；引用语义支持率、盲评（C vs A 胜率）仍未做（P3）。
> 数据：`data/sentinel/baseline-B/`；逐指标对照 `data/sentinel/baseline-B/comparison.json`（`scripts/compare_sentinel.py` 生成）。

## 1. 总览

**6/6 completed，硬门禁全绿，无静默丢失。** 4 题 sufficient（ai4s / LOB / 3988.HK / NVDA），2 题 partial（BE / 2228.HK——均为门禁收紧后的诚实评级，见 §4）。

| 题 | A（基线） | B（本轮） | 关键变化 |
| --- | --- | --- | --- |
| guidance（NVDA） | sufficient 1/1；obs 4（first_party 2）；dup_chunks 83 | **sufficient 1/1；obs 5（first_party 5、secondary 0）；dup_chunks 25（-70%）** | 指引-实际配对全部一手化（8-K 原文 + XBRL）；重复资料大幅下降 |
| restatement（LOB） | sufficient 1/1；**obs 0**（数字只在文本） | **sufficient 1/1；obs 4（全 first_party/pit_a）** | F2 typed 门禁生效：重述链条的关键数字进指标库 |
| deep-pdf（3988.HK） | sufficient 1/1；claims 1 | **sufficient 1/1；claims 2**；答案直引「附注八、分部报告」逐字数字（658,310 百万） | 400+ 页年报附注直达保持；dup_chunks 5→102（见 F16） |
| 2228.HK 中文财报 | partial 3/12（key 0.43）；stalled | **partial 8/13（key 0.875）；budget 正常耗尽**；claims 7→31；counter_claims 1→4；**verify_claim 真实调用 3 次（2 次数字硬检查 failed 被抓）**；dup_chunks 557→365 | 交题纪律 + 修复回环 + 核验器全部在真实中文 PDF 场景工作 |
| BE 深研 | sufficient 12/12；**obs 0** | partial 9/13（key 0.875）；**obs 11（first_party 10、pit_a 10）**；claims 7→38；counter_claims 0→3 | 数字全部入库（订单口径哨兵场景直接命中）；覆盖下降是门禁把纯文本答案变未答（A′ 同判定，诚实优先） |
| ai4s（/industry） | completed 124.8min；F1 obs 15 | **completed 144.2min；F1 obs 0（回归，见 F14）**；F2 44 只→F3 闸口 1 轮通过（代行留痕）→F4：TEM 100%/SDGR 88%/RXRX 88% converged、3 只港股 0% budget 诚实上报→F5 核查型报告（SDGR 市值验算 fail 差 12.3%、RXRX 收入两源分歧 77%、OCF/FCF 物理矛盾——全部走 conflict 机制） | 漏斗全链稳定复现；F5 交叉核验行为是新增质量信号 |

## 2. 本轮升级在真实运行中被验证的能力（B 组新增证据）

- **verify_claim 内容级核验（P2-A）**：2228 题 3 次真实调用，全部 `partially_supported`，
  其中 2 次**数字硬检查 failed**（"1.0/19.0"、"4.0" 未在支持原文逐字定位）——软审查
  没能盖过硬检查（设计目标达成）；next_actions 可执行（HKEX 乱码换渠道核对、两笔付款
  是否同一协议、B 级单一来源族补 A 级证据、期间归属系推断需原文依据）。
- **submit_question_result 批量提交**：BE 题 16 次真实调用（减少机械往返的行为面证据）。
- **track_sub_question**：BE 题 2 条内部子问题落库（不扩预算/范围）。
- **状态卡（语义压缩）**：research/context_compressed 事件逐轮落库并回流 brief；
  2228 题 dup_chunks 557→365、NVDA 83→25（重复资料下降与状态卡/文档复用相关，
  归因需消融确认，不独占归功）。
- **F10 量表闸**：BE 题运行中真实拦截 `'1065365 thousand'` 量级未绑定提交（拒绝带修法）。
- **F11 修复回环**：2228 题 stop_reason 从 A 组 stalled 变为 budget 正常耗尽
  （门禁拒绝不再触发提前停滞）。
- **P1-C 插件层**：plugins/manifest_frozen 每个研究 run 落库（34 工具、config_hash
  66cc7d9c…）；registry 驱动装配下 6/6 题数据源行为与 A 组一致（迁移不改变行为的
  真实运行证据）。
- **P2-B**：S2 全链（prepare→thesis→verify→commit）单测覆盖；B 组 /research 管道
  不含 S2（/profile 才走），真实运行验证留给下轮 profile 哨兵。

## 3. 指标对照（方案 §10.2 可自动采集子集，单次运行）

| 指标 | A | B | 判读 |
| --- | --- | --- | --- |
| 管道完成率 | 6/6（ai4s 需补跑 F5） | **6/6（一次通过，含 F5）** | 运行器宽限窗口 + 稳定性改善 |
| 硬门禁 | 全绿 | 全绿 | 保持 |
| 关键数值入库（obs>0 的题） | 2/6（NVDA、2228） | **5/6**（除 deep-pdf 与 ai4s F1） | F2 门禁直接因果 |
| first_party 观测占比 | NVDA 2/4 | NVDA 5/5、BE 10/11、LOB 4/4 | F7 域细化 + XBRL/8-K 原文路径 |
| 内容核验覆盖 | 0 | 3/106 validated claims | 工具可用但覆盖率低（F15） |
| 反证沉淀（counter_claims） | BE 0、2228 1 | BE 3、2228 4 | F8 纪律生效 |
| 重复窗口注册 dup_chunks | BE 305、2228 557、NVDA 83 | BE 636、2228 365、NVDA 25、3988 102 | 混合（F13 部分改善、部分恶化，见 F16） |
| 耗时 | targeted 12–17min；deep 43–52min；ai4s 125min | targeted 12–15min；deep 43–45min；ai4s 144min | 基本持平（ai4s +16%，港股 3 票深研跑满预算） |

## 4. verdict 口径说明（BE sufficient→partial 不是能力回退）

A 组 BE 的 sufficient 12/12 建立在「纯文本答案、0 typed 观测」上；F2 门禁（数值题
answered 必须含 obs-/calc- 引用）+ 交题欠账提醒把口径收紧后，A′ 与 B 都是 9/13
partial——**同一次运行的 typed 产出从 0 → 11（10 条一手 A 级）**。这是「不为评分
补造内容」的既定取舍（A′ 报告 F11 已定性），B 组数据进一步确认：partial 的内容
质量（观测/反证/核验留痕）显著高于 A 组 sufficient。

## 5. B 组新发现（未修，如实记录）

| # | 发现 | 证据 | 建议归属 |
| --- | --- | --- | --- |
| F14 | **ai4s F1 行业级 typed 观测回归 15→0**：问题覆盖 1/1、claims 5v 不变，但 market_size/growth_rate 无一入库；F1 在 8.5min 预算内 budget 耗尽（A-final 同预算写出 15 条） | B 组 ai4s assessment + F1 step 摘要 | 行业配方数值题的 brief 示例仍不够"顺手"（F6 复发）；或状态卡/新工具面加大 prompt 挤压了采集步数——需消融（关闭状态卡对照） |
| F15 | **verify_claim 覆盖率低**：3/106 validated 论断被核验（全部在 2228 题）；BE 38 条 validated 全 unchecked。工具在但模型不主动用 | claims_by_evidence_support 分布 | 在合成/S2 加门禁推动：报告引用的 validated 论断必须已核验（或合成阶段服务端批量核验 top-N 关键论断）；发布规则已拦 contradicted，缺的是"必须核验"的触发 |
| F16 | **dup_chunks 两极分化**：NVDA 83→25、2228 557→365 改善；BE 636、deep-pdf 5→102 恶化（后者疑为 search_document 重试循环） | budget 快照 | EvidencePack 按问题组织证据（已具备）+ 窗口 chunk 复用计数进 brief（F4 遗留）；deep-pdf 个案需回放工具调用序列定位 |
| F17 | assessment 的 numeric_consistency 未进 runner 信号采集（只能回 events.db 查） | compare 脚本缺该列 | run_sentinel._extract_signals 补键（评估脚手架小修） |

## 6. 结论与下一步

**结论**：B 组确认第一轮+第二轮升级的方向与净收益——typed 入库覆盖 2/6→5/6、
一手来源占比大幅上升、内容核验在真实中文 PDF 场景抓出数字未定位、反证沉淀翻倍、
2228 提交率与停滞形态改善、管道一次全通。代价与遗留同样清楚：行业 F1 typed 回归
（F14）、核验覆盖率低（F15）、重复窗口注册两极（F16）。

**下一步**（按失败分布）：
1. F15：合成/S2 的"报告引用论断必须核验"门禁 + 服务端批量核验 top-N（P2-A 收口）；
2. F14：行业配方数值题二轮修复（示例进 brief / 状态卡消融对照）；
3. Docling 试点（2228 题 4/8 中文 PDF garbled 仍是原文可得性最大缺口，B 组依旧）；
4. F16/F17 小修 + 第三次运行（C 组）验证；
5. P3：24 题扩展与盲评（人工评审 + LLM judge 校准），量化 C vs A 胜率。
