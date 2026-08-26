# -*- coding: utf-8 -*-
"""策略阈值与流量测算：阈值曲线、自动通过/总接纳阈值选择、敏感性、三段验证与测算流量。

本模块由旧脚本机械迁移生成：函数体与原实现逐行一致，常量经 settings 同步注入。
新增/修改逻辑时请同步更新对应报告与测试。
"""
import ast
import math
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import pipeline.settings as settings


def _sync_settings() -> None:
    """把 pipeline.settings 的常量刷入本模块全局命名空间（入口脚本在换模型/样本后调用）。"""
    settings.sync(globals())


_sync_settings()


from pipeline.binning_cnt import classify_extreme_role
from pipeline.common import remove_prefix, require_columns, safe_div, wilson_ci
from pipeline.risk_metrics import calc_portfolio_metrics, calc_segment_metrics, prefix_metrics



def compute_auto_accept_rows(
    curve: pd.DataFrame,
    config: Dict,
) -> Tuple[Optional[pd.Series], Optional[pd.Series]]:
    """按自动通过 / 整体接纳约束选择阈值行；接纳阈值不能比自动通过更严格。"""
    auto_row = select_threshold_under_constraints(curve, config["auto_constraints"])
    accept_row = select_threshold_under_constraints(curve, config["accept_constraints"])
    if auto_row is None or accept_row is None:
        return auto_row, accept_row
    if accept_row["threshold_order"] < auto_row["threshold_order"]:
        accept_row = auto_row
    return auto_row, accept_row
def build_threshold_curve(
    train: pd.DataFrame,
    final_edges: pd.DataFrame,
) -> pd.DataFrame:
    """使用最终箱右边界生成可上线的候选阈值曲线。"""
    max_score = train[SCORE_COL].max()
    threshold_table = final_edges[
        [
            "final_bin_order",
            FINAL_BIN_COL,
            "score_right",
            "merged_from",
            "extreme_bin_role",
        ]
    ].copy()
    threshold_table["threshold"] = threshold_table["score_right"].replace(np.inf, max_score)
    threshold_table = threshold_table.loc[threshold_table["threshold"].notna()].copy()
    threshold_table = threshold_table.sort_values("final_bin_order").reset_index(drop=True)

    total_n = len(train)
    total_principal = train["principal"].fillna(0).sum()
    score = train[SCORE_COL]

    rows = []
    previous_threshold: Optional[float] = None

    for threshold_order, threshold_row in threshold_table.iterrows():
        threshold = float(threshold_row["threshold"])

        if HIGH_SCORE_HIGH_RISK:
            cumulative_mask = score.le(threshold)
            marginal_mask = (
                cumulative_mask
                if previous_threshold is None
                else score.gt(previous_threshold) & score.le(threshold)
            )
        else:
            cumulative_mask = score.ge(threshold)
            marginal_mask = (
                cumulative_mask
                if previous_threshold is None
                else score.lt(previous_threshold) & score.ge(threshold)
            )

        cumulative_metrics = calc_portfolio_metrics(train.loc[cumulative_mask])
        marginal_metrics = calc_portfolio_metrics(train.loc[marginal_mask])

        row = {
            "threshold_order": threshold_order + 1,
            "threshold": threshold,
            "prev_threshold": previous_threshold,
            "final_bin_order": threshold_row["final_bin_order"],
            FINAL_BIN_COL: threshold_row[FINAL_BIN_COL],
            "merged_from": threshold_row["merged_from"],
            "extreme_bin_role": threshold_row["extreme_bin_role"],
        }
        row.update(prefix_metrics(cumulative_metrics, "cum"))
        row.update(prefix_metrics(marginal_metrics, "marginal"))
        for prefix in RISK_PREFIXES:
            cum_ci_low, cum_ci_high = wilson_ci(
                cumulative_metrics[f"{prefix}_cnt_bad"],
                cumulative_metrics[f"{prefix}_cnt_mature"],
            )
            row[f"cum_{prefix}_cnt_bad_rate_ci_low"] = cum_ci_low
            row[f"cum_{prefix}_cnt_bad_rate_ci_high"] = cum_ci_high
            marginal_ci_low, marginal_ci_high = wilson_ci(
                marginal_metrics[f"{prefix}_cnt_bad"],
                marginal_metrics[f"{prefix}_cnt_mature"],
            )
            row[f"marginal_{prefix}_cnt_bad_rate_ci_low"] = marginal_ci_low
            row[f"marginal_{prefix}_cnt_bad_rate_ci_high"] = marginal_ci_high
        row["cum_pass_rate"] = safe_div(cumulative_metrics["n"], total_n)
        row["cum_principal_pct"] = safe_div(
            cumulative_metrics["principal"],
            total_principal,
        )
        row["marginal_sample_pct"] = safe_div(marginal_metrics["n"], total_n)
        row["marginal_principal_pct"] = safe_div(
            marginal_metrics["principal"],
            total_principal,
        )
        rows.append(row)
        previous_threshold = threshold

    return pd.DataFrame(rows)
