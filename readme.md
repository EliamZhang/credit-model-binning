# 模型分数分箱与策略阈值方法及操作指南

> 本文档是本项目统一的方法论与操作说明，与当前 `binning.py` 的实际执行逻辑对应，涵盖样本设计、指标口径、分箱与阈值方法、方案验证、运行方式和结果解读。
>
> 当前脚本的核心目标是：以 `score_mlt` 为主模型分，在完整 Train 上建立稳定风险等级，并划分自动通过、人工审核和拒绝阈值，最终生成 `out/binning_strategy_report_YYYYMMDD.xlsx`。

---

## 一、方法框架

当前脚本按以下顺序执行（对应运行日志中的 1/9 ~ 9/9）：

```text
加载 3 张 CSV
→ 清理字段名并拼接分析宽表
→ 检查关键字段，按月份切分 Train / OOT
→ 在完整 Train 上对 score_mlt 做 20 等频初分，边界复用到 OOT
→ 计算每箱规模、1M30+、3M30+、金额风险、Lift 和累计指标
→ 自动合箱：小箱清理 → 单调合并 → 档位压缩 → 生成 6~8 档候选方案并评分
→ 将最终合箱映射应用到 Train / OOT
→ 验证单调性、PSI、AUC、KS 和月度稳定性
→ 在完整 Train 上生成最终箱边界阈值曲线
→ 按风险约束选择自动通过、人工审核、拒绝阈值
→ 计算 application_info 历史实际审批漏斗，并与模型策略测算流量对照
→ 输出 6 个 sheet 的 Excel 策略报告
```

方法遵循以下原则：

1. **样本隔离**：分箱边界、合箱方案和策略阈值均在 Train 上确定；OOT 仅用于样本外验证，不参与方案选择。
2. **约束优先**：最终方案必须优先满足档位数、主指标单调性、单箱统计充分性和极端边界保护等硬约束，再比较区分度和相邻风险分离度。
3. **过程可追溯**：逐步记录小箱清理、单调合并、档位压缩和候选生成过程，以及每次合并的风险差异、显著性、IV 损失和原因。

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
"C:\Users\zhangyuliang02\Desktop\Project.venv\Scripts\python.exe" -m pip install -r requirements.txt
```

默认使用的 Python 解释器为：

```text
C:\Users\zhangyuliang02\Desktop\Project.venv\Scripts\python.exe
```

当前脚本核心依赖 pandas / numpy / openpyxl；`requirements.txt` 中还包含了数据分析时常用的 scipy / statsmodels / matplotlib / jupyter。

### 3. 运行方式

```bash
"C:\Users\zhangyuliang02\Desktop\Project.venv\Scripts\python.exe" binning.py
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
- `application_month` 缺失时，用 `application_time` 的月份补齐；若仍缺失则该行不进入 Train / OOT，不参与任何分析。
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
status
application_status
assessment_status
```

### 6. 当前核心配置

```python
DATA_DIR = Path("res")
TRAIN_END_MONTH = "2025-10"
OOT_START_MONTH = "2025-11"
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
- 时间不在上述范围的样本不进入 Train / OOT，不参与任何分析。
- 完整 Train 用于学习分箱边界、执行合箱、选择候选方案和确定策略阈值；OOT 完全独立，不参与调参。
- 初始等频分 20 箱，自动合箱到 6~8 档（目标 7 档）。
- `application_month` 必须使用可按字符串正确比较的 `YYYY-MM` 格式。

---

## 三、风险标签和指标口径

当前代码同时计算 `1M30+` 和 `3M30+`，并分别提供笔数口径和金额口径。

| 指标 | 主要作用 |
| --- | --- |
| 1M30+ 笔数逾期率 | 短期风险识别；与 3M30+ 共同作为合箱单调性主指标 |
| 3M30+ 笔数逾期率 | 中期风险识别；同时作为成熟量、显著性检验和 IV 等统计环节的锚定指标 |
| 1M30+ / 3M30+ 金额逾期率 | 衡量金额损失强度，用于候选评价和最终验证，不直接触发主指标单调合箱 |

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

