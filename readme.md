# 模型分数分箱与策略阈值操作指南

> 本文档与当前 `binning.py` 的实际执行逻辑对应，按脚本运行顺序说明数据准备、模型分箱、分箱评估、阈值搜索、策略划分和结果表解读。
>
> 当前脚本的核心目标是：以 `score_mlt` 为主模型分，在 Development 样本上建立稳定风险等级，并划分自动通过、人工审核和拒绝阈值，最终生成 `out/binning_strategy_report_YYYYMMDD.xlsx`。

---

## 一、先理解整个流程

当前脚本按以下顺序执行（对应运行日志中的 1/9 ~ 9/9）：

```text
加载 3 张 CSV
→ 清理字段名并拼接分析宽表
→ 检查关键字段，按月份切分 Train / OOT
→ 在 Train 内切出最后 3 个月作为合箱 Validation，其余为 Development
→ 在 Development 上对 score_mlt 做 20 等频初分，边界复用到所有样本
→ 计算每箱规模、1M30+、3M30+、金额风险、Lift 和累计指标
→ 自动合箱：小箱清理 → 单调合并 → 档位压缩 → 生成 6~8 档候选方案并评分
→ 将最终合箱映射应用到 Development / Validation / Train / OOT
→ 验证单调性、PSI、AUC、KS 和月度稳定性
→ 在完整 Train 上生成最终箱边界阈值曲线
→ 按风险约束选择自动通过、人工审核、拒绝阈值
→ 输出 6 个 sheet 的 Excel 策略报告
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
│   └── aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv
└── out/
    └── binning_strategy_report_YYYYMMDD.xlsx   # 运行后生成
```

`out` 文件夹不需要提前创建，代码会自动创建。

### 2. 安装依赖

```bash
pip install numpy pandas openpyxl
```

当前脚本只依赖这三个包，不依赖 scipy / sklearn / statsmodels / matplotlib：相邻箱显著性检验用不依赖 scipy 的两比例 Z 检验实现，AUC / KS 直接按秩和与累计分布计算。

### 3. 运行方式

```bash
python binning.py
```

脚本运行完成后会输出：

```text
out/binning_strategy_report_YYYYMMDD.xlsx
```

文件名中的日期为运行当天。当前代码不会输出 CSV、PNG 或其他单独的中间文件。

### 4. 输入文件及作用

| 输入文件 | 连接字段 | 当前用途 |
| --- | --- | --- |
| `sample.csv` | `application_id`、`user_id` | 分析底表，决定最终保留哪些样本 |
| `application_info.csv` | `application_id`、`user_id` | 补充申请时间、表现标签、本金、审批状态等信息 |
| `aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv` | `application_id` | 提供主模型分，重命名为 `score_mlt` |

注意：当前脚本已不再读取申请模型分表（`score_apply`）和交易子模型表，只使用主模型分。

拼接逻辑要点：

- 以 `sample` 为底表，`application_info` 只补充 sample 中不存在的字段，避免出现 `_x` / `_y` 后缀。
- 模型分表按 `application_id` 去重（保留第一条）后左连接。
- `application_month` 缺失时，用 `application_time` 的月份补齐；若仍缺失则归入 `gap_or_unknown`。
- **分箱分析只保留存在模型分的样本**；模型分缺失的样本会在总览中单独展示数量和比例。

### 5. 当前必须存在的关键字段

脚本会检查以下字段，缺失时直接报错：

```text
application_id
user_id
application_time
application_month
score_mlt
duedate_1m_30
duedate_3m_30
principal
estimate_principal_remaining_mob1
estimate_principal_remaining_mob3
dpd_days_ever_mob1
dpd_days_ever_mob3
```

### 6. 当前核心配置

```python
DATA_DIR = Path("res")
TRAIN_END_MONTH = "2025-10"
OOT_START_MONTH = "2025-11"
VALIDATION_MONTH_COUNT = 3
MIN_DEVELOPMENT_MONTH_COUNT = 3
HIGH_SCORE_HIGH_RISK = True
SCORE_COL = "score_mlt"
INITIAL_BIN_COUNT = 20
MIN_FINAL_BIN_COUNT = 6
MAX_FINAL_BIN_COUNT = 8
TARGET_FINAL_BIN_COUNT = 7
```

含义：

