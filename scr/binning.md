# 风险模型分箱脚本处理逻辑与指标口径说明（字段血缘版）

> 对应脚本：`binning.py`  
> 当前输入文件：`res/aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv`、`res/application_info.csv`  
> 当前输出文件：`res/binning_result.md`  
> 文档目标：将分箱脚本中的每一个关键指标拆解为“从哪里取数、使用哪个字段、经过什么过滤与加工、最终如何计算”，便于模型、策略、数据和业务团队统一理解口径。

---

## 1. 先说明：本文能够确认到什么粒度

当前 Python 脚本直接读取的是两个 CSV 文件，并未记录这些 CSV 在数仓中对应的数据库、Schema 和物理表名。因此，本文可以准确说明到：

- 来源文件；
- 来源字段；
- 关联主键；
- 样本过滤条件；
- 字段加工方法；
- 指标计算公式；
- Python 实际执行逻辑。

但本文**不能凭空确认**两个 CSV 分别来自数仓中的哪张物理表。若后续需要形成正式的数据血缘文档，应由数据负责人补充以下信息：

| 当前逻辑数据源 | 待补充的物理来源 |
|---|---|
| `aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv` | 数据库名、Schema、模型分结果表名、分区条件 |
| `application_info.csv` | 数据库名、Schema、申请表现宽表名、字段生成日期 |

本文以下统一将两个 CSV 称为“模型分表”和“申请信息表”。

---

# 2. 脚本最终要解决什么问题

该脚本完成的不是单一的“模型分切箱”，而是一套从模型分层到策略阈值测算的完整流程：

```text
模型分表
    +
申请信息表
    ↓ application_id 内连接
分析宽表
    ↓ sample_datetime 时间切分
策略调优集 / OOT 集
    ↓ 仅保留 3M30 主标签有效样本
调优集等频 20 箱
    ↓
计算坏账率、WOE、IV、Lift、AUC、KS、单调性
    ↓
同时参考 3M30 和 1M30 做 ChiMerge 相邻箱合并
    ↓
形成最终风险等级
    ↓
将固定分箱应用到 OOT 样本
    ↓
计算 PSI、OOT AUC、OOT KS、OOT 坏账率
    ↓
逐阈值计算通过率、笔数坏账率、边际坏账率、金额口径风险
    ↓
生成保守 / 平衡 / 增长三套策略方案
    ↓
输出 binning_result.md
```

当前脚本**没有执行**以下模块：

- FPD7 标签生成或验证；
- 申请、审批、放款转化漏斗；
- EL、风险后收入和 UE；
- 按业务状态进行申请转化分析。

如需这些模块，应在脚本中增加对应数据源和计算逻辑，不能仅在方法论文档中描述。

---

# 3. 输入数据与字段血缘

## 3.1 模型分表

**文件：**

```text
res/aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv
```

当前脚本实际使用以下字段：

| 字段 | 用途 | 加工方式 | 进入哪些指标 |
|---|---|---|---|
| `application_id` | 与申请信息表关联 | 不加工，直接作为 inner join 主键 | 所有后续分析 |
| `sample_datetime` | 划分调优集与 OOT 集 | `pd.to_datetime()` 转为日期时间 | 样本切分 |
| `aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score` | 模型分 | 直接使用；未显式执行数值转换 | 分箱、AUC、KS、策略阈值、风险排序 |

模型分字段在脚本中配置为：

```python
SCORE_COL = "aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score"
SCORE_HIGHER_IS_RISKIER = True
```

因此当前业务含义为：

```text
模型分越高，客户风险越高。
模型分越低，客户风险越低。
```

对应策略方向为：

```text
自动通过：score <= threshold
拒绝：score > threshold
```

## 3.2 申请信息表

**文件：**

```text
res/application_info.csv
```

脚本不会读取该文件的全部字段，而是在关联前只保留以下字段：

| 字段 | 字段口径 | 当前脚本用途 |
|---|---|---|
| `application_id` | 申请唯一标识 | 与模型分表关联 |
| `duedate_3m_30` | 3M30 主标签，通常 1 表示坏、0 表示好、NULL 表示未成熟或不可用 | 主分箱标签、坏账率、WOE、IV、AUC、KS、策略测算 |
| `duedate_1m_30` | 1M30 辅助标签 | ChiMerge 合并时，与 3M30 一起判断相邻箱是否存在显著差异 |
| `principal` | 原始放款本金 | 金额口径风险的分母 |
| `estimate_principal_remaining_mob3` | MOB3 时点预计剩余本金 | 金额口径风险的分子金额基础 |
| `dpd_days_ever_mob3` | MOB3 内历史最大逾期天数 | 判断该笔 MOB3 剩余本金是否计入风险金额 |

## 3.3 两张表的关联逻辑

脚本执行：

```python
merged = score_df.merge(
    info_df[[
        "application_id",
        "duedate_3m_30",
        "duedate_1m_30",
        "principal",
        "estimate_principal_remaining_mob3",
        "dpd_days_ever_mob3",
    ]],
    on="application_id",
    how="inner",
)
```

等价 SQL 为：

```sql
SELECT
    s.application_id,
    s.sample_datetime,
    s.aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score,
    a.duedate_3m_30,
    a.duedate_1m_30,
    a.principal,
    a.estimate_principal_remaining_mob3,
    a.dpd_days_ever_mob3
FROM score_table s
INNER JOIN application_info_table a
    ON s.application_id = a.application_id;
```

### 关联后的样本含义

只有同时满足以下两个条件的申请才会进入分析宽表：

1. 模型分表中存在该 `application_id`；
2. 申请信息表中也存在该 `application_id`。

因此会被排除：

- 有模型分、但申请信息表中没有表现数据的申请；
- 有申请信息、但模型分表中没有模型分的申请。

### 必须先做的唯一性检查

脚本当前没有主动去重。如果任意一张表的 `application_id` 不唯一，会发生多对多扩张。

例如：

```text
模型分表：某 application_id 有 2 行
申请信息表：同一个 application_id 有 3 行
inner join 后：产生 2 × 3 = 6 行
```

这会直接重复计算样本数、坏样本数、坏账率和通过率。因此正式使用前应至少执行：

```sql
SELECT application_id, COUNT(*) AS cnt
FROM score_table
GROUP BY application_id
HAVING COUNT(*) > 1;
```

以及：

```sql
SELECT application_id, COUNT(*) AS cnt
FROM application_info_table
GROUP BY application_id
HAVING COUNT(*) > 1;
```

---

# 4. 参数配置与业务含义

| 参数 | 当前值 | 业务含义 |
|---|---:|---|
| `LABEL_COL` | `duedate_3m_30` | 主风险标签 |
| `LABEL_COL_1M30` | `duedate_1m_30` | 辅助风险标签 |
| `CHIMERGE_LABEL_COLS` | `[duedate_3m_30, duedate_1m_30]` | ChiMerge 同时参考两个标签 |
| `AMOUNT_COL` | `estimate_principal_remaining_mob3` | 风险金额分子所使用的金额字段 |
| `AMOUNT_DENOM_COL` | `principal` | 风险金额分母所使用的金额字段 |
| `AMOUNT_LABEL_COL` | `dpd_days_ever_mob3` | 金额是否属于 30+ 风险的判断字段 |
| `AMOUNT_LABEL_THRESHOLD` | `30` | `dpd_days_ever_mob3 >= 30` 判定为风险金额 |
| `SCORE_HIGHER_IS_RISKIER` | `True` | 分数越高风险越高 |
| `N_BINS` | `20` | 初始等频目标箱数 |
| `OOT_CUT_DATE` | `2025-10-21` | 调优集和 OOT 集的时间切点 |
| `CHIMERGE_MIN_BINS` | `6` | 最少保留 6 箱 |
| `CHIMERGE_MAX_BINS` | `10` | 超过 10 箱时强制继续合并 |
| `CHIMERGE_P_THRESHOLD` | `0.05` | 相邻箱差异显著性阈值 |
| `MIN_BIN_SIZE` | `3000` | 任一相邻箱样本低于 3000，可触发合并 |
| `MIN_BAD_COUNT` | `100` | 任一相邻箱 3M30 坏样本低于 100，可触发合并 |

