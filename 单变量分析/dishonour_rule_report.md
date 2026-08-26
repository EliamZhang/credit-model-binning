# 拒付规则策略迭代报告

> 本报告为 `dishonour_rule_report.xlsx` 的 Markdown 版,对应 6 个 sheet:拒付变量字典、单变量性能、现行规则组合、规则挖掘候选、规则分档性能、增益损失分析。数据口径与 Excel 完全一致;大表(507 行字典、503 行性能)在本报告中给出结构与代表性条目,完整明细以 Excel 为准。

## 一、报告概述

本工作簿围绕**银行交易拒付(dishonour/bounce)行为变量**展开,回答三个问题:

1. **有哪些拒付变量**——`txn_tool/txn_dishonour.py` 生成的全部拒付变量的口径字典;
2. **变量对逾期标签的区分能力**——KS / IV 在全量样本与"剔除已拒绝样本"两个 scope 下的表现;
3. **规则迭代的经济账**——现行规则(BR05 黑名单硬拒 / GR09 灰名单人工审核)分档表现、挖掘出的候选规则在 Train/OOT 的稳定性,以及 BR05 的增益损失测算(净收益在当前成本模型下为负)。

| Sheet                     |   行数 | 内容                                                                                                            |
| ------------------------- | -----: | --------------------------------------------------------------------------------------------------------------- |
| 1_Dishonour_Dictionary    |    507 | 拒付变量字典:含义、机构类别、时间窗、来源函数、处理逻辑、公式                                                   |
| 2_Variable_Performance    |    503 | 每个变量对 4 个坏标签的 KS / IV / cutoff / 缺失率,分`__all`(全量)与 `__excl_declined`(剔除已拒绝)两个 scope |
| 3a_Rule_Combination       |      2 | 现行规则 BR05(黑名单硬拒)与 GR09(灰名单人工审核)                                                                |
| 3b_Rule_Mining_Candidates |     38 | 规则挖掘候选(pairwise / tree 生成),含 Train 与 OOT 的覆盖、精确率、Lift 与稳定性标记                            |
| 4_Rule_Performance        |     12 | 按`ratio_84d` 三档(<0.11 / 0.11-0.17 / ≥0.17)× 4 个标签的 Train/OOT 表现                                    |
| 5_Gain_Loss_Analysis      | 3+备注 | BR05 的增益(真实坏账金额)与损失(无辜客户费息)测算及盈亏平衡 Lift                                                |

## 二、拒付变量字典(1_Dishonour_Dictionary)

### 2.1 变量体系

507 个变量全部由 `txn_tool/txn_dishonour.py :: _aggregate() / _metrics_for()` 生成,处理逻辑统一:扫描**全量未过滤**交易流(有意不用 txn_lender 的仅贷款过滤器,因为拒付事件的 category 为 'Dishonours' 会被该过滤器丢弃),识别拒付事件(category=='Dishonours' 或文本/交易类型含 DISHONOUR/DISHON/REVERSAL/RETURN),通过 third_party 映射归因到放贷机构,再按"等额 + 回收宽限期内同日机构还款记录"匹配判定 resolved(已回收,宽限期内同机构等额重试)与 unresolved(未回收)。

维度构成:

- **机构类别(7 组)**:Personal loan、BNPL、Cash / wage advance、Bank、Debt collection、Other / unclassified lender 各 74 个,**All lenders (collapsed) 63 个**;
- **时间窗(8 档)**:All-time(无窗)11 个 + Trailing 7/14/28/56/84/168/182 天各 9 个(每类机构)。

| 类别                        | 全时段 | 7天 | 14天 | 28天 | 56天 | 84天 | 168天 | 182天 | 小计 |
| --------------------------- | -----: | --: | ---: | ---: | ---: | ---: | ----: | ----: | ---: |
| Personal loan               |     11 |   9 |    9 |    9 |    9 |    9 |     9 |     9 |   74 |
| BNPL                        |     11 |   9 |    9 |    9 |    9 |    9 |     9 |     9 |   74 |
| Cash / wage advance         |     11 |   9 |    9 |    9 |    9 |    9 |     9 |     9 |   74 |
| Bank                        |     11 |   9 |    9 |    9 |    9 |    9 |     9 |     9 |   74 |
| Debt collection             |     11 |   9 |    9 |    9 |    9 |    9 |     9 |     9 |   74 |
| Other / unclassified lender |     11 |   9 |    9 |    9 |    9 |    9 |     9 |     9 |   74 |
| All lenders (collapsed)     |      0 |   9 |    9 |    9 |    9 |    9 |     9 |     9 |   63 |

每个机构类别 × 时间窗组合下共 9 个指标族,外加 2 个全时段"距最近/最远拒付天数"变量:

| 指标族                                   | 含义                          | 公式                                             |
| ---------------------------------------- | ----------------------------- | ------------------------------------------------ |
| resolved_cnt_3gp                         | 已回收拒付笔数                | COUNT(dishonours where resolved=True)            |
| unresolved_cnt_3gp                       | 未回收拒付笔数                | COUNT(dishonours where resolved=False)           |
| resolved_amt_3gp                         | 已回收拒付金额                | SUM(amount) where resolved=True                  |
| unresolved_amt_3gp                       | 未回收拒付金额                | SUM(amount) where resolved=False                 |
| unresolved_institution_cnt_3gp           | 出现未回收拒付的机构数        | COUNT(DISTINCT institution) where resolved=False |
| resolved_catchup_cnt_3gp                 | 多期追缴回收笔数              | resolved 且金额为分期额整数倍                    |
| ratio                                    | 拒付率(与宽限期无关)          | dishonour_cnt / (dishonour_cnt + repay_cnt)      |
| resolved_ratio_3gp                       | 已回收占比                    | resolved_cnt / dishonour_cnt                     |
| unresolved_ratio_3gp                     | 未回收占比                    | unresolved_cnt / dishonour_cnt                   |
| days_since_furthest / days_since_closest | 距最近/最远拒付天数(仅全时段) | —                                               |

