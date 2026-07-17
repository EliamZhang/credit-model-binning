# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

本仓库是一个信贷风控方法论文档项目，核心文档为 [credit-model-binning-and-strategy-threshold.md](credit-model-binning-and-strategy-threshold.md)，涵盖：

- 模型分的风险分层与策略阈值设计
- 主流分箱方法（等频、等距、ChiMerge、最优分箱等）的对比与选型
- 从样本划分、细箱分析、累计阈值测算到三段式策略（通过/审核/拒绝）的完整落地流程
- EL、风险后收入、UE 等收益与风险口径的定义
- 阈值选择的四种业务目标（冲量、风险、利润、综合平衡）
- 上线后的统一配置管理与持续监控

推荐方案：**等频 20 箱 → 单调/统计合并 → 业务调整 → 收益阈值优化**。

## 文档风格

- 文档使用简体中文撰写
- 数学公式使用 LaTeX 语法（\( ... \) 行内，\[ ... \] 块级）
- 表格使用 GFM 格式
- 引用使用 `>` 块引用

## 编辑约定

- 修改文档时保持中文术语的一致性（如"坏账率"而非混用"违约率"，"通过率"而非"批准率"）
- 新增方法或章节时，遵循现有的编号层级结构（中文数字章 → 数字节）
- 参考资料使用编号列表，链接格式为 `[来源名称](URL)`
