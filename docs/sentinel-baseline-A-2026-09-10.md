# 哨兵基线 A 运行报告（2026-09-10）

> 方案：[research-profile-tools-plugins-plan-2026-09-09.md](research-profile-tools-plugins-plan-2026-09-09.md) §10「与 ChatGPT Deep Research 对照的评估方案」的 A 组（当前基线）。
> 运行：`scripts/run_sentinel.py`，题集 `evals/sentinel_tasks.yaml`（6 题冻结）。
> 环境冻结：commit `c7f5af5`→`33fd781`（运行中发现缺陷即时整改，逐条记录于 §4）；模型 research=kimi-k3、fast=ZHIPU/GLM-5.3（pi provider 配置）；数据源预检 edgar/edgar_facts/hkex_news/web_search(Exa-Novita)/web_search_tavily/fundamentals/prices 可用，prices_stooq/news_gdelt/fundamentals_hk 降级（本机网络，fail-closed 不阻塞）。
> 数据与逐题 JSON：`data/sentinel/baseline-A-r4/`（前两次中止试跑留档 `pilot-*`）；ai4s 重跑 `data/sentinel/ai4s-rerun/`。
> 判读纪律：本报告是**单次运行**的过程/结构信号，不是质量终评——引用语义支持率、关键数值准确率等需人工对照原文（方案 §10.2），盲评对照（C vs A）未做。

## 1. 结果总览

| 题 | 结果 | 覆盖 | 产物 | typed 产出 | 耗时 | 关键信号 |
| --- | --- | --- | --- | --- | --- | --- |
| guidance-to-actual（NVDA） | **sufficient** ✅ | 1/1 | validated | 4 观测（2 guidance 一手 + 2 reported）+1 claim | 14m | Q1/Q2/Q3 指引与实际值全部命中原文（$78.0B±2%→$81.6B；$91.0B→$96.2B）；acceptance 时刻 PIT |
| restatement-handling（LOB） | **sufficient** ✅ | 1/1 | validated | 3 claims | 17m | 重述事实链全对：8-K 4.02（11-12）→10-K/A（11-17，accn 逐字正确）→FY22-24 现金流量表重述→内控重大缺陷→NT 10-Q；还追到 FY2025 最新 10-K |
| deep-pdf-late-table（3988.HK） | **sufficient** ✅ | 1/1 | validated | 8 观测（全 A 级一手，页码 locator 283/284） | 12m | **411 页年报附注 44「分部報告」直达**（read_document page_range 282-298）；FY2025+FY2024 双年同口径分部收入；旧 80 页墙下不可达 |
| be-orders-revenue（BE） | **sufficient** ✅ | 12/12（key 1.0） | validated | 0 观测 + 7 claims ⚠ | 52m | 12 题答案全部含原文数字与日期（Q2'26 首破十亿 $1,065.4M +166%；Oracle 扩容 2.8GW；反证=Hunterbrook 做空报告+CEO 否认）；**但 typed 观测为 0**（见 F2） |
| 2228hk-chinese-filings（2228.HK） | **partial** ⚠ | 3/12（key 0.43） | validated(partial) | 12 观测（全 A 级，页码 6/27/85/91/104/248）+7 claims+反证 | 43m | 4 份中文 PDF garbled 被质量闸拦截→worker 找到 350 页可读年报；DoveTree US$5.99B 或有总额 vs US$51M 已收首付正确区分；9 题未交答案（见 F5）；单位/币种错配（见 F3） |
| ai4s-tech-commercial（/industry） | 首跑 **blocked**→整改后重跑：F1→F3 已通过，F4 深研进行中 | F1 问题 0/1 但 2 claims（新判定不拦停） | — | F1 观测 0 ⚠ | 首跑 9m；重跑 F1-F3 共 40m | 重跑：F1 completed→thesis→F2 标的池 49 只（3 路并集均绑证据）→F3 粗筛+闸口代行审计留痕→深研名单 6 只（02228.HK/07666.HK/SDGR/RXRX/AC/301080.SZ，含港股与 A 股）→F4 fan-out 进行中；首跑暴露的 F10/F11 已修 | 

