# AI for Science：未来十年“卖铲人”投资地图与美股、港股 Top 5

**研究基准日：2026 年 8 月 23 日；股票价格统一采用最近一个完整交易日 2026 年 8 月 21 日收盘附近数据。**

**如果今天只能买一家公司，我选择：Bruker（NASDAQ: BRKR）。**

它并不是 AI for Science 中收入暴露最高的公司，也不是当前增长最快的公司；我选择它，是因为在“产业链关键程度 × 不可替代性 × 科学数据产生入口 × 市值弹性 × CEO/研发文化 × 当前估值”六项同时考虑后，**Bruker 是目前美股、港股上市公司中最接近“AI for Science 时代的科学测量设备基础设施”这一角色、同时又没有被估值完全透支的公司**。它更准确的半导体类比其实是 **KLA + 一部分 ASML**，而不是拥有 EUV 绝对垄断地位的 ASML。2026 年第二季度 Bruker Scientific Instruments 有机订单同比增长约 10%，book-to-bill 继续高于 1；而公司全年收入仍只有约 35 亿美元、市值约 91 亿美元，2026 年非 GAAP EPS 指引 $2.10–$2.15，对应当前约 28 倍前瞻 P/E。citeturn4search1turn0finance1

我的最终战略组合是：

**🥇 Bruker 31%  
🥈 GenScript 金斯瑞生物科技 24%  
🥉 Schrödinger 18%  
第四重仓 Tempus AI 17%  
第五重仓 XtalPi 晶泰控股 10%**

这不是“最纯 AI”的排名，而是**股票预期回报率**的排名。特别值得注意的是：Waters 的 Bottleneck Score 高于其中几家公司，Twist 的 AI for Science 收入弹性也非常高，但由于 Waters 的公司规模已经较大、Twist 当前估值极端提前反映增长，我没有把二者放入最终 Top 5。

## AI for Science 的产业地图、TAM 与真正的利润池

### 为什么我认为 AI for Science 是大周期，但不会复制纯 LLM 的路径

AlphaFold 已经证明，AI 可以把过去需要多年积累的结构生物学知识压缩成可计算的模型。AlphaFold 3 已经把预测范围扩展至蛋白、核酸、小分子、离子和修饰残基等几乎所有 PDB 常见分子类别；AlphaFold 数据库已经提供超过 2 亿个预测结构，并被 190 多个国家和地区的数百万研究人员使用。citeturn14search2turn14search3

但真正决定未来十年产业结构的并不是“模型预测越来越准”这一件事，而是另一件更重要的事情：

> **当生成一个 hypothesis、protein sequence、molecule 或 materials candidate 的边际成本趋近于零之后，真实世界验证的边际成本不会同步趋近于零。**

Nature 对自主实验系统的研究已经显示，自驱实验室可以把算法决策、机器人执行、测量、分析和下一轮实验设计连接成闭环；A-Lab 曾在 17 天连续运行中执行 353 个实验并合成 36 个目标材料。不过目前这些系统仍高度定制化，跨领域通用性与规模化仍是主要问题。citeturn14search0turn14search4turn14search16

更关键的是，2026 年关于实验互作组与 AlphaFold 类方法的研究仍显示：**AI 在真正 novel biological interaction 和受环境影响的构象变化上并不能完全替代实验数据。** 换言之，AI 把“设计”加速之后，瓶颈很可能向 synthesis、assay、proteomics、LC/MS、microscopy、automation、preclinical validation 和 manufacturing 转移。citeturn14search38turn14search30

所以我的核心产业判断是：

**LLM 的主要生产资料是 compute + data；AI for Science 的生产资料是 compute + scientific data + physical experiment。**

这使 AI for Science 产业链比纯互联网软件更像：

> **半导体产业链 + 云计算 + 自动化制造 + 生物医药研发**

的叠加体。

### 八层产业链

| 层级 | AI for Science 中的作用 | 最可能的瓶颈 | 主要上市公司 | 我的投资判断 |
|---|---|---|---|---|
| AI Compute | Foundation model、protein folding、MD、FEP、DFT、QM、simulation | GPU、HBM、网络、内存带宽 | NVDA、AMD、MU、AVGO、ANET、CRDO、COHR、LITE | 大产业赢家，但 **AI for Science 对总收入贡献太小** |
| Scientific Cloud / HPC | 数万次并行 simulation、virtual screening、科学 workflow | GPU capacity、HPC orchestration、data movement | AMZN、MSFT、GOOGL、ORCL、NVDA | 必要，但超大市值稀释 EPS 弹性 |
| Scientific Data | genomics、proteomics、clinical、imaging、real-world data | 高质量、可验证、proprietary data | TEM、ILMN、TXG、IQV | **重要利润池之一** |
| Scientific Instruments | LC/MS、proteomics、NMR、flow、microscopy、chromatography | 高端精密测量设备、application know-how | BRKR、WAT、TMO、A、DHR | **我认为是核心瓶颈层** |
| Laboratory Automation | AI agent → robot → instrument → data | robotics integration、sample handling、lab software | XtalPi、TMO、DHR、A 等 | **可能出现 VRT 式新利润池** |
| Consumables / Build | DNA、proteins、reagents、columns、assays、sample prep | high-complexity synthesis、质量、turnaround | GenScript、TWST、WAT、ILMN | **最接近 razor/blade** |
| AI Drug Discovery Platform | physics、generative chemistry、protein design、ADMET | proprietary workflows + validation | SDGR、RXRX、XtalPi、TEM | 高弹性，但模型 commoditization 风险大 |
| CRO / CDMO / CRDMO | 把更多候选物转化为真实开发项目 | preclinical、process、clinical、manufacturing | 2359、2269、CRL、IQV | AI 成功越多，后端 workload 越可能增长 |

### AI for Science 和普通 LLM 的算力需求并不完全相同

LLM 训练越来越强调低精度 Tensor throughput、HBM、all-reduce 和大规模网络，而科学计算同时存在大量 FP32/FP64、高精度数值模拟和 ensemble workloads。NVIDIA H100 本身专门提供 60 TFLOPS FP64 Tensor Core 运算能力；AWS 的生命科学 HPC 平台明确将 molecular dynamics、quantum mechanics、virtual screening 和 3D structure solution 作为主要科学工作负载。citeturn20search1turn20search2

尤其是 molecular dynamics，不一定是一台超大模型运行一次，而可能是**成千上万独立 simulation 并发**。AWS 曾公开展示 20,000 个 GROMACS simulations 的 ensemble workflow；NVIDIA 也在推动 ML interatomic potentials，把 AI 与原子级模拟融合。citeturn20search4turn20search3turn20search11

因此 AI for Science 确实继续利好 NVIDIA、Micron、Broadcom、Arista、Credo、Coherent 等，但我的股票排名没有把它们放在最前面，理由很简单：

**它们可能是更好的 AI 股票，却未必是对 AI for Science 增量收入最敏感的股票。**

例如，即使科学计算需求未来增长数倍，对一个已经达到数千亿美元甚至更高收入规模的 AI Infrastructure 公司，其 EPS 弹性仍可能远小于一家目前只有 3 亿–30 亿美元相关收入的 scientific-tools 公司。

### 我的未来十年 TAM 模型

下面不是引用某一家咨询机构的“AI Drug Discovery market size”，而是我将整个价值链按**可能被 AI for Science 增量拉动的可寻址支出**重新建模，因此不会把药物最终销售收入混入工具 TAM。

Waters 自己估算新组合公司的总 TAM 已约 $40B，其中原 Waters 业务约 $19B；IQVIA 同时显示，2025 年全球 biopharma R&D funding 仍处历史较高水平，AI 已开始在 R&D productivity 中形成可信信号。citeturn21search1turn21search2

