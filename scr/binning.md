# 风险模型分箱脚本处理逻辑详解

> 对应方法论文档：`binning.md`  
> 对应脚本路径：`scr/binning.py`  
> 本文目标：详细说明该分箱脚本从数据加载、样本划分、初始分箱、统计检验、ChiMerge 合并、OOT 验证、阈值测算、策略方案、转化漏斗、FPD7 对比到结果输出的全部处理逻辑。

---

## 1. 文档适用范围与说明

当前提供的文件是分箱方法论文档，并未包含 `scr/binning.py` 的完整 Python 源代码。因此，本文分为两类内容：

1. **明确逻辑**：原方法论文档中已经明确描述的处理规则、参数、公式和输出。
2. **实现解释**：为了帮助理解代码运行过程，对这些规则展开成接近代码执行顺序的说明。

对于下列源码级细节，当前文档无法完全确认，本文会明确标注：

- CSV 的实际读取参数，例如编码、分隔符、低内存模式；
- 是否在关联前做去重；
- 缺失分数、非法标签和异常本金的具体处理方式；
- ChiMerge 多个合并条件同时满足时的准确优先级；
- 三套策略方案在箱数不能整除时采用 `floor`、`ceil` 还是四舍五入；
- AUC、KS、卡方检验和 Z 检验所使用的具体库函数；
- Markdown 表格和图表的具体生成函数。

因此，本文可以作为代码逻辑说明书和代码评审清单，但若要做到逐行解释，仍需要同时提供 `scr/binning.py`。

---

# 2. 脚本整体目标

该脚本并不是单纯把模型分切成若干区间，而是完成一套从模型评估到策略阈值设计的完整流程。

核心目标可以概括为：

> 在策略调优样本上先进行等频细分箱，再利用相邻箱统计差异、风险单调性、样本量和坏样本量进行 ChiMerge 合并；随后将固定后的切点应用到 OOT 样本，验证风险排序能力和跨期稳定性；最终输出累计通过率与坏账率关系、保守/平衡/增长三套策略方案、审批转化漏斗以及 FPD7 早期风险表现。

完整链路为：

```text
读取模型分表
    +
读取申请信息表
    ↓ application_id 内连接
形成建模分析宽表
    ↓
生成或读取主标签、FPD7 标签及业务字段
    ↓ sample_datetime 切分
策略调优集 / OOT 集
    ↓ 排除主标签未成熟样本
调优有效集 / OOT 有效集
    ↓
调优集等频 20 箱
    ↓
计算基础指标、Lift、WOE、IV、累计指标、单调性
    ↓
相邻箱卡方检验 + 坏账率 Z 检验
    ↓
ChiMerge 动态合并
    ↓
形成最终风险等级和固定分数切点
    ↓
应用到 OOT 集，计算 PSI、IV、单调性
    ↓
累计阈值测算
    ↓
保守 / 平衡 / 增长三套策略
    ↓
全量申请转化漏斗
    ↓
FPD7 与 3M30 主标签对比
    ↓
输出 binning_result.md
```

---

# 3. 输入数据与字段依赖

## 3.1 数据文件

| 文件 | 主要用途 |
|---|---|
| `res/aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv` | 提供每笔申请的模型分、`application_id` 和 `sample_datetime` |
| `res/application_info.csv` | 提供风险标签、申请状态、本金、首期还款表现及其他业务字段 |

两张表通过 `application_id` 关联。

## 3.2 最低字段要求

根据当前方法论，脚本至少依赖以下字段。

### 模型分表

| 字段 | 用途 | 预期类型 |
|---|---|---|
| `application_id` | 两表关联主键 | 字符串或整数，但两表类型必须一致 |
| `sample_datetime` | 策略调优集与 OOT 集的时间切分 | 日期时间 |
| `aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score` | 模型分 | 数值型 |

### 申请信息表

| 字段 | 用途 | 预期类型 |
|---|---|---|
| `application_id` | 两表关联主键 | 与模型分表一致 |
| `duedate_3m_30` | 主风险标签 | 0/1，允许 NULL |
| `principal` | 金额加权累计坏账率 | 非负数值 |
| `application_status` | 审批漏斗和 FPD7 有效样本判断 | 字符串 |
| `first_payment_scheduled_date` | 判断首期是否已经到期满 7 天 | 日期 |
| `first_payment_days_past_due_ever` | 生成 FPD7 标签 | 数值型 |

## 3.3 推荐但当前文档未明确说明的数据检查

正式运行前建议检查：

1. `application_id` 在模型分表是否唯一；
2. `application_id` 在申请信息表是否唯一；
3. 两表主键类型是否一致；
4. 模型分能否转换为数值；
5. 主标签是否只包含 `0`、`1` 和 NULL；
6. `sample_datetime` 是否能正常解析；
7. `principal` 是否存在负数、0、NULL 或极端异常值；
8. 状态字段是否存在大小写、前后空格或新状态枚举；
9. 同一申请是否存在多次模型评分记录。

如果任意一张表中 `application_id` 不唯一，直接 inner join 可能产生多对多扩张。例如模型分表有 2 条、申请表有 3 条同一申请，关联后会产生 6 条记录，从而导致样本量、坏样本量和通过率全部被重复计算。

---

# 4. 参数配置及其影响

| 参数 | 默认值 | 处理逻辑 | 影响范围 |
|---|---:|---|---|
| `LABEL_COL` | `duedate_3m_30` | 指定主风险标签 | 分箱坏账率、WOE、IV、AUC、KS、策略测算 |
| `SCORE_COL` | `aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score` | 指定模型分字段 | 所有分箱和阈值计算 |
| `SCORE_HIGHER_IS_RISKIER` | `True` | 指定分数方向 | 风险排序、累计通过方向、策略区间方向 |
| `N_BINS` | `20` | 初始等频目标箱数 | 初始细分粒度 |
| `OOT_CUT_DATE` | `2025-10-21` | OOT 时间切点 | 调优集/OOT 集划分 |
| `FPD7_REF_DATE` | `2026-07-20` | FPD7 成熟度参考日 | FPD7 标签有效范围 |
| `CHIMERGE_MIN_BINS` | `6` | 最少保留风险箱数 | 防止过度合并 |
| `CHIMERGE_MAX_BINS` | `10` | 目标最大风险箱数 | 控制最终等级复杂度 |
| `CHIMERGE_P_THRESHOLD` | `0.05` | 相邻箱差异显著性阈值 | 判断是否可因差异不显著而合并 |
| `MIN_BIN_SIZE` | `3000` | 单箱最低样本量 | 低样本箱合并条件 |
| `MIN_BAD_COUNT` | `100` | 单箱最低坏样本量 | 统计稳定性合并条件 |

## 4.1 分数方向

`SCORE_HIGHER_IS_RISKIER` 是整个脚本最重要的方向性参数。

### 当 `SCORE_HIGHER_IS_RISKIER = True`

- 分数越低，风险越低；
- 分数越高，风险越高；
- 自动通过通常从低分开始累计；
- 阈值通过条件为：

```python
score <= threshold
```

- 风险等级应按分数从低到高排列；
- 累计通过率、累计坏账率从最低风险人群开始累加；
- 合并箱边界作为策略阈值时通常使用箱上限。

### 当 `SCORE_HIGHER_IS_RISKIER = False`

- 分数越高，风险越低；
- 分数越低，风险越高；
- 自动通过通常从高分开始累计；
- 阈值通过条件为：

```python
score >= threshold
```

- 风险等级应按分数从高到低排列；
- 累计通过率、累计坏账率从最高分开始累加；
- 合并箱边界作为策略阈值时通常使用箱下限。

任何一处未根据该参数反转，都可能导致：

- 低风险和高风险等级颠倒；
- 累计通过率方向错误；
- AUC 低于 0.5；
- 保守方案反而通过高风险人群；
- 阈值含义与线上规则相反。

---

# 5. 详细处理逻辑

## 5.1 第一步：加载数据

脚本首先读取模型分文件和申请信息文件。

逻辑上相当于：

```python
score_df = pd.read_csv(score_file)
application_df = pd.read_csv(application_file)
```

随后应完成基础类型转换：

```python
score_df[SCORE_COL] = pd.to_numeric(score_df[SCORE_COL], errors="coerce")
score_df["sample_datetime"] = pd.to_datetime(
    score_df["sample_datetime"], errors="coerce"
)
application_df[LABEL_COL] = pd.to_numeric(
    application_df[LABEL_COL], errors="coerce"
)
```

当前方法论文档未说明是否明确执行这些转换，但后续时间比较、分箱和标签计算都要求相应字段类型正确。

### 该步骤的输出

