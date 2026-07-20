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

## 流程

### 1. 加载数据 & 关联

- 读取模型分表和申请表
- 通过 `application_id` 做 inner join，模型分表自带 `sample_datetime`

### 2. 样本划分

- `sample_datetime < OOT_CUT_DATE` → **策略调优集**
- `sample_datetime >= OOT_CUT_DATE` → **OOT 集**
- 排除标签为 NULL 的记录（未成熟/未放款），得到 `tuning_valid` 和 `oot_valid`

### 3. 等频分箱

使用 `pd.qcut(scores, q=N_BINS, duplicates="drop")` 做等频分箱：

- 按分数从低到高排序（低分 = 低风险 = 优先通过）
- 同分值集中时自动合并相邻箱（`duplicates="drop"`），实际箱数可能少于目标

### 4. 计算各箱指标

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
B\\_pct = \frac{B}{\sum B}, \quad G\\_pct = \frac{G}{\sum G}
\]

\[
WOE = \ln\left(\frac{B\\_pct}{G\\_pct}\right)
\]

B_pct 或 G_pct 为 0 时 WOE 填 0。

#### IV（Information Value）

\[
IV\\_component = (B\\_pct - G\\_pct) \times WOE
\]

\[
IV = \sum IV\\_component
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

`|ρ| < 0.9` 时提示可能存在局部倒挂，后续合并步骤需关注。

### 5. 输出

结果写入 `res/binning_result.md`，包含：

- 摘要信息（样本量、整体坏账率、IV、Spearman ρ、OOT 坏账率）
- 分箱明细表（箱序、分数区间、样本量、坏样本、坏账率、SE、累计通过率、累计坏账率、WOE）

## 后续步骤

当前仅实现等频 20 箱初始分箱，后续方法待扩展：

1. 单调/统计合并（相邻箱合并以提升单调性）
2. 业务调整（结合业务经验调箱边界）
3. 收益阈值优化（使通过率与坏账率达到收益最优）