| 利润池 | 我的 2026 可寻址支出估计 | 2035 模型区间 | 主要增长驱动 |
|---|---:|---:|---|
| Scientific compute / infrastructure | $15–25B | $60–100B | models + MD/QM + simulation |
| Scientific cloud / workflow / data infra | $8–12B | $35–60B | on-demand HPC、agents |
| Scientific data generation / licensing | $12–18B | $40–70B | multimodal data |
| Scientific instruments | $45–55B | $80–110B | more experiments/sample throughput |
| Lab automation / robotics | $8–12B | $35–60B | autonomous lab |
| Consumables / DNA / protein / sample prep | $45–60B | $100–150B | experiment frequency |
| AI scientific software / discovery platforms | $5–8B | $25–50B | physics + foundation models |
| AI-induced CRO/CDMO/validation workload | $60–80B | $120–180B | more candidate flow-through |

**合计：2035 年大约 $0.5–0.8 trillion 的“AI for Science 可影响/可重构支出池”。**

这里最大的错误方式，是把这 $0.5–0.8T 都称为“AI Revenue”。实际上它包括本来已经存在、但被 AI 显著加速或重新分配的 instrument、consumable、CRO、compute 和 clinical spending。因此我更愿意称之为**价值链可重构 TAM**。

真正最可能得到超额利润的并不是 TAM 最大的环节，而是：

**新增实验量 × 产能/技术瓶颈 × 高转换成本 × 高毛利耗材 × 软件附着率。**

这与半导体最值得投资的环节往往不是最大 TAM，而是 EUV、HBM、CoWoS、optics 的逻辑高度相似。

## 从一百多家公司到十五家公司

本研究的初始 screening universe 覆盖超过 100 家美股、港股上市公司，跨越 AI accelerator、memory、optical、networking、cloud、scientific software、sequencing、proteomics、mass spectrometry、chromatography、automation、bioprocessing、CRO/CDMO、clinical data 和 AI-native biotech。

筛选时我主动淘汰了三类公司。

第一类是 **好公司但 AI for Science EPS 弹性太低**，典型包括 NVIDIA、Microsoft、Amazon、Thermo Fisher、Danaher。第二类是 **AI exposure 很纯但最终价值取决于少数药物成功与否**，也就是普通 AI-native biotech。第三类是 **TAM 很大但没有结构性瓶颈、客户可以轻易 multi-source 的 commodity suppliers**。

以下 AI exposure 均为**我的经济暴露估算，不是公司披露收入分类**。

### 十五家公司候选池

| 公司 | Ticker | 当前市值 | 最新收入基准 | 增长 | Gross Margin / 经营指标 | AI for Science Exposure | Bottleneck | Moat | Overall |
|---|---|---:|---:|---:|---|---:|---:|---|---:|
| **Bruker** | BRKR | **$9.1B** | FY26E ~$3.55B | ~3–4% | ~50%；利润率恢复中 | 20–35% | **90** | Strong | **86** |
| **GenScript** | 1548.HK | **~HK$73B** | H1 26 $404M comparable | **+27.3%** | LSG adj GM ~57.8% | 20–35% | **84** | Strong | **84.5** |
| **Schrödinger** | SDGR | **$1.49B** | FY25 $255.9M | **+23.3%** | software GM 74% | **85–95%** | 66 | Strong software | **81** |
| **Tempus AI** | TEM | **$13.3B** | Q2 run-rate ~$1.53B | **+22%** | Q2 GM ~64% | 40–60% | 70 | Strong data | **80** |
| **Twist** | TWST | **$8.48B** | FY26E ~$456–457M | ~20%+ | Q3 GM 52.8% | 20–35% | **81** | Strong | **79** |
| **Waters** | WAT | **$40.3B** | FY26E $6.42–6.48B | org CC +7–9% | >70% recurring；FCF conversion >20% historically | 8–15% | **92** | **Extremely Strong** | **79** |
| **XtalPi** | 2228.HK | **~HK$33B** | H1 RMB394M | AI4S **+136%** | 当前仍亏损 | **80–95%** | 77 | Medium/Strong | **76.5** |
| **WuXi AppTec** | 2359.HK | ~HK$600B+ | FY26E RMB58.5–60.5B | continuing +35–39% | H1 adj GM 53.9% | 10–20% | **82** | Strong | **77** |
| WuXi Biologics | 2269.HK | ~HK$210B | FY25 RMB21.8B | +16.7% | GM 46% | 5–15% | 79 | Strong | 75 |
| 10x Genomics | TXG | $8.47B | Q2 run-rate ~$604M | +3% ex settlement | GM 74% | 25–40% | 72 | Strong | 74 |
| Illumina | ILMN | $33.6B | FY26E ~$4.6B | Q2 +9.5% | GM 66.4% | 15–25% | 80 | Strong | 73 |
| Agilent | A | $45.0B | FY26E ~$7.4B | core +4.5–6% | high-50-ish recurring/tool mix | 8–15% | 74 | Strong | 72 |
| IQVIA | IQV | $43.5B | Q2 run-rate ~$17.5B | +8.7% | adj EBITDA ~$1B/Q | 10–20% | 76 | Strong data/network | 70 |
| Charles River | CRL | $14.2B | Q2 run-rate ~$4.0B | organic ~flat | adj OM 20.5% Q2 | 10–20% | 75 | Medium/Strong | 69 |
| Recursion | RXRX | $1.86B | milestone/collaboration revenue | lumpy | 亏损 | **90%+** | 45 | Medium | 64 |

Bruker 当前市值和股价来自 8 月 21 日市场数据；公司 2026 年收入指引约 $3.54–3.57B、EPS $2.10–2.15，第二季度 BSI 有机 bookings 增长约 10%。citeturn0finance1turn4search1 GenScript 上半年 comparable revenue $404.2M、同比 +27.3%，Life Science Group 收入 $319M、调整后营业利润 $94M、同比增长 102.8%，而 AI-enabled drug discovery 业务连续第三个半年度高速增长并再次同比翻倍。citeturn16search2turn24search9

Schrödinger 2025 年总收入 $255.9M、同比 +23.3%，软件收入 $199.5M、软件毛利率 74%；2026 年二季度仍维持软件 ACV 双位数增长目标。citeturn24search2turn2search3 Tempus 二季度收入 $382.5M、同比 +22%，其中 Data & Applications $93.2M、同比 +28%，gross profit $246.5M。citeturn11search0turn0finance7

Waters 二季度合并收入 $1.645B，原 Waters organic revenue 在 constant currency 下增长 9%；公司将 2026 年收入指引提高至 $6.415–6.476B、调整后 EPS $14.45–$14.65。citeturn22search1turn0finance0 XtalPi 2026 年上半年收入 RMB393.6M；剔除上年大额 upfront payment 的高基数后，基础业务增长约 73.8%，AI4S Intelligent Solutions 收入 RMB193.5M、同比 +136.4%，同时 R&D 同比增加 66%，期末现金约 RMB8.67B。citeturn24search7turn24search35

Illumina 二季度收入 $1.159B、同比 +9.5%，GAAP gross margin 66.4%；Agilent 二季度收入 $1.83B、同比 +10%，FY26 core growth 指引 4.5–6%。citeturn3search1turn3search0 IQVIA 二季度收入 $4.368B、同比 +8.7%，调整后 EBITDA $994M、EPS +12.1%；Charles River 二季度 organic revenue 基本持平，但调整后 operating margin 已恢复至 20.5%。citeturn10search2turn11search13

### 为什么大型 AI 基础设施公司没有进入十五强

这不是因为 NVIDIA、Micron、Broadcom、Arista、Credo、Coherent 不会受益，而是它们现在的估值和收入主要由**整体 AI infrastructure cycle**决定。