- `score_df`：模型分原始表；
- `application_df`：申请信息原始表。

### 关键异常

- 文件不存在；
- 字段名不一致；
- 日期无法解析；
- 分数字段全部为空；
- 标签包含非 0/1 值；
- 主键存在重复。

---

## 5.2 第二步：通过 `application_id` 做 inner join

两张表采用内连接：

```python
merged = score_df.merge(
    application_df,
    on="application_id",
    how="inner"
)
```

### inner join 的业务含义

只有同时存在模型分和申请信息的申请才会进入后续分析。

因此会排除：

1. 有模型分但申请信息表中找不到的申请；
2. 有申请信息但没有模型分的申请。

### 为什么使用 inner join

后续分箱必须同时具备：

- 模型分；
- 样本时间；
- 主风险标签；
- 必要业务字段。

缺少任一部分都无法参与完整分析。

### 必须关注的样本损失

建议输出以下核对数据：

```text
模型分表申请数
申请信息表申请数
成功关联申请数
模型分表未匹配数
申请信息表未匹配数
关联率
```

若 inner join 后样本下降明显，应先排查主键格式和数据覆盖，而不是直接继续分箱。

---

## 5.3 第三步：生成 FPD7 标签

方法论文档在后半部分描述 FPD7，但从代码执行依赖看，FPD7 标签通常会在宽表形成后统一生成。

### 5.3.1 FPD7 有效样本条件

申请必须同时满足：

```text
application_status = '4.Funded'
并且
first_payment_scheduled_date < FPD7_REF_DATE - 7 天
```

以默认参考日期 `2026-07-20` 为例：

```text
FPD7_REF_DATE - 7 天 = 2026-07-13
```

因此有效条件是：

```text
first_payment_scheduled_date < 2026-07-13
```

注意这里是严格小于 `<`，不是小于等于 `<=`。首期应还日在 `2026-07-13` 的申请不会进入有效样本。

### 5.3.2 标签赋值

在有效样本中：

```text
first_payment_days_past_due_ever > 7  → fpd7_flag = 1
first_payment_days_past_due_ever <= 7 → fpd7_flag = 0
```

其他记录：

```text
fpd7_flag = NULL
```

等价伪代码：

```python
mature_mask = (
    (merged["application_status"] == "4.Funded")
    & (
        merged["first_payment_scheduled_date"]
        < FPD7_REF_DATE - pd.Timedelta(days=7)
    )
)

merged["fpd7_flag"] = np.nan
merged.loc[
    mature_mask & (merged["first_payment_days_past_due_ever"] > 7),
    "fpd7_flag",
] = 1
merged.loc[
    mature_mask & (merged["first_payment_days_past_due_ever"] <= 7),
    "fpd7_flag",
] = 0
```

### 5.3.3 标签含义

- `1`：首期曾经超过 7 天逾期；
- `0`：首期最大逾期天数不超过 7 天；
- NULL：未放款、首期尚未充分成熟、日期缺失或逾期字段缺失。

FPD7 是较早期风险信号，成熟时间短于 3M30，但覆盖面仅限已放款且首期成熟订单，因此不能直接替代主标签。

---

## 5.4 第四步：按时间划分策略调优集和 OOT 集

使用模型分表自带的 `sample_datetime` 进行切分。

### 切分规则

```python
tuning = merged[merged["sample_datetime"] < OOT_CUT_DATE]
oot = merged[merged["sample_datetime"] >= OOT_CUT_DATE]
```

默认切点为 `2025-10-21`：

- `sample_datetime < 2025-10-21`：策略调优集；
- `sample_datetime >= 2025-10-21`：OOT 集。

### 为什么按时间切分

随机切分只能验证同一时间分布下的泛化能力，而 OOT 用未来时段的数据验证：

- 模型排序能力是否跨期保持；
- 分箱坏账率是否稳定；
- 分数分布是否漂移；
- 策略阈值在未来样本上是否仍有效。

### 日期缺失的处理

若 `sample_datetime` 为 NULL，则：

- 不满足 `< OOT_CUT_DATE`；
- 也不满足 `>= OOT_CUT_DATE`；
- 因而不会进入调优集或 OOT 集。

建议单独统计日期缺失样本数，避免无声丢失。

---

## 5.5 第五步：排除主标签未成熟样本

在调优集和 OOT 集中，分别排除主标签为 NULL 的记录：

```python
tuning_valid = tuning[tuning[LABEL_COL].notna()].copy
oot_valid = oot[oot[LABEL_COL].notna()].copy()
```

### 逻辑含义

主标签为 NULL 的申请通常表示：

- 订单尚未达到标签观察期；
- 未放款；
- 标签生成条件不满足；
- 数据缺失。

这些样本不能参与坏账率、WOE、IV、卡方、AUC 和 KS 的监督分析。

### 仍可用于哪些分析

虽然主标签为空的样本不能参与风险表现计算，但在某些情况下仍可用于：

- 全量申请转化率漏斗；
- 分数覆盖率检查；
- 线上申请分数分布监控。

当前方法论明确说明，转化率漏斗使用全量申请，而不是仅使用有效标签样本。

---

## 5.6 第六步：策略调优集等频 20 箱初分

使用：

```python
pd.qcut(
    tuning_valid[SCORE_COL],
    q=N_BINS,
    duplicates="drop"
)
```

### 5.6.1 等频分箱的目标

目标是将调优有效样本按分数排序后，尽量平均分成 20 组，使每箱样本量接近：

\[
\frac{N}{20}
\]

这种方法适合初始探索，因为各箱样本量相对均衡，坏账率估计通常比等距分箱更稳定。

### 5.6.2 实际箱数可能少于 20

`duplicates="drop"` 表示当多个分位点对应同一个分数值时，自动删除重复切点。

例如：

```text
理论 5% 分位点 = 0.137
理论 10% 分位点 = 0.137
```

两个分位点相同，无法形成有效区间，因此会合并，最终实际箱数可能是 19、18，甚至更少。

### 5.6.3 同分值不会被强行拆开

相同模型分的样本应该进入同一个箱。否则线上仅依据分数无法复现分箱结果。

### 5.6.4 缺失分数

`pd.qcut` 不会正常给缺失分数分配区间。建议在分箱前明确：

```python
score_valid = tuning_valid[SCORE_COL].notna()
```

缺失分数应：

- 从模型分箱分析中排除；
- 单独统计覆盖率；
- 如线上存在缺失分，应设置单独的缺失分规则，而不是默认为低风险或高风险。

### 5.6.5 初始箱的区间边界

`qcut` 通常生成类似：

```text
(0.102, 0.168]
(0.168, 0.204]
...
```

需要特别注意左右开闭规则。后续应用到 OOT 时，应使用完全一致的区间规则，避免边界分数落入不同风险等级。

---

## 5.7 第七步：计算初始分箱基础指标

对每一个初始箱进行聚合。

设第 \(i\) 箱：

- 样本数为 \(n_i\)；
- 坏样本数为 \(B_i\)；
- 好样本数为 \(G_i\)。

### 5.7.1 样本数

\[
n_i = \operatorname{COUNT}(i)
\]

表示该箱内标签有效的申请数。

### 5.7.2 坏样本数

\[
B_i = \sum y
\]

其中：

- `y = 1` 为坏样本；
- `y = 0` 为好样本。

### 5.7.3 好样本数

\[
G_i = n_i - B_i
\]

### 5.7.4 箱内坏账率

\[
r_i = \frac{B_i}{n_i}
\]

坏账率是判断风险排序最直接的指标。

若分数越高风险越高，理想情况是：

```text
箱 1 坏账率 < 箱 2 坏账率 < ... < 箱 K 坏账率
```

### 5.7.5 坏账率标准误

\[
SE_i = \sqrt{\frac{r_i(1-r_i)}{n_i}}
\]

标准误衡量该箱坏账率估计的不确定性：

- 样本越多，标准误通常越小；
- 坏账率接近 0.5 时标准误较大；
- 样本太少时，坏账率即使看起来差异很大，也可能不稳定。

可进一步形成近似 95% 置信区间：

\[
r_i \pm 1.96 \times SE_i
\]

当前方法论文档仅要求计算 `SE`，未明确是否输出置信区间。

### 5.7.6 整体坏账率

\[
r_{all} = \frac{\sum_i B_i}{\sum_i n_i}
\]

用于计算 Lift 和判断各箱相对风险水平。

---

## 5.8 第八步：计算累计指标

累计指标必须先将风险箱按“低风险 → 高风险”排列。

### 分数越高风险越高

按分数区间从低到高排序并累计。

### 分数越高风险越低

应按分数区间从高到低排序并累计。

