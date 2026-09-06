# -*- coding: utf-8 -*-
"""
模型注册表：每个模型分一份配置。

维护指南（新增模型）：
1. 复制一份已有配置（如 mlt），改 key 与各字段；
2. 把模型分文件放入数据集的 data_dir（默认 res/）；
3. 经验方向检查：用老客/新客样本验证分数与 3M30+ 违约率的方向（见 scripts 运行后
   的 20 等频初分日志，或单独做十分位统计），确认 high_score_high_risk 取值；
4. 用 scripts/bin_model.py --dataset <dataset> --model <key> --metric cnt 试跑；
5. 评审报告后把最终方案（阈值/档位）记录进该模型的 comment 字段，便于交叉分析复用。

字段说明：
- score_file / raw_score_col：模型分文件名与文件内原始列名；
- score_col / initial_bin_col / final_bin_col：管线内部使用的列名（各模型保持唯一，
  交叉分析按这些列拼接）；
- high_score_high_risk：True = 高分高风险（分箱与阈值按此方向解释）；
- final_bin_ranges（可选）：手动指定合箱方案 [(起始初箱, 结束初箱), ...]；指定后跳过自动合箱
  评分，管线校验结构（连续覆盖 1..初始箱数、档数 6~8）与 Train 主指标单调性后直接采用；
- strategy_config：默认策略的风险约束（auto/accept 两段，累计 1M30+/3M30+ 与边际 3M30+ 上限）；
- report_prefix：笔数口径分箱报告 Excel 的输出前缀；
- report_prefix_amt：金额口径分箱报告 Excel 的输出前缀（未启用的口径可缺省）。
"""

