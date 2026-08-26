# -*- coding: utf-8 -*-
"""
价值模型条件分箱：在 mlt 主模型分档内的价值模型分箱（笔数口径）

mlt 主模型分（笔数口径）已按 binning_mlt_cnt.py 评审为 7 档（A–G，高分高风险）。
本脚本在其基础上对价值模型分做**条件分箱**：在每个 mlt 档内，用 Train 样本对
score_worthiness 做 3 等频子箱切分（边界复用 OOT），形成 "mlt 档 × 价值子箱" 的
21 个组合格（A1–G3，combined_order 1–21），用于评估：

1. 组合方案的整体风险区分度（IV / 序数 AUC / KS 对比 mlt 单模型分档）；
2. 头部（最好档内再分层）与尾部（最坏档内再分层）风险人群的拉开效果；
3. 组合格的单调性与 Train/OOT 分布稳定性（PSI）；
4. 组合格在现行策略分段内的再分层能力（应用示意）。

核心流程（对应日志 1/6 ~ 6/6）：
1. 复用 mlt 与价值模型两套管线生成各自 7 档最终分档，取 306,149 笔双分样本；
2. 在每个 mlt 档内（Train）对价值分等频切 3 个子箱，边界复用到 OOT，生成组合格；
3. 计算组合格的样本量、1M30+/3M30+ 笔数逾期率（含 95% Wilson CI）、金额逾期率与 Lift；
4. 评估：档内子箱单调性与相邻显著性、头尾拉开、IV / AUC / KS / PSI 对比；
5. 输出 7 个 sheet 的 Excel 条件分箱报告。

运行方式：
    python binning_worthiness_cond_cnt.py

输入目录：res/
输出文件：out/binning_worthiness_cond_strategy_report_YYYYMMDD.xlsx
"""

import time
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

import binning_mlt_cnt as mlt
import binning_worthiness_cnt as wth
from binning_cross_mlt_wth import (
    add_bin_labels,
    amt_rate_of,
    build_cross_sample,
    build_model_final_bins,
    calc_auc_ks,
    group_overall_rates,
    rate_of,
    write_sheet,
)

OUT_DIR = Path("out")
REPORT_PATH = OUT_DIR / f"binning_worthiness_cond_strategy_report_{time.strftime('%Y%m%d')}.xlsx"

# 每个 mlt 档内价值模型分等频切分的子箱数（组合格总数 = 7 × 3 = 21）。
SUB_BIN_COUNT = 3

# 相邻子箱显著性检验的锚定指标（3M30+ 笔数口径）。
ANCHOR_DUE_COL = "duedate_3m_30"
ANCHOR_DPD_COL = "dpd_days_ever_mob3"
ANCHOR_REMAINING_COL = "estimate_principal_remaining_mob3"

# 风险类指标展示的样本量下限。
MIN_CELL_N_FOR_RATE = 50


# ============================================================
# 1. 条件分箱边界学习与应用
# ============================================================


def learn_sub_bin_edges(train_cross: pd.DataFrame) -> Dict[int, np.ndarray]:
    """在每个 mlt 档内（Train）对价值分做等频切分，返回 {mlt 档位: 边界数组}。"""
    tier_edges: Dict[int, np.ndarray] = {}
    for tier in range(1, 8):
        frame = train_cross.loc[train_cross["mlt_bin_order"].eq(tier)]
        score = pd.to_numeric(frame["wth_score"], errors="coerce").dropna()
        _, raw_edges = pd.qcut(score, q=SUB_BIN_COUNT, retbins=True, duplicates="drop")
        edges = np.unique(np.asarray(raw_edges, dtype="float64"))
        if len(edges) < 2:
            raise ValueError(
                f"mlt {chr(64 + tier)} 档内价值分唯一值不足，无法切分子箱"
            )
        edges[0] = -np.inf
        edges[-1] = np.inf
        tier_edges[tier] = edges
    return tier_edges


