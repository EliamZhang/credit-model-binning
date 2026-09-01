# -*- coding: utf-8 -*-
"""
管线全局设置：合箱/策略的所有常量集中于此，默认值 = mlt 笔数口径（老客）的既有取值。

运行机制（供人/AI 维护者理解）：
- 各 pipeline 模块在模块顶部 `import pipeline.settings as settings`，
  并通过 `_sync_settings()` 把 settings 的常量刷入本模块全局命名空间；
- 入口脚本先调用 settings.apply_dataset() / apply_model() 修改常量，
  再调用各模块的 _sync_settings()，随后执行管线；
- 函数体内对常量（如 SCORE_COL、MAX_FINAL_BIN_SHARE）的裸引用因此保持可读，
  且默认值即老客 mlt 笔数口径的冻结行为，重跑结果与历史报告完全一致。

新增模型/样本时通常不需要改本文件：模型相关字段由 configs/models.py 注入，
样本相关字段由 configs/datasets.py 注入。
"""

import time
from pathlib import Path

DATA_DIR = Path("res")
OUT_DIR = Path("out")
REPORT_PATH = OUT_DIR / f"binning_strategy_report_{time.strftime('%Y%m%d')}.xlsx"

SAMPLE_FILE = "old_sample.csv"
APPLICATION_FILE = "old_application_info.csv"
SCORE_FILE = "old_mlt_score.csv"

RAW_SCORE_COL = "aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score"
SCORE_COL = "score_mlt"

# 未完成申请状态：加载时整体剔除，不进入历史漏斗、分箱与策略测算。
INCOMPLETE_STATUSES = ["0.Incomplete", "1.In Progress"]

TRAIN_END_MONTH = "2025-10"
OOT_START_MONTH = "2025-11"

INITIAL_BIN_COUNT = 20
INITIAL_BIN_COL = "score_mlt_bin20"
FINAL_BIN_COL = "score_mlt_final_bin"

# 当前模型按"高分高风险"处理。
HIGH_SCORE_HIGH_RISK = True

# 最终风险档位数量。
MIN_FINAL_BIN_COUNT = 6
MAX_FINAL_BIN_COUNT = 8
TARGET_FINAL_BIN_COUNT = 7

# 合箱主指标：同时监控 1M30+ 和 3M30+ 笔数逾期率。
PRIMARY_RATE_COLS = ["1m30p_cnt_bad_rate", "3m30p_cnt_bad_rate"]
PRIMARY_RATE_COL = "3m30p_cnt_bad_rate"  # 保留单列引用，用于 p-value 等需要主锚定指标的场景
PRIMARY_MATURE_COL = "3m30p_cnt_mature"
PRIMARY_BAD_COL = "3m30p_cnt_bad"
PRIMARY_GOOD_COL = "3m30p_cnt_good"

# 最终箱约束。尾部箱允许更小，但仍需满足成熟量和好坏样本量。
MIN_MIDDLE_BIN_SAMPLE_PCT = 0.05
MIN_TAIL_BIN_SAMPLE_PCT = 0.025
MIN_FINAL_BIN_MATURE_COUNT = 1000
MIN_FINAL_BIN_BAD_COUNT = 20
MIN_FINAL_BIN_GOOD_COUNT = 200
MIN_EXTREME_BIN_MATURE_COUNT = 500
MIN_BEST_EXTREME_BIN_BAD_COUNT = 0
MIN_BEST_EXTREME_BIN_GOOD_COUNT = 200
MIN_WORST_EXTREME_BIN_BAD_COUNT = 20
MIN_WORST_EXTREME_BIN_GOOD_COUNT = 0

# 单调与相邻差异控制。
TRAIN_INVERSION_TOLERANCE = 0.0
MONTHLY_INVERSION_TOLERANCE = 0.003
ADJACENT_PVALUE_TO_MERGE = 0.10
MIN_ADJACENT_ABS_RATE_DIFF = 0.003

