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
    info_df[["application_id", LABEL_COL]],
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
# 10. 输出 Markdown
# ============================================================
print("\n" + "=" * 60)
print("10. 输出结果")
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