设累计到第 \(k\) 个低风险箱：

### 5.8.1 累计样本数

\[
CumN_k = \sum_{i=1}^{k}n_i
\]

### 5.8.2 累计坏样本数

\[
CumB_k = \sum_{i=1}^{k}B_i
\]

### 5.8.3 累计通过率

\[
CumPassRate_k = \frac{CumN_k}{N}
\]

含义：若将第 \(k\) 箱的边界作为自动通过阈值，理论上有多少比例的有效样本可以通过。

### 5.8.4 累计坏账率

\[
CumBadRate_k = \frac{CumB_k}{CumN_k}
\]

含义：截至当前阈值，所有通过客群的整体坏账率。

### 5.8.5 累计逻辑的业务解释

随着阈值逐步放宽：

- 通过率通常上升；
- 纳入的边际客群风险通常上升；
- 累计坏账率通常也会上升；
- 业务需要在增长和风险之间做权衡。

---

## 5.9 第九步：计算 Lift 和累计 Lift

### 5.9.1 单箱 Lift

\[
Lift_i = \frac{r_i}{r_{all}}
\]

解释：

- `Lift = 1`：该箱坏账率等于总体平均；
- `Lift > 1`：该箱风险高于总体平均；
- `Lift < 1`：该箱风险低于总体平均。

例如：

```text
总体坏账率 = 10%
某箱坏账率 = 20%
Lift = 20% / 10% = 2
```

表示该箱坏样本浓度为总体平均的 2 倍。

### 5.9.2 累计 Lift

\[
CumLift_k = \frac{CumBadRate_k}{r_{all}}
\]

用于衡量截至某一通过阈值，累计通过客群相对总体风险水平。

低风险累计区间理想情况下应满足：

```text
CumLift < 1
```

原方法论文档中“1 表示与平均水平一致；>1……”存在一个文字缺失，准确表达应是：

```text
= 1 表示与平均一致；> 1 表示高于平均；< 1 表示低于平均。
```

---

## 5.10 第十步：计算 WOE

### 5.10.1 好坏样本占比

设全部箱的坏样本总数：

\[
B_{total} = \sum_i B_i
\]

全部箱的好样本总数：

\[
G_{total} = \sum_i G_i
\]

第 \(i\) 箱坏样本占全部坏样本的比例：

\[
B\_pct_i = \frac{B_i}{B_{total}}
\]

第 \(i\) 箱好样本占全部好样本的比例：

\[
G\_pct_i = \frac{G_i}{G_{total}}
\]

### 5.10.2 WOE 公式

当前方法论使用：

\[
WOE_i = \ln\left(\frac{B\_pct_i}{G\_pct_i}\right)
\]

按照这一公式：

- `WOE > 0`：该箱坏样本相对更集中，风险偏高；
- `WOE < 0`：该箱好样本相对更集中，风险偏低；
- `WOE ≈ 0`：该箱好坏分布接近总体。

注意，部分评分卡资料使用相反定义：

\[
\ln(G\_pct/B\_pct)
\]

两种定义仅符号相反。脚本和文档必须统一，不能混用。

### 5.10.3 零占比处理

当前规则是：

```text
若 B_pct = 0 或 G_pct = 0，则 WOE = 0
```

这个处理可以避免：

- 除以 0；
- `ln(0)`；
- 正负无穷。

但统计含义并不理想，因为“全好箱”或“全坏箱”本应具有极强区分度，直接填 0 会将其表示为没有区分度。

更常见做法是平滑，例如：

\[
B_i^* = B_i + 0.5, \quad G_i^* = G_i + 0.5
\]

不过，为准确描述当前脚本逻辑，本文仍以“零占比时填 0”为准。

---

## 5.11 第十一步：计算 IV

### 5.11.1 单箱 IV 分量

\[
IV_i = (B\_pct_i - G\_pct_i) \times WOE_i
\]

### 5.11.2 总 IV

\[
IV = \sum_i IV_i
\]

### 5.11.3 常见解释

| IV 范围 | 常见解释 |
|---:|---|
| `< 0.02` | 几乎没有区分能力 |
| `0.02 ~ 0.10` | 较弱 |
| `0.10 ~ 0.30` | 中等 |
| `0.30 ~ 0.50` | 较强 |
| `> 0.50` | 极强，需要同时排查过拟合或数据泄漏 |

### 5.11.4 在本脚本中的作用

这里的 IV 不是用于筛选单个特征，而是用于描述“模型分经过当前分箱后”的好坏样本分离程度。

比较初始分箱和合并后分箱的 IV，可以判断合并是否损失过多区分能力。

通常：

- 合并后 IV 略微下降是正常的；
- 若下降很大，可能存在过度合并；
- 若 OOT IV 明显低于调优集，可能存在跨期衰减。

---

## 5.12 第十二步：检查坏账率单调性

使用 Spearman 秩相关系数衡量风险箱序号和箱内坏账率之间的单调关系。

设：

- 风险箱顺序为 \(1,2,...,K\)；
- 各箱坏账率为 \(r_1,r_2,...,r_K\)。

计算：

\[
\rho = \operatorname{Spearman}(BinOrder, BadRate)
\]

### 5.12.1 解释

若风险等级已经按低风险到高风险排序：

- `ρ` 接近 `1`：坏账率随风险等级稳定上升；
- `ρ` 接近 `-1`：风险顺序很可能反了；
- `ρ` 接近 `0`：坏账率没有明显单调关系。

当前规则使用：

```text
|ρ| < 0.9 时提示可能存在局部倒挂
```

之所以使用绝对值，可能是为了兼容不同分数方向；但更稳妥的做法是先统一按低风险到高风险排序，此时理论期望应明确为 `ρ > 0.9`，而不是只看绝对值。

### 5.12.2 局部倒挂

局部倒挂指相邻风险箱的坏账率没有按照预期方向变化。

低风险到高风险排序后，如果：

\[
r_{i+1} < r_i
\]

则第 \(i\) 箱和第 \(i+1\) 箱发生倒挂。

ChiMerge 会将倒挂作为可合并条件之一。

---

## 5.13 第十三步：相邻箱卡方检验

初始分箱完成后，对每一对相邻箱进行好坏分布差异检验。

例如相邻箱 A 和 B：

| 箱 | 坏样本 | 好样本 |
|---|---:|---:|
| A | \(B_A\) | \(G_A\) |
| B | \(B_B\) | \(G_B\) |

构建 2×2 列联表：

```python
contingency = [
    [B_A, G_A],
    [B_B, G_B],
]
```

卡方统计量为：

\[
\chi^2 = \sum_{i=1}^{2}\sum_{j=1}^{2}
\frac{(O_{ij}-E_{ij})^2}{E_{ij}}
\]

### 5.13.1 原假设

```text
H0：相邻两箱的好坏样本分布没有显著差异。
```

### 5.13.2 p 值解释

- `p >= 0.05`：没有足够证据认为两箱风险不同，适合合并；
- `p < 0.05`：两箱风险差异显著，通常不因“差异不显著”而合并。

### 5.13.3 ChiMerge 中的使用方式

每轮计算所有相邻箱的 p 值，优先选择 p 值最大的一对。

原因是：

```text
p 值越大 → 两箱越相似 → 合并造成的信息损失通常越小
```

### 5.13.4 小样本问题

若某些单元格期望频数过小，卡方近似可能不稳定。当前方法论通过 `MIN_BIN_SIZE` 和 `MIN_BAD_COUNT` 尽量避免这种情况，但未明确是否使用 Fisher 精确检验作为替代。

---

## 5.14 第十四步：相邻箱坏账率 Z 检验

Z 检验用于直接比较相邻两箱坏账率之差。

设：

\[
r_A = \frac{B_A}{n_A}, \quad r_B = \frac{B_B}{n_B}
\]

当前方法论给出的统计量为：

\[
z = \frac{r_B-r_A}
{\sqrt{\frac{r_A(1-r_A)}{n_A}+\frac{r_B(1-r_B)}{n_B}}}
\]

### 5.14.1 解释

- `z > 0`：B 箱坏账率高于 A 箱；
- `z < 0`：B 箱坏账率低于 A 箱，可能发生倒挂；
- `|z|` 较小：两箱坏账率差异相对不明显；
- `|z|` 较大：两箱坏账率差异较明显。

若同时计算双侧 p 值：

```text
p = 2 × [1 - Φ(|z|)]
```

### 5.14.2 与卡方检验的关系

在两个比例比较场景中，卡方检验和 Z 检验通常会给出相近结论，但输出 Z 检验具有额外价值：

