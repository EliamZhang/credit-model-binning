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

[scr/application_info_extract.sql](scr/application_info_extract.sql) 从 `ba.customer_profile_rawdata` 提取申请信息字段，筛选 `application_time >= '2025-01-01'`。

## 文档风格

- 文档使用简体中文撰写
- 数学公式使用 LaTeX 语法（`\( ... \)` 行内，`\[ ... \]` 块级）
- 表格使用 GFM 格式，引用使用 `>` 块引用

## 编辑约定

- 保持中文术语一致（"坏账率"非"违约率"，"通过率"非"批准率"）
- 新增章节遵循现有编号层级（中文数字章 → 数字节）
- 参考资料使用编号列表，格式 `[来源名称](URL)`
- 文件/列名使用英文小写下划线命名
