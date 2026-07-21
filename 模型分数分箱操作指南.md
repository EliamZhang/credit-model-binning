# 模型分数分箱与策略阈值操作指南

> 本文档与当前 `binning.py` 的实际执行逻辑对应，按脚本运行顺序说明数据准备、模型分箱、分箱评估、阈值搜索、策略划分和结果表解读。
>
> 当前脚本的核心目标是：以 `score_mlt` 为主模型分，在训练样本上建立稳定风险等级，并划分自动通过、人工审核和拒绝阈值，最终生成 `out/策略报告.xlsx`。

---

## 一、先理解整个流程

当前脚本按以下顺序执行：

```text
加载 5 张 CSV
→ 清理字段名并拼接分析宽表
→ 检查关键字段、重复和缺失
→ 按月份切分 Train / OOT
→ 在 Train 上对 score_mlt 做 20 等频初分
→ 计算每箱规模、1M30+、3M30+、金额风险、Lift 和累计指标
→ 诊断小样本、bad 数不足、倒挂、置信区间重叠和金额/笔数差异
→ 将 20 个初始箱合并为 8 个最终风险等级
→ 在 OOT 和各月份复用相同边界，验证单调性、PSI、AUC 和 KS
→ 生成最终箱边界阈值曲线和细粒度分位点阈值曲线
→ 按风险约束生成保守、平衡、增长三套策略
→ 比较 6/7/8/9 档合箱方案
→ 比较精确边界、4 位小数边界和 3 位小数边界
→ 使用 4 位小数边界重新计算分箱、阈值和策略
→ 扫描人工审核产能和风险上限
→ 输出 Excel 策略报告
```

需要区分两个概念：

- **分箱**：把连续模型分整理为稳定、单调、可解释的风险等级。
- **阈值**：决定哪些分数自动通过、哪些进入人工审核、哪些拒绝。

当前代码按**高分高风险**处理，因此：

```text
低分 = 低风险
高分 = 高风险
自动通过：score_mlt <= 自动通过阈值
人工审核：自动通过阈值 < score_mlt <= 拒绝阈值
拒绝：score_mlt > 拒绝阈值
```

---

## 二、运行准备

### 1. 目录结构

脚本使用相对路径读取数据，因此运行时需要保证当前工作目录包含 `binning.py` 和 `res` 文件夹。

```text
项目目录/
├── binning.py
├── res/
│   ├── sample.csv
│   ├── application_info.csv
│   ├── aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv
│   ├── aus_old_risk_apply_appmodel_v20260318_v1_2_lgb_score.csv
│   └── aus_old_risk_bid_submodel_v20260323_v1_2_txn_lgb_score.csv
└── out/
    └── 策略报告.xlsx               # 运行后生成
```

`out` 文件夹不需要提前创建，代码会自动创建。

### 2. 安装依赖

```bash
pip install numpy pandas scipy statsmodels matplotlib openpyxl
```

当前版本虽然导入了 `matplotlib` 和 `statsmodels`，但没有实际绘图，也没有使用 `statsmodels` 完成统计计算。

### 3. 运行方式

```bash
python binning.py
```

脚本运行完成后会输出：

```text
out/策略报告.xlsx
```

当前代码不会输出 CSV、PNG 或其他单独的中间文件。

### 4. 输入文件及作用

| 输入文件 | 连接字段 | 当前用途 |
| --- | --- | --- |
| `sample.csv` | `application_id`、`user_id` | 分析底表，决定最终保留哪些样本 |
| `application_info.csv` | `application_id`、`user_id` | 补充申请时间、表现标签、本金、审批状态等信息 |
| `aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv` | `application_id` | 提供主模型分，重命名为 `score_mlt` |
| `aus_old_risk_apply_appmodel_v20260318_v1_2_lgb_score.csv` | `application_id` | 提供申请模型分，重命名为 `score_apply`，当前未参与分箱 |
| `aus_old_risk_bid_submodel_v20260323_v1_2_txn_lgb_score.csv` | `application_id` | 补充交易子模型分及交易特征，当前未参与主分箱 |

### 5. 当前必须存在的关键字段

脚本会检查以下字段，缺失时直接报错：

```text
application_id
user_id
application_time
application_month
score_mlt
score_apply
duedate_1m_30
duedate_3m_30
principal
estimate_principal_remaining_mob1
estimate_principal_remaining_mob3
dpd_days_ever_mob1
dpd_days_ever_mob3
```

为了计算审批漏斗，宽表还会尽量保留：

```text
application_status
assessment_status
status
```

但需要注意：当前脚本虽然定义了漏斗计算函数，最终运行流程并没有调用该函数，也没有把漏斗指标写入 Excel。

### 6. 当前核心配置

```python
DATA_DIR = Path('res')
TRAIN_END_MONTH = '2026-03'
OOT_START_MONTH = '2026-04'
HIGH_SCORE_HIGH_RISK = True
SCORE_COL = 'score_mlt'
```

含义：

- `application_month <= 2026-03`：进入 `train`。
- `application_month >= 2026-04`：进入 `oot`。
- 时间为空或不能归入上述范围：进入 `gap_or_unknown`。
- 当前只对 `score_mlt` 建箱和切阈值。
- `application_month` 必须使用可按字符串正确比较的 `YYYY-MM` 格式。

---

## 三、风险标签和指标口径

当前代码同时计算 `1M30+` 和 `3M30+`，并分别提供笔数口径和金额口径。

### 1. 1M30+ 笔数口径

成熟样本：

```text
duedate_1m_30 ∈ {0, 1}
```

坏样本：

```text
duedate_1m_30 = 1
```

公式：

```text
1m30p_cnt_mature = duedate_1m_30 为 0 或 1 的样本数
1m30p_cnt_bad = duedate_1m_30 为 1 的样本数
1m30p_cnt_good = 1m30p_cnt_mature - 1m30p_cnt_bad
1m30p_cnt_bad_rate = 1m30p_cnt_bad / 1m30p_cnt_mature
```

未成熟样本不进入逾期率分母。

### 2. 3M30+ 笔数口径

成熟样本：

```text
duedate_3m_30 ∈ {0, 1}
```

坏样本：

```text
duedate_3m_30 = 1
```

公式：

```text
3m30p_cnt_mature = duedate_3m_30 为 0 或 1 的样本数
3m30p_cnt_bad = duedate_3m_30 为 1 的样本数
3m30p_cnt_good = 3m30p_cnt_mature - 3m30p_cnt_bad
3m30p_cnt_bad_rate = 3m30p_cnt_bad / 3m30p_cnt_mature
```

### 3. 1M30+ 金额口径

成熟条件：

```text
dpd_days_ever_mob1 非空
```

金额风险暴露：

```text
1m30p_amt_exposure
= 所有 dpd_days_ever_mob1 非空样本的 principal 之和
```

逾期金额：

```text
1m30p_amt_bad
= dpd_days_ever_mob1 >= 30 样本的 estimate_principal_remaining_mob1 之和
```

金额逾期率：

```text
1m30p_amt_bad_rate
= 1m30p_amt_bad / 1m30p_amt_exposure
```

### 4. 3M30+ 金额口径

成熟条件：

```text
dpd_days_ever_mob3 非空
```

金额风险暴露：