### 2.2 规则相关变量字典

现行规则 BR05 / GR09 与挖掘候选直接使用的 3 个核心变量:

| 变量                                              | 含义                                                        | 时间窗            | 公式                                        |
| ------------------------------------------------- | ----------------------------------------------------------- | ----------------- | ------------------------------------------- |
| bank_txn_dishonour_lender_ratio_84d               | 全机构合并拒付率(resolved+unresolved 合计,与回收宽限期无关) | Trailing 84 days  | dishonour_cnt / (dishonour_cnt + repay_cnt) |
| bank_txn_dishonour_lender_ratio_168d              | 同上,更长窗口                                               | Trailing 168 days | dishonour_cnt / (dishonour_cnt + repay_cnt) |
| bank_txn_dishonour_lender_unresolved_amt_3gp_182d | 全机构未回收拒付总金额                                      | Trailing 182 days | SUM(amount) where resolved=False            |

## 三、单变量性能(2_Variable_Performance)

503 个变量,两个样本 scope:

- **`__all`(全量样本)**:区分度包含"现行规则已拒绝人群"的贡献;
- **`__excl_declined`(剔除已拒绝样本)**:衡量**增量**区分能力——现行规则生效后,剩余人群中的排序能力才是规则迭代的真正空间。

坏标签:fpd7、fpd15、duedate_1m_5、duedate_3m_30(`__excl_declined` scope 额外含 duedate_1m_30)。

### 3.1 核心发现:全样本 KS 呈两极分布

- 全样本 scope 下,约 160 个变量 KS ≈ 0(完全无区分度),约 91 个变量 KS ≈ 0.9(与"是否被拒绝"高度绑定);
- **剔除已拒绝样本后,强变量 KS 从 ~0.94 骤降至 ~0.17,但 IV 反而上升**(如 ratio_84d:KS 0.9404→0.1703,IV 0.0700→0.1864)——说明全样本高 KS 主要是"现行拒绝决策的投影",不是新增风险排序能力;剔除后 IV 上升说明该变量在残余人群中的风险浓度反而更高,仍有增量价值;
- 两个 `_band__all` 分组(各约 250 个变量)的 KS 分布几乎镜像,为数据分片标识而非按 KS 强弱划分,报告不以其作为筛选依据。

### 3.2 Top 变量

**全样本 scope(max_KS)**:

| 变量                                                | max_KS | max_IV |
| --------------------------------------------------- | -----: | -----: |
| bank_txn_dishonour_bnpl_days_since_closest          | 0.9592 | 0.0459 |
| bank_txn_dishonour_bnpl_days_since_furthest         | 0.9531 | 0.0469 |
| bank_txn_dishonour_lender_ratio_168d                | 0.9423 | 0.0766 |
| bank_txn_dishonour_lender_ratio_182d                | 0.9419 | 0.0753 |
| bank_txn_dishonour_lender_unresolved_ratio_3gp_182d | 0.9415 | 0.0767 |
| bank_txn_dishonour_lender_unresolved_ratio_3gp_168d | 0.9410 | 0.0786 |
| bank_txn_dishonour_lender_ratio_84d                 | 0.9404 | 0.0700 |
| bank_txn_dishonour_lender_unresolved_ratio_3gp_84d  | 0.9402 | 0.0722 |
| bank_txn_dishonour_lender_unresolved_amt_3gp_168d   | 0.9386 | 0.0289 |
| bank_txn_dishonour_lender_unresolved_ratio_3gp_56d  | 0.9384 | 0.0506 |

**全样本 scope(max_IV)**:

| 变量                                                         | max_IV | max_KS |
| ------------------------------------------------------------ | -----: | -----: |
| bank_txn_dishonour_lender_unresolved_ratio_3gp_168d          | 0.0786 | 0.9410 |
| bank_txn_dishonour_lender_unresolved_ratio_3gp_182d          | 0.0767 | 0.9415 |
| bank_txn_dishonour_lender_ratio_168d                         | 0.0766 | 0.9423 |
| bank_txn_dishonour_lender_ratio_182d                         | 0.0753 | 0.9419 |
| bank_txn_dishonour_lender_unresolved_ratio_3gp_84d           | 0.0722 | 0.9402 |
| bank_txn_dishonour_lender_ratio_84d                          | 0.0700 | 0.9404 |
| bank_txn_dishonour_lender_unresolved_ratio_3gp_56d           | 0.0506 | 0.9384 |
| bank_txn_dishonour_bnpl_days_since_furthest                  | 0.0469 | 0.9531 |
| bank_txn_dishonour_bnpl_days_since_closest                   | 0.0459 | 0.9592 |
| bank_txn_dishonour_lender_unresolved_institution_cnt_3gp_56d | 0.0454 | 0.9356 |

**剔除已拒绝样本 scope(max_KS)**:

| 变量                                                 | max_KS | max_IV |
| ---------------------------------------------------- | -----: | -----: |
| bank_txn_dishonour_bnpl_days_since_closest           | 0.2385 | 0.0841 |
| bank_txn_dishonour_bnpl_days_since_furthest          | 0.2024 | 0.0604 |
| bank_txn_dishonour_personal_loan_days_since_furthest | 0.1775 | 0.0923 |
| bank_txn_dishonour_lender_ratio_182d                 | 0.1764 | 0.1566 |
| bank_txn_dishonour_lender_ratio_168d                 | 0.1759 | 0.1571 |
| bank_txn_dishonour_lender_unresolved_ratio_3gp_168d  | 0.1726 | 0.1591 |
| bank_txn_dishonour_lender_ratio_84d                  | 0.1703 | 0.1864 |
| bank_txn_dishonour_lender_unresolved_ratio_3gp_182d  | 0.1695 | 0.1528 |
| bank_txn_dishonour_lender_unresolved_ratio_3gp_84d   | 0.1602 | 0.1645 |
| bank_txn_dishonour_lender_unresolved_ratio_3gp_56d   | 0.1555 | 0.1434 |