- `application_month <= 2025-10`：进入 `train`。
- `application_month >= 2025-11`：进入 `oot`。
- 时间为空或不能归入上述范围：进入 `gap_or_unknown`。
- Train 内最后 3 个月作为合箱 Validation，其余为 Development；OOT 完全独立，不参与任何合箱调参。
- 初始等频分 20 箱，自动合箱到 6~8 档（目标 7 档）。
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
principal_amt = 箱内 principal 合计
sample_pct = 箱内样本数 / 全部箱样本数
score_min / score_max / score_mean = 箱内实际分数范围与均值
```

当前分箱统计使用 `application_id` 的行数计算 `n`。如果宽表中一笔申请存在多行，风险指标会按多行计算，因此正式运行前应确保主键口径正确。

### 6. Lift

```text
某箱 Lift = 某箱逾期率 / 该样本组整体逾期率
```

例如：

```text
3m30p_cnt_lift
= 该箱 3M30+ 笔数逾期率 / 该样本组整体 3M30+ 笔数逾期率
```

解读：

- `Lift < 1`：风险低于整体水平。
- `Lift = 1`：风险接近整体水平。
- `Lift > 1`：风险高于整体水平。

### 7. 累计指标

最终箱按 `bin_order` 从低风险向高风险排序，然后逐箱累计：

```text
cum_n
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

代码读取 3 张 CSV，并清理字段名中的 UTF-8 BOM 和少量乱码前缀。

主要对象：

```text
sample
application
score
```

运行日志会打印原始行数、有效模型分行数和模型分缺失行数。

查看重点：

- 文件是否成功读取。
- 行数是否符合预期。
- 模型分缺失率是否可接受。

### 2. 拼接分析宽表

拼接顺序：

1. 以 `sample` 为底表，用 `application_id + user_id` 左连接 `application_info.csv`（只补充 sample 缺失的字段）。
2. 模型分表按 `application_id` 去重（保留第一条），重命名为 `score_mlt` 后左连接。
3. `application_month` 缺失时用 `application_time` 的月份补齐。
4. 数值字段统一转为数值类型。
5. 只保留存在模型分的样本。

主要结果：

```text
data
```

### 3. 切分 Train、OOT 和未知样本

```text
train：application_month <= 2025-10
oot：application_month >= 2025-11
gap_or_unknown：其他情况
```

主要结果：

```text
all_data / train / oot
sample_group 列
```

查看重点：

- Train 是否覆盖足够长时间。
- OOT 是否有足够样本和成熟标签。
- OOT 只用于最终验证，不能参与任何合箱方案选择。

### 4. 切分 Development / Validation

在 Train 内再切两段：

- 优先取最后 `VALIDATION_MONTH_COUNT`（默认 3）个完整月份作为 Validation。
- 如果 Train 月份数不足 `MIN_DEVELOPMENT_MONTH_COUNT + 1`（默认 4），则按 `application_time` 顺序切出最后 20% 作为 Validation。
- 合箱只在 Development + Validation 上完成；OOT 全程不参与。

主要结果：

```text
development / validation / validation_months
```

### 5. 在 Development 上做 20 等频初分

主模型字段：

```text
score_mlt
```

初始分箱字段：

```text
score_mlt_bin20
```

处理逻辑：

1. 使用 `pd.qcut` 在 Development 上按分位数切 20 箱。
2. 相同分数过多时使用 `duplicates='drop'`，实际箱数可能少于 20；少于 `MIN_FINAL_BIN_COUNT`（6）时直接报错。
3. 最左边界改为 `-inf`，最右边界改为 `inf`。
4. 区间规则为 `(left, right]`。
5. 初始箱编号为 `B01`、`B02`……。
6. 将 Development 学到的边界原样应用到 Validation、完整 Train、OOT 和全量数据。

主要结果：

```text
score_mlt_bin_edges（edges 数组）
score_mlt_bin20（分箱标签）
```

### 6. 计算初始箱指标

在 Development 和 Validation 上分别计算 20 箱的完整统计（无样本的箱也保留，防止候选范围错位）。

主要结果：

```text
development_initial_stats
validation_initial_stats
```

指标包括：每箱规模、1M30+ / 3M30+ 笔数与金额指标、Lift、累计指标等。

### 7. 自动合箱到最终风险等级

