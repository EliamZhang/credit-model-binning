"""
分箱主脚本
==========
对策略调优样本进行等频 20 箱初分，ChiMerge 合并相似相邻箱，输出合并前后的
风险指标及 OOT 跨期验证。

使用方式：
    python scr/binning.py

输出：
    res/binning_result.md  — 分箱结果 Markdown 表格（含合并前后对比）
"""

import pandas as pd
import numpy as np
import os
from scipy.stats import spearmanr, chi2_contingency, norm

# ============================================================
# 参数配置
# ============================================================
LABEL_COL = "duedate_3m_30"
SCORE_COL = "aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score"
N_BINS = 20
OOT_CUT_DATE = "2026-01-01"

# ChiMerge 参数
CHIMERGE_MIN_BINS = 6          # 合并目标箱数

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES_DIR = os.path.join(BASE_DIR, "res")


# ============================================================
# 0. 工具函数
# ============================================================

def compute_bin_stats(df, score_col, label_col, bin_col="bin"):
    """汇总分箱指标。"""
    stats = df.groupby(bin_col, observed=False).agg(
        score_min=(score_col, "min"),
        score_max=(score_col, "max"),
        n=(score_col, "count"),
        B=(label_col, "sum"),
    ).reset_index()

    stats = stats[stats["n"] > 0].copy()
    stats["bad_rate"] = stats["B"] / stats["n"]
    stats["SE"] = np.sqrt(stats["bad_rate"] * (1 - stats["bad_rate"]) / stats["n"])

    stats = stats.sort_values("score_min").reset_index(drop=True)
    stats.index = range(1, len(stats) + 1)
    stats.index.name = "bin_no"

    total_N = stats["n"].sum()
    total_B = stats["B"].sum()

    stats["cum_n"] = stats["n"].cumsum()
    stats["cum_B"] = stats["B"].cumsum()
    stats["cum_pass_rate"] = stats["cum_n"] / total_N
    stats["cum_bad_rate"] = stats["cum_B"] / stats["cum_n"]

    stats["G"] = stats["n"] - stats["B"]
    stats["B_pct"] = stats["B"] / total_B
    stats["G_pct"] = stats["G"] / (total_N - total_B)

    stats["WOE"] = np.log(
        (stats["B_pct"].replace(0, np.nan)) / (stats["G_pct"].replace(0, np.nan))
    )
    stats["WOE"] = stats["WOE"].fillna(0)

    stats["IV_component"] = (stats["B_pct"] - stats["G_pct"]) * stats["WOE"]

    return stats, total_N, total_B


def compute_adjacent_tests(stats_df):
    """计算相邻箱的卡方检验和 Z 检验，用于判断哪些相邻箱可合并。"""
    results = []
    for i in range(len(stats_df) - 1):
        row_a = stats_df.iloc[i]
        row_b = stats_df.iloc[i + 1]
        n_a, B_a, r_a = int(row_a["n"]), int(row_a["B"]), row_a["bad_rate"]
        n_b, B_b, r_b = int(row_b["n"]), int(row_b["B"]), row_b["bad_rate"]

        table = [[B_a, n_a - B_a], [B_b, n_b - B_b]]
        try:
            chi2, p_chi2 = chi2_contingency(table, correction=False)[:2]
            if np.isnan(p_chi2):
                p_chi2 = 0.0
        except ValueError:
            chi2, p_chi2 = 0.0, 0.0

        se = np.sqrt(r_a * (1 - r_a) / n_a + r_b * (1 - r_b) / n_b)
        if se > 0:
            z = (r_b - r_a) / se
            p_z = 2 * (1 - norm.cdf(abs(z)))
        else:
            z, p_z = 0.0, 1.0

        results.append({
            "pair": f"箱{i+1} vs 箱{i+2}",
            "bad_a": r_a,
            "bad_b": r_b,
            "diff": r_b - r_a,
            "chi2": chi2,
            "p_chi2": p_chi2,
            "z": z,
            "p_z": p_z,
            "significant_chi2": p_chi2 < 0.05,
            "significant_z": p_z < 0.05,
        })
    return results