```text
3m30p_amt_exposure
= 所有 dpd_days_ever_mob3 非空样本的 principal 之和
```

逾期金额：

```text
3m30p_amt_bad
= dpd_days_ever_mob3 >= 30 样本的 estimate_principal_remaining_mob3 之和
```

金额逾期率：

```text
3m30p_amt_bad_rate
= 3m30p_amt_bad / 3m30p_amt_exposure
```

### 5. 通用规模指标

```text
n = 箱内 application_id 行数
application_id_nunique = 箱内唯一 application_id 数
principal_amt = 箱内 principal 合计
sample_pct = 箱内样本数 / 全部箱样本数
```

当前分箱统计使用 `application_id` 的行数计算 `n`。脚本会检查重复，但不会因为重复自动停止；如果宽表中一笔申请存在多行，风险指标会按多行计算，因此正式运行前应确保主键口径正确。

### 6. Lift

```text
某箱 Lift = 某箱逾期率 / 全体成熟样本逾期率
```

例如：

```text
3m30p_cnt_lift
= 该箱 3M30+ 笔数逾期率 / Train 整体 3M30+ 笔数逾期率
```

解读：

- `Lift < 1`：风险低于整体水平。
- `Lift = 1`：风险接近整体水平。
- `Lift > 1`：风险高于整体水平。

### 7. 标准误和 Wilson 置信区间

代码只对笔数逾期率计算标准误和 Wilson 置信区间：

```text
1m30p_cnt_bad_rate_se
1m30p_cnt_bad_rate_ci_lower
1m30p_cnt_bad_rate_ci_upper
3m30p_cnt_bad_rate_se
3m30p_cnt_bad_rate_ci_lower
3m30p_cnt_bad_rate_ci_upper
```

金额逾期率是本金加权比例，不是标准二项比例，因此当前代码不计算金额逾期率置信区间。

### 8. 累计指标

最终箱按 `bin_order` 从低风险向高风险排序，然后逐箱累计：

```text
cum_n
cum_principal
cum_pass_rate
cum_1m30p_cnt_mature
cum_1m30p_cnt_bad
cum_1m30p_cnt_bad_rate
cum_3m30p_cnt_mature
cum_3m30p_cnt_bad
cum_3m30p_cnt_bad_rate
cum_1m30p_amt_exposure
cum_1m30p_amt_bad
cum_1m30p_amt_bad_rate
cum_3m30p_amt_exposure
cum_3m30p_amt_bad
cum_3m30p_amt_bad_rate
```

累计指标表示：如果阈值放宽到当前箱右边界，累计接纳人群的规模和风险是多少。

---

## 四、代码实际执行步骤

### 1. 加载并清理 CSV

代码首先读取 5 张 CSV，并清理字段名中的 UTF-8 BOM 和少量乱码前缀。

主要对象：

```text
sample
app
mlt_score
apply_score
txn_score
```

运行日志会打印每张表的行列数。

查看重点：

- 文件是否成功读取。
- 行数是否符合预期。
- 字段名是否存在乱码。

### 2. 拼接分析宽表

拼接顺序：

1. 以 `sample` 为底表。
2. 使用 `application_id + user_id` 左连接 `application_info.csv`。
3. 模型分表先按 `application_id` 去重，再左连接。
4. `mlt_score` 和 `apply_score` 重复记录均保留第一条。
5. 交易子模型表保留除主键、时间和错误字段外的其他字段。
6. 只保留分箱、表现和审批分析需要的字段。
7. 日期字段转为日期类型，数值字段转为数值类型。

主要结果：

```text
df
```

查看重点：

- `sample` 与 `df` 行数是否一致。
- 模型分去重前后减少了多少记录。
- `score_mlt` 缺失率是否可接受。
- 表现标签、本金和剩余本金缺失率是否合理。

注意：去重逻辑是 `drop_duplicates(..., keep='first')`，没有按模型时间或更新时间排序。如果同一申请存在多个有效版本，应先在源数据层明确保留规则。

### 3. 数据质量校验

脚本生成：

```text
data_quality
key_checks
```

`data_quality` 字段：

| 字段 | 含义 |
| --- | --- |
| `column` | 字段名 |
| `dtype` | 数据类型 |
| `missing_cnt` | 缺失数量 |
| `missing_rate` | 缺失比例 |
| `nunique` | 非空唯一值数量 |

`key_checks` 包括：

```text
sample_rows
df_rows
sample_application_id_dup
df_application_id_dup
mlt_score_application_id_dup
apply_score_application_id_dup
txn_score_application_id_dup
application_month_min
application_month_max
application_month_nunique
```

这些结果只在脚本运行过程中计算，没有写入最终 Excel。

### 4. 切分 Train、OOT 和未知样本

```text
train：application_month <= 2026-03
oot：application_month >= 2026-04
gap_or_unknown：其他情况
```

主要结果：

```text
df['sample_group']
train_df
oot_df
split_summary
```

`split_summary` 字段：

| 字段 | 含义 |
| --- | --- |
| `sample_group` | `train`、`oot` 或 `gap_or_unknown` |
| `n` | 样本行数 |
| `application_id_nunique` | 唯一申请数 |
| `month_min`、`month_max` | 时间范围 |
| `score_mlt_missing_rate` | 主模型分缺失率 |
| `m1_mature` | 1M30+ 成熟样本数 |
| `m3_mature` | 3M30+ 成熟样本数 |

查看重点：

- Train 是否覆盖足够长时间。
- OOT 是否有足够样本和成熟标签。
- OOT 只用于验证，不能重新学习分箱边界。

### 5. 在 Train 上做 20 等频初分

主模型字段：

```text
score_mlt
```

初始分箱字段：

```text
score_mlt_bin20
```

处理逻辑：

1. 使用 `pd.qcut` 在 Train 上按分位数切 20 箱。
2. 相同分数过多时使用 `duplicates='drop'`，实际箱数可能少于 20。
3. 最左边界改为 `-inf`。
4. 最右边界改为 `inf`。
5. 区间规则为 `(left, right]`。
6. 初始箱编号为 `B01`、`B02`……。
7. 将 Train 学到的边界原样应用到全量和 OOT。

主要结果：

```text
score_mlt_bin_edges
score_mlt_bin_edges_df
train_binned_20
oot_binned_20
df_binned_20
bin_stats_20
```

边界表主要字段：

| 字段 | 含义 |
| --- | --- |
| `bin_order` | 初始箱顺序 |
| `score_mlt_bin20` | 初始箱名称，如 `B01` |
| `score_left` | 左边界，不包含 |
| `score_right` | 右边界，包含 |
| `interval_rule` | 固定为 `(left, right]` |

查看重点：

- `B01` 应是最低分、最低风险箱。
- `B20` 应是最高分、最高风险箱。
- 各箱样本量原则上接近。
- OOT 不允许重新执行 `qcut`。

注意：后续固定合箱配置默认存在 `B01` 至 `B20`。如果同分过多导致实际箱数明显少于 20，需要同步修改合箱映射，不能直接沿用当前范围。

### 6. 计算 20 箱指标并进行诊断

诊断参数：

```python
DIAG_CONFIG = {
    'min_bin_n': 1000,
    'min_cnt_mature': 1000,
    'min_cnt_bad': 30,
    'amt_cnt_gap_threshold': 0.03,
}
```