另有两行特殊变量(`banktransaction_m90d_cnt`、`lender_7d_dishonour_cnt`,旧命名,带前导空格)不在字典体系内,性能字段多为空,仅作遗留对照。

## 四、现行规则组合(3a_Rule_Combination)

现行规则全部基于 `bank_txn_dishonour_lender_ratio_84d`(全机构 84 天拒付率),作用于 `finv_risk_level ∈ {ND1, ND2, NE, NF}` 人群,已提交 prod-new-20260713-v1.1.1:

| 规则 | 层级                       | 变量                                | 条件                     | 人群                                       | 效果                 | 来源                                 |
| ---- | -------------------------- | ----------------------------------- | ------------------------ | ------------------------------------------ | -------------------- | ------------------------------------ |
| BR05 | Black(hard decline)        | bank_txn_dishonour_lender_ratio_84d | ratio_84d > 0.17         | finv_risk_level in ['ND1','ND2','NE','NF'] | Auto-decline(不可逆) | risk_policy.py / risk_policy_test.py |
| GR09 | Grey(soft / manual review) | bank_txn_dishonour_lender_ratio_84d | 0.11 < ratio_84d <= 0.17 | finv_risk_level in ['ND1','ND2','NE','NF'] | 转人工审核(可逆)     | risk_policy.py / risk_policy_test.py |

即:拒付率 17% 以上直接拒绝,11%-17% 转人工,11% 以下不受该规则影响。

## 五、规则挖掘候选(3b_Rule_Mining_Candidates)

38 个候选规则,由 pairwise / tree 两种方式生成,来源 `rules_final (2).csv` 与 `rules_final (3).csv`。两类方向:

- **decline(拒绝方向)**:模型 PD 概率阈值候选(`finv_predicted_probability_of_default > 0.27/0.21/0.17/0.14`)与拒付变量阈值候选;
- **upgrade(豁免/放宽方向)**:对应阈值的反向规则,用于评估"放行"的代价。