笔数逾期率的单箱、累计和边际点估计同时输出 95% Wilson 置信区间。设逾期率为 `p = bad / mature`、`z = 1.96`，则：

```text
置信区间 = (p + z²/(2m) ± z·sqrt(p(1-p)/m + z²/(4m²))) / (1 + z²/m)
```

其中 `m` 为成熟样本量。尾部箱样本较少时，应结合置信区间上界判断保守风险，不应只比较点估计。

### 8. 历史实际审批与模型策略测算口径

报告严格区分两类指标：

- **历史实际审批漏斗（`actual_*`）**：来自 `application_info.csv` 的真实申请与审批状态，所有数量按 `application_id` 去重。
- **模型策略测算流量（`strategy_estimated_*`）**：按 `score_mlt` 和 Train 确定的策略阈值测算，不代表历史真实审批结果。

历史实际审批漏斗定义：

| 指标 | 计算公式 | 判定条件 |
| --- | --- | --- |
| `actual_completion_rate` | 完成进件数 / 申请数 | `application_status` 不属于 `0.Incomplete`、`1.In Progress` |
| `actual_approval_rate` | 审批通过数 / 完成进件数 | `application_status` 首字符为 3 或 4 |
| `actual_auto_approval_rate` | 自动审批通过数 / 完成进件数 | 已审批通过且 `assessment_status` 含 `Auto Approved` |
| `actual_manual_approval_rate` | 人工审批通过数 / 完成进件数 | 已审批通过且 `assessment_status` 含 `Manual Approved` |
| `actual_auto_approval_share` | 自动审批通过数 / 全部审批通过数 | 衡量通过件中的自动审批构成 |
| `actual_manual_approval_share` | 人工审批通过数 / 全部审批通过数 | 衡量通过件中的人工审批构成 |
| `actual_deal_rate` | 成交数 / 全部审批通过数 | `status` 属于 `Active_Account`、`Closed`、`Blocked` |

模型策略测算流量定义：

```text
strategy_estimated_auto_pass_rate   = score ≤ 自动通过阈值的申请数 / 有效模型分申请数
strategy_estimated_manual_review_rate = 自动通过阈值 < score ≤ 总接纳阈值的申请数 / 有效模型分申请数
strategy_estimated_total_accept_rate  = 自动通过数与人工审核数之和 / 有效模型分申请数
strategy_estimated_reject_rate        = score > 总接纳阈值的申请数 / 有效模型分申请数
```

当前模型为高分高风险；低分高风险模型的比较方向相反。

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
其他（时间缺失或不在上述范围）：不参与任何分析
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

### 4. 在 Train 上做 20 等频初分

主模型字段：

```text
score_mlt
```

初始分箱字段：

```text
score_mlt_bin20
```

处理逻辑：

1. 使用 `pd.qcut` 在完整 Train 上按分位数切 20 箱。
2. 相同分数过多时使用 `duplicates='drop'`，实际箱数可能少于 20；少于 `MIN_FINAL_BIN_COUNT`（6）时直接报错。
3. 最左边界改为 `-inf`，最右边界改为 `inf`。
4. 区间规则为 `(left, right]`。
5. 初始箱编号为 `B01`、`B02`……。
6. 将 Train 学到的边界原样应用到 OOT 和全量数据。

主要结果：

```text
score_mlt_bin_edges（edges 数组）
score_mlt_bin20（分箱标签）
```

### 5. 计算初始箱指标

在 Train 上计算 20 箱的完整统计（无样本的箱也保留，防止候选范围错位）。

主要结果：

```text
train_initial_stats
```

指标包括：每箱规模、1M30+ / 3M30+ 笔数与金额指标、Lift、累计指标等。

### 6. 自动合箱到最终风险等级

这是当前版本的核心变化：**不再使用固定合箱方案，而是基于完整 Train 自动搜索并评分选出 6~8 档方案**（默认目标 7 档）。OOT 不参与合箱或候选选择。

