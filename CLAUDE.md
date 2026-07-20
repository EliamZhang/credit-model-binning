# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

本仓库是一个信贷风控方法论文档项目，核心文档为 [credit-model-binning-and-strategy-threshold.md](credit-model-binning-and-strategy-threshold.md)，涵盖模型分的风险分层、分箱方法、策略阈值设计、收益口径和持续监控。

推荐方案：**等频 20 箱 → 单调/统计合并 → 业务调整 → 收益阈值优化**。

[scr/binning.py](scr/binning.py) 是本方法论的程序实现载体，沿文档的 8 步流程（0.定义口径 → 7.上线治理）逐步落地。当前完成步骤 2-5（等频 20 箱初分、ChiMerge 合并、累计阈值曲线、三套方案）。修改脚本前先看文档对应章节了解业务口径，新增功能后同步更新 [scr/binning.md](scr/binning.md)。

三个模型分的关系：`aus_old_risk_apply_appmodel`（申请模型）和 `aus_old_risk_bid_submodel`（交易特征子模型）是子模型，融合后得到 `aus_old_risk_bid_mltmodel`（多头融合模型），分箱以 mlt 模型为主。

## 待完成功能清单

当前阶段可以概括为：风险分层和坏账率口径的策略雏形已经完成，真正还没做的是经济价值决策、上线配置治理和持续监控体系。

| 优先级 | 待完成功能 | 说明 |
|---|---|---|
| 高 | EL / 收入 / 风险后收入 / UE 测算 | 当前报告里已标注“EL/收入/UE 待补充经济数据后扩展”。现在阈值方案主要看通过率和坏账率，还没有真正做利润导向策略。 |
| 高 | 逐阈值经济价值优化 | 按候选阈值计算 `PD * LGD * EAD`、预计收入、总 UE、单笔 UE、边际 UE，用于寻找风险和利润约束下的最优阈值。 |
| 高 | 人工审核产能与审核净价值 | 当前只是把中间分数段命名为“人工审核”，还没有评估审核量、审核成本、审核后增量收益和产能约束。 |
| 高 | 配置表 / 版本治理输出 | 还没有生成模型分层与策略阈值配置表，也没有模型版本、PD 映射版本、策略版本、生效/失效日期、审批人、回滚版本等治理字段。 |
| 中 | PD 校准相关功能 | 现在主要用实际坏账率，没有平均预测 PD、O/E、校准截距、校准斜率、Brier Score，也没有 PD 映射版本管理。 |
| 中 | 按产品 / 渠道 / 客群 / 月度的稳定性拆解 | 目前只做 tuning vs OOT 总体对比，没有按月份、渠道、产品、用户类型等维度重算坏账率、PD、EL、UE 和排序稳定性。 |
| 中 | 压力测试 / 情景分析 | 文档要求基准、乐观、压力情景下的三套方案结果；当前还没有风险上浮、收入下降、资金成本变化等情景测算。 |
| 中 | 监控看板指标输出 | 还没有线上监控表：缺失率、异常率、PSI/CSI、通过率、拒绝率、审核率、规则命中率、实际损失、实际收入、UE 等。 |
| 中 | CSI / 特征漂移 | 当前只算了模型分 PSI，没有单变量 CSI 或分字段缺失、异常漂移。 |
| 中 | 样本划分更完整 | 方法论文档里的训练集、校准集、策略调优集、OOT、线上成熟样本，目前代码只切了 tuning 和 OOT。 |
| 中 | 业务调整与人工边界治理 | 当前 ChiMerge 后直接使用合并结果，没有业务取整、对齐既有评级、记录调整原因和影响的流程。 |
| 低 | 拒绝推断 / 审批选择偏差处理 | 文档提醒历史已通过客户会有选择偏差；当前没有探索样本、外部表现或拒绝推断敏感性分析。 |
| 低 | 最终管理层决策表补全 | 现在三套方案表缺 EL、收入、总 UE、审核量、主要风险这些核心管理层字段。 |

## 数据文件

`res/` 目录存放本地数据文件（已通过 `.gitignore` 排除，不推送到 git）：