- 可以看到差异方向；
- 可以直接识别倒挂；
- 可以辅助业务解释相邻箱风险差距。

当前文档未说明 Z 检验是否参与自动合并决策。明确参与自动合并的主要是卡方 p 值、倒挂、样本量和坏样本量；Z 检验更可能用于报告展示和人工复核。

---

# 6. ChiMerge 动态合并逻辑

## 6.1 合并目标

初始 20 箱粒度较细，可能存在：

- 相邻箱坏账率差异不显著；
- 局部坏账率倒挂；
- 单箱样本量不足；
- 单箱坏样本数不足；
- 风险等级过多，不便于策略使用。

ChiMerge 的目标不是机械合并到固定 6 箱，而是在风险区分能力、统计稳定性、单调性和业务可操作性之间取得平衡。

## 6.2 每轮合并前重新计算

每完成一次合并，都需要重新计算：

1. 当前所有箱的样本数；
2. 坏样本数；
3. 好样本数；
4. 坏账率；
5. 所有相邻箱的卡方 p 值；
6. 局部倒挂情况；
7. 低样本箱和低坏样本箱。

不能一直使用初始 20 箱的检验结果，因为合并后箱统计量已经变化。

## 6.3 可触发继续合并的条件

当前方法论包含四类继续合并条件。

### 条件 A：箱数超过最大上限

```text
current_bin_count > CHIMERGE_MAX_BINS
```

默认最大箱数为 10。

即使相邻箱差异都显著，只要当前仍超过 10 箱，脚本也会继续合并，以避免最终风险等级过碎。

### 条件 B：相邻箱差异不显著

```text
adjacent_p_value >= CHIMERGE_P_THRESHOLD
```

默认阈值为 0.05。

表示至少存在一对相邻箱的好坏分布没有显著差异，可以考虑合并。

### 条件 C：存在局部倒挂

按低风险到高风险排序后：

```text
next_bad_rate < current_bad_rate
```

倒挂表示当前风险等级不能形成稳定排序，应优先通过合并消除局部噪声。

### 条件 D：单箱样本不充分

任一箱满足：

```text
n < MIN_BIN_SIZE
或
B < MIN_BAD_COUNT
```

默认条件：

```text
样本数 < 3000
或
坏样本数 < 100
```

此类箱的坏账率统计稳定性较差，应优先与邻箱合并。

## 6.4 最少箱数约束

```text
current_bin_count > CHIMERGE_MIN_BINS
```

默认最少保留 6 箱。

即使仍存在倒挂、小样本或不显著相邻箱，只要已经达到 6 箱，脚本不应继续下降到 5 箱，否则违反最低箱数约束。

这意味着 `CHIMERGE_MIN_BINS` 是硬约束，而其他条件在到达最少箱数后可能无法继续被修复。

## 6.5 合并对象选择

方法论文档明确：

> 每轮合并 p 值最大且满足合并条件的一对相邻箱。

基础逻辑可写成：

```python
while current_bin_count > CHIMERGE_MIN_BINS:
    adjacent_stats = calculate_adjacent_statistics(current_bins)
    candidate = adjacent_stats.sort_values("chi2_p", ascending=False).iloc[0]

    if should_continue_merging(current_bins, adjacent_stats):
        merge(candidate.left_bin, candidate.right_bin)
    else:
        break
```

但这里存在一个需要结合源码确认的问题：

- 若某个低样本箱不在 p 值最大的一对中，脚本是仍然合并 p 值最大的一对，还是优先合并该低样本箱？
- 若倒挂对的 p 值不是最大，是否优先合并倒挂对？

更加合理的优先级通常是：

1. 先处理低样本或低坏样本箱；
2. 再处理倒挂箱；
3. 再处理差异不显著箱；
4. 若只是因为箱数超过上限，则合并 p 值最大的一对。

但当前文件没有明确写出这一完整优先级，因此不能断言源码一定如此实现。

## 6.6 低样本箱与哪个邻箱合并

一个中间箱通常有左右两个相邻箱。常见选择方式是：

1. 分别计算与左邻箱、右邻箱的卡方 p 值；
2. 选择 p 值更大的邻箱合并；
3. 若位于首箱，只能向右合并；
4. 若位于尾箱，只能向左合并。

该逻辑符合“选择风险分布最相似邻箱”的原则，但当前方法论文档未逐字说明，需结合源码确认。

## 6.7 区间合并方式

假设两个相邻箱：

```text
A = (a, b]
B = (b, c]
```

合并后：

```text
A+B = (a, c]
```

同时聚合：

\[
n_{A+B}=n_A+n_B
\]

\[
B_{A+B}=B_A+B_B
\]

\[
G_{A+B}=G_A+G_B
\]

\[
r_{A+B}=\frac{B_A+B_B}{n_A+n_B}
\]

## 6.8 停止条件

满足以下全部条件时提前停止：

1. 当前箱数不超过 `CHIMERGE_MAX_BINS`；
2. 不存在卡方 p 值不低于阈值的相邻箱；
3. 不存在局部倒挂；
4. 不存在低于最低样本量的箱；
5. 不存在低于最低坏样本量的箱。

伪代码：

```python
need_merge = (
    current_bin_count > CHIMERGE_MAX_BINS
    or has_non_significant_pair
    or has_monotonicity_violation
    or has_small_sample_bin
    or has_small_bad_count_bin
)

can_merge = current_bin_count > CHIMERGE_MIN_BINS

if need_merge and can_merge:
    merge_one_pair()
else:
    stop()
```

## 6.9 合并日志

每一步至少应记录：

| 字段 | 含义 |
|---|---|
| `step` | 第几轮合并 |
| `before_bin_count` | 合并前箱数 |
| `left_bin` | 左箱区间 |
| `right_bin` | 右箱区间 |
| `left_n/right_n` | 合并前样本量 |
| `left_bad_rate/right_bad_rate` | 合并前坏账率 |
| `chi2` | 相邻箱卡方统计量 |
| `p_value` | 卡方 p 值 |
| `merge_reason` | 超过上限、差异不显著、倒挂、低样本或低坏样本 |
| `after_bin` | 合并后新区间 |
| `after_bin_count` | 合并后箱数 |

最终还应记录停止原因，例如：

```text
停止：当前 8 箱，不超过上限 10 箱；相邻箱均显著；无倒挂；所有箱样本量和坏样本量均达标。
```

或者：

```text
停止：已达到最少箱数 6，虽然仍存在局部倒挂，但不能继续合并。
```

---

# 7. 形成最终风险箱和切点

ChiMerge 完成后，需要固定最终区间边界。

假设最终形成 8 箱：

```text
(-inf, c1]
(c1, c2]
(c2, c3]
...
(c7, inf]
```

## 7.1 首尾边界扩展

调优集实际最小分和最大分只代表历史样本范围。线上或 OOT 可能出现更低或更高的分数，因此：

- 首个下界设置为 `-inf`；
- 最后一个上界设置为 `inf`。

这样可以避免超出调优集范围的分数被分配为 NULL。

## 7.2 切点必须固定

OOT 验证时不能重新在 OOT 上做 `qcut`，也不能根据 OOT 坏账率重新合并。

正确方式是：

```text
只在调优集上学习切点
→ 固定切点
→ 原样应用于 OOT
```

否则会产生信息泄漏，OOT 就不再是真正的独立验证。

## 7.3 风险等级编号

建议始终按照低风险到高风险编号：

```text
Risk 1 = 最低风险
Risk K = 最高风险
```

无论原始分数方向如何，都通过排序统一成同一种业务含义。

---

# 8. OOT 跨期验证

## 8.1 将固定切点应用到 OOT

逻辑类似：

```python
oot_valid["risk_bin"] = pd.cut(
    oot_valid[SCORE_COL],
    bins=final_edges,
    include_lowest=True,
    duplicates="drop",
)
```

这里不能使用 OOT 自身分位点。

## 8.2 OOT 重新计算分箱指标

使用与调优集一致的逻辑计算：

- `n`；
- `B`；
- `G`；
- `bad_rate`；
- `Lift`；
- `WOE`；
- `IV`；
- 累计通过率；
- 累计坏账率；
- Spearman `ρ`。

## 8.3 跨期比较重点

### 风险排序

观察 OOT 坏账率是否仍随风险等级稳定上升。

### 坏账率绝对变化

某一风险箱 OOT 坏账率可能整体高于调优期，说明宏观环境、渠道或客群发生变化。

### 箱占比分布变化

若大量样本从中低风险箱移动到高风险箱，说明分数分布发生漂移。

### IV 变化

OOT IV 明显下降，说明模型分的区分能力可能衰减。

### Spearman 变化