主要结果：

```text
bin_diagnosis_20
diagnosis_summary
merge_priority_bins
```

诊断内容：

- 箱内样本量是否低于 1000。
- 1M30+ 或 3M30+ 成熟样本是否低于 1000。
- 1M30+ 或 3M30+ bad 数是否低于 30。
- 风险率是否比前一箱下降，即发生倒挂。
- 相邻箱 Wilson 置信区间是否重叠。
- 金额逾期率是否缺失。
- 金额逾期率与笔数逾期率绝对差是否超过 3 个百分点。

`merge_priority_score` 权重：

```text
样本量不足：3 分
1M30+ 成熟不足：2 分
3M30+ 成熟不足：2 分
1M30+ bad 不足：2 分
3M30+ bad 不足：2 分
1M30+ 倒挂：3 分
3M30+ 倒挂：4 分
相邻置信区间重叠：1 分
金额指标缺失：1 分
金额/笔数差异过大：1 分
```

分数越高，越需要优先检查或合并。

### 7. 将 20 箱合并为 8 个最终风险等级

当前代码使用固定合箱方案：

| 最终等级 | 初始箱 |
| --- | --- |
| `G01` | `B01-B02` |
| `G02` | `B03-B05` |
| `G03` | `B06-B08` |
| `G04` | `B09-B11` |
| `G05` | `B12-B14` |
| `G06` | `B15-B16` |
| `G07` | `B17-B18` |
| `G08` | `B19-B20` |

当前设计意图：

- 合并主要倒挂和差异不明显区域。
- 将中间风险段适当压缩。
- 高风险尾部保留两档，便于区分拒绝边界。

主要结果：

```text
score_mlt_final_merge_map
score_mlt_final_edges_df
train_final_binned
oot_final_binned
df_final_binned
bin_stats_final
final_monotonicity_check
```

`score_mlt_final_merge_map` 用于说明每个初始箱映射到哪个最终箱。

`score_mlt_final_edges_df` 用于生成最终等级的真实分数边界。

`final_monotonicity_check` 检查以下指标是否随 `G01 → G08` 非递减：

```text
1m30p_cnt_bad_rate
3m30p_cnt_bad_rate
1m30p_amt_bad_rate
3m30p_amt_bad_rate
```

查看重点：

- 主风险指标应尽量无倒挂。
- 每个最终箱样本量和成熟样本量应足够。
- 高风险箱 Lift 应明显高于低风险箱。
- 累计风险应随阈值放宽逐步上升。

### 8. OOT 和跨月验证

代码将相同的 8 档边界应用到 OOT 和每个月份，不重新学习边界。

主要结果：

```text
oot_bin_stats_final
oot_monotonicity_check
train_oot_bin_compare
psi_final
perf_by_group
monthly_bin_stats_final
monthly_stability_summary
monthly_perf
oot_3m_amount_maturity_check
validation_decision
```

验证内容：

#### 8.1 Train vs OOT 逐箱对比

比较每个最终箱的：

- 样本量和样本占比。
- 1M30+ 成熟样本和笔数逾期率。
- 3M30+ 成熟样本和笔数逾期率。
- 1M30+ 和 3M30+ 金额逾期率。

#### 8.2 PSI

PSI 比较 Train 和 OOT 在最终风险等级上的分布差异。

主要字段：

```text
expected_cnt / expected_pct：Train 数量和占比
actual_cnt / actual_pct：OOT 数量和占比
psi_component：单箱 PSI 贡献
psi_total：全部箱 PSI 合计
```

常用经验判断：

```text
PSI < 0.10：分布较稳定
0.10 <= PSI < 0.25：需要关注
PSI >= 0.25：分布变化较明显
```

上述区间是常用经验值，当前代码只计算 PSI，不自动按该区间给出结论。

#### 8.3 AUC 和 KS

代码分别对 `duedate_1m_30` 和 `duedate_3m_30` 计算：

```text
n
bad_cnt
good_cnt
bad_rate
auc
ks
```

AUC 和 KS 会按 Train、OOT 和月份输出，用于判断模型排序能力是否跨期衰减。

#### 8.4 月度单调性

`monthly_stability_summary` 记录每个月：

- 箱数和总样本量。
- 1M30+、3M30+ 成熟样本量和整体风险率。
- 单箱最小样本量。
- 单箱最小成熟样本量。
- 各风险指标的倒挂次数和倒挂箱位置。

#### 8.5 OOT 3M 金额成熟度检查

`oot_3m_amount_maturity_check` 对比：

```text
duedate_3m_30_mature
dpd_days_ever_mob3_notna
principal_notna
estimate_principal_remaining_mob3_notna
```

用途是确认 OOT 的 3M 金额指标是否已经具备可解释性。

### 9. 构造阈值曲线

代码同时生成两类阈值曲线。

#### 9.1 最终箱边界曲线

```text
threshold_curve_final_bins
```

候选阈值取 8 个最终风险等级的右边界。最后一个箱的右边界是 `inf`，代码使用 Train 中最大实际分数替代，以保留“全量通过”点。

#### 9.2 细粒度分位点曲线

```text
threshold_curve_quantile
```

候选阈值取 Train 分数的 1% 至 99% 分位点，并补充最大分数。

该曲线比最终箱边界更细，可用于观察：

- 是否存在更精确的通过率和风险平衡点。
- 最终箱边界是否损失较多规模。
- 阈值是否应继续对齐到可上线边界。

#### 9.3 累计与边际指标

对每个阈值，代码计算两类人群：

```text
cum：分数不高于当前阈值的累计通过人群
marginal：相对上一个阈值新增进入的人群
```

主要指标：

```text
threshold
prev_threshold
cum_n
cum_pass_rate
cum_principal_pct
cum_1m30p_cnt_bad_rate
cum_3m30p_cnt_bad_rate
cum_1m30p_amt_bad_rate
cum_3m30p_amt_bad_rate
marginal_n
marginal_sample_pct
marginal_1m30p_cnt_bad_rate
marginal_3m30p_cnt_bad_rate
```

解读方式：

- `cum_pass_rate`：阈值放宽到当前位置，累计通过多少样本。
- `cum_*_bad_rate`：累计接纳人群的整体风险。
- `marginal_*_bad_rate`：本次放宽阈值新增人群的风险。
- 如果累计风险尚可，但边际风险快速上升，说明阈值已接近风险拐点。

### 10. 生成保守、平衡和增长方案

代码在最终箱边界曲线上，选择满足风险约束且累计通过率最高的阈值。

#### 10.1 当前约束配置

| 方案 | 阶段 | 累计 1M30+ 上限 | 累计 3M30+ 上限 | 边际 3M30+ 上限 |
| --- | --- | ---: | ---: | ---: |
| 保守 | 自动通过 | 0.70% | 4.50% | 7.00% |
| 保守 | 总接纳 | 1.10% | 6.30% | 12.00% |
| 平衡 | 自动通过 | 0.90% | 5.50% | 9.00% |
| 平衡 | 总接纳 | 1.30% | 7.50% | 17.00% |
| 增长 | 自动通过 | 1.10% | 6.30% | 12.00% |
| 增长 | 总接纳 | 1.55% | 8.50% | 22.00% |

#### 10.2 阈值选择规则