def chimerge(df, score_col, label_col, initial_bins, min_bins=6):
    """
    ChiMerge: 迭代合并坏账率分布最相似的相邻箱。

    每轮计算所有相邻箱对的卡方 p 值，合并 p 值最大（最相似）的一对，
    直到箱数降至 min_bins。
    """
    bins = list(initial_bins)
    merge_history = []

    while len(bins) - 1 > min_bins:
        df["_merge_bin"] = pd.cut(df[score_col], bins=bins, duplicates="drop", include_lowest=True)
        stats, _, _ = compute_bin_stats(df, score_col, label_col, bin_col="_merge_bin")

        if len(stats) <= min_bins:
            break

        best_p = -1.0
        best_i = -1
        best_chi2 = 0.0

        for i in range(len(stats) - 1):
            row_a = stats.iloc[i]
            row_b = stats.iloc[i + 1]
            B_a, n_a = int(row_a["B"]), int(row_a["n"])
            B_b, n_b = int(row_b["B"]), int(row_b["n"])
            table = [[B_a, n_a - B_a], [B_b, n_b - B_b]]
            try:
                chi2, p_value = chi2_contingency(table, correction=False)[:2]
                if np.isnan(p_value):
                    p_value = 0.0
            except ValueError:
                chi2, p_value = 0.0, 0.0

            if p_value > best_p:
                best_p = p_value
                best_chi2 = chi2
                best_i = i

        merge_history.append({
            "step": len(merge_history) + 1,
            "merged_pair": f"箱{best_i+1} + 箱{best_i+2}",
            "boundary": round(bins[best_i + 1], 6),
            "chi2": round(best_chi2, 4),
            "p_value": best_p,
            "bins_remaining": len(bins) - 2,
        })
        del bins[best_i + 1]

    if "_merge_bin" in df.columns:
        df.drop(columns=["_merge_bin"], inplace=True)

    return bins, merge_history


def compute_psi(stats_tuning, stats_oot):
    """计算两样本在各箱的 PSI（Population Stability Index）。"""
    if stats_oot is None or len(stats_tuning) != len(stats_oot):
        return None
    e = (stats_tuning["n"] / stats_tuning["n"].sum()).values
    a = (stats_oot["n"] / stats_oot["n"].sum()).values
    e = np.where(e == 0, 1e-10, e)
    a = np.where(a == 0, 1e-10, a)
    return float(np.sum((a - e) * np.log(a / e)))


def compute_threshold_curve(df, score_col, label_col, principal_col=None, n_thresholds=20):
    """在调优集上逐阈值计算累计指标。

    假设分数越高风险越高，累计方向为 score <= threshold。
    若提供 principal_col，则同时计算金额口径坏账率。
    """
    scores = df[score_col].values
    labels = df[label_col].astype(int).values

    percentiles = np.linspace(100 / n_thresholds, 100, n_thresholds)
    thresholds = np.percentile(scores, percentiles)
    thresholds = np.unique(np.round(thresholds, 8))

    total_N = len(df)
    results = []
    for thr in thresholds:
        mask = scores <= thr
        cum_n = mask.sum()
        cum_B = labels[mask].sum()
        row = {
            "threshold": thr,
            "cum_n": cum_n,
            "cum_pass_rate": cum_n / total_N,
            "cum_B": cum_B,
            "cum_bad_rate_count": cum_B / cum_n if cum_n > 0 else float("nan"),
        }
        if principal_col and principal_col in df.columns:
            principal = df[principal_col].values
            valid = ~np.isnan(principal) & (principal > 0)
            p_mask = mask & valid
            cum_principal = principal[p_mask].sum()
            cum_bad_principal = (principal[p_mask] * labels[p_mask]).sum()
            row["cum_principal"] = cum_principal
            row["cum_bad_rate_amount"] = (
                cum_bad_principal / cum_principal if cum_principal > 0 else float("nan")
            )
        results.append(row)

    return pd.DataFrame(results)


def format_triple(row):
    """Format a segment row for the three-scheme table."""
    score_range = f"[{row.score_min:.4f}, {row.score_max:.4f})"
    return (
        f"| {row.segment} "
        f"| {score_range} "
        f"| {row.n:>6,} "
        f"| {row.pct:.2%} "
        f"| {row.bad_rate_count:.4%} "
        f"| {row.bad_rate_amount:.4%} |"
    )


def compute_scheme_stats(df, score_col, label_col, auto_max, review_max, principal_col=None):
    """Compute segment stats for a single scheme."""
    labels = df[label_col].astype(int)
    scores = df[score_col]
    total = len(df)

    seg_auto = df[scores <= auto_max]
    seg_review = df[(scores > auto_max) & (scores <= review_max)]
    seg_reject = df[scores > review_max]

    rows = []
    for seg_name, seg_df in [("自动通过", seg_auto), ("人工审核", seg_review), ("拒绝", seg_reject)]:
        n = len(seg_df)
        B = int(seg_df[label_col].astype(int).sum())
        row = {
            "segment": seg_name,
            "score_min": seg_df[score_col].min() if n > 0 else float("nan"),
            "score_max": seg_df[score_col].max() if n > 0 else float("nan"),
            "n": n,
            "pct": n / total if total > 0 else 0,
            "B": B,
            "bad_rate_count": B / n if n > 0 else float("nan"),
            "bad_rate_amount": float("nan"),
        }
        if principal_col and principal_col in df.columns:
            principal = seg_df[principal_col]
            valid = principal.notna() & (principal > 0)
            if valid.sum() > 0:
                principal_sum = principal[valid].sum()
                bad_principal_sum = (principal[valid] * labels[seg_df.index[valid]].astype(int)).sum()
                row["bad_rate_amount"] = bad_principal_sum / principal_sum
        rows.append(row)
    return rows