---

# 5. 样本构建口径

## 5.1 时间字段加工

来源：

```text
模型分表.sample_datetime
```

加工：

```python
merged["sample_datetime"] = pd.to_datetime(merged["sample_datetime"])
```

当前代码没有设置 `errors="coerce"`。因此若存在无法解析的日期字符串，脚本会直接报错，而不是转成 NULL。

## 5.2 调优集

来源宽表：`merged`

过滤条件：

```python
tuning = merged[merged["sample_datetime"] < "2025-10-21"]
```

等价口径：

```text
sample_datetime < 2025-10-21
```

## 5.3 OOT 集

来源宽表：`merged`

过滤条件：

```python
oot = merged[merged["sample_datetime"] >= "2025-10-21"]
```

等价口径：

```text
sample_datetime >= 2025-10-21
```

`2025-10-21 00:00:00` 正好归入 OOT 集。

## 5.4 调优有效样本

来源：`tuning`

过滤字段：

```text
申请信息表.duedate_3m_30
```

过滤条件：

```python
tuning_valid = tuning[tuning["duedate_3m_30"].notna()]
```

即：只有 `duedate_3m_30` 非空的申请才进入监督分箱和策略测算。

随后进行标签加工：

```python
duedate_3m_30 = fillna(0).astype(int)
duedate_1m_30 = fillna(0).astype(int)
```

但由于 `tuning_valid` 已经提前过滤了 `duedate_3m_30` 非空，因此实际主要影响的是：

```text
duedate_1m_30 的 NULL 会被填成 0。
```

这意味着在 ChiMerge 的 1M30 检验中，1M30 未成熟或缺失样本会被当成好样本处理。

> 这是当前代码的真实口径，并不一定是推荐口径。若 `duedate_1m_30 = NULL` 表示未成熟，建议在每个标签的卡方检验中分别使用该标签非空的样本，而不是填 0。

## 5.5 OOT 有效样本

来源：`oot`

过滤条件：

```python
oot_valid = oot[oot["duedate_3m_30"].notna()]
```

OOT 只要求主标签 `duedate_3m_30` 有效。当前脚本不会在 OOT 中使用 `duedate_1m_30` 做 ChiMerge。

## 5.6 模型分缺失的实际影响

当前脚本没有显式执行：

```python
df = df[df[SCORE_COL].notna()]
```

实际影响如下：

- `pd.qcut()` 不会给缺失分分配箱；
- `groupby(bin)` 时缺失分样本不会进入分箱统计；
- 但部分方案统计的总样本数仍可能包含缺失分样本；
- 缺失分在 `score <= threshold` 和 `score > threshold` 中都返回 False，可能既不进入通过，也不进入拒绝；
- 增长方案中 `approved = df`，反而可能把缺失分样本计入通过样本。

正式口径建议在分箱前明确拆分：

```text
有效评分样本：score IS NOT NULL
缺失评分样本：单独统计和制定策略
```

---

# 6. 初始等频 20 箱

## 6.1 使用的数据

| 项目 | 口径 |
|---|---|
| 数据集 | `tuning_valid` |
| 分箱字段 | 模型分表.`aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score` |
| 主标签 | 申请信息表.`duedate_3m_30` |
| 目标箱数 | 20 |

代码：

```python
tuning_valid["bin"], bins = pd.qcut(
    tuning_valid[SCORE_COL],
    q=20,
    duplicates="drop",
    retbins=True,
)
```

## 6.2 等频的实际含义

将调优有效样本按模型分从低到高排列，然后用分位点尽量切成 20 组。

若调优有效且有分数的样本数为：

```text
N_score
```

理论上每箱样本数约为：

\[
\text{每箱目标样本数} \approx \frac{N_{score}}{20}
\]

## 6.3 为什么实际可能少于 20 箱

参数：

```python
duplicates="drop"
```

当多个分位点落在相同分数上时，重复切点会被删除。

例如：

```text
5% 分位数  = 0.1820
10% 分位数 = 0.1820
```

相同分数不能同时作为两个有效边界，因此最终可能只形成 19 箱、18 箱或更少。

## 6.4 首尾开口边界

调优集生成切点后，代码执行：

```python
open_bins[0] = -np.inf
open_bins[-1] = np.inf
```

作用是将最终边界变为：

```text
(-∞, 第一个内部切点]
...
(最后一个内部切点, +∞]
```

这样 OOT 中即使出现低于调优集最小分或高于调优集最大分的有限数值，也仍能落入首箱或尾箱。

因此代码中所谓“OOT 超出切点范围”的样本，通常主要来自模型分为 NULL，而不是有限分数真正超出区间。

---

# 7. 分箱基础指标：字段来源与计算过程

以下指标均在某个分箱数据集上计算。初始分箱时数据为 `tuning_valid`，最终分箱时数据仍为 `tuning_valid`，但分箱字段由 `bin` 改为 `merged_bin`。

设第 \(i\) 个箱包含标签有效且成功分箱的申请。

## 7.1 箱内最低分 `score_min`

| 项目 | 口径 |
|---|---|
| 来源文件 | 模型分表 |
| 来源字段 | `aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score` |
| 数据范围 | 当前箱内样本 |
| 加工 | 取最小值 |

\[
score\_min_i = \min(score_j), \quad j \in i
\]

Python：

```python
score_min=(SCORE_COL, "min")
```

## 7.2 箱内最高分 `score_max`

| 项目 | 口径 |
|---|---|
| 来源文件 | 模型分表 |
| 来源字段 | 模型分字段 |
| 数据范围 | 当前箱内样本 |
| 加工 | 取最大值 |

\[
score\_max_i = \max(score_j), \quad j \in i
\]

需要注意：`score_max` 是该箱样本中**实际观察到的最高分**，不是 `pd.cut` 使用的理论切点。

## 7.3 样本数 `n`

| 项目 | 口径 |
|---|---|
| 来源文件 | 模型分表 |
| 计数字段 | 模型分字段 |
| 过滤条件 | 已进入当前箱 |
| Python 聚合 | `count(score)` |

\[
n_i = \operatorname{COUNT}(score_j), \quad j \in i
\]

由于 pandas 的 `count` 不统计 NULL，因此模型分为空的样本不会计入 `n_i`。

等价 SQL：

```sql
COUNT(score) AS n
```

## 7.4 坏样本数 `B`

| 项目 | 口径 |
|---|---|
| 来源文件 | 申请信息表 |
| 来源字段 | `duedate_3m_30` |
| 数据范围 | 当前箱内样本 |
| 标签定义 | 1 为坏，0 为好 |
| Python 聚合 | `sum(duedate_3m_30)` |

\[
B_i = \sum_{j \in i} duedate\_3m\_30_j
\]

等价 SQL：

```sql
SUM(duedate_3m_30) AS B
```

前提是标签只包含 0 和 1。若出现 2、-1 或其他值，当前脚本仍会直接求和，导致坏样本数失真。

## 7.5 好样本数 `G`

来源：由 `n` 和 `B` 二次加工得到。

\[
G_i = n_i - B_i
\]

Python：

```python
stats["G"] = stats["n"] - stats["B"]
```

## 7.6 箱内坏账率 `bad_rate`