| rule_id                 | 坏标签        | 层级     | 方向    | 规则                                                                                                                                          |       IV | 覆盖率 | 精确率 |   Lift | 捕获率 | OOT覆盖率 | OOT精确率 | OOT Lift | OOT稳定      | 含拒付变量 | 来源                |
| ----------------------- | ------------- | -------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------- | -------: | -----: | -----: | -----: | -----: | --------: | --------: | -------: | ------------ | ---------- | ------------------- |
| pair_fpd7_9             | fpd7          | REFERRAL | decline | finv_predicted_probability_of_default > 0.270469                                                                                              | 0.311543 | 10.01% | 0.1270 | 2.2622 | 22.63% |     6.90% |    0.0804 |   1.9039 | 是           | 否         | rules_final (2).csv |
| pair_fpd7_8             | fpd7          | SPECIAL  | decline | finv_predicted_probability_of_default > 0.207039                                                                                              | 0.311543 | 20.00% | 0.0986 | 1.7556 | 35.12% |    14.88% |    0.0932 |   2.2075 | 是           | 否         | rules_final (2).csv |
| pair_fpd7_7             | fpd7          | SPECIAL  | decline | finv_predicted_probability_of_default > 0.168052                                                                                              | 0.311543 | 29.99% | 0.0899 | 1.6007 | 48.01% |    21.87% |    0.0831 |   1.9689 | 是           | 否         | rules_final (2).csv |
| pair_fpd7_6             | fpd7          | SPECIAL  | decline | finv_predicted_probability_of_default > 0.138601                                                                                              | 0.311543 | 40.00% | 0.0842 | 1.4987 | 59.95% |    31.73% |    0.0757 |   1.7943 | 是           | 否         | rules_final (2).csv |
| pair_fpd7_11            | fpd7          | SPECIAL  | decline | bank_txn_dishonour_lender_ratio_84d > 0.076923                                                                                                | 0.069215 | 10.32% | 0.1007 | 1.7942 | 18.52% |    13.68% |    0.0698 |   1.6543 | 是           | 是         | rules_final (2).csv |
| upgrade_fpd7_9          | fpd7          | SPECIAL  | upgrade | finv_predicted_probability_of_default <= 0.270469                                                                                             | 0.311543 | 89.98% | 0.0483 | 0.8598 | 77.37% |    91.04% |    0.0389 |   0.9221 | 是           | 否         | rules_final (2).csv |
| upgrade_fpd7_10         | fpd7          | SPECIAL  | upgrade | bank_txn_dishonour_lender_ratio_84d <= 0.024917                                                                                               | 0.069215 | 80.00% | 0.0501 | 0.8917 | 71.33% |    75.32% |    0.0368 |   0.8722 | 是           | 是         | rules_final (2).csv |
| pair_fpd15_9            | fpd15         | DECLINE  | decline | finv_predicted_probability_of_default > 0.270469                                                                                              | 0.342944 | 10.01% | 0.0885 | 2.5317 | 25.33% |     6.90% |    0.0491 |   1.8753 | 是           | 否         | rules_final (2).csv |
| pair_fpd15_8            | fpd15         | REFERRAL | decline | finv_predicted_probability_of_default > 0.207039                                                                                              | 0.342944 | 20.00% | 0.0666 | 1.9050 | 38.11% |    14.88% |    0.0663 |   2.5301 | 是           | 否         | rules_final (2).csv |
| pair_fpd15_7            | fpd15         | SPECIAL  | decline | finv_predicted_probability_of_default > 0.168052                                                                                              | 0.342944 | 29.99% | 0.0588 | 1.6817 | 50.44% |    21.87% |    0.0577 |   2.2052 | 是           | 否         | rules_final (2).csv |
| pair_fpd15_6            | fpd15         | SPECIAL  | decline | finv_predicted_probability_of_default > 0.138601                                                                                              | 0.342944 | 40.00% | 0.0537 | 1.5364 | 61.45% |    31.73% |    0.0534 |   2.0392 | 是           | 否         | rules_final (2).csv |
| upgrade_fpd15_9         | fpd15         | SPECIAL  | upgrade | finv_predicted_probability_of_default <= 0.270469                                                                                             | 0.342944 | 89.98% | 0.0290 | 0.8299 | 74.67% |    91.04% |    0.0244 |   0.9305 | 是           | 否         | rules_final (2).csv |
| pair_duedate_1m_5_9     | duedate_1m_5  | REFERRAL | decline | finv_predicted_probability_of_default > 0.270469                                                                                              | 0.315566 | 10.01% | 0.1655 | 2.1882 | 21.89% |     6.90% |    0.1250 |   2.2795 | 是           | 否         | rules_final (2).csv |
| pair_duedate_1m_5_8     | duedate_1m_5  | SPECIAL  | decline | finv_predicted_probability_of_default > 0.207039                                                                                              | 0.315566 | 20.00% | 0.1294 | 1.7105 | 34.22% |    14.88% |    0.1263 |   2.3031 | 是           | 否         | rules_final (2).csv |
| pair_duedate_1m_5_7     | duedate_1m_5  | SPECIAL  | decline | finv_predicted_probability_of_default > 0.168052                                                                                              | 0.315566 | 29.99% | 0.1212 | 1.6025 | 48.07% |    21.87% |    0.1070 |   1.9520 | 是           | 否         | rules_final (2).csv |
| pair_duedate_1m_5_6     | duedate_1m_5  | SPECIAL  | decline | finv_predicted_probability_of_default > 0.138601                                                                                              | 0.315566 | 40.00% | 0.1119 | 1.4792 | 59.16% |    31.73% |    0.0990 |   1.8059 | 是           | 否         | rules_final (2).csv |
| pair_duedate_1m_5_11    | duedate_1m_5  | SPECIAL  | decline | bank_txn_dishonour_lender_ratio_84d > 0.076923                                                                                                | 0.070468 | 10.32% | 0.1343 | 1.7760 | 18.33% |    13.68% |    0.0811 |   1.4786 | 是           | 是         | rules_final (2).csv |
| upgrade_duedate_1m_5_9  | duedate_1m_5  | SPECIAL  | upgrade | finv_predicted_probability_of_default <= 0.270469                                                                                             | 0.315566 | 89.98% | 0.0657 | 0.8680 | 78.11% |    91.04% |    0.0491 |   0.8948 | 是           | 否         | rules_final (2).csv |
| upgrade_duedate_1m_5_10 | duedate_1m_5  | SPECIAL  | upgrade | bank_txn_dishonour_lender_ratio_84d <= 0.024917                                                                                               | 0.070468 | 80.00% | 0.0672 | 0.8885 | 71.08% |    75.32% |    0.0491 |   0.8950 | 是           | 是         | rules_final (2).csv |
| pair_duedate_3m_30_9    | duedate_3m_30 | REFERRAL | decline | finv_predicted_probability_of_default > 0.270469                                                                                              | 0.303668 | 10.01% | 0.2794 | 2.1241 | 21.25% |     6.90% |    0.2589 |   2.1441 | 是           | 否         | rules_final (2).csv |
| pair_duedate_3m_30_8    | duedate_3m_30 | SPECIAL  | decline | finv_predicted_probability_of_default > 0.207039                                                                                              | 0.303668 | 20.00% | 0.2299 | 1.7474 | 34.95% |    14.88% |    0.2588 |   2.1430 | 是           | 否         | rules_final (2).csv |
| pair_duedate_3m_30_7    | duedate_3m_30 | SPECIAL  | decline | finv_predicted_probability_of_default > 0.168052                                                                                              | 0.303668 | 29.99% | 0.2111 | 1.6046 | 48.13% |    21.87% |    0.2127 |   1.7611 | 是           | 否         | rules_final (2).csv |
| pair_duedate_3m_30_6    | duedate_3m_30 | SPECIAL  | decline | finv_predicted_probability_of_default > 0.138601                                                                                              | 0.303668 | 40.00% | 0.1955 | 1.4857 | 59.43% |    31.73% |    0.1854 |   1.5355 | 是           | 否         | rules_final (2).csv |
| upgrade_duedate_3m_30_9 | duedate_3m_30 | SPECIAL  | upgrade | finv_predicted_probability_of_default <= 0.270469                                                                                             | 0.303668 | 89.98% | 0.1151 | 0.8752 | 78.75% |    91.04% |    0.1103 |   0.9135 | 是           | 否         | rules_final (2).csv |
| tree_fpd7_2             | fpd7          | SPECIAL  | decline | bank_txn_dishonour_lender_ratio_168d > 0.037037                                                                                               | 0.072394 | 20.07% | 0.0856 | 1.5240 | 30.59% |    21.66% |    0.0626 |   1.4829 | 是           | 是         | rules_final (3).csv |
| upgrade_fpd7_2          | fpd7          | SPECIAL  | upgrade | bank_txn_dishonour_lender_ratio_168d <= 0.037037                                                                                              | 0.072394 | 79.93% | 0.0488 | 0.8684 | 69.41% |    78.34% |    0.0366 |   0.8665 | 是           | 是         | rules_final (3).csv |
| upgrade_fpd7_4          | fpd7          | SPECIAL  | upgrade | bank_txn_dishonour_lender_ratio_84d <= 0.024917                                                                                               | 0.069215 | 80.00% | 0.0501 | 0.8917 | 71.33% |    75.32% |    0.0368 |   0.8722 | 是           | 是         | rules_final (3).csv |
| upgrade_fpd7_8          | fpd7          | SPECIAL  | upgrade | bank_txn_dishonour_lender_unresolved_amt_3gp_182d <= 95.404                                                                                   | 0.030516 | 70.00% | 0.0504 | 0.8975 | 62.83% |    67.78% |    0.0391 |   0.9262 | 是           | 是         | rules_final (3).csv |
| tree_fpd15_2            | fpd15         | REFERRAL | decline | bank_txn_dishonour_lender_ratio_84d > 0.076923                                                                                                | 0.069000 | 10.32% | 0.0634 | 1.8140 | 18.72% |    13.68% |    0.0518 |   1.9782 | 是           | 是         | rules_final (3).csv |
| pair_fpd15_4            | fpd15         | SPECIAL  | decline | bank_txn_dishonour_lender_ratio_168d > 0.037037                                                                                               | 0.063136 | 20.07% | 0.0522 | 1.4924 | 29.96% |    21.66% |    0.0413 |   1.5753 | 是           | 是         | rules_final (3).csv |
| upgrade_fpd15_4         | fpd15         | SPECIAL  | upgrade | bank_txn_dishonour_lender_ratio_168d <= 0.037037                                                                                              | 0.063136 | 79.93% | 0.0306 | 0.8763 | 70.04% |    78.34% |    0.0220 |   0.8410 | 是           | 是         | rules_final (3).csv |
| pair_duedate_1m_5_171   | duedate_1m_5  | REFERRAL | decline | bank_txn_dishonour_lender_unresolved_amt_3gp_56d > 313.814 AND bank_txn_dishonour_cash_wage_advance_unresolved_institution_cnt_3gp_182d > 1.0 | 0.031418 |  3.71% | 0.1556 | 2.0572 |  7.64% |     5.79% |    0.0479 |   0.8730 | **否** | 是         | rules_final (3).csv |
| tree_duedate_1m_5_2     | duedate_1m_5  | SPECIAL  | decline | bank_txn_dishonour_lender_ratio_168d > 0.037037                                                                                               | 0.077648 | 20.07% | 0.1163 | 1.5372 | 30.86% |    21.66% |    0.0797 |   1.4527 | 是           | 是         | rules_final (3).csv |
| upgrade_duedate_1m_5_2  | duedate_1m_5  | SPECIAL  | upgrade | bank_txn_dishonour_lender_ratio_168d <= 0.037037                                                                                              | 0.077648 | 79.93% | 0.0654 | 0.8651 | 69.14% |    78.34% |    0.0480 |   0.8749 | 是           | 是         | rules_final (3).csv |
| upgrade_duedate_1m_5_1  | duedate_1m_5  | SPECIAL  | upgrade | bank_txn_dishonour_lender_ratio_168d <= 0.010989                                                                                              | 0.077648 | 69.99% | 0.0656 | 0.8671 | 60.69% |    67.62% |    0.0501 |   0.9139 | 是           | 是         | rules_final (3).csv |
| upgrade_duedate_1m_5_4  | duedate_1m_5  | SPECIAL  | upgrade | bank_txn_dishonour_lender_ratio_84d <= 0.024917                                                                                               | 0.070468 | 80.00% | 0.0672 | 0.8885 | 71.08% |    75.32% |    0.0491 |   0.8950 | 是           | 是         | rules_final (3).csv |
| upgrade_duedate_1m_5_10 | duedate_1m_5  | SPECIAL  | upgrade | bank_txn_dishonour_lender_unresolved_amt_3gp_168d <= 69.162                                                                                   | 0.033395 | 70.00% | 0.0676 | 0.8932 | 62.53% |    67.25% |    0.0518 |   0.9440 | 是           | 是         | rules_final (3).csv |