管道完整性：5/6 题走通 S1→synthesize→validated 产物→dossier 快照发布→process_eval 全链；ai4s 重跑已过 F1→F3（闸口代行路径验证成功），F4 深研 fan-out 于本报告提交时进行中（结果将自动落 `data/sentinel/ai4s-rerun/sentinel-results/ai4s-tech-commercial.json`）。所有硬门禁（引用可解析/数字可重算/时态/论断支持）全绿；`validated_claims_content_unchecked` 诚实披露（NVDA 1/1、BE 7/7、2228 7/7）；来源分档生效（2228 全 12 条 first_party A 级、NVDA guidance=first_party/reported=secondary）。

## 2. 本轮升级在真实运行中被验证的能力

- **Document Read v2**：411 页 PDF 附注直达（页码 locator 进证据）；中文 garbled PDF 拦截+警示+替代文档发现；HTML 单页文档全文检索；同文档去重复用（documents_stored 4–25，duplicate_documents 1–19）。
- **SEC XBRL（query_edgar_facts）**：NVDA/LOB 任务真实调用（收入/EPS/净利/现金流 tag+accn+原文链接），acceptance 分钟级 PIT；LOB 题的 10-K/A 链条靠它+query_edgar(include_history 语义)定位。
- **统一知识读取**：get_research_context 每题开工即调（复用冻结计划问题状态）；read_evidence 批量回读。
- **P0 可信度**：冲突 0 开放（BE/2228 运行中未留未裁决冲突）；validated=引用校验的诚实语义贯穿 assessment notes/产物；一手/二手分档如实（转载不冒充一手）。
- **收官提醒+16 步**（运行中整改后）：NVDA 从「11 条证据零提交」变为「预算内交满答案+观测」。

## 3. 运行中即时整改（试跑→修复→重跑，全部有回归测试）

| # | 缺陷（试跑实证） | 整改 | commit |
| --- | --- | --- | --- |
| R1 | propose_metric 的 guidance/consensus schema 空壳 `{"type":"object"}`；模型传 `target_period:"FY2027Q1"` 连拒无形状提示 | schema 完整声明对象形状；三类拒绝（period 缺 start/target_period 字符串/多数字缺 value_span）全部附可照抄修法示例 | `73dca22` |
| R2 | worker 对剩余步数零感知：12 步烧在发现/精读，11 条完美证据零提交（NVDA r2 stalled） | kernel 收官提醒（剩余≤3 步注入一次 system 提醒）；问题驱动组 12→16 步 | `fe81f55` |
| R3 | 哨兵会话 stalled 后主 agent 自主重发 10 题 standard 研究（污染题目范围与预算） | runner.set_wake(None)；哨兵运行器禁用唤醒（基线可复现） | `fe81f55` |
| R4 | `published_at="2026-02-25"`（日期串→naive datetime）与 aware 比较 TypeError 炸门禁 | 观测模型层时区归一（无时区按 UTC，存量反序列化同样归一） | `fe81f55` |
| R5 | 行业级 growth_rate 被主体闸 4 连拒 → F1 零写入 stalled → /industry 全漏斗 blocked | growth_rate 放开 industry 主体（公司指标写行业实体仍拒） | `33fd781` |
| R6 | F1 停滞判定只看字段完整度：问题 1/1 sufficient 仍 blocked | 与 step_research 同口径：问题/观测/论断任一有效产出不拦停；摘要如实报覆盖与缺口 | `33fd781` |
| R7 | 过时停滞建议「待接入 HKEXnews/Exa」（两源早已装配，2228 实测 hkex_news 正常） | 建议按装配状态分流：乱码换英文版/HTML、文档精读工具指引、未装配才提示查 key | `33fd781` |
| R8 | 'RMB thousands' 自造单位连拒 4 次（只给允许集合不给修法） | 拒绝附正向修法（规范 unit + unit_text 承载原文量表） | `33fd781` |

