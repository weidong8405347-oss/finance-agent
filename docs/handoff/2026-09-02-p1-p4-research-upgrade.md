# Handoff：调研能力提升 P1-P4（2026-09-02）

> 目的：新会话接手「投资调研能力升级」的收尾。读本文档 + docs/research-capability-upgrade.md
> （设计文档与裁决记录）即可开工，无需翻聊天记录。

---

## 0. 一句话现状

P1（速效修复）/P2（数据源）/P3（行业漏斗）/**P4 代码（投资判断层）已完成并测试全绿**；
P4 的**真实验收跑到 F3 闸口待批**（`appr-1b5ec981`，session `live-2be18efd`，服务在 8001 端口）；
批准后还需观察 F4.5 委员会 + F5 新结构报告的产出质量。P5（前端配置 UI）未开工。

## 1. 项目状态

- 仓库：`~/dev/work/finance-agent`，**全部改动未提交**（git 工作区脏，30+ 文件）——建议接手后先跑测试再提交
- 测试：`uv run pytest` → **287 passed, 9 skipped**（网络冒烟默认跳过）；前端 `cd frontend && npx vitest run` → 11 passed；`uv run ruff check src tests` 干净
- 运行：`uv run python -m finance_agent serve --no-open --no-build --port 8001`
- 本次实测驱动的事故修复全在 `docs/research-capability-upgrade.md` 的「P3 真实验收实录」一节

### 模型配置（用户 pi 配置已改）

- `research` = dashscope:kimi-k3 @ effort=max（用户实测超 pa/gpt-5.6-sol）
- `research-alt` = dashscope:ZHIPU/GLM-5.3 @ max（P4 双强交叉第二模型，P4 新增）
- `research-worker-1/2/3` = deepseek-v4-flash-0731 / ZHIPU/GLM-5.3-Flash / qwen3.8-flash
  （后两个是本会话验证可用后加进 `~/.pi/agent/models.json` 的）
- 数据源 key：`.env` 已配 `EXA_API_KEY` + `TAVILY_API_KEY`

### 数据源现状（本机网络实测）

| 源 | 状态 |
| --- | --- |
| EDGAR / HKEXnews / Exa / Tavily | ✅ 健康 |
| yfinance（prices + fundamentals） | ⚠️ 限流漂移（429 间歇）；stooq 已被反爬（PoW）——预检会降级不拦 |
| GDELT | ❌ 本机不可达（SSL 握手被断，疑似被墙） |
| akshare/东财 | ❌ 本机被拒（requests 走系统代理；直连也被断）——港股基本面短期靠 HKEXnews+行情推算或上 FMP |

## 2. 接手第一件事：完成 P4 验收 run

服务可能还活着（`curl -s localhost:8001/api/commands` 活着即免重启）。当前卡在 F3 闸口：

```bash
# 批准闸口（推荐名单 ['02228.HK','7666.HK','ABSI','SDGR','RLAY','ABCL']）
curl -s -X POST http://127.0.0.1:8001/api/approvals/appr-1b5ec981 \
  -H "Content-Type: application/json" -d '{"approved": true}'
```

然后每 3-5 分钟查一次进度直到 `command/done`：

```bash
uv run python -c "
import sqlite3, json
con = sqlite3.connect('data/events.db')
for t, p in con.execute(\"select type, payload from events where run_id='live-2be18efd' order by rowid\").fetchall():
    p = json.loads(p)
    if t in ('step_agent/end','command/done','report/published'):
        print(t.split('/')[-1], p.get('step',''), p.get('status',p.get('outcome','')), str(p.get('summary',''))[:130])
"
```

### 验收检查点（P4 质量目标，对照 docs/samples/ 四份范本）

1. **F4.5 委员会**：`data/reports/*committee*/committee_<ticker>.md` 每票一份；四视角有真实分歧、空头有实质论证、CIO 有明确倾向（偏多/偏空/五五开），不是平衡废话
2. **F5 报告结构**（`data/reports/*rank_report/industry_report.md`）：thesis 先行 → 漏斗淘汰逻辑（含"为什么 X 没入选"）→ 矩阵逐格证据锚点+四维评分 → 估值快照（calc valuation_snapshot 倍数）→ 排序 → 潜力评级 → 分歧与缺口
3. **评分纪律**：每个分值必须绑证据锚点（playbooks/scoring.md 的 rubric），发现无锚打分就是违规
4. 若报告质量仍明显低于范本 → 不要直接加代码，先归因是 prompt 层（playbook 文本）还是数据层（档案缺事实）——playbook 直接改 `playbooks/*.md` 即热生效

## 3. P4 已落地的件（对应文档 §5.1 三项裁决）

| 件 | 位置 | 说明 |
| --- | --- | --- |
| F1.5 thesis step | `commands/steps.py::step_thesis` | 强模型带研究预算写 `thesis` 字段（版本化+绑证据）；写不出 → blocked |
| 投资委员会 | `commands/steps.py::step_committee` | 四视角（kimi-k3@max / GLM-5.3@max 分担）+ 空头 + CIO；产出落 reports artifact（判断不进 KB） |
| 评分体系 | `playbooks/scoring.md` | 瓶颈/护城河/成长/估值默认 40/25/20/15，行业可调（thesis 备忘录携带理由） |
| 估值工具链 | `research/calc.py` | 新增 `valuation_snapshot`（EV/EV-Sales/EV-EBITDA/P-E/FCF yield，Decimal） |
| PDF 抽取 | `gateway/fetch.py` + pypdf | `fetch_document` 统一 HTML+PDF；edgar.fetch_filing_text 委托兼容 |
| /decide 注入 | `decision/loop.py context_note` | 委员会 CIO 记录进决策卡上下文；评级/仓位只在决策卡域 |
| F5 重构 | `steps.py::step_rank_report` + `playbooks/rank_report.md` | 范本结构 + thesis/committee/rubric 注入；空报告 → blocked |

## 4. 已知残留问题（按优先级）

1. **02228.HK vs 2228.HK 代码归一化**：池子里双胞胎并存（F2 挖掘产出）。修法：F2 的 `propose_candidates` 或落库前去重时做港股代码零填充归一（`zfill(5)+.HK` 或去零，二选一并写进工具校验）
2. **SDGR 等卡片超时被淘汰**——是超时不是否定；超时卡现在可见可重研，但最好单独 `/profile SDGR` 补
3. **「人才密度」「CEO 战略眼光」维度仍全空**——数据源不缺（web 能挖），缺的是维度组 playbook 指引（在 `playbooks/dimension_researcher.md` 的 risk_mgmt 组补深挖路径：访谈/致股东信/电话会/高管变动）
4. **F3 全量卡片 ~20 分钟**——6 并行 flash + 429 退避；要更快可研究卡批并发上限或加 Tavily 直取基本面
5. **GLM-5.3-Flash 是 worker 池里最慢的**（reasoning 模型）——观察其产出质量再决定是否留池

## 5. P5（未开工）

- provider 配置前端页（读写在 P1 已就绪：`data/llm-providers.json`，改文件即热生效）
- 漏斗进度可视化（F1-F5 step 卡已有事件流，前端只是还没专门面板）

## 6. 提交建议

接手人完成 P4 验收后建议提交（Conventional Commits，本仓库语义化发布）：

```
feat(research): 调研能力升级 P1-P4——行业漏斗/维度并行/判断层/四新数据源/停滞诊断与预检
```

正文列出：/industry 五步漏斗、维度并行 worker 池（三 flash）、thesis+委员会判断层、
Exa/Tavily/HKEXnews/基本面四源、预检+stalled 诊断卡、429 退避重试、PDF 抽取、
审批打回 comment、通知带 ticker/command_id。测试 287+11 绿。