对每套方案分别寻找：

1. **自动通过阈值**：满足自动通过约束的最大阈值。
2. **总接纳阈值**：满足接纳约束的最大阈值。
3. 如果总接纳阈值低于自动通过阈值，则将两者对齐。

最终三段：

```text
自动通过：score_mlt <= auto_pass_threshold
人工审核：auto_pass_threshold < score_mlt <= reject_threshold
拒绝：score_mlt > reject_threshold
```

主要结果：

```text
strategy_plan
strategy_segment_train
strategy_segment_oot
strategy_segment_report
```

`strategy_plan` 给出每套方案的阈值和整体比例。

`strategy_segment_report` 分别计算自动通过、人工审核、拒绝三段在 Train 和 OOT 的规模及风险。

### 11. 分箱优化分析

#### 11.1 相邻箱显著性检验

代码对 20 个初始箱的相邻箱执行：

- 两比例 z 检验。
- 卡方检验。
- 风险方向判断。

主要结果：

```text
adjacent_sig_tests
adjacent_sig_summary
```

当出现以下任一情况时，`merge_hint` 标记为“建议合并”：

- 后一箱风险低于前一箱，即倒挂。
- 两比例 z 检验在 5% 显著性水平下不显著。

#### 11.2 比较 6/7/8/9 档方案

当前候选范围：

```text
6档：B01-02 / B03-05 / B06-08 / B09-14 / B15-18 / B19-20
7档：B01-02 / B03-05 / B06-08 / B09-11 / B12-14 / B15-18 / B19-20
8档：当前正式方案
9档：在高风险端将 B19、B20 分开
```

主要结果：

```text
candidate_merge_compare
candidate_merge_details
```

综合评分：

```text
candidate_score
= 箱数
- Train 倒挂总数 × 10
- OOT 3M30+ 倒挂数 × 5
- 出现月度 3M30+ 倒挂的月份数 × 0.5
- PSI × 100
```

分数越高，代表在保留风险区分度的同时，单调性和跨期稳定性相对更好。

#### 11.3 边界取整敏感性

代码比较：

```text
精确分位点边界
4 位小数边界
3 位小数边界
```

主要结果：

```text
rounded_boundary_compare
rounded_boundary_details
rounded_boundary_recommendation
```

检查内容：

- 有多少 Train 样本因取整而换箱。
- 最大单箱样本量变化。
- 最大 3M30+ 风险率变化。
- Train 和 OOT 是否新增倒挂。

当前代码的固定说明文本推荐 4 位小数边界。

### 12. 使用 4 位小数边界重新计算

代码不会仅修改展示精度，而是重新使用 4 位小数边界完成一次全流程复算：

```text
重新分箱
→ 重新计算 Train/OOT 分箱指标
→ 重新检查单调性
→ 重新生成阈值曲线
→ 重新生成三套策略
→ 对比精确边界和 4 位小数边界
```

主要结果：

```text
score_mlt_final_edges_rounded4_df
train_final_binned_rounded4
oot_final_binned_rounded4
bin_stats_final_rounded4
oot_bin_stats_final_rounded4
rounded4_bin_compare
rounded4_threshold_curve_final_bins
strategy_plan_rounded4
strategy_segment_report_rounded4
strategy_plan_compare_rounded4
rounded4_recalc_decision
```

当前代码固定写入的上线规则文本为：

```text
自动通过：score_mlt <= 0.0975
人工审核：0.0975 < score_mlt <= 0.1894
拒绝：score_mlt > 0.1894
```

这些规则文字是代码中的固定字符串，不是每次运行后自动从 `strategy_plan_rounded4` 拼接生成。更换数据、时间范围、合箱方案或风险约束后，必须同步更新这些文字，避免报告结论与计算结果不一致。

### 13. 阈值敏感性扫描

当前扫描配置：

```python
manual_review_caps = [10%, 15%, 20%, 25%, 30%]
accepted_3m30p_caps = [6%, 7%, 8%, 9%]
accepted_1m30p_cap = 1.5%
accepted_marginal_3m30p_cap = 22%
auto_cap_ratio = 75%
```

含义：

- 横向改变接纳人群的 3M30+ 风险上限。
- 纵向改变人工审核区间最大占比。
- 自动通过人群使用总接纳风险上限的 75% 作为更严格约束。
- 对每一组约束，寻找接纳率最高、自动通过率最高、人工审核率最低的可行阈值组合。

主要结果：

```text
threshold_sensitivity_rounded4
threshold_sensitivity_quantile
threshold_sensitivity_matrix_rounded4
threshold_sensitivity_final_vs_quantile
threshold_sensitivity_decision
```

当前代码固定读取：

```text
人工审核上限 = 25%
接纳人群 3M30+ 上限 = 8%
```

并将该组合写入推荐结论。

注意：代码通过 `.iloc[0]` 直接获取该组合。如果该组合没有可行方案，脚本可能报错。正式使用时建议增加“结果为空”的保护逻辑。

### 14. 生成 Excel 报告

最后使用 `openpyxl` 创建：

```text
out/策略报告.xlsx
```

Excel 共 5 个 Sheet。所有浮点数最多保留 6 位小数，但代码没有设置真正的 Excel 百分比格式，因此：

```text
0.08 表示 8%
0.125 表示 12.5%
```

查看时不要把小数值误认为已经乘以 100 的百分数。

---

## 五、Excel 结果表总览与查看方法

建议按以下顺序阅读：

```text
1.策略推荐结论
→ 2.最终分箱与验证
→ 3.分箱优化过程
→ 4.边界与阈值敏感性
→ 5.三套策略方案对比
```

### Sheet 1：`1.策略推荐结论`

#### 表 1：最终推荐结论

底层对象：

```text
strategy_recommendation
```

主要字段：

| 字段 | 含义 |
| --- | --- |
| `recommended_strategy` | 当前推荐方案名称 |
| `reason` | 推荐原因 |
| `auto_pass_rule` | 自动通过规则 |
| `manual_review_rule` | 人工审核规则 |
| `reject_rule` | 拒绝规则 |
| `train_auto_pass_rate` | Train 自动通过占比 |
| `train_manual_review_rate` | Train 人工审核占比 |
| `train_reject_rate` | Train 拒绝占比 |
| `train_accepted_3m30p_cnt_bad_rate` | Train 总接纳人群 3M30+ 笔数风险 |
| `oot_auto_pass_3m30p_cnt_bad_rate` | OOT 自动通过段 3M30+ 风险 |
| `oot_manual_review_3m30p_cnt_bad_rate` | OOT 人工审核段 3M30+ 风险 |
| `note` | 使用限制和后续复核事项 |

查看方法：

1. 先确认推荐方案名称和三段规则。
2. 检查自动通过、人工审核、拒绝占比是否符合业务目标。
3. 检查 OOT 自动通过段和人工审核段风险是否保持明显分层。
4. 再到 Sheet 2 和 Sheet 4 验证该结论是否稳定。

注意：规则文本是当前代码中的固定字符串，数据变化后需要人工核对。

#### 表 2：4 位小数边界复算结论

底层对象：

```text
rounded4_recalc_decision
```

主要字段：