#### 6.1 单箱硬约束

每个最终箱都必须满足（Train 上检查）：

| 约束 | 默认值 | 说明 |
| --- | ---: | --- |
| 中间箱样本占比 | >= 5% | 头尾箱放宽到 2.5% |
| 主指标成熟量 | >= 1000 | `3m30p_cnt_mature` |
| 主指标坏样本量 | >= 20 | `3m30p_cnt_bad` |
| 主指标好样本量 | >= 200 | `3m30p_cnt_good` |

最好/最坏两个极端初始箱（默认各圈选 1 个，见 6.4）使用放宽约束（`bin_constraint_minimums`）：

| 极端箱约束 | 默认值 | 说明 |
| --- | ---: | --- |
| 主指标成熟量 | >= 500 | 放宽到普通箱的一半 |
| 最好箱坏样本量 | >= 0 | 低风险端天然坏样本少，不设下限 |
| 最好箱好样本量 | >= 200 | 与普通箱一致 |
| 最坏箱坏样本量 | >= 20 | 与普通箱一致 |
| 最坏箱好样本量 | >= 0 | 高风险端天然好样本少，不设下限 |

四项约束必须同时满足。若存在多个违规箱，代码按各项相对缺口之和计算违反严重度：

```text
violation_severity = Σ max(0, 1 - 实际值 / 要求值)
```

小箱清理阶段优先处理违反严重度最高的箱。

#### 6.2 单调性要求

- Train 上主指标（1M30+、3M30+ 笔数逾期率）不允许相邻倒挂。
- 候选评分同时监控 Train 上四个风险率（含金额口径）的倒挂数。
- 月度稳定性检查允许 0.3 个百分点的容忍倒挂。

#### 6.3 合并代价

合并某对相邻箱时计算综合代价：

```text
merge_cost = 风险率差距 × MERGE_COST_RATE_GAP_WEIGHT（默认 100）
           + (1 - 两比例 Z 检验 p 值)
           + IV 损失 × MERGE_COST_IV_LOSS_WEIGHT（默认 10）
           + 保护边界惩罚（默认 100，若该边界受策略保护）
           + 极端边界惩罚（默认 10000，若该边界为极端圈选边界）
```

风险越接近、差异越不显著、IV 损失越小，越优先合并；跨越策略保护边界或极端圈选边界会显著抬高代价。

各项计算口径如下：

- 风险率差距取 1M30+、3M30+ 两个笔数逾期率绝对差距的较大值，避免任一主指标差异明显时被轻易合并。
- 两比例 Z 检验以 3M30+ 坏样本量和成熟量计算；`p` 值越高，表示相邻箱差异越不显著。
- IV 以 3M30+ 好坏样本量计算并加入 0.5 平滑项；IV 损失为 `max(0, 合并前 IV - 合并后 IV)`。

#### 6.4 保护边界

合箱中有两类边界需要保护，机制不同：

1. **策略保护边界**（尽量保留，必要场景仍可跨越，跨越按 `PROTECTED_BOUNDARY_PENALTY=100` 计入合并代价）：
   - 自动通过 / 整体接纳约束对应的累计 3M30+ 风险边界。
   - 边际 3M30+ 风险超过上限的边界。
   - `PROTECT_LARGEST_RISK_JUMPS`（默认 1）个风险跳升最大的边界。

2. **极端圈选边界**（默认 `PROTECT_EXTREME_INITIAL_BINS=True`，从低风险端圈选 1 个最好初始箱、从高风险端圈选 1 个最坏初始箱，两处切点即极端边界）：
   - 默认**硬禁止跨越**：`ALLOW_EXTREME_BIN_MERGE_FOR_HARD_CONSTRAINTS=False` 时，极端边界不进入任何合箱候选，四个合箱阶段都不会跨过它，保证最好箱不被高风险相邻箱稀释、最坏箱不被低风险相邻箱稀释。
   - 若改为 `True`：允许跨越，但按 `EXTREME_BOUNDARY_PENALTY=10000` 计入合并代价，且候选评分按 `EXTREME_BOUNDARY_VIOLATION_PENALTY=50` 每次扣分。
   - 硬约束要求极端边界跨越数为 0（见 6.6）。

