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
| --- | --- | --- |
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

## 风险指标公式

### Vintage 逾期标签

`application_info.csv` 中实际存在的标签字段：

| 字段 | 含义 |
| --- | --- |
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
| --- | --- |
| `principal` | 贷款本金 |
| `estimate_principal_remaining_mob{n}` | MOB{n} 时点的剩余本金（n=0~4） |
| `dpd_days_mob{n}` | MOB{n} 时点的逾期天数（n=0~4） |
| `dpd_days_ever_mob{n}` | MOB{n} 时点的历史最大逾期天数（n=0~4） |

### 标签定义

脚本使用两个独立标签，分别服务于不同口径的坏账率计算：

| 标签 | 数据列 | 判断逻辑 | 用途 | 适用指标 |
| --- | --- | --- | --- | --- |
| 笔数坏账标签 | `duedate_3m_30` | MOB3 是否逾期 ≥ 30 天（0/1/NULL） | 分箱逻辑 | 笔数坏账率、AUC、KS、Spearman ρ、ChiMerge 合并判断 |
| 金额坏账标签 | `dpd_days_ever_mob3` | MOB3 内历史最大逾期天数 ≥ 30 时记为 1 | 展示 | 金额坏账率（累计 / 分段 / 方案） |

> 两个标签的数据来源和业务含义不同，不可互换。`duedate_3m_30` 反映**标的逾期**（第三期到期的逾期状态），`dpd_days_ever_mob3 >= 30` 反映**资产逾期**（MOB3 内是否曾逾期 ≥ 30 天），后者更匹配金额口径的风险敞口。

ChiMerge 合并时同时检验两个标签的卡方 p 值：`CHIMERGE_LABEL_COLS = ["duedate_3m_30", "duedate_1m_30"]`，相邻箱必须在两个标签上均不显著（p ≥ 0.05）才允许合并。

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
