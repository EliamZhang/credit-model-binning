"""一次性实验：评估拆 B 档后的人数分布与区分度影响。"""
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

import binning

data = binning.load_analysis_data()
all_data, train, oot = binning.split_train_oot(data)

edges = binning.learn_equal_freq_edges(train, binning.SCORE_COL, binning.INITIAL_BIN_COUNT)
initial_edges = binning.build_initial_edge_table(edges)
all_binned = binning.apply_edges(all_data, binning.SCORE_COL, edges, binning.INITIAL_BIN_COL)
train_binned = all_binned.loc[all_binned["sample_group"].eq("train")].copy()
oot_binned = all_binned.loc[all_binned["sample_group"].eq("oot")].copy()

train_initial_stats = binning.calc_complete_initial_stats(train_binned, initial_edges)
oot_initial_stats = binning.calc_complete_initial_stats(oot_binned, initial_edges)

PLANS = {
    # 当前方案与拆 B 档的多个变体
    "当前7档": [(1, 1), (2, 8), (9, 11), (12, 15), (16, 17), (18, 19), (20, 20)],
    "拆B:7档(2,4)(5,8)+合(16,19)": [(1, 1), (2, 4), (5, 8), (9, 11), (12, 15), (16, 19), (20, 20)],
    "拆B:7档(2,4)(5,8)+合(14,15,12)": [(1, 1), (2, 4), (5, 8), (9, 11), (12, 15), (16, 17), (18, 20)],
    "拆B:7档(2,5)(6,8)+合(12,15,16)": [(1, 1), (2, 5), (6, 8), (9, 11), (12, 16), (17, 19), (20, 20)],
    "拆B:8档": [(1, 1), (2, 4), (5, 8), (9, 11), (12, 15), (16, 17), (18, 19), (20, 20)],
    "拆B:9档": [(1, 1), (2, 4), (5, 8), (9, 11), (12, 13), (14, 15), (16, 17), (18, 19), (20, 20)],
    "当前8档": [(1, 1), (2, 8), (9, 11), (12, 13), (14, 15), (16, 17), (18, 19), (20, 20)],
}

print(f"{'方案':<28} {'档数':>3} {'各档样本占比(Train)':<44} {'maxShare':>8} {'IV':>7} {'IV保留':>7} {'违例':>4} {'约束':>4} {'PSI':>7}")
for name, ranges in PLANS.items():
    tr = binning.aggregate_initial_stats_by_ranges(train_initial_stats, ranges)
    ot = binning.aggregate_initial_stats_by_ranges(oot_initial_stats, ranges)

    n = tr["n"].sum()
    shares = tr["n"] / n
    max_share = shares.max()
    shares_txt = " ".join(f"{s:.0%}" for s in shares)

    iv = binning.calc_iv_from_stats(tr)
    iv_ret = binning.safe_div(iv, binning.calc_iv_from_stats(train_initial_stats))

    prim_inv = binning.count_rate_inversions(tr, binning.PRIMARY_RATE_COLS, tolerance=binning.TRAIN_INVERSION_TOLERANCE)
    all_inv = binning.count_rate_inversions(tr, binning.ALL_RISK_RATE_COLS, tolerance=binning.TRAIN_INVERSION_TOLERANCE)
    cons = binning.calc_bin_constraint_details(tr)
    cons_viol = int((~cons["all_constraints_ok"]).sum())

    psi = binning.calc_psi_from_bin_stats(tr, ot)

    print(f"{name:<28} {len(ranges):>3} {shares_txt:<44} {max_share:>7.2%} {iv:>7.4f} {iv_ret:>7.4f} {prim_inv:>4} {cons_viol:>4} {psi:>7.4f}")

print()
print("整体 IV(初始20箱):", f"{binning.calc_iv_from_stats(train_initial_stats):.4f}")
print()

# 详细看两个最可行方案：当前7档 vs 拆B 7档(2,4)(5,8)+合(16,19) vs 8档
for name in ["当前7档", "拆B:7档(2,4)(5,8)+合(16,19)", "拆B:8档"]:
    ranges = PLANS[name]
    tr = binning.aggregate_initial_stats_by_ranges(train_initial_stats, ranges)
    ot = binning.aggregate_initial_stats_by_ranges(oot_initial_stats, ranges)
    print(f"=== {name} ({len(ranges)}档)")
    for i in range(len(ranges)):
        r3t = tr[f"3m30p_cnt_bad_rate"].iloc[i]
        r3o = ot[f"3m30p_cnt_bad_rate"].iloc[i]
        r1t = tr[f"1m30p_cnt_bad_rate"].iloc[i]
        print(f"  {chr(65+i)}  Train n={tr['n'].iloc[i]:7.0f} ({tr['n'].iloc[i]/tr['n'].sum():5.2%})  1M30+ {r1t:.2%}  3M30+ {r3t:.2%}   |  OOT 3M30+ {r3o:.2%}")

# 模拟方案1的阈值选择：构造 curve（各档右边界 → 累计/边际指标）
print()
print("=== 方案1 阈值选择模拟（逐档校验）")
for name in ["当前7档", "拆B:7档(2,4)(5,8)+合(16,19)"]:
    ranges = PLANS[name]
    tr = binning.aggregate_initial_stats_by_ranges(train_initial_stats, ranges)
    print(f"--- {name}")
    n_total = tr["n"].sum()
    cum_n = cum_bad1 = cum_mat1 = cum_bad3 = cum_mat3 = 0.0
    for i in range(len(ranges)):
        row = tr.iloc[i]
        cum_n += row["n"]
        cum_bad1 += row["1m30p_cnt_bad"]; cum_mat1 += row["1m30p_cnt_mature"]
        cum_bad3 += row["3m30p_cnt_bad"]; cum_mat3 += row["3m30p_cnt_mature"]
        cum1 = cum_bad1 / cum_mat1
        cum3 = cum_bad3 / cum_mat3
        marg3 = row["3m30p_cnt_bad"] / row["3m30p_cnt_mature"]
        auto_ok = (cum1 <= 0.009) and (cum3 <= 0.055) and (marg3 <= 0.09)
        acc_ok = (cum1 <= 0.013) and (cum3 <= 0.075) and (marg3 <= 0.17)
        print(f"  {chr(65+i)} thresh={row['score_right']:.6f} cum_pass={cum_n/n_total:.2%} cum1m={cum1:.2%} cum3m={cum3:.2%} marg3m={marg3:.2%}  auto={'✓' if auto_ok else '✗'} accept={'✓' if acc_ok else '✗'}")