| 字段 | 含义 |
| --- | --- |
| `rounded4_train_monotonic` | 4 位边界下 Train 四类风险是否全部单调 |
| `rounded4_oot_3m30p_cnt_monotonic` | 4 位边界下 OOT 3M30+ 笔数风险是否单调 |
| `max_abs_train_bin_n_delta` | 取整前后最大单箱样本量差异 |
| `max_abs_train_3m30p_rate_delta` | 取整前后最大单箱 3M30+ 风险差异 |
| `recommended_auto_pass_rule` | 4 位边界自动通过规则 |
| `recommended_manual_review_rule` | 4 位边界人工审核规则 |
| `recommended_reject_rule` | 4 位边界拒绝规则 |
| `recommendation` | 是否适合上线的说明 |

查看方法：

- 重点看两个单调性字段是否为 `True`。
- 关注样本迁移后风险率变化是否很小。
- 核对 4 位小数规则是否与后续策略表一致。

#### 表 3：推荐方案三段指标—Train（4 位小数）

底层对象：

```text
strategy_segment_report_rounded4
```

筛选条件：

```text
strategy_name = 平衡方案
sample_group 包含 train
```

#### 表 4：推荐方案三段指标—OOT（4 位小数）

底层对象同上，筛选 OOT。

两张表共同字段：

| 字段 | 含义 |
| --- | --- |
| `decision` | 自动通过、人工审核或拒绝 |
| `n` | 该段样本量 |
| `sample_pct` | 该段样本占全部样本比例 |
| `1m30p_cnt_mature` | 该段 1M30+ 成熟样本量 |
| `1m30p_cnt_bad_rate` | 该段 1M30+ 笔数逾期率 |
| `3m30p_cnt_mature` | 该段 3M30+ 成熟样本量 |
| `3m30p_cnt_bad_rate` | 该段 3M30+ 笔数逾期率 |
| `1m30p_amt_bad_rate` | 该段 1M30+ 金额逾期率 |
| `3m30p_amt_bad_rate` | 该段 3M30+ 金额逾期率 |

查看方法：

- 风险应呈现“自动通过 < 人工审核 < 拒绝”的整体梯度。
- Train 和 OOT 的三段占比及风险不应出现严重反转。
- 如果 OOT 3M 金额指标为空，应先检查表现成熟度，不能直接解释为零风险。

### Sheet 2：`2.最终分箱与验证`

#### 表 1：8 档最终风险等级（精确边界）

底层对象：

```text
bin_stats_final
```

主要字段：

| 字段 | 含义 |
| --- | --- |
| `bin_order` | 风险等级顺序，1 为最低风险 |
| `merged_from` | 由哪些初始箱合并而来 |
| `score_left` | 分数左边界，不包含 |
| `score_right` | 分数右边界，包含 |
| `n` | 箱内样本量 |
| `sample_pct` | 箱内样本占比 |
| `score_min`、`score_max` | 箱内实际最低分和最高分 |
| `1m30p_cnt_mature`、`1m30p_cnt_bad` | 1M30+ 成熟量和坏样本量 |
| `1m30p_cnt_bad_rate`、`1m30p_cnt_lift` | 1M30+ 风险率和 Lift |
| `3m30p_cnt_mature`、`3m30p_cnt_bad` | 3M30+ 成熟量和坏样本量 |
| `3m30p_cnt_bad_rate`、`3m30p_cnt_lift` | 3M30+ 风险率和 Lift |
| `1m30p_amt_bad_rate`、`3m30p_amt_bad_rate` | 金额风险率 |
| `cum_pass_rate` | 累计通过率 |
| `cum_1m30p_cnt_bad_rate` | 累计 1M30+ 笔数风险 |
| `cum_3m30p_cnt_bad_rate` | 累计 3M30+ 笔数风险 |

查看方法：

1. 从 `bin_order=1` 向下看风险是否逐步升高。
2. 看每箱样本量、成熟量和 bad 数是否足够。
3. 看低风险箱和高风险箱的 Lift 是否拉开。
4. 用 `cum_pass_rate` 与累计风险判断阈值放宽效果。

#### 表 2：阈值曲线（各等级右边界作为阈值）

底层对象：

```text
threshold_curve_final_bins
```

主要字段：

| 字段 | 含义 |
| --- | --- |
| `threshold_order` | 阈值顺序 |
| `threshold` | 当前候选分数阈值 |
| `score_mlt_final_bin` | 阈值对应的最终风险等级 |
| `merged_from` | 对应初始箱范围 |
| `cum_n` | 累计通过样本量 |
| `cum_pass_rate` | 累计通过率 |
| `cum_1m30p_cnt_mature` | 累计 1M30+ 成熟量 |
| `cum_1m30p_cnt_bad_rate` | 累计 1M30+ 风险 |
| `cum_3m30p_cnt_mature` | 累计 3M30+ 成熟量 |
| `cum_3m30p_cnt_bad_rate` | 累计 3M30+ 风险 |
| `cum_1m30p_amt_bad_rate`、`cum_3m30p_amt_bad_rate` | 累计金额风险 |
| `marginal_n` | 相比上一个阈值新增样本量 |
| `marginal_sample_pct` | 新增样本占比 |
| `marginal_1m30p_cnt_bad_rate` | 新增人群 1M30+ 风险 |
| `marginal_3m30p_cnt_bad_rate` | 新增人群 3M30+ 风险 |

查看方法：

- 先看累计通过率提升了多少。
- 再看累计风险是否仍在上限内。
- 最后重点看新增人群的边际风险是否突然恶化。

#### 表 3：Train vs OOT 逐箱对比

底层对象：

```text
train_oot_bin_compare
```

字段以 `_train` 和 `_oot` 结尾，表示相同指标在两个样本组的结果。

主要比较：

```text
n
sample_pct
1m30p_cnt_mature
1m30p_cnt_bad_rate
3m30p_cnt_mature
3m30p_cnt_bad_rate
1m30p_amt_bad_rate
3m30p_amt_bad_rate
```

查看方法：

- 同一风险等级在 OOT 中的风险应与 Train 方向一致。
- 分布占比变化较大的箱需要结合 PSI 排查。
- OOT 单箱成熟量很小时，不要过度解释短期波动。

#### 表 4：PSI 分布稳定性

底层对象：

```text
psi_final
```

字段：

| 字段 | 含义 |
| --- | --- |
| `expected_cnt`、`expected_pct` | Train 数量和占比 |
| `actual_cnt`、`actual_pct` | OOT 数量和占比 |
| `psi_component` | 该风险等级对总 PSI 的贡献 |

总 PSI 写在表标题中。

查看方法：

- 先看总 PSI。
- 再看哪个箱的 `psi_component` 最大。
- 若高风险尾部贡献较大，需要排查渠道、产品或客群结构是否变化。

#### 表 5：AUC/KS 汇总

底层对象：

```text
perf_by_group
```

字段：

| 字段 | 含义 |
| --- | --- |
| `sample_group` | Train 或 OOT |
| `label` | `duedate_1m_30` 或 `duedate_3m_30` |
| `n` | 可用于评估的成熟样本量 |
| `bad_cnt` | 坏样本量 |
| `bad_rate` | 整体坏样本率 |
| `auc` | 排序能力 |
| `ks` | 好坏样本最大累计分布差异 |

查看方法：