#### 6.5 合并顺序

```text
第 1 步 小箱清理：样本占比、成熟量或好坏样本量不足的箱优先合并
第 2 步 单调合并：主指标出现相邻倒挂时，从倒挂最严重的一对开始合并（PAVA 风格）
第 3 步 档位压缩：若仍超过 8 档，强制合并到 <= 8 档
第 4 步 候选生成：继续按“统计不显著或风险率接近”合并出 8 档、7 档、6 档候选
```

每一步产生一个候选方案并记录合并原因；初始 20 箱也作为一个候选。

四个阶段的执行口径为：

1. **小箱清理**：定位违反严重度最高的箱，只比较其左右相邻合并方案，选择代价较低者，直至约束满足或达到最少档位数。
2. **单调合并**：1M30+、3M30+ 任一主指标倒挂即纳入处理，从跌幅最严重的相邻对开始合并，直至无倒挂或达到最少档位数。
3. **档位压缩**：档位数超过 8 时，从全部允许的相邻对中反复选择合并代价最低者。
4. **候选生成**：继续生成 8、7、6 档候选；优先合并两比例检验 `p >= 0.10` 或主指标差距 `<= 0.3%` 的相邻对，否则选择全部允许相邻对中代价最低者。

初始状态和每次合并后的状态均进入候选集合，并按合箱范围去重，因此最终选择覆盖整个合箱路径，而不是只比较各阶段的终态。

#### 6.6 候选方案评分

每个候选方案计算综合得分：

```text
candidate_score
= +100 × 硬约束全部满足
- 30 × Train 主指标倒挂数
- 4  × Train 全指标倒挂数
- 15 × 单箱约束违反数
+ 12 × min(主指标 IV 保留率, 1.5)
+ 100 × max(0, 最小相邻风险差距)
- 1.5 × |档位数 - 7|
- 50 × 极端边界跨越数
```

各权重由 `CANDIDATE_SCORE_WEIGHTS` 统一配置（键名同上，值会输出到 `06_附录`）。**硬约束** = 档位数在 6~8 之间 + Train 主指标倒挂为 0 + 单箱约束全部满足 + 极端边界跨越数为 0。

排序优先级（依次）：

```text
硬约束通过 → Train 主指标倒挂数 → 约束违反数 → Train 全指标倒挂数
→ candidate_score → IV 保留率 → 档位距离
```

#### 6.7 最终选定

按上述排序取第一名的合箱范围作为最终方案。若没有任何候选通过全部硬约束，则退而求其次：忽略硬约束筛选，直接从全部候选中按同样排序取第一名（此时 `hard_constraints_ok` 为 False，需在候选评分表中确认原因）。运行日志会打印实际档位数和方案，例如：

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

### 7. 将最终合箱映射应用到所有样本

```text
train_final / oot_final / all_final
```

并生成 Train / OOT 两个数据集的最终箱统计。

### 8. 最终验证

#### 8.1 单调性检查

对 Train / OOT 分别检查四类风险率是否随风险等级非递减：

```text
1m30p_cnt_bad_rate
3m30p_cnt_bad_rate
1m30p_amt_bad_rate
3m30p_amt_bad_rate
```

输出每个数据集的单调性结论、倒挂次数和倒挂位置。

#### 8.2 PSI

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

上述区间是常用经验值，当前代码计算 Train/OOT PSI，但不将其用于合箱候选选择，以保持 OOT 的独立性。

#### 8.3 AUC 和 KS

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

#### 8.4 月度稳定性

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

验证指标采用以下统一口径：

```text
PSI = Σ (OOT占比 - Train占比) × ln(OOT占比 / Train占比)
AUC = (坏样本秩和 - 坏样本数×(坏样本数+1)/2) / (坏样本数×好样本数)
KS  = max(|累计坏样本占比 - 累计好样本占比|)
```