def apply_sub_bins(
    cross: pd.DataFrame,
    tier_edges: Dict[int, np.ndarray],
) -> pd.DataFrame:
    """把各档内子箱边界应用到样本，生成 wth_sub_bin 与组合标签/序。"""
    result = cross.copy()
    sub_parts: List[pd.Series] = []
    for tier in range(1, 8):
        mask = result["mlt_bin_order"].eq(tier)
        sub = pd.cut(
            result.loc[mask, "wth_score"],
            bins=tier_edges[tier],
            labels=list(range(1, len(tier_edges[tier]))),
            include_lowest=True,
        )
        sub_parts.append(sub)
    result["wth_sub_bin"] = pd.concat(sub_parts).astype("Int64")
    result["combined_label"] = (
        result["mlt_bin"] + result["wth_sub_bin"].astype("Int64").astype("string")
    )
    result["combined_order"] = (
        (result["mlt_bin_order"].astype(int) - 1) * SUB_BIN_COUNT
        + result["wth_sub_bin"]
    ).astype(int)
    return result


def build_sub_bin_edge_table(tier_edges: Dict[int, np.ndarray]) -> pd.DataFrame:
    """输出每个 mlt 档内价值分子箱的边界表。"""
    rows = []
    for tier in range(1, 8):
        edges = tier_edges[tier]
        for idx in range(len(edges) - 1):
            rows.append(
                {
                    "mlt_bin_order": tier,
                    "mlt_bin": chr(64 + tier),
                    "wth_sub_bin": idx + 1,
                    "combined_label": f"{chr(64 + tier)}{idx + 1}",
                    "score_left": edges[idx],
                    "score_right": edges[idx + 1],
                }
            )
    return pd.DataFrame(rows)


# ============================================================
# 2. 组合格统计
# ============================================================


def build_combined_stats(
    cross: pd.DataFrame,
    sample_group: str,
) -> pd.DataFrame:
    """某样本组的 21 个组合格统计（含 95% Wilson CI 与 Lift）。"""
    group = cross.loc[cross["sample_group"].eq(sample_group)].copy()
    overall = group_overall_rates(group)
    total = len(group)
    group_principal = float(
        pd.to_numeric(group["principal"], errors="coerce").fillna(0).sum()
    )

    rows: List[Dict] = []
    for tier in range(1, 8):
        for sub in range(1, SUB_BIN_COUNT + 1):
            cell = group.loc[
                group["mlt_bin_order"].eq(tier) & group["wth_sub_bin"].eq(sub)
            ]
            if cell.empty:
                continue
            n = len(cell)
            m3, b3, r3 = rate_of(cell, "duedate_3m_30")
            m1, b1, r1 = rate_of(cell, "duedate_1m_30")
            ci3_low, ci3_high = mlt.wilson_ci(np.array([b3]), np.array([m3]))
            ci1_low, ci1_high = mlt.wilson_ci(np.array([b1]), np.array([m1]))
            a3 = amt_rate_of(cell, ANCHOR_DPD_COL, ANCHOR_REMAINING_COL)
            principal = float(
                pd.to_numeric(cell["principal"], errors="coerce").fillna(0).sum()
            )
            funnel = mlt._actual_funnel_row(cell, "cell")
            rows.append(
                {
                    "combined_order": (tier - 1) * SUB_BIN_COUNT + sub,
                    "combined_label": f"{chr(64 + tier)}{sub}",
                    "mlt_bin_order": tier,
                    "mlt_bin": chr(64 + tier),
                    "wth_sub_bin": sub,
                    "n": n,
                    "sample_pct": n / total,
                    "1m30p_cnt_bad_rate": r1,
                    "1m30p_cnt_bad_rate_ci_low": float(ci1_low[0]),
                    "1m30p_cnt_bad_rate_ci_high": float(ci1_high[0]),
                    "3m30p_cnt_bad_rate": r3,
                    "3m30p_cnt_bad_rate_ci_low": float(ci3_low[0]),
                    "3m30p_cnt_bad_rate_ci_high": float(ci3_high[0]),
                    "3m30p_amt_bad_rate": a3,
                    "3m30p_cnt_lift": r3 / overall["3m30p_cnt_bad_rate"]
                    if overall["3m30p_cnt_bad_rate"] else np.nan,
                    "principal_pct": principal / group_principal if group_principal else np.nan,
                    "actual_approval_rate": funnel["actual_approval_rate"],
                    "actual_deal_rate": funnel["actual_deal_rate"],
                    "3m30p_cnt_mature": m3,
                    "3m30p_cnt_bad": b3,
                    "3m30p_cnt_good": m3 - b3,
                }
            )
    return pd.DataFrame(rows)