这是当前版本的核心变化：**不再使用固定合箱方案，而是自动搜索并评分选出 6~8 档方案**（默认目标 7 档）。合箱只在 Development + Validation 上进行。

#### 7.1 单箱硬约束

每个最终箱都必须满足（Development 上检查）：

| 约束 | 默认值 | 说明 |
| --- | ---: | --- |
| 中间箱样本占比 | >= 5% | 头尾箱放宽到 2.5% |
| 主指标成熟量 | >= 1000 | `3m30p_cnt_mature` |
| 主指标坏样本量 | >= 20 | `3m30p_cnt_bad` |
| 主指标好样本量 | >= 200 | `3m30p_cnt_good` |

#### 7.2 单调性要求

- Development 上主指标（1M30+、3M30+ 笔数逾期率）不允许相邻倒挂。
- Validation 上允许不超过 0.3 个百分点的容忍倒挂。
- 候选评分同时监控四个风险率（含金额口径）的 Validation 倒挂数。

#### 7.3 合并代价

合并某对相邻箱时计算综合代价：

```text
merge_cost = 风险率差距 × 100
           + (1 - 两比例 Z 检验 p 值)
           + IV 损失 × 10
           + 保护边界惩罚（100，若该边界受保护）
           + 头尾箱边界惩罚（15，若合并涉及最左/最右边界）
```

风险越接近、差异越不显著、IV 损失越小，越优先合并。

#### 7.4 保护边界

以下边界在自动合箱中默认尽量保留（强制处理小箱或倒挂时仍允许跨越）：

- 自动通过 / 整体接纳约束对应的累计 3M30+ 风险边界。
- 边际 3M30+ 风险超过上限的边界。
- `PROTECT_LARGEST_RISK_JUMPS`（默认 1）个风险跳升最大的边界。

#### 7.5 合并顺序

```text
第 1 步 小箱清理：样本占比、成熟量或好坏样本量不足的箱优先合并
第 2 步 单调合并：主指标出现相邻倒挂时，从倒挂最严重的一对开始合并（PAVA 风格）
第 3 步 档位压缩：若仍超过 8 档，强制合并到 <= 8 档
第 4 步 候选生成：继续按“统计不显著或风险率接近”合并出 6 档、7 档等候选
```

每一步产生一个候选方案并记录合并原因；初始 20 箱也作为一个候选。

#### 7.6 候选方案评分

每个候选方案计算综合得分：

```text
candidate_score
= 100 × 硬约束全部满足
- 30 × Development 主指标倒挂数
- 12 × Validation 主指标倒挂数
- 4  × Validation 全指标倒挂数
- 15 × 单箱约束违反数
- 150 × max(0, Validation PSI - 0.05)
+ 12 × 主指标 IV 保留率（截断到 0~1.5）
+ 5  × Development/Validation 风险排序 Spearman 相关
+ 100 × 最小相邻风险差距
+ 2  × 候选策略接纳率
+ 头尾箱纯度评分
- 1.5 × |档位数 - 7|
```

排序优先级（依次）：

```text
硬约束通过 → Development 倒挂数 → 约束违反数 → Validation 倒挂数
→ Validation PSI 可接受 → Validation PSI 偏好 → candidate_score
→ IV 保留率 → 跨样本排序相关 → 档位距离
```

#### 7.7 头尾箱纯度评分

`extreme_score_component` 用于偏好“头箱更干净、尾箱更分明”的方案：

```text
= 8 × max(0, 尾箱 Lift - 头箱 Lift)          # Lift 差越大越好
+ 250 × max(0, 头相邻差 + 尾相邻差)            # 首尾与相邻箱的风险跳升
- 10 × max(0, 头箱 Lift - 0.70)              # 头箱 Lift 过高则惩罚
- 10 × max(0, 1.30 - 尾箱 Lift)              # 尾箱 Lift 不足则惩罚
```

#### 7.8 最终选定

按上述排序取第一名的合箱范围作为最终方案。运行日志会打印实际档位数和方案，例如：

```text
3/9 自动合箱完成：7 档，方案=[(1,2), (3,5), (6,8), (9,11), (12,14), (15,18), (19,20)]
```

主要结果：

```text
merge_candidates（候选评分表，selected=True 为最终方案）
merge_steps（合箱步骤过程表）
protected_boundaries（受保护边界集合）
score_mlt_final_bin（最终风险等级，A、B、C……按风险从低到高编号）
```

