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
| `N_BINS` | `20` | 目标分箱数 |
| `OOT_CUT_DATE` | `2026-01-01` | OOT 切分日期，`>=` 此日期划入 OOT 集 |
| `CHIMERGE_MIN_BINS` | `6` | ChiMerge 合并目标箱数 |

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

从初始 20 箱开始，每轮计算所有相邻箱对的卡方 p 值，合并 p 值最大（分布最相似）的一对，直到箱数降至 `CHIMERGE_MIN_BINS`。

合并顺序：
1. 优先合并 p 值最大的相邻对（风险分布最接近）
2. 若多个相邻对 p 值接近，优先合并样本量较小的对
3. 合并过程中保持箱序单调性
4. 到达目标箱数后停止

### 7. OOT 跨期验证

- 将合并后的切点应用于 OOT 集
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

基于合并后的分箱，将分数从小到大排列，以 20 个等分位点为候选阈值，逐点计算累计指标。

累计方向：`score <= threshold`（分数越高风险越高，累计从低分到高分方向）。

| 指标 | 公式 | 说明 |
|---|---|---|
| 累计通过率 | cum_n / total_N | 阈值以下申请占比 |
| 累计坏账率（笔数） | cum_B / cum_n | 每笔等权 |
| 累计坏账率（金额） | sum(principal × y) / sum(principal) | 本金加权，仅含已放款样本 |

输出中将合并箱的上限标注为参考线，方便与风险等级对齐。

### 9. 三套方案设计

基于合并后的 6 个风险等级，设计三套策略方案，每套方案将分数段划分为自动通过（低风险）、人工审核（中风险）和拒绝（高风险）三区。

| 方案 | 自动通过 ≤ | 审核 ≤ | 整体通过率 | 策略思路 |
|---|---|---|---|---|
| 保守 | 0.0536（bins 1-2） | 0.1414（bins 3-4） | ~80% | 低坏账率，强抗风险 |
| 平衡（推荐） | 0.0798（bins 1-3） | 0.2648（bins 4-5） | ~95% | 自动审批与审核量平衡 |
| 增长 | 0.1414（bins 1-4） | 0.2648（bin 5） | ~95% | 高自动通过率，审核量最小 |

OOT 集同步验证各方案的通过率和坏账率。

### 10. 输出

结果写入 `res/binning_result.md`，包含：

- 摘要（合并前后对比）
- 初始等频 20 箱明细
- 相邻箱差异检验表（卡方 + Z 检验）
- ChiMerge 合并过程
- 合并后分箱明细（调优集 + OOT 集 + PSI）
- 合并后结论
- 累计阈值曲线（通过率与坏账率的 trade-off）
- 三套方案设计（保守/平衡/增长 + 三段式策略 + OOT 验证）

## 后续步骤

当前已完成：
1. 等频 20 箱初始分箱（步骤 2）
2. 相邻箱差异检验 + ChiMerge 合并（步骤 3）
3. 累计阈值曲线（步骤 4 — 阶段 A）
4. 三套方案设计（步骤 5 — 保守/平衡/增长 + 三段式策略）

后续待扩展：
- 业务调整（结合业务经验调箱边界）
- 阶段 B：补充利率/收入/成本数据后，扩展 EL、风险后收入和 UE
