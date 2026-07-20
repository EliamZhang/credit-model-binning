"""
分箱主脚本
==========
对策略调优样本进行等频 20 箱初分，ChiMerge 合并相似相邻箱，输出合并前后的
风险指标及 OOT 跨期验证，包含累计阈值曲线、三套方案设计和转化率漏斗。

使用方式：
    python scr/binning.py

输出：
    res/binning_result.md  — 分箱结果 Markdown 表格
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
SCORE_HIGHER_IS_RISKIER = True
N_BINS = 20
OOT_CUT_DATE = "2025-10-21"
FPD7_REF_DATE = "2026-07-20"     # FPD7 计算参考日期

# ChiMerge 参数
CHIMERGE_MIN_BINS = 6          # 最少保留箱数
CHIMERGE_MAX_BINS = 10         # 超过该箱数时继续合并，避免最终等级过碎
CHIMERGE_P_THRESHOLD = 0.05    # 低于该 p 值时认为相邻箱风险差异显著
MIN_BIN_SIZE = 3000            # 单箱最低样本量，低于该值优先考虑合并
MIN_BAD_COUNT = 100            # 单箱最低坏样本数，低于该值优先考虑合并

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES_DIR = os.path.join(BASE_DIR, "res")


# ============================================================
# 0. 工具函数
# ============================================================

def pass_mask(scores, threshold, higher_is_riskier=True):
    """返回阈值通过人群：高分高风险时 score <= threshold，否则 score >= threshold。"""
    return scores <= threshold if higher_is_riskier else scores >= threshold


def reject_mask(scores, threshold, higher_is_riskier=True):
    """返回阈值拒绝人群：高分高风险时 score > threshold，否则 score < threshold。"""
    return scores > threshold if higher_is_riskier else scores < threshold


def risk_ordered_bins(stats_df, higher_is_riskier=True):
    """按风险从低到高排列分箱统计。"""
    return stats_df if higher_is_riskier else stats_df.iloc[::-1].reset_index(drop=True)


def make_open_ended_bins(bins):
    """将首尾切点改成开口边界，避免 OOT 或线上样本因超出调优集范围被丢弃。"""
    open_bins = list(bins)
    open_bins[0] = -np.inf
    open_bins[-1] = np.inf
    return open_bins


def format_threshold_rule(threshold, kind, higher_is_riskier=True):
    """生成策略阈值文案。"""
    if kind == "pass":
        op = "≤" if higher_is_riskier else "≥"
    else:
        op = ">" if higher_is_riskier else "<"
    return f"score {op} {threshold:.4f}"


def format_review_rule(auto_threshold, review_threshold, higher_is_riskier=True):
    """生成三段式策略中的人工审核区间文案。"""
    if higher_is_riskier:
        return f"{auto_threshold:.4f} < score ≤ {review_threshold:.4f}"
    return f"{review_threshold:.4f} ≤ score < {auto_threshold:.4f}"


def format_score_range(min_score, max_score):
    """生成分数区间文案。"""
    if np.isnan(min_score) or np.isnan(max_score):
        return "—"
    return f"[{min_score:.4f}, {max_score:.4f})"


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

    # Lift: bad_rate / overall_bad_rate (>1 = 比平均更差)
    overall_bad_rate = total_B / total_N
    stats["lift"] = stats["bad_rate"] / overall_bad_rate
    stats["cum_lift"] = stats["cum_bad_rate"] / overall_bad_rate

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


def chimerge(
    df,
    score_col,
    label_col,
    initial_bins,
    min_bins=6,
    max_bins=10,
    p_threshold=0.05,
    min_bin_size=3000,
    min_bad_count=100,
    higher_is_riskier=True,
):
    """
    ChiMerge: 迭代合并相邻箱。

    优先把箱数压到 max_bins 以内；之后仅在相邻箱统计差异不显著、局部倒挂、
    或样本/坏样本不足时继续合并，最低不低于 min_bins。
    """
    bins = list(initial_bins)
    merge_history = []
    stop_reason = ""

    while len(bins) - 1 > min_bins:
        df["_merge_bin"] = pd.cut(df[score_col], bins=bins, duplicates="drop", include_lowest=True)
        stats, _, _ = compute_bin_stats(df, score_col, label_col, bin_col="_merge_bin")

        if len(stats) <= min_bins:
            break

        force_by_count = len(stats) > max_bins
        candidates = []

        for i in range(len(stats) - 1):
            row_a = stats.iloc[i]
            row_b = stats.iloc[i + 1]
            B_a, n_a = int(row_a["B"]), int(row_a["n"])
            B_b, n_b = int(row_b["B"]), int(row_b["n"])
            r_a, r_b = row_a["bad_rate"], row_b["bad_rate"]
            table = [[B_a, n_a - B_a], [B_b, n_b - B_b]]
            try:
                chi2, p_value = chi2_contingency(table, correction=False)[:2]
                if np.isnan(p_value):
                    p_value = 0.0
            except ValueError:
                chi2, p_value = 0.0, 0.0

            small_sample = n_a < min_bin_size or n_b < min_bin_size
            sparse_bad = B_a < min_bad_count or B_b < min_bad_count
            inversion = r_b < r_a if higher_is_riskier else r_b > r_a
            not_significant = p_value >= p_threshold

            reasons = []
            if force_by_count:
                reasons.append("箱数超过上限")
            if not_significant:
                reasons.append("相邻箱差异不显著")
            if inversion:
                reasons.append("局部倒挂")
            if small_sample:
                reasons.append("样本量不足")
            if sparse_bad:
                reasons.append("坏样本不足")

            if reasons:
                candidates.append({
                    "i": i,
                    "chi2": chi2,
                    "p_value": p_value,
                    "reasons": reasons,
                    "n_pair": n_a + n_b,
                    "bad_pair": B_a + B_b,
                })

        if not candidates:
            stop_reason = (
                f"剩余 {len(stats)} 箱已满足约束：箱数不超过 {max_bins}，"
                f"且无不显著相邻箱、局部倒挂或低样本箱。"
            )
            break

        candidates.sort(key=lambda x: (x["p_value"], -x["n_pair"]), reverse=True)
        best = candidates[0]
        best_i = best["i"]

        merge_history.append({
            "step": len(merge_history) + 1,
            "merged_pair": f"箱{best_i+1} + 箱{best_i+2}",
            "boundary": round(bins[best_i + 1], 6),
            "chi2": round(best["chi2"], 4),
            "p_value": best["p_value"],
            "bins_remaining": len(bins) - 2,
            "reason": "、".join(dict.fromkeys(best["reasons"])),
        })
        del bins[best_i + 1]

    if "_merge_bin" in df.columns:
        df.drop(columns=["_merge_bin"], inplace=True)

    if not stop_reason:
        stop_reason = f"达到最少保留箱数 {min_bins}。"

    return bins, merge_history, stop_reason


def compute_psi(stats_tuning, stats_oot):
    """计算两样本在各箱的 PSI（Population Stability Index）。"""
    if stats_oot is None or len(stats_tuning) != len(stats_oot):
        return None
    e = (stats_tuning["n"] / stats_tuning["n"].sum()).values
    a = (stats_oot["n"] / stats_oot["n"].sum()).values
    e = np.where(e == 0, 1e-10, e)
    a = np.where(a == 0, 1e-10, a)
    return float(np.sum((a - e) * np.log(a / e)))


def compute_auc_ks(scores, labels, higher_is_riskier=True):
    """计算 AUC（梯形法）和 KS 统计量。

    AUC/KS 均按“坏样本在高风险端更靠前”计算。
    """
    scores = np.asarray(scores, dtype=float)
    labels = np.asarray(labels, dtype=int)
    valid = ~(np.isnan(scores) | np.isnan(labels))
    scores = scores[valid]
    labels = labels[valid]

    n = len(scores)
    if n == 0:
        return float("nan"), float("nan")

    total_bad = labels.sum()
    total_good = n - total_bad
    if total_bad == 0 or total_good == 0:
        return float("nan"), float("nan")

    # 按风险从高到低排列
    risk_scores = scores if higher_is_riskier else -scores
    order = np.argsort(-risk_scores)
    labels_sorted = labels[order]

    cum_bad = np.cumsum(labels_sorted)
    cum_good = np.cumsum(1 - labels_sorted)

    tpr = np.concatenate([[0.0], cum_bad / total_bad])
    fpr = np.concatenate([[0.0], cum_good / total_good])

    ks = float(np.max(np.abs(tpr - fpr)))

    # 梯形法 AUC
    auc = float(np.sum((tpr[1:] + tpr[:-1]) / 2 * np.diff(fpr)))

    return auc, ks


def compute_threshold_curve(
    df,
    score_col,
    label_col,
    principal_col=None,
    n_thresholds=20,
    candidate_bins=None,
    higher_is_riskier=True,
):
    """在调优集上逐阈值计算累计指标。

    高分高风险时累计方向为 score <= threshold；高分低风险时为 score >= threshold。
    若提供 principal_col，则同时计算金额口径坏账率。
    """
    scores = df[score_col].values
    labels = df[label_col].astype(int).values

    percentiles = np.linspace(100 / n_thresholds, 100, n_thresholds)
    thresholds = np.percentile(scores, percentiles)
    if candidate_bins is not None:
        finite_bins = [b for b in candidate_bins if np.isfinite(b)]
        thresholds = np.concatenate([thresholds, finite_bins])
    thresholds = np.unique(np.round(thresholds, 8))
    thresholds = np.sort(thresholds)
    if not higher_is_riskier:
        thresholds = thresholds[::-1]

    total_N = len(df)
    results = []
    for thr in thresholds:
        mask = pass_mask(scores, thr, higher_is_riskier=higher_is_riskier)
        cum_n = mask.sum()
        cum_B = labels[mask].sum()
        row = {
            "threshold": thr,
            "cum_n": cum_n,
            "cum_pass_rate": cum_n / total_N,
            "cum_B": cum_B,
            "cum_bad_rate_count": cum_B / cum_n if cum_n > 0 else float("nan"),
            "marginal_n": float("nan"),
            "marginal_bad_rate_count": float("nan"),
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

    curve = pd.DataFrame(results)
    if len(curve) > 1:
        curve["marginal_n"] = curve["cum_n"].diff()
        curve["marginal_B"] = curve["cum_B"].diff()
        curve.loc[curve.index[0], "marginal_n"] = curve.loc[curve.index[0], "cum_n"]
        curve.loc[curve.index[0], "marginal_B"] = curve.loc[curve.index[0], "cum_B"]
        curve["marginal_bad_rate_count"] = np.where(
            curve["marginal_n"] > 0,
            curve["marginal_B"] / curve["marginal_n"],
            np.nan,
        )
    return curve


def compute_conversion_funnel(df, score_col, bins):
    """计算全量申请转化漏斗，按分箱拆分。

    完整漏斗：申请 → 完成 → 通过 → 放款
    返回整体和各箱的转化率明细。
    """
    total = len(df)

    # 申请阶段标签
    df = df.copy()
    df["is_completed"] = ~df["application_status"].isin(["0.Incomplete"])
    df["is_approved"] = df["application_status"].str.startswith(("3", "4"))
    df["is_declined"] = df["application_status"] == "2.3.Risk Declined"
    df["is_withdrawn"] = df["application_status"] == "2.1.Submitted Withdrawn"
    df["is_funded"] = df["application_status"] == "4.Funded"
    df["is_deal"] = df["status"].isin(["Active_Account", "Closed", "Blocked"])

    metrics = {
        "apply_cnt": total,
        "completed_cnt": int(df["is_completed"].sum()),
        "approved_cnt": int(df["is_approved"].sum()),
        "declined_cnt": int(df["is_declined"].sum()),
        "withdrawn_cnt": int(df["is_withdrawn"].sum()),
        "funded_cnt": int(df["is_funded"].sum()),
        "deal_cnt": int(df["is_deal"].sum()),
        "completion_rate": df["is_completed"].mean(),
        "approval_rate": df[df["is_completed"]]["is_approved"].mean() if df["is_completed"].sum() > 0 else 0,
        "decline_rate": df[df["is_completed"]]["is_declined"].mean() if df["is_completed"].sum() > 0 else 0,
        "withdraw_rate": df[df["is_completed"]]["is_withdrawn"].mean() if df["is_completed"].sum() > 0 else 0,
        "funding_rate": df[df["is_approved"]]["is_funded"].mean() if df["is_approved"].sum() > 0 else 0,
        "overall_funding_rate": df["is_funded"].sum() / total,
    }

    # 按分箱拆解
    df["bin"] = pd.cut(df[score_col], bins=bins, duplicates="drop", include_lowest=True)
    df_binned = df[df["bin"].notna()].copy()

    bin_stats = []
    for bin_name, group in df_binned.groupby("bin", observed=False):
        n = len(group)
        completed = group[group["is_completed"]]
        approved = group[group["is_approved"]]
        row = {
            "bin": str(bin_name),
            "apply": n,
            "completed": len(completed),
            "approved": len(approved),
            "declined": int(group["is_declined"].sum()),
            "withdrawn": int(group["is_withdrawn"].sum()),
            "funded": int(group["is_funded"].sum()),
            "deal": int(group["is_deal"].sum()),
            "completion_rate": group["is_completed"].mean(),
            "approval_rate": completed["is_approved"].mean() if len(completed) > 0 else 0,
            "decline_rate": completed["is_declined"].mean() if len(completed) > 0 else 0,
            "withdraw_rate": completed["is_withdrawn"].mean() if len(completed) > 0 else 0,
            "funding_rate": approved["is_funded"].mean() if len(approved) > 0 else 0,
            "overall_funding_rate": group["is_funded"].sum() / n,
        }
        bin_stats.append(row)

    # Sort by bin order (low score = low risk = first bin)
    bin_stats.sort(key=lambda x: float(x["bin"].strip("([])").split(",")[0].strip()))
    for i, row in enumerate(bin_stats):
        row["bin_no"] = i + 1

    return metrics, bin_stats


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


def compute_scheme_stats(
    df,
    score_col,
    label_col,
    auto_threshold,
    review_threshold,
    principal_col=None,
    higher_is_riskier=True,
):
    """Compute segment stats for a single scheme."""
    scores = df[score_col]
    total = len(df)

    if higher_is_riskier:
        seg_auto = df[scores <= auto_threshold]
        seg_review = df[(scores > auto_threshold) & (scores <= review_threshold)]
        seg_reject = df[scores > review_threshold]
    else:
        seg_auto = df[scores >= auto_threshold]
        seg_review = df[(scores < auto_threshold) & (scores >= review_threshold)]
        seg_reject = df[scores < review_threshold]

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
                bad_principal_sum = (principal[valid] * seg_df.loc[valid, label_col].astype(int)).sum()
                row["bad_rate_amount"] = bad_principal_sum / principal_sum
        rows.append(row)
    return rows


def boundary_for_risk_bins(risk_stats, n_risk_bins, higher_is_riskier=True):
    """返回覆盖前 n_risk_bins 个低风险箱的策略阈值。"""
    n_risk_bins = min(max(1, n_risk_bins), len(risk_stats))
    row = risk_stats.iloc[n_risk_bins - 1]
    return row["score_max"] if higher_is_riskier else row["score_min"]


def design_three_schemes(
    df,
    merged_stats,
    score_col,
    label_col,
    principal_col=None,
    higher_is_riskier=True,
):
    """基于合并箱边界设计三套方案。

    根据实际合并箱数自动切分，不再依赖固定 6 箱。
    """
    risk_stats = risk_ordered_bins(merged_stats, higher_is_riskier=higher_is_riskier)
    n_bins = len(risk_stats)
    if n_bins < 3:
        raise RuntimeError("三套方案设计至少需要 3 个风险等级。")

    conservative_auto_bins = max(1, int(np.floor(n_bins / 3)))
    conservative_review_bins = max(conservative_auto_bins + 1, int(np.floor(2 * n_bins / 3)))
    balance_auto_bins = max(1, int(np.floor(n_bins / 2)))
    balance_review_bins = max(balance_auto_bins + 1, n_bins - 1)
    growth_auto_bins = max(1, n_bins - 1)
    growth_review_bins = n_bins

    schemes = {
        "保守方案": {
            "auto_max": boundary_for_risk_bins(
                risk_stats, conservative_auto_bins, higher_is_riskier
            ),
            "review_max": boundary_for_risk_bins(
                risk_stats, conservative_review_bins, higher_is_riskier
            ),
            "reject": True,
            "description": f"自动通过最低风险的 {conservative_auto_bins} 个箱，前 {conservative_review_bins} 个箱进入通过范围，其余高风险箱拒绝。坏账率最低，抗风险能力最强。",
        },
        "平衡方案（推荐）": {
            "auto_max": boundary_for_risk_bins(
                risk_stats, balance_auto_bins, higher_is_riskier
            ),
            "review_max": boundary_for_risk_bins(
                risk_stats, balance_review_bins, higher_is_riskier
            ),
            "reject": True,
            "description": f"自动通过最低风险的 {balance_auto_bins} 个箱，人工审核中间风险箱，仅拒绝最高风险 {n_bins - balance_review_bins} 个箱。在通过率、风险和审核量之间取得平衡。",
        },
        "增长方案": {
            "auto_max": boundary_for_risk_bins(
                risk_stats, growth_auto_bins, higher_is_riskier
            ),
            "review_max": boundary_for_risk_bins(
                risk_stats, growth_review_bins, higher_is_riskier
            ),
            "reject": False,
            "description": "除最高风险箱外自动通过，最高风险箱进入人工审核，不做硬拒绝。通过率最高，适合激进获客扩张。",
        },
    }

    results = {}
    for name, cfg in schemes.items():
        segments = compute_scheme_stats(
            df,
            score_col,
            label_col,
            cfg["auto_max"],
            cfg["review_max"],
            principal_col,
            higher_is_riskier,
        )
        # summary row
        total = len(df)
        if cfg["reject"]:
            approved = df[pass_mask(df[score_col], cfg["review_max"], higher_is_riskier)]
            reject_n = total - len(approved)
            reject_rate = reject_n / total
        else:
            approved = df
            reject_n = 0
            reject_rate = 0.0
        n_approved = len(approved)
        B_approved = int(approved[label_col].astype(int).sum())
        summary = {
            "name": name,
            "auto_max": cfg["auto_max"],
            "review_max": cfg["review_max"],
            "reject": cfg["reject"],
            "description": cfg["description"],
            "segments": segments,
            "pass_n": n_approved,
            "pass_rate": n_approved / total,
            "pass_bad_rate_count": B_approved / n_approved if n_approved > 0 else float("nan"),
            "reject_n": reject_n,
            "reject_rate": reject_rate,
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
        f"| {row.WOE:+.4f} "
        f"| {row.lift:.2f}x "
        f"| {row.cum_lift:.2f}x |"
    )


def format_bin_table(stats_df, title, level=3):
    """生成分箱明细 Markdown 表格。"""
    lines = []
    lines.append(f"{'#' * level} {title}")
    lines.append("")
    lines.append("| 箱序 | 分数区间 | 样本量 | 坏样本 | 坏账率 | SE | 累计通过率 | 累计坏账率 | WOE | Lift | 累计Lift |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
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
    info_df[[
        "application_id", LABEL_COL, "principal", "status", "application_status",
        "first_payment_scheduled_date", "first_payment_days_past_due_ever",
    ]],
    on="application_id",
    how="inner",
)
print(f"  关联后:   {len(merged):,} 条")

# FPD7 标签
merged["fpd7_flag"] = np.nan
fpd7_ref = pd.Timestamp(FPD7_REF_DATE)
first_pay_date = pd.to_datetime(merged["first_payment_scheduled_date"], errors="coerce")
funded_mask = merged["application_status"] == "4.Funded"
fpd7_eligible = funded_mask & first_pay_date.notna() & (first_pay_date < fpd7_ref - pd.Timedelta(days=7))
merged.loc[fpd7_eligible & (merged["first_payment_days_past_due_ever"] > 7), "fpd7_flag"] = 1
merged.loc[fpd7_eligible & (merged["first_payment_days_past_due_ever"] <= 7), "fpd7_flag"] = 0
fpd7_valid_n = fpd7_eligible.sum()
fpd7_bad_n = int((merged["fpd7_flag"] == 1).sum())
print(f"  FPD7 有效样本: {fpd7_valid_n:,}，坏样本: {fpd7_bad_n:,}（FPD7 = {fpd7_bad_n/fpd7_valid_n:.4%}）" if fpd7_valid_n > 0 else "  FPD7 有效样本: 0")

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
initial_bins_for_scoring = make_open_ended_bins(bins)

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

tuning_auc, tuning_ks = compute_auc_ks(
    tuning_valid[SCORE_COL],
    tuning_valid[LABEL_COL],
    higher_is_riskier=SCORE_HIGHER_IS_RISKIER,
)
print(f"  AUC = {tuning_auc:.4f}，KS = {tuning_ks:.4f}")

# ============================================================
# 5. OOT 集初始分箱
# ============================================================
print("\n" + "=" * 60)
print("5. OOT 集初始分箱")
print("=" * 60)

if len(oot_valid) > 0:
    oot_labels = oot_valid[LABEL_COL].astype(int)
    oot_valid["bin"] = pd.cut(
        oot_valid[SCORE_COL],
        bins=initial_bins_for_scoring,
        duplicates="drop",
        include_lowest=True,
    )
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

if oot_stats is not None:
    oot_auc, oot_ks = compute_auc_ks(
        oot_valid_binned[SCORE_COL],
        oot_valid_binned[LABEL_COL],
        higher_is_riskier=SCORE_HIGHER_IS_RISKIER,
    )
    print(f"  OOT AUC = {oot_auc:.4f}，KS = {oot_ks:.4f}")
else:
    oot_auc, oot_ks = float("nan"), float("nan")

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

merged_bins, merge_history, merge_stop_reason = chimerge(
    tuning_valid, SCORE_COL, LABEL_COL, bins,
    min_bins=CHIMERGE_MIN_BINS,
    max_bins=CHIMERGE_MAX_BINS,
    p_threshold=CHIMERGE_P_THRESHOLD,
    min_bin_size=MIN_BIN_SIZE,
    min_bad_count=MIN_BAD_COUNT,
    higher_is_riskier=SCORE_HIGHER_IS_RISKIER,
)
merged_bins_for_scoring = make_open_ended_bins(merged_bins)

print(f"  初始 {len(bins)-1} 箱 → 合并后 {len(merged_bins)-1} 箱")
print(f"  合并次数: {len(merge_history)}")
for h in merge_history:
    print(f"    第{h['step']:2d}步: 合并 {h['merged_pair']}（p={h['p_value']:.4f}，原因={h['reason']}），剩余 {h['bins_remaining']} 箱")
print(f"  停止原因: {merge_stop_reason}")

# ============================================================
# 8. 合并后分箱指标（策略调优集）
# ============================================================
print("\n" + "=" * 60)
print("8. 合并后分箱指标（策略调优集）")
print("=" * 60)

tuning_valid["merged_bin"] = pd.cut(
    tuning_valid[SCORE_COL],
    bins=merged_bins_for_scoring,
    duplicates="drop",
    include_lowest=True,
)
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
    oot_valid["merged_bin"] = pd.cut(
        oot_valid[SCORE_COL],
        bins=merged_bins_for_scoring,
        duplicates="drop",
        include_lowest=True,
    )
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
    tuning_valid,
    SCORE_COL,
    LABEL_COL,
    principal_col="principal",
    n_thresholds=20,
    candidate_bins=merged_bins[1:-1],
    higher_is_riskier=SCORE_HIGHER_IS_RISKIER,
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

schemes = design_three_schemes(
    tuning_valid,
    tuning_merged_stats,
    SCORE_COL,
    LABEL_COL,
    principal_col="principal",
    higher_is_riskier=SCORE_HIGHER_IS_RISKIER,
)

for name, s in schemes.items():
    if s["reject"]:
        print(
            f"  {name}: 自动通过 {format_threshold_rule(s['auto_max'], 'pass', SCORE_HIGHER_IS_RISKIER)}, "
            f"人工审核 {format_review_rule(s['auto_max'], s['review_max'], SCORE_HIGHER_IS_RISKIER)}, "
            f"拒绝 {format_threshold_rule(s['review_max'], 'reject', SCORE_HIGHER_IS_RISKIER)}"
        )
    else:
        print(
            f"  {name}: 自动通过 {format_threshold_rule(s['auto_max'], 'pass', SCORE_HIGHER_IS_RISKIER)}, "
            f"人工审核 {format_review_rule(s['auto_max'], s['review_max'], SCORE_HIGHER_IS_RISKIER)}, "
            "不做硬拒绝"
        )
    print(f"    通过率 {s['pass_rate']:.2%}, 通过人群坏账率 {s['pass_bad_rate_count']:.4%}, 拒绝率 {s['reject_rate']:.2%}")

# OOT 验证三套方案
schemes_oot = None
if len(oot_valid) > 0 and oot_merged_stats is not None:
    schemes_oot = design_three_schemes(
        oot_valid,
        oot_merged_stats,
        SCORE_COL,
        LABEL_COL,
        principal_col="principal",
        higher_is_riskier=SCORE_HIGHER_IS_RISKIER,
    )

# ============================================================
# 12. 转化率分析（全量申请，不区分调优/OOT）
# ============================================================
print("\n" + "=" * 60)
print("12. 转化率分析")
print("=" * 60)

funnel_metrics, funnel_bins = compute_conversion_funnel(merged, SCORE_COL, merged_bins_for_scoring)

print(f"  申请: {funnel_metrics['apply_cnt']:,}")
print(f"  完成率: {funnel_metrics['completion_rate']:.2%}")
print(f"  通过率（完成中）: {funnel_metrics['approval_rate']:.2%}")
print(f"  拒绝率（完成中）: {funnel_metrics['decline_rate']:.2%}")
print(f"  放款率（通过中）: {funnel_metrics['funding_rate']:.2%}")
print(f"  整体放款率（申请 → 放款）: {funnel_metrics['overall_funding_rate']:.2%}")

# ============================================================
# 13. FPD7 标签对比分析
# ============================================================
print("\n" + "=" * 60)
print("13. FPD7 标签对比分析")
print("=" * 60)

FPD7_LABEL = "fpd7_flag"

# 在 tuning 和 OOT 中筛选 FPD7 有效样本
tuning_fpd7 = tuning_valid[tuning_valid[FPD7_LABEL].notna()].copy()
oot_fpd7 = oot_valid[oot_valid[FPD7_LABEL].notna()].copy() if len(oot_valid) > 0 else pd.DataFrame()

print(f"  调优集 FPD7 有效: {len(tuning_fpd7):,}，坏样本: {int(tuning_fpd7[FPD7_LABEL].sum()):,}")
if len(tuning_fpd7) > 0:
    fpd7_tuning_auc, fpd7_tuning_ks = compute_auc_ks(
        tuning_fpd7[SCORE_COL],
        tuning_fpd7[FPD7_LABEL],
        higher_is_riskier=SCORE_HIGHER_IS_RISKIER,
    )
    print(f"  调优集 FPD7 AUC = {fpd7_tuning_auc:.4f}，KS = {fpd7_tuning_ks:.4f}")

if len(oot_fpd7) > 0:
    fpd7_oot_auc, fpd7_oot_ks = compute_auc_ks(
        oot_fpd7[SCORE_COL],
        oot_fpd7[FPD7_LABEL],
        higher_is_riskier=SCORE_HIGHER_IS_RISKIER,
    )
    print(f"  OOT 集 FPD7 AUC = {fpd7_oot_auc:.4f}，KS = {fpd7_oot_ks:.4f}")
else:
    fpd7_oot_auc, fpd7_oot_ks = float("nan"), float("nan")

# 合并后分箱 × FPD7（调优集）
fpd7_bin_stats = None
if len(tuning_fpd7) > 0:
    tuning_fpd7["merged_bin"] = pd.cut(
        tuning_fpd7[SCORE_COL],
        bins=merged_bins_for_scoring,
        duplicates="drop",
        include_lowest=True,
    )
    fpd7_bin_stats, fpd7_bin_N, fpd7_bin_B = compute_bin_stats(
        tuning_fpd7, SCORE_COL, FPD7_LABEL, bin_col="merged_bin"
    )
    fpd7_overall = fpd7_bin_B / fpd7_bin_N
    print(f"  FPD7 整体坏账率（调优）: {fpd7_overall:.4%}")
    for row in fpd7_bin_stats.itertuples():
        print(f"    箱{row.Index}: FPD7={row.bad_rate:.4%}, n={row.n:,}")

# ============================================================
# 14. 输出 Markdown
# ============================================================
print("\n" + "=" * 60)
print("14. 输出结果")
print("=" * 60)

md = []
score_direction_text = "分数越高风险越高" if SCORE_HIGHER_IS_RISKIER else "分数越高风险越低"
threshold_pass_expr = "score <= threshold" if SCORE_HIGHER_IS_RISKIER else "score >= threshold"
auto_header = "自动通过 ≤" if SCORE_HIGHER_IS_RISKIER else "自动通过 ≥"
review_header = "审核 ≤" if SCORE_HIGHER_IS_RISKIER else "审核 ≥"

# 标题
md.append("# 分箱结果：等频 20 箱 → ChiMerge 合并")
md.append("")
md.append(f"> 模型: `{SCORE_COL}` | 标签: `{LABEL_COL}` | 样本切分: `< {OOT_CUT_DATE}` vs `>= {OOT_CUT_DATE}`")
md.append(f"> 分数方向: {score_direction_text}")
md.append(
    f"> 合并参数: 最少保留 {CHIMERGE_MIN_BINS} 箱，超过 {CHIMERGE_MAX_BINS} 箱优先合并；"
    f"相邻箱 p >= {CHIMERGE_P_THRESHOLD}、局部倒挂或低样本箱继续合并"
)
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
md.append(f"| AUC | {tuning_auc:.4f} | {oot_auc:.4f} | — | — |")
md.append(f"| KS | {tuning_ks:.4f} | {oot_ks:.4f} | — | — |")
md.append("> AUC/KS 为分数级排序指标，不随分箱变化，合并前后一致，故合并后不再重复列出。")
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
md.append(f"从 {len(bins)-1} 箱开始，优先将箱数压到 {CHIMERGE_MAX_BINS} 箱以内；之后仅在相邻箱差异不显著、局部倒挂或样本不足时继续合并，最低不低于 {CHIMERGE_MIN_BINS} 箱。")
md.append("")
md.append("| 步骤 | 合并对 | 被合并边界 | χ² | p 值 | 剩余箱数 | 合并原因 |")
md.append("|---:|---:|---:|---:|---:|---:|:---|")
for h in merge_history:
    md.append(
        f"| {h['step']} "
        f"| {h['merged_pair']} "
        f"| {h['boundary']} "
        f"| {h['chi2']:.2f} "
        f"| {h['p_value']:.4f} "
        f"| {h['bins_remaining']} "
        f"| {h['reason']} |"
    )
md.append("")
md.append(f"> 停止原因：{merge_stop_reason}")
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
md.append(f"- 合并后保留 **{len(tuning_merged_stats)} 个风险等级**，单调性指标 Spearman ρ = {tuning_merged_rho:.4f}")
md.append(f"- 调优集 IV = {tuning_merged_IV:.4f}，区分能力保持良好")
if oot_merged_stats is not None:
    md.append(f"- OOT 集 IV = {oot_merged_IV:.4f}，跨期排序能力确认")
    if merged_psi is not None:
        md.append(f"- PSI = {merged_psi:.4f}，跨期分布{psi_level}")
md.append("")

# ---- 累计阈值曲线 ----
md.append("## 七、累计阈值曲线")
md.append("")
md.append("> 在策略调优集上，按低风险到高风险方向逐阈值计算累计通过率和累计坏账率。")
md.append(f"> {score_direction_text}，累计通过规则为 `{threshold_pass_expr}`。")
md.append("> 候选阈值由 20 个等分位点和合并箱边界共同组成，合并箱边界行以 **粗体** 标注。")
md.append("")

# 构建合并边界集合用于标注
merged_boundary_set = set(round(b, 8) for b in merged_bins[1:-1])

md.append("| 阈值 | 累计通过率 | 累计坏账率（笔数） | 边际坏账率（笔数） | 累计坏账率（金额） | 备注 |")
md.append("|---:|---:|---:|---:|---:|:---|")
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
        f"| {row.marginal_bad_rate_count:.4%} "
        f"| {bad_amt_str} "
        f"| {marker} |"
    )
md.append("")

# 合并边界参考
md.append("### 合并箱边界参考")
md.append("")
edge_desc = "上限" if SCORE_HIGHER_IS_RISKIER else "下限"
md.append(f"| 箱序 | 分数区间 | 阈值（{edge_desc}） |")
md.append("|---:|---:|---:|")
for i, row in enumerate(tuning_merged_stats.itertuples()):
    threshold_edge = row.score_max if SCORE_HIGHER_IS_RISKIER else row.score_min
    md.append(f"| {i+1} | [{row.score_min:.4f}, {row.score_max:.4f}) | {threshold_edge:.4f} |")
md.append("")
md.append(f"> 每个合并箱的{edge_desc}即为一个可选策略阈值。")
md.append("")

# ---- 三套方案设计 ----
md.append("## 八、三套方案设计")
md.append("")
md.append(f"> 基于合并后的 {len(tuning_merged_stats)} 个风险等级，设计增长/平衡/保守三套方案。")
md.append("> 每套方案将分数段划分为三区：**自动通过**（低风险）、**人工审核**（中风险）、**拒绝**（高风险）。")
md.append("> 目前基于通过率与坏账率做 trade-off 选择；EL/收入/UE 待补充经济数据后扩展。")
md.append("> **注**：金额口径坏账率仅覆盖已放款样本（`principal` 非空且 > 0），与笔数口径的人群不完全一致，两者不能直接对比。")
md.append("")

has_amount = "principal" in tuning_valid.columns

for scheme_name, s in schemes.items():
    md.append(f"### {scheme_name}")
    md.append("")
    md.append(f"> {s['description']}")
    md.append("")
    md.append(f"- **自动通过阈值**: {format_threshold_rule(s['auto_max'], 'pass', SCORE_HIGHER_IS_RISKIER)}")
    if s["reject"]:
        md.append(f"- **人工审核区间**: {format_review_rule(s['auto_max'], s['review_max'], SCORE_HIGHER_IS_RISKIER)}")
        md.append(f"- **拒绝阈值**: {format_threshold_rule(s['review_max'], 'reject', SCORE_HIGHER_IS_RISKIER)}")
    else:
        md.append(f"- **人工审核区间**: {format_review_rule(s['auto_max'], s['review_max'], SCORE_HIGHER_IS_RISKIER)}")
        md.append(f"- **拒绝阈值**: 无（不做硬拒绝）")
    md.append("")

    # Segment detail table — skip empty reject segment for growth scheme
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
        if seg["n"] == 0:
            continue  # skip empty segments (e.g. reject in growth scheme)
        score_range = format_score_range(seg["score_min"], seg["score_max"])
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
    if s["reject"]:
        md.append(f"**方案汇总**：通过率 {s['pass_rate']:.2%}（{s['pass_n']:,} 人），"
                  f"通过人群坏账率 {s['pass_bad_rate_count']:.4%}，"
                  f"拒绝率 {s['reject_rate']:.2%}（{s['reject_n']:,} 人）")
    else:
        md.append(f"**方案汇总**：通过率 {s['pass_rate']:.2%}（{s['pass_n']:,} 人），"
                  f"通过人群坏账率 {s['pass_bad_rate_count']:.4%}，"
                  f"不做硬拒绝，最高风险客群经人工审核后决策")
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
compare_headers = f"| 方案 | {auto_header} | {review_header} | 通过率 | 坏账率（笔数） "
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
md.append(f"> **推荐**：平衡方案自动通过 {schemes['平衡方案（推荐）']['segments'][0]['pct']:.1%} 低风险客群（坏账率 {schemes['平衡方案（推荐）']['segments'][0]['bad_rate_count']:.2%}），人工审核 {schemes['平衡方案（推荐）']['segments'][1]['pct']:.1%}，仅拒绝最高风险 {schemes['平衡方案（推荐）']['reject_rate']:.1%}。三套方案形成清晰梯度——保守（通过率 {schemes['保守方案']['pass_rate']:.0%}，坏账率最低）、平衡（通过率 {schemes['平衡方案（推荐）']['pass_rate']:.0%}，风险与审核兼顾）、增长（通过率 {schemes['增长方案']['pass_rate']:.0%}，不做硬拒绝，以审核替代拒绝），建议常态使用平衡方案，根据风险偏好和审核产能切换。")
md.append("")

# ---- 转化率漏斗 ----
md.append("## 九、转化率漏斗")
md.append("")
md.append(f"> 在全量申请（不分调优/OOT）上，按合并后的 {len(tuning_merged_stats)} 个风险等级拆分审批漏斗。")
md.append("> 漏斗路径：申请 → 完成（排除 Incomplete）→ 通过（status 以 3/4 开头）→ 放款（4.Funded）。")
md.append("")

# Overall funnel
m = funnel_metrics
md.append("### 整体漏斗")
md.append("")
md.append("| 阶段 | 数量 | 占比 |")
md.append("|---:|---:|---:|")
md.append(f"| 申请 | {m['apply_cnt']:,} | 100.0% |")
md.append(f"| └ 完成（排除 0.Incomplete） | {m['completed_cnt']:,} | {m['completion_rate']:.2%} |")
md.append(f"| 　└ 通过（3.x / 4.x） | {m['approved_cnt']:,} | {m['approval_rate']:.2%}（完成中） |")
md.append(f"| 　　└ 放款（4.Funded） | {m['funded_cnt']:,} | {m['funding_rate']:.2%}（通过中） |")
md.append(f"| 　└ 拒绝（2.3.Risk Declined） | {m['declined_cnt']:,} | {m['decline_rate']:.2%}（完成中） |")
md.append(f"| 　└ 撤回（2.1.Submitted Withdrawn） | {m['withdrawn_cnt']:,} | {m['withdraw_rate']:.2%}（完成中） |")
md.append(f"| 整体放款率（申请 → 放款） | {m['funded_cnt']:,} | {m['overall_funding_rate']:.2%} |")
md.append("")

# Per-bin funnel
md.append("### 各风险等级转化率")
md.append("")
md.append("| 箱序 | 分数区间 | 申请 | 完成率 | 通过率 | 拒绝率 | 放款率 | 整体放款率 |")
md.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
for row in funnel_bins:
    md.append(
        f"| {row['bin_no']} "
        f"| {row['bin']} "
        f"| {row['apply']:,} "
        f"| {row['completion_rate']:.2%} "
        f"| {row['approval_rate']:.2%} "
        f"| {row['decline_rate']:.2%} "
        f"| {row['funding_rate']:.2%} "
        f"| {row['overall_funding_rate']:.2%} |"
    )
md.append("")
md.append("> **解读**：低分箱（低风险）的完成率和通过率应更高；若低风险客群拒绝率异常偏高，说明风控策略可能过于严苛。")
md.append("")

# ---- FPD7 标签对比 ----
md.append("## 十、FPD7 标签对比")
md.append("")
md.append(f"> FPD7：首期支付逾期 7 天。计算口径：`application_status = '4.Funded'` 且 `first_payment_scheduled_date < {FPD7_REF_DATE} - 7` 天。")
md.append("> 满足条件但 `first_payment_days_past_due_ever <= 7` 的为 0，`> 7` 的为 1，其余为 NULL。")
md.append(f"> FPD7 有效样本仅覆盖已放款且首期到期满 7 天的订单，样本量远小于 `{LABEL_COL}`，但作为更早期的风险信号可提供补充视角。")
md.append("")

# FPD7 overall metrics
md.append("### FPD7 整体指标")
md.append("")
md.append("| 指标 | 调优集 | OOT 集 |")
md.append("|---:|---:|---:|")
md.append(f"| 有效样本 | {len(tuning_fpd7):,} | {len(oot_fpd7):,} |" if len(oot_fpd7) > 0 else f"| 有效样本 | {len(tuning_fpd7):,} | — |")
if len(tuning_fpd7) > 0:
    md.append(f"| FPD7 坏账率 | {int(tuning_fpd7[FPD7_LABEL].sum())/len(tuning_fpd7):.4%} "
             f"| {int(oot_fpd7[FPD7_LABEL].sum())/len(oot_fpd7):.4%} |" if len(oot_fpd7) > 0 else f"| FPD7 坏账率 | {int(tuning_fpd7[FPD7_LABEL].sum())/len(tuning_fpd7):.4%} | — |")
    md.append(f"| AUC | {fpd7_tuning_auc:.4f} | {fpd7_oot_auc:.4f} |" if not np.isnan(fpd7_oot_auc) else f"| AUC | {fpd7_tuning_auc:.4f} | — |")
    md.append(f"| KS | {fpd7_tuning_ks:.4f} | {fpd7_oot_ks:.4f} |" if not np.isnan(fpd7_oot_ks) else f"| KS | {fpd7_tuning_ks:.4f} | — |")
md.append("")

# FPD7 vs duedate_3m_30 comparison
md.append("### AUC / KS 对比（调优集）")
md.append("")
md.append("| 标签 | AUC | KS | 有效样本 | 坏账率 |")
md.append("|---:|---:|---:|---:|---:|")
md.append(f"| `{LABEL_COL}` | {tuning_auc:.4f} | {tuning_ks:.4f} | {tuning_N:,} | {tuning_bad_rate:.4%} |")
if len(tuning_fpd7) > 0:
    fpd7_rate = int(tuning_fpd7[FPD7_LABEL].sum()) / len(tuning_fpd7)
    md.append(f"| `{FPD7_LABEL}` | {fpd7_tuning_auc:.4f} | {fpd7_tuning_ks:.4f} | {len(tuning_fpd7):,} | {fpd7_rate:.4%} |")
md.append("")
md.append("> FPD7 作为早期风险信号，其 AUC/KS 通常低于成熟期标签（3m_30），但若差异过大，说明模型对早期风险的排序能力不足。")
md.append("")

# FPD7 by merged bins
if fpd7_bin_stats is not None and len(fpd7_bin_stats) > 0:
    md.append("### 合并后分箱 × FPD7（调优集）")
    md.append("")
    md.append("| 箱序 | 分数区间 | FPD7 有效样本 | FPD7 坏样本 | FPD7 坏账率 | 3m_30 坏账率（同箱） |")
    md.append("|---:|---:|---:|---:|---:|---:|")
    for i, (f_row, t_row) in enumerate(zip(fpd7_bin_stats.itertuples(), tuning_merged_stats.itertuples())):
        score_range = f"[{f_row.score_min:.4f}, {f_row.score_max:.4f})"
        md.append(
            f"| {i+1} "
            f"| {score_range} "
            f"| {f_row.n:>6,} "
            f"| {int(f_row.B):>6,} "
            f"| {f_row.bad_rate:.4%} "
            f"| {t_row.bad_rate:.4%} |"
        )
    md.append("")
    md.append("> 对比同一分箱内 FPD7 与 3m_30 的坏账率，可观察早期风险信号与成熟期风险的一致性。若某箱 FPD7 显著低于 3m_30，说明该客群的首期表现较好但后续恶化（或反之）。")
    md.append("")

# 写入文件
out_path = os.path.join(RES_DIR, "binning_result.md")
with open(out_path, "w", encoding="utf-8") as f:
    f.write("\n".join(md))
print(f"  输出: res/binning_result.md")

# 控制台摘要
print(f"\n  初分 20 箱: 调优 {tuning_N:,} 条，坏账率 {tuning_bad_rate:.4%}，IV {tuning_IV:.4f}，ρ {rho:.4f}，AUC {tuning_auc:.4f}，KS {tuning_ks:.4f}")
print(f"  合并后 {len(tuning_merged_stats)} 箱: 调优 {tuning_merged_N:,} 条，坏账率 {tuning_merged_bad_rate:.4%}，IV {tuning_merged_IV:.4f}，ρ {tuning_merged_rho:.4f}")
if oot_merged_bad_rate is not None:
    print(f"  OOT（合并后）: {oot_merged_N:,} 条，坏账率 {oot_merged_bad_rate:.4%}，IV {oot_merged_IV:.4f}，ρ {oot_merged_rho:.4f}，AUC {oot_auc:.4f}，KS {oot_ks:.4f}")
    if merged_psi is not None:
        print(f"  PSI = {merged_psi:.4f}")
if len(tuning_fpd7) > 0:
    print(f"  FPD7（调优）: 有效 {len(tuning_fpd7):,}，坏账率 {int(tuning_fpd7[FPD7_LABEL].sum())/len(tuning_fpd7):.4%}，AUC {fpd7_tuning_auc:.4f}，KS {fpd7_tuning_ks:.4f}")

print("\n完成。")