MODELS = {
    "mlt": {
        "name": "mlt 主风险模型",
        "cross_tag": "mlt",
        "display_short": "mlt",
        "score_file": "old_mlt_score.csv",
        "raw_score_col": "aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score",
        "score_col": "score_mlt",
        "initial_bin_col": "score_mlt_bin20",
        "final_bin_col": "score_mlt_final_bin",
        "high_score_high_risk": True,
        "strategy_config": {
            "strategy_name": "默认策略",
            "objective": "平衡通过率、整体风险和边际风险",
            "auto_constraints": {
                "max_cum_1m30p_cnt_bad_rate": 0.0090,
                "max_cum_3m30p_cnt_bad_rate": 0.0550,
                "max_marginal_3m30p_cnt_bad_rate": 0.0900,
            },
            "accept_constraints": {
                "max_cum_1m30p_cnt_bad_rate": 0.0130,
                "max_cum_3m30p_cnt_bad_rate": 0.0750,
                "max_marginal_3m30p_cnt_bad_rate": 0.1700,
            },
        },
        "report_prefix": "binning_strategy_report",
        "report_prefix_amt": "binning_amt_strategy_report",
        # 报告生成器可选字段（供 scr/_gen_new_reports.py 使用）。
        "report_meta": {"report_name": "mlt 主风险模型", "md_name": "mlt", "value_model": False},
    },
    "worthiness": {
        "name": "价值模型",
        "cross_tag": "wth",
        "display_short": "价值模型",
        "score_file": "old_worthiness_score.csv",
        "raw_score_col": "aus_new_worthiness_bid_3rdmodel_v1_0_20260429",
        "score_col": "score_worthiness",
        "initial_bin_col": "score_worthiness_bin20",
        "final_bin_col": "score_worthiness_final_bin",
        "high_score_high_risk": True,
        # 风险方向经数据验证为高分高风险（与 mlt 一致）；价值语义为"低分 = 高价值"，
        # 两者不冲突：低分档同时是低风险 + 高价值档（见 docs/ 价值模型各报告）。
        "strategy_config": {
            "strategy_name": "默认策略",
            "objective": "平衡通过率、整体风险和边际风险",
            "auto_constraints": {
                "max_cum_1m30p_cnt_bad_rate": 0.0090,
                "max_cum_3m30p_cnt_bad_rate": 0.0550,
                "max_marginal_3m30p_cnt_bad_rate": 0.0900,
            },
            "accept_constraints": {
                "max_cum_1m30p_cnt_bad_rate": 0.0130,
                "max_cum_3m30p_cnt_bad_rate": 0.0750,
                "max_marginal_3m30p_cnt_bad_rate": 0.1700,
            },
        },
        "report_prefix": "binning_worthiness_strategy_report",
        # 报告生成器可选字段（供 scr/_gen_new_reports.py 使用）。
        "report_meta": {"report_name": "价值模型", "md_name": "价值", "value_model": True},
    },
    "new_mlt": {
        "name": "新客 mlt 主风险模型",
        "cross_tag": "new_mlt",
        "display_short": "新客mlt",
        "score_file": "new_mlt_score.csv",
        "raw_score_col": "aus_new_risk_bid_3rdmodel_v1_0_20251201",
        "score_col": "score_new_mlt",
        "initial_bin_col": "score_new_mlt_bin20",
        "final_bin_col": "score_new_mlt_final_bin",
        # 方向经 scripts/check_data.py 十分位方向验证通过（4.34%→35.65%，倒挂 0 处），
        # 高分高风险成立。分文件 -1.0 特殊值（6.61%，无银行交易数据人群兜底分）已按缺失分
        # 处理（scr/_mask_new_mlt_minus1.py 置空，2026-09-01 用户确认），缺失 42,575 笔。
        "high_score_high_risk": True,
        # 最终方案（2026-09-01 用户确认）：7 档 [(1,1),(2,4),(5,8),(9,10),(11,14),(15,19),(20,20)]，
        # 自动通过阈值 0.08716503179896717（A+B 档，Train 流量 20%），人工审核上限 0.1389779549508124
        # （C 档，总接纳 40%）；Train 3M30+ AUC 0.711 / PSI 0.0075；接纳人群 3M30+ 7.40%。
        # 策略约束沿用默认值（用户确认不再按新客数据校准）。
        "strategy_config": {
            "strategy_name": "默认策略",
            "objective": "平衡通过率、整体风险和边际风险",
            "auto_constraints": {
                "max_cum_1m30p_cnt_bad_rate": 0.0090,
                "max_cum_3m30p_cnt_bad_rate": 0.0550,
                "max_marginal_3m30p_cnt_bad_rate": 0.0900,
            },
            "accept_constraints": {
                "max_cum_1m30p_cnt_bad_rate": 0.0130,
                "max_cum_3m30p_cnt_bad_rate": 0.0750,
                "max_marginal_3m30p_cnt_bad_rate": 0.1700,
            },
        },
        "report_prefix": "binning_new_mlt_strategy_report",
        # 报告生成器可选字段（供 scr/_gen_new_reports.py 使用）；report_notes 为本次评审的
        # 运行事实叙事（逐字复刻原生成器调用实参），换数据/重跑后需按新运行结果更新。
        "report_meta": {"report_name": "mlt 主风险模型", "md_name": "mlt", "value_model": False},
        "report_notes": {
            "decile": "4.34% → 35.65%",
            "decile_inversion": "倒挂 0 处",
            "missing_note": "（4,588 笔为文件本身缺失，其余 37,987 笔为无银行交易数据人群的 −1.0 兜底分、按缺失分置空处理，2026-09-01 用户确认）",
            "merge_note": "自动合箱：小箱清理 → 单调合并 → 档位压缩 → 候选生成，最终选中 7 档方案（详见三（二））。",
            "dist_note": "分布整形拆分后可得到合规子箱（5.00% / 20.00%），但合回 7 档的唯一不超限合并 (1,1)+(2,4) 跨越极端箱边界 B01（默认禁止），其余相邻对合并后占比均重新超限，整形失败、原候选保留（详见三（四））",
            "steps_note": "前 6 步为小箱清理：初始 20 箱的 6 个单箱硬约束违反全部消除，主指标倒挂由 8 处降至 3 处；第 7–8 步为单调合并，消除全部剩余倒挂（降至 0）；第 9–12 步为档位压缩，将档位压缩至上限 8 档；第 13–14 步为候选生成，产出 7 档与 6 档候选。全过程未跨越极端箱边界。第 13 步生成的 7 档方案中 F 档（初始箱 15–19）Train 占比 25.00%，超过 21% 的人数分布上限：分布整形拆分为 (15,15) 与 (16,19)（5.00% / 20.00% 均不超限）后，合回 7 档的唯一不超限合并 (1,1)+(2,4)（20.00%）跨越极端箱边界 1（默认禁止），其余相邻对合并后占比均重新超限（35% / 30% / 30% / 25% / 25%），整形失败、原候选保留，属该口径下的合法结果（详见三（四））。",
            "cand_note": "三个 6–8 档候选均满足硬约束。自动选中的 7 档方案综合得分最高：IV 保留率 0.9661 介于 8 档（0.9728）与 6 档（0.9249）之间，档位数量最接近目标 7 档；8 档方案信息保留更高但档位复杂度更高，6 档方案信息损失最大。F 档（初始箱 15–19）Train 占比 25.00% 超过 21% 的人数分布上限，分布整形无可行回并方案、原候选保留（见三（三））。",
        },
    },
    "new_worthiness": {
        "name": "新客价值模型",
        "cross_tag": "new_wth",
        "display_short": "新客价值模型",
        "score_file": "new_worthiness_score.csv",
        "raw_score_col": "aus_new_worthiness_bid_3rdmodel_v1_0_20260429",
        "score_col": "score_new_worthiness",
        "initial_bin_col": "score_new_worthiness_bin20",
        "final_bin_col": "score_new_worthiness_final_bin",
        # 方向经 scripts/check_data.py 十分位方向验证通过（6.50%→39.26%，倒挂 0 处），
        # 高分高风险成立；价值语义"低分 = 高价值"与风险方向不冲突（低分档 = 低风险 + 高价值）。
        "high_score_high_risk": True,
        # 最终方案（2026-09-01 用户确认，手动指定 7 档）：[(1,1),(2,3),(4,4),(5,9),(10,12),(13,16),(17,20)]。
        # 自动通过阈值 0.1170685806554901（A 档，Train 流量 5%），人工审核上限 0.1933179021763764
        # （C 档，总接纳 20%）；A/B/C 三档与自动 6 档方案边界一致，阈值与策略不变；
        # 7 档 3M30+ IV 保留率 96.71%（自动 6 档 90.72%）、Train/OOT PSI 0.0065；
        # 拒绝段细化为 D~G 四档（3M30+ 12.83% / 20.35% / 27.60% / 37.81%），
        # G 档合并最坏极端箱 B20（极端边界跨越 1 处，消除 B20 小样本 1M30+ 倒挂，经用户确认）。
        # 策略约束沿用默认值（用户确认不再按新客数据校准）。
        "final_bin_ranges": [(1, 1), (2, 3), (4, 4), (5, 9), (10, 12), (13, 16), (17, 20)],
        "strategy_config": {
            "strategy_name": "默认策略",
            "objective": "平衡通过率、整体风险和边际风险",
            "auto_constraints": {
                "max_cum_1m30p_cnt_bad_rate": 0.0090,
                "max_cum_3m30p_cnt_bad_rate": 0.0550,
                "max_marginal_3m30p_cnt_bad_rate": 0.0900,
            },
            "accept_constraints": {
                "max_cum_1m30p_cnt_bad_rate": 0.0130,
                "max_cum_3m30p_cnt_bad_rate": 0.0750,
                "max_marginal_3m30p_cnt_bad_rate": 0.1700,
            },
        },
        "report_prefix": "binning_new_worthiness_strategy_report",
        # 报告生成器可选字段（供 scr/_gen_new_reports.py 使用）；report_notes 为本次评审的
        # 运行事实叙事（逐字复刻原生成器调用实参），换数据/重跑后需按新运行结果更新。
        "report_meta": {
            "report_name": "价值模型",
            "md_name": "价值",
            "value_model": True,
            "y_interest": {"raw_col": "raw_interest_income_3m", "threshold": "160", "label_col": "y_interest_income_3m"},
        },
        "report_notes": {
            "decile": "6.50% → 39.26%",
            "decile_inversion": "倒挂 0 处",
            "missing_note": "（均为无银行交易数据人群，与老客价值模型缺失口径一致，2026-09-01 用户确认）",
            "merge_note": "手动指定（模型配置 final_bin_ranges，2026-09-01 用户确认）：自动合箱在该口径下选中 6 档 [(1,1),(2,3),(4,4),(5,9),(10,19),(20,20)]（Train 主指标与全指标倒挂 0 处、箱级约束违规 2 项）；经评审改为手动 7 档，将 (10,19) 拆为 (10,12)+(13,16) 并与 (17,20) 合并，消除 7/8 档候选残留的 B20 单箱倒挂——校验结果 Train 主指标倒挂 0 处、极端边界跨越 1 处（边界 19，经用户确认）、箱级约束违规 1 项（C 档占比 4.9998% 略低于中间箱 5% 下限，与自动 6 档方案同性质），A/B/C 三档边界与阈值不受影响。",
            "dist_note": "自动 6 档方案的 E 档（B10–B19）50.00% 超限更严重且无可行拆分点；最终手动 7 档方案已按用户确认采用（详见三（四））",
            "steps_note": "第 1 步为小箱清理（合并 B18+B19）；第 2–12 步为档位压缩（19 档 → 8 档），主指标倒挂由初始 7 处降至 1 处、箱级约束违规由初始 10 项降至 2 项；第 13–14 步为候选生成，产出 7 档与 6 档候选。自动合箱全过程未跨越极端箱边界（最终手动方案的边界 19 跨越另见三（二））。7/8 档候选残留 1 处主指标倒挂（该结构均含最坏极端箱 B20 单箱，见候选表 ranges 列）、6 档候选无倒挂（详见三（四））；最终方案按用户确认的手动 7 档执行，将 (10,19) 拆为 (10,12)+(13,16) 并与 (17,20) 合并，消除 B20 单箱倒挂（详见三（二））。",
            "cand_note": "该口径下自动合箱的 6–8 档候选均未完全满足硬约束（7/8 档候选主指标倒挂 1 处、6 档候选无倒挂；箱级约束违规 1–2 项，含 C 档占比 4.9998% 略低于中间箱 5% 下限），自动选中综合得分最高的 6 档方案（倒挂 0 处、违规 2 项）。经评审改为手动 7 档方案（模型配置 final_bin_ranges）：将 (10,19) 拆为 (10,12)+(13,16) 并与 (17,20) 合并，消除 B20 单箱倒挂、违规降至 1 项（C 档同性质，2026-09-01 用户确认，详见三（二））。",
        },
    },
}

REQUIRED_MODEL_KEYS = [
    "name",
    "cross_tag",
    "score_file",
    "raw_score_col",
    "score_col",
    "initial_bin_col",
    "final_bin_col",
    "high_score_high_risk",
    "strategy_config",
    "report_prefix",
]