## 4. 基线发现（**已全部整改**，commit `a19fa90`，27 例回归；下表保留原始发现记录）

> 2026-09-10 更新：F1–F9 逐项修复（focus 编译/数值题 typed 门禁/unit↔currency 闸/
> 交题欠账提醒/行业 typed 示例/披露域一手归类/反证沉淀纪律/targeted 检索预算 20）；
> A' 验证重跑（NVDA/BE/2228.HK 三题，验证门禁不过度拦截与行为改善）在 ai4s
> 重跑完成后自动顺序执行，结果落 `data/sentinel/baseline-A-prime/`。

| # | 发现 | 证据 | 建议归属 |
| --- | --- | --- | --- |
| F1 | **--focus 在 deep 模式未编译成专门问题**：BE 题 focus=订单口径与收入确认，计划仍是标准 12 题，订单口径场景未被直接验证 | plan_created objective="深度研究 BE"，问题列表无 backlog 专项 | plan.py objective/focus 编译（P0 级小修） |
| F2 | **typed 观测使用不稳定**：BE financial 组 0 次 propose_metric（写了 4 个旧字段走阻力最小路径），2228 却写出 12 条；数字留在答案文本里不进指标库 → 图表/序列/数值门禁全部旁路 | BE group_end：propose_metric=0；2228=12 | 问题驱动纪律强化（financial 模块问题 answered 需 obs/calc refs 或说明）；P2-A verifier 一并覆盖 |
| F3 | **单位/币种一致性缺口**：2228 观测出现 unit=USD+currency=CNY 自相矛盾、cfo 疑似千元未归一（-399238 vs revenue 393627000 同库并存） | metrics.db 抽查 | 语义闸加 unit↔currency 一致性 + 同实体同指标量级离群检测（P1 级） |
| F4 | **重复窗口注册巨大**：duplicate_chunks BE=305、2228=557（dedupe 保住上下文不膨胀，但模型步数浪费在重复 fetch/read） | budget 快照 | fetch/read 响应强化「已存档勿重复抓取」提示已加；考虑窗口 chunk 复用计数进 brief |
| F5 | **deep 计划提交率不稳**：2228 12 题只交 3（4 轮后 stalled），BE 12/12；收官提醒对 targeted 有效，deep 多轮场景仍不稳 | question_updated 计数 | 轮末未交题清单回流下一轮 brief（已有 gaps 回流，扩展到「已答证据但未交题」明细） |
| F6 | 行业级 typed 观测仍为 0（growth_rate 修复后 F1 只写出 claims）：行业配方缺 market_size/growth_rate 的可用示例与维度约定 | ai4s 重跑 F1 观测=0 | 行业 MetricSpec 示例进 recipe/brief（P2-B 行业配方） |
| F7 | 直接 URL 抓取的 sec.gov 原文归为 web_fetch → 无法进入一手档（NVDA 新闻稿实际是发行人原文） | NVDA evidence_quality secondary=2 | 来源角色按 URL 域细化（sec.gov/hkexnews → issuer_filing），或文档从记录进入时继承角色 |
| F8 | BE counter-evidence 答案含强反证（Hunterbrook）但 counter_evidence_claims=0：反证进了问题结论、没沉淀成带 counter_refs 的 claim → analytical_depth 低估 | BE assessment | 纪律/验证器要求反证结论落 claim(counter_refs)（P2-A） |
| F9 | 检索预算 targeted=12 次打满（NVDA 12/12）：预算表按旧工具面调参，文档工具时代 fetch/search 也扣检索预算 | budget exhausted 无（max_rounds 先停）但 12/12 | budget_for_mode 参数复调（P3 消融时一并） |