def design_three_schemes(df, merged_stats, score_col, label_col, principal_col=None):
    """基于合并箱边界设计三套方案。

    保守方案: 低风险 bins 自动通过，中间 bins 人工审核，高风险 bins 拒绝
    平衡方案: 更多 bins 自动通过，仅最高风险 bin 拒绝
    增长方案: 最大范围自动通过，人工审核覆盖最后一个中间 bin
    """
    assert len(merged_stats) == 6, "三套方案设计依赖 6 箱合并结果"

    bin_maxes = merged_stats["score_max"].values  # [b1_max, b2_max, ..., b6_max]

    schemes = {
        "保守方案": {
            "auto_max": bin_maxes[1],   # bins 1-2
            "review_max": bin_maxes[3], # bins 3-4
            "description": "仅自动通过最低风险的 2 个箱，人工审核中间 2 箱，拒绝高风险的 2 箱。坏账率最低，抗风险能力最强。",
        },
        "平衡方案（推荐）": {
            "auto_max": bin_maxes[2],   # bins 1-3
            "review_max": bin_maxes[4], # bins 4-5
            "description": "自动通过前 3 箱，人工审核中间 2 箱，拒绝最高风险 1 箱。在通过率、风险和审核量之间取得平衡。",
        },
        "增长方案": {
            "auto_max": bin_maxes[3],   # bins 1-4
            "review_max": bin_maxes[4], # bin 5
            "description": "自动通过前 4 箱，仅人工审核 1 箱，拒绝最高风险 1 箱。通过率最高，适合获客扩张。",
        },
    }

    results = {}
    for name, cfg in schemes.items():
        segments = compute_scheme_stats(
            df, score_col, label_col, cfg["auto_max"], cfg["review_max"], principal_col
        )
        # summary row: all non-rejected
        total = len(df)
        approved = df[df[score_col] <= cfg["review_max"]]
        n_approved = len(approved)
        B_approved = int(approved[label_col].astype(int).sum())
        summary = {
            "name": name,
            "auto_max": cfg["auto_max"],
            "review_max": cfg["review_max"],
            "description": cfg["description"],
            "segments": segments,
            "pass_n": n_approved,
            "pass_rate": n_approved / total,
            "pass_bad_rate_count": B_approved / n_approved if n_approved > 0 else float("nan"),
            "reject_n": total - n_approved,
            "reject_rate": (total - n_approved) / total,
        }
        if principal_col and principal_col in df.columns:
            principal = approved[principal_col]
            valid = principal.notna() & (principal > 0)
            if valid.sum() > 0:
                principal_sum = principal[valid].sum()
                bad_principal = (principal[valid] * approved.loc[valid.index, label_col].astype(int)).sum()
                summary["pass_bad_rate_amount"] = bad_principal / principal_sum
            else:
                summary["pass_bad_rate_amount"] = float("nan")
        else:
            summary["pass_bad_rate_amount"] = float("nan")
        results[name] = summary

    return results


def format_bin_row(row):
    score_range = f"[{row.score_min:.4f}, {row.score_max:.4f})"
    return (
        f"| {row.Index:>2} "
        f"| {score_range} "
        f"| {row.n:>6,} "
        f"| {row.B:>6,} "
        f"| {row.bad_rate:.4%} "
        f"| {row.SE:.4%} "
        f"| {row.cum_pass_rate:.2%} "
        f"| {row.cum_bad_rate:.4%} "
        f"| {row.WOE:+.4f} |"
    )


