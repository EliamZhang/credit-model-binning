# -*- coding: utf-8 -*-
"""笔数口径合箱管线：20 等频初分、四阶段自动合箱、候选评分、极端箱保护与分布整形。

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


from pipeline.common import safe_div
from pipeline.risk_metrics import add_bin_derived_metrics, calc_bin_stats, calc_iv_from_stats, two_proportion_pvalue



def learn_equal_freq_edges(
    data: pd.DataFrame,
    score_col: str,
    n_bins: int,
) -> np.ndarray:
    """仅在 Train 上学习等频边界，并将首尾扩展为无穷。"""
    score = pd.to_numeric(data[score_col], errors="coerce").dropna()
    if score.empty:
        raise ValueError(f"{score_col} 全为空，无法分箱")

    _, raw_edges = pd.qcut(score, q=n_bins, retbins=True, duplicates="drop")
    edges = np.unique(np.asarray(raw_edges, dtype="float64"))
    if len(edges) < 2:
        raise ValueError(f"{score_col} 唯一值不足，无法形成有效分箱")

    edges[0] = -np.inf
    edges[-1] = np.inf
    return edges
def build_initial_edge_table(edges: np.ndarray) -> pd.DataFrame:
    """生成初始分箱边界配置表。"""
    rows = []
    for idx in range(len(edges) - 1):
        order = idx + 1
        rows.append(
            {
                "bin_order": order,
                INITIAL_BIN_COL: f"B{order:02d}",
                "score_left": edges[idx],
                "score_right": edges[idx + 1],
            }
        )
    return pd.DataFrame(rows)
def apply_edges(
    data: pd.DataFrame,
    score_col: str,
    edges: np.ndarray,
    bin_col: str,
) -> pd.DataFrame:
    """将 Train 学到的边界复用到任意样本。"""
    result = data.copy()
    labels = list(range(1, len(edges)))
    cut_result = pd.cut(
        pd.to_numeric(result[score_col], errors="coerce"),
        bins=edges,
        labels=labels,
        include_lowest=True,
        right=True,
    )
    result["initial_bin_order"] = cut_result.astype("Int64")
    result[bin_col] = result["initial_bin_order"].map(
        {order: f"B{order:02d}" for order in labels}
    )
    return result
def build_merge_map(
    ranges: Sequence[Tuple[int, int]],
    initial_bin_count: int,
) -> pd.DataFrame:
    """生成初始箱到最终风险等级的映射。"""
    rows = []
    for final_order, (start, end) in enumerate(ranges, start=1):
        final_label = chr(ord("A") + final_order - 1)
        merged_from = f"B{start:02d}-B{end:02d}" if start != end else f"B{start:02d}"
        for initial_order in range(start, end + 1):
            rows.append(
                {
                    "initial_bin_order": initial_order,
                    INITIAL_BIN_COL: f"B{initial_order:02d}",
                    "final_bin_order": final_order,
                    FINAL_BIN_COL: final_label,
                    "merged_from": merged_from,
                }
            )
    return pd.DataFrame(rows)
def apply_merge_map(data: pd.DataFrame, merge_map: pd.DataFrame) -> pd.DataFrame:
    """将初始箱映射到最终风险等级。"""
    result = data.merge(
        merge_map[[INITIAL_BIN_COL, "final_bin_order", FINAL_BIN_COL]],
        on=INITIAL_BIN_COL,
        how="left",
    )
    result["bin_order"] = result["final_bin_order"].astype("Int64")
    return result
def build_final_edge_table(
    initial_edges: pd.DataFrame,
    merge_map: pd.DataFrame,
    initial_bin_count: int,
) -> pd.DataFrame:
    """生成最终风险等级的上线边界表。"""
    merged = initial_edges.merge(
        merge_map[[INITIAL_BIN_COL, "final_bin_order", FINAL_BIN_COL, "merged_from"]],
        on=INITIAL_BIN_COL,
        how="left",
    ).sort_values("bin_order")

    final_edges = (
        merged.groupby(
            ["final_bin_order", FINAL_BIN_COL, "merged_from"],
            observed=True,
        )
        .agg(
            score_left=("score_left", "first"),
            score_right=("score_right", "last"),
            source_bin_start=("bin_order", "min"),
            source_bin_end=("bin_order", "max"),
        )
        .reset_index()
        .sort_values("final_bin_order")
        .reset_index(drop=True)
    )
    final_edges["extreme_bin_role"] = final_edges.apply(
        lambda row: classify_extreme_role(
            row["source_bin_start"],
            row["source_bin_end"],
            initial_bin_count,
        ),
        axis=1,
    )
    return final_edges
def format_merge_ranges(ranges: Sequence[Tuple[int, int]]) -> str:
    """将合箱范围格式化为便于报告阅读和复用的字符串。"""
    return "[" + ", ".join(f"({start},{end})" for start, end in ranges) + "]"
def parse_merge_ranges(text: str) -> List[Tuple[int, int]]:
    """将候选表中的范围字符串还原为整数区间。"""
    parsed = ast.literal_eval(str(text))
    return [(int(start), int(end)) for start, end in parsed]
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
def oriented_rate(values: pd.Series) -> pd.Series:
    """统一转换为随风险等级应非递减的方向。"""
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric if HIGH_SCORE_HIGH_RISK else -numeric
def count_rate_inversions(
    stats: pd.DataFrame,
    rate_cols: Sequence[str],
    tolerance: float = 0.0,
) -> int:
    """统计风险率相邻显著倒挂次数。"""
    total = 0
    ordered = stats.sort_values("final_bin_order")
    for rate_col in rate_cols:
        diff = oriented_rate(ordered[rate_col]).diff()
        total += int(diff.lt(-tolerance).fillna(False).sum())
    return total
def required_sample_pct(position: int, bin_count: int) -> float:
    """头尾风险箱使用较低样本占比要求，中间箱使用标准要求。"""
    if position in {0, bin_count - 1}:
        return MIN_TAIL_BIN_SAMPLE_PCT
    return MIN_MIDDLE_BIN_SAMPLE_PCT
def _extreme_ranges(initial_bin_count: int) -> Dict[str, Tuple[int, int]]:
    """按模型风险方向返回最好/最坏极端初始箱范围。"""
    best_count = max(0, min(BEST_EXTREME_INITIAL_BIN_COUNT, initial_bin_count))
    worst_count = max(0, min(WORST_EXTREME_INITIAL_BIN_COUNT, initial_bin_count))
    empty = (0, -1)

    if HIGH_SCORE_HIGH_RISK:
        best_range = (1, best_count) if best_count > 0 else empty
        worst_range = (
            initial_bin_count - worst_count + 1,
            initial_bin_count,
        ) if worst_count > 0 else empty
    else:
        best_range = (
            initial_bin_count - best_count + 1,
            initial_bin_count,
        ) if best_count > 0 else empty
        worst_range = (1, worst_count) if worst_count > 0 else empty

    return {
        "best_extreme": best_range,
        "worst_extreme": worst_range,
    }
def identify_extreme_boundaries(initial_bin_count: int) -> Set[int]:
    """
    返回用于圈出最好/最坏极端人群的边界。

    边界编号 k 代表 Bk 与 B(k+1) 之间的切点。
    """
    if not PROTECT_EXTREME_INITIAL_BINS:
        return set()

    max_boundary = initial_bin_count - 1
    boundaries: Set[int] = set()
    for start, end in _extreme_ranges(initial_bin_count).values():
        if start <= 0 or end < start:
            continue
        boundary = end if start == 1 else start - 1
        if 1 <= boundary <= max_boundary:
            boundaries.add(int(boundary))
    return boundaries
def _range_overlaps(left: Tuple[int, int], right: Tuple[int, int]) -> bool:
    """判断两个初始箱范围是否有交集。"""
    return left[0] <= right[1] and right[0] <= left[1]
def classify_extreme_role(
    source_start: int,
    source_end: int,
    initial_bin_count: int,
) -> str:
    """标记最终箱是否为最好/最坏极端箱，或是否已经吞并了极端箱。"""
    if not PROTECT_EXTREME_INITIAL_BINS:
        return ""

    current = (int(source_start), int(source_end))
    roles = []
    for role, expected in _extreme_ranges(initial_bin_count).items():
        if expected[0] <= 0 or expected[1] < expected[0]:
            continue
        if current == expected:
            roles.append(role)
        elif _range_overlaps(current, expected):
            roles.append(f"contains_{role}")

    return ",".join(roles)
def count_crossed_boundaries(
    ranges: Sequence[Tuple[int, int]],
    boundaries: Set[int],
) -> int:
    """统计被当前合箱方案跨越的保护边界数量。"""
    if not boundaries:
        return 0
    preserved = {int(end) for _, end in ranges[:-1]}
    return len(set(boundaries) - preserved)
def bin_constraint_minimums(
    row: pd.Series,
    position: int,
    bin_count: int,
    initial_bin_count: int,
) -> Dict[str, float]:
    """根据普通箱/极端箱返回该最终箱的约束下限。"""
    role = classify_extreme_role(
        row["source_bin_start"],
        row["source_bin_end"],
        initial_bin_count,
    )
    minimums = {
        "sample_pct": required_sample_pct(position, bin_count),
        "mature_count": MIN_FINAL_BIN_MATURE_COUNT,
        "bad_count": MIN_FINAL_BIN_BAD_COUNT,
        "good_count": MIN_FINAL_BIN_GOOD_COUNT,
    }

    if role == "best_extreme":
        minimums.update(
            {
                "mature_count": MIN_EXTREME_BIN_MATURE_COUNT,
                "bad_count": MIN_BEST_EXTREME_BIN_BAD_COUNT,
                "good_count": MIN_BEST_EXTREME_BIN_GOOD_COUNT,
            }
        )
    elif role == "worst_extreme":
        minimums.update(
            {
                "mature_count": MIN_EXTREME_BIN_MATURE_COUNT,
                "bad_count": MIN_WORST_EXTREME_BIN_BAD_COUNT,
                "good_count": MIN_WORST_EXTREME_BIN_GOOD_COUNT,
            }
        )

    return minimums
def filter_blocked_pair_indices(
    ranges: Sequence[Tuple[int, int]],
    pair_indices: Sequence[int],
    blocked_boundaries: Optional[Set[int]],
) -> List[int]:
    """剔除会跨越硬保护边界的相邻合箱位置。"""
    if not blocked_boundaries:
        return list(pair_indices)
    return [
        pair_index
        for pair_index in pair_indices
        if int(ranges[pair_index][1]) not in blocked_boundaries
    ]
def calc_bin_constraint_details(stats: pd.DataFrame) -> pd.DataFrame:
    """计算每个最终箱的样本、成熟量和好坏样本约束。"""
    rows = []
    ordered = stats.sort_values("final_bin_order").reset_index(drop=True)
    initial_bin_count = (
        int(ordered["source_bin_end"].max())
        if "source_bin_end" in ordered.columns and not ordered.empty
        else len(ordered)
    )
    for position, row in ordered.iterrows():
        minimums = bin_constraint_minimums(
            row,
            position,
            len(ordered),
            initial_bin_count,
        )
        min_sample_pct = minimums["sample_pct"]
        min_mature_count = minimums["mature_count"]
        min_bad_count = minimums["bad_count"]
        min_good_count = minimums["good_count"]
        checks = {
            "sample_ok": row["sample_pct"] >= min_sample_pct,
            "mature_ok": row[PRIMARY_MATURE_COL] >= min_mature_count,
            "bad_ok": row[PRIMARY_BAD_COL] >= min_bad_count,
            "good_ok": row[PRIMARY_GOOD_COL] >= min_good_count,
        }

        def shortage(value: float, minimum: float) -> float:
            if minimum <= 0:
                return 0.0
            return max(0.0, 1 - safe_div(value, minimum))

        severity = 0.0
        severity += shortage(row["sample_pct"], min_sample_pct)
        severity += shortage(row[PRIMARY_MATURE_COL], min_mature_count)
        severity += shortage(row[PRIMARY_BAD_COL], min_bad_count)
        severity += shortage(row[PRIMARY_GOOD_COL], min_good_count)

        rows.append(
            {
                "final_bin_order": int(row["final_bin_order"]),
                FINAL_BIN_COL: row[FINAL_BIN_COL],
                "required_sample_pct": min_sample_pct,
                "required_mature_count": min_mature_count,
                "required_bad_count": min_bad_count,
                "required_good_count": min_good_count,
                **checks,
                "all_constraints_ok": all(checks.values()),
                "violation_severity": severity,
            }
        )
    return pd.DataFrame(rows)
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

        cum_limit = constraints.get("max_cum_3m30p_cnt_bad_rate")
        if cum_limit is not None:
            eligible = ordered.loc[ordered["cum_3m30p_cnt_bad_rate"].le(cum_limit)]
            if not eligible.empty:
                boundary = int(eligible["bin_order"].max())
                if 1 <= boundary <= max_boundary:
                    boundaries.add(boundary)

        marginal_limit = constraints.get("max_marginal_3m30p_cnt_bad_rate")
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
def merge_ranges_at(
    ranges: Sequence[Tuple[int, int]],
    pair_index: int,
) -> List[Tuple[int, int]]:
    """合并 ranges[pair_index] 与其右侧相邻范围。"""
    if pair_index < 0 or pair_index >= len(ranges) - 1:
        raise IndexError(f"无效相邻合箱位置: {pair_index}")
    result = list(ranges)
    left = result[pair_index]
    right = result[pair_index + 1]
    result[pair_index:pair_index + 2] = [(left[0], right[1])]
    return result
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
def primary_inversion_pair_indices(stats: pd.DataFrame) -> List[int]:
    """返回任一主风险指标发生倒挂的相邻箱左侧位置（去重）。"""
    ordered = stats.sort_values("final_bin_order").reset_index(drop=True)
    violation_rows: Set[int] = set()
    for rate_col in PRIMARY_RATE_COLS:
        diff = oriented_rate(ordered[rate_col]).diff()
        rows = ordered.index[diff.lt(-TRAIN_INVERSION_TOLERANCE).fillna(False)]
        violation_rows.update(int(r - 1) for r in rows if r > 0)
    return sorted(violation_rows)
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
            "主指标 1M30+/3M30+ 出现相邻倒挂",
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
def selected_ranges_from_candidate_table(
    candidates: pd.DataFrame,
) -> List[Tuple[int, int]]:
    """从候选评分表读取最终方案。"""
    selected = candidates.loc[candidates["selected"].eq(True)]
    if selected.empty:
        raise ValueError("未生成任何可用的合箱候选方案")
    return parse_merge_ranges(str(selected.iloc[0]["ranges"]))
def resolve_merge_ranges(
    candidates: pd.DataFrame,
    initial_bin_count: int,
    train_initial_stats: pd.DataFrame,
) -> List[Tuple[int, int]]:
    """选定最终合箱方案：settings.FINAL_BIN_RANGES 指定时校验后直接采用，否则走自动评分表。

    手动方案（模型配置 final_bin_ranges）硬校验：连续覆盖 1..initial_bin_count、档数在
    [MIN_FINAL_BIN_COUNT, MAX_FINAL_BIN_COUNT]、Train 主指标（PRIMARY_RATE_COLS）无倒挂；
    箱级约束与极端边界跨越数仅写入日志供评审（与自动路径的评分口径一致，不阻断）。
    """
    if not FINAL_BIN_RANGES:
        return selected_ranges_from_candidate_table(candidates)

    ranges = (
        parse_merge_ranges(FINAL_BIN_RANGES)
        if isinstance(FINAL_BIN_RANGES, str)
        else [tuple(r) for r in FINAL_BIN_RANGES]
    )
    covered = [i for lo, hi in ranges for i in range(lo, hi + 1)]
    if covered != list(range(1, initial_bin_count + 1)):
        raise ValueError(
            f"手动合箱方案未连续覆盖 1..{initial_bin_count}：{format_merge_ranges(ranges)}"
        )
    if not (MIN_FINAL_BIN_COUNT <= len(ranges) <= MAX_FINAL_BIN_COUNT):
        raise ValueError(
            f"手动合箱方案档数 {len(ranges)} 超出 {MIN_FINAL_BIN_COUNT}~{MAX_FINAL_BIN_COUNT}"
        )

    merged_stats = aggregate_initial_stats_by_ranges(train_initial_stats, ranges)
    inversions = count_rate_inversions(merged_stats, PRIMARY_RATE_COLS, TRAIN_INVERSION_TOLERANCE)
    if inversions:
        raise ValueError(f"手动合箱方案 Train 主指标倒挂 {inversions} 处，请调整方案")

    details = calc_bin_constraint_details(merged_stats)
    violation_count = int((~details["all_constraints_ok"]).sum())
    crossed = count_crossed_boundaries(
        ranges,
        identify_extreme_boundaries(initial_bin_count),
    )
    print(
        f"手动合箱方案校验：覆盖 1..{initial_bin_count}、{len(ranges)} 档、"
        f"Train 主指标倒挂 0 处、极端边界跨越 {crossed} 处、箱级约束违规 {violation_count} 项"
    )
    return ranges
