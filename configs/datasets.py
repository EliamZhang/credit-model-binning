# -*- coding: utf-8 -*-
"""
数据集注册表：每个样本集一份配置。

维护指南（新增样本集）：
1. 复制一份已有配置（如 laoke），改 key 与各字段；
2. 把样本数据文件放入 data_dir（默认 res/）；
3. 用 scripts/bin_model.py --dataset <key> --model mlt --metric cnt 试跑；
4. 输出 Excel 后人工核对样本量、月份切分是否符合预期；
5. 若需配套报告，参照 docs/ 下既有报告撰写，并用 scr/ 下核对脚本验证数值。

字段说明：
- data_dir / sample_file / application_file：样本底表、申请信息表路径；
- train_end_month / oot_start_month：Train 截止月 / OOT 起始月（YYYY-MM 字符串比较口径）；
- incomplete_statuses：未完成申请状态值（加载时整体剔除）；
- value_semantics：可选说明字段，记录该样本下模型分的业务语义（仅文档用途）。
"""

DATASETS = {
    "laoke": {
        "name": "老客",
        "data_dir": "res",
        "sample_file": "old_sample.csv",
        "application_file": "old_application_info.csv",
        "train_end_month": "2025-10",
        "oot_start_month": "2025-11",
        "incomplete_statuses": ["0.Incomplete", "1.In Progress"],
        "value_semantics": "价值模型分越低 = 价值越高（利息贡献越高），高分 = 高风险（经验验证）",
    },
    "new": {
        "name": "新客",
        "data_dir": "res",
        "sample_file": "new_sample.csv",
        "application_file": "new_application_info.csv",
        "train_end_month": "2025-10",
        "oot_start_month": "2025-11",
        "incomplete_statuses": ["0.Incomplete", "1.In Progress"],
        "value_semantics": "新客价值模型口径：分数越低价值越高（见 docs/新客价值模型效果评估文档_0520.html）",
    },
}

REQUIRED_DATASET_KEYS = [
    "name",
    "data_dir",
    "sample_file",
    "application_file",
    "train_end_month",
    "oot_start_month",
    "incomplete_statuses",
]