PSI 计算加入 `1e-6` 平滑项；AUC、KS 分别对 Train/OOT 的 1M30+ 和 3M30+ 标签计算。

### 9. 构造阈值曲线

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

### 10. 生成模型策略测算方案

当前版本只生成**一套默认策略**（不再是保守/平衡/增长三套），在最终箱边界阈值曲线上，选择满足风险约束且累计通过率最高的阈值。

#### 10.1 当前约束配置

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

#### 10.2 阈值选择规则

1. **自动通过阈值**：满足自动通过约束的最大阈值。
2. **总接纳阈值**：满足接纳约束的最大阈值。
3. 如果总接纳阈值低于自动通过阈值，则将两者对齐。

其中“最大阈值”是指：过滤掉任一约束不满足的候选后，选择累计通过率最高的最终箱右边界；累计通过率并列时，选择档位更靠后的边界。

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
actual_funnel_report（Train / OOT / All 历史实际审批漏斗）
strategy_estimated_flow（Train / OOT / All 模型策略测算流量）
```

`strategy_segments` 分别计算自动通过、人工审核、拒绝三段在 Train 和 OOT 的规模及风险。

#### 10.3 历史实际审批与模型策略测算对照

代码按相同的 Train/OOT 时间切片分别输出：

- 历史实际审批漏斗：完成率、审批通过率、自动/人工审批通过率、自动/人工审批占比和成交转化率；
- 模型策略测算流量：自动通过率、人工审核率、总接纳率和拒绝率；
- 全量汇总：用于核对 Train 与 OOT 加总以及两套口径的总体差异。

两套口径只能并列比较，不可互相替代：历史实际指标包含当期完整业务审批流程，模型策略测算指标仅反映当前模型阈值下的理论流量。

#### 10.4 阈值上线规则

- 线上使用与离线一致的浮点模型分和原始边界精度，不对阈值二次取整；工程上必须限制小数位时，只允许向更严方向向下取整，并重新验证三段规模和风险。
- 分档采用 `(left, right]`：分数等于阈值时进入右闭档；线上必须使用数值比较，不得使用格式化字符串比较。
- 模型分缺失、NaN 或 Inf 不进入自动通过，按拒绝处理；超出训练分数范围的有效值由 `±inf` 边界归入对应极端箱。
- 上线前使用边界值及其相邻浮点值核对离线与线上分档，一致率应达到 100%；上线后持续监控各档占比及 PSI。

### 11. 生成 Excel 报告

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
| 时间切分 | Train 截止月份、OOT 起始月份 |
| 分箱 | 初始箱数量、最终箱数量、合箱主指标、最终采用合箱方案、受保护初始边界 |
| 稳定性 | 最终箱 Train/OOT PSI |
| 候选评分 | Train 主指标与全指标倒挂数、主指标 IV 保留率、候选综合得分 |
| 模型效果 | 各样本组 × 各标签的 bad_rate / AUC / KS |
| 单调性 | train / oot 最终箱是否全部单调 |
| 历史实际审批漏斗 | Train/OOT 的完成率、审批通过率、自动/人工审批指标和成交转化率 |
| 模型策略测算流量 | Train/OOT 的测算自动通过率、人工审核率、总接纳率和拒绝率 |
| 模型策略阈值 | 自动通过阈值及截止档、人工审核上限/拒绝阈值及截止档 |
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

每个候选方案一行，`selected=True` 的行即为最终方案。字段包括档位数、合箱范围、各阶段合并原因、Train 主指标与全指标倒挂数、单箱约束违反数、IV 保留率和 `candidate_score`。

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
final_train_stats / final_oot_stats
```

Train / OOT 的最终箱统计合并为一张表，用 `sample_group` 列区分。每一行对应一个最终风险档，并将风险、历史实际审批、模型策略测算流量和箱级模型诊断放在同一行，便于直接比较：