| 项目 | 分子 | 分母 |
|---|---|---|
| 字段 | `SUM(duedate_3m_30)` | `COUNT(score)` |
| 来源 | 申请信息表 | 模型分表 |
| 样本 | 当前箱 | 当前箱 |

\[
bad\_rate_i = \frac{B_i}{n_i}
\]

等价 SQL：

```sql
SUM(duedate_3m_30) / COUNT(score) AS bad_rate
```

业务解释：该风险箱中，3M30 坏样本占该箱有效评分样本的比例。

## 7.7 坏账率标准误 `SE`

来源字段：

- `bad_rate_i`：由上一节加工；
- `n_i`：当前箱样本数。

\[
SE_i = \sqrt{\frac{bad\_rate_i(1-bad\_rate_i)}{n_i}}
\]

Python：

```python
SE = np.sqrt(bad_rate * (1 - bad_rate) / n)
```

业务解释：用于衡量箱内坏账率估计的不确定性。样本越少，通常标准误越大。

若需要展示近似 95% 置信区间，可进一步计算：

\[
CI_{95\%} = bad\_rate_i \pm 1.96 \times SE_i
\]

当前脚本只输出 `SE`，没有输出置信区间。

## 7.8 整体样本数 `total_N`

来源：所有非空分箱的 `n_i` 求和。

\[
total\_N = \sum_{i=1}^{K} n_i
\]

## 7.9 整体坏样本数 `total_B`

来源：所有非空分箱的 `B_i` 求和。

\[
total\_B = \sum_{i=1}^{K} B_i
\]

## 7.10 整体坏账率 `overall_bad_rate`

\[
overall\_bad\_rate = \frac{total\_B}{total\_N}
\]

等价 SQL：

```sql
SUM(duedate_3m_30) / COUNT(score)
```

---

# 8. 累计通过指标：从低风险向高风险累计

当前配置为：

```text
SCORE_HIGHER_IS_RISKIER = True
```

因此分数从低到高即风险从低到高。`compute_bin_stats()` 会按 `score_min` 升序排列，然后累计。

设累计到第 \(k\) 个风险箱。

## 8.1 累计样本数 `cum_n`

来源：各箱 `n` 从低分箱向高分箱累加。

\[
cum\_n_k = \sum_{i=1}^{k} n_i
\]

## 8.2 累计坏样本数 `cum_B`

来源：各箱 `B` 从低分箱向高分箱累加。

\[
cum\_B_k = \sum_{i=1}^{k} B_i
\]

## 8.3 累计通过率 `cum_pass_rate`

| 分子 | 分母 |
|---|---|
| 截至当前风险箱累计样本数 `cum_n_k` | 全部分箱有效样本数 `total_N` |

\[
cum\_pass\_rate_k = \frac{cum\_n_k}{total\_N}
\]

业务解释：若将第 \(k\) 箱的上边界作为自动通过阈值，理论上有多少有效评分样本可以通过。

## 8.4 累计坏账率 `cum_bad_rate`

| 分子 | 分母 |
|---|---|
| 截至当前风险箱累计坏样本数 `cum_B_k` | 截至当前风险箱累计样本数 `cum_n_k` |

\[
cum\_bad\_rate_k = \frac{cum\_B_k}{cum\_n_k}
\]

业务解释：当前阈值以下所有通过客群的整体 3M30 坏账率。

## 8.5 分数方向的限制

脚本虽然提供 `SCORE_HIGHER_IS_RISKIER=False` 的配置，但 `compute_bin_stats()` 内部始终按 `score_min` 升序计算累计指标，并未在该函数中反转。

因此：

- 当前 `True` 配置下，累计方向正确；
- 若未来改为“分数越高风险越低”，应同步修改累计排序，否则累计通过率和累计坏账率方向会错误。

---

# 9. Lift 指标

## 9.1 单箱 Lift

来源：

- 当前箱 `bad_rate_i`；
- 全体样本 `overall_bad_rate`。

\[
Lift_i = \frac{bad\_rate_i}{overall\_bad\_rate}
\]

解释：

| Lift | 含义 |
|---:|---|
| `< 1` | 当前箱风险低于整体平均 |
| `= 1` | 当前箱风险等于整体平均 |
| `> 1` | 当前箱风险高于整体平均 |

例如：

```text
整体 3M30 坏账率 = 10%
某箱 3M30 坏账率 = 20%
Lift = 20% / 10% = 2.0
```

表示该箱坏账率为整体平均的 2 倍。

## 9.2 累计 Lift

来源：

- 当前阈值累计坏账率 `cum_bad_rate_k`；
- 整体坏账率 `overall_bad_rate`。

\[
cum\_Lift_k = \frac{cum\_bad\_rate_k}{overall\_bad\_rate}
\]

对于低风险通过客群，通常希望累计 Lift 小于 1。

---

# 10. WOE 与 IV：明确分子、分母和样本

## 10.1 第 i 箱坏样本占全部坏样本的比例 `B_pct`

| 分子 | 分母 |
|---|---|
| 当前箱 `SUM(duedate_3m_30)` | 所有箱 `SUM(duedate_3m_30)` |

\[
B\_pct_i = \frac{B_i}{total\_B}
\]

## 10.2 第 i 箱好样本占全部好样本的比例 `G_pct`

| 分子 | 分母 |
|---|---|
| 当前箱好样本数 `G_i` | 所有箱好样本数 `total_N-total_B` |

\[
G\_pct_i = \frac{G_i}{total\_N-total\_B}
\]

## 10.3 WOE

当前脚本定义为：

\[
WOE_i = \ln\left(\frac{B\_pct_i}{G\_pct_i}\right)
\]

具体加工过程：

```text
1. 从申请信息表取 duedate_3m_30。
2. 按模型分箱统计每箱 B_i 和 G_i。
3. 计算该箱占全部坏样本的比例 B_pct_i。
4. 计算该箱占全部好样本的比例 G_pct_i。
5. 计算 ln(B_pct_i / G_pct_i)。
```

解释：

| WOE | 当前脚本定义下的含义 |
|---:|---|
| `> 0` | 坏样本在该箱相对更集中，风险较高 |
| `< 0` | 好样本在该箱相对更集中，风险较低 |
| `≈ 0` | 好坏样本分布接近整体 |

部分评分卡资料使用 `ln(G_pct/B_pct)`，符号会与当前脚本相反。评审时必须以脚本定义为准。

## 10.4 WOE 为 0 的处理

代码先将 0 替换为 NaN：

```python
B_pct.replace(0, np.nan)
G_pct.replace(0, np.nan)
```

计算后再：

```python
WOE.fillna(0)
```

因此当前真实口径是：

```text
若某箱没有坏样本，WOE = 0；
若某箱没有好样本，WOE = 0。
```

该处理可以避免无穷值，但会把“全好箱”或“全坏箱”错误地表现为没有区分能力。

更常见的标准做法是加平滑项，例如：

\[
B_i^* = B_i + 0.5, \qquad G_i^* = G_i + 0.5
\]

然后再计算占比和 WOE。

## 10.5 单箱 IV 分量

\[
IV_i = (B\_pct_i-G\_pct_i)\times WOE_i
\]

## 10.6 总 IV

\[
IV = \sum_{i=1}^{K} IV_i
\]

在该脚本中，IV 描述的是“模型分经过当前分箱后，对 3M30 好坏样本的分离程度”，不是单个原始特征的 IV。

一般关注：

- 初始 20 箱 IV；
- ChiMerge 后 IV；
- OOT IV；
- 合并前后 IV 损失；
- 调优集与 OOT 的 IV 衰减。

---

# 11. Spearman 单调性

## 11.1 使用字段

| 输入 | 来源 |
|---|---|
| 风险箱序号 | 分箱统计结果的行索引 `1,2,...,K` |
| 箱内坏账率 | 由 `duedate_3m_30` 计算的 `bad_rate` |