- [res/application_info.csv](res/application_info.csv) — 申请信息表（420,508 行，53 列），由原始 Spark 导出按模型分表的 `application_id` 精准匹配筛选。新增 `status`（账户状态）和 `application_status`（申请状态）字段。含 `user_id`、申请时间、放款日期、`LTI/PTI/NSTI`、`principal`、`estimate_principal_remaining_mob{0~4}`、`dpd_days_mob{0~4}`、`dpd_days_ever_mob{0~4}`、`first_payment_*`、`duedate_{1m,2m,3m}_5`、`duedate_{1m,2m,3m,4m}_30`、`application_tag/user_tag/loan_tag`、`requested_loan_*`、`payout_*`、`original_term_*`、`actual_term_*`
- [res/aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv](res/aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv) — 多头融合模型分（分箱主模型，420,519 行）。字段：`application_id, user_id, sample_datetime, aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score`
- [res/aus_old_risk_apply_appmodel_v20260318_v1_2_lgb_score.csv](res/aus_old_risk_apply_appmodel_v20260318_v1_2_lgb_score.csv) — 申请模型分（子模型，420,519 行）。字段：`application_id, user_id, sample_datetime, aus_old_risk_apply_appmodel_v20260318_v1_2_lgb_score`
- [res/aus_old_risk_bid_submodel_v20260323_v1_2_txn_lgb_score.csv](res/aus_old_risk_bid_submodel_v20260323_v1_2_txn_lgb_score.csv) — 交易特征子模型分（420,519 行）。字段：`application_id, user_id, sample_datetime, feature_error, aus_old_risk_bid_submodel_v20260323_v1_2_txn_lgb_score` 及 21 个交易特征列
- [res/sample.csv](res/sample.csv) — 样本表（420,508 行）。字段：`application_id, user_id, sample_datetime`
- [res/binning_result.md](res/binning_result.md) — 分箱结果（由脚本生成）

模型分表和申请表通过 `application_id` 关联。数据覆盖时间范围：2024-01-01 ~ 2026-07-19，含 `user_tag=Existing` 和 `user_tag=New` 用户。

## 脚本

- [scr/binning.py](scr/binning.py) — 分箱主脚本。读取 mlt 模型分和申请表，按 `application_id` 关联获取标签 `duedate_3m_30`，以 2025-10-21 划分策略调优集/OOT 集，输出 `res/binning_result.md`
- [scr/binning.md](scr/binning.md) — 分箱方法论文档，与 binning.py 逻辑同步更新
- [scr/application_info_extract.sql](scr/application_info_extract.sql) — 从 `ba.customer_profile_rawdata` 提取申请信息的原始 SQL。筛选 `application_time ≥ '2025-01-01'`。当前本地 `application_info.csv` 由 Spark 导出，非此 SQL 产出，SQL 仅作字段参考

## 风险指标公式

### Vintage 逾期标签

`application_info.csv` 中实际存在的标签字段：

| 字段 | 含义 |
|---|---|
| `duedate_1m_5` | 首期 5 DPD |
| `duedate_2m_5` | 第二期 5 DPD |
| `duedate_3m_5` | 第三期 5 DPD |
| `duedate_1m_30` | 首期 30 DPD |
| `duedate_2m_30` | 第二期 30 DPD |
| `duedate_3m_30` | 第三期 30 DPD（常用目标） |
| `duedate_4m_30` | 第四期 30 DPD |

每条记录为 0/1/NULL。脚本中两个标签的分工：