# 单箱样本占比上限（人数分布控制）：最终任意一档的 Train 样本占比不得超过该值，
# 超限的候选方案会被自动做"均衡拆分 + 相邻再合并"整形后重新参与评分。
MAX_FINAL_BIN_SHARE = 0.21

# 策略关键边界保护。强制处理小箱或倒挂时仍允许跨越保护边界。
PROTECT_LARGEST_RISK_JUMPS = 1
PROTECTED_BOUNDARY_PENALTY = 100.0

# 极端人群圈选：默认保留最好和最坏各 1 个初始等频箱，不让常规合箱跨越。
PROTECT_EXTREME_INITIAL_BINS = True
BEST_EXTREME_INITIAL_BIN_COUNT = 1
WORST_EXTREME_INITIAL_BIN_COUNT = 1
ALLOW_EXTREME_BIN_MERGE_FOR_HARD_CONSTRAINTS = False
EXTREME_BOUNDARY_PENALTY = 10000.0
EXTREME_BOUNDARY_VIOLATION_PENALTY = 50.0

# 候选合箱评分权重。
MERGE_COST_RATE_GAP_WEIGHT = 100.0
MERGE_COST_IV_LOSS_WEIGHT = 10.0
IV_RETENTION_SCORE_CAP = 1.5
CANDIDATE_SCORE_WEIGHTS = {
    "hard_constraints_ok": 100.0,
    "train_primary_inversion": -30.0,
    "train_all_inversion": -4.0,
    "constraint_violation": -15.0,
    "iv_retention": 12.0,
    "min_adjacent_rate_diff": 100.0,
    "target_bin_distance": -1.5,
    "extreme_boundary_violation": -EXTREME_BOUNDARY_VIOLATION_PENALTY,
}

# 指标计算平滑项。
PSI_EPS = 1e-6
IV_SMOOTHING_EPS = 0.5

# 默认策略的风险约束。
STRATEGY_CONFIG = {
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
}

RISK_NUMERIC_COLS = [
    SCORE_COL,
    "duedate_1m_30",
    "duedate_3m_30",
    "principal",
    "estimate_principal_remaining_mob1",
    "estimate_principal_remaining_mob3",
    "dpd_days_ever_mob1",
    "dpd_days_ever_mob3",
]

REQUIRED_ANALYSIS_COLS = [
    "application_id",
    "user_id",
    "application_time",
    "application_month",
    SCORE_COL,
    "duedate_1m_30",
    "duedate_3m_30",
    "principal",
    "estimate_principal_remaining_mob1",
    "estimate_principal_remaining_mob3",
    "dpd_days_ever_mob1",
    "dpd_days_ever_mob3",
    # 历史实际审批漏斗字段，均来自 application_info.csv。
    "status",
    "application_status",
    "assessment_status",
]

# 风险指标统一配置：避免 1M30+ / 3M30+ 在多个函数中重复写同一套逻辑。
RISK_PREFIXES = ("1m30p", "3m30p")
ALL_RISK_RATE_COLS = [
    "1m30p_cnt_bad_rate",
    "3m30p_cnt_bad_rate",
    "1m30p_amt_bad_rate",
    "3m30p_amt_bad_rate",
]
RISK_HELPER_CONFIG = {
    "1m30p": {
        "due_col": "duedate_1m_30",
        "dpd_col": "dpd_days_ever_mob1",
        "remaining_col": "estimate_principal_remaining_mob1",
        "helper_prefix": "_m1",
    },
    "3m30p": {
        "due_col": "duedate_3m_30",
        "dpd_col": "dpd_days_ever_mob3",
        "remaining_col": "estimate_principal_remaining_mob3",
        "helper_prefix": "_m3",
    },
}

# 金额口径专属列（bin_amt 使用）：金额加权的坏样本/敞口/好样本口径。
PRIMARY_AMT_BAD_COL = "3m30p_amt_bad"
PRIMARY_AMT_EXPOSURE_COL = "3m30p_amt_exposure"
PRIMARY_AMT_GOOD_COL = "3m30p_amt_good"

