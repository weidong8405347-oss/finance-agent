# F2 标的池挖掘 playbook（candidate_pool）

任务：产出该赛道的候选标的池，每只票都必须有「属于该赛道」的公开证据。

## 双通道纪律（防漏检）
1. **语义通道**：用 web 搜索多角度挖掘——中英文各来一遍
   （如 "AI for Science 标的" + "AI4S companies" + 子赛道名 + "上市公司"）；
   覆盖美股、港股、A 股；未上市候选单列（标注最新估值/IPO 传闻，注明来源）。
2. **防漏检自查**：龙头不可能漏（按市值/知名度常识核对）；「沾边股」
   （该行业收入占比 <30%）标注「非纯正标的」；冷门小票不因偏好龙头而漏。

## 证据绑定（硬纪律）
- 每只候选必须 ≥1 条已登记证据（register_evidence）支撑其赛道归属；
  禁止凭训练记忆列票——「我觉得它属于这里」不是证据。
- 每票记录：ticker、名称、市场（US/HK/CN）、子赛道归属、归属证据 id、一句话主业。

## 输出（写档案字段）
- `player_landscape`（list）：[{ticker, name, market, sub_sector, one_liner,
  listed(bool), evidence_ids}]