# ============================================================
# 3. 档内子箱评估与头尾拉开
# ============================================================


def build_tier_sub_bin_summary(train_stats: pd.DataFrame) -> pd.DataFrame:
    """每个 mlt 档内的子箱风险、跨度与相邻显著性（Train）。"""
    rows: List[Dict] = []
    for tier in range(1, 8):
        frame = train_stats.loc[train_stats["mlt_bin_order"].eq(tier)].sort_values(
            "wth_sub_bin"
        )
        tier_cells = frame[
            ["combined_label", "n", "3m30p_cnt_bad", "3m30p_cnt_mature", "3m30p_cnt_bad_rate"]
        ].copy()
        rates = [
            row["3m30p_cnt_bad_rate"]
            for _, row in tier_cells.iterrows()
            if row["n"] >= MIN_CELL_N_FOR_RATE
        ]
        spread = (max(rates) - min(rates)) if len(rates) >= 2 else np.nan

        p_values = []
        for k in range(1, SUB_BIN_COUNT):
            left = tier_cells.loc[tier_cells["combined_label"].eq(f"{chr(64 + tier)}{k}")]
            right = tier_cells.loc[tier_cells["combined_label"].eq(f"{chr(64 + tier)}{k + 1}")]
            if left.empty or right.empty:
                p_values.append(np.nan)
                continue
            p_values.append(
                mlt.two_proportion_pvalue(
                    float(left.iloc[0]["3m30p_cnt_bad"]),
                    float(left.iloc[0]["3m30p_cnt_mature"]),
                    float(right.iloc[0]["3m30p_cnt_bad"]),
                    float(right.iloc[0]["3m30p_cnt_mature"]),
                )
            )

        row = {
            "mlt_bin": chr(64 + tier),
            "mlt_bin_order": tier,
        }
        for k in range(1, SUB_BIN_COUNT + 1):
            hit = tier_cells.loc[tier_cells["combined_label"].eq(f"{chr(64 + tier)}{k}")]
            if hit.empty:
                row[f"sub{k}_3m30p"] = np.nan
                row[f"sub{k}_n"] = 0
            else:
                row[f"sub{k}_3m30p"] = hit.iloc[0]["3m30p_cnt_bad_rate"]
                row[f"sub{k}_n"] = int(hit.iloc[0]["n"])
        row["within_tier_spread"] = spread
        for idx in range(SUB_BIN_COUNT - 1):
            row[f"p_sub{idx + 1}_vs_sub{idx + 2}"] = p_values[idx]
        rows.append(row)
    return pd.DataFrame(rows)