计算：

\[
\rho = Spearman(BinOrder, BadRate)
\]

当前箱按模型分从低到高排列，且当前配置为高分高风险，因此理想结果为：

```text
ρ 接近 +1
```

代码提示逻辑为：

```python
if abs(rho) < 0.9:
    提示单调性较差
```

但从业务方向看，更严格的判断应为：

```text
rho < 0.9 时提示
```

因为 `rho ≈ -1` 虽然绝对值很高，但代表风险方向完全相反。

## 11.2 局部倒挂

对于相邻箱 A、B，若 B 是更高分、更高风险箱，则理想情况为：

\[
bad\_rate_B \ge bad\_rate_A
\]

若出现：

\[
bad\_rate_B < bad\_rate_A
\]

则定义为局部倒挂，并可触发 ChiMerge 合并。

---

# 12. 相邻箱卡方检验

## 12.1 主标签 3M30 的 2×2 表

对于相邻箱 A、B：

| 箱 | 坏样本 | 好样本 |
|---|---:|---:|
| A | `SUM(duedate_3m_30)` | `n_A - SUM(duedate_3m_30)` |
| B | `SUM(duedate_3m_30)` | `n_B - SUM(duedate_3m_30)` |

形成：

```python
[
    [B_A, n_A - B_A],
    [B_B, n_B - B_B],
]
```

## 12.2 辅助标签 1M30 的 2×2 表

对于同一对相邻箱，脚本还会取：

```text
申请信息表.duedate_1m_30
```

但在进入 ChiMerge 前，NULL 已被填成 0。

因此 1M30 的坏样本数为：

\[
B^{1M30}_A = \sum duedate\_1m\_30
\]

分母仍使用基于主分箱样本形成的 `n_A`，即：

\[
G^{1M30}_A = n_A-B^{1M30}_A
\]

这相当于把 1M30 缺失样本作为 1M30 好样本。

## 12.3 卡方统计量

对每个标签分别计算：

\[
\chi^2 = \sum \frac{(O-E)^2}{E}
\]

原假设：

```text
相邻两个箱在该标签下的好坏样本分布没有显著差异。
```

判断：

| p 值 | 当前脚本解释 |
|---:|---|
| `p >= 0.05` | 没有足够证据认为两箱不同，可视为差异不显著 |
| `p < 0.05` | 两箱风险分布存在显著差异 |

## 12.4 双标签如何共同决定“差异不显著”

脚本同时计算：

```text
p_3m30
p_1m30
```

只有当两个标签都满足：

```text
p_3m30 >= 0.05
且
p_1m30 >= 0.05
```

才会将该相邻箱对标记为：

```text
相邻箱差异不显著
```

也就是说，只要其中任意一个标签差异显著，就不能仅以“差异不显著”为理由合并。

---

# 13. 相邻箱坏账率 Z 检验

Z 检验只使用主标签 `duedate_3m_30` 的箱内坏账率。

设：

\[
r_A=\frac{B_A}{n_A}, \qquad r_B=\frac{B_B}{n_B}
\]

标准误：

\[
SE_{A,B}=\sqrt{\frac{r_A(1-r_A)}{n_A}+\frac{r_B(1-r_B)}{n_B}}
\]

Z 值：

\[
z=\frac{r_B-r_A}{SE_{A,B}}
\]

双侧 p 值：

\[
p_z=2\times(1-\Phi(|z|))
\]

解释：

| 结果 | 含义 |
|---|---|
| `z > 0` | 更高风险箱 B 的坏账率高于 A |
| `z < 0` | B 的坏账率低于 A，存在倒挂倾向 |
| `p_z < 0.05` | 两箱坏账率差异显著 |
| `p_z >= 0.05` | 两箱坏账率差异不显著 |

当前 Z 检验用于报告展示，不直接决定 ChiMerge 候选箱；ChiMerge 的显著性判断使用双标签卡方检验。

---

# 14. ChiMerge 动态合并：每一步到底怎么决定

## 14.1 初始输入

| 输入 | 来源 |
|---|---|
| 初始切点 `bins` | 调优集模型分的等频分位点 |
| 主标签 | `duedate_3m_30` |
| 辅助标签 | `duedate_1m_30` |
| 样本量 | 每个当前箱的 `COUNT(score)` |
| 主标签坏样本数 | 每箱 `SUM(duedate_3m_30)` |

## 14.2 每轮重新分箱

每删除一个相邻边界后，脚本会使用新的 `bins` 再次执行：

```python
pd.cut(score, bins=bins, include_lowest=True)
```

然后重新计算每箱：

- 样本数；
- 3M30 坏样本数；
- 3M30 坏账率；
- 3M30 卡方 p 值；
- 1M30 卡方 p 值；
- 是否倒挂；
- 是否样本不足；
- 是否坏样本不足。

## 14.3 可成为合并候选的条件

相邻箱对满足以下任意一个条件，就会进入候选列表。

### 条件 A：当前箱数超过 10

```text
当前箱数 > CHIMERGE_MAX_BINS
```

此时所有相邻箱对都会因为“箱数超过上限”成为候选，目的是先把箱数压到 10 箱以内。

### 条件 B：两个标签均差异不显著

```text
p_3m30 >= 0.05
且
p_1m30 >= 0.05
```

### 条件 C：3M30 坏账率局部倒挂

当前配置高分高风险，因此：

```text
右侧高分箱坏账率 < 左侧低分箱坏账率
```

即：

\[
r_B<r_A
\]

### 条件 D：样本量不足

```text
n_A < 3000
或
n_B < 3000
```

### 条件 E：3M30 坏样本不足

```text
B_A < 100
或
B_B < 100
```

注意这里只检查主标签 `duedate_3m_30` 的坏样本数，不检查 1M30 坏样本数是否低于 100。

## 14.4 候选箱对的排序规则

对每一对相邻箱，代码先计算：

\[
min\_p=\min(p_{3M30},p_{1M30})
\]

随后按以下键降序排序：

```python
(p_value, -n_pair)
```

其中：

```text
p_value = min(p_3m30, p_1m30)
n_pair = n_A + n_B
```

因此实际优先级为：

1. 优先选择 `min(p_3m30, p_1m30)` 最大的相邻箱对；
2. 若 p 值相同，优先选择合计样本量更小的相邻箱对。

这意味着脚本希望优先合并在两个标签下都相对相似的箱；当相似度相同，先处理较小的箱对。

## 14.5 合并动作

假设相邻箱 A、B 的公共边界为：

```text
bins[i+1]
```

合并动作就是删除该边界：

```python
del bins[i + 1]
```

例如：

```text
原区间：(-∞, 0.20]、(0.20, 0.30]
删除边界 0.20
合并后：(-∞, 0.30]
```

## 14.6 停止条件

循环最低不能少于 6 箱：

```text
当前箱数 <= 6 时停止
```

在箱数不超过 10 后，若所有相邻箱均同时满足：

- 两个标签中至少一个差异显著；
- 不倒挂；
- 两箱样本都不低于 3000；
- 两箱主标签坏样本都不低于 100；

则停止继续合并。

---

# 15. 合并后风险等级与阈值边界

## 15.1 最终分箱

使用 ChiMerge 返回的切点：

```python
tuning_valid["merged_bin"] = pd.cut(
    tuning_valid[SCORE_COL],
    bins=merged_bins_for_scoring,
    include_lowest=True,
)
```

首尾切点已扩展为 `-∞` 和 `+∞`。

## 15.2 风险等级顺序

当前高分高风险，因此：

```text
风险等级 1：最低分、最低风险
风险等级 K：最高分、最高风险
```

## 15.3 当前代码中的“阈值”并不完全等于理论切点

`boundary_for_risk_bins()` 并没有直接读取 `merged_bins` 中的真实边界，而是从分箱统计表中取：

