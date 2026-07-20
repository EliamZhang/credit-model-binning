"""
等频 20 箱初始分箱脚本
========================
对策略调优样本进行等频 20 箱分箱，输出每箱的风险指标。

使用方式：
    python scr/equal_freq_binning.py

输出：
    res/binning_result.md                  — 分箱结果 Markdown 表格
"""

import pandas as pd
import numpy as np
import os

# ============================================================
# 参数配置
# ============================================================
LABEL_COL = "duedate_3m_30"       # 风险标签
SCORE_COL = "aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score"
N_BINS = 20                        # 初始分箱数
OOT_CUT_DATE = "2026-01-01"       # OOT 切分日期（>= 此日期为 OOT）

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

# 关联：取标签列
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

# 排除标签为 NULL 的记录（未成熟/未放款）
tuning_valid = tuning[tuning[LABEL_COL].notna()].copy()
oot_valid = oot[oot[LABEL_COL].notna()].copy()

print(f"  策略调优集（< {OOT_CUT_DATE}）: {len(tuning):,} 条，有效标签 {len(tuning_valid):,} 条")
print(f"  OOT 集    （>= {OOT_CUT_DATE}）: {len(oot):,} 条，有效标签 {len(oot_valid):,} 条")

if len(tuning_valid) == 0:
    raise RuntimeError("策略调优集无有效标签，请检查数据或切分日期。")

# ============================================================
# 3. 等频 20 箱
# ============================================================
print("\n" + "=" * 60)
print("3. 等频 20 箱分箱")
print("=" * 60)

scores = tuning_valid[SCORE_COL]
labels = tuning_valid[LABEL_COL].astype(int)

# 等频分箱，同分多时自动合并减少箱数
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
# 4. 计算各箱指标
# ============================================================
print("\n" + "=" * 60)
print("4. 计算各箱指标")
print("=" * 60)

bin_stats = tuning_valid.groupby("bin", observed=False).agg(
    score_min=(SCORE_COL, "min"),
    score_max=(SCORE_COL, "max"),
    n=(SCORE_COL, "count"),
    B=(LABEL_COL, "sum"),
).reset_index()

bin_stats["bad_rate"] = bin_stats["B"] / bin_stats["n"]
bin_stats["SE"] = np.sqrt(bin_stats["bad_rate"] * (1 - bin_stats["bad_rate"]) / bin_stats["n"])

# 按分数从低到高排序（低分 = 低风险 = 优先通过）
bin_stats = bin_stats.sort_values("score_min").reset_index(drop=True)
bin_stats.index = range(1, len(bin_stats) + 1)
bin_stats.index.name = "bin_no"

# 累计指标（从低分向高分累计，模拟"通过低风险客群"）
total_N = bin_stats["n"].sum()
total_B = bin_stats["B"].sum()

bin_stats["cum_n"] = bin_stats["n"].cumsum()
bin_stats["cum_B"] = bin_stats["B"].cumsum()
bin_stats["cum_pass_rate"] = bin_stats["cum_n"] / total_N
bin_stats["cum_bad_rate"] = bin_stats["cum_B"] / bin_stats["cum_n"]

# 整体坏账率
overall_bad_rate = total_B / total_N
print(f"  整体坏账率: {overall_bad_rate:.4%}")

# WOE & IV（坏/好方向定义）
bin_stats["G"] = bin_stats["n"] - bin_stats["B"]
bin_stats["B_pct"] = bin_stats["B"] / total_B
bin_stats["G_pct"] = bin_stats["G"] / (total_N - total_B)

# 避免除零
bin_stats["WOE"] = np.log(
    (bin_stats["B_pct"].replace(0, np.nan)) / (bin_stats["G_pct"].replace(0, np.nan))
)
bin_stats["WOE"] = bin_stats["WOE"].fillna(0)

bin_stats["IV_component"] = (bin_stats["B_pct"] - bin_stats["G_pct"]) * bin_stats["WOE"]
total_IV = bin_stats["IV_component"].sum()

print(f"  总 IV: {total_IV:.4f}")

# 单调性检查：Spearman 秩相关
from scipy.stats import spearmanr
rho, p_value = spearmanr(bin_stats.index, bin_stats["bad_rate"])
print(f"  箱序 vs 坏账率 Spearman ρ = {rho:.4f}（p = {p_value:.4f}）")
if abs(rho) < 0.9:
    print("  ⚠ 单调性较差，可能存在局部倒挂，后续合并步骤需关注。")

# ============================================================
# 5. 输出 Markdown 文件
# ============================================================
print("\n" + "=" * 60)
print("5. 输出结果")
print("=" * 60)

oot_bad_rate = oot_valid[LABEL_COL].astype(int).mean() if len(oot_valid) > 0 else None

md_lines = []
md_lines.append("# 等频 20 箱分箱结果")
md_lines.append("")
md_lines.append(f"> 策略调优集：{len(tuning_valid):,} 条（< {OOT_CUT_DATE}），OOT 集：{len(oot_valid):,} 条（>= {OOT_CUT_DATE}）")
md_lines.append(f"> 整体坏账率 = {overall_bad_rate:.4%}，总 IV = {total_IV:.4f}，Spearman ρ = {rho:.4f}")
if oot_bad_rate is not None:
    md_lines.append(f"> OOT 坏账率 = {oot_bad_rate:.4%}")
md_lines.append("")

md_lines.append("| 箱序 | 分数区间 | 样本量 | 坏样本 | 坏账率 | SE | 累计通过率 | 累计坏账率 | WOE |")
md_lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

for row in bin_stats.itertuples():
    score_range = f"[{row.score_min:.4f}, {row.score_max:.4f})"
    md_lines.append(
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

out_path = os.path.join(RES_DIR, "binning_result.md")
with open(out_path, "w", encoding="utf-8") as f:
    f.write("\n".join(md_lines))
print(f"  输出: res/binning_20_equal_freq.md")

# 控制台摘要
print(f"\n  整体坏账率 = {overall_bad_rate:.4%}，总 IV = {total_IV:.4f}，Spearman ρ = {rho:.4f}")
if oot_bad_rate is not None:
    print(f"  OOT 集: {len(oot_valid):,} 条，坏账率 = {oot_bad_rate:.4%}")
else:
    print("  OOT 集无有效标签，可能尚不成熟。")

print("\n完成。")