def select_threshold_under_constraints(
    curve: pd.DataFrame,
    constraints: Dict[str, float],
) -> Optional[pd.Series]:
    """选择满足风险约束且累计通过率最高的阈值。"""
    eligible = curve.copy()
    for constraint_name, maximum in constraints.items():
        metric = remove_prefix(constraint_name, "max_")
        require_columns(eligible, [metric], "策略阈值曲线")
        eligible = eligible.loc[eligible[metric].le(maximum)]

    if eligible.empty:
        return None

    return eligible.sort_values(
        ["cum_pass_rate", "threshold_order"],
        ascending=[False, False],
    ).iloc[0]
def build_strategy_plan(
    curve: pd.DataFrame,
    config: Dict,
) -> pd.DataFrame:
    """根据唯一一套配置生成自动通过、人工审核和拒绝阈值。"""
    auto_row, accept_row = compute_auto_accept_rows(curve, config)

    base = {
        "strategy_name": config["strategy_name"],
        "objective": config["objective"],
    }

    if auto_row is None or accept_row is None:
        return pd.DataFrame([{**base, "status": "无满足约束的阈值"}])

    result = {
        **base,
        "status": "OK",
        "auto_pass_threshold": auto_row["threshold"],
        "auto_pass_bin": auto_row[FINAL_BIN_COL],
        "reject_threshold": accept_row["threshold"],
        "manual_review_upper_bin": accept_row[FINAL_BIN_COL],
        "strategy_estimated_auto_pass_rate": auto_row["cum_pass_rate"],
        "strategy_estimated_total_accept_rate": accept_row["cum_pass_rate"],
        "strategy_estimated_manual_review_rate": (
            accept_row["cum_pass_rate"] - auto_row["cum_pass_rate"]
        ),
        "strategy_estimated_reject_rate": 1 - accept_row["cum_pass_rate"],
        "accepted_1m30p_cnt_bad_rate": accept_row["cum_1m30p_cnt_bad_rate"],
        "accepted_3m30p_cnt_bad_rate": accept_row["cum_3m30p_cnt_bad_rate"],
        "accepted_1m30p_amt_bad_rate": accept_row["cum_1m30p_amt_bad_rate"],
        "accepted_3m30p_amt_bad_rate": accept_row["cum_3m30p_amt_bad_rate"],
        "last_accepted_marginal_3m30p_cnt_bad_rate": accept_row[
            "marginal_3m30p_cnt_bad_rate"
        ],
    }
    return pd.DataFrame([result])