def build_head_tail_eval(
    train_stats: pd.DataFrame,
    oot_stats: pd.DataFrame,
    cross: pd.DataFrame,
) -> pd.DataFrame:
    """头部（A 档内最安全子箱）与尾部（G 档内最危险子箱）的拉开评估。"""
    rows: List[Dict] = []
    for group_key, display_name, stats in [
        ("train", "Train", train_stats),
        ("oot", "OOT", oot_stats),
    ]:
        group = cross.loc[cross["sample_group"].eq(group_key)]
        for tier_label, tier in [("头部", 1), ("尾部", 7)]:
            tier_frame = group.loc[group["mlt_bin_order"].eq(tier)]
            _, _, tier_rate = rate_of(tier_frame, "duedate_3m_30")
            best = stats.loc[stats["combined_label"].eq(f"{chr(64 + tier)}1")]
            worst = stats.loc[stats["combined_label"].eq(f"{chr(64 + tier)}{SUB_BIN_COUNT}")]
            rows.append(
                {
                    "sample_group": display_name,
                    "position": tier_label,
                    "mlt_tier": chr(64 + tier),
                    "mlt_tier_n": len(tier_frame),
                    "mlt_tier_3m30p": tier_rate,
                    "best_sub_label": f"{chr(64 + tier)}1",
                    "best_sub_3m30p": float(best.iloc[0]["3m30p_cnt_bad_rate"]) if not best.empty else np.nan,
                    "best_sub_lift": float(best.iloc[0]["3m30p_cnt_lift"]) if not best.empty else np.nan,
                    "worst_sub_label": f"{chr(64 + tier)}{SUB_BIN_COUNT}",
                    "worst_sub_3m30p": float(worst.iloc[0]["3m30p_cnt_bad_rate"]) if not worst.empty else np.nan,
                    "worst_sub_lift": float(worst.iloc[0]["3m30p_cnt_lift"]) if not worst.empty else np.nan,
                    "sub_spread": (
                        float(worst.iloc[0]["3m30p_cnt_bad_rate"]) - float(best.iloc[0]["3m30p_cnt_bad_rate"])
                        if not best.empty and not worst.empty else np.nan
                    ),
                }
            )
    return pd.DataFrame(rows)


# ============================================================
# 4. 区分度与稳定性对比
# ============================================================


def build_discrimination_comparison(cross: pd.DataFrame) -> pd.DataFrame:
    """mlt 7 档 / 价值 7 档 / 组合 21 格的 IV、序数 AUC、KS 与单调性对比。"""
    rows: List[Dict] = []
    for group_name in ["train", "oot"]:
        group = cross.loc[cross["sample_group"].eq(group_name)]
        for scheme, key in [
            ("mlt 7 档", "mlt_bin_order"),
            ("价值模型 7 档", "wth_bin_order"),
            ("组合 21 格", "combined_order"),
        ]:
            for label_name, label_col in [("1M30+", "duedate_1m_30"), ("3M30+", "duedate_3m_30")]:
                frame = group.loc[group[label_col].isin([0, 1]) & group[key].notna()]
                bad = frame[label_col].eq(1).sum()
                mature = len(frame)
                auc, ks = calc_auc_ks(
                    pd.to_numeric(group[key], errors="coerce"),
                    pd.to_numeric(group[label_col], errors="coerce"),
                )
                rows.append(
                    {
                        "sample_group": group_name,
                        "scheme": scheme,
                        "label": label_name,
                        "auc": auc,
                        "ks": ks,
                        "mature": int(mature),
                        "bad": int(bad),
                    }
                )
    return pd.DataFrame(rows)


def calc_scheme_iv(
    cross: pd.DataFrame,
    key: str,
    group_name: str = "train",
) -> float:
    """某分组方案（档位/组合格）的 3M30+ IV。"""
    group = cross.loc[cross["sample_group"].eq(group_name)]
    parts = []
    for value in sorted(group[key].dropna().unique().astype(int)):
        frame = group.loc[group[key].eq(value)]
        _, bad, _ = rate_of(frame, "duedate_3m_30")
        mature = int(
            pd.to_numeric(frame["duedate_3m_30"], errors="coerce").isin([0, 1]).sum()
        )
        parts.append(
            {
                "3m30p_cnt_bad": bad,
                "3m30p_cnt_good": mature - bad,
            }
        )
    stats = pd.DataFrame(parts)
    return mlt.calc_iv_from_stats(stats)


def build_iv_comparison(cross: pd.DataFrame) -> pd.DataFrame:
    """三种分组方案的 3M30+ IV 对比。"""
    rows = []
    for scheme, key in [
        ("mlt 7 档", "mlt_bin_order"),
        ("价值模型 7 档", "wth_bin_order"),
        ("组合 21 格", "combined_order"),
    ]:
        rows.append(
            {
                "scheme": scheme,
                "bin_count": int(cross.loc[cross["sample_group"].eq("train"), key].nunique()),
                "train_3m30p_iv": calc_scheme_iv(cross, key, "train"),
                "oot_3m30p_iv": calc_scheme_iv(cross, key, "oot"),
            }
        )
    return pd.DataFrame(rows)