若调优期单调但 OOT 倒挂，说明风险排序跨期稳定性不足。

---

# 9. PSI 稳定性计算

## 9.1 PSI 公式

设第 \(i\) 箱：

- 调优集样本占比为 \(e_i\)，作为 Expected；
- OOT 样本占比为 \(a_i\)，作为 Actual。

则：

\[
PSI = \sum_i(a_i-e_i)\ln\left(\frac{a_i}{e_i}\right)
\]

## 9.2 单箱 PSI 分量

\[
PSI_i=(a_i-e_i)\ln\left(\frac{a_i}{e_i}\right)
\]

总 PSI 是所有风险箱分量之和。

## 9.3 常见解释

| PSI | 常见判断 |
|---:|---|
| `< 0.10` | 分布相对稳定 |
| `0.10 ~ 0.25` | 存在一定漂移，需要关注 |
| `> 0.25` | 漂移较明显，需要进一步拆解 |

## 9.4 零占比处理

若某箱在调优集或 OOT 占比为 0，直接计算会出现除以 0 或 `ln(0)`。

常见处理是设置极小值，例如：

```python
EPS = 1e-6
expected_pct = max(expected_pct, EPS)
actual_pct = max(actual_pct, EPS)
```

当前方法论文档未说明实际平滑值，需要结合源码确认。

## 9.5 PSI 的含义边界

PSI 只反映分数或风险等级分布漂移，不直接代表模型坏账预测失效。

可能出现：

- PSI 高，但风险排序仍然稳定；
- PSI 低，但每箱坏账率明显上升；
- PSI 低，但某个重要渠道内部漂移严重。

因此 PSI 必须与 OOT 坏账率、IV、AUC、KS 和单调性共同判断。

---

# 10. 累计阈值测算

## 10.1 候选阈值来源

候选阈值由两部分组成：

1. 模型分的 20 个等分位点；
2. ChiMerge 后的最终风险箱边界。

之后应：

- 合并候选集合；
- 删除重复阈值；
- 删除 NULL；
- 根据分数方向排序。

伪代码：

```python
candidate_thresholds = sorted(
    set(quantile_thresholds) | set(final_bin_boundaries)
)
```

## 10.2 每个阈值的通过样本

### 分数越高风险越高

```python
pass_mask = score <= threshold
```

### 分数越高风险越低

```python
pass_mask = score >= threshold
```

## 10.3 累计通过率

\[
PassRate(t)=\frac{N(score\ passes\ t)}{N_{all}}
\]

这里的分母通常是调优有效集总样本数。

## 10.4 累计坏账率：笔数口径

\[
CumBadRate_{count}(t)=
\frac{\sum y\cdot I(score\ passes\ t)}
{\sum I(score\ passes\ t)}
\]

每笔申请等权。

## 10.5 边际坏账率：笔数口径

设前一个阈值为 \(t_{k-1}\)，当前阈值为 \(t_k\)。新增客群是两个阈值通过集合之差。

\[
MarginalN_k=CumN_k-CumN_{k-1}
\]

\[
MarginalB_k=CumB_k-CumB_{k-1}
\]

\[
MarginalBadRate_k=\frac{MarginalB_k}{MarginalN_k}
\]

边际坏账率比累计坏账率更适合判断“是否值得继续放宽阈值”。

例如：

```text
当前累计坏账率：8%
再放宽 5% 通过率后，新增客群坏账率：25%
```

虽然累计坏账率可能只上升到 8.8%，但新增客群风险已经明显偏高。

## 10.6 累计坏账率：金额口径

仅对有本金数据、通常也是已放款样本计算：

\[
CumBadRate_{amount}(t)=
\frac{\sum principal_i\times y_i\times I(score_i\ passes\ t)}
{\sum principal_i\times I(score_i\ passes\ t)}
\]

该指标回答的是：

> 通过客群对应的放款本金中，有多少比例属于坏样本。

与笔数口径的区别：

- 笔数口径：每笔申请权重相同；
- 金额口径：大额贷款权重更高。

## 10.7 金额口径样本范围

方法论文档明确写明“仅含已放款样本”。因此需要避免：

- 未放款申请 `principal` 为空被错误当作 0；
- 审批通过但未放款样本进入金额分母；
- 负数或异常本金影响结果。

## 10.8 风险箱边界标记

阈值曲线中需要标记最终风险箱边界，方便业务将连续阈值与离散风险等级对应。

- 高分高风险：使用每箱上限作为累计通过阈值；
- 高分低风险：使用每箱下限作为累计通过阈值。

---

# 11. 三套策略方案设计

最终风险箱按低风险到高风险排列，并划分为：

1. 自动通过；
2. 人工审核；
3. 拒绝。

方案根据实际最终箱数动态生成，不再强制依赖固定 6 箱。

## 11.1 保守方案

### 自动通过

约最低风险前 1/3 的风险箱。

### 进入通过范围

约前 2/3 的风险箱，其中：

- 最低风险部分自动通过；
- 中间部分人工审核。

### 拒绝

最高风险约后 1/3 的风险箱。

### 目标

- 控制坏账率；
- 提高抗风险能力；
- 接受较低通过率。

## 11.2 平衡方案

### 自动通过

约最低风险前 1/2 的风险箱。

### 人工审核

中间风险箱。

### 拒绝

仅最高风险箱。

### 目标

- 在自动审批率和审核量之间取得平衡；
- 不对中等风险客群直接拒绝；
- 当前方法论将其作为推荐方案。

## 11.3 增长方案

### 自动通过

除最高风险箱外，其余风险箱自动通过。

### 人工审核

最高风险箱。

### 拒绝

不设置硬拒绝。

### 目标

- 最大化通过率；
- 将高风险客群交由人工审核或其他策略判断；
- 适合增长目标更强、风险承受能力更高的阶段。

## 11.4 箱数不能整除时的处理

例如最终有 8 箱：

- 1/3 约等于 2.67；
- 1/2 等于 4；
- 2/3 约等于 5.33。

代码必须决定使用：

- 向下取整；
- 向上取整；
- 四舍五入；
- 或根据累计样本占比而不是箱数切分。

当前方法论只写了“约”，未说明具体取整方式，因此最终边界需要结合源码或输出结果确认。

## 11.5 方案评估指标

每套方案至少应输出：

| 指标 | 含义 |
|---|---|
| 自动通过样本数 | 无需人工审核的申请数 |
| 自动通过率 | 自动通过样本 / 全部有效样本 |
| 人工审核样本数 | 中风险区申请数 |
| 人工审核率 | 审核样本 / 全部有效样本 |
| 拒绝样本数 | 高风险区申请数 |
| 拒绝率 | 拒绝样本 / 全部有效样本 |
| 自动通过坏账率 | 自动通过区风险水平 |
| 自动通过金额坏账率 | 自动通过区本金加权风险 |
| 总通过范围坏账率 | 自动通过 + 审核后可通过范围的风险水平 |

## 11.6 OOT 同步验证

策略区间必须使用调优集确定的固定切点，再应用于 OOT。

不能在 OOT 中重新按“前 1/3 箱”学习新边界。

OOT 重点比较：

- 自动通过率是否变化；
- 自动通过坏账率是否上升；
- 审核区占比是否异常；
- 最高风险箱是否仍然保持最高坏账率；
- 推荐方案在 OOT 是否仍可接受。

---

# 12. 全量申请转化率漏斗

该部分使用全量关联申请，不限制主标签是否成熟，也不只看调优或 OOT。

流程为：

```text
申请
→ 完成
→ 通过
→ 放款
```

## 12.1 申请数 Apply

每一笔进入样本宽表的申请均计为申请。

\[
Apply=N_{all}
\]

## 12.2 完成数 Completed

排除：

```text
0.Incomplete
```

即：

```python
completed_mask = application_status != "0.Incomplete"
```

需注意：若状态为空，`!=` 在不同实现中可能将其视为 True，因此建议显式处理 NULL。

## 12.3 通过数 Approved

状态属于：

```text
3.x 或 4.x
```

这通常表示审批通过和已放款状态。

实际代码可能使用：

```python
status.str.startswith(("3.", "4."))
```

也可能使用明确枚举列表。当前方法论文档未给出完整状态枚举，需要结合源码确认。

## 12.4 放款数 Funded

```text
application_status = '4.Funded'
```

## 12.5 拒绝数 Declined

方法论给出了拒绝率：

\[
DeclineRate=Declined/Completed
\]

但未明确拒绝状态的完整枚举。

可能的实现方式包括：

```text
Completed - Approved
```

或按特定 `2.x` 拒绝状态识别。两种口径可能不同，需要结合源码确认。

## 12.6 漏斗指标