def build_threshold_sensitivity(
    curve: pd.DataFrame,
    strategy_plan: pd.DataFrame,
) -> pd.DataFrame:
    """
    生成阈值敏感性表：对自动通过 / 总接纳阈值各展示当前、收严一档、放松一档
    对通过率、风险率和人工审核量的边际影响，供风险与业务确认风险上限取值时参考。
    """
    valid = strategy_plan.loc[strategy_plan["status"].eq("OK")]
    if valid.empty or curve.empty:
        return pd.DataFrame()

    strategy = valid.iloc[0]
    auto_threshold = float(strategy["auto_pass_threshold"])
    accept_threshold = float(strategy["reject_threshold"])

    curve = curve.reset_index(drop=True)
    threshold_values = curve["threshold"].astype(float).to_numpy()

    def row_position(value: float) -> int:
        return int(np.flatnonzero(np.isclose(threshold_values, value))[0])

    auto_pos = row_position(auto_threshold)
    accept_pos = row_position(accept_threshold)

    def collect(
        threshold_type: str,
        base_pos: int,
        other_pos: int,
    ) -> List[Dict]:
        rows: List[Dict] = []
        for scenario, variant_pos, note in [
            ("当前", base_pos, ""),
            ("收严一档", base_pos - 1, ""),
            ("放松一档", base_pos + 1, ""),
        ]:
            variant = curve.iloc[variant_pos] if 0 <= variant_pos < len(curve) else None
            if variant is None:
                edge_note = (
                    "已是曲线最严档位，无更严候选"
                    if scenario == "收严一档"
                    else "已是曲线最松档位，无更松候选"
                )
                rows.append({
                    "threshold_type": threshold_type,
                    "scenario": scenario,
                    "threshold": np.nan,
                    FINAL_BIN_COL: "",
                    "note": edge_note,
                })
                continue

            if threshold_type == "自动通过阈值":
                auto_row, accept_row = variant, curve.iloc[other_pos]
                if accept_row["threshold_order"] < variant["threshold_order"]:
                    auto_row = accept_row
                    note = "放松后越过总接纳阈值，按规则对齐（人工审核量为 0）"
            else:
                auto_row, accept_row = curve.iloc[other_pos], variant
                if variant["threshold_order"] < auto_row["threshold_order"]:
                    accept_row = auto_row
                    note = "收严后严于自动通过阈值，按规则对齐"

            row = {
                "threshold_type": threshold_type,
                "scenario": scenario,
                "threshold": (
                    auto_row["threshold"] if threshold_type == "自动通过阈值"
                    else accept_row["threshold"]
                ),
                FINAL_BIN_COL: (
                    auto_row[FINAL_BIN_COL] if threshold_type == "自动通过阈值"
                    else accept_row[FINAL_BIN_COL]
                ),
                "strategy_estimated_auto_pass_rate": float(auto_row["cum_pass_rate"]),
                "strategy_estimated_manual_review_rate": max(
                    0.0, float(accept_row["cum_pass_rate"] - auto_row["cum_pass_rate"])
                ),
                "strategy_estimated_total_accept_rate": float(accept_row["cum_pass_rate"]),
                "strategy_estimated_reject_rate": 1.0 - float(accept_row["cum_pass_rate"]),
                "auto_1m30p_cnt_bad_rate": float(auto_row["cum_1m30p_cnt_bad_rate"]),
                "auto_3m30p_cnt_bad_rate": float(auto_row["cum_3m30p_cnt_bad_rate"]),
                "accept_3m30p_cnt_bad_rate": float(accept_row["cum_3m30p_cnt_bad_rate"]),
                "accept_marginal_3m30p_cnt_bad_rate": float(
                    accept_row["marginal_3m30p_cnt_bad_rate"]
                ),
                "accept_marginal_3m30p_cnt_bad_rate_ci_high": float(
                    accept_row["marginal_3m30p_cnt_bad_rate_ci_high"]
                ),
                "note": note,
            }
            rows.append(row)
        return rows

    rows = [
        *collect("自动通过阈值", auto_pos, accept_pos),
        *collect("总接纳阈值", accept_pos, auto_pos),
    ]
    result = pd.DataFrame(rows)

    for threshold_type in result["threshold_type"].unique():
        mask = result["threshold_type"].eq(threshold_type)
        base = result.loc[mask & result["scenario"].eq("当前")]
        if base.empty:
            continue
        for metric in [
            "strategy_estimated_auto_pass_rate",
            "strategy_estimated_manual_review_rate",
            "strategy_estimated_total_accept_rate",
            "strategy_estimated_reject_rate",
        ]:
            result.loc[mask, f"{metric}_delta"] = (
                result.loc[mask, metric] - base.iloc[0][metric]
            )

    first_columns = [
        "threshold_type",
        "scenario",
        "threshold",
        FINAL_BIN_COL,
        "strategy_estimated_auto_pass_rate",
        "strategy_estimated_manual_review_rate",
        "strategy_estimated_total_accept_rate",
        "strategy_estimated_reject_rate",
        "auto_1m30p_cnt_bad_rate",
        "auto_3m30p_cnt_bad_rate",
        "accept_3m30p_cnt_bad_rate",
        "accept_marginal_3m30p_cnt_bad_rate",
        "accept_marginal_3m30p_cnt_bad_rate_ci_high",
        "strategy_estimated_auto_pass_rate_delta",
        "strategy_estimated_manual_review_rate_delta",
        "strategy_estimated_total_accept_rate_delta",
        "strategy_estimated_reject_rate_delta",
        "note",
    ]
    remaining_columns = [col for col in result.columns if col not in first_columns]
    return result[[col for col in first_columns if col in result.columns] + remaining_columns]