如果 AI for Science 的 GPU/HBM/optical spending 从例如 $10B 增至 $50B，这个增量被 GPU、cloud、networking、memory 多个环节分走以后，对数千亿美元级别基础设施企业的 incremental EPS 影响未必高于一个当前收入 $300M–$3B 的科学工具供应商。

换句话说：

**NVIDIA 是“AI 的 NVIDIA”；我要找的是“AI for Science 自己的 NVIDIA”。**

### 估值快照

以下 EV/Sales、EV/EBITDA 属于基于最新市值、公司指导和净债务状况做的近似值，目的在于跨公司比较，而不是替代 Bloomberg/FactSet 实时 consensus。

| 公司 | P/E | Forward P/E | EV/Sales | EV/EBITDA | FCF Yield | 我的估值判断 |
|---|---:|---:|---:|---:|---:|---|
| BRKR | GAAP N/M | **~28x adj** | ~3x | ~18–20x | ~2–3% | **合理偏低** |
| GenScript | GAAP N/M | ~60–75x normalized | ~9–11x | N/M | ~2–4% normalized | 中高 |
| SDGR | N/M | N/M | **~4x ex net cash** | N/M | 负 | **期权价值高** |
| TEM | N/M | N/M | ~8x | N/M | 接近转正 | 高但可解释 |
| TWST | N/M | N/M | **~18x** | N/M | 负/接近 breakeven | **明显昂贵** |
| WAT | GAAP distorted | **~28x adj** | ~7x | ~20x+ | >3% | 合理至偏高 |
| XtalPi | N/M | N/M | **~30x EV/TTM sales** | N/M | 负 | **非常昂贵** |
| WuXi AppTec | profitable | ~mid-20s | ~8–9x | high-teens | positive | 增长调整后尚可 |
| WuXi Bio | ~37x | ~30s | ~8x | high-teens | positive | 中高 |
| TXG | N/M | N/M | ~13x | N/M | 负 | 高 |
| ILMN | ~40x | ~40x | ~7x | high-20s+ | ~2% | 高 |
| A | ~32x | mid/high-20s | ~6x | ~20x | ~2–3% | 合理偏高 |
| IQV | ~32x GAAP | high-teens/low-20s adj | ~3–4x | mid-teens | ~4% | **较合理** |
| CRL | GAAP N/M | low/mid-20s adj | ~4x | mid-teens | positive | **价值型** |
| RXRX | N/M | N/M | revenue multiple N/M | N/M | 大幅负 | binary |

Bruker 当前 $59.56、市值约 $9.07B；Schrödinger $19.71、市值约 $1.49B；Tempus $72.69、市值约 $13.26B；Twist $145.59、市值约 $8.48B。citeturn0finance1turn0finance5turn0finance7turn0finance6

这里已经出现一个重要结论：

> **Twist 是更纯的 AI-for-Science “build layer”资产，但 Bruker 是明显更好的当前 risk/reward。**

这就是为什么“技术逻辑最好”不能等于“股票最值得买”。

## Top 8 深度分析：护城河、竞争、CEO 与经营杠杆

### Top 8 的完整 100 分评分

| 公司 | 产业重要性 15 | 护城河 15 | 竞争力 10 | AI 弹性 15 | TAM 10 | CEO 10 | 财务 10 | Op Lev. 5 | 估值 5 | 10× 5 | 总分 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **BRKR** | 14.0 | 13.5 | 8.5 | 10.0 | 9.0 | **9.5** | 8.0 | 4.5 | 4.5 | 4.5 | **86.0** |
| **GenScript** | 13.5 | 12.0 | 8.5 | 13.0 | 9.0 | 7.5 | 8.5 | **5.0** | 3.5 | 4.0 | **84.5** |
| **SDGR** | 11.5 | 11.0 | 7.5 | **14.5** | 9.0 | 9.0 | 6.0 | 4.5 | 4.0 | 4.0 | **81.0** |
| **TEM** | 11.0 | 11.0 | 8.0 | 13.0 | 9.0 | 8.0 | 7.0 | 4.5 | 4.0 | 4.5 | **80.0** |
| **TWST** | 13.0 | 12.5 | 8.0 | 13.5 | 9.5 | 9.0 | 5.0 | 4.5 | **1.5** | 2.5 | **79.0** |
| **WAT** | **15.0** | **14.0** | 9.0 | 6.0 | 9.0 | 9.0 | **9.5** | 4.0 | 3.0 | 0.5 | **79.0** |
| **WuXi AppTec** | 13.0 | 12.0 | 9.0 | 7.0 | 9.0 | 9.0 | 9.5 | **5.0** | 2.0 | 1.5 | **77.0** |
| **XtalPi** | 12.5 | 10.0 | 7.0 | **15.0** | **10.0** | 8.0 | 4.0 | **5.0** | **1.0** | 4.0 | **76.5** |

**Bruker——我认为最值得做第一重仓。**  
Bruker 的核心不是“有一个 AI 产品”，而是 timsTOF、TIMS/PASEF mass spectrometry、NMR、microscopy/materials characterization 等一系列直接产生 scientific ground-truth data 的设备。最新 timsUltra AIP 面向 single-cell proteomics、immunopeptidomics 和 metaproteomics；Bruker 也已经把用超过 700 万条 MS/MS spectra 训练的 AI-enhanced de novo sequencing 嵌入 ProteoScape workflow。citeturn18search9turn18search1

更重要的是，它正在从传统“仪器周期股”向高 throughput biology 靠近。timsTOF MALDI PharmaPulse 已被设计为全自动、label-free、可用于 millions-of-compounds screening 的高通量系统，这正好是 AI model 产生海量 candidate 后需要的 physical screening layer。citeturn18search19

竞争对手包括 Thermo Fisher、Waters、Agilent、Danaher/SCIEX 和 JEOL。这个行业不会 winner-take-all，而会形成**若干极强 niche oligopolies**。因此 Bruker 不是 ASML 式 90%+ monopoly，但其 IP、ion mobility know-how、installed workflows、academic ecosystem 和 application expertise 形成较强 switching cost。

CEO Frank Laukien 从 1991 年起领导 Bruker，同时是公司最大股东，拥有 MIT physics 和 Harvard chemical physics 背景。这是少数仍由科学家型长期 CEO 领导的大型上市 scientific-instrument 公司。citeturn18search3 2025 年公司 R&D 约 $395M、约占收入 11.5%，明显高于许多成熟工具公司。citeturn24search20turn24search24

它的问题也很明显：2024–2025 的收购使 operating margin 一度下降，FY25 非 GAAP EPS 从 FY24 的 $2.41 降至 $1.83；因此现在其实是在押注 **organic orders recovery + acquisition digestion + margin normalization**。citeturn18search6turn24search0 正因为市场仍把它当成“执行有问题的仪器公司”，我认为才存在重估空间。

**GenScript——最像“AI 生物学的 TSMC-style wet-lab fab”。**  
AI 可以在几分钟内生成一万个 protein sequence，但这些 sequence 必须被写出来、表达、纯化、检测、验证。GenScript 的 Gene-to-Protein platform 把 gene synthesis、protein/antibody expression、engineering 和 validation 串在一起，这正是“digital design → physical biology”的 build layer。公司截至目前已服务超过 20 万客户、覆盖 100 多个国家和地区。citeturn16search6

最重要的实证数据已经出现：2026 年上半年 AI-enabled drug discovery 业务同比再次翻倍，已经是连续第三个半年度高速增长；Life Science Group 收入 +28.8%，调整后 gross profit +46.1%，adjusted operating profit **+102.8%**。citeturn16search2 这几乎就是用户要求寻找的：

> Revenue +20–30% → EBIT/EPS +50–100%

式 operating leverage。