### 完成率

\[
CompletionRate=\frac{Completed}{Apply}
\]

### 通过率

\[
ApprovalRate=\frac{Approved}{Completed}
\]

### 拒绝率

\[
DeclineRate=\frac{Declined}{Completed}
\]

### 放款率

\[
FundingRate=\frac{Funded}{Approved}
\]

### 整体放款率

\[
OverallFundingRate=\frac{Funded}{Apply}
\]

## 12.7 按风险等级拆解

将全量申请使用最终分箱切点映射到风险等级，然后分别计算每个风险等级的漏斗。

目的包括：

- 检查低风险客群是否被过度拒绝；
- 检查高风险客群是否仍有较高通过率；
- 判断审批策略是否与模型风险排序一致；
- 发现低风险客群在绑卡、资料或流程环节的损失。

## 12.8 当前文档中的一个不一致

方法论前文说明最终风险箱不再固定为 6 箱，但转化率漏斗部分写的是：

```text
按 6 个风险箱拆解
```

这可能是旧版本描述残留。

更一致的逻辑应是：

```text
按 ChiMerge 最终形成的实际风险箱数拆解
```

除非源码在漏斗阶段又将最终箱重新映射成固定 6 个业务等级。该点需结合源码确认。

---

# 13. FPD7 与主标签对比

## 13.1 对比目的

主标签 `duedate_3m_30` 需要更长时间成熟，FPD7 更早出现。

通过两者对比，可以判断：

- 模型是否能够提前识别早期还款风险；
- FPD7 是否可以作为监控指标；
- 早期风险排序与中期风险排序是否一致；
- OOT 样本尚未完全成熟时，能否用 FPD7 提供补充信号。

## 13.2 调优集和 OOT 分别过滤 FPD7 有效样本

```python
tuning_fpd7 = tuning[tuning["fpd7_flag"].notna()]
oot_fpd7 = oot[oot["fpd7_flag"].notna()]
```

注意 FPD7 有效样本不等于主标签有效样本，两者覆盖范围不同。

## 13.3 FPD7 整体指标

分别输出：

- 有效样本数；
- 坏样本数；
- FPD7 坏账率；
- AUC；
- KS。

### AUC

AUC 衡量模型分对好坏样本的整体排序能力。

若分数越高风险越高，可以直接用分数预测坏样本。

若分数越高风险越低，则应使用：

```python
-risk_score
```

或将正负类方向做相应调整，否则 AUC 可能低于 0.5。

### KS

KS 是不同阈值下坏样本累计分布与好样本累计分布之间的最大差值：

\[
KS=\max_t|CDF_{bad}(t)-CDF_{good}(t)|
\]

KS 越高，说明模型对好坏样本的分离能力越强。

## 13.4 主标签和 FPD7 的 AUC/KS 对比

建议输出类似：

| 数据集 | 标签 | 有效样本数 | 坏账率 | AUC | KS |
|---|---|---:|---:|---:|---:|
| 调优集 | 3M30 | ... | ... | ... | ... |
| 调优集 | FPD7 | ... | ... | ... | ... |
| OOT | 3M30 | ... | ... | ... | ... |
| OOT | FPD7 | ... | ... | ... | ... |

比较时不能仅看 AUC/KS 数值，还要考虑：

- 标签定义不同；
- 有效样本范围不同；
- 坏样本率不同；
- FPD7 仅覆盖已放款样本，可能存在审批选择偏差。

## 13.5 按最终风险箱对比坏账率

对每个最终风险箱，同时计算：

- 主标签有效样本数；
- 3M30 坏账率；
- FPD7 有效样本数；
- FPD7 坏账率。

目标是判断两个标签是否都呈现一致的风险递增趋势。

例如：

```text
Risk 1：3M30 低，FPD7 低
Risk 2：两者略升
...
Risk K：两者最高
```

若 3M30 单调但 FPD7 倒挂，可能说明：

- FPD7 样本量不足；
- 模型主要识别中长期风险，而不是首期风险；
- 某些风险箱受审批和放款选择影响；
- FPD7 标签口径或日期成熟度需要复核。

---

# 14. 结果文件输出逻辑

结果写入：

```text
res/binning_result.md
```

预计包含以下模块。

## 14.1 关键结论与推荐方案

包括：

- 最终箱数；
- 调优集和 OOT 的主要表现；
- PSI；
- 推荐使用保守、平衡还是增长方案；
- 主要风险提示。

## 14.2 模型表现摘要

比较初始分箱和合并后分箱：

- 箱数；
- IV；
- Spearman `ρ`；
- 是否存在倒挂；
- 最小箱样本量；
- 最小箱坏样本量。

## 14.3 初始等频箱明细

每箱可能包括：

- 箱编号；
- 分数下限和上限；
- `n`；
- `B`；
- `G`；
- 坏账率；
- 标准误；
- Lift；
- WOE；
- IV 分量；
- 累计通过率；
- 累计坏账率。

## 14.4 相邻箱差异检验表

每对相邻箱包括：

- 左箱和右箱；
- 两箱样本量；
- 两箱坏账率；
- 坏账率差；
- 卡方统计量；
- 卡方 p 值；
- Z 值；
- Z 检验 p 值；
- 是否倒挂；
- 是否建议合并。

## 14.5 ChiMerge 合并过程

逐轮输出合并对象、合并原因、合并前后指标和停止原因。

## 14.6 合并后调优集明细

使用最终风险箱展示调优期表现。

## 14.7 合并后 OOT 明细

使用相同边界展示 OOT 表现，并与调优集对齐。

## 14.8 PSI 明细

每箱展示：

- 调优占比；
- OOT 占比；
- 占比变化；
- PSI 分量；
- 总 PSI。

## 14.9 累计阈值表或曲线

展示：

- 候选阈值；
- 累计通过率；
- 累计坏账率；
- 边际坏账率；
- 金额坏账率；
- 是否为最终风险箱边界。

## 14.10 三套策略方案

展示每套方案在调优和 OOT 的：

- 自动通过区间；
- 人工审核区间；
- 拒绝区间；
- 自动通过率；
- 审核率；
- 拒绝率；
- 自动通过坏账率；
- 可能的策略优缺点。

## 14.11 转化率漏斗

展示整体和各风险等级的：

- 申请数；
- 完成数；
- 通过数；
- 拒绝数；
- 放款数；
- 各阶段转化率。

## 14.12 FPD7 标签对比

展示：

- FPD7 整体表现；
- 主标签与 FPD7 AUC/KS 对比；
- 各风险箱的 3M30 和 FPD7 坏账率。

---

# 15. 接近代码实现的完整伪代码