```text
score_max
```

即当前箱样本中实际观察到的最高分。

例如：

```text
理论箱边界：0.3000
当前箱最大实际分：0.2978
```

脚本会把 `0.2978` 当作策略阈值，而不是 `0.3000`。

这在调优样本上通常差异不大，但存在以下问题：

- 无法完整表达原始区间边界；
- 线上复现时可能出现边界附近样本归属不一致；
- OOT 中同一风险箱的观察最大分可能不同，导致 OOT 方案阈值发生变化。

推荐改为直接使用 `merged_bins` 的内部切点作为正式策略阈值。

## 15.4 区间开闭展示不一致

`pd.qcut` 和 `pd.cut` 默认生成右闭区间，通常为：

```text
(a, b]
```

但脚本 Markdown 展示函数写成：

```text
[a, b)
```

因此报告中的括号方向与实际 pandas 分箱规则不一致。正式文档建议统一展示为：

```text
最低风险箱：[-∞, b1]
中间箱：(b1, b2]
最高风险箱：(bK-1, +∞]
```

---

# 16. OOT 验证口径

## 16.1 OOT 分箱使用什么切点

OOT 使用调优集得到的固定切点，不重新做 `qcut`，也不重新执行 ChiMerge：

```python
oot_valid["merged_bin"] = pd.cut(
    oot_valid[SCORE_COL],
    bins=merged_bins_for_scoring,
    include_lowest=True,
)
```

这是正确的 OOT 分箱原则：

```text
切点由调优集确定；
OOT 只用于验证，不参与重新调箱。
```

## 16.2 OOT 重新计算哪些指标

在同一组分箱下，重新计算：

- 每箱样本量；
- 每箱 3M30 坏样本数；
- 每箱 3M30 坏账率；
- WOE；
- IV；
- Spearman；
- AUC；
- KS；
- PSI。

## 16.3 OOT 方案验证的当前问题

虽然 OOT 分箱本身使用固定切点，但脚本随后调用：

```python
design_three_schemes(oot_valid, oot_merged_stats, ...)
```

该函数会根据 OOT 每箱观察到的 `score_min/score_max` 重新生成方案阈值，而不是直接使用调优集方案的 `auto_max/review_max`。

因此当前报告中的“方案 OOT 验证”不是严格意义上的固定阈值回放，而是：

```text
使用相同风险箱结构，但根据 OOT 箱内实际最大分重新确定阈值。
```

标准做法应为：

```text
1. 在调优集确定 auto_threshold 和 review_threshold；
2. 将完全相同的两个数值直接应用到 OOT；
3. 计算 OOT 通过率、坏账率和拒绝率。
```

---

# 17. PSI：从哪个字段计算、如何加工

## 17.1 输入

PSI 不直接使用标签，只使用各风险箱的样本占比。

| 数据集 | 计数字段 | 分箱 |
|---|---|---|
| 调优集 | `COUNT(score)` | 合并后固定风险箱 |
| OOT 集 | `COUNT(score)` | 同一组固定风险箱 |

## 17.2 调优集箱占比

\[
e_i=\frac{n^{tuning}_i}{\sum_i n^{tuning}_i}
\]

## 17.3 OOT 箱占比

\[
a_i=\frac{n^{oot}_i}{\sum_i n^{oot}_i}
\]

## 17.4 单箱 PSI

\[
PSI_i=(a_i-e_i)\times\ln\left(\frac{a_i}{e_i}\right)
\]

## 17.5 总 PSI

\[
PSI=\sum_i PSI_i
\]

## 17.6 零占比处理

当前代码将 0 替换为：

```text
1e-10
```

避免 `ln(0)`。

## 17.7 当前实现的限制

`compute_bin_stats()` 会删除样本数为 0 的箱。如果 OOT 某个风险箱完全没有样本，调优统计和 OOT 统计的行数可能不同。

代码遇到：

```text
len(stats_tuning) != len(stats_oot)
```

会直接返回：

```text
PSI = None
```

更稳妥的做法是基于完整风险箱清单进行左连接，将缺失箱样本数补 0 后再计算 PSI。

---

# 18. AUC 与 KS：实际计算过程

## 18.1 使用字段

| 输入 | 来源 |
|---|---|
| `scores` | 模型分表的模型分字段 |
| `labels` | 申请信息表的 `duedate_3m_30` |

先排除：

```text
score 为 NaN 或 label 为 NaN 的样本
```

## 18.2 风险排序

当前高分高风险：

```python
order = np.argsort(-scores)
```

即按模型分从高到低排序，最风险样本排在前面。

## 18.3 TPR

累计坏样本数：

\[
cum\_bad_k=\sum_{j=1}^{k}y_j
\]

总坏样本数：

\[
total\_bad=\sum_j y_j
\]

\[
TPR_k=\frac{cum\_bad_k}{total\_bad}
\]

## 18.4 FPR

累计好样本数：

\[
cum\_good_k=\sum_{j=1}^{k}(1-y_j)
\]

总好样本数：

\[
total\_good=N-total\_bad
\]

\[
FPR_k=\frac{cum\_good_k}{total\_good}
\]

## 18.5 KS

\[
KS=\max_k|TPR_k-FPR_k|
\]

## 18.6 AUC

脚本使用 ROC 曲线相邻点的梯形面积求和：

\[
AUC=\sum_k\frac{TPR_k+TPR_{k-1}}{2}\times(FPR_k-FPR_{k-1})
\]

## 18.7 同分样本问题

当前实现直接对每一条记录排序，没有对相同分数进行统一聚合或平均秩处理。因此在大量同分样本存在时，同分样本内部的排序可能影响 AUC 和 KS 的细微结果。

若需与 `sklearn.metrics.roc_auc_score` 或标准模型评估平台完全对齐，建议使用标准库函数，并明确同分处理方式。

---

# 19. 累计阈值曲线

## 19.1 候选阈值从哪里来

候选阈值由两部分组成。

### 来源一：调优集模型分的 20 个分位点

```python
percentiles = np.linspace(5, 100, 20)
thresholds = np.percentile(scores, percentiles)
```

即：

```text
5%、10%、15%、...、100% 分位数
```

### 来源二：ChiMerge 内部边界

```text
merged_bins[1:-1]
```

首尾 `-∞/+∞` 不作为阈值。

最终会：

- 合并两类阈值；
- 四舍五入到 8 位；
- 去重；
- 从低到高排序。

## 19.2 每个阈值的通过人群

当前高分高风险：

```text
score <= threshold
```

设阈值为 \(t\)，通过集合为：

\[
P(t)=\{j\mid score_j\le t\}
\]

## 19.3 阈值累计样本数

\[
cum\_n(t)=\operatorname{COUNT}\{j:score_j\le t\}
\]

## 19.4 阈值累计坏样本数

来源字段：`duedate_3m_30`

\[
cum\_B(t)=\sum_{j:score_j\le t}duedate\_3m\_30_j
\]

## 19.5 阈值累计通过率

| 分子 | 分母 |
|---|---|
| `score <= t` 的样本数 | `tuning_valid` 总行数 |

\[
cum\_pass\_rate(t)=\frac{cum\_n(t)}{N_{tuning\_valid}}
\]

注意：分母使用 `len(df)`，可能包含模型分为空的样本；而分子不会包含模型分为空的样本。因此模型分缺失会压低累计通过率。

## 19.6 阈值累计坏账率：笔数口径

| 分子 | 分母 |
|---|---|
| 通过人群 `SUM(duedate_3m_30)` | 通过人群样本数 |

\[
cum\_bad\_rate_{count}(t)=\frac{cum\_B(t)}{cum\_n(t)}
\]

## 19.7 边际样本数