其竞争对手包括 Twist、Danaher/IDT、Thermo Fisher、Eurofins 以及大量 regional synthesis providers。市场不是 winner-take-all，因此我给其 moat 12/15 而不是 15/15。真正的护城河来自**全球交付、complex construct execution、自动化、从 DNA 到 protein 的 workflow breadth，以及客户关系规模**，不是一个不可复制的单项专利。

CEO 项目我反而扣分。当前 GenScript 采用 rotating CEO 架构，Sherry Shao 为当值 Rotating CEO；虽然近年执行出色，但这种组织模式不像 Jensen、Lisa Su、Morris Chang 或 Frank Laukien 那样具有高度可识别的长期资本配置责任人。citeturn16search25turn16search2

此外，GenScript 的美国地缘政治风险不能忽视。美国国会部分议员过去曾就 GenScript 与中国的关系提出国家安全和知识产权担忧。citeturn16search10turn16search13 这是估值折价应长期存在的原因之一。

**Schrödinger——不是硬件瓶颈，但可能是 Top 5 中最好的 10× option。**  
Schrödinger 的优势在于 physics-based molecular modeling、FEP、software ecosystem 与 drug-discovery platform 的长期积累，而不是简单套一个生成式 AI UI。2025 年 software revenue $199.5M、同比 +10.6%，software gross margin 74%；drug discovery revenue 从 $27.2M 增至 $56.4M，使总收入增长 23.3%。citeturn24search2

当前市值只有约 $1.49B，而年末现金约 $402M，因此企业价值远低于大多数上市 AI biotech。citeturn0finance5turn24search2 如果 hosted software ACV 最终从约 $200M 级别向 $600M–$1B 发展，并同时形成多个有经济价值的 drug-discovery milestones，它的 10× 数学要比 Waters、Illumina 或 WuXi AppTec 容易得多。

但它的 Bottleneck Score 只有 66。原因是客户可以采用 open-source RDKit、internal computational chemistry、Cadence/OpenEye、Dassault/BIOVIA、XtalPi、AI foundation models 等替代路径。因此 **SDGR 是高质量 scientific software platform，却不是不可绕过的 ASML**。

**Tempus AI——真正稀缺的是 multimodal clinical data，而不是模型。**  
Tempus 二季度 diagnostics revenue $289.3M、同比 +20%，Data & Applications $93.2M、同比 +28%；gross profit $246.5M、同比 +26%，同时新增 Data & Applications contracts 约 $200M。citeturn11search0

其真正的 moat 不是“会训练 AI”，而是：

**diagnostic tests → proprietary multimodal clinical/genomic data → pharma models/insights → more commercial relationships → more data。**

这是一种数据 flywheel。

竞争对手是 Guardant、Natera、Roche/Foundation Medicine/Flatiron、Caris 以及 pharma 内部数据体系。Tempus 不具备不可替代 monopoly，所以 Bottleneck Score 只有 70；但在所有 Top 8 中，它已经是少数**同时具备 20%+ revenue growth、60%+ gross margin 和接近盈利拐点**的 AI-native scientific-data 公司。citeturn11search0turn11search20

**Twist——产业位置极好，股票价格却提前反映了很多成功。**  
Twist 的 silicon-based DNA synthesis platform 是非常漂亮的 AI-for-Science “write layer”。AI 可以生成任意 protein/DNA design，但最终必须把序列物理制造出来。公司甚至明确指出，AI-enabled drug discovery 需要制造越来越复杂的 AI-generated sequences，而不是为了可制造性重新修改设计。citeturn19search17turn19search16

2026 财年前三季度继续体现这一趋势，DNA Synthesis & Protein Solutions 增长明显快于 NGS，gross margin 已升到 50% 以上；公司还被 AWS 选为 Amazon Bio Discovery 的 wet-lab partner。citeturn11search3turn19search19

CEO Emily Leproust 是 co-founder，长期坚持 silicon DNA manufacturing 路线，而且公司已经把长期烧钱的 DNA data storage 业务剥离为独立公司，这反映出资本配置纪律正在改善。citeturn23search2turn23search20

问题只有一个，但非常严重：

**约 $8.5B 市值对 FY26 约 $456–457M revenue，估值接近 19× sales。**

因此，即使我非常喜欢业务，也不能把股票放进当前 Top 5。它需要的不只是“业务很好”，而是**未来十年持续接近 25–30% CAGR + 20–30% margin**才能支撑真正巨额回报。

**Waters——Bottleneck Score 第一，但不是股票回报第一。**  
Waters 的 instrumentation、chromatography、MS、chemistry、Empower software 和现在新增的 BD Biosciences/Diagnostic Solutions 构成非常深的 regulated workflow installed base。公司称超过 70% revenue 是 annual recurring，历史上超过 20% revenue 可转化为 FCF；内部估算约 80% 在 FDA、EMA 和 NMPA 提交的药物使用 Waters Empower software。citeturn4search2turn21search1

如果实验量增长 10 倍，客户当然可以买 Agilent、Thermo 或 SCIEX，但在 regulated pharma QA/QC 中，更换 instrument + method + software 的 switching cost 很高。因此它是我的 **Bottleneck Score 92/100，所有候选第一名。**

我却没有把它放入 Top 5，因为最新组合公司 FY26 revenue 已约 $6.4B、市值约 $40B。citeturn22search1turn0finance0 AI for Science 即使增加 $1B–$2B incremental revenue，也不像对 $1B–$3B 小公司那样改变公司价值。

这是本研究最重要的纪律：

> **最高护城河 ≠ 最大股票上涨弹性。**

**XtalPi——最接近“VRT of Autonomous Labs”，但估值已经非常激进。**  
XtalPi 正在把 AI agents、physics-based modeling 和 robotics 放在同一技术栈中，其平台目标就是 autonomous molecular discovery。citeturn22search0

2026 年 H1 的 AI4S Intelligent Solutions revenue 增长 136.4%，同时 R&D 同比 +66%，主要投向 autonomous laboratories、agent systems、pipeline programs 和 multimodal platforms。citeturn24search7turn24search35 这可能是整个港股中最直接的 AI-for-Science pure-play。

但当前约 HK$33B 市值对应的基础收入仍很小，收入受 licensing milestone 影响也很大。因此它是我的 **最高成长 optionality 之一，却只能配 10%**。

**WuXi AppTec——商业质量可能比排名更高，但 geopolitical discount 不能忽略。**  
2026 年上半年 WuXi AppTec revenue 达 RMB28.9B、同比 +38.9%，continuing operations 增长约 48%，adjusted non-IFRS gross margin 53.9%，adjusted net profit 增长 83.2%；公司把全年 revenue guidance 提升至 RMB58.5–60.5B。citeturn13search0turn13search12

如果 AI 把 candidate generation 加快数倍，药化、DMPK、tox、preclinical、development 和 manufacturing 并不会自动消失，因此 WuXi 的 CRDMO 模式确实是 downstream shovel。

问题是政治风险已经不再只是理论。BIOSECURE Act 已于 2025 年 12 月随 FY2026 NDAA 成为法律；2026 年 6 月 WuXi AppTec 被加入美国 1260H 清单，虽然 2026 年 8 月 7 日美国法院已发出 preliminary injunction 阻止该项 designation 的执行，但 broader BIOSECURE implementation 的不确定性仍存在。citeturn23search0turn23search6

所以它在我的产业评分很高，在股票排名却只有第八。

## Bottleneck、CEO、护城河、收入弹性与十倍路径排名

### Bottleneck Score

这是我认为本报告最重要的一张表。问题只有一个：

> **假设 AI for Science 总实验/计算/开发 activity 增长 10×，客户是否能绕过这家公司？**