def check_combined_monotonicity(stats: pd.DataFrame, sample_group: str) -> pd.DataFrame:
    """组合格按 combined_order 排序后检查 3M30+/1M30+ 笔数逾期率的倒挂。"""
    ordered = stats.sort_values("combined_order").reset_index(drop=True)
    rows = []
    for rate_col in ["1m30p_cnt_bad_rate", "3m30p_cnt_bad_rate"]:
        rates = ordered[rate_col].astype(float)
        inversions = int((rates.diff() < 0).sum())
        drop = float(rates.diff().min()) if inversions else 0.0
        rows.append(
            {
                "sample_group": sample_group,
                "rate_col": rate_col,
                "inversion_cnt": inversions,
                "max_drop": drop,
            }
        )
    return pd.DataFrame(rows)


def calc_combined_psi(train_stats: pd.DataFrame, oot_stats: pd.DataFrame) -> float:
    """组合 21 格分布的 Train/OOT PSI。"""
    merged = train_stats[["combined_label", "sample_pct"]].merge(
        oot_stats[["combined_label", "sample_pct"]],
        on="combined_label",
        how="outer",
        suffixes=("_train", "_oot"),
    ).fillna(0.0)
    eps = 1e-6
    psi = float(
        np.sum(
            (merged["sample_pct_oot"] - merged["sample_pct_train"])
            * np.log((merged["sample_pct_oot"] + eps) / (merged["sample_pct_train"] + eps))
        )
    )
    return psi


def build_segment_relayer_table(cross: pd.DataFrame) -> pd.DataFrame:
    """现行 mlt 策略分段内，价值子箱的再分层能力（应用示意）。"""
    rows: List[Dict] = []
    for group_name in ["train", "oot"]:
        group = cross.loc[cross["sample_group"].eq(group_name)]
        for seg_label, mask in [
            ("自动通过（mlt ≤ 3）", group["mlt_bin_order"].le(3)),
            ("人工审核（3 < mlt ≤ 5）", group["mlt_bin_order"].gt(3) & group["mlt_bin_order"].le(5)),
            ("拒绝（mlt > 5）", group["mlt_bin_order"].gt(5)),
        ]:
            seg = group.loc[mask]
            best = seg.loc[seg["wth_sub_bin"].eq(1)]
            worst = seg.loc[seg["wth_sub_bin"].eq(SUB_BIN_COUNT)]
            _, _, seg_rate = rate_of(seg, "duedate_3m_30")
            _, _, best_rate = rate_of(best, "duedate_3m_30")
            _, _, worst_rate = rate_of(worst, "duedate_3m_30")
            rows.append(
                {
                    "sample_group": group_name,
                    "segment": seg_label,
                    "segment_n": len(seg),
                    "segment_3m30p": seg_rate,
                    "sub1_3m30p": best_rate,
                    "sub3_3m30p": worst_rate,
                    "sub_spread": (worst_rate - best_rate) if worst_rate == worst_rate and best_rate == best_rate else np.nan,
                }
            )
    return pd.DataFrame(rows)


def build_sub_bin_strategy_table(cross: pd.DataFrame) -> pd.DataFrame:
    """二维策略对照：mlt 档上限 × 条件子箱上限 的自动通过/总接纳方案。"""
    rows: List[Dict] = []
    policies = [
        ("mlt 单模型（现行）", 3, SUB_BIN_COUNT, 5, SUB_BIN_COUNT),
        ("接纳收子箱 ≤2", 3, SUB_BIN_COUNT, 5, 2),
        ("自动通过收子箱 ≤2", 3, 2, 5, SUB_BIN_COUNT),
        ("两端均收子箱 ≤2", 3, 2, 5, 2),
        ("两端均收子箱 ≤1", 3, 1, 5, 1),
    ]
    for name, auto_mlt, auto_sub, acc_mlt, acc_sub in policies:
        row = {
            "policy": name,
            "mlt_auto_bin": auto_mlt,
            "sub_auto_bin": auto_sub,
            "mlt_accept_bin": acc_mlt,
            "sub_accept_bin": acc_sub,
        }
        for group_key, prefix in [("train", "train"), ("oot", "oot")]:
            g = cross.loc[cross["sample_group"].eq(group_key)]
            total = len(g)
            auto = g["mlt_bin_order"].le(auto_mlt) & g["wth_sub_bin"].le(auto_sub)
            accept = g["mlt_bin_order"].le(acc_mlt) & g["wth_sub_bin"].le(acc_sub)
            manual = accept & ~auto
            reject = ~accept
            for seg_label, mask in [("auto", auto), ("manual", manual), ("accept", accept), ("reject", reject)]:
                seg = g.loc[mask]
                _, _, r3 = rate_of(seg, "duedate_3m_30")
                row[f"{prefix}_{seg_label}_rate"] = len(seg) / total
                row[f"{prefix}_{seg_label}_3m30p"] = r3
        rows.append(row)
    return pd.DataFrame(rows)


