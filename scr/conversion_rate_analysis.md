# 转化率计算

基于 `ba.customer_profile_rawdata` 表，从申请到放款的各环节转化率定义。

## 状态字段说明

| 字段 | 含义 |
|---|---|
| `application_status` | 申请状态（`3.x`/`4.x` 开头表示通过） |
| `assessment_status` | 审核结果（含 `Auto Approved` / `Manual Approved`） |
| `status` | 账户状态（`Active_Account` / `Closed` / `Blocked` 表示已放款） |

## 计数口径

| 指标 | 逻辑 |
|---|---|
| `apply_cnt` | 申请样本数 |
| `completed_application_cnt` | 已完成申请数（排除 `0.Incomplete`、`1.In Progress`） |
| `approved_application_cnt` | `application_status` 以 `3` 或 `4` 开头 |
| `auto_approved_application_cnt` | 通过且 `assessment_status` 含 `Auto Approved` |
| `manual_approved_application_cnt` | 通过且 `assessment_status` 含 `Manual Approved` |
| `deal_sample_cnt` | `status IN ('Active_Account','Closed','Blocked')` |

## 转化率公式

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

仅对已放款（`4.Funded`）且首期还款日已过 7 天的申请计算，本质与其他 `duedate_*` 标签相同，输出 0/1/NULL。

> 依赖字段来自 `ba.customer_profile_rawdata`：`first_payment_scheduled_date`、`first_payment_days_past_due_ever`。

### Servicing Vintage（资产表现）

| 字段 | 含义 |
|---|---|
| `principal` | 贷款本金 |
| `closed_flag` | 是否已结清 |
| `estimate_principal_remaining_mob{n}` | MOB{n} 时点的剩余本金（n=0~12） |
| `dpd_days_mob{n}` | MOB{n} 时点的逾期天数（n=0~12） |

### 风险指标公式

计算口径分为两种维度：

| 维度 | 说明 | 适用场景 |
|---|---|---|
| 标的率（按笔数） | 每笔贷款等权，`SUM(y) / COUNT(*)` | 衡量多少比例的**人**逾期 |
| 金额率（按本金） | 本金加权，`SUM(principal * y) / SUM(principal)` | 衡量多少比例的**钱**逾期 |

| 指标 | 标的率（笔数） | 金额率（本金加权） | 说明 |
|---|---|---|---|
| 逾期率 | `AVG(y)` | `SUM(principal * y) / SUM(principal)` | y=1 的占比，NULL 不计入分母 |
| AUC | 梯形法：`SUM((tpr + next_tpr) / 2 * (fpr - next_fpr))` | 同左（排序级指标，不区分口径） | ROC 曲线下面积 |
| Pearson 相关系数 | `CORR(score_a, score_b)` | 同左 | 两模型分数的线性相关性 |
| PSI | 参考主文档分箱方法论 | 同左 | 分数分布稳定性 |

> 金额率的逾期率为金额逾期率（即逾期本金/总本金），更能反映真实风险敞口。实际分析时建议两者同时汇报。

### 状态值说明

| `application_status` | 含义 |
|---|---|
| `0.Incomplete` | 未完成申请 |
| `1.In Progress` | 进行中 |
| `2.1.Submitted Withdrawn` | 已撤回 |
| `2.3.Risk Declined` | 风控拒绝 |
| `3.x` / `4.x` | 通过（含 `4.Funded` 已放款） |