- 比较 Train 与 OOT 的 AUC、KS 是否明显下降。
- 结合 `n` 和 `bad_cnt` 判断结果是否稳定。

#### 表 6：逐月 AUC/KS

底层对象：

```text
monthly_perf
```

字段与 AUC/KS 汇总表相同，但索引为 `application_month`。

查看方法：

- 观察是否某几个月明显失效。
- 若只在特定月份下降，优先排查当月数据、政策、渠道或客群变化。

#### 表 7：月度分箱稳定性

底层对象：

```text
monthly_stability_summary
```

主要字段：

| 字段 | 含义 |
| --- | --- |
| `bin_cnt` | 当月实际出现的最终箱数量 |
| `n` | 当月样本量 |
| `m1_mature`、`m3_mature` | 当月成熟样本量 |
| `m1_bad_rate`、`m3_bad_rate` | 当月整体风险率 |
| `min_bin_n` | 当月最小单箱样本量 |
| `min_m1_mature_per_bin` | 当月最小单箱 1M 成熟量 |
| `min_m3_mature_per_bin` | 当月最小单箱 3M 成熟量 |
| `1m30p_cnt_bad_rate_violation_cnt` | 当月 1M30+ 倒挂次数 |
| `3m30p_cnt_bad_rate_violation_cnt` | 当月 3M30+ 倒挂次数 |

查看方法：

- 重点查看主标签 3M30+ 是否长期稳定。
- 单月轻微倒挂需要结合成熟样本量判断。
- 多个月连续倒挂说明边界或模型可能失效。

### Sheet 3：`3.分箱优化过程`

#### 表 1：20 等频箱诊断摘要

底层对象：

```text
diagnosis_summary
```

主要字段表示：

- 初始箱数量。
- 1M30+、3M30+ 倒挂箱数量。
- 成熟样本不足箱数量。
- bad 数不足箱数量。
- 相邻置信区间重叠数量。
- 金额/笔数风险差异过大箱数量。

查看方法：

- 用于快速判断 20 箱是否过细。
- 问题数量较多时，应优先合箱，而不是直接上线 20 档。

#### 表 2：20 等频箱初步诊断

底层对象：

```text
bin_diagnosis_20
```

主要字段：

| 字段 | 含义 |
| --- | --- |
| `score_mlt_bin20` | 初始箱名称 |
| `n` | 箱内样本量 |
| `1m30p_cnt_mature`、`1m30p_cnt_bad` | 1M30+ 成熟量和坏样本量 |
| `1m30p_cnt_bad_rate` | 1M30+ 风险率 |
| `1m30p_cnt_rate_diff_prev` | 与前一箱的风险率差 |
| `3m30p_cnt_mature`、`3m30p_cnt_bad` | 3M30+ 成熟量和坏样本量 |
| `3m30p_cnt_bad_rate` | 3M30+ 风险率 |
| `3m30p_cnt_rate_diff_prev` | 与前一箱的风险率差 |
| `merge_priority_score` | 合并优先级分数 |
| `diagnosis_flags` | 问题说明 |

查看方法：

- `*_rate_diff_prev < 0` 表示倒挂。
- `merge_priority_score` 越高，越应优先合并或排查。
- 不要只看单个标签，应同时看 1M30+、3M30+ 和金额风险。

#### 表 3：6/7/8/9 档候选分箱方案对比

底层对象：

```text
candidate_merge_compare
```

主要字段：

| 字段 | 含义 |
| --- | --- |
| `bin_cnt` | 最终档位数量 |
| `min_train_n` | Train 最小单箱样本量 |
| `min_train_1m_mature`、`min_train_3m_mature` | 最小单箱成熟量 |
| `min_train_1m_bad`、`min_train_3m_bad` | 最小单箱坏样本量 |
| `train_violation_cnt` | Train 四类风险倒挂总数 |
| `oot_1m_cnt_violation_cnt` | OOT 1M30+ 倒挂数 |
| `oot_3m_cnt_violation_cnt` | OOT 3M30+ 倒挂数 |
| `psi_total` | Train/OOT 分布 PSI |
| `months_with_1m_cnt_violation` | 出现 1M30+ 倒挂的月份数 |
| `months_with_3m_cnt_violation` | 出现 3M30+ 倒挂的月份数 |
| `candidate_score` | 代码定义的综合评分 |

查看方法：

- 不应机械选择箱数最多的方案。
- 优先选择 Train、OOT 和月度均较稳定，同时仍保留风险区分度的方案。

#### 表 4：分箱优化结论

底层对象：

```text
binning_optimization_decision
```

包含主方案、备选方案、显著性检验结论、边界精度建议和推荐边界。

注意：其中推荐边界和样本迁移比例是固定文字，数据变更后需要重新核对。

#### 表 5：相邻箱显著性检验

底层对象：

```text
adjacent_sig_tests
```

Excel 只保留 `merge_hint = 建议合并` 的记录。

主要字段：

| 字段 | 含义 |
| --- | --- |
| `metric` | `1m30p` 或 `3m30p` |
| `left_bin_order`、`right_bin_order` | 相邻两个箱 |
| `left_rate`、`right_rate` | 两箱风险率 |
| `rate_diff` | 右箱减左箱风险率 |
| `direction_ok` | 风险方向是否正确 |
| `z_stat`、`z_p_value` | 两比例 z 检验结果 |
| `chi2_p_value` | 卡方检验 p 值 |
| `significant_5pct` | 5% 水平下是否显著 |
| `merge_hint` | 是否建议合并 |

查看方法：

- 倒挂时优先合并。
- 差异不显著时，合并通常能提升稳定性。
- 显著性只是辅助依据，还要结合主标签、样本量和业务解释。

### Sheet 4：`4.边界与阈值敏感性`

#### 表 1：3/4 位小数边界取整对比

底层对象：

```text
rounded_boundary_compare
```

主要字段：

| 字段 | 含义 |
| --- | --- |
| `round_decimals` | 保留小数位数 |
| `shifted_n` | 取整后换箱样本量 |
| `shifted_pct` | 换箱样本比例 |
| `max_abs_bin_n_delta` | 最大单箱样本量变化 |
| `max_abs_3m30p_rate_delta` | 最大 3M30+ 风险率变化 |
| `train_violation_cnt` | Train 新方案倒挂总数 |
| `oot_1m_cnt_violation_cnt` | OOT 1M30+ 倒挂数 |
| `oot_3m_cnt_violation_cnt` | OOT 3M30+ 倒挂数 |

查看方法：

- 选择迁移比例小、风险变化小且不新增主标签倒挂的精度。

#### 表 2：精确边界 vs 4 位小数边界逐箱对比

底层对象：

```text
rounded4_bin_compare
```

主要字段以 `_exact`、`_rounded4` 和 `_delta` 区分：

```text
n
sample_pct
1m30p_cnt_bad_rate
3m30p_cnt_bad_rate
1m30p_amt_bad_rate
3m30p_amt_bad_rate
```

查看方法：

- 关注单箱样本迁移是否集中在某个风险边界。
- 重点检查 3M30+ 风险率差异和高风险尾部变化。

#### 表 3：精确边界 vs 4 位小数策略方案对比

底层对象：

```text
strategy_plan_compare_rounded4
```

主要比较：

