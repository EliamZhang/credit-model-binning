# 分箱方法论

> 对应脚本：[scr/binning.py](scr/binning.py)

## 数据源

| 文件 | 用途 |
|---|---|
| `res/aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv` | 多头融合模型分（主模型） |
| `res/application_info.csv` | 申请信息表，提供风险标签 |

通过 `application_id` 关联。

## 参数配置

| 参数 | 默认值 | 说明 |
|---|---|---|
| `LABEL_COL` | `duedate_3m_30` | 风险标签（三期 30 DPD） |
| `SCORE_COL` | `aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score` | 模型分字段 |
| `SCORE_HIGHER_IS_RISKIER` | `True` | 分数方向。`True` 表示分数越高风险越高，累计通过规则为 `score <= threshold`；`False` 表示分数越高风险越低，累计通过规则为 `score >= threshold` |
| `N_BINS` | `20` | 目标分箱数 |
| `OOT_CUT_DATE` | `2025-10-21` | OOT 切分日期，`>=` 此日期划入 OOT 集 |
| `FPD7_REF_DATE` | `2026-07-20` | FPD7 计算参考日期，用于判断首期是否到期满 7 天 |
| `CHIMERGE_MIN_BINS` | `6` | ChiMerge 最少保留箱数 |
| `CHIMERGE_MAX_BINS` | `10` | 超过该箱数时继续合并，避免最终风险等级过碎 |
| `CHIMERGE_P_THRESHOLD` | `0.05` | 相邻箱卡方检验 p 值阈值，p 值不低于该阈值时认为相邻箱风险差异不显著，可继续合并 |
| `MIN_BIN_SIZE` | `3000` | 单箱最低样本量，低于该值时优先考虑合并 |
| `MIN_BAD_COUNT` | `100` | 单箱最低坏样本数，低于该值时优先考虑合并 |

## 流程

### 1. 加载数据 & 关联

- 读取模型分表和申请表
- 通过 `application_id` 做 inner join，模型分表自带 `sample_datetime`

### 2. 样本划分

- `sample_datetime < OOT_CUT_DATE` → **策略调优集**
- `sample_datetime >= OOT_CUT_DATE` → **OOT 集**
- 排除标签为 NULL 的记录（未成熟/未放款），得到 `tuning_valid` 和 `oot_valid`

### 3. 等频 20 箱初分

使用 `pd.qcut(scores, q=N_BINS, duplicates="drop")` 做等频分箱：

- 按分数从低到高排序
- 同分值集中时自动合并相邻箱（`duplicates="drop"`），实际箱数可能少于目标

### 4. 初始分箱指标

对策略调优集汇总每箱的以下指标：

| 指标 | 公式 | 说明 |
|---|---|---|
| `n` | `COUNT` | 箱内样本数 |
| `B` | `SUM(y)` | 箱内坏样本数 |
| `bad_rate` | `B / n` | 坏账率 |
| `SE` | `sqrt(bad_rate * (1 - bad_rate) / n)` | 标准误 |
| `cum_n` | `cumsum(n)` | 累计通过样本数（低分 → 高分） |
| `cum_pass_rate` | `cum_n / total_N` | 累计通过率 |
| `cum_bad_rate` | `cum_B / cum_n` | 累计坏账率 |

#### Lift

\[
Lift = \frac{bad\_rate}{overall\_bad\_rate}
\]

\[
Cumulative\ Lift = \frac{cum\_bad\_rate}{overall\_bad\_rate}
\]

> 1 表示与平均水平一致；> 1 表示该箱坏样本浓度高于平均（风险偏高）；< 1 表示低于平均。

#### WOE（Weight of Evidence）

\[
G = n - B
\]

\[
B\_pct = \frac{B}{\sum B}, \quad G\_pct = \frac{G}{\sum G}
\]

\[
WOE = \ln\left(\frac{B\_pct}{G\_pct}\right)
\]

B_pct 或 G_pct 为 0 时 WOE 填 0。

#### IV（Information Value）

\[
IV\_component = (B\_pct - G\_pct) \times WOE
\]

\[
IV = \sum IV\_component
\]