假设候选阈值按从低到高排列为 \(t_1,t_2,...,t_m\)：

\[
marginal\_n_k=cum\_n(t_k)-cum\_n(t_{k-1})
\]

第一个阈值：

\[
marginal\_n_1=cum\_n(t_1)
\]

## 19.8 边际坏样本数

\[
marginal\_B_k=cum\_B(t_k)-cum\_B(t_{k-1})
\]

## 19.9 边际坏账率：笔数口径

\[
marginal\_bad\_rate_{count,k}=\frac{marginal\_B_k}{marginal\_n_k}
\]

业务解释：阈值从上一个点放宽到当前点时，新增加的那批边际客群的 3M30 坏账率。

---

# 20. 金额口径风险：逐字段拆解

金额口径是当前文档中最容易误解的指标，需要特别明确。

## 20.1 来源字段

| 作用 | 来源文件 | 字段 |
|---|---|---|
| 风险标识 | 申请信息表 | `dpd_days_ever_mob3` |
| 分子金额 | 申请信息表 | `estimate_principal_remaining_mob3` |
| 分母金额 | 申请信息表 | `principal` |
| 阈值人群 | 模型分表 | 模型分字段 |

## 20.2 风险标识

代码：

```python
amount_labels = (dpd_days_ever_mob3 >= 30).astype(int)
```

加工结果：

```text
dpd_days_ever_mob3 >= 30 → amount_label = 1
dpd_days_ever_mob3 < 30  → amount_label = 0
```

## 20.3 有效金额样本

代码要求：

```text
estimate_principal_remaining_mob3 非空且 > 0
principal 非空且 > 0
```

当前代码还写了 `amount_label.notna()`，但 `dpd_days_ever_mob3 >= 30` 已经返回布尔值；当原字段为 NULL 时，比较结果通常为 False，因此该判断无法真正排除 DPD 缺失样本。

所以当前实际口径很可能是：

```text
DPD 缺失会被当作 amount_label = 0，即非风险样本。
```

推荐先对原始字段判断非空：

```python
valid = (
    estimate_principal_remaining_mob3.notna()
    & (estimate_principal_remaining_mob3 > 0)
    & principal.notna()
    & (principal > 0)
    & dpd_days_ever_mob3.notna()
)
```

## 20.4 金额口径分子

对于阈值通过人群和有效金额样本：

\[
BadAmount(t)=\sum_{j\in P(t)}
estimate\_principal\_remaining\_mob3_j
\times I(dpd\_days\_ever\_mob3_j\ge30)
\]

即只有 MOB3 历史最大逾期达到 30 天的申请，其 MOB3 剩余本金才进入分子。

## 20.5 金额口径分母

\[
TotalPrincipal(t)=\sum_{j\in P(t)}principal_j
\]

但该求和只覆盖同时满足以下条件的记录：

```text
estimate_principal_remaining_mob3 > 0
principal > 0
```

## 20.6 金额口径风险率

\[
cum\_bad\_rate_{amount}(t)
=
\frac{BadAmount(t)}{TotalPrincipal(t)}
\]

完整展开：

\[
\frac{
\sum estimate\_principal\_remaining\_mob3
\times I(dpd\_days\_ever\_mob3\ge30)
}{
\sum principal
}
\]

## 20.7 这个指标不等于传统金额坏账率

该指标分子使用：

```text
MOB3 剩余本金
```

分母使用：

```text
原始放款本金
```

因此分子和分母不是同一时点、也不是同一金额基础。它更接近：

```text
MOB3 30+ 风险剩余本金 / 原始放款本金
```

不应简单命名为“金额坏账率”，建议在报告中使用更准确的名称：

> **MOB3 30+ 风险剩余本金率**

## 20.8 示例

假设通过人群有 3 笔：

| 申请 | principal | MOB3 剩余本金 | MOB3 最大 DPD | 是否计入分子 |
|---|---:|---:|---:|---:|
| A | 1,000 | 600 | 35 | 是，计 600 |
| B | 2,000 | 1,000 | 10 | 否，计 0 |
| C | 1,500 | 500 | 45 | 是，计 500 |

则：

\[
分子=600+0+500=1,100
\]

\[
分母=1,000+2,000+1,500=4,500
\]

\[
MOB3\ 30+风险剩余本金率=1,100/4,500=24.44\%
\]

---

# 21. 三套策略方案：阈值如何形成

三套方案基于合并后的风险箱数量自动生成。

设最终风险箱数为：

\[
K=len(risk\_stats)
\]

当前高分高风险，因此风险箱已按低分到高分排列。

## 21.1 保守方案

自动通过箱数：

\[
K^{cons}_{auto}=\max\left(1,\left\lfloor\frac{K}{3}\right\rfloor\right)
\]

进入非拒绝范围的箱数：

\[
K^{cons}_{review}=\max\left(K^{cons}_{auto}+1,\left\lfloor\frac{2K}{3}\right\rfloor\right)
\]

策略：

```text
最低风险前 K_auto 个箱：自动通过
其后直到 K_review：人工审核
剩余高风险箱：拒绝
```

## 21.2 平衡方案

自动通过箱数：

\[
K^{bal}_{auto}=\max\left(1,\left\lfloor\frac{K}{2}\right\rfloor\right)
\]

进入非拒绝范围的箱数：

\[
K^{bal}_{review}=\max(K^{bal}_{auto}+1,K-1)
\]

在通常情况下，这意味着：

```text
约前一半风险箱自动通过；
中间风险箱人工审核；
只拒绝最高风险 1 个箱。
```

## 21.3 增长方案

自动通过箱数：

\[
K^{growth}_{auto}=\max(1,K-1)
\]

人工审核覆盖到：

\[
K^{growth}_{review}=K
\]

策略：

```text
除最高风险箱外自动通过；
最高风险箱人工审核；
不做硬拒绝。
```

## 21.4 自动通过阈值

当前代码取第 `K_auto` 个低风险箱的 `score_max`：

\[
auto\_threshold=score\_max(K_{auto})
\]

自动通过：

```text
score <= auto_threshold
```

## 21.5 人工审核上限

取第 `K_review` 个低风险箱的 `score_max`：

\[
review\_threshold=score\_max(K_{review})
\]

人工审核：

```text
auto_threshold < score <= review_threshold
```

拒绝：

```text
score > review_threshold
```

增长方案不执行硬拒绝。

## 21.6 每个策略段的样本量

来源：`tuning_valid` 或当前传入的数据集。

\[
n_{segment}=\operatorname{COUNT}(segment)
\]

## 21.7 每个策略段的样本占比

\[
pct_{segment}=\frac{n_{segment}}{N_{df}}
\]

注意：分母为传入 DataFrame 的总行数，可能包含模型分为空且未进入任何策略段的样本。

## 21.8 每个策略段的笔数坏账率

来源标签：`duedate_3m_30`

\[
bad\_rate_{count,segment}
=
\frac{\sum_{j\in segment}duedate\_3m\_30_j}{n_{segment}}
\]

## 21.9 方案通过率

对于保守和平衡方案：

```text
通过范围 = 自动通过 + 人工审核
```

即：

\[
Pass=\{j:score_j\le review\_threshold\}
\]

\[
pass\_rate=\frac{|Pass|}{N_{df}}
\]

对于增长方案，代码直接设置：

```python
approved = df
```

所以：

```text
pass_rate = 100%
reject_rate = 0%
```

这代表“无硬拒绝”，并不代表所有申请最终无需人工判断即可放款。

## 21.10 通过人群坏账率

\[
pass\_bad\_rate_{count}
=
\frac{\sum_{j\in Pass}duedate\_3m\_30_j}{|Pass|}
\]

## 21.11 拒绝率

保守和平衡方案：

\[
reject\_rate=\frac{N_{df}-|Pass|}{N_{df}}
\]