```text
auto_pass_threshold
reject_threshold
auto_pass_rate
manual_review_rate
reject_rate
accepted_1m30p_cnt_bad_rate
accepted_3m30p_cnt_bad_rate
```

查看方法：

- 如果阈值、规模和风险变化很小，可以优先使用 4 位小数边界上线。

#### 表 4：阈值敏感性矩阵

底层对象：

```text
threshold_sensitivity_matrix_rounded4
```

行：人工审核率上限。

列：接纳人群 3M30+ 风险上限。

单元格格式：

```text
接纳率 / 自动通过率 / 人工审核率 / 实际接纳 3M30+ 风险
```

查看方法：

- 先确定团队最大人工审核产能。
- 再确定业务可接受的 3M30+ 风险上限。
- 两者交叉位置即对应候选方案。

#### 表 5：阈值敏感性扫描明细

底层对象：

```text
threshold_sensitivity_rounded4
```

主要字段：

| 字段 | 含义 |
| --- | --- |
| `max_manual_review_rate` | 人工审核率上限 |
| `max_accepted_3m30p_cnt_bad_rate` | 接纳人群 3M30+ 上限 |
| `status` | 是否存在可行方案 |
| `auto_pass_threshold` | 自动通过阈值 |
| `reject_threshold` | 拒绝阈值 |
| `auto_pass_rate` | 自动通过占比 |
| `accepted_rate` | 自动通过加人工审核的总接纳率 |
| `manual_review_rate` | 人工审核占比 |
| `reject_rate` | 拒绝占比 |
| `auto_3m30p_cnt_bad_rate` | 自动通过人群 3M30+ 风险 |
| `accepted_3m30p_cnt_bad_rate` | 总接纳人群 3M30+ 风险 |
| `last_accepted_marginal_3m30p_cnt_bad_rate` | 最后接纳增量人群的 3M30+ 风险 |

查看方法：

- `status=无可行方案` 表示当前风险与产能约束不能同时满足。
- 不仅看总接纳风险，还要看最后一段新增人群风险。

#### 表 6：阈值敏感性推荐结论

底层对象：

```text
threshold_sensitivity_decision
```

包含推荐产能假设、风险上限、三段规则、自动通过率、人工审核率、拒绝率和接纳风险。

当前固定推荐场景为：

```text
人工审核率上限 25%
接纳人群 3M30+ 上限 8%
```

#### 表 7：分位点曲线 vs 最终箱边界对比

底层对象：

```text
threshold_sensitivity_final_vs_quantile
```

主要字段：

```text
accepted_rate_rounded4
accepted_rate_quantile
accepted_rate_gain_quantile
auto_pass_rate_rounded4
auto_pass_rate_quantile
auto_rate_gain_quantile
```

查看方法：

- 衡量细粒度分位点阈值比最终箱边界多获得多少接纳率或自动通过率。
- 如果提升很小，优先选择更稳定、易解释的最终箱边界。
- 如果提升明显，再评估是否值得使用更细阈值上线。

### Sheet 5：`5.三套策略方案对比`

#### 表 1：三套策略方案总览

底层对象：

```text
strategy_plan
```

主要字段：

| 字段 | 含义 |
| --- | --- |
| `objective` | 方案目标 |
| `auto_pass_bin` | 自动通过上限对应风险等级 |
| `auto_pass_threshold` | 自动通过阈值 |
| `manual_review_upper_bin` | 人工审核上限对应风险等级 |
| `reject_threshold` | 拒绝阈值 |
| `auto_pass_rate` | 自动通过占比 |
| `manual_review_rate` | 人工审核占比 |
| `reject_rate` | 拒绝占比 |
| `accepted_1m30p_cnt_bad_rate` | 总接纳人群 1M30+ 风险 |
| `accepted_3m30p_cnt_bad_rate` | 总接纳人群 3M30+ 风险 |
| `accepted_1m30p_amt_bad_rate` | 总接纳人群 1M30+ 金额风险 |
| `accepted_3m30p_amt_bad_rate` | 总接纳人群 3M30+ 金额风险 |
| `last_accepted_marginal_1m30p_cnt_bad_rate` | 最后一段新增人群 1M30+ 风险 |
| `last_accepted_marginal_3m30p_cnt_bad_rate` | 最后一段新增人群 3M30+ 风险 |

查看方法：

- 比较三套方案在通过率、审核量、拒绝量和风险上的取舍。
- 不应只选择通过率最高的方案。
- 正式选择前还应加入收益、损失和审核成本。

#### 表 2：各方案三段指标—Train

#### 表 3：各方案三段指标—OOT

底层对象：

```text
strategy_segment_report
```

索引为：

```text
strategy_name + decision
```

字段：

```text
n
sample_pct
1m30p_cnt_mature
1m30p_cnt_bad_rate
3m30p_cnt_mature
3m30p_cnt_bad_rate
1m30p_amt_bad_rate
3m30p_amt_bad_rate
```

查看方法：

- 对同一方案比较自动通过、人工审核和拒绝三段风险梯度。
- 对同一决策段比较 Train 和 OOT 是否稳定。
- 对不同方案比较人工审核量是否可执行。

---

## 六、当前代码中已定义但未实际输出的内容

### 1. 审批漏斗函数

代码定义了：

```text
calc_funnel_stats(data, group_col=None)
```

可计算：

```text
apply_cnt
completed_application_cnt
approved_application_cnt
auto_approved_application_cnt
manual_approved_application_cnt
deal_sample_cnt
completion_rate
approval_rate
auto_approval_rate
manual_approval_rate
auto_approval_share
manual_approval_share
deal_rate
```

但当前主流程没有调用该函数，也没有把结果写入 `策略报告.xlsx`。

需要输出时，可增加：

```python
funnel_total = calc_funnel_stats(df_final_binned)
funnel_by_bin = calc_funnel_stats(df_final_binned, group_col=FINAL_BIN_COL)
```

然后将结果写入新的 Excel 区块。

### 2. `score_apply` 和交易特征

当前代码会拼接并保留 `score_apply` 和交易子模型字段，但：

- 不对 `score_apply` 单独分箱。
- 不比较 `score_mlt`、`score_apply` 和交易子模型效果。
- 不使用交易特征决定合箱或阈值。

因此当前报告本质上是 `score_mlt` 的分箱与策略报告。

### 3. 图表

虽然代码导入了 `matplotlib`，但没有调用 `plot` 或 `savefig`，因此不会生成：

- 分箱风险趋势图。
- 阈值通过率—风险曲线。
- PSI 图。
- AUC/KS 图。

当前所有结果均以 Excel 表格呈现。

---

## 七、复现操作步骤

### 第 1 步：准备数据

将 5 张输入表放到 `res` 目录，并确认文件名与代码完全一致。

### 第 2 步：检查字段

至少确认：

```text
application_id、user_id 唯一性
application_month 格式为 YYYY-MM
score_mlt 非空比例
1M30+ 和 3M30+ 成熟比例
principal 和剩余本金字段覆盖率
```

### 第 3 步：确认配置

根据本次分析目的修改：

```python
TRAIN_END_MONTH
OOT_START_MONTH
HIGH_SCORE_HIGH_RISK
SCORE_COL
DIAG_CONFIG
FINAL_BIN_RANGES
STRATEGY_CONFIGS
THRESHOLD_SENSITIVITY_CONFIG
```