def build_strategy_segment_report(
    train: pd.DataFrame,
    oot: pd.DataFrame,
    strategy_plan: pd.DataFrame,
) -> pd.DataFrame:
    """验证唯一策略在 Train 和 OOT 中的三段表现。"""
    valid = strategy_plan.loc[strategy_plan["status"].eq("OK")]
    if valid.empty:
        return pd.DataFrame()

    strategy = valid.iloc[0]
    auto_threshold = float(strategy["auto_pass_threshold"])
    reject_threshold = float(strategy["reject_threshold"])
    segments = [
        ("自动通过", None, auto_threshold),
        ("人工审核", auto_threshold, reject_threshold),
        ("拒绝", reject_threshold, None),
    ]

    rows = []
    for sample_group, data in [("train", train), ("oot", oot)]:
        for decision, lower, upper in segments:
            metrics = calc_segment_metrics(data, lower, upper)
            segment_rate = metrics.pop("sample_pct")
            segment_principal_rate = metrics.pop("principal_pct")
            rows.append(
                {
                    "sample_group": sample_group,
                    "strategy_name": strategy["strategy_name"],
                    "decision": decision,
                    "lower_threshold_exclusive": lower,
                    "upper_threshold_inclusive": upper,
                    "strategy_estimated_segment_rate": segment_rate,
                    "strategy_estimated_segment_principal_rate": segment_principal_rate,
                    **metrics,
                }
            )

    return pd.DataFrame(rows)