| IV 范围 | 区分能力 |
|---|---|
| < 0.02 | 几乎无 |
| 0.02 ~ 0.1 | 弱 |
| 0.1 ~ 0.3 | 中等 |
| 0.3 ~ 0.5 | 强 |
| > 0.5 | 极强 |

#### 单调性检查

使用 Spearman 秩相关系数检验箱序与坏账率的单调性：

\[
\rho = \frac{\text{cov}(R_{\text{bin}}, R_{\text{bad\_rate}})}{\sigma_{R_{\text{bin}}} \cdot \sigma_{R_{\text{bad\_rate}}}}
\]

`|ρ| < 0.9` 时提示可能存在局部倒挂。

### 5. 相邻箱差异检验

对初始 20 箱的每一对相邻箱，执行两种统计检验：

#### 卡方检验（Chi-Square Test）

2×2 列联表（箱 A vs 箱 B，好 vs 坏）：

\[
\chi^2 = \sum_{i=1}^{2}\sum_{j=1}^{2}\frac{(O_{ij}-E_{ij})^2}{E_{ij}}
\]

p 值越大，两箱好坏分布越接近，越适合合并。

#### Z 检验（比例差异）

\[
z = \frac{r_B - r_A}{\sqrt{\frac{r_A(1-r_A)}{n_A} + \frac{r_B(1-r_B)}{n_B}}}
\]

用于直接比较两箱坏账率是否有显著差异。

### 6. ChiMerge 合并

从初始 20 箱开始，每轮计算所有相邻箱对的卡方 p 值，合并 p 值最大且满足合并条件的一对。合并不再机械降到固定 6 箱，而是采用“箱数上限 + 统计差异 + 样本充分性 + 单调性”的组合约束：

1. 若当前箱数超过 `CHIMERGE_MAX_BINS`，继续合并，避免最终等级过碎；
2. 若相邻箱卡方 p 值不低于 `CHIMERGE_P_THRESHOLD`，说明风险差异不显著，可合并；
3. 若相邻箱出现局部倒挂，可合并；
4. 若单箱样本量低于 `MIN_BIN_SIZE` 或坏样本数低于 `MIN_BAD_COUNT`，优先合并；
5. 合并最低不低于 `CHIMERGE_MIN_BINS`；
6. 若箱数已不超过上限，且不存在不显著相邻箱、局部倒挂或低样本箱，则提前停止。

输出中记录每一步的合并原因和最终停止原因，方便复核是否存在过度合并。

### 7. OOT 跨期验证

- 将合并后的切点应用于 OOT 集
- 首尾切点使用 `-inf` / `inf` 开口边界，避免 OOT 或线上样本因超出调优集分数范围被排除
- 复算各箱坏账率、IV、Spearman ρ
- 计算 PSI（Population Stability Index）

\[
PSI = \sum_i (a_i - e_i) \times \ln\frac{a_i}{e_i}
\]

| PSI 区间 | 常见判断 |
|---|---|
| < 0.10 | 分布相对稳定 |
| 0.10 ~ 0.25 | 存在一定漂移 |
| > 0.25 | 漂移较明显 |

### 8. 累计阈值测算

基于合并后的分箱，按低风险到高风险方向排列，以 20 个等分位点和合并箱边界共同作为候选阈值，逐点计算累计指标。

累计方向由 `SCORE_HIGHER_IS_RISKIER` 控制：

- 分数越高风险越高：`score <= threshold`
- 分数越高风险越低：`score >= threshold`

| 指标 | 公式 | 说明 |
|---|---|---|
| 累计通过率 | cum_n / total_N | 满足阈值通过规则的申请占比 |
| 累计坏账率（笔数） | cum_B / cum_n | 每笔等权 |
| 边际坏账率（笔数） | 新增坏样本 / 新增样本 | 相邻阈值之间新增客群的坏账率 |
| 累计坏账率（金额） | sum(principal × y) / sum(principal) | 本金加权，仅含已放款样本 |

输出中将合并箱边界标注为参考线；分数越高风险越高时取箱上限，分数越高风险越低时取箱下限，方便与风险等级对齐。

### 9. 三套方案设计

基于合并后的风险等级，设计三套策略方案，每套方案将分数段划分为自动通过（低风险）、人工审核（中风险）和拒绝（高风险）三区。方案不再依赖固定 6 箱，而是根据实际合并箱数按低风险到高风险顺序自动切分。