| 排名 | 公司 | Bottleneck Score | 类比 |
|---:|---|---:|---|
| 1 | **Waters** | **92** | regulated analytical infrastructure |
| 2 | **Bruker** | **90** | high-end scientific metrology |
| 3 | **GenScript** | **84** | outsourced biological build/validation |
| 4 | **WuXi AppTec** | **82** | outsourced R&D “fab” |
| 5 | **Twist** | **81** | biological writing substrate |
| 6 | Illumina | 80 | sequencing installed-base ecosystem |
| 7 | WuXi Biologics | 79 | biologics manufacturing |
| 8 | XtalPi | 77 | autonomous-lab orchestration |
| 9 | IQVIA | 76 | clinical/data network |
| 10 | Charles River | 75 | preclinical validation |
| 11 | Agilent | 74 | analytical workflow |
| 12 | 10x Genomics | 72 | single-cell/spatial data |
| 13 | Tempus | 70 | multimodal clinical data |
| 14 | Schrödinger | 66 | scientific software |
| 15 | Recursion | 45 | drug/platform outcome dependent |

这里的核心洞见是：

**AI-native ≠ bottleneck。**

Recursion 和 Schrödinger 对 AI 的纯暴露远远高于 Waters，但客户绕过它们的可能性也明显更高。

### 技术护城河排名

我的前五名是：

**Waters 93  
Bruker 90  
WuXi AppTec 84  
GenScript 82  
Twist 82**

Waters 的优势来自 regulated installed base、method/software qualification、Empower ecosystem 和 recurring consumables；Bruker 来自 highly specialized instrumentation、TIMS/PASEF know-how、academic/scientific ecosystem；WuXi 来自规模、process know-how、global project execution；GenScript 来自 integrated Gene-to-Protein workflow；Twist 来自 silicon DNA synthesis manufacturing architecture。citeturn21search1turn18search9turn16search2turn19search17

未来五年，这些行业整体上不会变成 winner-take-all。真正可能出现的结构是：

**总体 fragmented，细分技术路线 oligopoly。**

这和 semiconductor equipment 比较接近：不是整个行业只有 ASML，而是 EUV、etch、deposition、metrology 各有高度集中节点。

### CEO / 管理层排名

| 排名 | 公司 | CEO Score | 核心判断 |
|---:|---|---:|---|
| 1 | **Bruker / Frank Laukien** | **9.5** | scientist-founder mindset、35 年战略连续性 |
| 2 | Twist / Emily Leproust | 9.0 | founder、silicon synthesis 长期下注 |
| 3 | Schrödinger | 9.0 | scientific-software 长期主义 |
| 4 | Waters / Udit Batra | 9.0 | execution + 大型组合重构 |
| 5 | WuXi AppTec | 9.0 | 全球 CRDMO 执行能力 |
| 6 | Tempus | 8.0 | founder-led data platform |
| 7 | XtalPi | 8.0 | scientific founder culture |
| 8 | GenScript | 7.5 | 执行强，但 rotating CEO 模式降低责任人清晰度 |

Waters 的资本配置尤其值得关注：2026 年已完成 BD Biosciences/Diagnostic Solutions 的大型组合，新增业务在合并后的第一个完整季度即实现 mid-single-digit comparable growth，并且公司连续提高全年 guidance。citeturn22search1turn22search2

Bruker 则相反：过去两年 aggressive M&A 先造成 margin dilution，因此它的 CEO 得分高，但**资本配置近期成绩不是满分**。这也是投资逻辑必须监控的地方。citeturn18search6

### AI for Science Revenue Elasticity

如果只看“AI for Science 增长三倍以后，对公司收入变化比例”，排序与 Bottleneck 完全不同：

| 公司 | 我的 Exposure / Elasticity Score | 原因 |
|---|---:|---|
| Recursion | 97 | 几乎全部 thesis 都是 AI-native discovery |
| XtalPi | **96** | AI + robotics 几乎就是公司战略核心 |
| Schrödinger | **93** | scientific modeling / drug discovery |
| Twist | **88** | AI design 增加 DNA/protein build demand |
| Tempus | **84** | data/AI/precision medicine |
| GenScript | **82** | wet-lab validation 已出现 AI customer pull |
| Bruker | 65 | incremental scientific experiment volume |
| 10x Genomics | 63 | multimodal biology data generation |
| Waters | 45 | 很大一部分业务是 QA/QC/diagnostics |
| WuXi AppTec | 42 | AI 是 workload driver，不是收入分类 |

GenScript 是少数已经出现真实财务 evidence 的公司：AI-enabled drug discovery 业务连续多个半年度高速增长，而 2026H1 Life Science Group adjusted operating profit 的增幅是收入增幅的约 3.5 倍。citeturn16search2

### 10× Potential Test

| 公司 | 当前市值 | 10× 市值 | 我认为需要的长期状态 | 10× Probability |
|---|---:|---:|---|---|
| **BRKR** | $9.1B | ~$91B | revenue ~$10–11B；net margin 22–23%；P/E ~38–40x | **15–30%** |
| **GenScript** | ~$9.3B eq. | ~$93B | revenue ~$9B；net margin ~28%；P/E ~45x | **15–30%** |
| **SDGR** | $1.49B | ~$14.9B | revenue ~$1.3–1.5B；net margin 25–28%；P/E 45–50x | **15–30%** |
| **TEM** | $13.3B | ~$133B | revenue ~$9.5–10.5B；net margin 25–28%；P/E ~50x | **15–30%** |
| **XtalPi** | ~$4.25B eq. | ~$42.5B | revenue ~$2.4–2.7B；net margin ~30%；P/E 60x+ | **5–15%** |
| TWST | $8.5B | ~$85B | revenue ~$5.5–6B；net margin ~30%；P/E ~45–50x | **5–15%** |
| WAT | $40B | ~$400B | revenue $25B+、very high margin | **<5%** |
| WuXi AppTec | ~$79B eq. | ~$790B | requires global mega-cap pharma-services outcome | **<5%** |

最有意思的是 SDGR。

它并不需要成为世界最大的 pharmaceutical company。由于当前 enterprise value 只有约 $1B 左右量级，**只要最终形成 $1B+ software/discovery ecosystem，并实现成熟软件经济学，其 10× 数学就开始成立。**

而 Waters 即使是全行业最不可替代的公司之一，10× 路径也已经非常困难。

这正是“公司质量”和“股票上行空间”的区别。

## Top 5 Bear / Base / Bull / Super Bull 情景估值

下面全部是**我的模型，而非公司 guidance 或 Wall Street consensus**。为了让美股和港股可比较，统一采用美元计量；港股模型假设长期汇率近似固定，仅用于估值归一化。

每个年份单元格依次为：

**Revenue ($B) / Operating Margin / EPS / FCF ($B) / Target Market Cap ($B)**

CAGR 是从 2026 年当前市值到 2032 年目标市值的年化回报，不包含股息。

### Bruker

Bruker 的现实起点是 FY26 revenue 约 $3.55B、non-GAAP EPS $2.10–2.15，而市值只有约 $9.1B。citeturn4search1turn0finance1

| 情景 | 2027 | 2028 | 2030 | 2032 | 2032 市值 CAGR |
|---|---|---|---|---|---:|
| Bear | 3.70 / 13% / 2.53 / .33 / 8.9 | 3.90 / 14% / 2.87 / .39 / 9.9 | 4.40 / 15.5% / 3.58 / .51 / 12.0 | 5.00 / 17% / 4.47 / .65 / **15.0** | **8.7%** |
| **Base** | 3.90 / 16% / 3.28 / .43 / 16.8 | 4.30 / 17.5% / 3.95 / .56 / 19.8 | 5.40 / 20% / 5.67 / .86 / 27.6 | 6.80 / 22.5% / 8.04 / 1.29 / **39.2** | **27.6%** |
| Bull | 4.10 / 17% / 3.66 / .49 / 22.2 | 4.70 / 19% / 4.69 / .66 / 28.0 | 6.50 / 23% / 7.85 / 1.17 / 45.4 | 8.50 / 25% / 11.16 / 1.79 / **64.6** | **38.7%** |
| Super Bull | 4.30 / 18% / 4.07 / .56 / 27.3 | 5.00 / 21% / 5.52 / .80 / 36.3 | 7.50 / 25% / 9.85 / 1.58 / 63.0 | 10.50 / 28% / 15.44 / 2.52 / **98.8** | **48.9%** |

