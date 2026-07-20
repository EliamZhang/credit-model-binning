"""
分箱主脚本
==========
对策略调优样本进行等频分箱，输出每箱的风险指标及 OOT 跨期验证。

使用方式：
    python scr/binning.py

输出：
    res/binning_result.md  — 分箱结果 Markdown 表格（含策略调优集和 OOT 集对比）
"""

import pandas as pd
import numpy as np
import os

# ============================================================
# 参数配置
# ============================================================
LABEL_COL = "duedate_3m_30"
SCORE_COL = "aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score"
N_BINS = 20
OOT_CUT_DATE = "2026-01-01"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES_DIR = os.path.join(BASE_DIR, "res")

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


def compute_bin_stats(df, score_col, label_col):
    """汇总分箱指标。"""
    stats = df.groupby("bin", observed=False).agg(
        score_min=(score_col, "min"),
        score_max=(score_col, "max"),
        n=(score_col, "count"),
        B=(label_col, "sum"),
    ).reset_index()

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


# ============================================================
# 3. 等频分箱（基于策略调优集）
# ============================================================
print("\n" + "=" * 60)
print("3. 等频分箱")
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
# 4. 策略调优集各箱指标
# ============================================================
print("\n" + "=" * 60)
print("4. 计算策略调优集各箱指标")
print("=" * 60)

tuning_stats, tuning_N, tuning_B = compute_bin_stats(tuning_valid, SCORE_COL, LABEL_COL)
tuning_bad_rate = tuning_B / tuning_N
tuning_IV = tuning_stats["IV_component"].sum()

from scipy.stats import spearmanr
rho, p_value = spearmanr(tuning_stats.index, tuning_stats["bad_rate"])

print(f"  整体坏账率: {tuning_bad_rate:.4%}")
print(f"  总 IV: {tuning_IV:.4f}")
print(f"  箱序 vs 坏账率 Spearman ρ = {rho:.4f}（p = {p_value:.4f}）")
if abs(rho) < 0.9:
    print("  ⚠ 单调性较差，可能存在局部倒挂，后续合并步骤需关注。")

# ============================================================
# 5. OOT 集应用同一分箱
# ============================================================
print("\n" + "=" * 60)
print("5. OOT 集跨期验证")
print("=" * 60)

if len(oot_valid) > 0:
    oot_labels = oot_valid[LABEL_COL].astype(int)
    oot_valid["bin"] = pd.cut(oot_valid[SCORE_COL], bins=bins, duplicates="drop")
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
# 6. 输出 Markdown 文件
# ============================================================
print("\n" + "=" * 60)
print("6. 输出结果")
print("=" * 60)

md_lines = []
md_lines.append("# 等频 20 箱分箱结果")
md_lines.append("")

# 摘要
md_lines.append("## 摘要")
md_lines.append("")
md_lines.append(f"| 项目 | 策略调优集 | OOT 集 |")
md_lines.append(f"|:---|---:|---:|")
md_lines.append(f"| 样本切分 | `< {OOT_CUT_DATE}` | `>= {OOT_CUT_DATE}` |")
md_lines.append(f"| 总样本量 | {len(tuning_valid):,} | {len(oot_valid):,} |")
md_lines.append(f"| 坏账率 | {tuning_bad_rate:.4%} | {oot_bad_rate:.4%}" if oot_bad_rate is not None else f"| 坏账率 | {tuning_bad_rate:.4%} | N/A |")
md_lines.append(f"| IV | {tuning_IV:.4f} | {oot_IV:.4f}" if oot_stats is not None else f"| IV | {tuning_IV:.4f} | N/A |")
md_lines.append(f"| Spearman ρ | {rho:.4f} | {oot_rho:.4f}" if oot_stats is not None else f"| Spearman ρ | {rho:.4f} | N/A |")
md_lines.append("")

# 策略调优集表格
md_lines.append("## 策略调优集分箱明细")
md_lines.append("")
md_lines.append("| 箱序 | 分数区间 | 样本量 | 坏样本 | 坏账率 | SE | 累计通过率 | 累计坏账率 | WOE |")
md_lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

for row in tuning_stats.itertuples():
    md_lines.append(format_bin_row(row))

md_lines.append("")

# OOT 集表格
if oot_stats is not None:
    md_lines.append("## OOT 集分箱明细（同一分箱切点）")
    md_lines.append("")
    md_lines.append("| 箱序 | 分数区间 | 样本量 | 坏样本 | 坏账率 | SE | 累计通过率 | 累计坏账率 | WOE |")
    md_lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

    for row in oot_stats.itertuples():
        md_lines.append(format_bin_row(row))

    md_lines.append("")

out_path = os.path.join(RES_DIR, "binning_result.md")
with open(out_path, "w", encoding="utf-8") as f:
    f.write("\n".join(md_lines))
print(f"  输出: res/binning_result.md")

# 控制台摘要
print(f"\n  策略调优集: {len(tuning_valid):,} 条，坏账率 = {tuning_bad_rate:.4%}，IV = {tuning_IV:.4f}，ρ = {rho:.4f}")
if oot_bad_rate is not None:
    print(f"  OOT 集:   {len(oot_valid):,} 条，坏账率 = {oot_bad_rate:.4%}，IV = {oot_IV:.4f}，ρ = {oot_rho:.4f}")
else:
    print("  OOT 集无有效标签，可能尚不成熟。")

print("\n完成。")