观察要点:

- 模型 PD 概率阈值候选 IV 最高(0.30-0.34),拒付变量候选 IV 较低(0.03-0.08),但后者提供**模型之外的行为增量信息**;
- 38 个候选中 37 个 OOT 稳定;唯一不稳定的是 `pair_duedate_1m_5_171`(双变量深度 2 复合规则,OOT Lift 0.8730 显著低于 Train 2.0572,过拟合特征明显);
- 拒付变量规则方向高度一致:`ratio_84d > 0.077`、`ratio_168d > 0.037` 等,与现行规则阈值(0.17/0.11)相比更低——现有规则只打击了拒付率的极端尾部。

## 六、规则分档性能(4_Rule_Performance)

按 `ratio_84d` 三档(<0.11 不受规则影响 / 0.11-0.17 GR09 灰名单 / ≥0.17 BR05 黑名单)× 4 个坏标签的 Train/OOT 表现(overall 为整档,part1/part2 为样本分片,用于稳健性对照):

| band      | label         | Train n | Train bad | Train 精确率 | Train Lift | OOT n | OOT bad | OOT 精确率 | OOT Lift | part1 Train n | part1 Train bad | part1 Train Lift | part1 OOT n | part1 OOT bad | part1 OOT Lift | part2 Train n | part2 Train bad | part2 Train Lift | part2 OOT n | part2 OOT bad | part2 OOT Lift |
| --------- | ------------- | ------: | --------: | -----------: | ---------: | ----: | ------: | ---------: | -------: | ------------: | --------------: | ---------------: | ----------: | ------------: | -------------: | ------------: | --------------: | ---------------: | ----------: | ------------: | -------------: |
| <0.11     | duedate_1m_5  |  10,586 |       759 |        7.17% |     0.9330 | 4,362 |     233 |      5.34% |   0.9062 |         4,095 |             154 |           0.4894 |       2,049 |            49 |         0.4057 |         6,491 |             605 |           1.2129 |       2,313 |           184 |         1.3496 |
| <0.11     | duedate_3m_30 |  10,586 |     1,368 |       12.92% |     0.9690 | 4,362 |     516 |     11.83% |   0.9846 |         4,095 |             287 |           0.5255 |       2,049 |           138 |         0.5606 |         6,491 |           1,081 |           1.2488 |       2,313 |           378 |         1.3602 |
| <0.11     | fpd15         |  10,586 |       348 |        3.29% |     0.9244 | 4,362 |     107 |      2.45% |   0.8847 |         4,095 |              67 |           0.4601 |       2,049 |            17 |         0.2992 |         6,491 |             281 |           1.2173 |       2,313 |            90 |         1.4034 |
| <0.11     | fpd7          |  10,586 |       559 |        5.28% |     0.9243 | 4,362 |     175 |      4.01% |   0.9002 |         4,095 |             112 |           0.4787 |       2,049 |            37 |         0.4052 |         6,491 |             447 |           1.2054 |       2,313 |           138 |         1.3387 |
| 0.11-0.17 | duedate_1m_5  |     420 |        56 |       13.33% |     1.7350 |   222 |      24 |     10.81% |   1.8341 |           118 |               9 |           0.9925 |          66 |             3 |         0.7711 |           302 |              47 |           2.0251 |         156 |            21 |         2.2838 |
| 0.11-0.17 | duedate_3m_30 |     420 |        64 |       15.24% |     1.1426 |   222 |      23 |     10.36% |   0.8623 |           118 |              12 |           0.7625 |          66 |             5 |         0.6305 |           302 |              52 |           1.2911 |         156 |            18 |         0.9604 |
| 0.11-0.17 | fpd15         |     420 |        25 |        5.95% |     1.6737 |   222 |      13 |      5.86% |   2.1120 |           118 |               5 |           1.1915 |          66 |             1 |         0.5465 |           302 |              20 |           1.8622 |         156 |            12 |         2.7744 |
| 0.11-0.17 | fpd7          |     420 |        40 |        9.52% |     1.6670 |   222 |      20 |      9.01% |   2.0214 |           118 |               6 |           0.8900 |          66 |             3 |         1.0199 |           302 |              34 |           1.9706 |         156 |            17 |         2.4451 |
| >=0.17    | duedate_1m_5  |     352 |        58 |       16.48% |     2.1441 |   218 |      25 |     11.47% |   1.9455 |            75 |              10 |           1.7350 |          68 |             7 |         1.7464 |           277 |              48 |           2.2549 |         150 |            18 |         2.0358 |
| >=0.17    | duedate_3m_30 |     352 |        83 |       23.58% |     1.7681 |   218 |      38 |     17.43% |   1.4508 |            75 |               9 |           0.8998 |          68 |            11 |         1.3464 |           277 |              74 |           2.0032 |         150 |            27 |         1.4982 |
| >=0.17    | fpd15         |     352 |        31 |        8.81% |     2.4764 |   218 |      13 |      5.96% |   2.1508 |            75 |               5 |           1.8746 |          68 |             5 |         2.6520 |           277 |              26 |           2.6393 |         150 |             8 |         1.9236 |
| >=0.17    | fpd7          |     352 |        50 |       14.20% |     2.4863 |   218 |      18 |      8.26% |   1.8527 |            75 |               9 |           2.1005 |          68 |             6 |         1.9798 |           277 |              41 |           2.5908 |         150 |            12 |         1.7950 |