**这就是我把 BRKR 放第一的根本原因。** 它不需要 30% revenue CAGR 才能得到非常好的回报；只需要 instrument cycle recovery、proteomics share gains、margin restoration 和长期 10% 左右 growth 的组合，Base Case 就有较大的 operating + valuation leverage。

### GenScript

GenScript 的起点是 H1 2026 comparable revenue $404.2M，Life Science Group revenue +28.8%，adjusted operating profit +102.8%。citeturn16search2

| 情景 | 2027 | 2028 | 2030 | 2032 | CAGR |
|---|---|---|---|---|---:|
| Bear | 1.00 / 12% / .05 / .06 / 2.6 | 1.10 / 13% / .06 / .08 / 3.0 | 1.35 / 14% / .07 / .11 / 3.9 | 1.60 / 15% / .09 / .14 / **4.9** | **-10.1%** |
| **Base** | 1.10 / 17% / .07 / .11 / 5.6 | 1.35 / 19% / .10 / .16 / 7.6 | 2.00 / 22% / .17 / .30 / 12.6 | 2.90 / 25% / .28 / .55 / **20.8** | **14.3%** |
| Bull | 1.20 / 18% / .08 / .13 / 7.4 | 1.55 / 21% / .13 / .22 / 11.0 | 2.60 / 25% / .25 / .47 / 21.3 | 4.00 / 28% / .44 / .88 / **36.7** | **25.7%** |
| Super Bull | 1.30 / 20% / .10 / .16 / 10.1 | 1.80 / 23% / .16 / .29 / 15.7 | 3.40 / 28% / .37 / .71 / 35.1 | 5.50 / 30% / .64 / 1.32 / **60.9** | **36.7%** |

这里要特别诚实：**当前股价已经开始 price in AI wet-lab thesis。**

所以 GenScript 是第二重仓，而不是第一。它需要持续 20%+ growth，才能让当前 valuation 合理地 compound。

### Schrödinger

公司 2025 年 software gross margin 74%，year-end cash 约 $402M，而当前 equity market cap 约 $1.49B。citeturn24search2turn0finance5

| 情景 | 2027 | 2028 | 2030 | 2032 | CAGR |
|---|---|---|---|---|---:|
| Bear | .28 / -45% / -1.66 / -.11 / 1.2 | .30 / -35% / -1.39 / -.09 / 1.3 | .36 / -20% / -.95 / -.06 / 1.5 | .42 / -10% / -.55 / -.03 / **1.7** | **1.8%** |
| **Base** | .34 / -30% / -1.35 / -.09 / 1.8 | .40 / -18% / -.95 / -.05 / 2.0 | .58 / 5% / .31 / .02 / 3.3 | .85 / 18% / 1.62 / .13 / **4.7** | **21.0%** |
| Bull | .38 / -25% / -1.25 / -.08 / 2.3 | .48 / -10% / -.63 / -.02 / 2.8 | .78 / 15% / 1.24 / .09 / 4.7 | 1.25 / 25% / 3.30 / .28 / **11.7** | **40.8%** |
| Super Bull | .42 / -20% / -1.11 / -.06 / 2.9 | .56 / -5% / -.37 / 0 / 3.8 | 1.00 / 22% / 2.32 / .18 / 9.5 | 1.65 / 30% / 5.23 / .45 / **20.2** | **54.4%** |

这里的 asymmetry 非常明显：

**Bear 情景公司仍有现金缓冲；Super Bull 可以真正超过 10×。**

所以它虽然 Bottleneck Score 明显低于 Waters，却进入最终第三重仓。

### Tempus AI

Tempus 的当前 revenue run-rate 已超过 $1.5B，Q2 revenue +22%，Data & Applications +28%。citeturn11search0

| 情景 | 2027 | 2028 | 2030 | 2032 | CAGR |
|---|---|---|---|---|---:|
| Bear | 1.75 / 0% / .00 / -.04 / 10.5 | 2.00 / 3% / .26 / .00 / 10.0 | 2.60 / 8% / .91 / .13 / 10.4 | 3.20 / 10% / 1.40 / .22 / **7.7** | **-8.7%** |
| **Base** | 1.90 / 4% / .33 / .04 / 15.2 | 2.35 / 8% / .82 / .12 / 16.4 | 3.60 / 14% / 2.21 / .36 / 14.5 | 5.20 / 18% / 4.11 / .73 / **26.2** | **12.0%** |
| Bull | 2.10 / 6% / .55 / .08 / 18.9 | 2.70 / 10% / 1.18 / .19 / 10.4 | 4.70 / 18% / 3.71 / .66 / 31.4 | 7.50 / 24% / 7.90 / 1.43 / **64.8** | **30.3%** |
| Super Bull | 2.30 / 8% / .81 / .11 / 23.0 | 3.10 / 13% / 1.77 / .31 / 17.2 | 6.00 / 22% / 5.79 / 1.08 / 54.4 | 10.00 / 28% / 12.28 / 2.30 / **112.0** | **42.7%** |

Tempus 的风险收益是一个典型的“execution multiple”。

如果最后只是一家 fast-growing diagnostics company，目前价格并不便宜；如果 clinical/genomic multimodal data 真正成为 pharmaceutical AI 的核心训练和 evidence infrastructure，它则有进入 $50–100B market-cap class 的可能。

### XtalPi

XtalPi H1 2026 AI4S revenue +136.4%，但公司仍录得 RMB224.9M net loss，而且当前市值对 revenue 已极高。citeturn24search7turn24search35

| 情景 | 2027 | 2028 | 2030 | 2032 | CAGR |
|---|---|---|---|---|---:|
| Bear | .12 / -45% / -.01 / -.05 / 3.4 | .14 / -30% / -.01 / -.04 / 3.2 | .18 / -10% / -.00 / -.01 / 3.0 | .23 / 5% / .00 / .01 / **3.0** | **-5.4%** |
| **Base** | .15 / -25% / -.01 / -.03 / 4.5 | .21 / -10% / -.00 / -.01 / 5.0 | .38 / 8% / .01 / .02 / 6.5 | .72 / 18% / .03 / .10 / **6.7** | **7.9%** |
| Bull | .18 / -18% / -.01 / -.03 / 5.7 | .28 / 0% / .00 / .00 / 7.4 | .65 / 18% / .02 / .09 / 6.8 | 1.30 / 25% / .06 / .26 / **16.4** | **25.2%** |
| Super Bull | .22 / -10% / -.01 / -.02 / 7.8 | .38 / 8% / .01 / .02 / 11.8 | .95 / 24% / .05 / .18 / 14.2 | 2.20 / 30% / .13 / .55 / **37.7** | **43.8%** |

这张表解释了我为什么只给它 **10% weight**：

**产业逻辑可以极度 bullish，但当前 valuation 已经要求未来发生非常大的商业成功。**

它是一个 call option，不是 core compounder。

## Top 5 组合、买入区间、Reverse Thesis 与 Kill Criteria

### 一百万元人民币的最终组合