```text
strategy_estimated_decision / strategy_estimated_bin_flow_rate
strategy_estimated_cumulative_flow_rate
actual_completion_rate / actual_approval_rate
actual_auto_approval_rate / actual_manual_approval_rate
actual_auto_approval_share / actual_manual_approval_share / actual_deal_rate
1m30p_iv_component / 3m30p_iv_component
1m30p_ks_curve / 3m30p_ks_curve
train_oot_psi_component / train_oot_psi_total
strategy_estimated_overall_auto_pass_rate
strategy_estimated_overall_manual_review_rate
strategy_estimated_overall_total_accept_rate
strategy_estimated_overall_reject_rate
overall_1m30p_auc / overall_3m30p_auc
overall_1m30p_ks / overall_3m30p_ks
```

其中，历史实际指标在每个 `score_mlt_final_bin` 内按唯一 `application_id` 重新计算；策略测算字段表示该箱的策略归属、单箱流量贡献及累计流量。所有指标均使用独立字段，不把 1M/3M、笔数/金额、自动/人工或 AUC/KS 合并在同一列。AUC、整体 KS、整体 PSI 和整体策略转化率属于样本组指标，不定义为单箱指标，因此使用带 `overall` 或 `total` 的独立字段在分箱表中重复展示；箱级 IV、KS 曲线点和 PSI 分项另设独立字段。

字段与分箱过程类似（不含初始箱列，含 `merged_from`、`score_left`、`score_right`、累计指标等），并在 1M30+ / 3M30+ 笔数逾期率及累计口径旁附带 95% Wilson 置信区间：

```text
1m30p_cnt_bad_rate_ci_low / 1m30p_cnt_bad_rate_ci_high
3m30p_cnt_bad_rate_ci_low / 3m30p_cnt_bad_rate_ci_high
cum_1m30p_cnt_bad_rate_ci_low / cum_3m30p_cnt_bad_rate_ci_high（含 _high 上界）
```

查看方法：

- 从 `bin_order=1` 向下看风险是否逐步升高。
- 同一风险等级在 Train / OOT 中的风险方向是否一致。
- 对照箱内历史实际审批表现与模型策略归属，识别实际流程和测算策略差异最大的风险档。
- `*_iv_component` 可求和得到样本组整体 IV；`*_ks_curve` 的最大值与离散分档口径 KS 对应；各箱 `train_oot_psi_component` 之和等于整体 PSI。
- OOT 单箱成熟量很小时，不要过度解释短期波动。
- 尾部箱样本量小、置信区间宽，应结合 `*_cnt_bad_rate_ci_high`（保守风险上界）解读风险率，不能只看点估计。

### Sheet 4：`04_策略方案`

包含六个 section：

#### 表 1：历史实际审批漏斗

底层对象：

```text
actual_funnel_report
```

按 Train / OOT / All 输出 `actual_*` 数量和比率，数据来自 `application_info.csv`，所有数量按唯一 `application_id` 统计。

#### 表 2：模型策略测算流量

底层对象：

```text
strategy_estimated_flow
```

按 Train / OOT / All 输出 `strategy_estimated_*` 数量和比率，并列展示测算自动通过、人工审核、总接纳和拒绝流量。

#### 表 3：阈值选择过程

底层对象：

```text
threshold_selection
```

每个候选阈值一行，展示累计/边际指标（含 3M30+ 累计与边际的 95% Wilson 置信区间上界 `cum_3m30p_cnt_bad_rate_ci_high` / `marginal_3m30p_cnt_bad_rate_ci_high`），并标记：

```text
auto_all_constraints_ok / accept_all_constraints_ok（约束是否满足）
selected_role（自动通过阈值 / 人工审核上限·拒绝阈值 / 两者重合）
selection_reason
```

选中行有绿色/橙色高亮，约束不满足的标记为红色。当累计风险点估计满足约束但 `_ci_high` 越过约束线时，说明该阈值恰好落在不确定区间，需结合尾部箱样本量判断是否从严选择。

#### 表 4：模型策略测算结果

底层对象：