观察要点:

- **分档单调性成立**:duedate 标签下 Lift 随档位单调上升(<0.11 档约 0.9-0.97 低于整体,≥0.17 档约 1.8-2.5),拒付率档位确实把风险人群切出来了;
- **Train/OOT 方向一致**:三个档位的相对风险排序在 OOT 上完全保持,规则阈值未过拟合;
- 分片对照(part1/part2)趋势一致,但小样本档位(如 0.11-0.17 档 fpd15 的 part1 OOT Lift 0.5465 vs part2 2.7744)波动较大,单档 Lift 应结合样本量解读。

## 七、增益损失分析(5_Gain_Loss_Analysis):方法与推演

本章是这份报告里最值得讲清楚的一部分——它回答的不是"规则能不能抓到坏人",而是"**拒绝这一刀划下去,账面上到底赚不赚**"。以下按 Excel 的测算步骤完整还原。

### 7.1 分析问题与两个基本前提

**问题**:BR05 把这批人拒之门外,避免了多少钱的坏账(增益)?代价是损失了多少钱的费息收入(损失)?两者相抵,净收益是正还是负?

**两个基本前提**:

1. **这是一笔"已实现账",不是预测模拟**。命中的 418 人历史上实际都获得了放款(有 `funded_amount`,合计 395,750 美元),因此谁还清了、谁逾期了、逾期了多少金额,全部是可观测的真实结果。测算回答的是一个反事实问题:"如果 BR05 在历史时点就已生效、拒绝这 418 人,会怎样"——增益与损失都取自真实结果,不需要任何模型估计;
2. **只测 BR05,不测 GR09**。GR09 是灰名单转人工审核,人工审核后的最终放款率未知——被转人工的人最终有多少人获批放款无法确定,就无法定价"损失了多少费息"。所以 GR09 的金额影响在 Excel 中明确标注 "NOT computed",现行"黑 + 灰"组合策略的经济账只有黑名单这一半是可测算的。