| 排名 | 股票 | 类型 | 权重 | 100 万元配置 | 为什么不是 20% |
|---|---|---|---:|---:|---|
| 🥇 | **Bruker** | Compounder + bottleneck | **31%** | **31 万元** | 最优 moat/valuation/size 组合 |
| 🥈 | **GenScript** | Compounder + high elasticity | **24%** | **24 万元** | AI wet-lab evidence 已出现，但 geopolitics/valuation 要折价 |
| 🥉 | **Schrödinger** | Explosive Growth | **18%** | **18 万元** | 小市值、net cash、10× 数学最好之一，但 bottleneck 较弱 |
| 4 | **Tempus AI** | Data compounder | **17%** | **17 万元** | data flywheel 强，但当前 valuation 已不低 |
| 5 | **XtalPi** | Explosive Growth / optionality | **10%** | **10 万元** | 最大纯度、最大增长可能，也有最大估值/执行风险 |

不是平均 20%，因为五家公司并不存在相同的 downside。

BRKR 的 downside 主要是 instrument cycle 和 M&A execution；XtalPi 的 downside 则可能是 autonomous-lab commercialization 远低于市场预期，从而造成 sales multiple 巨幅压缩。因此，即使 XtalPi 的 Super Bull upside 很大，也不应该得到和 BRKR 相同的资本。

### 买入区间

这些区间基于前述长期模型和我要求的至少约 12–15% base-case expected return，而不是技术分析。

| 公司 | 当前价格 | Strong Buy | Buy | Hold | Reduce | 当前动作 |
|---|---:|---:|---:|---:|---:|---|
| **BRKR** | **$59.56** | **< $55** | **$55–68** | $68–85 | >$85 | **BUY** |
| **GenScript** | **~HK$34.4** | **< HK$27** | **HK$27–36** | HK$36–45 | >HK$45 | **BUY** |
| **SDGR** | **$19.71** | **< $17** | **$17–24** | $24–34 | >$34 | **BUY** |
| **TEM** | **$72.69** | **< $55** | **$55–75** | $75–100 | >$100 | **BUY，接近上沿** |
| **XtalPi** | **~HK$7.71** | **< HK$5.5** | **HK$5.5–8.0** | HK$8–10.5 | >HK$10.5 | **BUY，严格控制仓位** |

美股当前价来自 2026 年 8 月 21 日市场数据。citeturn0finance1turn0finance5turn0finance7 港股 GenScript 与 XtalPi 最近股价分别约 HK$34.4 和 HK$7.7。citeturn15search5turn15search8

### Reverse Thesis 与 Kill Criteria

| 公司 | 为什么这笔投资可能是错的 | Kill Criteria |
|---|---|---|
| **BRKR** | MS/proteomics share 被 Thermo/Waters/SCIEX反超；2024–25 acquisitions 永久稀释 margin；academic/pharma capex cycle 长期疲弱 | BSI book-to-bill 连续 4 季 <1；organic growth 连续 6 季落后行业；2028 adj OM 仍无法回到 17%+；net leverage 不下降 |
| **GenScript** | gene/protein services commoditize；美国客户因 geopolitical risk 转单；AI wet-lab 增长只是低基数 | AI-enabled business 连续两个半年度 <25% growth；LSG adj GM 跌破 ~52%；LSG op margin 重新跌至 mid-teens 以下；大型美国客户流失 |
| **SDGR** | AI foundation models / open source 侵蚀 software pricing；drug pipeline 不产生经济价值；长期 cash burn | software ACV <10% growth 持续；software GM <68%；没有明显 drug milestones；现金 runway 跌至 <2.5 年且仍无盈利路径 |
| **TEM** | genomic/clinical data 被 commoditize；reimbursement 压力；Guardant/Natera/Caris/Roche 建立更强闭环 | Data & Apps <20% growth；diagnostic volume <15%；GM <60%；2027 仍无法持续正 adjusted EBITDA |
| **XtalPi** | robotics deployment 不能规模化；license revenue 太 lumpy；中国 geopolitical discount 扩大 | AI4S growth 连续一年 <50%；cash burn >RMB1.5B/年；R&D 高增长但商业转化停滞；核心 pharma collaborations 不续约 |

Bruker 当前最需要监控的就是 orders-to-revenue conversion 和 margin；目前 BSI organic bookings 已转为约双位数增长，这是 thesis 重新向上的第一步，而不是最终证明。citeturn4search1

GenScript 的 thesis 则已经出现更直接的财务验证：H1 Life Science Group revenue +28.8%，adjusted operating profit +102.8%。因此，如果未来出现“收入仍增长但利润率开始持续下降”，反而是非常重要的负面信号。citeturn16search2

XtalPi 最大风险不是技术本身，而是**商业化速度不够快以支撑目前估值**。H1 公司一方面 AI4S 收入 +136.4%，另一方面 R&D 已上升 66%、净亏损超过 RMB200M，这说明投资者实际上正在为未来多年的高速规模化提前支付价格。citeturn24search7turn24search35

## 最终投资判断

### AI for Science 会不会形成 Semiconductor / Internet / Cloud / AI 级别的大产业周期？

**我的答案：会，但表现形式不同。**

它不太可能出现“一个 AI for Science software market 从 $10B 直接变 $1T”这种单一市场。

更可能发生的是：

**AI 把科学 hypothesis generation 降低几个数量级  
→ candidate 数量爆炸  
→ simulation 增长  
→ wet-lab experiments 增长  
→ scientific data 增长  
→ instrument utilization 增长  
→ consumables 增长  
→ preclinical/clinical workload 增长  
→ autonomous labs 渗透  
→ 更多真实数据反馈模型。**

这是一条正反馈链。

IQVIA 最新 R&D 数据已经显示 AI-enabled programs 开始出现 success-rate/productivity 的可信信号，但整体 clinical productivity 并未因此完全解决；trial duration 和 inter-trial intervals 仍存在明显摩擦。citeturn21search2

因此我认为 AI for Science 最重要的经济价值可能不是“少花钱研发同样多的药”，而是：

> **单位时间尝试的科学 hypothesis 数量大幅上升。**

如果这一点成立，工具链的收入反而可能增长，而不是下降。

### 未来十年最大利润池在哪里？

我认为最终价值最大的是四个池：

**第一：真实世界 scientific data generation。**  
Genomics、proteomics、imaging、clinical data 和 assay output 是下一代模型的 ground truth。

**第二：high-end analytical instruments + recurring consumables。**  
LC/MS、proteomics、chromatography、single-cell、flow、sample prep。这是 Waters、Bruker、Illumina、Agilent 等公司的区域。

**第三：automated wet-lab execution。**  
AI Agent 只有真正能驱动 robot → instrument → data → next experiment，才成为“AI Scientist”。Nature 对 autonomous lab 的研究已经证明闭环在特定领域可行，但 scalable generality 尚未解决，这意味着商业机会仍然很早。citeturn14search0turn14search8

**第四：downstream validation / CRDMO。**  
AI 不会自动完成 toxicology、PK/PD、clinical trial、process development、manufacturing。候选物数量上升反而可能增加这些 workload。

### 哪个环节最可能出现 Supply Bottleneck？

**第一名：high-end scientific measurement。**

如果 proteomics、spatial、mass spec throughput 突然增长 3×，客户无法像增加云服务器一样一夜之间增加 3× 熟练 MS/NMR capacity。制造是一个约束，但更大的约束是：

**instrument + application method + trained operator + workflow + service + data pipeline。**

这就是我给 Waters 92、Bruker 90 的原因。

**第二名：复杂 DNA/protein synthesis 与 biological validation。**

模型生成 molecule/protein 非常快；物理 manufacture、expression、purification、functional assay 没有同样的无限 scale。GenScript 与 Twist 是最直接的受益者。

**第三名：autonomous lab integration。**