```text
strategy_plan
```

字段：

```text
status（OK 或 无满足约束的阈值）
auto_pass_threshold / auto_pass_bin
reject_threshold / manual_review_upper_bin
strategy_estimated_auto_pass_rate / strategy_estimated_total_accept_rate
strategy_estimated_manual_review_rate / strategy_estimated_reject_rate
accepted_1m30p_cnt_bad_rate / accepted_3m30p_cnt_bad_rate
accepted_1m30p_amt_bad_rate / accepted_3m30p_amt_bad_rate
last_accepted_marginal_3m30p_cnt_bad_rate
```

#### 表 5：模型策略测算阈值敏感性

底层对象：

```text
threshold_sensitivity
```

对自动通过 / 总接纳阈值各输出当前、收严一档、放松一档（一档 = 相邻箱边界）的对比，供风险与业务确认风险上限取值时参考：

```text
threshold_type（自动通过阈值 / 总接纳阈值）  scenario（当前 / 收严一档 / 放松一档）
threshold（变体后阈值）
strategy_estimated_auto_pass_rate / strategy_estimated_manual_review_rate
strategy_estimated_total_accept_rate / strategy_estimated_reject_rate
auto_1m30p_cnt_bad_rate / auto_3m30p_cnt_bad_rate（自动通过人群风险）
accept_3m30p_cnt_bad_rate / accept_marginal_3m30p_cnt_bad_rate（接纳人群风险）
accept_marginal_3m30p_cnt_bad_rate_ci_high（接纳边际 3M30+ 风险 95% Wilson 上界）
strategy_estimated_*_rate_delta（与当前方案的差异）
note（无更严/更松候选、越过对方阈值按规则对齐等说明）
```

当前方案行绿色高亮；阈值移动越过对方阈值时按“总接纳阈值不得严于自动通过阈值”规则对齐，并在 note 中说明。

#### 表 6：模型策略测算分段风险验证

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

Train / OOT × 四类风险率的 `is_monotonic_non_decreasing`、`violation_cnt`、`violation_bins`。

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
基础配置：DATA_DIR / TRAIN_END_MONTH / OOT_START_MONTH
        / INITIAL_BIN_COUNT / HIGH_SCORE_HIGH_RISK
合箱配置：MIN/MAX/TARGET_FINAL_BIN_COUNT / PRIMARY_RATE_COL / 单箱约束
        / 单调与相邻差异控制
        / PROTECTED_BOUNDARIES / SELECTED_FINAL_BIN_RANGES
极端箱配置：PROTECT_EXTREME_INITIAL_BINS / BEST/WORST_EXTREME_INITIAL_BIN_COUNT
        / ALLOW_EXTREME_BIN_MERGE_FOR_HARD_CONSTRAINTS / EXTREME_BOUNDARIES
        / EXTREME_BOUNDARY_PENALTY / EXTREME_BOUNDARY_VIOLATION_PENALTY
        / MIN_EXTREME_BIN_MATURE_COUNT / MIN_BEST/WORST_EXTREME_BIN_*_COUNT
评分配置：PROTECTED_BOUNDARY_PENALTY / MERGE_COST_RATE_GAP_WEIGHT
        / MERGE_COST_IV_LOSS_WEIGHT / IV_RETENTION_SCORE_CAP / PSI_EPS / IV_SMOOTHING_EPS
