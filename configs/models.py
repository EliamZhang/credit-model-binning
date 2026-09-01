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
    },
    "xinke_mlt": {
        "name": "新客 mlt 主风险模型",
        "cross_tag": "xke_mlt",
        "display_short": "新客mlt",
        "score_file": "xinke_mlt_score.csv",
        "raw_score_col": "aus_new_risk_bid_3rdmodel_v1_0_20251201",
        "score_col": "score_xinke_mlt",
        "initial_bin_col": "score_xinke_mlt_bin20",
        "final_bin_col": "score_xinke_mlt_final_bin",
        # 方向经 scripts/check_data.py 十分位方向验证通过（4.34%→35.65%，倒挂 0 处），
        # 高分高风险成立。分文件 -1.0 特殊值（6.61%，无银行交易数据人群兜底分）已按缺失分
        # 处理（scr/_mask_xinke_mlt_minus1.py 置空，2026-09-01 用户确认），缺失 42,575 笔。
        "high_score_high_risk": True,
        # 最终方案（2026-09-01 用户确认）：7 档 [(1,1),(2,4),(5,8),(9,10),(11,14),(15,19),(20,20)]，
        # 自动通过阈值 0.08716503179896717（A+B 档，Train 流量 20%），人工审核上限 0.1389779549508124
        # （C 档，总接纳 40%）；Train 3M30+ AUC 0.711 / PSI 0.0075；接纳人群 3M30+ 7.40%。
        # 策略约束沿用默认值（用户确认不再按新客数据校准）。
        # 策略约束先沿用默认值；新客分箱完成后需按新客数据重新校准并与用户确认。
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
        "report_prefix": "binning_xinke_mlt_strategy_report",
    },
    "xinke_worthiness": {
        "name": "新客价值模型",
        "cross_tag": "xke_wth",
        "display_short": "新客价值模型",
        "score_file": "xinke_worthiness_score.csv",
        "raw_score_col": "aus_new_worthiness_bid_3rdmodel_v1_0_20260429",
        "score_col": "score_xinke_worthiness",
        "initial_bin_col": "score_xinke_worthiness_bin20",
        "final_bin_col": "score_xinke_worthiness_final_bin",
        # 方向经 scripts/check_data.py 十分位方向验证通过（6.50%→39.26%，倒挂 0 处），
        # 高分高风险成立；价值语义"低分 = 高价值"与风险方向不冲突（低分档 = 低风险 + 高价值）。
        "high_score_high_risk": True,
        # 最终方案（2026-09-01 用户确认）：6 档 [(1,1),(2,3),(4,4),(5,9),(10,19),(20,20)]，
        # 自动通过阈值 0.1170685806554901（A 档，Train 流量 5%），人工审核上限 0.1933179021763764
        # （C 档，总接纳 20%）；Train 3M30+ AUC 0.670 / PSI 0.0055；接纳人群 3M30+ 7.47%。
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
        "report_prefix": "binning_xinke_worthiness_strategy_report",
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