增长方案：

\[
reject\_rate=0
\]

## 21.12 方案按箱数切分的局限

当前方案仅按“风险箱数量”切分，不直接考虑：

- 每箱样本占比；
- 审核团队产能；
- 目标通过率；
- 风险预算；
- 单位经济性；
- 预计损失；
- 收入和利润。

例如最终 6 个箱的样本占比可能并不均匀。前三箱不一定正好覆盖 50% 的申请。

因此三套方案更适合作为初始候选方案，而不是最终上线阈值。

---

# 22. 指标口径汇总表

| 指标 | 来源文件与字段 | 过滤/加工 | 公式 |
|---|---|---|---|
| 箱样本数 | 模型分表.`score` | 当前箱，排除 score NULL | `COUNT(score)` |
| 3M30 坏样本数 | 申请信息表.`duedate_3m_30` | 当前箱，标签应为 0/1 | `SUM(duedate_3m_30)` |
| 3M30 坏账率 | 上述两个字段 | 当前箱 | `SUM(label)/COUNT(score)` |
| 好样本数 | `n`、`B` | 二次加工 | `n-B` |
| 标准误 | `bad_rate`、`n` | 二次加工 | `sqrt(r(1-r)/n)` |
| 累计通过率 | 各箱 `n` | 低风险到高风险累计 | `cum_n/total_N` |
| 累计坏账率 | 各箱 `B`、`n` | 低风险到高风险累计 | `cum_B/cum_n` |
| Lift | 箱坏账率、整体坏账率 | 二次加工 | `bin_bad_rate/overall_bad_rate` |
| WOE | 每箱好坏样本分布 | 0 占比当前填 0 | `ln(B_pct/G_pct)` |
| IV | `B_pct`、`G_pct`、`WOE` | 各箱求和 | `Σ(B_pct-G_pct)×WOE` |
| Spearman | 箱序、箱坏账率 | 箱按分数升序 | `Spearman(bin_order,bad_rate)` |
| 卡方 p 值 | `duedate_3m_30`、`duedate_1m_30` | 相邻箱 2×2 表 | `chi2_contingency` |
| Z 值 | 3M30 箱坏账率与样本数 | 相邻箱 | `(rB-rA)/SE` |
| PSI | 调优/OOT 每箱样本占比 | 0 替换为 `1e-10` | `Σ(a-e)ln(a/e)` |
| AUC | 模型分、`duedate_3m_30` | 按高风险到低风险排序 | ROC 梯形面积 |
| KS | 模型分、`duedate_3m_30` | 按高风险到低风险排序 | `max|TPR-FPR|` |
| 累计笔数坏账率 | 模型分、`duedate_3m_30` | `score<=threshold` | `SUM(label)/COUNT(pass)` |
| 边际坏账率 | 相邻两个累计阈值结果 | 差分 | `Δ坏样本/Δ样本` |
| MOB3 30+ 风险剩余本金率 | `estimate_principal_remaining_mob3`、`principal`、`dpd_days_ever_mob3` | 金额均 >0，DPD>=30 计分子 | `Σ剩余本金×I(DPD>=30)/Σprincipal` |
| 方案通过率 | 模型分 | 通过范围内样本 | `pass_n/total_n` |
| 方案坏账率 | `duedate_3m_30` | 通过范围内样本 | `SUM(label)/pass_n` |

---

# 23. 等价 SQL 口径示例

以下 SQL 用于帮助理解，不代表当前脚本可直接在某一数据库运行；物理表名和函数语法需要按实际数仓修改。

## 23.1 构建分析宽表

```sql
WITH merged AS (
    SELECT
        s.application_id,
        CAST(s.sample_datetime AS TIMESTAMP) AS sample_datetime,
        CAST(s.aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score AS DOUBLE) AS score,
        CAST(a.duedate_3m_30 AS INT) AS duedate_3m_30,
        CAST(a.duedate_1m_30 AS INT) AS duedate_1m_30,
        CAST(a.principal AS DOUBLE) AS principal,
        CAST(a.estimate_principal_remaining_mob3 AS DOUBLE) AS estimate_principal_remaining_mob3,
        CAST(a.dpd_days_ever_mob3 AS DOUBLE) AS dpd_days_ever_mob3
    FROM score_table s
    INNER JOIN application_info_table a
        ON s.application_id = a.application_id
)
SELECT *
FROM merged;
```

## 23.2 调优有效样本

```sql
SELECT *
FROM merged
WHERE sample_datetime < TIMESTAMP '2025-10-21 00:00:00'
  AND duedate_3m_30 IS NOT NULL
  AND score IS NOT NULL;
```

其中最后一条 `score IS NOT NULL` 是建议补充的标准过滤，当前 Python 没有显式执行。

## 23.3 某风险箱的基础指标

```sql
SELECT
    risk_bin,
    MIN(score) AS score_min,
    MAX(score) AS score_max,
    COUNT(score) AS n,
    SUM(duedate_3m_30) AS bad_n,
    COUNT(score) - SUM(duedate_3m_30) AS good_n,
    SUM(duedate_3m_30) * 1.0 / COUNT(score) AS bad_rate
FROM tuning_valid
GROUP BY risk_bin;
```

## 23.4 某策略阈值的累计笔数风险

```sql
SELECT
    COUNT(*) AS pass_n,
    SUM(duedate_3m_30) AS pass_bad_n,
    COUNT(*) * 1.0 / total_sample_n AS pass_rate,
    SUM(duedate_3m_30) * 1.0 / COUNT(*) AS pass_bad_rate
FROM tuning_valid
WHERE score <= :threshold;
```

## 23.5 建议的金额口径 SQL

```sql
SELECT
    SUM(
        CASE
            WHEN dpd_days_ever_mob3 >= 30
            THEN estimate_principal_remaining_mob3
            ELSE 0
        END
    ) / SUM(principal) AS mob3_30plus_risk_remaining_principal_rate
FROM tuning_valid
WHERE score <= :threshold
  AND estimate_principal_remaining_mob3 IS NOT NULL
  AND estimate_principal_remaining_mob3 > 0
  AND principal IS NOT NULL
  AND principal > 0
  AND dpd_days_ever_mob3 IS NOT NULL;
```

---

# 24. 当前代码与建议正式口径的差异

| 问题 | 当前代码实际逻辑 | 建议正式口径 | 影响 |
|---|---|---|---|
| 物理数据血缘 | 只记录 CSV 名 | 补充数据库、Schema、表名、分区 | 无法追溯字段源头 |
| `application_id` 重复 | 不检查、不去重 | 关联前校验唯一性 | 可能多对多扩张 |
| 模型分类型 | 未显式转数值 | `to_numeric(errors='coerce')` 并检查缺失率 | qcut 或比较可能报错 |
| 模型分缺失 | 未统一排除 | 单独拆分缺失分样本 | 分母与分子范围不一致 |
| 1M30 缺失 | 填 0 | 按 1M30 有效样本单独检验 | 未成熟样本被当好样本 |
| WOE 零频 | WOE 填 0 | 使用 0.5 平滑或其他约定 | 低估极端箱区分能力 |
| 累计方向 | 固定按分数升序 | 根据分数方向动态排序 | 改成高分低风险时会错 |
| 风险箱展示 | 报告写 `[a,b)` | pandas 实际通常为 `(a,b]` | 边界解释不一致 |
| 策略阈值 | 使用箱内观察 `score_max` | 使用真实 `merged_bins` 切点 | 线上边界可能不一致 |
| OOT 方案验证 | 用 OOT `score_max` 重新定阈值 | 固定使用调优集阈值 | OOT 结果不是真正回放 |
| PSI 空箱 | 行数不等直接返回 None | 完整箱清单补 0 | 无法识别空箱漂移 |
| DPD 缺失 | 可能被当作非风险 | 原字段非空才进入金额口径 | 金额风险被低估 |
| 金额指标命名 | “金额坏账率” | “MOB3 30+ 风险剩余本金率” | 避免误解分子分母 |
| AUC 同分 | 未做标准同分处理 | 使用标准库并对齐平台 | 结果可能有细微差异 |
| 策略方案 | 按箱数切分 | 增加目标通过率、风险预算、审核产能 | 不一定适合上线 |