# 金额口径下的主指标与策略约束（apply_metric("amt") 时启用）。
AMT_PRIMARY_RATE_COLS = ["1m30p_amt_bad_rate", "3m30p_amt_bad_rate"]
AMT_PRIMARY_RATE_COL = "3m30p_amt_bad_rate"
AMT_STRATEGY_CONFIG = {
    "strategy_name": "默认策略（金额口径）",
    "objective": "平衡通过率、整体风险和边际风险（金额逾期率口径）",
    "auto_constraints": {
        "max_cum_1m30p_amt_bad_rate": 0.0054,
        "max_cum_3m30p_amt_bad_rate": 0.0390,
        "max_marginal_3m30p_amt_bad_rate": 0.0760,
    },
    "accept_constraints": {
        "max_cum_1m30p_amt_bad_rate": 0.0110,
        "max_cum_3m30p_amt_bad_rate": 0.0597,
        "max_marginal_3m30p_amt_bad_rate": 0.1500,
    },
}

# 以上常量名清单：各 pipeline 模块的 _sync_settings() 据此把 settings 刷入模块全局。
CONSTANT_NAMES = [
    "DATA_DIR",
    "OUT_DIR",
    "REPORT_PATH",
    "SAMPLE_FILE",
    "APPLICATION_FILE",
    "SCORE_FILE",
    "RAW_SCORE_COL",
    "SCORE_COL",
    "INCOMPLETE_STATUSES",
    "TRAIN_END_MONTH",
    "OOT_START_MONTH",
    "INITIAL_BIN_COUNT",
    "INITIAL_BIN_COL",
    "FINAL_BIN_COL",
    "HIGH_SCORE_HIGH_RISK",
    "MIN_FINAL_BIN_COUNT",
    "MAX_FINAL_BIN_COUNT",
    "TARGET_FINAL_BIN_COUNT",
    "PRIMARY_RATE_COLS",
    "PRIMARY_RATE_COL",
    "PRIMARY_MATURE_COL",
    "PRIMARY_BAD_COL",
    "PRIMARY_GOOD_COL",
    "MIN_MIDDLE_BIN_SAMPLE_PCT",
    "MIN_TAIL_BIN_SAMPLE_PCT",
    "MIN_FINAL_BIN_MATURE_COUNT",
    "MIN_FINAL_BIN_BAD_COUNT",
    "MIN_FINAL_BIN_GOOD_COUNT",
    "MIN_EXTREME_BIN_MATURE_COUNT",
    "MIN_BEST_EXTREME_BIN_BAD_COUNT",
    "MIN_BEST_EXTREME_BIN_GOOD_COUNT",
    "MIN_WORST_EXTREME_BIN_BAD_COUNT",
    "MIN_WORST_EXTREME_BIN_GOOD_COUNT",
    "TRAIN_INVERSION_TOLERANCE",
    "MONTHLY_INVERSION_TOLERANCE",
    "ADJACENT_PVALUE_TO_MERGE",
    "MIN_ADJACENT_ABS_RATE_DIFF",
    "MAX_FINAL_BIN_SHARE",
    "PROTECT_LARGEST_RISK_JUMPS",
    "PROTECTED_BOUNDARY_PENALTY",
    "PROTECT_EXTREME_INITIAL_BINS",
    "BEST_EXTREME_INITIAL_BIN_COUNT",
    "WORST_EXTREME_INITIAL_BIN_COUNT",
    "ALLOW_EXTREME_BIN_MERGE_FOR_HARD_CONSTRAINTS",
    "EXTREME_BOUNDARY_PENALTY",
    "EXTREME_BOUNDARY_VIOLATION_PENALTY",
    "MERGE_COST_RATE_GAP_WEIGHT",
    "MERGE_COST_IV_LOSS_WEIGHT",
    "IV_RETENTION_SCORE_CAP",
    "CANDIDATE_SCORE_WEIGHTS",
    "PSI_EPS",
    "IV_SMOOTHING_EPS",
    "STRATEGY_CONFIG",
    "RISK_NUMERIC_COLS",
    "REQUIRED_ANALYSIS_COLS",
    "RISK_PREFIXES",
    "ALL_RISK_RATE_COLS",
    "RISK_HELPER_CONFIG",
    "PRIMARY_AMT_BAD_COL",
    "PRIMARY_AMT_EXPOSURE_COL",
    "PRIMARY_AMT_GOOD_COL",
]