### 8. 将最终合箱映射应用到所有样本

```text
development_final / validation_final / train_final / oot_final / all_final
```

并生成 Development / Validation / Train / OOT 四个切片的最终箱统计（`bin_stats_final_*`）。

### 9. 最终验证

#### 9.1 单调性检查

对四个切片分别检查四类风险率是否随风险等级非递减：

```text
1m30p_cnt_bad_rate
3m30p_cnt_bad_rate
1m30p_amt_bad_rate
3m30p_amt_bad_rate
```

输出每个切片的单调性结论、倒挂次数和倒挂位置。

#### 9.2 PSI

比较 Train 和 OOT 在最终风险等级上的分布差异。

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

上述区间是常用经验值，当前代码只计算 PSI，不自动按该区间给出结论。合箱选型中使用的两个阈值是：`PREFERRED_MAX_VALIDATION_PSI = 0.05`（偏好）、`MAX_ACCEPTABLE_VALIDATION_PSI = 0.10`（可接受）。

#### 9.3 AUC 和 KS

代码分别对 `duedate_1m_30` 和 `duedate_3m_30`，按 Train / OOT 计算：

```text
n
bad_cnt
good_cnt
bad_rate
auc
ks
```

AUC 和 KS 直接按秩和与累计好坏分布计算，不依赖 sklearn。

#### 9.4 月度稳定性

按月输出每个最终箱的样本量、成熟量、主指标风险率和相邻倒挂标记（`primary_inversion_flag`），并汇总每个月：

```text
n
成熟量 / 坏样本量
主指标整体风险率
箱数
主指标倒挂次数
最大单次风险跌幅
主指标单调性是否 OK
```

### 10. 构造阈值曲线

阈值曲线只用**最终箱右边界**作为候选阈值；最后一个箱的右边界是 `inf`，代码使用 Train 中最大实际分数替代，以保留“全量通过”点。

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
marginal_3m30p_cnt_bad_rate
```

解读方式：

- `cum_pass_rate`：阈值放宽到当前位置，累计通过多少样本。
- `cum_*_bad_rate`：累计接纳人群的整体风险。
- `marginal_*_bad_rate`：本次放宽阈值新增人群的风险。
- 如果累计风险尚可，但边际风险快速上升，说明阈值已接近风险拐点。

### 11. 生成策略方案

当前版本只生成**一套默认策略**（不再是保守/平衡/增长三套），在最终箱边界阈值曲线上，选择满足风险约束且累计通过率最高的阈值。

#### 11.1 当前约束配置

```python
STRATEGY_CONFIG = {
    "strategy_name": "默认策略",
    "objective": "平衡通过率、整体风险和边际风险",
    "auto_constraints": {
        "max_cum_1m30p_cnt_bad_rate": 0.0090,
        "max_cum_3m30p_cnt_bad_rate": 0.0550,
        "max_marginal_3m30p_cnt_bad_rate": 0.0900,
    },
    "accept_constraints": {
        "max_cum_1m30p_cnt_bad_rate": 0.0130,
        "max_cum_3m30p_cnt_bad_rate": 0.0750,
        "max_marginal_3m30p_cnt_bad_rate": 0.1700,
    },
}
```

| 阶段 | 累计 1M30+ 上限 | 累计 3M30+ 上限 | 边际 3M30+ 上限 |
| --- | ---: | ---: | ---: |
| 自动通过 | 0.90% | 5.50% | 9.00% |
| 总接纳 | 1.30% | 7.50% | 17.00% |

#### 11.2 阈值选择规则

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
strategy_plan（策略结果表，status=OK 表示有解）
strategy_segments（Train / OOT 三段验证表）
```

`strategy_segments` 分别计算自动通过、人工审核、拒绝三段在 Train 和 OOT 的规模及风险。

### 12. 生成 Excel 报告

最后使用 `openpyxl` 创建：

```text
out/binning_strategy_report_YYYYMMDD.xlsx
```

共 6 个 Sheet，见下一节。

---

## 五、Excel 结果表总览与查看方法

建议按以下顺序阅读：

```text
01_总览 → 02_分箱详情 → 03_最终分箱统计 → 04_策略方案 → 05_模型验证 → 06_附录
```

### Sheet 1：`01_总览`

