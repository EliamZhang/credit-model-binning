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
        # 报告生成器渲染清单与数据集级叙事（可选字段，供 scr/_gen_new_reports.py 使用）。
        "report_meta": {
            "models": ["mlt", "worthiness"],  # 笔数口径单模型（按此顺序渲染）
            "amt_models": ["mlt"],  # 金额口径单模型（可选）
            "cross_pairs": [("mlt", "worthiness")],
            "generator": "scr/_gen_new_reports.py",
        },
    },
    "new": {
        "name": "新客",
        "data_dir": "res",
        "sample_file": "new_sample.csv",
        "application_file": "new_application_info.csv",
        "train_end_month": "2025-10",
        "oot_start_month": "2025-11",
        "incomplete_statuses": ["0.Incomplete", "1.In Progress"],
        "value_semantics": "新客价值模型口径：分数越低价值越高（见 docs/价值评估_新客_0520.html）",
        # 报告生成器渲染清单与数据集级叙事（可选字段，供 scr/_gen_new_reports.py 使用）。
        # report_notes 数值插值占位符：{n_miss_a}/{pct_a}/{n_miss_b}/{pct_b}/{pearson}。
        "report_meta": {
            "models": ["new_mlt", "new_worthiness"],  # 笔数口径单模型（按此顺序渲染）
            "cross_pairs": [("new_mlt", "new_worthiness")],
            "generator": "scr/_gen_new_reports.py",
        },
        "report_notes": {
            "maturity_note": "新客 Train 3M30+ 标签成熟率约 10.71%（成交样本才有 duedate 表现标签，新客完成申请成交率约 12%，属结构性口径，2026-09-01 已与用户确认记录在案）",
            "cross_short_a": "mlt",
            "cross_short_b": "wth",
            "cross_missing_note": "mlt 缺失 {n_miss_a} 笔（{pct_a}，含无银行交易数据人群的 −1.0 兜底分置空，2026-09-01 用户确认）、价值模型缺失 {n_miss_b} 笔（{pct_b}，无银行交易数据人群）",
            "value_note_single": "价值语义说明：价值模型的本义为\"低分 = 高价值\"（新客价值模型文档口径：分数越低，利息贡献越高，见 docs/价值评估_新客_0520.html）；价值模型分与 mlt 主模型分在双分样本上的 Pearson 相关为 {pearson}（见《两模型交叉效果评估报告（新客mlt × 新客价值模型）》）。因此本报告的 A 档（最低分）同时是\"高价值 + 低风险\"档，G 档（最高分）同时是\"高风险 + 低价值\"档；风险类结论不受该语义影响，经营/提额类场景（优先经营象限、额度分层）需结合该语义使用。",
            "value_note_cross": "价值模型分为\"低分 = 高价值\"（分数越低，利息贡献越高，见 docs/价值评估_新客_0520.html）。因此价值档 A（最低分）同时是\"高价值 + 低风险\"档；下文\"价值 ≤ C\"即\"价值好（低分）人群\"。",
        },
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