---

# 25. 推荐的正式字段口径表

为了让模型、策略和数据团队后续不再重复确认，建议将以下内容固化为配置表。

| 配置项 | 推荐记录内容 |
|---|---|
| 模型名称 | `aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb` |
| 模型分字段 | 完整库表和字段名 |
| 分数方向 | 高分高风险 / 高分低风险 |
| 主标签 | 完整库表、字段、观察期、成熟条件、0/1 定义 |
| 辅助标签 | 完整库表、字段、成熟条件 |
| 样本时间 | `sample_datetime` 的业务含义：申请时间、评分时间或放款时间 |
| 调优时间范围 | 起始时间、结束时间 |
| OOT 时间范围 | 起始时间、结束时间 |
| 初始分箱方式 | 等频 20 箱 |
| 最终切点 | 精确数值及左右开闭规则 |
| 缺失分规则 | 拒绝、审核或单独策略 |
| 计数坏账率 | 分子、分母、样本过滤 |
| 金额风险率 | 分子字段、分母字段、时点和 DPD 条件 |
| 策略阈值 | 自动通过、人工审核、拒绝边界 |
| 版本号 | 分箱版本、策略版本、生效日期 |
| 审批人 | 模型、策略、风险、业务负责人 |

---

# 26. 建议的运行前质量检查

## 26.1 数据源检查

- 两个输入文件是否存在；
- 文件日期和数据版本是否正确；
- `application_id` 是否唯一；
- 两表关联率是否符合预期；
- 关联后是否出现行数异常增长。

## 26.2 字段类型检查

- `sample_datetime` 是否全部可解析；
- 模型分是否为数值；
- `duedate_3m_30` 是否只包含 0、1、NULL；
- `duedate_1m_30` 是否只包含 0、1、NULL；
- 本金和剩余本金是否为非负数；
- DPD 是否为非负数或 NULL。

## 26.3 样本检查

至少输出：

```text
模型分表总行数
模型分表 application_id 去重数
申请信息表总行数
申请信息表 application_id 去重数
inner join 后总行数
调优集总行数
调优集 3M30 有效行数
OOT 总行数
OOT 3M30 有效行数
模型分缺失数及缺失率
1M30 缺失数及缺失率
金额字段有效样本数及覆盖率
```

## 26.4 分箱检查

- 初始实际箱数；
- 每箱样本数；
- 每箱坏样本数；
- 每箱分数最小值、最大值和真实切点；
- 是否存在空箱；
- 是否存在局部倒挂；
- 合并后 IV 损失；
- 调优和 OOT 风险方向是否一致。

## 26.5 策略检查

- 阈值是否来自真实切点；
- OOT 是否固定使用调优阈值；
- 自动通过、审核、拒绝三段是否无遗漏、无重叠；
- 缺失分是否有明确归属；
- 通过率分母是否与分段占比分母一致；
- 笔数和金额指标是否使用同一批有效样本，若不同需明确标注。

---

# 27. 推荐的端到端伪代码

```python
# 1. 读取数据
score_df = read_score_table()
info_df = read_application_info_table()

# 2. 字段类型和唯一性检查
assert_unique(score_df, "application_id")
assert_unique(info_df, "application_id")
score_df[SCORE_COL] = to_numeric(score_df[SCORE_COL])
score_df["sample_datetime"] = to_datetime(score_df["sample_datetime"])
validate_binary_label(info_df["duedate_3m_30"])
validate_binary_label(info_df["duedate_1m_30"])

# 3. 关联
merged = inner_join(
    score_df[["application_id", "sample_datetime", SCORE_COL]],
    info_df[[
        "application_id",
        "duedate_3m_30",
        "duedate_1m_30",
        "principal",
        "estimate_principal_remaining_mob3",
        "dpd_days_ever_mob3",
    ]],
    on="application_id",
)

# 4. 分割调优和 OOT
TUNING = merged[sample_datetime < "2025-10-21"]
OOT = merged[sample_datetime >= "2025-10-21"]

# 5. 主模型有效样本
TUNING_VALID = TUNING[
    duedate_3m_30.notna() & score.notna()
]
OOT_VALID = OOT[
    duedate_3m_30.notna() & score.notna()
]

# 6. 调优集初始等频 20 箱
initial_bins = qcut(TUNING_VALID.score, q=20)

# 7. 计算初始箱指标
initial_stats = bin_stats(
    score,
    duedate_3m_30,
    initial_bins,
)

# 8. ChiMerge
# 3M30 和 1M30 应分别使用各自标签有效样本计算卡方
final_bins = chimerge(
    score=TUNING_VALID.score,
    labels=[duedate_3m_30, duedate_1m_30],
    min_bins=6,
    max_bins=10,
)

# 9. 固定切点
TUNING_VALID.final_bin = cut(TUNING_VALID.score, final_bins)
OOT_VALID.final_bin = cut(OOT_VALID.score, final_bins)

# 10. 调优和 OOT 指标
TUNING_STATS = bin_stats(TUNING_VALID)
OOT_STATS = bin_stats(OOT_VALID)
PSI = psi_with_complete_bin_index(TUNING_STATS, OOT_STATS)
AUC_T, KS_T = standard_auc_ks(TUNING_VALID)
AUC_O, KS_O = standard_auc_ks(OOT_VALID)

# 11. 策略阈值必须直接来自 final_bins
AUTO_THRESHOLD = selected_final_bin_edge
REVIEW_THRESHOLD = selected_final_bin_edge

# 12. OOT 固定阈值回放
TUNING_SCHEME = evaluate_scheme(
    TUNING_VALID,
    AUTO_THRESHOLD,
    REVIEW_THRESHOLD,
)
OOT_SCHEME = evaluate_scheme(
    OOT_VALID,
    AUTO_THRESHOLD,
    REVIEW_THRESHOLD,
)

# 13. 输出
write_markdown_report()
```

---

# 28. 最终口径总结

当前脚本的核心主线可以概括为：

> 从模型分表获取 `application_id`、`sample_datetime` 和模型分，从申请信息表获取 `duedate_3m_30`、`duedate_1m_30`、`principal`、`estimate_principal_remaining_mob3` 和 `dpd_days_ever_mob3`，通过 `application_id` 内连接形成分析宽表；按 `sample_datetime = 2025-10-21` 切分调优集与 OOT 集，以 `duedate_3m_30` 非空作为主标签有效条件；在调优集上对模型分做等频 20 箱，再同时参考 3M30、1M30 的相邻箱卡方差异，以及 3M30 坏账率倒挂、样本量和坏样本量进行 ChiMerge；最终使用固定风险箱验证 OOT 的坏账率、IV、AUC、KS 和 PSI，并测算不同阈值下的通过率、3M30 笔数坏账率和 MOB3 30+ 风险剩余本金率，形成保守、平衡和增长三套候选策略。

正式上线前，最需要优先修正的四项是：

1. 补齐两个 CSV 对应的真实数仓表和字段血缘；
2. 使用真实 `merged_bins` 切点，而不是箱内观察到的 `score_max` 作为阈值；
3. OOT 必须固定回放调优集阈值，不能基于 OOT 样本重新生成阈值；
4. 不应将 `duedate_1m_30` 或 `dpd_days_ever_mob3` 的缺失值默认当作正常样本。