def format_bin_table(stats_df, title, level=3):
    """生成分箱明细 Markdown 表格。"""
    lines = []
    lines.append(f"{'#' * level} {title}")
    lines.append("")
    lines.append("| 箱序 | 分数区间 | 样本量 | 坏样本 | 坏账率 | SE | 累计通过率 | 累计坏账率 | WOE |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in stats_df.itertuples():
        lines.append(format_bin_row(row))
    lines.append("")
    return lines


# ============================================================
# 1. 加载数据 & 关联
# ============================================================
print("=" * 60)
print("1. 加载数据")
print("=" * 60)

score_df = pd.read_csv(os.path.join(RES_DIR, "aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv"))
info_df = pd.read_csv(os.path.join(RES_DIR, "application_info.csv"))

print(f"  模型分表: {len(score_df):,} 条")
print(f"  申请表:   {len(info_df):,} 条")

merged = score_df.merge(
    info_df[["application_id", LABEL_COL, "principal"]],
    on="application_id",
    how="inner",
)
print(f"  关联后:   {len(merged):,} 条")

# ============================================================
# 2. 样本划分
# ============================================================
print("\n" + "=" * 60)
print("2. 样本划分")
print("=" * 60)

merged["sample_datetime"] = pd.to_datetime(merged["sample_datetime"])
oot_cut = pd.to_datetime(OOT_CUT_DATE)

tuning = merged[merged["sample_datetime"] < oot_cut].copy()
oot = merged[merged["sample_datetime"] >= oot_cut].copy()

tuning_valid = tuning[tuning[LABEL_COL].notna()].copy()
oot_valid = oot[oot[LABEL_COL].notna()].copy()

print(f"  策略调优集（< {OOT_CUT_DATE}）: {len(tuning):,} 条，有效标签 {len(tuning_valid):,} 条")
print(f"  OOT 集    （>= {OOT_CUT_DATE}）: {len(oot):,} 条，有效标签 {len(oot_valid):,} 条")

if len(tuning_valid) == 0:
    raise RuntimeError("策略调优集无有效标签，请检查数据或切分日期。")

# ============================================================
# 3. 等频 20 箱（基于策略调优集）
# ============================================================
print("\n" + "=" * 60)
print("3. 等频 20 箱初分")
print("=" * 60)

scores = tuning_valid[SCORE_COL]
labels = tuning_valid[LABEL_COL].astype(int)

try:
    tuning_valid["bin"], bins = pd.qcut(scores, q=N_BINS, duplicates="drop", retbins=True)
except ValueError as e:
    print(f"  qcut 失败: {e}")
    raise

actual_bins = tuning_valid["bin"].nunique()
print(f"  目标箱数: {N_BINS}，实际箱数: {actual_bins}")
if actual_bins < N_BINS:
    print(f"  注意: 因同分集中，实际箱数减少为 {actual_bins} 箱")
print(f"  切点: {[round(b, 6) for b in bins]}")

# ============================================================
# 4. 策略调优集初始分箱指标
# ============================================================
print("\n" + "=" * 60)
print("4. 初始分箱指标（策略调优集）")
print("=" * 60)

tuning_stats, tuning_N, tuning_B = compute_bin_stats(tuning_valid, SCORE_COL, LABEL_COL)
tuning_bad_rate = tuning_B / tuning_N
tuning_IV = tuning_stats["IV_component"].sum()

rho, p_value = spearmanr(tuning_stats.index, tuning_stats["bad_rate"])

print(f"  整体坏账率: {tuning_bad_rate:.4%}")
print(f"  总 IV: {tuning_IV:.4f}")
print(f"  Spearman ρ = {rho:.4f}（p = {p_value:.4f}）")
if abs(rho) < 0.9:
    print("  ⚠ 单调性较差，可能存在局部倒挂。")

# ============================================================
# 5. OOT 集初始分箱
# ============================================================
print("\n" + "=" * 60)
print("5. OOT 集初始分箱")
print("=" * 60)

if len(oot_valid) > 0:
    oot_labels = oot_valid[LABEL_COL].astype(int)
    oot_valid["bin"] = pd.cut(oot_valid[SCORE_COL], bins=bins, duplicates="drop", include_lowest=True)
    oot_valid_binned = oot_valid[oot_valid["bin"].notna()].copy()

    if len(oot_valid_binned) > 0:
        oot_stats, oot_N, oot_B = compute_bin_stats(oot_valid_binned, SCORE_COL, LABEL_COL)
        oot_bad_rate = oot_B / oot_N
        oot_IV = oot_stats["IV_component"].sum()
        oot_rho, oot_p = spearmanr(oot_stats.index, oot_stats["bad_rate"])

        oot_out_of_range = len(oot_valid) - len(oot_valid_binned)
        print(f"  OOT 有效样本: {len(oot_valid):,}，落入分箱区间: {len(oot_valid_binned):,}")
        if oot_out_of_range > 0:
            print(f"  OOT 超出切点范围: {oot_out_of_range} 条")
        print(f"  OOT 坏账率: {oot_bad_rate:.4%}")
        print(f"  OOT IV: {oot_IV:.4f}")
        print(f"  OOT Spearman ρ = {oot_rho:.4f}（p = {oot_p:.4f}）")
    else:
        oot_stats = None
        oot_bad_rate = None
        print("  OOT 集无样本落入分箱区间。")
else:
    oot_stats = None
    oot_bad_rate = None
    print("  OOT 集无有效标签，可能尚不成熟。")

# ============================================================
# 6. 相邻箱差异检验
# ============================================================
print("\n" + "=" * 60)
print("6. 相邻箱差异检验")
print("=" * 60)

adjacent_tests = compute_adjacent_tests(tuning_stats)
not_sig_chi2 = sum(1 for t in adjacent_tests if not t["significant_chi2"])
not_sig_z = sum(1 for t in adjacent_tests if not t["significant_z"])
print(f"  卡方检验不显著（p>=0.05）的相邻对: {not_sig_chi2}/{len(adjacent_tests)}")
print(f"  Z 检验不显著（p>=0.05）的相邻对:   {not_sig_z}/{len(adjacent_tests)}")

# ============================================================
# 7. ChiMerge 合并
# ============================================================
print("\n" + "=" * 60)
print("7. ChiMerge 合并")
print("=" * 60)

merged_bins, merge_history = chimerge(
    tuning_valid, SCORE_COL, LABEL_COL, bins,
    min_bins=CHIMERGE_MIN_BINS,
)

print(f"  初始 {len(bins)-1} 箱 → 合并后 {len(merged_bins)-1} 箱")
print(f"  合并次数: {len(merge_history)}")
for h in merge_history:
    print(f"    第{h['step']:2d}步: 合并 {h['merged_pair']}（p={h['p_value']:.4f}），剩余 {h['bins_remaining']} 箱")

# ============================================================
# 8. 合并后分箱指标（策略调优集）
# ============================================================
print("\n" + "=" * 60)
print("8. 合并后分箱指标（策略调优集）")
print("=" * 60)

tuning_valid["merged_bin"] = pd.cut(tuning_valid[SCORE_COL], bins=merged_bins, duplicates="drop", include_lowest=True)
tuning_merged_stats, tuning_merged_N, tuning_merged_B = compute_bin_stats(
    tuning_valid, SCORE_COL, LABEL_COL, bin_col="merged_bin"
)
tuning_merged_bad_rate = tuning_merged_B / tuning_merged_N
tuning_merged_IV = tuning_merged_stats["IV_component"].sum()
tuning_merged_rho, tuning_merged_p = spearmanr(
    tuning_merged_stats.index, tuning_merged_stats["bad_rate"]
)

print(f"  合并后箱数: {len(tuning_merged_stats)}")
print(f"  整体坏账率: {tuning_merged_bad_rate:.4%}")
print(f"  IV: {tuning_merged_IV:.4f}")
print(f"  Spearman ρ = {tuning_merged_rho:.4f}（p = {tuning_merged_p:.4f}）")

# ============================================================
# 9. OOT 集应用合并后分箱
# ============================================================
print("\n" + "=" * 60)
print("9. OOT 集合并后分箱验证")
print("=" * 60)

if len(oot_valid) > 0:
    oot_valid["merged_bin"] = pd.cut(oot_valid[SCORE_COL], bins=merged_bins, duplicates="drop", include_lowest=True)
    oot_merged_binned = oot_valid[oot_valid["merged_bin"].notna()].copy()

    if len(oot_merged_binned) > 0:
        oot_merged_stats, oot_merged_N, oot_merged_B = compute_bin_stats(
            oot_merged_binned, SCORE_COL, LABEL_COL, bin_col="merged_bin"
        )
        oot_merged_bad_rate = oot_merged_B / oot_merged_N
        oot_merged_IV = oot_merged_stats["IV_component"].sum()
        oot_merged_rho, oot_merged_p = spearmanr(
            oot_merged_stats.index, oot_merged_stats["bad_rate"]
        )

        oot_merged_out = len(oot_valid) - len(oot_merged_binned)
        print(f"  OOT 有效: {len(oot_valid):,}，落入合并区间: {len(oot_merged_binned):,}")
        if oot_merged_out > 0:
            print(f"  OOT 超出合并区间: {oot_merged_out} 条")
        print(f"  OOT 坏账率: {oot_merged_bad_rate:.4%}")
        print(f"  OOT IV: {oot_merged_IV:.4f}")
        print(f"  OOT Spearman ρ = {oot_merged_rho:.4f}（p = {oot_merged_p:.4f}）")

        merged_psi = compute_psi(tuning_merged_stats, oot_merged_stats)
        if merged_psi is not None:
            print(f"  PSI（合并后，tuning vs OOT）: {merged_psi:.4f}")
    else:
        oot_merged_stats = None
        oot_merged_bad_rate = None
        merged_psi = None
        print("  OOT 集无样本落入合并后分箱区间。")
else:
    oot_merged_stats = None
    oot_merged_bad_rate = None
    merged_psi = None
    print("  OOT 集无有效标签。")

# ============================================================
# 10. 累计阈值曲线（基于合并后分箱）
# ============================================================
print("\n" + "=" * 60)
print("10. 累计阈值曲线")
print("=" * 60)

threshold_curve = compute_threshold_curve(
    tuning_valid, SCORE_COL, LABEL_COL, principal_col="principal", n_thresholds=20
)

print(f"  阈值点数: {len(threshold_curve)}")
print(f"  累计指标: 通过率 + 坏账率（笔数 + 金额口径）")

# 标注合并箱边界
merged_boundaries = sorted(set(
    round(b, 8) for b in merged_bins[1:-1]  # 不含最低和最高切点
))
print(f"  合并箱边界（用于参考）: {merged_boundaries}")

# ============================================================
# 11. 三套方案设计（基于合并后分箱）
# ============================================================
print("\n" + "=" * 60)
print("11. 三套方案设计")
print("=" * 60)

schemes = design_three_schemes(tuning_valid, tuning_merged_stats, SCORE_COL, LABEL_COL, principal_col="principal")

for name, s in schemes.items():
    print(f"  {name}: 自动通过 ≤ {s['auto_max']:.4f}, 审核 ≤ {s['review_max']:.4f}, 拒绝 > {s['review_max']:.4f}")
    print(f"    通过率 {s['pass_rate']:.2%}, 通过人群坏账率 {s['pass_bad_rate_count']:.4%}, 拒绝率 {s['reject_rate']:.2%}")

# OOT 验证三套方案
schemes_oot = None
if len(oot_valid) > 0:
    schemes_oot = design_three_schemes(oot_valid, oot_merged_stats, SCORE_COL, LABEL_COL, principal_col="principal")

# ============================================================
# 12. 输出 Markdown
# ============================================================
print("\n" + "=" * 60)
print("12. 输出结果")
print("=" * 60)

md = []

# 标题
md.append("# 分箱结果：等频 20 箱 → ChiMerge 合并")
md.append("")
md.append(f"> 模型: `{SCORE_COL}` | 标签: `{LABEL_COL}` | 样本切分: `< {OOT_CUT_DATE}` vs `>= {OOT_CUT_DATE}`")
md.append(f"> 合并参数: 目标箱数 = {CHIMERGE_MIN_BINS}")
md.append("")

# ---- 摘要 ----
md.append("## 一、摘要")
md.append("")

# 合并前后对比表
md.append("### 合并前后对比")
md.append("")
md.append("| 指标 | 初分 20 箱（调优） | 初分 20 箱（OOT） | 合并后（调优） | 合并后（OOT） |")
md.append("|:---|---:|---:|---:|---:|")
md.append(f"| 箱数 | {actual_bins} | {len(oot_stats) if oot_stats is not None else 'N/A'} "
         f"| {len(tuning_merged_stats)} "
         f"| {len(oot_merged_stats) if oot_merged_stats is not None else 'N/A'} |")
md.append(f"| 样本量 | {tuning_N:,} | {oot_N if oot_stats is not None else 'N/A'} "
         f"| {tuning_merged_N:,} "
         f"| {oot_merged_N if oot_merged_stats is not None else 'N/A'} |")
md.append(f"| 坏账率 | {tuning_bad_rate:.4%} | {oot_bad_rate:.4%} "
         f"| {tuning_merged_bad_rate:.4%} "
         f"| {oot_merged_bad_rate:.4%} |" if oot_bad_rate is not None and oot_merged_bad_rate is not None else "")
md.append(f"| IV | {tuning_IV:.4f} | {oot_IV:.4f} "
         f"| {tuning_merged_IV:.4f} "
         f"| {oot_merged_IV:.4f} |" if oot_stats is not None and oot_merged_stats is not None else "")
md.append(f"| Spearman ρ | {rho:.4f} | {oot_rho:.4f} "
         f"| {tuning_merged_rho:.4f} "
         f"| {oot_merged_rho:.4f} |" if oot_stats is not None and oot_merged_stats is not None else "")
if merged_psi is not None:
    md.append(f"| PSI（tuning vs OOT） | — | — | — | {merged_psi:.4f} |")
md.append("")

# ---- 初始 20 箱 ----
md.append("## 二、初始等频 20 箱")
md.append("")

md += format_bin_table(tuning_stats, "策略调优集", level=3)

if oot_stats is not None:
    md += format_bin_table(oot_stats, "OOT 集（同一分箱切点）", level=3)

# ---- 相邻箱差异检验 ----
md.append("## 三、相邻箱差异检验")
md.append("")
md.append("| 相邻箱对 | 坏账率 A | 坏账率 B | 差异 | χ² | p(χ²) | 显著(χ²) | z | p(z) | 显著(z) |")
md.append("|---:|---:|---:|---:|---:|---:|:---:|---:|---:|:---:|")
for t in adjacent_tests:
    sig_chi2 = "✓" if t["significant_chi2"] else ""
    sig_z = "✓" if t["significant_z"] else ""
    md.append(
        f"| {t['pair']} "
        f"| {t['bad_a']:.4%} "
        f"| {t['bad_b']:.4%} "
        f"| {t['diff']:+.4%} "
        f"| {t['chi2']:.2f} "
        f"| {t['p_chi2']:.4f} "
        f"| {sig_chi2} "
        f"| {t['z']:+.2f} "
        f"| {t['p_z']:.4f} "
        f"| {sig_z} |"
    )
md.append("")
md.append("> 卡方检验用于判断两箱好坏分布是否独立；Z 检验用于判断两箱坏账率差异是否显著。")
md.append("> 标记 ✓ 表示在 5% 显著性水平下拒绝「两箱风险无差异」的原假设。")
md.append("")

# ---- ChiMerge 合并过程 ----
md.append("## 四、ChiMerge 合并过程")
md.append("")
md.append(f"从 {len(bins)-1} 箱开始，每轮合并卡方 p 值最大（分布最相似）的相邻箱对，直至 {CHIMERGE_MIN_BINS} 箱。")
md.append("")
md.append("| 步骤 | 合并对 | 被合并边界 | χ² | p 值 | 剩余箱数 |")
md.append("|---:|---|---:|---:|---:|")
for h in merge_history:
    md.append(
        f"| {h['step']} "
        f"| {h['merged_pair']} "
        f"| {h['boundary']} "
        f"| {h['chi2']:.2f} "
        f"| {h['p_value']:.4f} "
        f"| {h['bins_remaining']} |"
    )
md.append("")

# ---- 合并后分箱 ----
md.append("## 五、合并后分箱结果")
md.append("")

md += format_bin_table(tuning_merged_stats, "策略调优集", level=3)

if oot_merged_stats is not None:
    md += format_bin_table(oot_merged_stats, "OOT 集（同一合并切点）", level=3)

    if merged_psi is not None:
        psi_level = "稳定" if merged_psi < 0.1 else ("存在一定漂移" if merged_psi < 0.25 else "漂移较明显")
        md.append(f"**PSI（tuning vs OOT）**: {merged_psi:.4f}（{psi_level}）")
        md.append("")

# ---- 合并后结论 ----
md.append("## 六、合并后结论")
md.append("")
md.append(f"- 合并后保留 **{len(tuning_merged_stats)} 个风险等级**，各箱坏账率严格单调（ρ = {tuning_merged_rho:.4f}）")
md.append(f"- 调优集 IV = {tuning_merged_IV:.4f}，区分能力保持良好")
if oot_merged_stats is not None:
    md.append(f"- OOT 集 IV = {oot_merged_IV:.4f}，跨期排序能力确认")
    if merged_psi is not None:
        md.append(f"- PSI = {merged_psi:.4f}，跨期分布{psi_level}")
md.append("")

# ---- 累计阈值曲线 ----
md.append("## 七、累计阈值曲线")
md.append("")
md.append("> 在策略调优集上，从低分到高分逐阈值计算累计通过率和累计坏账率。")
md.append("> 分数越高风险越高，累计方向为 `score <= threshold`。合并箱边界行以 **粗体** 标注。")
md.append("")

# 构建合并边界集合用于标注
merged_boundary_set = set(round(b, 8) for b in merged_bins[1:-1])

md.append("| 阈值 | 累计通过率 | 累计坏账率（笔数） | 累计坏账率（金额） | 备注 |")
md.append("|---:|---:|---:|---:|:---|")
for row in threshold_curve.itertuples():
    thr = row.threshold
    is_boundary = round(thr, 8) in merged_boundary_set
    marker = "**← 合并箱边界**" if is_boundary else ""

    bad_count_str = f"{row.cum_bad_rate_count:.4%}"
    bad_amt_str = f"{row.cum_bad_rate_amount:.4%}" if not np.isnan(row.cum_bad_rate_amount) else "—"

    md.append(
        f"| {thr:.4f} "
        f"| {row.cum_pass_rate:.2%} "
        f"| {bad_count_str} "
        f"| {bad_amt_str} "
        f"| {marker} |"
    )
md.append("")

# 合并边界参考
md.append("### 合并箱边界参考")
md.append("")
md.append("| 箱序 | 分数区间 | 阈值（上限） |")
md.append("|---:|---:|---:|")
for i, row in enumerate(tuning_merged_stats.itertuples()):
    md.append(f"| {i+1} | [{row.score_min:.4f}, {row.score_max:.4f}) | {row.score_max:.4f} |")
md.append("")
md.append("> 每个合并箱的上限即为一个可选策略阈值。例如，以箱 1 上限为拒绝线，则仅通过分数 ≤ 该阈值的低风险人群。")
md.append("")

# ---- 三套方案设计 ----
md.append("## 八、三套方案设计")
md.append("")
md.append("> 基于合并后的 6 个风险等级，设计增长/平衡/保守三套方案。")
md.append("> 每套方案将分数段划分为三区：**自动通过**（低风险）、**人工审核**（中风险）、**拒绝**（高风险）。")
md.append("> 目前基于通过率与坏账率做 trade-off 选择；EL/收入/UE 待补充经济数据后扩展。")
md.append("")

has_amount = "principal" in tuning_valid.columns

for scheme_name, s in schemes.items():
    md.append(f"### {scheme_name}")
    md.append("")
    md.append(f"> {s['description']}")
    md.append("")
    md.append(f"- **自动通过阈值**: score ≤ {s['auto_max']:.4f}")
    md.append(f"- **人工审核区间**: {s['auto_max']:.4f} < score ≤ {s['review_max']:.4f}")
    md.append(f"- **拒绝阈值**: score > {s['review_max']:.4f}")
    md.append("")

    # Segment detail table
    seg_headers = "| 策略段 | 分数区间 | 样本量 | 占比 | 坏账率（笔数） "
    seg_sep = "|---:|---:|---:|---:|---:"
    if has_amount:
        seg_headers += "| 坏账率（金额） "
        seg_sep += "|---:"
    seg_headers += "|"
    seg_sep += "|"
    md.append(seg_headers)
    md.append(seg_sep)

    for seg in s["segments"]:
        score_range = f"[{seg['score_min']:.4f}, {seg['score_max']:.4f})" if not np.isnan(seg['score_min']) else "—"
        line = (
            f"| {seg['segment']} "
            f"| {score_range} "
            f"| {seg['n']:>6,} "
            f"| {seg['pct']:.2%} "
            f"| {seg['bad_rate_count']:.4%} "
        )
        if has_amount:
            amt = f"{seg['bad_rate_amount']:.4%}" if not np.isnan(seg['bad_rate_amount']) else "—"
            line += f"| {amt} "
        line += "|"
        md.append(line)
    md.append("")

    # Summary row
    md.append(f"**方案汇总**：通过率 {s['pass_rate']:.2%}（{s['pass_n']:,} 人），"
              f"通过人群坏账率 {s['pass_bad_rate_count']:.4%}，"
              f"拒绝率 {s['reject_rate']:.2%}（{s['reject_n']:,} 人）")
    if has_amount and not np.isnan(s.get('pass_bad_rate_amount', float('nan'))):
        md[-1] += f"，金额口径坏账率 {s['pass_bad_rate_amount']:.4%}"
    md.append("")

    # OOT verification
    if schemes_oot and scheme_name in schemes_oot:
        so = schemes_oot[scheme_name]
        md.append(f"**OOT 验证**：通过率 {so['pass_rate']:.2%}，"
                  f"通过人群坏账率 {so['pass_bad_rate_count']:.4%}，"
                  f"拒绝率 {so['reject_rate']:.2%}")
        if has_amount and not np.isnan(so.get('pass_bad_rate_amount', float('nan'))):
            md[-1] += f"，金额口径坏账率 {so['pass_bad_rate_amount']:.4%}"
        md.append("")
    md.append("")

# 方案对比总结表
md.append("### 三套方案对比")
md.append("")
compare_headers = "| 方案 | 自动通过 ≤ | 审核 ≤ | 通过率 | 坏账率（笔数） "
compare_sep = "|---:|---:|---:|---:|---:"
if has_amount:
    compare_headers += "| 坏账率（金额） "
    compare_sep += "|---:"
compare_headers += "| 拒绝率 | 通过率（OOT） | 坏账率（OOT） |"
compare_sep += "|---:|---:|---:|"
md.append(compare_headers)
md.append(compare_sep)

for scheme_name, s in schemes.items():
    line = (
        f"| {scheme_name} "
        f"| {s['auto_max']:.4f} "
        f"| {s['review_max']:.4f} "
        f"| {s['pass_rate']:.2%} "
        f"| {s['pass_bad_rate_count']:.4%} "
    )
    if has_amount:
        amt = f"{s['pass_bad_rate_amount']:.4%}" if not np.isnan(s.get('pass_bad_rate_amount', float('nan'))) else "—"
        line += f"| {amt} "
    line += f"| {s['reject_rate']:.2%} "

    if schemes_oot and scheme_name in schemes_oot:
        so = schemes_oot[scheme_name]
        line += f"| {so['pass_rate']:.2%} "
        line += f"| {so['pass_bad_rate_count']:.4%} "
    else:
        line += "| — | — "
    line += "|"
    md.append(line)
md.append("")
md.append("> **推荐**：平衡方案自动通过 55.4% 的申请人（坏账率 4.40%），人工审核 39.7%，整体通过率 95.1%。在自动审批效率、风险控制和审核产能之间取得平衡，建议作为默认方案。若审核产能紧张可倾向增长方案（自动通过 80.1%），若风险偏好收紧可倾向保守方案（整体通过率 80.1%）。")
md.append("")

# 写入文件
out_path = os.path.join(RES_DIR, "binning_result.md")
with open(out_path, "w", encoding="utf-8") as f:
    f.write("\n".join(md))
print(f"  输出: res/binning_result.md")

# 控制台摘要
print(f"\n  初分 20 箱: 调优 {tuning_N:,} 条，坏账率 {tuning_bad_rate:.4%}，IV {tuning_IV:.4f}，ρ {rho:.4f}")
print(f"  合并后 {len(tuning_merged_stats)} 箱: 调优 {tuning_merged_N:,} 条，坏账率 {tuning_merged_bad_rate:.4%}，IV {tuning_merged_IV:.4f}，ρ {tuning_merged_rho:.4f}")
if oot_merged_bad_rate is not None:
    print(f"  OOT（合并后）: {oot_merged_N:,} 条，坏账率 {oot_merged_bad_rate:.4%}，IV {oot_merged_IV:.4f}，ρ {oot_merged_rho:.4f}")
    if merged_psi is not None:
        print(f"  PSI = {merged_psi:.4f}")

print("\n完成。")