def build_strategy_estimated_flow_report(
    train: pd.DataFrame,
    oot: pd.DataFrame,
    strategy_plan: pd.DataFrame,
) -> pd.DataFrame:
    """按模型分阈值输出 Train、OOT 与全量的模型策略测算流量。"""
    valid = strategy_plan.loc[strategy_plan["status"].eq("OK")]
    if valid.empty:
        return pd.DataFrame()

    strategy = valid.iloc[0]
    auto_threshold = float(strategy["auto_pass_threshold"])
    accept_threshold = float(strategy["reject_threshold"])
    all_data = pd.concat([train, oot], ignore_index=True)
    rows = []

    for sample_group, data in [("Train", train), ("OOT", oot), ("All", all_data)]:
        score = data[SCORE_COL]
        valid_score = score.notna()
        if HIGH_SCORE_HIGH_RISK:
            auto_mask = valid_score & score.le(auto_threshold)
            manual_mask = valid_score & score.gt(auto_threshold) & score.le(accept_threshold)
            reject_mask = valid_score & score.gt(accept_threshold)
        else:
            auto_mask = valid_score & score.ge(auto_threshold)
            manual_mask = valid_score & score.lt(auto_threshold) & score.ge(accept_threshold)
            reject_mask = valid_score & score.lt(accept_threshold)

        def unique_count(mask: pd.Series) -> int:
            return int(data.loc[mask, "application_id"].nunique(dropna=True))

        total_cnt = unique_count(valid_score)
        auto_cnt = unique_count(auto_mask)
        manual_cnt = unique_count(manual_mask)
        reject_cnt = unique_count(reject_mask)
        accepted_cnt = auto_cnt + manual_cnt
        rows.append(
            {
                "metric_scope": "模型策略测算流量（score_mlt阈值）",
                "sample_group": sample_group,
                "strategy_estimated_total_application_cnt": total_cnt,
                "strategy_estimated_auto_pass_cnt": auto_cnt,
                "strategy_estimated_manual_review_cnt": manual_cnt,
                "strategy_estimated_total_accept_cnt": accepted_cnt,
                "strategy_estimated_reject_cnt": reject_cnt,
                "strategy_estimated_auto_pass_rate": safe_div(auto_cnt, total_cnt),
                "strategy_estimated_manual_review_rate": safe_div(manual_cnt, total_cnt),
                "strategy_estimated_total_accept_rate": safe_div(accepted_cnt, total_cnt),
                "strategy_estimated_reject_rate": safe_div(reject_cnt, total_cnt),
                "auto_pass_threshold": auto_threshold,
                "total_accept_threshold": accept_threshold,
            }
        )

    return pd.DataFrame(rows)
def build_binning_process_table(
    initial_stats: pd.DataFrame,
    merge_map: pd.DataFrame,
) -> pd.DataFrame:
    """
    汇总初始分箱、风险表现和最终合箱映射。

    这张表用于回答三个问题：
    1. 每个初始箱的样本量和风险表现如何；
    2. 相邻箱之间是否出现风险倒挂；
    3. 每个初始箱最终被合并到哪个风险等级。
    """
    process = initial_stats.copy().rename(columns={"bin_order": "initial_bin_order"})
    process = process.merge(
        merge_map[
            [
                "initial_bin_order",
                INITIAL_BIN_COL,
                "final_bin_order",
                FINAL_BIN_COL,
                "merged_from",
            ]
        ],
        on=["initial_bin_order", INITIAL_BIN_COL],
        how="left",
    )
    process = process.sort_values("initial_bin_order").reset_index(drop=True)
    initial_bin_count = int(process["initial_bin_order"].max())
    process["extreme_bin_role"] = process["initial_bin_order"].apply(
        lambda order: classify_extreme_role(order, order, initial_bin_count)
    )

    for prefix in ["1m30p", "3m30p"]:
        rate_col = f"{prefix}_cnt_bad_rate"
        diff_col = f"{prefix}_rate_diff_prev"
        inversion_col = f"{prefix}_inversion_flag"
        process[diff_col] = process[rate_col].diff()
        process[inversion_col] = process[diff_col].lt(0).fillna(False)

    process["merge_action"] = np.where(
        process["merged_from"].astype(str).str.contains("-", regex=False),
        "相邻箱合并",
        "单箱保留",
    )

    key_columns = [
        "initial_bin_order",
        INITIAL_BIN_COL,
        "score_left",
        "score_right",
        "score_min",
        "score_max",
        "score_mean",
        "n",
        "sample_pct",
        "1m30p_cnt_mature",
        "1m30p_cnt_bad",
        "1m30p_cnt_bad_rate",
        "1m30p_rate_diff_prev",
        "1m30p_inversion_flag",
        "3m30p_cnt_mature",
        "3m30p_cnt_bad",
        "3m30p_cnt_bad_rate",
        "3m30p_rate_diff_prev",
        "3m30p_inversion_flag",
        "1m30p_amt_exposure",
        "1m30p_amt_bad",
        "1m30p_amt_bad_rate",
        "3m30p_amt_exposure",
        "3m30p_amt_bad",
        "3m30p_amt_bad_rate",
        "cum_pass_rate",
        "cum_1m30p_cnt_bad_rate",
        "cum_3m30p_cnt_bad_rate",
        "final_bin_order",
        FINAL_BIN_COL,
        "merged_from",
        "extreme_bin_role",
        "merge_action",
    ]
    return process[[col for col in key_columns if col in process.columns]]