def build_sub_bin_strategy_grid(cross: pd.DataFrame) -> pd.DataFrame:
    """AND 接纳网格：mlt 档上限 × 条件子箱上限，Train/OOT 接纳率与接纳 3M30+。"""
    rows: List[Dict] = []
    for mlt_cut in range(1, 8):
        for sub_cut in range(1, SUB_BIN_COUNT + 1):
            row = {"mlt_accept_bin": mlt_cut, "sub_accept_bin": sub_cut}
            for group_key, prefix in [("train", "train"), ("oot", "oot")]:
                g = cross.loc[cross["sample_group"].eq(group_key)]
                total = len(g)
                accept = g["mlt_bin_order"].le(mlt_cut) & g["wth_sub_bin"].le(sub_cut)
                seg = g.loc[accept]
                _, _, r3 = rate_of(seg, "duedate_3m_30")
                row[f"{prefix}_accept_rate"] = len(seg) / total
                row[f"{prefix}_accept_3m30p"] = r3
            row["is_current_mlt"] = mlt_cut == 5 and sub_cut == SUB_BIN_COUNT
            rows.append(row)
    return pd.DataFrame(rows)


# ============================================================
# 5. 主流程
# ============================================================


def main() -> None:
    t0 = time.time()
    print("=" * 60)
    print("价值模型条件分箱：mlt 分档内价值分子箱（笔数口径）")
    print("=" * 60)

    # 1) 两模型最终分档与双分样本。
    mlt_final, mlt_edges, mlt_ranges = build_model_final_bins(mlt, "mlt")
    wth_final, wth_edges, wth_ranges = build_model_final_bins(wth, "wth")
    mlt_edges = add_bin_labels(mlt_edges, mlt.FINAL_BIN_COL)
    wth_edges = add_bin_labels(wth_edges, wth.FINAL_BIN_COL)
    cross = build_cross_sample(mlt_final, wth_final)
    train_cross = cross.loc[cross["sample_group"].eq("train")].copy()
    print(f"[1/6] 双分样本：{len(cross):,} 笔（Train {len(train_cross):,}）")

    # 2) 档内条件切分。
    tier_edges = learn_sub_bin_edges(train_cross)
    cross = apply_sub_bins(cross, tier_edges)
    edge_table = build_sub_bin_edge_table(tier_edges)
    print(f"[2/6] 条件分箱完成：{len(edge_table)} 个子箱边界")

    # 3) 组合格统计。
    train_stats = build_combined_stats(cross, "train")
    oot_stats = build_combined_stats(cross, "oot")
    print(f"[3/6] 组合格统计完成（Train {len(train_stats)} 格 / OOT {len(oot_stats)} 格）")

    # 4) 档内评估与头尾拉开。
    tier_summary = build_tier_sub_bin_summary(train_stats)
    head_tail = build_head_tail_eval(train_stats, oot_stats, cross)
    monotonicity = pd.concat(
        [
            check_combined_monotonicity(train_stats, "train"),
            check_combined_monotonicity(oot_stats, "oot"),
        ],
        ignore_index=True,
    )
    psi_combined = calc_combined_psi(train_stats, oot_stats)
    print(f"[4/6] 档内评估完成：组合分布 PSI={psi_combined:.4f}")

    # 5) 区分度对比。
    disc = build_discrimination_comparison(cross)
    iv_cmp = build_iv_comparison(cross)
    seg_relay = build_segment_relayer_table(cross)
    strategy_table = build_sub_bin_strategy_table(cross)
    strategy_grid = build_sub_bin_strategy_grid(cross)
    print("[5/6] 区分度对比与二维策略模拟完成")

    # 6) 总览与附录。
    train_head = head_tail.loc[
        (head_tail["sample_group"].eq("Train")) & (head_tail["position"].eq("头部"))
    ].iloc[0]
    train_tail = head_tail.loc[
        (head_tail["sample_group"].eq("Train")) & (head_tail["position"].eq("尾部"))
    ].iloc[0]
    oot_head = head_tail.loc[
        (head_tail["sample_group"].eq("OOT")) & (head_tail["position"].eq("头部"))
    ].iloc[0]
    oot_tail = head_tail.loc[
        (head_tail["sample_group"].eq("OOT")) & (head_tail["position"].eq("尾部"))
    ].iloc[0]

    overview = pd.DataFrame(
        [
            {"section": "样本", "metric": "双分样本量（Train / OOT）", "value": f"{len(train_cross):,} / {int(cross['sample_group'].eq('oot').sum()):,}"},
            {"section": "设计", "metric": "子箱切分方式", "value": f"每个 mlt 档内对价值分等频切 {SUB_BIN_COUNT} 个子箱（Train 学边界、OOT 复用）"},
            {"section": "设计", "metric": "组合格数量", "value": len(train_stats)},
            {"section": "设计", "metric": "组合分布 Train/OOT PSI", "value": psi_combined},
            {"section": "单调性", "metric": "Train 组合格 3M30+ / 1M30+ 倒挂数", "value": f"{int(monotonicity.loc[(monotonicity['sample_group'].eq('train')) & (monotonicity['rate_col'].eq('3m30p_cnt_bad_rate')), 'inversion_cnt'].iloc[0])} / {int(monotonicity.loc[(monotonicity['sample_group'].eq('train')) & (monotonicity['rate_col'].eq('1m30p_cnt_bad_rate')), 'inversion_cnt'].iloc[0])}"},
            {"section": "单调性", "metric": "OOT 组合格 3M30+ / 1M30+ 倒挂数", "value": f"{int(monotonicity.loc[(monotonicity['sample_group'].eq('oot')) & (monotonicity['rate_col'].eq('3m30p_cnt_bad_rate')), 'inversion_cnt'].iloc[0])} / {int(monotonicity.loc[(monotonicity['sample_group'].eq('oot')) & (monotonicity['rate_col'].eq('1m30p_cnt_bad_rate')), 'inversion_cnt'].iloc[0])}"},
            {"section": "头尾拉开", "metric": "Train 头部（A 档整体 → A1 子箱）3M30+ / A1 Lift", "value": f"{float(train_head['mlt_tier_3m30p'])*100:.2f}% → {float(train_head['best_sub_3m30p'])*100:.2f}% / Lift {float(train_head['best_sub_lift']):.4f}"},
            {"section": "头尾拉开", "metric": "Train 尾部（G 档 → G3 子箱）3M30+ / Lift", "value": f"{float(train_tail['mlt_tier_3m30p'])*100:.2f}% → {float(train_tail['worst_sub_3m30p'])*100:.2f}% / Lift {float(train_tail['worst_sub_lift']):.4f}"},
            {"section": "头尾拉开", "metric": "OOT 头部 A1 / 尾部 G3 3M30+", "value": f"{float(oot_head['best_sub_3m30p'])*100:.2f}% / {float(oot_tail['worst_sub_3m30p'])*100:.2f}%"},
            {"section": "区分度", "metric": "Train 3M30+ IV（mlt 7 档 / 组合 21 格）", "value": f"{iv_cmp.loc[iv_cmp['scheme'].eq('mlt 7 档'), 'train_3m30p_iv'].iloc[0]:.4f} / {iv_cmp.loc[iv_cmp['scheme'].eq('组合 21 格'), 'train_3m30p_iv'].iloc[0]:.4f}"},
            {"section": "区分度", "metric": "Train 3M30+ 序数 AUC（mlt 7 档 / 组合 21 格）", "value": f"{disc.loc[(disc['scheme'].eq('mlt 7 档')) & (disc['label'].eq('3M30+')) & (disc['sample_group'].eq('train')), 'auc'].iloc[0]:.4f} / {disc.loc[(disc['scheme'].eq('组合 21 格')) & (disc['label'].eq('3M30+')) & (disc['sample_group'].eq('train')), 'auc'].iloc[0]:.4f}"},
            {"section": "区分度", "metric": "OOT 3M30+ 序数 AUC（mlt 7 档 / 组合 21 格）", "value": f"{disc.loc[(disc['scheme'].eq('mlt 7 档')) & (disc['label'].eq('3M30+')) & (disc['sample_group'].eq('oot')), 'auc'].iloc[0]:.4f} / {disc.loc[(disc['scheme'].eq('组合 21 格')) & (disc['label'].eq('3M30+')) & (disc['sample_group'].eq('oot')), 'auc'].iloc[0]:.4f}"},
        ]
    )
    appendix = pd.DataFrame(
        [
            {"config_group": "基础配置", "config_name": "SUB_BIN_COUNT", "config_value": SUB_BIN_COUNT},
            {"config_group": "基础配置", "config_name": "ANCHOR_DUE_COL", "config_value": ANCHOR_DUE_COL},
            {"config_group": "基础配置", "config_name": "MIN_CELL_N_FOR_RATE", "config_value": MIN_CELL_N_FOR_RATE},
            {"config_group": "口径", "config_name": "组合标签", "config_value": "mlt 档字母 + 档内价值子箱序号（A1–G3）"},
            {"config_group": "口径", "config_name": "组合序", "config_value": "(mlt 档位 − 1) × 3 + 子箱序号，1–21 从低风险到高风险"},
            {"config_group": "口径", "config_name": "IV / AUC 口径", "config_value": "IV 按 3M30+ 好坏样本量（0.5 平滑）；AUC/KS 按序数值（秩和法）"},
        ]
    )
    print("[6/6] 总览与附录完成")

    wb = None
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    write_sheet(wb, "01_总览", [("价值模型条件分箱总览", None), (None, overview)])
    write_sheet(wb, "02_条件分箱边界", [("每个 mlt 档内价值分子箱边界（Train 学习，OOT 复用）", None), (None, edge_table)])
    write_sheet(wb, "03_组合格统计_Train", [("Train 组合格统计（A1–G3，按 combined_order 排序）", None), (None, train_stats)])
    write_sheet(wb, "04_组合格统计_OOT", [("OOT 组合格统计", None), (None, oot_stats)])
    write_sheet(wb, "05_档内子箱与头尾评估", [
        ("档内子箱风险与相邻显著性（Train）", None),
        (None, tier_summary),
        ("头尾拉开评估", None),
        (None, head_tail),
    ])
    write_sheet(wb, "06_区分度对比", [
        ("序数 AUC / KS 对比", None),
        (None, disc),
        ("3M30+ IV 对比", None),
        (None, iv_cmp),
        ("组合格单调性", None),
        (None, monotonicity),
        ("现行策略分段内的价值子箱再分层（应用示意）", None),
        (None, seg_relay),
    ])
    write_sheet(wb, "07_二维策略模拟", [
        ("策略对照（mlt 档上限 × 条件子箱上限）", None),
        (None, strategy_table),
        ("AND 接纳网格（mlt 档上限 × 子箱上限）", None),
        (None, strategy_grid),
    ])
    write_sheet(wb, "08_附录", [("配置参数", None), (None, appendix)])
    wb.save(REPORT_PATH)
    print(f"完成 => {REPORT_PATH}（耗时 {time.time() - t0:.1f}s）")


if __name__ == "__main__":
    main()