## 5. 指标对照（方案 §10.2 口径的可自动采集子集，单次运行）

| 指标 | 基线 A 观测值 | 备注 |
| --- | --- | --- |
| 引用可解析率 | 5/5 题硬门禁通过（100%） | integrity citations_resolvable 全绿 |
| 数字可重算 | 100%（有观测的题全过） | numbers_recomputable |
| 有效问题覆盖 | sufficient 题 100%；2228=25%（3/12） | 均值被 deep 提交率拖累（F5） |
| 原文可获取率 | 中文 PDF 4/8 garbled（拦截可见）；404 显式报错 3 次 | garbled 是 pypdf 能力边界，Docling/OCR 试点（方案 P1-A 第 5 步）是解法 |
| 重复资料率 | duplicate_documents 1–19/题；duplicate_chunks 最高 557 | 文档级去重生效；窗口级重复是步数浪费（F4） |
| 成本 | targeted ≈14–17m/题；deep ≈43–52m/题；NVDA tokens 748K | kimi-k3 研究主力；预算墙钟打满属预期 |
| 体验 | 无静默丢失；partial/stalled 均有诊断与摘要 | 取消路径未测（哨兵不含） |

## 6. 结论与下一步

**结论**：本轮升级（P0+统一知识读取+Document Read v2+SEC XBRL）在 6 题哨兵上**方向正确且已被真实运行验证**——5/6 题产出 validated 产物与快照，其中 3 题 sufficient；411 页 PDF 附注直达、中文乱码诚实拦截、重述链条逐字准确、指引-实际正确配对，都是旧工具面做不到的。同时基线暴露 9 项待整改（F1–F9），其中 F2/F3（typed 观测稳定性与单位一致性）直接关系「关键数值准确率 ≥98%」目标，是下一轮最高优先。

**下一步**（按方案 §11.2 失败分布决策）：
1. ~~修 F1/F2/F3~~ **已完成**（commit `a19fa90`，F1–F9 全部整改 + 27 例回归）；
2. A' 验证重跑（已排队自动执行）对照本报告冻结 A 组结果；
3. B 组：Docling 试点解决 garbled 中文 PDF（2228 题 4/8 文档乱码是当前最大原文可得性缺口）；
4. P2-A 内容级 verifier（F8 反证沉淀已加纪律提示，硬核验归 verifier；引用语义支持率人工标注子集）；
5. ai4s 重跑 F4–F5 完成后补漏斗全链数据（A 股候选 301080.SZ 在无 A 股披露源下的诚实降级表现是额外看点）。

---

## 7. A' 验证重跑与 A 对照（2026-09-10，F1–F9 整改后，commit `a19fa90`）

同模型（research=kimi-k3 / fast=GLM-5.3）、同题集、同运行器（含闸口代行）；三题重跑：NVDA / BE / 2228.HK。数据：`data/sentinel/baseline-A-prime/`。

| 题 | A（基线） | A'（整改后） | 对照结论 |
| --- | --- | --- | --- |
| NVDA guidance | sufficient 1/1；obs 4；答案 typed refs **0**；first_party **2**/4；833s | sufficient 1/1；obs 4；答案 typed refs **4/8**；first_party **4/4**；675s | 质量持平；F2 门禁触发 1 次→模型当轮恢复并带 obs 引用交题 ✓；F7 域细化把 sec.gov 直拉新闻稿正确归一手 ✓ |
| BE 深研 | sufficient 12/12；obs **0**；focus 未编译；3097s | **partial 9/13**（key 0.625）；obs **2**；**focus 题编译且已答**；数值题全部带 typed refs；2084s | F1 ✓：`focus-0c9baca5` 直接回答订单口径——「PR 口径 ~$20B 总 backlog vs GAAP RPO $394.4M」，两条 firm_backlog 观测以 **basis=operating_metric/GAAP 正确分离**（语义键设计经受实战）；F2 ✓ 数字进库；覆盖度下降是门禁把「纯文本答案」变成未答 + stalled 提前终止（见 F11），符合「不为评分补造」原则但需调优 |
| 2228.HK 中文财报 | partial **3/12**（key 0.43）；obs 12（含 unit=USD+currency=CNY 违例）；2601s | partial **12/13**（key **1.0**）；obs **16**（页码 locator 27–292）；**unit 违例 0**；开放冲突 1 项**可见披露**；2908s | F5 交题欠账提醒注入 **13 次**、F3 单位闸生效、提交率 3/12→12/13——整改直接因果；net_income H1 双口径（-224,944 vs -251,901 千元）形成竞争版本并诚实降为 partial（绝不静默覆盖 ✓），但 worker 未在预算内裁决（F12） |