硬件机器人本身未必有 moat，但把 liquid handling、chemical synthesis、measurement、AI agent、data model 串成可靠闭环，可能形成新的 systems-integration bottleneck。XtalPi 是目前上市公司中最直接的 pure-play 之一。citeturn22search0turn24search7

### 哪些公司真正拥有不可替代性？

严格意义上，我没有发现 AI for Science 上市股票中存在今天 ASML/EUV 那种 **“single-source near-monopoly”**。

这是整个研究最需要避免的过度类比。

相对不可替代性最高的是：

**Waters → regulated chromatography/MS/software installed base  
Bruker → high-end scientific measurement niches  
GenScript → scaled Gene-to-Protein execution  
WuXi AppTec → global integrated CRDMO execution  
Twist → high-throughput silicon DNA synthesis**

其中 Waters 和 Bruker 的 moat 最接近 semiconductor equipment；GenScript 和 WuXi 更接近 outsourced foundry；Twist 更像 specialized component supplier。

### 最大 EPS 弹性是谁？

若 AI for Science 比预期快 3×，我认为上涨弹性最大的三只股票是：

**第一：Schrödinger。**  
当前市值只有约 $1.5B，软件 GM 74%，一旦收入进入 $500M–$1B 而 OpEx 增长显著慢于 revenue，operating leverage 极大。citeturn24search2turn0finance5

**第二：XtalPi。**  
AI4S 当前已经增长 136%，规模仍极小。如果 autonomous-lab spending 真正进入大型 pharma capex budget，它可能从 project business 变成 platform business。citeturn24search7

**第三：GenScript。**  
这是三者中最有实际利润证据的一家：H1 LSG revenue +28.8%，operating profit +102.8%。citeturn16search2

### AI for Science 时代的 ASML、TSMC、MU、CRDO、LITE、VRT 分别是谁？

这不是说商业模式完全相同，而是按产业功能类比：

| 半导体类比 | AI for Science 最接近公司 | 原因 |
|---|---|---|
| **ASML / KLA** | **Bruker / Waters** | 掌握高端科学测量与关键 workflow |
| **TSMC** | **GenScript / WuXi AppTec** | 把 digital scientific design 变成 physical execution |
| **MU / HBM** | **Illumina / Twist** | 数据产生/生物信息物理 substrate，量随 workload 增长 |
| **CRDO / LITE** | **Twist / GenScript** | 当前体量较小、处关键高速增长组件层 |
| **VRT** | **XtalPi** | autonomous lab 的 physical infrastructure / orchestration |
| **Scientific software layer** | **Schrödinger** | physics-based computational platform |
| **Data moat** | **Tempus** | proprietary clinical/genomic flywheel |

真正最值得注意的不是哪个公司“长得像 NVIDIA”，而是**哪些公司的经济位置会因为 bottleneck 转移而改善**。

### 如果最终只有少数 AI Drug Discovery 公司成功，哪些卖铲人还会赚钱？

我的答案是：

**Bruker、Waters、GenScript、Twist、Illumina、Agilent、IQVIA、WuXi AppTec。**

因为即使只有 10% 的 AI biotech 最终成功，这些工具公司仍然服务传统 pharma、academic science、diagnostics、biologics、materials science、QA/QC 和 clinical R&D。

Waters 尤其明显：其业务深度嵌入 regulated high-volume testing，本身就不依赖某一家 AI biotech 成功。citeturn21search1turn22search1

### 如果 AI Drug Discovery 整体失败，哪些公司仍然值得持有？

**第一：Bruker。  
第二：Waters。  
第三：GenScript。  
第四：IQVIA。  
第五：WuXi AppTec。**

其中 Bruker 即便没有 AI Drug Discovery，仍受益于 proteomics、structural biology、materials、semiconductor characterization 和学术科研；Waters 的 pharma QA/QC、diagnostics、food/environment testing 也完全不依赖 AI drug design。citeturn18search13turn21search1

这也是我不把纯 AI-native biotech 做第一重仓的原因。

### 港股有没有真正值得关注的“中国供应链隐形冠军”？

**有，而且我认为最值得关注的不是 WuXi，而是 GenScript。**

WuXi 已经是全球资本市场非常熟悉的大型 CRDMO，市值也已经非常大。真正更接近 2019–2022 年 CRDO/LITE 式“市场突然发现一个小得多的关键 component supplier”的，是：

**GenScript 的 Gene-to-Protein / biological validation infrastructure。**

2026H1 数据是我看到的最重要证据：AI-enabled drug discovery business 再次翻倍，而 Life Science Group operating profit 同比超过翻倍。citeturn16search2

第二个港股 hidden-option 是 **XtalPi**。

但它与 GenScript 最大区别是：

**GenScript 已经证明 operating leverage；XtalPi 主要还在证明 TAM 和商业化。**

所以二者在组合中的合理仓位绝不能相同。

### 如果 AI for Science 比市场预期快 3 倍，哪三只股票弹性最大？

按**股票上涨弹性而不是公司质量**：

**🥇 Schrödinger  
🥈 XtalPi  
🥉 GenScript**

如果要求同时考虑 downside protection，我会把排序改成：

**🥇 GenScript  
🥈 Schrödinger  
🥉 Bruker**

### 最终 AI for Science Top 5

**🥇 第一重仓：Bruker（BRKR）——31%**

我的定义：**AI for Science 的 scientific-metrology bottleneck。**

当前约 $9.1B 市值、FY26 revenue 约 $3.55B、约 28× FY26 adjusted P/E；BSI organic bookings 已重新约双位数增长，R&D 强度约 11.5%，创始科学家型 CEO 仍控制长期技术路线。citeturn0finance1turn4search1turn18search3turn24search24

**🥈 第二重仓：GenScript 金斯瑞生物科技（1548.HK）——24%**

我的定义：**AI Biology 的 wet-lab foundry / Gene-to-Protein infrastructure。**

最重要的数据不是“AI 概念”，而是 AI drug-discovery business 已连续第三个半年度高速增长并再次翻倍，同时 Life Science Group adjusted operating profit +102.8%。citeturn16search2

**🥉 第三重仓：Schrödinger（SDGR）——18%**

我的定义：**高毛利 scientific software + asymmetric discovery option。**

只有约 $1.49B market cap、约 $0.4B cash，software GM 74%，所以 10× 的数学远比大型工具股容易。citeturn24search2turn0finance5

**第四重仓：Tempus AI（TEM）——17%**

我的定义：**AI for Science 的 multimodal clinical-data flywheel。**

Data & Applications 已保持约 28% 增长，同时 diagnostics 持续扩张，真正资产是不断积累、可用于 pharma R&D 的 clinical/genomic dataset，而不只是一个 AI model。citeturn11search0

**第五重仓：XtalPi 晶泰控股（2228.HK）——10%**

我的定义：**Autonomous Lab / Physical AI for Science 的高风险 call option。**

AI4S H1 revenue +136.4%，但 R&D 也 +66%，目前必须用非常大的未来增长才能合理化 valuation，因此只能小仓位。citeturn24search7turn24search35

**最终唯一答案：如果只能买一家公司，我买 Bruker。**

原因不是它拥有最强的 AI narrative，而恰恰是相反：

> **AI 模型越便宜，Bruker 的 scientific ground-truth measurement 越重要；  
> AI 生成的候选越多，Bruker 的高端 proteomics、mass spectrometry 和自动化测量 workload 越大；  
> 无论最终赢家是 OpenAI、Google、Anthropic、Isomorphic Labs、Recursion、Schrödinger、XtalPi，甚至未来尚未成立的模型公司，现实世界仍必须被测量。**

这就是我认为 AI for Science 最值得投资的第一原则：

**不要押谁最会“想”；  
押那些掌握“现实世界怎样证明 AI 想对了”的公司。**