# 当前合箱口径标记（apply_metric 设置；编排模块据此在 cnt/amt 实现间分发）。
CURRENT_METRIC = "cnt"


def apply_metric(metric: str) -> None:
    """切换合箱口径：cnt = 笔数口径（默认），amt = 金额口径。

    调用顺序约定：apply_dataset → apply_model → apply_metric。
    amt 口径的策略约束为金额率上限（AMT_STRATEGY_CONFIG）；cnt 口径沿用
    apply_model 注入的模型策略约束（STRATEGY_CONFIG 不动）。
    """
    globals()["CURRENT_METRIC"] = metric
    if metric == "amt":
        globals()["PRIMARY_RATE_COLS"] = AMT_PRIMARY_RATE_COLS
        globals()["PRIMARY_RATE_COL"] = AMT_PRIMARY_RATE_COL
        globals()["STRATEGY_CONFIG"] = AMT_STRATEGY_CONFIG
    else:
        globals()["PRIMARY_RATE_COLS"] = ["1m30p_cnt_bad_rate", "3m30p_cnt_bad_rate"]
        globals()["PRIMARY_RATE_COL"] = "3m30p_cnt_bad_rate"


def apply_dataset(dataset_cfg: dict) -> None:
    """把数据集配置注入 settings（在 _sync_settings 之前调用）。"""
    globals()["DATA_DIR"] = Path(dataset_cfg["data_dir"])
    globals()["SAMPLE_FILE"] = dataset_cfg["sample_file"]
    globals()["APPLICATION_FILE"] = dataset_cfg["application_file"]
    globals()["TRAIN_END_MONTH"] = dataset_cfg["train_end_month"]
    globals()["OOT_START_MONTH"] = dataset_cfg["oot_start_month"]
    globals()["INCOMPLETE_STATUSES"] = list(dataset_cfg["incomplete_statuses"])


def apply_model(model_cfg: dict) -> None:
    """把模型配置注入 settings（在 _sync_settings 之前调用）。"""
    globals()["SCORE_FILE"] = model_cfg["score_file"]
    globals()["RAW_SCORE_COL"] = model_cfg["raw_score_col"]
    globals()["SCORE_COL"] = model_cfg["score_col"]
    globals()["INITIAL_BIN_COL"] = model_cfg["initial_bin_col"]
    globals()["FINAL_BIN_COL"] = model_cfg["final_bin_col"]
    globals()["HIGH_SCORE_HIGH_RISK"] = model_cfg["high_score_high_risk"]
    globals()["STRATEGY_CONFIG"] = model_cfg["strategy_config"]
    # RISK_NUMERIC_COLS / REQUIRED_ANALYSIS_COLS 中引用 SCORE_COL 的取值随之更新。
    globals()["RISK_NUMERIC_COLS"] = [model_cfg["score_col"]] + RISK_NUMERIC_COLS[1:]
    globals()["REQUIRED_ANALYSIS_COLS"] = [
        model_cfg["score_col"] if col == "score_mlt" else col
        for col in REQUIRED_ANALYSIS_COLS
    ]


def sync(module_globals: dict) -> None:
    """把 settings 的常量刷入调用模块的全局命名空间。"""
    for name in CONSTANT_NAMES:
        module_globals[name] = globals()[name]