def build_threshold_selection_table(
    threshold_curve: pd.DataFrame,
    strategy_plan: pd.DataFrame,
    config: Dict,
) -> pd.DataFrame:
    """
    在阈值曲线上补充约束检查结果和最终阈值标记。

    Excel 中可以直接看到：
    - 每个候选阈值的累计通过率、累计风险和边际风险；
    - 是否满足自动通过约束；
    - 是否满足整体接纳约束；
    - 哪一行最终被选为自动通过阈值或人工审核上限。
    """
    result = threshold_curve.copy()

    for group_name, constraints in [
        ("auto", config["auto_constraints"]),
        ("accept", config["accept_constraints"]),
    ]:
        check_columns = []
        for constraint_name, limit in constraints.items():
            metric = remove_prefix(constraint_name, "max_")
            check_col = f"{group_name}_check_{metric}"
            limit_col = f"{group_name}_limit_{metric}"
            result[limit_col] = limit
            result[check_col] = result[metric].le(limit).fillna(False)
            check_columns.append(check_col)
        result[f"{group_name}_all_constraints_ok"] = result[check_columns].all(axis=1)

    result["selected_role"] = ""
    result["selection_reason"] = ""

    valid = strategy_plan.loc[strategy_plan["status"].eq("OK")]
    if not valid.empty:
        strategy = valid.iloc[0]
        auto_threshold = float(strategy["auto_pass_threshold"])
        reject_threshold = float(strategy["reject_threshold"])

        auto_mask = np.isclose(result["threshold"].astype(float), auto_threshold)
        reject_mask = np.isclose(result["threshold"].astype(float), reject_threshold)

        result.loc[auto_mask, "selected_role"] = "自动通过阈值"
        result.loc[auto_mask, "selection_reason"] = (
            "满足自动通过全部风险约束，且累计通过率最高"
        )

        same_threshold = auto_mask & reject_mask
        result.loc[reject_mask & ~same_threshold, "selected_role"] = "人工审核上限/拒绝阈值"
        result.loc[reject_mask & ~same_threshold, "selection_reason"] = (
            "满足整体接纳全部风险约束，且累计接纳率最高"
        )
        result.loc[same_threshold, "selected_role"] = "自动通过阈值及拒绝阈值"
        result.loc[same_threshold, "selection_reason"] = (
            "自动通过与整体接纳最终选择了同一阈值"
        )

    first_columns = [
        "selected_role",
        "selection_reason",
        "threshold_order",
        "threshold",
        "prev_threshold",
        "final_bin_order",
        FINAL_BIN_COL,
        "merged_from",
        "extreme_bin_role",
        "cum_pass_rate",
        "cum_n",
        "cum_principal_pct",
        "cum_1m30p_cnt_mature",
        "cum_1m30p_cnt_bad",
        "cum_1m30p_cnt_bad_rate",
        "cum_3m30p_cnt_mature",
        "cum_3m30p_cnt_bad",
        "cum_3m30p_cnt_bad_rate",
        "cum_3m30p_cnt_bad_rate_ci_high",
        "cum_1m30p_amt_exposure",
        "cum_1m30p_amt_bad",
        "cum_1m30p_amt_bad_rate",
        "cum_3m30p_amt_exposure",
        "cum_3m30p_amt_bad",
        "cum_3m30p_amt_bad_rate",
        "marginal_sample_pct",
        "marginal_n",
        "marginal_3m30p_cnt_mature",
        "marginal_3m30p_cnt_bad",
        "marginal_3m30p_cnt_bad_rate",
        "marginal_3m30p_cnt_bad_rate_ci_high",
        "auto_all_constraints_ok",
        "accept_all_constraints_ok",
    ]
    remaining_columns = [col for col in result.columns if col not in first_columns]
    return result[[col for col in first_columns if col in result.columns] + remaining_columns]
