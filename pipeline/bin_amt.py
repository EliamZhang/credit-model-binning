# -*- coding: utf-8 -*-
"""金额口径合箱管线：与 binning_cnt 共享通用函数，仅保留金额口径差异实现及其传递依赖（金额主指标、金额加权 IV、金额口径阈值曲线等）。

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


from pipeline.binning_cnt import calc_bin_constraint_details, classify_extreme_role, count_crossed_boundaries, count_rate_inversions, filter_blocked_pair_indices, format_merge_ranges, identify_extreme_boundaries, merge_ranges_at, oriented_rate, parse_merge_ranges, primary_inversion_pair_indices
from pipeline.common import remove_prefix, require_columns, safe_div, wilson_ci
from pipeline.data_loading import build_bin_actual_funnel_report
from pipeline.risk_metrics import add_bin_lift, add_risk_helper_columns, two_proportion_pvalue
from pipeline.strategy import compute_auto_accept_rows



def add_bin_derived_metrics(
    stats: pd.DataFrame,
    order_col: str,
    include_total_n: bool = True,
) -> pd.DataFrame:
    """
    统一补充分箱派生指标。

    业务口径（混合口径）：
    - 主指标：金额逾期率 = 逾期剩余本金 / 成熟本金敞口；
    - 参考口径：笔数逾期率 = 逾期样本量 / 成熟样本量（显著性检验与样本约束仍用笔数）；
    - 累计指标始终按低风险到高风险的箱顺序计算。
    """
    result = stats.sort_values(order_col).reset_index(drop=True).copy()
    total_n = result["n"].sum()
    if include_total_n:
        result["total_n"] = total_n
    result["sample_pct"] = safe_div(result["n"], total_n)

    for prefix in RISK_PREFIXES:
        result[f"{prefix}_cnt_good"] = (
            result[f"{prefix}_cnt_mature"] - result[f"{prefix}_cnt_bad"]
        )
        result[f"{prefix}_cnt_bad_rate"] = safe_div(
            result[f"{prefix}_cnt_bad"], result[f"{prefix}_cnt_mature"]
        )
        ci_low, ci_high = wilson_ci(
            result[f"{prefix}_cnt_bad"], result[f"{prefix}_cnt_mature"]
        )
        result[f"{prefix}_cnt_bad_rate_ci_low"] = ci_low
        result[f"{prefix}_cnt_bad_rate_ci_high"] = ci_high
        result[f"{prefix}_amt_bad_rate"] = safe_div(
            result[f"{prefix}_amt_bad"], result[f"{prefix}_amt_exposure"]
        )
        # 金额加权 IV 所需的"好金额"列（敞口 - 逾期金额，max 防极端样本 bad>exposure）。
        result[f"{prefix}_amt_good"] = (
            result[f"{prefix}_amt_exposure"] - result[f"{prefix}_amt_bad"]
        ).clip(lower=0)

    result["cum_n"] = result["n"].cumsum()
    result["cum_pass_rate"] = safe_div(result["cum_n"], total_n)

    for prefix in RISK_PREFIXES:
        result[f"cum_{prefix}_cnt_mature"] = result[f"{prefix}_cnt_mature"].cumsum()
        result[f"cum_{prefix}_cnt_bad"] = result[f"{prefix}_cnt_bad"].cumsum()
        result[f"cum_{prefix}_cnt_bad_rate"] = safe_div(
            result[f"cum_{prefix}_cnt_bad"],
            result[f"cum_{prefix}_cnt_mature"],
        )
        cum_ci_low, cum_ci_high = wilson_ci(
            result[f"cum_{prefix}_cnt_bad"],
            result[f"cum_{prefix}_cnt_mature"],
        )
        result[f"cum_{prefix}_cnt_bad_rate_ci_low"] = cum_ci_low
        result[f"cum_{prefix}_cnt_bad_rate_ci_high"] = cum_ci_high
        result[f"cum_{prefix}_amt_exposure"] = result[f"{prefix}_amt_exposure"].cumsum()
        result[f"cum_{prefix}_amt_bad"] = result[f"{prefix}_amt_bad"].cumsum()
        result[f"cum_{prefix}_amt_bad_rate"] = safe_div(
            result[f"cum_{prefix}_amt_bad"],
            result[f"cum_{prefix}_amt_exposure"],
        )

    return result
def calc_bin_stats(
    data: pd.DataFrame,
    bin_col: str,
    order_col: str,
    score_col: Optional[str] = None,
) -> pd.DataFrame:
    """
    按分箱计算核心指标，并保留 Excel 可复算的分子和分母。
    """
    if score_col is None:
        score_col = SCORE_COL
    required = [
        "application_id",
        bin_col,
        order_col,
        score_col,
        *RISK_NUMERIC_COLS[1:],
    ]
    require_columns(data, required, "calc_bin_stats")

    work = add_risk_helper_columns(data)
    aggregations = {
        "n": ("application_id", "count"),
        "principal_amt": ("_principal", "sum"),
        "score_min": (score_col, "min"),
        "score_max": (score_col, "max"),
        "score_mean": (score_col, "mean"),
    }
    for prefix, config in RISK_HELPER_CONFIG.items():
        helper = config["helper_prefix"]
        aggregations[f"{prefix}_cnt_mature"] = (f"{helper}_mature_cnt", "sum")
        aggregations[f"{prefix}_cnt_bad"] = (f"{helper}_bad_cnt", "sum")
    for prefix, config in RISK_HELPER_CONFIG.items():
        helper = config["helper_prefix"]
        aggregations[f"{prefix}_amt_exposure"] = (f"{helper}_amt_exposure", "sum")
        aggregations[f"{prefix}_amt_bad"] = (f"{helper}_amt_bad", "sum")

    stats = (
        work.groupby([bin_col, order_col], dropna=False, observed=True)
        .agg(**aggregations)
        .reset_index()
        .rename(columns={order_col: "bin_order"})
    )
    return add_bin_derived_metrics(stats, order_col="bin_order")
def add_bin_model_diagnostics(stats: pd.DataFrame) -> pd.DataFrame:
    """补充箱级 IV 分项与累计 KS 曲线；整体 AUC/KS 仍在模型验证表展示。

    金额口径：IV 分项按金额加权（bad=逾期金额、good=敞口-逾期金额），
    与合箱 IV 保留率口径一致；KS 曲线保持笔数口径（反映排序能力）。
    """
    result = stats.sort_values("bin_order").reset_index(drop=True).copy()
    bin_count = len(result)
    if bin_count == 0:
        return result

    for prefix in ["1m30p", "3m30p"]:
        bad = pd.to_numeric(result[f"{prefix}_amt_bad"], errors="coerce").fillna(0.0)
        good = pd.to_numeric(result[f"{prefix}_amt_good"], errors="coerce").fillna(0.0)
        bad_dist = (bad + IV_SMOOTHING_EPS) / (
            bad.sum() + IV_SMOOTHING_EPS * bin_count
        )
        good_dist = (good + IV_SMOOTHING_EPS) / (
            good.sum() + IV_SMOOTHING_EPS * bin_count
        )
        woe = np.log(bad_dist / good_dist)

        result[f"{prefix}_bad_distribution"] = bad_dist
        result[f"{prefix}_good_distribution"] = good_dist
        result[f"{prefix}_woe"] = woe
        result[f"{prefix}_iv_component"] = (bad_dist - good_dist) * woe

        # KS 曲线保持笔数口径：高风险端向低风险端累计，与整体 KS 的风险排序方向一致。
        cnt_bad = pd.to_numeric(result[f"{prefix}_cnt_bad"], errors="coerce").fillna(0.0)
        cnt_good = pd.to_numeric(result[f"{prefix}_cnt_good"], errors="coerce").fillna(0.0)
        cum_bad_from_high = cnt_bad.iloc[::-1].cumsum().iloc[::-1]
        cum_good_from_high = cnt_good.iloc[::-1].cumsum().iloc[::-1]
        result[f"{prefix}_ks_curve"] = (
            safe_div(cum_bad_from_high, cnt_bad.sum())
            - safe_div(cum_good_from_high, cnt_good.sum())
        ).abs()

    return result
def build_enriched_final_bin_report(
    stats: pd.DataFrame,
    data: pd.DataFrame,
    strategy_plan: pd.DataFrame,
    psi: pd.DataFrame,
) -> pd.DataFrame:
    """整合箱级风险、历史实际审批、策略流量贡献和稳定性诊断指标。"""
    result = add_bin_lift(add_bin_model_diagnostics(stats))
    actual_by_bin = build_bin_actual_funnel_report(data)
    result = result.merge(
        actual_by_bin,
        on=["bin_order", FINAL_BIN_COL],
        how="left",
        validate="one_to_one",
    )

    psi_columns = [FINAL_BIN_COL, "psi_component", "psi_total"]
    available_psi_columns = [col for col in psi_columns if col in psi.columns]
    result = result.merge(
        psi[available_psi_columns].rename(
            columns={
                "psi_component": "train_oot_psi_component",
                "psi_total": "train_oot_psi_total",
            }
        ),
        on=FINAL_BIN_COL,
        how="left",
        validate="many_to_one",
    )

    valid_strategy = strategy_plan.loc[strategy_plan["status"].eq("OK")]
    if valid_strategy.empty:
        result["strategy_estimated_decision"] = "未生成"
    else:
        strategy = valid_strategy.iloc[0]
        auto_bin = str(strategy["auto_pass_bin"])
        accept_bin = str(strategy["manual_review_upper_bin"])
        bin_order_map = result.set_index(FINAL_BIN_COL)["bin_order"].to_dict()
        auto_order = bin_order_map.get(auto_bin)
        accept_order = bin_order_map.get(accept_bin)
        if auto_order is None or accept_order is None:
            raise ValueError("策略阈值档位未匹配最终分箱")
        result["strategy_estimated_decision"] = np.select(
            [
                result["bin_order"].le(auto_order),
                result["bin_order"].le(accept_order),
            ],
            ["自动通过", "人工审核"],
            default="拒绝",
        )

    result["strategy_estimated_bin_flow_rate"] = result["sample_pct"]
    result["strategy_estimated_cumulative_flow_rate"] = result["cum_pass_rate"]

    priority_columns = [
        "bin_order",
        FINAL_BIN_COL,
        "score_left",
        "score_right",
        "n",
        "sample_pct",
        "cum_pass_rate",
        "strategy_estimated_decision",
        "strategy_estimated_bin_flow_rate",
        "strategy_estimated_cumulative_flow_rate",
        "actual_apply_cnt",
        "actual_completion_rate",
        "actual_approval_rate",
        "actual_auto_approval_rate",
        "actual_manual_approval_rate",
        "actual_auto_approval_share",
        "actual_manual_approval_share",
        "actual_deal_rate",
        "1m30p_cnt_bad_rate",
        "1m30p_amt_bad_rate",
        "3m30p_cnt_bad_rate",
        "3m30p_amt_bad_rate",
        "1m30p_cnt_lift",
        "1m30p_amt_lift",
        "3m30p_cnt_lift",
        "3m30p_amt_lift",
        "cum_1m30p_cnt_mature",
        "cum_1m30p_cnt_bad",
        "cum_1m30p_cnt_bad_rate",
        "cum_1m30p_amt_exposure",
        "cum_1m30p_amt_bad",
        "cum_1m30p_amt_bad_rate",
        "cum_3m30p_cnt_mature",
        "cum_3m30p_cnt_bad",
        "cum_3m30p_cnt_bad_rate",
        "cum_3m30p_amt_exposure",
        "cum_3m30p_amt_bad",
        "cum_3m30p_amt_bad_rate",
        "1m30p_iv_component",
        "1m30p_ks_curve",
        "3m30p_iv_component",
        "3m30p_ks_curve",
        "train_oot_psi_component",
        "train_oot_psi_total",
    ]
    priority_columns = [col for col in priority_columns if col in result.columns]
    remaining_columns = [col for col in result.columns if col not in priority_columns]
    return result[priority_columns + remaining_columns]
def calc_complete_initial_stats(
    data: pd.DataFrame,
    initial_edges: pd.DataFrame,
) -> pd.DataFrame:
    """计算完整初始箱统计；无样本的箱也保留，防止候选范围错位。"""
    stats = calc_bin_stats(
        data,
        bin_col=INITIAL_BIN_COL,
        order_col="initial_bin_order",
    )
    stats = stats.loc[stats[INITIAL_BIN_COL].notna()].copy()

    edge_cols = [
        "bin_order",
        INITIAL_BIN_COL,
        "score_left",
        "score_right",
    ]
    result = initial_edges[edge_cols].merge(
        stats.drop(columns=["score_left", "score_right"], errors="ignore"),
        on=["bin_order", INITIAL_BIN_COL],
        how="left",
    )

    additive_cols = ["n", "principal_amt"]
    for prefix in RISK_PREFIXES:
        additive_cols.extend(
            [
                f"{prefix}_cnt_mature",
                f"{prefix}_cnt_bad",
                f"{prefix}_cnt_good",
                f"{prefix}_amt_exposure",
                f"{prefix}_amt_bad",
                f"{prefix}_amt_good",
            ]
        )

    for col in additive_cols:
        if col not in result.columns:
            result[col] = 0.0
        result[col] = pd.to_numeric(result[col], errors="coerce").fillna(0)

    return add_bin_derived_metrics(result, order_col="bin_order")
def aggregate_initial_stats_by_ranges(
    initial_stats: pd.DataFrame,
    ranges: Sequence[Tuple[int, int]],
) -> pd.DataFrame:
    """将连续初始箱按候选范围聚合为最终风险档。"""
    stats = initial_stats.sort_values("bin_order").reset_index(drop=True)
    total_n = float(stats["n"].sum())
    rows = []

    for final_order, (start, end) in enumerate(ranges, start=1):
        part = stats.loc[stats["bin_order"].between(start, end, inclusive="both")]
        if part.empty:
            raise ValueError(f"候选合箱范围 ({start}, {end}) 未匹配任何初始箱")

        n = float(part["n"].sum())
        row = {
            "final_bin_order": final_order,
            FINAL_BIN_COL: chr(ord("A") + final_order - 1),
            "merged_from": (
                f"B{start:02d}-B{end:02d}" if start != end else f"B{start:02d}"
            ),
            "source_bin_start": start,
            "source_bin_end": end,
            "n": n,
            "principal_amt": float(part["principal_amt"].sum()),
            "score_left": part["score_left"].iloc[0],
            "score_right": part["score_right"].iloc[-1],
            "score_min": part["score_min"].min(),
            "score_max": part["score_max"].max(),
            "score_mean": safe_div((part["score_mean"] * part["n"]).sum(), n),
        }

        for prefix in RISK_PREFIXES:
            mature = float(part[f"{prefix}_cnt_mature"].sum())
            bad = float(part[f"{prefix}_cnt_bad"].sum())
            exposure = float(part[f"{prefix}_amt_exposure"].sum())
            bad_amount = float(part[f"{prefix}_amt_bad"].sum())
            row.update(
                {
                    f"{prefix}_cnt_mature": mature,
                    f"{prefix}_cnt_bad": bad,
                    f"{prefix}_cnt_good": mature - bad,
                    f"{prefix}_amt_exposure": exposure,
                    f"{prefix}_amt_bad": bad_amount,
                    f"{prefix}_amt_good": max(0.0, exposure - bad_amount),
                    f"{prefix}_cnt_bad_rate": safe_div(bad, mature),
                    f"{prefix}_amt_bad_rate": safe_div(bad_amount, exposure),
                }
            )

        row["sample_pct"] = safe_div(n, total_n)
        rows.append(row)

    return add_bin_derived_metrics(
        pd.DataFrame(rows),
        order_col="final_bin_order",
        include_total_n=False,
    )
def calc_iv_from_stats(
    stats: pd.DataFrame,
    bad_col: str = PRIMARY_AMT_BAD_COL,
    good_col: str = PRIMARY_AMT_GOOD_COL,
    eps: float = IV_SMOOTHING_EPS,
) -> float:
    """使用箱级好坏金额（金额加权 IV）计算 IV。"""
    bad = pd.to_numeric(stats[bad_col], errors="coerce").fillna(0).to_numpy(float)
    good = pd.to_numeric(stats[good_col], errors="coerce").fillna(0).to_numpy(float)
    if bad.sum() <= 0 or good.sum() <= 0:
        return np.nan

    bad_dist = (bad + eps) / (bad.sum() + eps * len(bad))
    good_dist = (good + eps) / (good.sum() + eps * len(good))
    return float(np.sum((bad_dist - good_dist) * np.log(bad_dist / good_dist)))
def identify_protected_boundaries(
    initial_stats: pd.DataFrame,
    config: Dict,
) -> Set[int]:
    """
    找出应尽量保留的初始箱边界。

    边界编号 k 代表 Bk 与 B(k+1) 之间的切点。
    """
    ordered = initial_stats.sort_values("bin_order").reset_index(drop=True)
    boundaries: Set[int] = identify_extreme_boundaries(len(ordered))
    max_boundary = len(ordered) - 1

    for constraint_group in ["auto_constraints", "accept_constraints"]:
        constraints = config[constraint_group]

        cum_limit = constraints.get("max_cum_3m30p_amt_bad_rate")
        if cum_limit is not None:
            eligible = ordered.loc[ordered["cum_3m30p_amt_bad_rate"].le(cum_limit)]
            if not eligible.empty:
                boundary = int(eligible["bin_order"].max())
                if 1 <= boundary <= max_boundary:
                    boundaries.add(boundary)

        marginal_limit = constraints.get("max_marginal_3m30p_amt_bad_rate")
        if marginal_limit is not None:
            above = ordered.loc[ordered[PRIMARY_RATE_COL].gt(marginal_limit)]
            if not above.empty:
                boundary = int(above["bin_order"].min()) - 1
                if 1 <= boundary <= max_boundary:
                    boundaries.add(boundary)

    if PROTECT_LARGEST_RISK_JUMPS > 0:
        oriented = oriented_rate(ordered[PRIMARY_RATE_COL])
        jumps = oriented.diff().dropna().sort_values(ascending=False)
        for idx in jumps.head(PROTECT_LARGEST_RISK_JUMPS).index:
            boundary = int(ordered.loc[idx, "bin_order"]) - 1
            if 1 <= boundary <= max_boundary:
                boundaries.add(boundary)

    return boundaries
def pair_merge_diagnostics(
    current_stats: pd.DataFrame,
    ranges: Sequence[Tuple[int, int]],
    pair_index: int,
    initial_stats: pd.DataFrame,
    protected_boundaries: Set[int],
    extreme_boundaries: Optional[Set[int]] = None,
    ignore_protection: bool = False,
) -> Dict[str, float]:
    """计算合并某对相邻箱的风险差异、显著性、IV 损失和综合代价。"""
    left = current_stats.iloc[pair_index]
    right = current_stats.iloc[pair_index + 1]

    left_rate = left[PRIMARY_RATE_COL]
    right_rate = right[PRIMARY_RATE_COL]
    if pd.isna(left_rate) or pd.isna(right_rate):
        rate_gap = 0.0
    else:
        rate_gap = abs(float(left_rate) - float(right_rate))
    # 双主指标下取最大差异，确保任一指标差异显著时不会被轻易合并。
    for rate_col in PRIMARY_RATE_COLS:
        lr = left.get(rate_col)
        rr = right.get(rate_col)
        if pd.notna(lr) and pd.notna(rr):
            rate_gap = max(rate_gap, abs(float(lr) - float(rr)))

    p_value = two_proportion_pvalue(
        left[PRIMARY_BAD_COL],
        left[PRIMARY_MATURE_COL],
        right[PRIMARY_BAD_COL],
        right[PRIMARY_MATURE_COL],
    )
    p_for_cost = 0.0 if pd.isna(p_value) else p_value

    current_iv = calc_iv_from_stats(current_stats)
    merged_ranges = merge_ranges_at(ranges, pair_index)
    merged_stats = aggregate_initial_stats_by_ranges(initial_stats, merged_ranges)
    merged_iv = calc_iv_from_stats(merged_stats)
    iv_loss = 0.0
    if pd.notna(current_iv) and pd.notna(merged_iv):
        iv_loss = max(0.0, float(current_iv - merged_iv))

    boundary = int(ranges[pair_index][1])
    is_protected = boundary in protected_boundaries
    is_extreme_boundary = boundary in (extreme_boundaries or set())
    protection_penalty = (
        0.0
        if ignore_protection or not is_protected
        else PROTECTED_BOUNDARY_PENALTY
    )
    if is_extreme_boundary and not ignore_protection:
        protection_penalty += EXTREME_BOUNDARY_PENALTY

    # 风险越接近、差异越不显著、IV 损失越小，越优先合并。
    cost = (
        rate_gap * MERGE_COST_RATE_GAP_WEIGHT
        + (1 - p_for_cost)
        + iv_loss * MERGE_COST_IV_LOSS_WEIGHT
        + protection_penalty
    )
    return {
        "pair_index": pair_index,
        "boundary": boundary,
        "left_rate": left_rate,
        "right_rate": right_rate,
        "abs_rate_diff": rate_gap,
        "p_value": p_value,
        "iv_loss": iv_loss,
        "is_protected_boundary": is_protected,
        "is_extreme_boundary": is_extreme_boundary,
        "merge_cost": cost,
    }
def choose_best_adjacent_pair(
    ranges: Sequence[Tuple[int, int]],
    initial_stats: pd.DataFrame,
    protected_boundaries: Set[int],
    extreme_boundaries: Optional[Set[int]] = None,
    blocked_boundaries: Optional[Set[int]] = None,
    allowed_pair_indices: Optional[Sequence[int]] = None,
    ignore_protection: bool = False,
) -> Dict[str, float]:
    """从允许的相邻箱中选择综合代价最低的一对。"""
    current_stats = aggregate_initial_stats_by_ranges(initial_stats, ranges)
    pair_indices = (
        list(allowed_pair_indices)
        if allowed_pair_indices is not None
        else list(range(len(ranges) - 1))
    )
    pair_indices = filter_blocked_pair_indices(
        ranges,
        pair_indices,
        blocked_boundaries,
    )
    if not pair_indices:
        raise ValueError("没有可合并的相邻箱")

    diagnostics = [
        pair_merge_diagnostics(
            current_stats,
            ranges,
            pair_index,
            initial_stats,
            protected_boundaries,
            extreme_boundaries=extreme_boundaries,
            ignore_protection=ignore_protection,
        )
        for pair_index in pair_indices
    ]
    return min(diagnostics, key=lambda item: item["merge_cost"])
def evaluate_merge_candidate(
    train_initial_stats: pd.DataFrame,
    ranges: Sequence[Tuple[int, int]],
    initial_iv: float,
    extreme_boundaries: Set[int],
    step_no: int,
    stage: str,
    merge_reason: str,
) -> Dict[str, object]:
    """计算一个候选合箱方案的完整评分指标。"""
    train_stats = aggregate_initial_stats_by_ranges(train_initial_stats, ranges)

    rate_cols = ALL_RISK_RATE_COLS
    train_primary_inversions = count_rate_inversions(
        train_stats,
        PRIMARY_RATE_COLS,
        tolerance=TRAIN_INVERSION_TOLERANCE,
    )
    train_all_inversions = count_rate_inversions(
        train_stats,
        rate_cols,
        tolerance=TRAIN_INVERSION_TOLERANCE,
    )

    constraint_details = calc_bin_constraint_details(train_stats)
    constraint_violation_count = int((~constraint_details["all_constraints_ok"]).sum())

    final_iv = calc_iv_from_stats(train_stats)
    iv_retention = safe_div(final_iv, initial_iv)

    adjacent_diffs = oriented_rate(train_stats[PRIMARY_RATE_COL]).diff().dropna()
    min_adjacent_rate_diff: float = (
        float(adjacent_diffs.min()) if not adjacent_diffs.empty else np.nan
    )
    # 双主指标：取两个指标中更小的相邻差异，确保任一指标区分度不足时都被识别。
    for rate_col in PRIMARY_RATE_COLS:
        diffs = oriented_rate(train_stats[rate_col]).diff().dropna()
        if not diffs.empty:
            col_min = float(diffs.min())
            if pd.isna(min_adjacent_rate_diff) or col_min < min_adjacent_rate_diff:
                min_adjacent_rate_diff = col_min

    final_bin_count = len(ranges)
    eligible_bin_count = MIN_FINAL_BIN_COUNT <= final_bin_count <= MAX_FINAL_BIN_COUNT
    extreme_boundary_violation_count = count_crossed_boundaries(ranges, extreme_boundaries)
    hard_constraints_ok = all(
        [
            eligible_bin_count,
            train_primary_inversions == 0,
            constraint_violation_count == 0,
            extreme_boundary_violation_count == 0,
        ]
    )

    iv_value = (
        0.0
        if pd.isna(iv_retention)
        else float(np.clip(iv_retention, 0, IV_RETENTION_SCORE_CAP))
    )
    min_sep_value = 0.0 if pd.isna(min_adjacent_rate_diff) else max(0.0, min_adjacent_rate_diff)
    weights = CANDIDATE_SCORE_WEIGHTS

    candidate_score = (
        weights["hard_constraints_ok"] * int(hard_constraints_ok)
        + weights["train_primary_inversion"] * train_primary_inversions
        + weights["train_all_inversion"] * train_all_inversions
        + weights["constraint_violation"] * constraint_violation_count
        + weights["iv_retention"] * iv_value
        + weights["min_adjacent_rate_diff"] * min_sep_value
        + weights["target_bin_distance"] * abs(final_bin_count - TARGET_FINAL_BIN_COUNT)
        + weights["extreme_boundary_violation"] * extreme_boundary_violation_count
    )

    return {
        "selected": False,
        "step_no": step_no,
        "stage": stage,
        "merge_reason": merge_reason,
        "hard_constraints_ok": hard_constraints_ok,
        "eligible_bin_count": eligible_bin_count,
        "final_bin_count": final_bin_count,
        "ranges": format_merge_ranges(ranges),
        "train_primary_inversion_cnt": train_primary_inversions,
        "train_all_inversion_cnt": train_all_inversions,
        "constraint_violation_count": constraint_violation_count,
        "extreme_boundary_violation_count": extreme_boundary_violation_count,
        "min_train_sample_pct": float(train_stats["sample_pct"].min()),
        "min_train_mature_count": float(train_stats[PRIMARY_MATURE_COL].min()),
        "min_train_bad_count": float(train_stats[PRIMARY_BAD_COL].min()),
        "min_train_good_count": float(train_stats[PRIMARY_GOOD_COL].min()),
        "primary_iv": final_iv,
        "primary_iv_retention": iv_retention,
        "min_adjacent_primary_rate_diff": min_adjacent_rate_diff,
        "candidate_score": candidate_score,
    }
def build_merge_candidate_score_table(
    train_initial_stats: pd.DataFrame,
    initial_bin_count: int,
    config: Dict,
) -> Tuple[pd.DataFrame, pd.DataFrame, Set[int]]:
    """
    按可解释的分阶段流程生成候选合箱方案。

    阶段一：清理样本、成熟量、坏样本或好样本不足的箱；
    阶段二：使用双主指标 1M30+/3M30+ 执行 PAVA 风格单调合并；
    阶段三：按相邻风险差异、显著性、IV 损失和策略边界保护压缩到 6~8 档；
    阶段四：继续生成 8、7、6 档候选，并基于完整 Train 评分选择。
    """
    ranges: List[Tuple[int, int]] = [(idx, idx) for idx in range(1, initial_bin_count + 1)]
    extreme_boundaries = identify_extreme_boundaries(initial_bin_count)
    protected_boundaries = identify_protected_boundaries(train_initial_stats, config)
    hard_blocked_boundaries = (
        set()
        if ALLOW_EXTREME_BIN_MERGE_FOR_HARD_CONSTRAINTS
        else set(extreme_boundaries)
    )
    initial_iv = calc_iv_from_stats(train_initial_stats)

    candidate_rows: List[Dict[str, object]] = []
    step_rows: List[Dict[str, object]] = []
    step_no = 0

    def perform_merge(
        pair_index: int,
        stage: str,
        reason: str,
        diagnostics: Dict[str, float],
    ) -> None:
        nonlocal ranges, step_no
        before_ranges = list(ranges)
        left_range = ranges[pair_index]
        right_range = ranges[pair_index + 1]
        ranges = merge_ranges_at(ranges, pair_index)
        step_no += 1

        step_rows.append(
            {
                "step_no": step_no,
                "stage": stage,
                "merge_reason": reason,
                "left_range": str(left_range),
                "right_range": str(right_range),
                "merged_range": str(ranges[pair_index]),
                "boundary": diagnostics.get("boundary"),
                "is_protected_boundary": diagnostics.get("is_protected_boundary"),
                "is_extreme_boundary": diagnostics.get("is_extreme_boundary"),
                "left_primary_rate": diagnostics.get("left_rate"),
                "right_primary_rate": diagnostics.get("right_rate"),
                "abs_primary_rate_diff": diagnostics.get("abs_rate_diff"),
                "two_proportion_p_value": diagnostics.get("p_value"),
                "primary_iv_loss": diagnostics.get("iv_loss"),
                "before_ranges": format_merge_ranges(before_ranges),
                "after_ranges": format_merge_ranges(ranges),
                "after_bin_count": len(ranges),
            }
        )
        candidate_rows.append(
            evaluate_merge_candidate(
                train_initial_stats,
                ranges,
                initial_iv,
                extreme_boundaries,
                step_no,
                stage,
                reason,
            )
        )

    # 0. 初始状态仅用于过程记录。
    candidate_rows.append(
        evaluate_merge_candidate(
            train_initial_stats,
            ranges,
            initial_iv,
            extreme_boundaries,
            step_no,
            "initial",
            "20 等频初始箱",
        )
    )

    # 1. 小箱清理。
    while len(ranges) > MIN_FINAL_BIN_COUNT:
        current_stats = aggregate_initial_stats_by_ranges(train_initial_stats, ranges)
        constraints = calc_bin_constraint_details(current_stats)
        violating = constraints.loc[~constraints["all_constraints_ok"]]
        if violating.empty:
            break

        target_order = int(
            violating.sort_values("violation_severity", ascending=False).iloc[0][
                "final_bin_order"
            ]
        )
        target_index = target_order - 1
        allowed_pairs = []
        if target_index > 0:
            allowed_pairs.append(target_index - 1)
        if target_index < len(ranges) - 1:
            allowed_pairs.append(target_index)

        try:
            diagnostics = choose_best_adjacent_pair(
                ranges,
                train_initial_stats,
                protected_boundaries,
                extreme_boundaries=extreme_boundaries,
                blocked_boundaries=hard_blocked_boundaries,
                allowed_pair_indices=allowed_pairs,
                ignore_protection=True,
            )
        except ValueError:
            break
        perform_merge(
            int(diagnostics["pair_index"]),
            "small_bin_cleanup",
            "样本占比、成熟量或好坏样本量不足",
            diagnostics,
        )

    # 2. PAVA 风格主指标单调合并。
    while len(ranges) > MIN_FINAL_BIN_COUNT:
        current_stats = aggregate_initial_stats_by_ranges(train_initial_stats, ranges)
        inversion_pairs = primary_inversion_pair_indices(current_stats)
        if not inversion_pairs:
            break

        # 从倒挂最严重的一对开始处理（双主指标取跌幅最大者）。
        oriented = {
            col: oriented_rate(current_stats[col])
            for col in PRIMARY_RATE_COLS
        }
        pair_index = min(
            inversion_pairs,
            key=lambda idx: min(
                oriented[col].iloc[idx + 1] - oriented[col].iloc[idx]
                for col in PRIMARY_RATE_COLS
            ),
        )
        try:
            diagnostics = choose_best_adjacent_pair(
                ranges,
                train_initial_stats,
                protected_boundaries,
                extreme_boundaries=extreme_boundaries,
                blocked_boundaries=hard_blocked_boundaries,
                allowed_pair_indices=[pair_index],
                ignore_protection=True,
            )
        except ValueError:
            break
        perform_merge(
            pair_index,
            "pava_monotonic_merge",
            "主指标（金额口径）1M30+/3M30+ 出现相邻倒挂",
            diagnostics,
        )

    # 3. 如果档位仍多于上限，强制压缩到 MAX_FINAL_BIN_COUNT。
    while len(ranges) > MAX_FINAL_BIN_COUNT:
        try:
            diagnostics = choose_best_adjacent_pair(
                ranges,
                train_initial_stats,
                protected_boundaries,
                extreme_boundaries=extreme_boundaries,
                blocked_boundaries=hard_blocked_boundaries,
            )
        except ValueError:
            break
        perform_merge(
            int(diagnostics["pair_index"]),
            "granularity_reduction",
            "档位数量超过上限，选择信息损失最小的相邻箱",
            diagnostics,
        )

    # 4. 继续生成 7 档和 6 档候选。
    while len(ranges) > MIN_FINAL_BIN_COUNT:
        current_stats = aggregate_initial_stats_by_ranges(train_initial_stats, ranges)
        candidate_pair_indices = filter_blocked_pair_indices(
            ranges,
            range(len(ranges) - 1),
            hard_blocked_boundaries,
        )
        if not candidate_pair_indices:
            break
        all_diagnostics = [
            pair_merge_diagnostics(
                current_stats,
                ranges,
                pair_index,
                train_initial_stats,
                protected_boundaries,
                extreme_boundaries=extreme_boundaries,
            )
            for pair_index in candidate_pair_indices
        ]

        statistically_similar = [
            item
            for item in all_diagnostics
            if (
                (pd.notna(item["p_value"]) and item["p_value"] >= ADJACENT_PVALUE_TO_MERGE)
                or item["abs_rate_diff"] <= MIN_ADJACENT_ABS_RATE_DIFF
            )
        ]
        diagnostics = min(
            statistically_similar or all_diagnostics,
            key=lambda item: item["merge_cost"],
        )
        reason = (
            "相邻风险差异不显著或风险率接近"
            if statistically_similar
            else "生成更精简候选档位，选择信息损失最小的相邻箱"
        )
        perform_merge(
            int(diagnostics["pair_index"]),
            "candidate_reduction",
            reason,
            diagnostics,
        )

    candidates = pd.DataFrame(candidate_rows).drop_duplicates(subset=["ranges"], keep="last")

    # 分布整形：若启用单箱样本占比上限，为超限候选生成"均衡拆分 + 相邻再合并"的整形方案。
    if MAX_FINAL_BIN_SHARE and MAX_FINAL_BIN_SHARE > 0:
        extra_rows: List[Dict[str, object]] = []
        for _, cand in candidates.iterrows():
            cand_ranges = parse_merge_ranges(str(cand["ranges"]))
            refined = refine_ranges_under_share_cap(
                cand_ranges,
                train_initial_stats,
                protected_boundaries,
                extreme_boundaries,
                MAX_FINAL_BIN_SHARE,
                int(cand["final_bin_count"]),
            )
            if refined is None or refined == cand_ranges:
                continue
            extra_rows.append(
                evaluate_merge_candidate(
                    train_initial_stats,
                    refined,
                    initial_iv,
                    extreme_boundaries,
                    step_no + 1,
                    "share_balancing",
                    "单箱样本占比超过上限，均衡拆分并合并相邻箱",
                )
            )
        if extra_rows:
            candidates = pd.concat(
                [candidates, pd.DataFrame(extra_rows)],
                ignore_index=True,
            ).drop_duplicates(subset=["ranges"], keep="last")

    candidates["target_bin_distance"] = (
        candidates["final_bin_count"] - TARGET_FINAL_BIN_COUNT
    ).abs()

    eligible = candidates.loc[candidates["eligible_bin_count"]].copy()
    selection_pool = eligible if not eligible.empty else candidates.copy()
    selection_pool = selection_pool.sort_values(
        [
            "hard_constraints_ok",
            "train_primary_inversion_cnt",
            "constraint_violation_count",
            "train_all_inversion_cnt",
            "candidate_score",
            "primary_iv_retention",
            "target_bin_distance",
        ],
        ascending=[False, True, True, True, False, False, True],
        na_position="last",
    )

    if selection_pool.empty:
        raise ValueError("未生成任何可用合箱候选方案")

    selected_ranges_text = selection_pool.iloc[0]["ranges"]
    candidates.loc[candidates["ranges"].eq(selected_ranges_text), "selected"] = True
    candidates = candidates.sort_values(
        ["selected", "hard_constraints_ok", "candidate_score", "final_bin_count"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)

    steps = pd.DataFrame(step_rows)
    return candidates, steps, protected_boundaries
def refine_ranges_under_share_cap(
    ranges: Sequence[Tuple[int, int]],
    train_initial_stats: pd.DataFrame,
    protected_boundaries: Set[int],
    extreme_boundaries: Set[int],
    max_share: float,
    target_bin_count: int,
) -> Optional[List[Tuple[int, int]]]:
    """
    单箱样本占比上限整形：把样本占比超过上限的箱沿低风险侧优先的可行拆点拆成两个，
    若档数超出目标则合并代价最小的相邻对回到目标档数。

    返回整形后的范围列表；无法整形（拆不出合规子箱或合不回去）时返回 None。
    """
    stats = train_initial_stats.sort_values("bin_order").reset_index(drop=True)
    total_n = float(stats["n"].sum())

    def range_share(start: int, end: int) -> float:
        part = stats.loc[stats["bin_order"].between(start, end, inclusive="both")]
        return float(part["n"].sum()) / total_n

    def max_share_of(current: Sequence[Tuple[int, int]]) -> float:
        return max(range_share(start, end) for start, end in current)

    refined = list(ranges)
    for _ in range(16):
        overlarge = [
            (start, end)
            for start, end in refined
            if range_share(start, end) > max_share
        ]
        if not overlarge:
            break
        start, end = max(overlarge, key=lambda r: range_share(*r))
        # 从低风险侧取第一个可行拆点：低风险端样本密集，优先拆小。
        best_split = None
        for mid in range(start, end):
            if range_share(start, mid) <= max_share and range_share(mid + 1, end) <= max_share:
                best_split = mid
                break
        if best_split is None:
            return None
        idx = refined.index((start, end))
        refined = (
            refined[:idx] + [(start, best_split), (best_split + 1, end)] + refined[idx + 1 :]
        )

    while len(refined) > target_bin_count:
        current_stats = aggregate_initial_stats_by_ranges(train_initial_stats, refined)
        pair_indices = filter_blocked_pair_indices(
            refined,
            list(range(len(refined) - 1)),
            extreme_boundaries,
        )
        feasible = []
        for pair_index in pair_indices:
            merged = merge_ranges_at(refined, pair_index)
            if max_share_of(merged) <= max_share:
                feasible.append((pair_index, merged))
        if not feasible:
            return None
        diagnostics, merged = min(
            (
                (
                    pair_merge_diagnostics(
                        current_stats,
                        refined,
                        pair_index,
                        train_initial_stats,
                        protected_boundaries,
                        extreme_boundaries=extreme_boundaries,
                    ),
                    merged_ranges,
                )
                for pair_index, merged_ranges in feasible
            ),
            key=lambda item: item[0]["merge_cost"],
        )
        del diagnostics
        refined = merged

    return refined
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
        "last_accepted_marginal_3m30p_amt_bad_rate": accept_row[
            "marginal_3m30p_amt_bad_rate"
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
                "auto_1m30p_amt_bad_rate": float(auto_row["cum_1m30p_amt_bad_rate"]),
                "auto_3m30p_amt_bad_rate": float(auto_row["cum_3m30p_amt_bad_rate"]),
                "accept_3m30p_amt_bad_rate": float(accept_row["cum_3m30p_amt_bad_rate"]),
                "accept_marginal_3m30p_amt_bad_rate": float(
                    accept_row["marginal_3m30p_amt_bad_rate"]
                ),
                # 笔数口径作为参考列保留。
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
        "auto_1m30p_amt_bad_rate",
        "auto_3m30p_amt_bad_rate",
        "accept_3m30p_amt_bad_rate",
        "accept_marginal_3m30p_amt_bad_rate",
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
        # 金额口径：倒挂标记按金额逾期率判定（主指标）。
        rate_col = f"{prefix}_amt_bad_rate"
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
        # 金额口径主指标列（倒挂标记基于金额率）。
        "1m30p_amt_exposure",
        "1m30p_amt_bad",
        "1m30p_amt_bad_rate",
        "3m30p_amt_exposure",
        "3m30p_amt_bad",
        "3m30p_amt_bad_rate",
        "1m30p_rate_diff_prev",
        "1m30p_inversion_flag",
        "3m30p_rate_diff_prev",
        "3m30p_inversion_flag",
        # 笔数口径参考列。
        "1m30p_cnt_mature",
        "1m30p_cnt_bad",
        "1m30p_cnt_bad_rate",
        "3m30p_cnt_mature",
        "3m30p_cnt_bad",
        "3m30p_cnt_bad_rate",
        "cum_pass_rate",
        "cum_1m30p_amt_bad_rate",
        "cum_3m30p_amt_bad_rate",
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
        # 金额口径主展示（约束判定基于金额累计率）。
        "cum_1m30p_amt_exposure",
        "cum_1m30p_amt_bad",
        "cum_1m30p_amt_bad_rate",
        "cum_3m30p_amt_exposure",
        "cum_3m30p_amt_bad",
        "cum_3m30p_amt_bad_rate",
        "marginal_sample_pct",
        "marginal_n",
        "marginal_3m30p_amt_exposure",
        "marginal_3m30p_amt_bad",
        "marginal_3m30p_amt_bad_rate",
        "auto_all_constraints_ok",
        "accept_all_constraints_ok",
        # 笔数口径参考列（含 95% Wilson CI 上界）。
        "cum_1m30p_cnt_mature",
        "cum_1m30p_cnt_bad",
        "cum_1m30p_cnt_bad_rate",
        "cum_3m30p_cnt_mature",
        "cum_3m30p_cnt_bad",
        "cum_3m30p_cnt_bad_rate",
        "cum_3m30p_cnt_bad_rate_ci_high",
        "marginal_3m30p_cnt_mature",
        "marginal_3m30p_cnt_bad",
        "marginal_3m30p_cnt_bad_rate",
        "marginal_3m30p_cnt_bad_rate_ci_high",
    ]
    remaining_columns = [col for col in result.columns if col not in first_columns]
    return result[[col for col in first_columns if col in result.columns] + remaining_columns]
def build_metric_dictionary() -> pd.DataFrame:
    """输出 Excel 核心字段和计算口径说明。"""
    rows = [
        ("通用", "n", "箱内或区间内的申请样本量", "COUNT(application_id)"),
        ("通用", "sample_pct", "箱内样本占全部样本的比例", "n / total_n"),
        ("通用", "principal_amt", "箱内样本本金合计", "SUM(principal)"),
        ("分箱", "score_left / score_right", "分箱的模型分左右边界", "(score_left, score_right]"),
        ("分箱", "merged_from", "最终风险档位由哪些初始箱合并而来", "例如 B06-B08"),
        ("分箱", "extreme_bin_role", "最好/最坏极端箱标识", "由 PROTECT_EXTREME_INITIAL_BINS 相关配置生成"),
        ("分箱", "extreme_boundary_violation_count", "候选方案跨越极端保护边界的数量", "0 表示最好/最坏极端边界被保留"),
        ("金额风险（主指标）", "1m30p_amt_exposure", "1M30+ 已成熟样本的本金敞口", "SUM(principal) WHERE MOB1 已成熟"),
        ("金额风险（主指标）", "1m30p_amt_bad", "MOB1 30+ 样本的剩余本金", "SUM(estimate_principal_remaining_mob1)"),
        ("金额风险（主指标）", "1m30p_amt_bad_rate", "1M30+ 金额逾期率（主指标）", "1m30p_amt_bad / 1m30p_amt_exposure"),
        ("金额风险（主指标）", "1m30p_amt_lift", "1M30+ 金额 Lift", "1m30p_amt_bad_rate ÷ 整体 1M30+ 金额逾期率"),
        ("金额风险（主指标）", "3m30p_amt_exposure", "3M30+ 已成熟样本的本金敞口", "SUM(principal) WHERE MOB3 已成熟"),
        ("金额风险（主指标）", "3m30p_amt_bad", "MOB3 30+ 样本的剩余本金", "SUM(estimate_principal_remaining_mob3)"),
        ("金额风险（主指标）", "3m30p_amt_bad_rate", "3M30+ 金额逾期率（主指标，合箱/约束/阈值锚定）", "3m30p_amt_bad / 3m30p_amt_exposure"),
        ("金额风险（主指标）", "3m30p_amt_good", "3M30+ 已成熟样本的未逾期剩余本金", "MAX(0, 3m30p_amt_exposure - 3m30p_amt_bad)"),
        ("金额风险（主指标）", "3m30p_amt_lift", "3M30+ 金额 Lift", "3m30p_amt_bad_rate ÷ 整体 3M30+ 金额逾期率"),
        ("金额风险（主指标）", "cum_1m30p_amt_exposure / cum_1m30p_amt_bad", "累计 1M30+ 本金敞口 / 逾期剩余本金", "按 bin_order 从低风险向高风险逐箱累加"),
        ("金额风险（主指标）", "cum_1m30p_amt_bad_rate", "累计 1M30+ 金额逾期率", "cum_1m30p_amt_bad / cum_1m30p_amt_exposure"),
        ("金额风险（主指标）", "cum_3m30p_amt_exposure / cum_3m30p_amt_bad", "累计 3M30+ 本金敞口 / 逾期剩余本金", "按 bin_order 从低风险向高风险逐箱累加"),
        ("金额风险（主指标）", "cum_3m30p_amt_bad_rate", "累计 3M30+ 金额逾期率", "cum_3m30p_amt_bad / cum_3m30p_amt_exposure"),
        ("笔数风险（参考口径）", "1m30p_cnt_mature", "1M30+ 已成熟样本量", "duedate_1m_30 IN (0, 1)"),
        ("笔数风险（参考口径）", "1m30p_cnt_bad", "1M30+ 逾期样本量", "duedate_1m_30 = 1"),
        ("笔数风险（参考口径）", "1m30p_cnt_bad_rate", "1M30+ 笔数逾期率", "1m30p_cnt_bad / 1m30p_cnt_mature"),
        ("笔数风险（参考口径）", "1m30p_cnt_lift", "1M30+ 笔数 Lift", "1m30p_cnt_bad_rate ÷ 整体 1M30+ 笔数逾期率"),
        ("笔数风险（参考口径）", "3m30p_cnt_mature", "3M30+ 已成熟样本量", "duedate_3m_30 IN (0, 1)"),
        ("笔数风险（参考口径）", "3m30p_cnt_bad", "3M30+ 逾期样本量", "duedate_3m_30 = 1"),
        ("笔数风险（参考口径）", "3m30p_cnt_bad_rate", "3M30+ 笔数逾期率", "3m30p_cnt_bad / 3m30p_cnt_mature"),
        ("笔数风险（参考口径）", "3m30p_cnt_lift", "3M30+ 笔数 Lift", "3m30p_cnt_bad_rate ÷ 整体 3M30+ 笔数逾期率"),
        ("笔数风险（参考口径）", "1m30p_cnt_bad_rate_ci_low / ci_high", "1M30+ 笔数逾期率 95% Wilson 置信区间下/上界", "Wilson 区间（z=1.96）；成熟量为 0 时为空"),
        ("笔数风险（参考口径）", "3m30p_cnt_bad_rate_ci_low / ci_high", "3M30+ 笔数逾期率 95% Wilson 置信区间下/上界", "Wilson 区间（z=1.96）；成熟量为 0 时为空"),
        ("笔数风险（参考口径）", "cum_1m30p_cnt_mature / cum_1m30p_cnt_bad", "累计 1M30+ 已成熟样本量 / 逾期样本量", "按 bin_order 从低风险向高风险逐箱累加"),
        ("笔数风险（参考口径）", "cum_1m30p_cnt_bad_rate", "累计 1M30+ 笔数逾期率", "cum_1m30p_cnt_bad / cum_1m30p_cnt_mature"),
        ("笔数风险（参考口径）", "cum_3m30p_cnt_mature / cum_3m30p_cnt_bad", "累计 3M30+ 已成熟样本量 / 逾期样本量", "按 bin_order 从低风险向高风险逐箱累加"),
        ("笔数风险（参考口径）", "cum_3m30p_cnt_bad_rate", "累计 3M30+ 笔数逾期率", "cum_3m30p_cnt_bad / cum_3m30p_cnt_mature"),
        ("笔数风险（参考口径）", "cum_*_cnt_bad_rate_ci_low / ci_high", "累计笔数逾期率的 95% Wilson 置信区间下/上界", "按累计成熟量与逾期量计算"),
        ("模型策略测算", "cum_pass_rate", "从低风险端累计到当前阈值的模型策略测算通过率", "cum_n / total_n"),
        ("阈值", "marginal_sample_pct", "当前档位新增样本占比", "marginal_n / total_n"),
        ("阈值", "marginal_3m30p_amt_bad_rate", "当前新增档位自身的 3M30+ 金额逾期率（主指标）", "marginal_3m30p_amt_bad / marginal_3m30p_amt_exposure"),
        ("阈值", "marginal_3m30p_cnt_bad_rate", "当前新增档位自身的 3M30+ 笔数逾期率（参考）", "marginal_bad / marginal_mature"),
        ("阈值", "marginal_*_cnt_bad_rate_ci_low / ci_high", "边际档位笔数逾期率 95% Wilson 置信区间下/上界", "按边际档位成熟量与逾期量计算"),
        ("阈值", "auto_all_constraints_ok", "该候选阈值是否满足自动通过全部约束", "全部自动通过检查项均为 True"),
        ("阈值", "accept_all_constraints_ok", "该候选阈值是否满足整体接纳全部约束", "全部整体接纳检查项均为 True"),
        ("历史实际审批漏斗", "actual_completion_rate", "历史实际进件完成率", "completed_application_cnt / apply_cnt"),
        ("历史实际审批漏斗", "actual_approval_rate", "历史实际审批通过率", "approved_application_cnt / completed_application_cnt"),
        ("历史实际审批漏斗", "actual_auto_approval_rate", "历史实际自动审批通过率", "auto_approved_application_cnt / completed_application_cnt"),
        ("历史实际审批漏斗", "actual_manual_approval_rate", "历史实际人工审批通过率", "manual_approved_application_cnt / completed_application_cnt"),
        ("历史实际审批漏斗", "actual_auto_approval_share", "历史实际通过件中的自动审批占比", "auto_approved_application_cnt / approved_application_cnt"),
        ("历史实际审批漏斗", "actual_manual_approval_share", "历史实际通过件中的人工审批占比", "manual_approved_application_cnt / approved_application_cnt"),
        ("历史实际审批漏斗", "actual_deal_rate", "历史实际成交转化率", "deal_sample_cnt / approved_application_cnt"),
        ("箱级历史实际审批漏斗", "actual_*（03_最终分箱统计）", "按 Train/OOT 与最终风险档下钻的历史实际数量和比率", "在每个 score_mlt_final_bin 内按唯一 application_id 复算"),
        ("模型策略测算", "strategy_estimated_auto_pass_rate", "模型阈值测算的自动通过样本占比", "score_mlt 满足自动通过阈值的申请数 / 有效模型分申请数"),
        ("模型策略测算", "strategy_estimated_manual_review_rate", "模型阈值测算的人工审核样本占比", "人工审核分数区间申请数 / 有效模型分申请数"),
        ("模型策略测算", "strategy_estimated_total_accept_rate", "模型阈值测算的总接纳样本占比", "自动通过数与人工审核数之和 / 有效模型分申请数"),
        ("模型策略测算", "strategy_estimated_reject_rate", "模型阈值测算的拒绝样本占比", "超过总接纳阈值的申请数 / 有效模型分申请数"),
        ("箱级模型策略测算", "strategy_estimated_decision", "最终风险档对应的策略归属", "按自动通过阈值和总接纳阈值映射为自动通过/人工审核/拒绝"),
        ("箱级模型策略测算", "strategy_estimated_bin_flow_rate", "该风险档对策略流量的贡献", "箱内申请数 / 当前样本组有效模型分申请数"),
        ("箱级模型策略测算", "strategy_estimated_cumulative_flow_rate", "从低风险端累计至当前档的策略流量", "累计申请数 / 当前样本组有效模型分申请数"),
        ("分箱表整体指标", "strategy_estimated_overall_*_rate", "当前 Train/OOT 样本组的整体策略测算转化率", "整体指标在分箱表中独立成列并重复展示；不作为单箱指标解释"),
        ("分箱表整体指标", "overall_1m30p_auc / overall_3m30p_auc", "当前 Train/OOT 样本组的整体 AUC", "整体指标在分箱表中独立成列并重复展示；AUC 不定义为单箱指标"),
        ("分箱表整体指标", "overall_1m30p_ks / overall_3m30p_ks", "当前 Train/OOT 样本组的整体 KS", "整体指标在分箱表中独立成列并重复展示；与箱级 *_ks_curve 区分"),
        ("箱级模型诊断", "*_iv_component", "1M30+/3M30+ 的箱级 IV 分项（金额加权）", "(bad_dist-good_dist) * LN(bad_dist/good_dist)，bad/good 取金额口径（amt_bad / amt_good）"),
        ("箱级模型诊断", "*_ks_curve", "由高风险端累计至当前档的 KS 曲线值", "ABS(cum_bad_dist_from_high-cum_good_dist_from_high)"),
        ("箱级模型诊断", "train_oot_psi_component", "当前风险档对 Train/OOT PSI 的贡献", "(OOT%-Train%) * LN(OOT%/Train%)"),
        ("验证", "PSI", "Train 与 OOT 的分箱分布稳定性", "SUM((OOT%-Train%) * LN(OOT%/Train%))"),
        ("验证", "AUC / KS", "模型风险区分能力指标", "分别衡量排序能力和好坏样本累计差异"),
    ]
    return pd.DataFrame(rows, columns=["category", "field", "definition", "calculation"])
def build_monthly_bin_stability(data: pd.DataFrame) -> pd.DataFrame:
    """按月份、样本组和最终风险档输出箱级稳定性指标。"""
    rows = []
    valid = data.loc[
        data["sample_group"].isin(["train", "oot"])
        & data["application_month"].notna()
        & data[FINAL_BIN_COL].notna()
    ].copy()

    for (sample_group, application_month), month_data in valid.groupby(
        ["sample_group", "application_month"],
        observed=True,
    ):
        stats = calc_bin_stats(
            month_data,
            bin_col=FINAL_BIN_COL,
            order_col="bin_order",
        )
        stats.insert(0, "application_month", application_month)
        stats.insert(0, "sample_group", sample_group)
        rows.append(stats)

    if not rows:
        return pd.DataFrame()

    result = pd.concat(rows, ignore_index=True)
    result["primary_rate_diff_prev"] = result.groupby(
        ["sample_group", "application_month"],
        observed=True,
    )[PRIMARY_RATE_COL].diff()
    if not HIGH_SCORE_HIGH_RISK:
        result["primary_rate_diff_prev"] = -result["primary_rate_diff_prev"]
    result["primary_inversion_flag"] = (
        result["primary_rate_diff_prev"] < -MONTHLY_INVERSION_TOLERANCE
    )
    return result
def build_monthly_stability_summary(monthly_stats: pd.DataFrame) -> pd.DataFrame:
    """汇总每个月的主风险指标单调性和样本表现。"""
    if monthly_stats.empty:
        return pd.DataFrame()

    return (
        monthly_stats.groupby(["sample_group", "application_month"], observed=True)
        .agg(
            n=("n", "sum"),
            mature_count=(PRIMARY_MATURE_COL, "sum"),
            bad_count=(PRIMARY_BAD_COL, "sum"),
            amt_bad_sum=(PRIMARY_AMT_BAD_COL, "sum"),
            amt_exposure_sum=(PRIMARY_AMT_EXPOSURE_COL, "sum"),
            bin_count=(FINAL_BIN_COL, "nunique"),
            primary_inversion_count=("primary_inversion_flag", "sum"),
            max_primary_rate_drop=("primary_rate_diff_prev", lambda s: float((-s).clip(lower=0).max())),
        )
        .reset_index()
        .assign(
            # 月度主风险指标为金额口径；笔数成熟/坏账数仅作参考展示。
            primary_bad_rate=lambda frame: safe_div(
                frame["amt_bad_sum"], frame["amt_exposure_sum"]
            ),
            primary_monotonic_ok=lambda frame: frame["primary_inversion_count"].eq(0),
        )
    )
def build_overview(
    data: pd.DataFrame,
    train: pd.DataFrame,
    oot: pd.DataFrame,
    initial_bin_count: int,
    final_bin_count: int,
    selected_merge_ranges: Sequence[Tuple[int, int]],
    selected_candidate: Optional[pd.Series],
    protected_boundaries: Set[int],
    psi: pd.DataFrame,
    performance: pd.DataFrame,
    monotonicity: pd.DataFrame,
    strategy_plan: pd.DataFrame,
    actual_funnel_report: pd.DataFrame,
    strategy_estimated_flow: pd.DataFrame,
) -> pd.DataFrame:
    """整理报告首页的核心结论，并按模块分组展示。"""
    source_row_count = int(data.attrs.get("source_row_count", len(data)))
    removed_incomplete_count = int(data.attrs.get("removed_incomplete_count", 0))
    score_missing_count = int(data.attrs.get("score_missing_count", 0))

    rows = [
        ("样本", "原始样本量", source_row_count),
        ("样本", "剔除未完成申请量", removed_incomplete_count),
        ("样本", "剔除未完成申请率", safe_div(removed_incomplete_count, source_row_count)),
        ("样本", "有效模型分样本量", len(data)),
        ("样本", "模型分缺失量", score_missing_count),
        ("样本", "模型分缺失率", safe_div(score_missing_count, source_row_count)),
        ("样本", "Train 样本量", len(train)),
        ("样本", "OOT 样本量", len(oot)),
        ("时间切分", "Train 截止月份", TRAIN_END_MONTH),
        ("时间切分", "OOT 起始月份", OOT_START_MONTH),
        ("分箱", "初始箱数量", initial_bin_count),
        ("分箱", "最终箱数量", final_bin_count),
        ("分箱", "合箱主指标", " / ".join(PRIMARY_RATE_COLS)),
        ("分箱", "最终采用合箱方案", format_merge_ranges(selected_merge_ranges)),
        ("分箱", "受保护初始边界", ",".join(map(str, sorted(protected_boundaries)))),
        ("稳定性", "最终箱 Train/OOT PSI", psi["psi_total"].iloc[0]),
    ]

    if selected_candidate is not None:
        rows.extend(
            [
                ("候选评分", "Train 主指标倒挂数", selected_candidate.get("train_primary_inversion_cnt")),
                ("候选评分", "Train 全指标倒挂数", selected_candidate.get("train_all_inversion_cnt")),
                ("候选评分", "主指标 IV 保留率", selected_candidate.get("primary_iv_retention")),
                ("候选评分", "候选综合得分", selected_candidate.get("candidate_score")),
            ]
        )

    for _, perf in performance.iterrows():
        prefix = f"{perf['sample_group']}_{perf['label']}"
        rows.extend(
            [
                ("模型效果", f"{prefix}_bad_rate", perf["bad_rate"]),
                ("模型效果", f"{prefix}_auc", perf["auc"]),
                ("模型效果", f"{prefix}_ks", perf["ks"]),
            ]
        )

    for sample_group in ["train", "oot"]:
        sample_check = monotonicity.loc[monotonicity["sample_group"].eq(sample_group)]
        if sample_check.empty:
            continue
        rows.append(
            (
                "单调性",
                f"{sample_group}_最终箱全部单调",
                bool(sample_check["is_monotonic_non_decreasing"].all()),
            )
        )

    for sample_group in ["Train", "OOT"]:
        actual_rows = actual_funnel_report.loc[
            actual_funnel_report["sample_group"].eq(sample_group)
        ]
        if not actual_rows.empty:
            actual = actual_rows.iloc[0]
            rows.extend(
                [
                    ("历史实际审批漏斗", f"{sample_group}_进件完成率", actual["actual_completion_rate"]),
                    ("历史实际审批漏斗", f"{sample_group}_审批通过率", actual["actual_approval_rate"]),
                    ("历史实际审批漏斗", f"{sample_group}_自动审批通过率", actual["actual_auto_approval_rate"]),
                    ("历史实际审批漏斗", f"{sample_group}_人工审批通过率", actual["actual_manual_approval_rate"]),
                    ("历史实际审批漏斗", f"{sample_group}_自动审批占比", actual["actual_auto_approval_share"]),
                    ("历史实际审批漏斗", f"{sample_group}_人工审批占比", actual["actual_manual_approval_share"]),
                    ("历史实际审批漏斗", f"{sample_group}_成交转化率", actual["actual_deal_rate"]),
                ]
            )

        estimated_rows = strategy_estimated_flow.loc[
            strategy_estimated_flow["sample_group"].eq(sample_group)
        ]
        if not estimated_rows.empty:
            estimated = estimated_rows.iloc[0]
            rows.extend(
                [
                    ("模型策略测算流量", f"{sample_group}_测算自动通过率", estimated["strategy_estimated_auto_pass_rate"]),
                    ("模型策略测算流量", f"{sample_group}_测算人工审核率", estimated["strategy_estimated_manual_review_rate"]),
                    ("模型策略测算流量", f"{sample_group}_测算总接纳率", estimated["strategy_estimated_total_accept_rate"]),
                    ("模型策略测算流量", f"{sample_group}_测算拒绝率", estimated["strategy_estimated_reject_rate"]),
                ]
            )

    valid_strategy = strategy_plan.loc[strategy_plan["status"].eq("OK")]
    if not valid_strategy.empty:
        row = valid_strategy.iloc[0]
        rows.extend(
            [
                ("模型策略阈值", "策略名称", row["strategy_name"]),
                ("模型策略阈值", "自动通过阈值", row["auto_pass_threshold"]),
                ("模型策略阈值", "自动通过截止风险档", row["auto_pass_bin"]),
                ("模型策略阈值", "人工审核上限/拒绝阈值", row["reject_threshold"]),
                ("模型策略阈值", "人工审核截止风险档", row["manual_review_upper_bin"]),
                ("策略风险", "接纳人群1M30+金额逾期率", row["accepted_1m30p_amt_bad_rate"]),
                ("策略风险", "接纳人群3M30+金额逾期率", row["accepted_3m30p_amt_bad_rate"]),
                ("策略风险", "最后接纳档边际3M30+金额逾期率", row["last_accepted_marginal_3m30p_amt_bad_rate"]),
                ("策略风险", "接纳人群1M30+笔数逾期率(参考)", row["accepted_1m30p_cnt_bad_rate"]),
                ("策略风险", "接纳人群3M30+笔数逾期率(参考)", row["accepted_3m30p_cnt_bad_rate"]),
            ]
        )
    else:
        status = strategy_plan.iloc[0]["status"] if not strategy_plan.empty else "未生成"
        rows.append(("策略", "策略状态", status))

    return pd.DataFrame(rows, columns=["section", "metric", "value"])
