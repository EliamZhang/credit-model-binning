# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

本仓库是一个信贷风控方法论文档项目，核心文档为 [credit-model-binning-and-strategy-threshold.md](credit-model-binning-and-strategy-threshold.md)，涵盖模型分的风险分层、分箱方法、策略阈值设计、收益口径和持续监控。

推荐方案：**等频 20 箱 → 单调/统计合并 → 业务调整 → 收益阈值优化**。

## 数据文件

`res/` 目录存放本地数据文件（已通过 `.gitignore` 排除，不推送到 git）：

- [res/model_score.csv](res/model_score.csv) — 模型分表。主键 `application_id`，字段：`user_id, application_id, sample_datetime, aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score`
- [res/sample.csv](res/sample.csv) — 样本表（宽表）。主键 `application_id`，包含 `user_id, application_id, sample_datetime, sample_month, sample_quarter, user_type` 及各宽限期字段
- [res/application_info.xlsx](res/application_info.xlsx) — 申请信息表

模型分和样本表通过 `application_id` 关联。

## SQL 脚本

- [scr/application_info_extract.sql](scr/application_info_extract.sql) — 从 `ba.customer_profile_rawdata` 提取申请信息，包含 user_id、application_id、申请时间、放款日期、LTI/PTI/NSTI、各 MOB 的本金余额和逾期天数、首期还款信息、宽限期字段、标签字段、金额和期限字段。筛选 `application_time >= '2025-01-01'`

## 转化率计算

基于 `ba.customer_profile_rawdata`，从申请到放款的各环节转化率。

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

### Vintage 逾期标签（listing vintage）

| 维度 | 字段格式 | 取值范围 |
|---|---|---|
| 5 DPD | `duedate_{m}m_5` | 1m ~ 12m，共 12 期 |
| 30 DPD | `duedate_{m}m_30` | 1m ~ 12m，共 12 期 |
| 60 DPD | `duedate_{m}m_60` | 1m ~ 12m，共 12 期 |

每条记录表示第 m 个账单日后 N 天是否逾期（0/1/NULL）。常用目标：`duedate_1m_5`（首期短期逾期）、`duedate_3m_30`（三期严重逾期）。

### FPD7（首期支付逾期 7 天）

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

仅对已放款（`4.Funded`）且首期还款日已过 7 天的申请计算，输出 0/1/NULL。依赖字段：`first_payment_scheduled_date`、`first_payment_days_past_due_ever`（均来自 `ba.customer_profile_rawdata`）。

### Servicing Vintage（资产表现）

| 字段 | 含义 |
|---|---|
| `principal` | 贷款本金 |
| `closed_flag` | 是否已结清 |
| `estimate_principal_remaining_mob{n}` | MOB{n} 时点的剩余本金（n=0~12） |
| `dpd_days_mob{n}` | MOB{n} 时点的逾期天数（n=0~12） |

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