- `duedate_3m_30`（`LABEL_COL`）：主标签，计算坏账率、排序单调性、AUC/KS 均基于此列
- `duedate_1m_30`：辅助标签，ChiMerge 合并时要求两列的卡方检验**同时满足**才允许合并（[scr/binning.py:24](scr/binning.py#L24) `CHIMERGE_LABEL_COLS`）

### Servicing Vintage（资产表现）

`application_info.csv` 中实际存在的字段（MOB 0~4）：

| 字段 | 含义 |
|---|---|
| `principal` | 贷款本金 |
| `estimate_principal_remaining_mob{n}` | MOB{n} 时点的剩余本金（n=0~4） |
| `dpd_days_mob{n}` | MOB{n} 时点的逾期天数（n=0~4） |
| `dpd_days_ever_mob{n}` | MOB{n} 时点的历史最大逾期天数（n=0~4） |

### 标签定义

脚本使用两个独立标签，分别服务于不同口径的坏账率计算：

| 标签 | 数据列 | 判断逻辑 | 用途 | 适用指标 |
|---|---|---|---|
| 笔数坏账标签 | `duedate_3m_30` | MOB3 是否逾期 ≥ 30 天（0/1/NULL） | 分箱逻辑 | 笔数坏账率、AUC、KS、Spearman ρ、ChiMerge 合并判断 |
| 金额坏账标签 | `dpd_days_ever_mob3` | MOB3 内历史最大逾期天数 ≥ 30 时记为 1 | 展示 | 金额坏账率（累计 / 分段 / 方案） |

> 两个标签的数据来源和业务含义不同，不可互换。`duedate_3m_30` 反映**标的逾期**（第三期到期的逾期状态），`dpd_days_ever_mob3 ≥ 30` 反映**资产逾期**（MOB3 内是否曾逾期 ≥ 30 天），后者更匹配金额口径的风险敞口。

ChiMerge 合并时同时检验两个标签的卡方 p 值：`CHIMERGE_LABEL_COLS = ["duedate_3m_30", "duedate_1m_30"]`，相邻箱必须在两个标签上均不显著（p ≥ 0.05）才允许合并。

### 数据来源与加工链路

所有指标基于以下两张表通过 `application_id` 做 inner join 后计算：

| 表 | 文件 | 关键字段 | 用途 |
|---|---|---|---|
| 模型分表 | `aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv` | `application_id` | 关联主键 |
| 模型分表 | 同上 | `sample_datetime` | 切分 tuning（2025-10-21 之前）/ OOT（2025-10-21 及之后） |
| 模型分表 | 同上 | `aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score` | 模型分（分箱对象） |
| 申请信息表 | `application_info.csv` | `application_id` | 关联主键 |
| 申请信息表 | 同上 | `duedate_3m_30` | 笔数坏账标签（主标签，0/1/NULL，分箱逻辑唯一标签） |
| 申请信息表 | 同上 | `duedate_1m_30` | 辅助标签（ChiMerge 双标签卡方检验） |
| 申请信息表 | 同上 | `dpd_days_ever_mob3` | 金额坏账标签（`≥ 30` 记为 1） |
| 申请信息表 | 同上 | `principal` | 原贷本金（金额坏账率分母） |
| 申请信息表 | 同上 | `estimate_principal_remaining_mob3` | MOB3 剩余本金（金额坏账率分子） |

关联后过滤 `duedate_3m_30 IS NOT NULL` 得到有效样本，再按 `sample_datetime` 划分 tuning 和 OOT。

---

### 一、分箱内指标（单箱独立统计）

`compute_bin_stats()` 对每个分箱计算以下指标。设箱内样本量为 n、坏样本数为 B（B = 箱内 SUM(duedate_3m_30)）、好样本数为 G = n − B，整体总样本为 total_N、总坏样本为 total_B。

| 指标 | 代码变量 | 公式 | 用途 | 业务含义 |
|---|---|---|---|
| 样本量 | `n` | `过滤 duedate_3m_30 IS NOT NULL 后，按分箱 COUNT(*)` | 分箱逻辑 | 箱内样本总数（低于 MIN_BIN_SIZE 触发合并） |
| 坏样本数 | `B` | `过滤 duedate_3m_30 IS NOT NULL 后，按分箱 SUM(duedate_3m_30)` | 分箱逻辑 | 箱内 label=1 的样本数（低于 MIN_BAD_COUNT 触发合并） |
| 好样本数 | `G` | `n − B` | 展示 | 箱内 label=0 的样本数 |
| 笔数坏账率 | `bad_rate` | `SUM(duedate_3m_30) / COUNT(*) 按分箱` | 分箱逻辑 | 箱内坏样本占比。ChiMerge 用此判断局部倒挂 |
| 笔数坏账率标准误 | `SE` | `sqrt(bad_rate × (1 − bad_rate) / n)` | 展示 | 坏账率的抽样标准误 |
| 坏样本占比 | `B_pct` | `该箱 SUM(duedate_3m_30) / 全样本 SUM(duedate_3m_30)` | 展示 | 该箱坏样本占全部坏样本的比例 |
| 好样本占比 | `G_pct` | `该箱 (n − B) / 全样本 (total_N − total_B)` | 展示 | 该箱好样本占全部好样本的比例 |
| WOE | `WOE` | `ln(B_pct / G_pct)` | 展示 | 箱内好坏比的对数，正值表示坏样本集中 |
| 单箱 IV | `IV_component` | `(B_pct − G_pct) × WOE` | 展示 | 该箱对总 IV 的贡献，总 IV = Σ IV_component |
| Lift | `lift` | `该箱 bad_rate / 全样本 bad_rate（全样本 = SUM(duedate_3m_30) / COUNT(duedate_3m_30)）` | 展示 | `大于 1` 表示该箱风险高于整体平均水平 |

### 二、累计指标（低风险到高风险逐箱累加）

在分箱按分数从低到高排列后，对上述分箱内指标做累加计算。累计方向为 score ≤ 当前箱上限。

| 指标 | 代码变量 | 公式 | 用途 | 业务含义 |
|---|---|---|---|
| 累计样本量 | `cum_n` | `cumsum(n) 按分箱风险序累加` | 展示 | 截止该箱的通过样本数 |
| 累计坏样本 | `cum_B` | `cumsum(B) 按分箱风险序累加` | 展示 | 截止该箱的坏样本数 |
| 累计通过率 | `cum_pass_rate` | `cum_n / total_N（total_N = 全样本 COUNT(duedate_3m_30 IS NOT NULL)）` | 展示 | 截止该箱的通过样本占比 |
| 累计笔数坏账率 | `cum_bad_rate` | `cum_B / cum_n` | 展示 | 截止该箱的坏样本占比 |
| 累计 Lift | `cum_lift` | `cum_bad_rate / 全样本 bad_rate（全样本 = SUM(duedate_3m_30) / COUNT(duedate_3m_30)）` | 展示 | 截止该箱的风险与整体平均的比值 |

### 三、逐阈值指标（连续分数轴上逐点计算）

`compute_threshold_curve()` 不再按分箱边界，而是在连续分数轴上取 N 个阈值点（默认 20 个等分位点 + 合并箱边界点），对每个阈值计算累计通过人群的风险指标。

> 累计方向：高分高风险时 `score ≤ threshold`，高分低风险时 `score ≥ threshold`。

| 指标 | 代码变量 | 公式 | 用途 | 业务含义 |
|---|---|---|---|
| 阈值 | `threshold` | `模型分等分位点（20 个）∪ 合并箱上界` | 展示 | 策略切点候选值 |
| 累计样本量 | `cum_n` | `COUNT(*) WHERE 模型分 ≤ threshold AND duedate_3m_30 IS NOT NULL` | 展示 | 该阈值下的通过样本数 |
| 累计通过率 | `cum_pass_rate` | `cum_n / 全样本 COUNT(duedate_3m_30 IS NOT NULL)` | 展示 | 该阈值下的通过占比 |
| 累计笔数坏账率 | `cum_bad_rate_count` | `SUM(duedate_3m_30 WHERE 模型分 ≤ threshold) / cum_n` | 展示 | 通过人群的笔数坏账率 |
| 边际笔数坏账率 | `marginal_bad_rate_count` | `SUM(duedate_3m_30 WHERE prev_threshold 低于 模型分 ≤ threshold) / COUNT(*) WHERE prev_threshold 低于 模型分 ≤ threshold` | 展示 | 相邻阈值间新增通过人群的坏账率 |
| 累计金额坏账率 | `cum_bad_rate_amount` | `SUM(estimate_principal_remaining_mob3 WHERE dpd_days_ever_mob3 ≥ 30 AND 模型分 ≤ threshold) / SUM(principal WHERE 模型分 ≤ threshold)，仅计分子分母均非空且 大于 0` | 展示 | 通过人群的金额口径坏账率 |

> 金额坏账率公式：分子 = `SUM(estimate_principal_remaining_mob3 WHERE dpd_days_ever_mob3 ≥ 30)`，分母 = `SUM(principal)`，仅计分子分母均非空且 `大于 0` 的样本。分子分母的对应人群不完全一致（剩余本金可能为空），不能与笔数坏账率直接对比绝对值。

### 四、模型级排序指标

| 指标 | 代码位置 | 公式 | 用途 | 业务含义 |
|---|---|---|---|
| 整体笔数坏账率 | `total_B / total_N` | `全样本 SUM(duedate_3m_30) / COUNT(duedate_3m_30 IS NOT NULL)` | 展示 | 全样本的笔数坏账率 |
| 总 IV | `tuning_IV` | `Σ 各箱 (B_pct − G_pct) × ln(B_pct / G_pct)` | 展示 | 模型分的整体区分能力，`高于 0.5` 为强 |
| AUC | `compute_auc_ks()` | `在原始 aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score 上，以 duedate_3m_30 为标签，梯形法：Σ((TPRᵢ + TPRᵢ₋₁) / 2 × (FPRᵢ − FPRᵢ₋₁))` | 展示 | 模型排序能力，0.5 为随机，1.0 为完美 |
| KS | `compute_auc_ks()` | `在原始模型分上，以 duedate_3m_30 为标签，max(|TPR − FPR|)` | 展示 | 好坏样本分布的最大分离度 |
| Spearman ρ | `spearmanr(bin_index, bad_rate)` | `spearmanr(分箱序 1..k, 各箱 SUM(duedate_3m_30) / COUNT(*))` | 分箱逻辑 | 分箱序与笔数坏账率的单调性，合并后必须越接近 1 越好，倒挂则需回溯合并 |

> AUC/KS 在原始分数上计算，不依赖分箱，合并前后数值不变。

### 五、分布稳定性指标

| 指标 | 代码函数 | 公式 | 用途 | 业务含义 |
|---|---|---|---|
| PSI | `compute_psi()` | `Σ((OOT各箱占比 − tuning各箱占比) × ln(OOT各箱占比 / tuning各箱占比))，各箱占比 = 模型分落入该箱的 COUNT(*) / 全样本 COUNT(*)` | 展示 | 模型分分布跨期稳定性。`低于 0.1` 稳定，`0.1~0.25` 轻微漂移，`高于 0.25` 明显漂移 |
| 卡方检验 | `compute_adjacent_tests()` / `_chi2_for_table()` | `相邻两箱 [[SUM(duedate_3m_30)_a, n_a − SUM(duedate_3m_30)_a], [SUM(duedate_3m_30)_b, n_b − SUM(duedate_3m_30)_b]] 的 χ² 独立性检验（ChiMerge 同时检验 duedate_1m_30）` | 分箱逻辑 | 判断相邻两箱的好坏分布是否独立，p `低于 0.05` 表示差异显著不可合并 |
| Z 检验 | `compute_adjacent_tests()` | `(bad_rate_b − bad_rate_a) / sqrt(bad_rate_a(1−bad_rate_a)/n_a + bad_rate_b(1−bad_rate_b)/n_b)，其中 bad_rate = SUM(duedate_3m_30) / COUNT(*)` | 展示 | 判断相邻两箱笔数坏账率差异是否显著，p = 2 × (1 − Φ(|z|)) |

> ChiMerge 合并时对 `CHIMERGE_LABEL_COLS` 中的每个标签分别计算卡方检验，所有标签均不显著（p ≥ 0.05）才允许合并。

### 六、方案指标

`design_three_schemes()` 基于合并后的风险等级生成保守/平衡/增长三套方案，每套方案将分数轴划分为三段：自动通过、人工审核、拒绝。方案边界由覆盖的低风险箱数决定。

**方案边界参数：**

| 参数 | 代码变量 | 含义 |
|---|---|---|
| 自动通过上限 | `auto_max` | score ≤ auto_max 自动通过 |
| 审核上限 | `review_max` | `auto_max 低于 score ≤ review_max 人工审核` |
| 是否拒绝 | `reject` | `score 高于 review_max 的客群是否直接拒绝（增长方案不拒绝）` |

**方案分段指标**（`compute_scheme_stats()`，对每个策略段分别计算）：

| 指标 | 代码变量 | 公式 | 用途 | 业务含义 |
|---|---|---|---|
| 分段样本量 | `n` | `COUNT(*) WHERE 模型分在策略段内 AND duedate_3m_30 IS NOT NULL` | 展示 | 该策略段的样本数 |
| 分段占比 | `pct` | `分段 n / 全样本 COUNT(duedate_3m_30 IS NOT NULL)` | 展示 | 该策略段占全样本的比例 |
| 分段笔数坏账率 | `bad_rate_count` | `SUM(duedate_3m_30 WHERE 模型分在段内) / 分段 n` | 展示 | 段内的笔数坏账率 |
| 分段金额坏账率 | `bad_rate_amount` | `SUM(estimate_principal_remaining_mob3 WHERE dpd_days_ever_mob3 ≥ 30 AND 模型分在段内) / SUM(principal WHERE 模型分在段内)，仅计均非空且 大于 0` | 展示 | 段内的金额口径坏账率 |

**方案汇总指标**（通过人群 = score ≤ review_max）：

| 指标 | 代码变量 | 公式 | 用途 | 业务含义 |
|---|---|---|---|
| 方案通过样本量 | `pass_n` | `COUNT(*) WHERE 模型分 ≤ review_max AND duedate_3m_30 IS NOT NULL` | 展示 | 通过（自动+审核）的总样本数 |
| 方案通过率 | `pass_rate` | `pass_n / 全样本 COUNT(duedate_3m_30 IS NOT NULL)` | 展示 | 通过样本占全样本比例 |
| 通过人群笔数坏账率 | `pass_bad_rate_count` | `SUM(duedate_3m_30 WHERE 模型分 ≤ review_max) / pass_n` | 展示 | 通过人群的笔数坏账率 |
| 通过人群金额坏账率 | `pass_bad_rate_amount` | `SUM(estimate_principal_remaining_mob3 WHERE dpd_days_ever_mob3 ≥ 30 AND 模型分 ≤ review_max) / SUM(principal WHERE 模型分 ≤ review_max)，仅计均非空且 大于 0` | 展示 | 通过人群的金额口径坏账率 |
| 方案拒绝样本量 | `reject_n` | `全样本 COUNT(duedate_3m_30 IS NOT NULL) − pass_n` | 展示 | 拒绝的样本数 |
| 方案拒绝率 | `reject_rate` | `reject_n / 全样本 COUNT(duedate_3m_30 IS NOT NULL)` | 展示 | 拒绝样本占全样本比例 |

> 金额坏账率反映真实风险敞口，建议与笔数坏账率同时汇报。所有指标均按 `SCORE_HIGHER_IS_RISKIER` 的方向计算累计和阈值，当前配置为高分高风险（score 越低风险越低）。

## 文档风格

- 文档使用简体中文撰写
- 数学公式使用 LaTeX 语法（`\( ... \)` 行内，`\[ ... \]` 块级）
- 表格使用 GFM 格式，引用使用 `>` 块引用

## 编辑约定

- 新增章节遵循现有编号层级（中文数字章 → 数字节）
- 参考资料使用编号列表，格式 `[来源名称](URL)`
- 文件/列名使用英文小写下划线命名

## 协作偏好

- **记忆统一管理**：所有项目记忆、用户偏好、对话要求统一记录在本 CLAUDE.md 中，不使用 `.claude/memory/` 目录
- **数据文件**：合并后的文件替换原始文件，中间文件及时删除精简目录结构
- **输出格式**：脚本输出 Markdown（.md），不输出 CSV
- **命名规范**：脚本和输出文件使用有意义的英文名，如 `binning.py` → `binning_result.md`
- **同步更新**：CLAUDE.md 和 scr/*.md 文档随代码变更同步更新，保持与实际情况一致
- **分支策略**：`master` 为正式分支，开发使用 `staging` 分支，功能完成后提交推送
- **脚本与文档关系**：[credit-model-binning-and-strategy-threshold.md](credit-model-binning-and-strategy-threshold.md) 是方法论蓝图（8 步流程），[scr/binning.py](scr/binning.py) 是程序实现载体，沿文档步骤逐步落地