```python
# 1. 配置
LABEL_COL = "duedate_3m_30"
SCORE_COL = "aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score"
SCORE_HIGHER_IS_RISKIER = True
N_BINS = 20
OOT_CUT_DATE = pd.Timestamp("2025-10-21")
FPD7_REF_DATE = pd.Timestamp("2026-07-20")
CHIMERGE_MIN_BINS = 6
CHIMERGE_MAX_BINS = 10
CHIMERGE_P_THRESHOLD = 0.05
MIN_BIN_SIZE = 3000
MIN_BAD_COUNT = 100

# 2. 读取数据
score_df = read_score_file()
application_df = read_application_file()

# 3. 类型转换和字段检查
validate_required_columns(score_df, application_df)
parse_score_and_datetime(score_df)
parse_label_and_business_fields(application_df)

# 4. 两表关联
merged = inner_join_on_application_id(score_df, application_df)

# 5. 生成 FPD7 标签
merged["fpd7_flag"] = build_fpd7_flag(
    status=merged["application_status"],
    scheduled_date=merged["first_payment_scheduled_date"],
    max_dpd=merged["first_payment_days_past_due_ever"],
    ref_date=FPD7_REF_DATE,
)

# 6. 按时间切分
strategy_tuning = merged[merged["sample_datetime"] < OOT_CUT_DATE]
oot = merged[merged["sample_datetime"] >= OOT_CUT_DATE]

# 7. 主标签有效样本
tuning_valid = strategy_tuning[strategy_tuning[LABEL_COL].notna()]
oot_valid = oot[oot[LABEL_COL].notna()]

# 8. 调优集等频初分
initial_bins, initial_edges = qcut_score(
    tuning_valid[SCORE_COL],
    q=N_BINS,
    duplicates="drop",
)

# 9. 初始指标
initial_summary = summarize_bins(
    data=tuning_valid,
    bin_col="initial_bin",
    label_col=LABEL_COL,
    score_direction=SCORE_HIGHER_IS_RISKIER,
)

# 10. 初始箱单调性
initial_spearman = calculate_spearman(initial_summary)
initial_inversions = find_adjacent_inversions(initial_summary)

# 11. 相邻箱统计检验
adjacent_test = calculate_adjacent_chi2_and_z(initial_summary)

# 12. ChiMerge
current_bins = initial_bins
merge_log = []

while number_of_bins(current_bins) > CHIMERGE_MIN_BINS:
    current_summary = summarize_bins(...)
    adjacent_stats = calculate_adjacent_chi2_and_z(current_summary)

    has_too_many_bins = number_of_bins(current_bins) > CHIMERGE_MAX_BINS
    has_non_significant_pair = any(
        adjacent_stats["chi2_p"] >= CHIMERGE_P_THRESHOLD
    )
    has_inversion = detect_monotonicity_violation(current_summary)
    has_small_bin = any(current_summary["n"] < MIN_BIN_SIZE)
    has_small_bad_bin = any(current_summary["B"] < MIN_BAD_COUNT)

    need_merge = (
        has_too_many_bins
        or has_non_significant_pair
        or has_inversion
        or has_small_bin
        or has_small_bad_bin
    )

    if not need_merge:
        stop_reason = "所有停止条件均满足"
        break

    pair_to_merge = select_adjacent_pair(
        current_summary,
        adjacent_stats,
        # 准确优先级需结合源码确认
    )

    current_bins = merge_pair(current_bins, pair_to_merge)
    merge_log.append(record_merge_detail(...))

final_edges = extract_final_edges(current_bins)
final_edges[0] = -np.inf
final_edges[-1] = np.inf

# 13. 调优集最终分箱
strategy_tuning["final_risk_bin"] = apply_fixed_edges(
    strategy_tuning[SCORE_COL], final_edges
)

# 14. OOT 使用相同切点
oot["final_risk_bin"] = apply_fixed_edges(
    oot[SCORE_COL], final_edges
)

# 15. 调优和 OOT 最终指标
tuning_final_summary = summarize_bins(tuning_valid, ...)
oot_final_summary = summarize_bins(oot_valid, ...)

# 16. 稳定性
psi_detail, total_psi = calculate_psi(
    expected=tuning_final_summary["n_pct"],
    actual=oot_final_summary["n_pct"],
)

# 17. 累计阈值
quantile_thresholds = calculate_score_quantiles(tuning_valid[SCORE_COL], 20)
bin_thresholds = extract_strategy_boundaries(final_edges)
candidate_thresholds = deduplicate_and_sort(
    quantile_thresholds + bin_thresholds
)

threshold_table = []
for threshold in candidate_thresholds:
    if SCORE_HIGHER_IS_RISKIER:
        pass_mask = tuning_valid[SCORE_COL] <= threshold
    else:
        pass_mask = tuning_valid[SCORE_COL] >= threshold

    threshold_table.append(
        calculate_count_and_amount_metrics(
            tuning_valid,
            pass_mask,
            label_col=LABEL_COL,
            principal_col="principal",
        )
    )

threshold_table = calculate_marginal_metrics(threshold_table)

# 18. 三套策略
conservative = build_conservative_plan(final_risk_bins)
balanced = build_balanced_plan(final_risk_bins)
growth = build_growth_plan(final_risk_bins)

strategy_results = evaluate_plans(
    plans=[conservative, balanced, growth],
    tuning=tuning_valid,
    oot=oot_valid,
)

# 19. 转化率漏斗
full_data_with_bin = apply_fixed_edges_to_full_data(merged, final_edges)
funnel_summary = calculate_application_funnel_by_risk_bin(
    full_data_with_bin,
    status_col="application_status",
)

# 20. FPD7 对比
tuning_fpd7 = strategy_tuning[strategy_tuning["fpd7_flag"].notna()]
oot_fpd7 = oot[oot["fpd7_flag"].notna()]

fpd7_metrics = calculate_auc_ks_and_bad_rate(
    tuning_fpd7,
    oot_fpd7,
    score_col=SCORE_COL,
    score_direction=SCORE_HIGHER_IS_RISKIER,
)

label_comparison = compare_main_label_and_fpd7(...)
fpd7_bin_comparison = compare_bad_rate_by_final_bin(...)

# 21. 输出 Markdown
write_markdown_report(
    path="res/binning_result.md",
    initial_summary=initial_summary,
    adjacent_test=adjacent_test,
    merge_log=merge_log,
    tuning_final_summary=tuning_final_summary,
    oot_final_summary=oot_final_summary,
    psi_detail=psi_detail,
    threshold_table=threshold_table,
    strategy_results=strategy_results,
    funnel_summary=funnel_summary,
    fpd7_metrics=fpd7_metrics,
)
```

---

# 16. 各类数据集的准确区别

| 数据集 | 时间范围 | 主标签要求 | 主要用途 |
|---|---|---|---|
| `merged` | 全部 | 不要求 | 全量宽表、转化漏斗、FPD7 生成 |
| `tuning` | OOT 切点前 | 不要求 | 调优期全量申请分析 |
| `tuning_valid` | OOT 切点前 | 主标签非空 | 初始分箱、ChiMerge、阈值设计 |
| `oot` | OOT 切点及以后 | 不要求 | OOT 全量申请分析 |
| `oot_valid` | OOT 切点及以后 | 主标签非空 | OOT 风险表现和 PSI 验证 |
| `tuning_fpd7` | OOT 切点前 | FPD7 非空 | 调优期早期风险表现 |
| `oot_fpd7` | OOT 切点及以后 | FPD7 非空 | OOT 早期风险表现 |

必须避免将这些数据集混用。例如：

- 用全量 `tuning` 计算主标签坏账率会引入 NULL；
- 用 `tuning_valid` 计算审批漏斗会丢失未成熟申请；
- 用 `oot_valid` 重新学习分箱切点会产生信息泄漏；
- 用主标签有效样本代替 FPD7 有效样本会改变 FPD7 口径。

---

# 17. 关键边界和异常处理

## 17.1 分数为空

建议：

- 不参与 qcut、ChiMerge 和阈值测算；
- 输出分数缺失率；
- 在线上设置明确的缺失分兜底策略。

## 17.2 标签不是 0/1

若出现 `2`、`-1`、字符串或其他值，`SUM(y)` 将失去坏样本数含义。应在计算前校验。

## 17.3 全部样本为好或全部为坏

此时：

- WOE/IV 无法正常计算；
- AUC/KS 无法计算；
- 卡方检验可能失效。

脚本应给出明确提示，而不是静默输出 0。

## 17.4 某箱没有坏样本或没有好样本

当前 WOE 规则填 0，但会低估区分度。报告中最好增加警告。

## 17.5 qcut 实际箱数过少

若分数离散度低，初始箱数可能已经少于 `CHIMERGE_MIN_BINS`。此时 ChiMerge 无法保留 6 箱，脚本应以实际可形成箱数为准并输出原因。

## 17.6 OOT 某箱无样本

需要：

- 在 OOT 表中仍保留该风险箱；
- 样本数和占比填 0；
- 坏账率可能填 NULL；
- PSI 使用平滑值处理。

不能直接删除空箱，否则调优和 OOT 风险箱无法对齐。

## 17.7 累计阈值下无通过样本

当阈值过严导致通过样本数为 0：

- 通过率为 0；
- 累计坏账率应为 NULL，而不是 0；
- 金额坏账率应为 NULL。

## 17.8 金额分母为 0

若通过样本中没有有效本金，金额坏账率应为 NULL，并注明样本不足。

## 17.9 日期边界

- `sample_datetime = OOT_CUT_DATE`：进入 OOT；
- `first_payment_scheduled_date = FPD7_REF_DATE - 7 天`：根据当前严格 `<` 规则，不进入 FPD7 有效样本；
- 日期字段若包含时分秒，需要确认按时间戳比较还是先取日期。

## 17.10 时区

若 `sample_datetime` 带时区而切点不带时区，Pandas 可能报错。跨地区系统应统一时区后再切分。

---

# 18. 当前逻辑中需要特别关注的事项

## 18.1 WOE 零值处理可能低估区分能力

当前逻辑将 `B_pct = 0` 或 `G_pct = 0` 的 WOE 设为 0。运行不会报错，但统计解释偏弱。建议后续改为平滑方法，并在版本变更中说明 IV 口径变化。

## 18.2 Spearman 使用绝对值可能掩盖方向错误

如果风险箱已经按低风险到高风险排序，理想值应接近 `+1`。若得到 `-0.99`，绝对值虽然很高，但实际很可能是风险顺序完全颠倒。

建议同时输出：

- 原始 `ρ`；
- 预期方向；
- 是否满足方向性单调。