按模块分组展示核心结论（`section` 分组、`metric` 指标名、`value` 值）：

| 模块 | 主要内容 |
| --- | --- |
| 样本 | 原始样本量、有效模型分样本量、模型分缺失量/率 |
| 时间切分 | Train 截止月份、Validation 月份、OOT 起始月份 |
| 分箱 | 初始箱数量、最终箱数量、合箱主指标、最终采用合箱方案、受保护初始边界 |
| 稳定性 | 最终箱 Train/OOT PSI |
| 候选评分 | Development/Validation 倒挂数、两者 PSI、IV 保留率、跨样本排序相关、候选综合得分 |
| 模型效果 | 各样本组 × 各标签的 bad_rate / AUC / KS |
| 单调性 | development / validation / train / oot 最终箱是否全部单调 |
| 策略 | 自动通过阈值及截止档、人工审核上限/拒绝阈值及截止档、三段占比 |
| 策略风险 | 接纳人群 1M30+ / 3M30+ 笔数与金额逾期率、最后接纳档边际 3M30+ |

查看方法：

1. 先看分箱方案和档位数是否符合预期。
2. 检查最终箱 PSI、单调性和 AUC/KS。
3. 检查三段规则与占比是否符合业务目标。
4. 再到对应 sheet 验证细节。

### Sheet 2：`02_分箱详情`

包含三个 section：

#### 表 1：分箱过程

底层对象：

```text
binning_process
```

每个初始箱一行，展示：

```text
initial_bin_order / score_mlt_bin20
score_left / score_right / score_min / score_max / score_mean
n / sample_pct
1m30p / 3m30p 的成熟量、坏样本量、风险率、与前一箱风险差（rate_diff_prev）
倒挂标记（1m30p_inversion_flag / 3m30p_inversion_flag）
1m30p / 3m30p 金额敞口、逾期金额、金额逾期率
cum_pass_rate / cum_1m30p_cnt_bad_rate / cum_3m30p_cnt_bad_rate
final_bin_order / score_mlt_final_bin / merged_from / merge_action
```

查看方法：

- `*_rate_diff_prev < 0` 表示该箱风险低于前一箱，即倒挂。
- `merge_action` 为“相邻箱合并”表示该初始箱与相邻箱合并为同一最终档。
- 关注被合并的箱是否有业务上的解释。

#### 表 2：合箱候选评分

底层对象：

```text
merge_candidates
```

每个候选方案一行，`selected=True` 的行即为最终方案。字段包括档位数、合箱范围、各阶段合并原因、Development/Validation 倒挂数、单箱约束违反数、Validation PSI、IV 保留率、跨样本排序相关、头尾箱指标和 `candidate_score`。

查看方法：

- 理解“为什么最终选择了这个方案”。
- 比较被选方案与相邻候选的倒挂数和 PSI。

#### 表 3：合箱步骤

底层对象：

```text
merge_steps
```

按顺序记录每次合箱的前后范围、档位数、合并阶段（small_bin_cleanup / pava_monotonic_merge / granularity_reduction / candidate_reduction）和合并原因。

### Sheet 3：`03_最终分箱统计`

底层对象：

```text
final_development_stats / final_validation_stats / final_train_stats / final_oot_stats
```

四个切片的最终箱统计合并为一张表，用 `sample_group` 列区分 Development / Validation / Train / OOT。字段与分箱过程类似（不含初始箱列，含 `merged_from`、`score_left`、`score_right`、累计指标等）。

查看方法：

- 从 `bin_order=1` 向下看风险是否逐步升高。
- 同一风险等级在四个切片中的风险方向是否一致。
- OOT 单箱成熟量很小时，不要过度解释短期波动。

### Sheet 4：`04_策略方案`

包含三个 section：

#### 表 1：阈值选择过程

底层对象：

```text
threshold_selection
```

每个候选阈值一行，展示累计/边际指标，并标记：

```text
auto_all_constraints_ok / accept_all_constraints_ok（约束是否满足）
selected_role（自动通过阈值 / 人工审核上限·拒绝阈值 / 两者重合）
selection_reason
```

选中行有绿色/橙色高亮，约束不满足的标记为红色。

#### 表 2：策略结果

底层对象：

```text
strategy_plan
```

字段：