**ai4s 漏斗重跑**（R5/R6 整改后）：F1 completed（2 claims，新判定不拦停）→ thesis 落档 → F2 标的池 **49 只**（3 路并集均绑归属证据）→ F3 粗筛 **43 张卡** + 闸口代行（审计留痕）→ 深研名单 6 只（02228.HK/07666.HK/SDGR/RXRX/AC/**301080.SZ**）→ F4 深研完成（SDGR 88%/RXRX 100%/AC 88%/301080.SZ 100% converged；两只港股 budget 0% 诚实上报，失败隔离生效）→ F4.5 委员会 **6 票全产出**（四视角+空头+CIO，artifacts 落盘）→ **F5 rank_report 被运行器 120min 超时杀死**（CommandRunner 是 daemon 线程，主进程退出即杀在飞 step）——运行器已整改（45min 宽限窗口 + status=completed_after_grace）且题集超时调至 180min；漏斗机制本身全链验证通过。

## 8. A' 新发现（F10–F13，未修）

| # | 发现 | 证据 | 建议归属 |
| --- | --- | --- | --- |
| F10 | **千元归一不一致 + 语义键漂移**：2228 revenue 802,623（千元原样，p.242）与 802,623,000（归一至元，p.29）因 dims 漂移不同语义键、无冲突标记并存；cfo（千元）与 revenue（元）跨指标量表不一致 | A' metrics.db 观测清单 | 同实体同指标的量表归一策略 + 量级离群检测（P1 级，数值准确率命门） |
| F11 | **stalled 提前终止**：answer_question 被拒后的修复尝试不计入进展信号，BE A' 4 题未答即判 stalled（budget 未耗尽） | BE A' stop=stalled, exhausted=[] | 方案 §8.3 既有方向：把「可验证的提交尝试/高价值精读」纳入进展信号 |
| F12 | **开放冲突未在预算内裁决**：2228 net_income 冲突，list_conflicts/adjudicate_conflict 工具在但 worker 没用 | A' open_conflicts=1 | brief 纪律：数值题交题前查 list_conflicts，有开放冲突先裁决或注明 |
| F13 | **重复窗口注册仍高**：duplicate_chunks BE 305→646、2228 557→803（门禁重试轮次增加；dedupe 保住上下文不膨胀但烧步数） | budget 快照 | P2-A EvidencePack（按问题组织证据包）根治 |

正面确认：BE 双口径 backlog 以 basis 正确分离（未误报冲突）；2228 竞争版本机制正确标记双口径净利（append-only 不覆盖）；未答题全部带详细 attempts（「search_document 0 命中，10-K 未给 TAM 数字」级别的可核查记录）；诚实降级贯穿（港股 0% 完整度如实上报、garbled 拦截、A 股候选无披露源时靠 web 证据收敛）。

## 9. 更新后的下一步

> 2026-09-11 状态：F10–F13 已整改（`9ec8283`）；P1-C/P2-A/P2-B 已落地（见
> [实施记录](research-profile-tools-implementation-2026-09-10.md) §5）；**B 组对照
> 已跑完（6/6 completed）**，结果与新发现 F14–F17 见
> [B 组对照报告](sentinel-baseline-B-comparison-2026-09-11.md)。

1. ~~F10 量表归一 + F12 裁决纪律~~ **已完成**（`9ec8283`）；
2. ~~F11 进展信号扩展~~ **已完成**（提交类门禁拒绝 + 高价值精读计为可验证探索，有界回环）；
3. B 组：Docling 试点（2228 题 4/8 文档 garbled 仍是原文可得性最大缺口，B 组依旧）；
4. ~~P2-A 内容级 verifier + EvidencePack~~ **已完成并真实运行验证**（2228 题 3 次核验、
   2 次数字硬检查拦截）；遗留 F15：核验覆盖率低，需合成/S2 门禁推动；
5. ~~ai4s F5 补跑~~ **已完成**（宽限窗口版运行器，124.8min 全链，见 §10）。

---

## 10. ai4s 漏斗全链补跑（A 组基线收官，2026-09-10 深夜）

前次 ai4s 重跑 F5（rank_report）被旧运行器 120min 超时杀死（daemon 线程随主进程退出）。
本次用**宽限窗口版运行器**（180min 题时限 + 45min 宽限，`1f685f0`）在冻结 worktree
（代码 `5eb0d2e`，与 A' 同基线）补跑全链，闸口代行带审计留痕。

**结果：completed，124.8 分钟（未触发时限与宽限），F1→F5 七步全部完成。**
数据：`data/sentinel/ai4s-final/`；环境冻结 research=kimi-k3 / fast=GLM-5.3。

| 阶段 | 结果 | 与前次对照 |
| --- | --- | --- |
| F1 赛道地图 | completed（stalled 但有效产出不拦停）：**观测 15 条**、论断 5v/1d、问题覆盖 1/1；字段完整度 20%（market_size/growth_rate/competition/policy 缺口如实） | 前次观测 0 → **15**（a19fa90 行业 typed 示例 + growth_rate 主体闸修复生效） |
| F1.5 thesis | 产业判断备忘录落档（绑证据） | 持平 |
| F2 标的池 | **47 只**（3 路并集均绑归属证据） | 前次 49 只（检索波动，正常） |
| F3 粗筛+闸口 | 43 卡 → 闸口第 1 轮通过（代行批准留痕：op=industry_screen, round=1）→ 深研 6 只：SDGR/RXRX/TEM/02228.HK/07666.HK/RLAY | 前次深研名单含 AC/301080.SZ，本次 TEM/RLAY（LLM 非确定性，均在池内） |
| F4 深研 fan-out | RXRX 100%（converged）、RLAY 100%（converged）、SDGR 94%（budget）、TEM 94%（budget）、02228.HK 0%（budget）、07666.HK 0%（budget）——港股两只无进展**诚实上报**，失败隔离生效 | 前次同形态（港股源覆盖缺口是已知边界，归 B 组 HK 接入 spike） |
| F4.5 委员会 | **6 票全产出**（四视角+空头+CIO，artifacts 落盘） | 持平 |
| F5 排序报告 | **completed**（前次被杀）：核查型报告——SDGR 客户数 29→27 与 RLAY 现金 $121.2M 经 10-K/10-Q **A 级原文证实**；TEM Q2 数据属实但标注 C 级源降级；四只有数据票估值快照已算 | 运行器整改直接因果 |

附注：industry 链路不发布 dossier 快照（dossier_published=0 为当前设计行为，非缺陷）；
预检降级源 prices_stooq/news_gdelt/fundamentals_hk（本机网络，fail-closed 不阻塞）与 A 组一致。

**基线验证状态：已完成。** A 组 6 题 + F1–F9 整改 + A' 三题对照 + ai4s 漏斗全链
（含 F5）全部有冻结数据；F10–F13 已整改（`9ec8283`）。B 组对照（在 P1-C/P2-A/P2-B
落地后的代码上重跑哨兵）为下一步，见实施记录 §6。