## 18.3 ChiMerge 条件优先级不够明确

“合并 p 值最大且满足条件的一对”没有完全解释低样本箱、倒挂箱和箱数上限之间的优先关系。建议在源码和报告中记录选择候选对的具体原因。

## 18.4 最终动态箱数与固定 6 箱描述冲突

策略方案写的是动态箱数，但转化漏斗写的是 6 箱。需要统一。

## 18.5 三套方案仅按箱数切分可能造成样本占比失衡

等频初分经过合并后，各最终箱样本量可能差异很大。

例如最低风险前 3 箱可能覆盖 60% 样本，而不是 3/8。若只按箱数比例划分，方案通过率可能与“约前 1/3 客群”完全不同。

建议同时输出：

- 按箱数划分结果；
- 实际累计样本占比；
- 实际累计坏账率。

## 18.6 OOT 主标签成熟度可能不一致

OOT 越接近当前日期，标签未成熟比例可能越高。即使过滤 NULL，剩余成熟样本也可能偏向更早申请，造成选择偏差。

建议按月输出：

- 总申请数；
- 标签有效数；
- 标签成熟率；
- 坏账率。

## 18.7 审批漏斗受历史策略影响

各风险箱的审批率并不完全代表模型质量，因为历史审批策略会影响：

- 哪些人被放款；
- 哪些人有机会产生风险标签；
- 各风险箱主标签有效率；
- FPD7 样本分布。

因此漏斗应作为策略诊断，而不是单独作为模型好坏结论。

---

# 19. 结果评审时建议重点回答的问题

## 19.1 模型排序能力

- 初始 20 箱坏账率是否大体递增？
- 合并后是否完全或基本单调？
- 调优集和 OOT 的 Spearman 分别是多少？
- OOT 是否出现新的倒挂？

## 19.2 统计稳定性

- 每箱样本量是否超过 3000？
- 每箱坏样本数是否超过 100？
- 标准误是否可接受？
- 是否存在全好箱或全坏箱？

## 19.3 跨期稳定性

- 总 PSI 是否低于 0.10？
- 哪些风险箱贡献了主要 PSI？
- OOT 每箱坏账率是否整体抬升？
- OOT IV、AUC、KS 是否明显下降？

## 19.4 策略可用性

- 平衡方案自动通过率是多少？
- 自动通过区坏账率是多少？
- 审核区规模是否超出运营能力？
- 最高风险箱拒绝后能减少多少坏样本？
- 阈值是否能被线上规则准确表达？

## 19.5 业务转化

- 低风险箱完成率是否异常低？
- 低风险箱审批通过率是否过低？
- 高风险箱是否仍有大量放款？
- 哪个阶段造成优质客群损失？

## 19.6 早期风险

- FPD7 在调优和 OOT 是否单调？
- FPD7 AUC/KS 与 3M30 差距多大？
- FPD7 是否能作为更高频的监控指标？

---

# 20. 建议的运行日志与质量检查

脚本运行时建议输出以下检查信息。

```text
[数据加载]
模型分表行数：...
申请信息表行数：...

[主键检查]
模型分表重复 application_id：...
申请信息表重复 application_id：...

[关联结果]
关联后行数：...
模型分匹配率：...
申请信息匹配率：...

[字段质量]
分数缺失率：...
主标签缺失率：...
非法标签数：...
日期解析失败数：...
本金缺失率：...

[样本划分]
调优集总数：...
调优集主标签有效数：...
OOT 总数：...
OOT 主标签有效数：...

[初始分箱]
目标箱数：20
实际箱数：...
初始 IV：...
初始 Spearman：...

[ChiMerge]
最终箱数：...
合并次数：...
停止原因：...
最终 IV：...
最终 Spearman：...

[OOT]
OOT IV：...
OOT Spearman：...
PSI：...

[FPD7]
调优有效样本：...
OOT 有效样本：...

[输出]
结果文件：res/binning_result.md
```

---

# 21. 后续扩展逻辑

当前方法论文档列出的后续扩展包括以下内容。

## 21.1 业务调箱

在统计合并后，允许结合业务经验调整边界，但每次调整必须记录：

- 原边界；
- 新边界；
- 调整原因；
- 对样本量、坏账率、IV、PSI 和策略通过率的影响；
- 调整人和版本日期。

## 21.2 EL：预期损失

补充 PD、LGD、EAD 后：

\[
EL = PD \times LGD \times EAD
\]

在每个候选阈值下计算累计 EL，可将“坏账率”进一步转化为金额损失。

## 21.3 风险后收入

补充利息、费用、资金成本、运营成本和信用损失后：

\[
RiskAdjustedRevenue
= Interest+Fee-FundingCost-OperatingCost-EL
\]

用于判断放宽阈值后新增客群是否仍创造正收益。

## 21.4 UE：单位经济性

可按每笔申请、每笔放款或每单位本金计算单位经济收益，用于增长、平衡和保守方案的最终选择。

## 21.5 PD 校准

包括：

- O/E；
- 校准截距；
- 校准斜率；
- Brier Score；
- 分箱预测 PD 与实际坏账率对比。

## 21.6 分层稳定性

按以下维度分别计算坏账率、AUC、KS、PSI 和通过率：

- 产品；
- 渠道；
- 新老客；
- 客群类型；
- 月份；
- 地区；
- 收入类型。

## 21.7 监控看板

建议监控：

- 分数分布；
- 风险箱占比；
- PSI/CSI；
- 自动通过率；
- 审核率；
- 拒绝率；
- 放款率；
- 各风险箱 FPD7；
- 各风险箱 3M30；
- 边际客群风险；
- 标签成熟率。

## 21.8 配置表和版本治理

最终切点和策略应输出为统一配置，例如：

| 版本 | 生效日期 | 分数方向 | 风险等级 | 下界 | 上界 | 策略动作 | 调优坏账率 | OOT 坏账率 |
|---|---|---|---|---:|---:|---|---:|---:|
| v1 | ... | 高分高风险 | R1 | -inf | ... | 自动通过 | ... | ... |
| v1 | ... | 高分高风险 | R2 | ... | ... | 自动通过 | ... | ... |
| v1 | ... | 高分高风险 | R3 | ... | ... | 人工审核 | ... | ... |

每次修改切点、分数方向、标签或参数都应生成新版本，不应直接覆盖历史配置。

---

# 22. 一句话总结每个模块

| 模块 | 一句话说明 |
|---|---|
| 数据关联 | 将模型分和申请风险表现拼成一张分析宽表 |
| 时间切分 | 用历史样本调优，用未来样本做真正 OOT 验证 |
| 标签过滤 | 只用已成熟标签计算风险表现 |
| 等频初分 | 先建立足够细且样本量相对均衡的风险区间 |
| 初始指标 | 衡量每箱风险、稳定性、区分度和累计通过表现 |
| 相邻检验 | 判断相邻风险箱是否真的有统计差异 |
| ChiMerge | 合并相似、倒挂或不稳定的小箱，形成可用风险等级 |
| OOT 验证 | 检验固定切点在未来时段是否仍然有效 |
| PSI | 检查调优期和 OOT 的风险等级分布是否漂移 |
| 阈值测算 | 量化放宽阈值带来的通过率和风险变化 |
| 三套方案 | 将风险等级转化为自动通过、审核和拒绝策略 |
| 转化漏斗 | 检查不同风险客群在审批链路中的流失和放款情况 |
| FPD7 对比 | 用更早期标签补充判断模型的短期风险识别能力 |
| 报告输出 | 将模型、分箱、策略和验证结果统一沉淀为 Markdown |

---

# 23. 最终结论

该脚本的核心不是“把模型分分成 20 箱”，而是完成四个层次的工作：

1. **统计层**：通过等频分箱、卡方检验、Z 检验、WOE、IV 和 Spearman 判断风险区分是否成立；
2. **稳定层**：通过最低样本量、最低坏样本量、OOT 和 PSI 判断分箱是否稳定；
3. **策略层**：通过累计通过率、累计坏账率、边际坏账率和金额坏账率寻找可执行阈值；
4. **业务层**：通过保守/平衡/增长方案、审批漏斗和 FPD7 对比，将模型分转化为可落地、可验证、可持续监控的风险策略。

当前逻辑已经覆盖分箱和策略设计的主要环节。最需要进一步结合源码确认的部分是：

- 数据去重和异常值处理；
- ChiMerge 候选箱选择优先级；
- 三套策略箱数取整方式；
- 转化率漏斗的拒绝状态定义；
- 动态最终箱数与“固定 6 箱”描述之间的关系；
- WOE、PSI 零值平滑方式；
- AUC、KS 的分数方向处理。