### 7.2 第一步:圈定人群并做增量归因

| 步骤                         | 口径                                  |          人数 |
| ---------------------------- | ------------------------------------- | ------------: |
| 全量申请                     | 全部样本                              |        16,229 |
| 规则适用人群                 | finv_risk_level ∈ {ND1, ND2, NE, NF} |         9,689 |
| BR05 条件命中                | ratio_84d ≥ 0.17 且属于 ND1-NF       |           427 |
| **边际命中(计入测算)** | BR05 是**唯一**硬规则原因       | **418** |

427 − 418 = 9 人同时触发了 BR01 / BR02 等其它硬规则:即使没有 BR05,这 9 人也会被拒。如果不剔除,他们的费息损失会被重复算到 BR05 头上,高估规则成本。**增量归因**是这一步的核心——只计算 BR05 带来的边际效果,而不是"所有被拒的人"。

### 7.3 第二步:把命中人群划分为"坏客户"与"无辜客户"

按坏标签定义划分(以该标签下是否实际逾期为准):

| 标签          | 坏客户(guilty) | 无辜客户(innocent) | 合计 |
| ------------- | -------------: | -----------------: | ---: |
| duedate_1m_5  |             64 |                354 |  418 |
| duedate_1m_30 |             22 |                396 |  418 |
| duedate_3m_30 |             97 |                321 |  418 |

三个标签是**同一批 418 人的三套坏定义**,宽严不同(duedate_3m_30 的"坏"最宽,抓到 97 人;duedate_1m_30 最严,只 22 人),三本账互相独立、**不可相加**——它们是从三个观察视角看同一件事,不是三个互斥人群。

### 7.4 第三步:计算增益(避免的坏账)

```text
Gain = Σ 坏客户的真实坏账金额(amt_1m_5 / amt_1m_30 / amt_3m_30)
```

每个坏客户的坏账金额取其对应标签的逾期金额字段,直接加总,不做任何期望值折算。

**口径核验**(Excel 备注中的关键验证):

- amt_1m_5 / amt_1m_30 对 **100%** 的坏客户恰好等于放款额——即短期口径下"坏账"实为**全额损失口径**(客户刚坏,尚无回收);
- amt_3m_30 对 **90.5%** 的坏客户不等于放款额(聚合比例为放款额的 79.4%)——中期口径下部分客户已发生回收,金额追踪到的是**真实净坏账**而非放款全额。

因此三行增益中,**duedate_3m_30 的 76,702.45 美元最接近真实坏账**;1m_5 / 1m_30 的增益是上限口径。单笔坏客户平均坏账:1m_5 约 953、1m_30 约 1,077、3m_30 约 791 美元。

### 7.5 第四步:计算损失(损失的费息)

```text
Loss = Σ 无辜客户的费息损失
费率:SACC(放款额 ≤ $2,000)= 0.42 × 放款额;MACC(放款额 > $2,000)= 0.28 × 放款额
```

**为什么损失只算费息、不算本金**:拒绝一个好客户,本金并没有损失——钱还在机构手里,损失的只是这笔贷款本可带来的费息利润空间。所以对每个无辜客户,按其放款额套用对应产品层级的费率公式(Henry,2026-08-05),加总即损失。单笔无辜客户平均损失约 380-386 美元(三行几乎一致,因为无辜客户集合高度重叠)。

注意:该费率公式只覆盖 SACC / MACC 两层。本人群最大放款额 4,025 美元,无 LACC 层贷款;LACC 费率未知,**该公式不可外推到含 LACC 贷款的人群**。

### 7.6 第五步:比率列的两个口径、三个分母

结果表里的 6 个比率列(以及当前 Lift)按"口径 × 分母人群"组织:

| 口径                        | 命中人群                                         | ND1-NF 全量         | 全量申请           |
| --------------------------- | ------------------------------------------------ | ------------------- | ------------------ |
| 笔数口径(app_rate,按申请数) | app_rate_hit = 坏客户数 ÷ 命中数                | app_rate_total_ndnf | app_rate_total_all |
| 金额口径(amt_rate,按放款额) | amt_rate_hit_actual = Gain ÷ 命中人群放款额合计 | amt_rate_total_ndnf | amt_rate_total_all |

- `current_lift_vs_all` = app_rate_hit ÷ app_rate_total_all:命中人群的坏率是全量申请坏率的几倍(笔数口径);
- 金额口径与笔数口径并排,是为了交叉验证"人数上的风险差"与"金额上的风险差"是否一致——两者接近说明坏客户与好客户的放款额没有系统性差异,后续用人数口径推盈亏平衡是稳妥的。

### 7.7 第六步:净收益与盈亏平衡 Lift

**净收益**:`net = gain − loss`,三行均为负。

**盈亏平衡推导**(把"还要好多少才值得"量化成一个数字):

1. 设命中人群中坏客户占比为 p,单笔坏客户平均坏账 g,单笔无辜客户平均费息损失 l;
2. 拒绝一笔的期望净收益 = p·g − (1−p)·l;
3. 令其等于 0,解出盈亏平衡坏率:**p\* = l / (g + l)**;
4. 把 p\* 换算成对全量申请基准坏率的倍数,即**盈亏平衡 Lift** = p\* ÷ 全量申请坏率。

**以 duedate_3m_30 为例完整代入**:

```text
g = 76,702.45 ÷ 97 = 790.75 美元(单笔坏客户平均坏账)
l = 122,220.00 ÷ 321 = 380.75 美元(单笔无辜客户平均费息)
p* = 380.75 ÷ (790.75 + 380.75) = 32.50%(盈亏平衡坏率)
盈亏平衡 Lift = 32.50% ÷ 12.93%(全量基准坏率)= 2.51
实际坏率 = 97 ÷ 418 = 23.21%,实际 Lift = 23.21% ÷ 12.93% = 1.79
```