候选评分权重：CANDIDATE_SCORE_WEIGHTS 全部键值（hard_constraints_ok 等）
策略配置：自动通过与总接纳的累计/边际风险上限
```

#### 表 2：上线执行规则

底层对象：

```text
online_execution_rules
```

供引擎团队上线时逐项核对的静态清单，覆盖：

```text
分数精度：模型分与边界的精度要求、不二次取整
阈值取整：默认原始精度部署；工程必须取整时只允许向更严方向（floor）+ 取整后复核
区间开闭：(left, right] 分档规则、边界相等归入右闭档、数值比较禁止字符串比较
空值与异常值：缺失分按拒绝、NaN/Inf 不入自动通过、超界分数按极端箱兜底
一致性校验：阈值清单逐项核对、离线/线上分档对照一致率 100%、上线后分档占比监控
```

#### 表 3：指标说明

底层对象：

```text
metric_dictionary
```

核心字段的名称和计算口径说明（含 `*_cnt_bad_rate_ci_low / ci_high` 置信区间字段）。

---

## 六、当前代码中已定义但未实际输出的内容

当前脚本没有定义未使用的分析功能。历史版本中的以下内容已移除：

- `score_apply` 和交易子模型表的读取与拼接。
- 3/4 位小数边界取整敏感性分析。
- 阈值敏感性全矩阵扫描（人工审核产能 × 风险上限矩阵）；当前仅输出自动通过 / 总接纳阈值收严、放松一档的敏感性表（见 Sheet 4 表 3），未做全矩阵扫描。
- 三套策略方案（保守/平衡/增长）对比。

> 说明：多档位候选的横向比较并未移除——当前通过“候选生成 + 候选评分”在同一流程内比较 8/7/6 档候选并选出最优方案（见 6.5 / 6.6），只是不再像历史版本那样把每个档位作为独立完整方案并列对比。

历史实际审批漏斗已恢复，并与 `score_mlt` 阈值下的模型策略测算流量分开输出。当前报告包含一套自动合箱、一套默认模型策略，以及历史实际审批表现对照。

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
TRAIN_INVERSION_TOLERANCE
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
- Train / OOT 样本量。
- 实际初始箱数量（日志 `2/9`）。
- 自动合箱结果（日志 `3/9`，档位数和方案）。
- 是否出现“无满足约束的阈值”或报错。

### 第 6 步：按顺序查看 Excel

1. `01_总览`：看最终合箱方案、PSI、单调性、AUC/KS 和三段规则。
2. `02_分箱详情`：理解 20 箱如何合并为最终档。
3. `03_最终分箱统计`：确认 Train / OOT 的最终箱风险梯度。
4. `04_策略方案`：确认阈值选择过程和三段占比。
5. `05_模型验证`：确认 OOT、PSI、月度稳定性。
6. `06_附录`：核对配置参数与指标口径。

### 第 7 步：上线前核对

至少确认：

```text
模型版本
模型分方向
Train / OOT 时间范围
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

当前版本不再使用固定合箱方案，最终档位（6~8 档）和合箱范围由完整 Train 自动决定。更换数据或时间范围后：

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

`candidate_score` 的各权重（倒挂惩罚、单箱约束惩罚、IV 保留率和相邻风险差距加分等）是代码中的经验值，用于在多个可行方案中选择单调性、统计充分性和区分度更好的方案。它们只影响候选排序，不影响硬约束（档位数、单调性、单箱规模、极端边界跨越）的判定。

### 8. 极端箱保护会限制合箱自由度

`PROTECT_EXTREME_INITIAL_BINS=True`（默认）时，最好/最坏各 1 个初始箱的边界默认被硬禁止跨越，任何合箱阶段都不能跨过。代价是合箱自由度受限：

- 如果极端箱本身样本量很小，硬保护可能让该档位区间异常窄。
- 数据或时间范围变化后，极端箱边界会随初始等频分箱移动，最终方案可能随之变化。
- 若最终没有任何候选满足全部硬约束（含极端边界跨越数 = 0），脚本会退化为忽略硬约束、直接按综合得分取第一名，需在 `02_分箱详情` 的合箱候选评分表中确认该方案的可解释性。

---

## 九、一句话总结

> **当前脚本在完整 Train 上将 `score_mlt` 等频切成 20 箱，结合样本量、成熟度和风险倒挂自动合箱为 6~8 个风险等级（目标 7 档），并在 OOT 上进行独立验证；随后在风险约束下测算自动通过、人工审核、总接纳和拒绝流量，同时基于 `application_info` 计算历史实际审批漏斗，最终将两套口径分开输出到 6 个 sheet 的 Excel 策略报告。**