| 方案 | 自动通过范围 | 审核/拒绝范围 | 策略思路 |
|---|---|---|---|
| 保守 | 约最低风险前 1/3 箱 | 约前 2/3 箱进入通过范围，其余拒绝 | 低坏账率，强抗风险 |
| 平衡（推荐） | 约最低风险前 1/2 箱 | 中间风险箱人工审核，仅最高风险箱拒绝 | 自动审批与审核量平衡 |
| 增长 | 除最高风险箱外自动通过 | 最高风险箱人工审核，不做硬拒绝 | 最大通过率，适合增长目标 |

OOT 集同步验证各方案的通过率和坏账率。

### 10. 转化率漏斗

在全量申请（不分调优/OOT）上，按合并后的风险等级拆分审批漏斗：

申请 → 完成（排除 0.Incomplete） → 通过（3.x / 4.x） → 放款（4.Funded）

| 转化率 | 公式 | 说明 |
|---|---|---|
| 完成率 | completed / apply | 申请中完成审核的比例 |
| 通过率 | approved / completed | 完成中通过的比例 |
| 拒绝率 | declined / completed | 完成中风控拒绝的比例 |
| 放款率 | funded / approved | 通过中成功放款的比例 |
| 整体放款率 | funded / apply | 申请到放款的端到端转化 |

输出中按 6 个风险箱拆解各阶段转化率，用于判断风控策略是否在低风险客群上过于严苛。

### 11. FPD7 标签对比

在调优集和 OOT 集上，使用 `fpd7_flag` 作为早期风险信号标签，与主标签 `duedate_3m_30` 做对比分析。

**FPD7 计算口径**：`application_status = '4.Funded'` 且 `first_payment_scheduled_date < FPD7_REF_DATE - 7` 天。满足条件且 `first_payment_days_past_due_ever > 7` 为 1，`<= 7` 为 0，其余为 NULL。

> FPD7 有效样本仅覆盖已放款且首期到期满 7 天的订单，样本量远小于主标签，但作为更早期的风险信号可提供补充视角。

输出包括：

- 调优集和 OOT 集的 FPD7 整体指标（有效样本数、坏账率、AUC、KS）
- 主标签与 FPD7 的 AUC/KS 对比表，用于判断模型对早期风险的排序能力
- 合并后分箱 × FPD7 的逐箱坏账率，与同箱 3m_30 坏账率并列对比

### 12. 输出

结果写入 `res/binning_result.md`，包含：

- 关键结论与推荐方案（方案对比 + 风险等级速览）
- 模型表现摘要（合并前后对比）
- 初始等频 20 箱明细
- 相邻箱差异检验表（卡方 + Z 检验）
- ChiMerge 合并过程
- 合并后分箱明细（调优集 + OOT 集 + PSI）
- 合并后结论
- 累计阈值曲线（通过率与坏账率的 trade-off）
- 三套方案设计（保守/平衡/增长 + 三段式策略 + OOT 验证）
- 转化率漏斗（全量申请 → 放款，按风险等级拆解）
- FPD7 标签对比（早期风险信号 vs 主标签 3m_30）

## 后续步骤

当前已完成：
1. 等频 20 箱初始分箱 + 初始指标
2. 相邻箱差异检验（卡方 + Z 检验）
3. ChiMerge 合并（参数化停止条件）
4. OOT 跨期验证（PSI + IV + Spearman ρ）
5. 累计阈值曲线（笔数 + 金额口径，边际坏账率）
6. 三套方案设计（保守/平衡/增长 + OOT 验证）
7. 转化率漏斗（全量申请按风险等级拆解审批转化）
8. FPD7 标签对比（早期风险信号 vs 3m_30）

后续待扩展：
- 业务调整（结合业务经验调箱边界，记录调整原因）
- 阶段 B：补充利率/收入/成本数据后，扩展 EL、风险后收入和 UE
- PD 校准（O/E、校准截距/斜率、Brier Score）
- 按产品/渠道/客群/月度的稳定性拆解
- 监控看板指标输出（PSI/CSI、通过率、拒绝率等）
- 配置表/版本治理输出
