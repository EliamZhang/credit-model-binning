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
        "score_file": "aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv",
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
        "score_file": "aus_new_worthiness_bid_3rdmodel_v1_0_20260429.csv",
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