### 第 4 步：运行脚本

```bash
python binning.py
```

### 第 5 步：检查运行日志

重点确认：

- 5 张表是否成功加载。
- 去重前后行数是否合理。
- 宽表行数和主键重复数。
- Train/OOT 样本量。
- 实际初始箱数量。
- 是否出现空候选方案或索引越界报错。

### 第 6 步：按顺序查看 Excel

1. Sheet 1：看最终推荐规则。
2. Sheet 2：确认最终箱单调性、OOT、PSI、AUC、KS。
3. Sheet 3：理解为什么从 20 箱合并为 8 档。
4. Sheet 4：确认 4 位边界和审核产能约束。
5. Sheet 5：比较保守、平衡、增长方案。

### 第 7 步：上线前核对

至少确认：

```text
模型版本
模型分方向
Train/OOT 时间范围
风险标签定义
成熟样本定义
最终箱边界
区间规则 (left, right]
自动通过阈值
拒绝阈值
人工审核产能
生效时间
回滚版本
```

---

## 八、当前实现需要特别注意的问题

### 1. 多处结论文字为硬编码

Sheet 1 的“最终推荐结论”来自精确边界方案，而后面的 Train/OOT 三段指标来自 4 位小数复算结果；两部分计算基础并不完全相同。

以下对象包含固定规则或固定说明：

```text
strategy_recommendation
binning_optimization_decision
rounded4_recalc_decision
threshold_sensitivity_decision
```

其中部分指标动态取自计算结果，但阈值规则、推荐边界和说明文字并非全部动态生成。

更换数据或参数后，应逐项核对这些结论，最好改为从结果表自动拼接。

### 2. 分数方向不能只修改一个开关

当前脚本整体按高分高风险设计：

- 初始箱按分数从低到高编号。
- 累计指标从低分向高分累计。
- 固定合箱范围和策略规则也按该方向解释。

如果模型是低分高风险，仅将 `HIGH_SCORE_HIGH_RISK=False` 不能保证全部逻辑正确，还需要同步调整分箱顺序、累计方向、合箱解释和阈值规则。

### 3. 初始箱数可能少于 20

`qcut(..., duplicates='drop')` 允许删除重复边界，但后续固定合箱范围仍按 20 箱编写。

如果实际箱数少于 20，应重新生成 `FINAL_BIN_RANGES` 和候选方案。

### 4. 重复记录仅做检查，不会强制终止

模型分表会保留第一条重复记录，宽表重复也只记录在质量检查中。

正式生产前应明确：

- 风险标的是申请、用户还是借据。
- 同一申请多条模型分应按什么版本和时间保留。
- 同一申请多条表现记录是否需要聚合。

### 5. 比例在 Excel 中以小数显示

例如：

```text
0.075 = 7.5%
```

当前 Excel 没有设置百分比数字格式，只是将浮点数保留到 6 位小数。

### 6. 推荐敏感性场景可能为空

代码固定获取“审核上限 25%、接纳 3M30+ 上限 8%”的结果。如果该组合无解，可能触发 `.iloc[0]` 报错。

建议增加空结果判断，再决定是否输出推荐结论。

---

## 九、下一步优化方案

基础流程完成后，可以继续从“风险分箱”升级到“风险、规模、收益一体化优化”。这部分不是必须先完成才能上线，但适合用于策略精调和业务收益评估。

### 1. 引入收益、EL 和 UE

阈值不应只回答“风险是否可接受”，还应回答“新增客户是否值得接纳”。可以补充：

| 指标 | 含义 | 作用 |
| --- | --- | --- |
| `EAD` | 违约风险暴露，可用放款本金、余额或额度近似 | 计算预期损失 |
| `PD` | 违约概率，可由模型分、校准模型或分箱风险率估算 | 衡量客户风险 |
| `LGD` | 违约损失率 | 将违约概率转为损失 |
| `EL` | `EAD × PD × LGD` | 预期损失 |
| `revenue` | 利息、手续费、服务费等收入 | 衡量收入贡献 |
| `funding_cost` | 资金成本 | 收益扣减 |
| `operation_cost` | 审核、催收和运营成本 | 收益扣减 |
| `UE` | 单客风险后价值 | 判断客户是否有正向经济价值 |

阈值曲线可进一步增加：

```text
累计 EL
累计 revenue
累计 UE
边际 EL
边际 revenue
边际 UE
人工审核成本
```

解读：

- 累计 UE 判断整体方案是否创造价值。
- 边际 UE 判断继续放宽阈值是否值得。
- EL 将风险率转换为金额损失。
- 审核成本用于判断人工审核区间是否真正有价值。

### 2. 将人工审核产能变成硬约束

建议正式加入：

```text
manual_review_rate <= team_capacity
manual_review_cnt_per_day = daily_apply_cnt × manual_review_rate
```

如果审核量超出产能，可以：

- 收紧拒绝阈值，缩短人工审核区间。
- 将人工审核拆成强审、轻审、补充材料和降额等子策略。
- 对低收益、高风险人群直接拒绝。
- 对高收益、中高风险人群使用降额、提价或加强验证。

### 3. 细粒度阈值搜索与上线边界对齐

建议采用两步法：

1. 在分位点曲线上寻找满足风险、收益和产能约束的最优点。
2. 将最优点对齐到最终箱边界或固定小数边界。
3. 比较对齐前后的通过率、风险率、EL 和 UE 损失。
4. 对齐损失较小时，优先使用更稳定、易解释的边界。
5. 对齐损失较大时，可保留细粒度阈值，但需要加强上线监控。

### 4. 补充分群和压力测试

整体稳定不代表所有客群都稳定，建议按以下维度复核：

| 维度 | 观察重点 |
| --- | --- |
| 月份 | 是否存在季节性或 vintage 漂移 |
| 产品 | 不同产品风险排序是否一致 |
| 渠道 | 是否存在局部分数失效 |
| 额度段 | 高额度客户是否带来金额损失集中 |
| 新老客 | 风险排序和收益结构是否不同 |
| 地区或客群 | 是否存在局部风险偏移 |

如果某个分群明显失效，应考虑单独阈值、策略附加规则或重新建模，而不是只调整全局阈值。

### 5. 上线后监控

建议持续监控：

| 指标 | 用途 |
| --- | --- |
| 分数分布 PSI | 判断客群是否漂移 |
| 各风险等级样本占比 | 判断分数分布是否变化 |
| 各风险等级 1M30+ / 3M30+ | 判断风险排序是否稳定 |
| 自动通过 / 人工审核 / 拒绝占比 | 判断策略执行是否偏移 |
| 人工审核通过率 | 判断审核区间是否有效 |
| EL / UE | 判断风险后收益是否达标 |
| 边际风险和边际 UE | 判断阈值是否需要调整 |

出现分布漂移、风险倒挂、收益恶化或审核量异常时，应重新执行分箱验证、边界复算和阈值敏感性分析。

---

## 十、一句话总结

> **当前脚本先在 Train 上将 `score_mlt` 等频切成 20 箱，再结合样本量、成熟度、风险倒挂和跨期稳定性合并为 8 个风险等级；随后沿低风险到高风险方向计算累计与边际风险，并在风险上限和人工审核产能约束下划分自动通过、人工审核和拒绝阈值。**