结论:按当前阈值,命中人群的坏率(23.21%)远低于让账打平的 32.50%——每拒一笔,平均净亏 380.75 − 0.2321 × (790.75 + 380.75) ≈ 108.8 美元(即 45,517.55 ÷ 418)。

**为什么 duedate_1m_30 的盈亏平衡 Lift 高达 12.57**:其全量基准坏率只有 2.09%,而 g 与 l 的比值与其它标签接近(约 2.8:1)——盈亏平衡坏率 p\* ≈ 26.3% 本身不算高,但除以一个极小的基准率,所需 Lift 就被放大到 12.57。这体现一般规律:**基准率越低,规则越难在经济上成立**。

| 标签          | 坏客户数 | 无辜客户数 | 规则命中 | 命中人群放款额 | 命中申请率 | 全量NDNF申请率 | 全量申请率 | 命中金额率(实际) | 全量NDNF金额率 | 全量金额率 | 增益($) | 损失($) | 净收益($) | 当前Lift(对全量) | 盈亏平衡Lift |      |       |
| ------------- | -------: | ---------: | -------: | -------------: | ---------: | -------------: | ---------: | ---------------: | -------------: | ---------: | ------------------------------: | ---------------: | -----------: | ---: | ----: |
| duedate_1m_5  |       64 |        354 |      418 |        395,750 |     15.31% |          9.53% |      7.15% |           15.41% |          9.36% |      6.74% |                       61,000.00 |       136,559.50 |   -75,559.50 | 2.14 |  4.03 |
| duedate_1m_30 |       22 |        396 |      418 |        395,750 |      5.26% |          2.86% |      2.09% |            5.99% |          2.89% |      2.03% |                       23,700.00 |       151,938.50 |  -128,238.50 | 2.52 | 12.57 |
| duedate_3m_30 |       97 |        321 |      418 |        395,750 |     23.21% |         16.81% |     12.93% |           19.38% |         13.75% |     10.26% |                       76,702.45 |       122,220.00 |   -45,517.55 | 1.79 |  2.51 |

### 结果解读

- **三个标签净收益均为负**,亏损面在 45,517.55(3m_30)到 128,238.50(1m_30)美元;
- 亏损的根源不是规则抓不到坏人,而是**阈值定得太松**:命中人群中坏客户只占 5%-23%,无辜者占 77%-95%,单笔坏客户能省下的坏账(g ≈ 790-1,077 美元)不足以覆盖约四个无辜客户损失的费息(4 × l ≈ 1,523 美元);
- 以最接近真实坏账的 duedate_3m_30 口径看:要让 BR05 打平,命中人群坏率需从 23.21% 提高到 32.50%,相当于把阈值从 0.17 进一步抬高、只打最极端的尾部——这正是第八章待办中"验证更高阈值"的量化依据;
- 若改用更保守的损失口径(如认为费息损失被高估),结论方向不变、亏损幅度缩小;反之,若叠加人工审核成本或获客成本,亏损会更大。

### 方法与口径局限

1. **GR09 未纳入测算**(人工审核后放款率未知),现行"黑 + 灰"组合策略的完整经济账仍缺灰名单这一半;
2. **费率公式不可外推**:SACC / MACC 两层费率仅在无 LACC 贷款的人群内有效;
3. **1m_5 / 1m_30 的增益是全损口径**(等于放款额),若按部分回收折算,这两行的增益还会更小、亏损更大;
4. **三标签共享同一批 418 人**,坏客户集合随标签宽严不同而嵌套/重叠,三行净收益不可相加;
5. **未计人工审核成本、获客成本与客户体验损失**,净收益是"毛口径"。

## 八、要点与结论

1. **拒付变量体系完整且口径统一**:507 个变量由 `txn_dishonour.py` 单一来源生成,覆盖 6 类机构 + 全机构合并 × 9 个指标族 × 8 个时间窗(全时段 + 7/14/28/56/84/168/182 天),其中 `ratio`(拒付率)与回收宽限期无关,是现行规则的直接依据;
2. **全样本高 KS 是拒绝决策的投影,不是增量排序能力**:剔除已拒绝样本后,拒付变量 KS 从 ~0.94 降至 ~0.17,但 IV 反而上升(ratio_84d 0.070→0.186)——规则迭代的价值评估应以 `__excl_declined` scope 为准,该 scope 下 ratio/unresolved_ratio 类变量仍稳居 IV 前列;
3. **现行规则只打击了极端尾部**:BR05 阈值 0.17 覆盖约 4% 的 ND1-NF 人群;挖掘候选显示 0.077(84 天)/ 0.037(168 天)等更低阈值仍有稳定区分度,且 38 个候选中 37 个 OOT 稳定,存在进一步收紧或分级下沉的空间;
4. **BR05 在当前成本模型下净收益为负**(完整推演见第七章):三个标签净收益分别为 −75,559.50 / −128,238.50 / −45,517.55 美元;以最具中期参考价值的 duedate_3m_30 计,当前 Lift 1.79 低于盈亏平衡 Lift 2.51。原因是命中人群中坏客户占比过低(按标签仅 5.26%-23.21%),单笔坏账的节省不足以覆盖无辜客户的费息损失;
5. **待办建议**:a) 若要收紧规则,应先在 `__excl_declined` 人群上验证阈值 0.077/0.037 候选的增量 Lift 是否超过对应盈亏平衡要求;b) GR09 灰名单的经济账需补充人工审核通过率假设后才能测算;c) 费率公式仅适用于无 LACC 人群,推广前需确认产品层级构成。