```text
status（OK 或 无满足约束的阈值）
auto_pass_threshold / auto_pass_bin
reject_threshold / manual_review_upper_bin
auto_pass_rate / accepted_rate / manual_review_rate / reject_rate
accepted_1m30p_cnt_bad_rate / accepted_3m30p_cnt_bad_rate
accepted_1m30p_amt_bad_rate / accepted_3m30p_amt_bad_rate
last_accepted_marginal_3m30p_cnt_bad_rate
```

#### 表 3：策略分段验证

底层对象：

```text
strategy_segments
```

按 Train / OOT × 自动通过 / 人工审核 / 拒绝六段输出规模与风险指标。

查看方法：

- 风险应呈现“自动通过 < 人工审核 < 拒绝”的整体梯度。
- Train 和 OOT 的三段占比及风险不应出现严重反转。
- 如果 OOT 3M 金额指标为空，应先检查表现成熟度，不能直接解释为零风险。

### Sheet 5：`05_模型验证`

包含六个 section：

#### 表 1：Train/OOT 逐箱对比

底层对象：

```text
train_oot_compare
```

最终箱的 Train 与 OOT 指标并列（`_train` / `_oot` 后缀），含 `merged_from`、`score_left`、`score_right`。

#### 表 2：AUC/KS

底层对象：

```text
performance
```

Train / OOT × 1M30+ / 3M30+ 的 `n`、`bad_cnt`、`good_cnt`、`bad_rate`、`auc`、`ks`。

#### 表 3：PSI

底层对象：

```text
psi
```

逐箱 `expected_*` / `actual_*` / `psi_component`，`psi_total` 在最后一行。

#### 表 4：单调性

底层对象：

```text
monotonicity
```

四个切片 × 四类风险率的 `is_monotonic_non_decreasing`、`violation_cnt`、`violation_bins`。

#### 表 5：月度稳定性汇总

底层对象：

```text
monthly_stability_summary
```

每个月一行：样本量、成熟量、坏样本量、整体风险率、箱数、倒挂次数、最大单次风险跌幅、单调性是否 OK。

#### 表 6：月度箱表现

底层对象：

```text
monthly_stability
```

按月份 × 样本组 × 最终档输出箱级指标与 `primary_inversion_flag`（相邻倒挂标记，黄色高亮）。

### Sheet 6：`06_附录`

#### 表 1：配置参数

底层对象：

```text
config_table
```

输出便于修改和版本管理的参数表，包括：

```text
基础配置：DATA_DIR / TRAIN_END_MONTH / OOT_START_MONTH / VALIDATION_MONTH_COUNT
        / ACTUAL_VALIDATION_MONTHS / INITIAL_BIN_COUNT / HIGH_SCORE_HIGH_RISK
合箱配置：MIN/MAX/TARGET_FINAL_BIN_COUNT / PRIMARY_RATE_COL / 单箱约束
        / 头尾箱评分权重 / 单调与相邻差异控制 / Validation PSI 阈值
        / PROTECTED_BOUNDARIES / SELECTED_FINAL_BIN_RANGES
策略配置：自动通过与总接纳的累计/边际风险上限
```

#### 表 2：指标说明

底层对象：

```text
metric_dictionary
```

核心字段的名称和计算口径说明。

---

## 六、当前代码中已定义但未实际输出的内容

当前脚本没有定义未使用的分析功能。历史版本中的以下内容已移除：

- 审批漏斗函数（`calc_funnel_stats`）。
- `score_apply` 和交易子模型表的读取与拼接。
- 6/7/8/9 档候选合箱方案的横向比较。
- 3/4 位小数边界取整敏感性分析。
- 阈值敏感性扫描（人工审核产能 × 风险上限矩阵）。
- 三套策略方案（保守/平衡/增长）对比。

当前报告本质上是对 `score_mlt` 的一套自动合箱 + 一套默认策略的完整报告。

---

## 七、复现操作步骤

### 第 1 步：准备数据

