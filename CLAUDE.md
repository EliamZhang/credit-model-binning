# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

本仓库是一个信贷风控方法论文档项目，核心文档为 [credit-model-binning-and-strategy-threshold.md](credit-model-binning-and-strategy-threshold.md)，涵盖模型分的风险分层、分箱方法、策略阈值设计、收益口径和持续监控。

推荐方案：**等频 20 箱 → 单调/统计合并 → 业务调整 → 收益阈值优化**。

[scr/binning.py](scr/binning.py) 是本方法论的程序实现载体，沿文档的 8 步流程（0.定义口径 → 7.上线治理）逐步落地。当前完成步骤 2-5（等频 20 箱初分、ChiMerge 合并、累计阈值曲线、三套方案）及转化率漏斗、FPD7 标签对比。修改脚本前先看文档对应章节了解业务口径，新增功能后同步更新 [scr/binning.md](scr/binning.md)。

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
- [scr/application_info_extract.sql](scr/application_info_extract.sql) — 从 `ba.customer_profile_rawdata` 提取申请信息的原始 SQL。筛选 `application_time >= '2025-01-01'`。当前本地 `application_info.csv` 由 Spark 导出，非此 SQL 产出，SQL 仅作字段参考

## 转化率计算（原始 SQL 口径参考）

> 注意：以下字段（`application_status`、`assessment_status`、`status`）来自 `ba.customer_profile_rawdata`。当前本地 `application_info.csv` 已包含 `application_status` 和 `status` 字段，`assessment_status` 未纳入本地数据。

### 状态字段说明

| 字段 | 含义 |
|---|---|
| `application_status` | 申请状态（`3.x`/`4.x` 开头表示通过） |
| `assessment_status` | 审核结果（含 `Auto Approved` / `Manual Approved`） |
| `status` | 账户状态（`Active_Account` / `Closed` / `Blocked` 表示已放款） |

### 申请状态枚举

| `application_status` | 含义 |
|---|---|
| `0.Incomplete` | 未完成申请 |
| `1.In Progress` | 进行中 |
| `2.1.Submitted Withdrawn` | 已撤回 |
| `2.3.Risk Declined` | 风控拒绝 |
| `3.x` / `4.x` | 通过（含 `4.Funded` 已放款） |

### 申请状态标签（flag）

| 标签 | 逻辑 |
|---|---|
| `completed_application_flag` | `application_status NOT IN ('0.Incomplete','1.In Progress')` |
| `approved_application_flag` | `LEFT(application_status,1) IN ('3','4')` |
| `auto_approved_application_flag` | 通过且 `assessment_status LIKE '%Auto Approved%'` |
| `manual_approved_application_flag` | 通过且 `assessment_status LIKE '%Manual Approved%'` |
| `declined_application_flag` | `application_status = '2.3.Risk Declined'` |
| `auto_declined_application_flag` | 拒绝且 `assessment_status LIKE '%Auto Declined%'` |
| `manual_declined_application_flag` | 拒绝且 `assessment_status LIKE '%Manual Declined%'` |
| `withdrawn_application_flag` | `application_status = '2.1.Submitted Withdrawn'` |
| `funded_application_flag` | `application_status = '4.Funded'` |

### 计数口径

| 指标 | 逻辑 |
|---|---|
| `apply_cnt` | 申请样本数 |
| `completed_application_cnt` | 已完成申请数（排除 `0.Incomplete`、`1.In Progress`） |
| `approved_application_cnt` | `application_status` 以 `3` 或 `4` 开头 |
| `auto_approved_application_cnt` | 通过且 `assessment_status` 含 `Auto Approved` |
| `manual_approved_application_cnt` | 通过且 `assessment_status` 含 `Manual Approved` |
| `deal_sample_cnt` | `status IN ('Active_Account','Closed','Blocked')` |

### 转化率公式

| 转化率 | 公式 | 说明 |
|---|---|---|
| 完成率 | `completed_application_cnt / apply_cnt` | 申请中完成的比例 |
| 通过率 | `approved_application_cnt / completed_application_cnt` | 完成中通过的比例 |
| 自动通过率 | `auto_approved_application_cnt / completed_application_cnt` | 完成中自动通过的比例 |
| 人工通过率 | `manual_approved_application_cnt / completed_application_cnt` | 完成中人工通过的比例 |
| 自动通过占比 | `auto_approved_application_cnt / approved_application_cnt` | 通过中自动通过的比例 |
| 人工通过占比 | `manual_approved_application_cnt / approved_application_cnt` | 通过中人工通过的比例 |
| 放款率 | `deal_sample_cnt / approved_application_cnt` | 通过中成功放款的比例 |

## 风险指标

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

每条记录为 0/1/NULL。当前分箱脚本使用 `duedate_3m_30` 作为风险标签。

### FPD7（首期支付逾期 7 天）

`application_info.csv` 中有对应字段：`first_payment_scheduled_date`、`first_payment_days_past_due_ever`。原始 SQL 逻辑：

```sql
CASE
    WHEN application_status = '4.Funded'
     AND first_payment_scheduled_date < CURRENT_DATE() - 7
     AND first_payment_days_past_due_ever > 7
    THEN 1
    WHEN application_status = '4.Funded'
     AND first_payment_scheduled_date < CURRENT_DATE() - 7
    THEN 0
    ELSE NULL
END AS fpd7_flag
```

> 注意：脚本中 FPD7 计算使用 `application_status == '4.Funded'` 判断是否放款，与原始 SQL 口径一致。

### Servicing Vintage（资产表现）

`application_info.csv` 中实际存在的字段（MOB 0~4）：

| 字段 | 含义 |
|---|---|
| `principal` | 贷款本金 |
| `estimate_principal_remaining_mob{n}` | MOB{n} 时点的剩余本金（n=0~4） |
| `dpd_days_mob{n}` | MOB{n} 时点的逾期天数（n=0~4） |
| `dpd_days_ever_mob{n}` | MOB{n} 时点的历史最大逾期天数（n=0~4） |

### 风险指标公式

两种计算口径：

| 维度 | 说明 | 适用场景 |
|---|---|---|
| 标的率（按笔数） | 每笔贷款等权，`SUM(y) / COUNT(*)` | 衡量多少比例的**人**逾期 |
| 金额率（按本金） | 本金加权，`SUM(principal * y) / SUM(principal)` | 衡量多少比例的**钱**逾期 |

| 指标 | 标的率（笔数） | 金额率（本金加权） | 说明 |
|---|---|---|---|
| 逾期率 | `AVG(y)` | `SUM(principal * y) / SUM(principal)` | y=1 的占比，NULL 不计分母 |
| AUC | 梯形法：`SUM((tpr + next_tpr) / 2 * (fpr - next_fpr))` | 同左 | ROC 曲线下面积，排序级指标不区分口径 |
| Pearson 相关系数 | `CORR(score_a, score_b)` | 同左 | 两模型分数的线性相关性 |
| PSI | 参考主文档分箱方法论 | 同左 | 分数分布稳定性 |

> 金额逾期率更能反映真实风险敞口，实际分析建议两者同时汇报。

## 文档风格

- 文档使用简体中文撰写
- 数学公式使用 LaTeX 语法（`\( ... \)` 行内，`\[ ... \]` 块级）
- 表格使用 GFM 格式，引用使用 `>` 块引用

## 编辑约定

- 保持中文术语一致（"坏账率"非"违约率"，"通过率"非"批准率"）
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