将 3 张输入表放到 `res` 目录，并确认文件名与代码完全一致。

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
VALIDATION_MONTH_COUNT
HIGH_SCORE_HIGH_RISK
SCORE_COL
INITIAL_BIN_COUNT
MIN_FINAL_BIN_COUNT / MAX_FINAL_BIN_COUNT / TARGET_FINAL_BIN_COUNT
单箱约束（MIN_FINAL_BIN_*_COUNT 等）
STRATEGY_CONFIG
```

### 第 4 步：运行脚本

```bash
python binning.py
```

### 第 5 步：检查运行日志

重点确认：

- 3 张表是否成功加载，模型分缺失量是否合理。
- Train / Development / Validation / OOT 样本量。
- 实际初始箱数量（日志 `2/9`）。
- 自动合箱结果（日志 `3/9`，档位数和方案）。
- 是否出现“无满足约束的阈值”或报错。

### 第 6 步：按顺序查看 Excel

1. `01_总览`：看最终合箱方案、PSI、单调性、AUC/KS 和三段规则。
2. `02_分箱详情`：理解 20 箱如何合并为最终档。
3. `03_最终分箱统计`：确认四个切片的最终箱风险梯度。
4. `04_策略方案`：确认阈值选择过程和三段占比。
5. `05_模型验证`：确认 OOT、PSI、月度稳定性。
6. `06_附录`：核对配置参数与指标口径。

### 第 7 步：上线前核对

至少确认：

```text
模型版本
模型分方向
Train / Validation / OOT 时间范围
风险标签定义
成熟样本定义
最终箱边界
区间规则 (left, right]
自动通过阈值
拒绝阈值
生效时间
回滚版本
```

---

## 八、当前实现需要特别注意的问题

### 1. 自动合箱结果随数据变化

当前版本不再使用固定合箱方案，最终档位（6~8 档）和合箱范围由 Development + Validation 的数据自动决定。更换数据或时间范围后：

- 最终方案可能变化，需要重新评审而不是直接沿用历史边界。
- 阈值曲线的候选阈值来自最终箱右边界，因此合箱变化会直接改变策略阈值。
- 建议在报告中确认 `SELECTED_FINAL_BIN_RANGES` 与评审结论一致。

### 2. 分数方向不能只修改一个开关

当前脚本整体按高分高风险设计：

- 初始箱按分数从低到高编号。
- 累计指标从低分向高分累计。
- 单调性检查、合箱方向和阈值规则也按该方向解释。

如果模型是低分高风险，仅将 `HIGH_SCORE_HIGH_RISK=False` 不能保证全部逻辑正确，还需要同步调整分箱顺序、累计方向、合箱解释和阈值规则。

### 3. 初始箱数可能少于 20

`qcut(..., duplicates='drop')` 允许删除重复边界，实际初始箱数可能少于 20。少于 `MIN_FINAL_BIN_COUNT`（6）时脚本会直接报错。

### 4. 重复记录仅做去重，不会强制终止

模型分表按 `application_id` 去重（保留第一条），宽表重复也只影响指标口径。正式生产前应明确：

- 风险标的是申请、用户还是借据。
- 同一申请多条模型分应按什么版本和时间保留。
- 同一申请多条表现记录是否需要聚合。

### 5. 比例在 Excel 中以百分比格式显示

所有包含 `rate` / `pct` / `retention` 的列已设置为 `0.00%` 数字格式：

```text
0.075 → 7.50%
```

AUC / KS / PSI / 相关系数 / p 值使用 `0.0000`，阈值和分数边界使用 `0.0000`。

### 6. 策略无解时的行为

如果 `STRATEGY_CONFIG` 的约束过紧（阈值曲线没有任何一行同时满足约束），`strategy_plan` 的 `status` 为“无满足约束的阈值”，报告仍会正常输出，但策略区块只有状态行。需要根据实际情况放宽约束后重跑。

### 7. 合箱候选评分的权重是经验值

`candidate_score` 的各权重（倒挂惩罚、PSI 惩罚、IV 保留率加分等）是代码中的经验值，用于在多个可行方案中挑选“更单调、更稳定、区分度更好”的方案。它们只影响候选排序，不影响硬约束（档位数、单调性、单箱规模）的判定。

---

## 九、一句话总结

> **当前脚本在 Development 上将 `score_mlt` 等频切成 20 箱，结合样本量、成熟度、风险倒挂、跨期稳定性和头尾箱纯度自动合箱为 6~8 个风险等级（目标 7 档），在 Validation 上验证、OOT 上确认；随后沿低风险到高风险方向计算累计与边际风险，并在风险上限约束下划分自动通过、人工审核和拒绝阈值，最终输出 6 个 sheet 的 Excel 策略报告。**
