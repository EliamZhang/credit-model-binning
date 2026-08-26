# -*- coding: utf-8 -*-
"""风险指标：1M30+/3M30+ 笔数与金额口径、Lift、IV、PSI、AUC/KS、单调性检查与箱级统计。

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


from pipeline.common import require_columns, safe_div, wilson_ci
from pipeline.data_loading import build_bin_actual_funnel_report



def add_risk_helper_columns(data: pd.DataFrame) -> pd.DataFrame:
    """生成分箱统计所需的成熟、逾期、敞口和逾期金额字段。"""
    work = data.copy()
    work["_principal"] = work["principal"].fillna(0)

    for config in RISK_HELPER_CONFIG.values():
        due_col = config["due_col"]
        dpd_col = config["dpd_col"]
        helper = config["helper_prefix"]

        mature = work[dpd_col].notna()
        work[f"{helper}_mature_cnt"] = work[due_col].isin([0, 1])
        work[f"{helper}_bad_cnt"] = work[due_col].eq(1)
        work[f"{helper}_amt_exposure"] = np.where(mature, work["_principal"], 0)
        work[f"{helper}_amt_bad"] = np.where(
            mature & work[dpd_col].ge(30),
            work[config["remaining_col"]].fillna(0),
            0,
        )

    return work
def add_bin_derived_metrics(
    stats: pd.DataFrame,
    order_col: str,
    include_total_n: bool = True,
) -> pd.DataFrame:
    """
    统一补充分箱派生指标。

    业务口径保持不变：
    - 笔数逾期率 = 逾期样本量 / 成熟样本量；
    - 金额逾期率 = 逾期剩余本金 / 成熟本金敞口；
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
    """补充箱级 IV 分项与累计 KS 曲线；整体 AUC/KS 仍在模型验证表展示。"""
    result = stats.sort_values("bin_order").reset_index(drop=True).copy()
    bin_count = len(result)
    if bin_count == 0:
        return result

    for prefix in ["1m30p", "3m30p"]:
        bad = pd.to_numeric(result[f"{prefix}_cnt_bad"], errors="coerce").fillna(0.0)
        good = pd.to_numeric(result[f"{prefix}_cnt_good"], errors="coerce").fillna(0.0)
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

        # 高风险端向低风险端累计，与整体 KS 的风险排序方向一致。
        cum_bad_from_high = bad.iloc[::-1].cumsum().iloc[::-1]
        cum_good_from_high = good.iloc[::-1].cumsum().iloc[::-1]
        result[f"{prefix}_ks_curve"] = (
            safe_div(cum_bad_from_high, bad.sum())
            - safe_div(cum_good_from_high, good.sum())
        ).abs()

    return result
def add_bin_lift(stats: pd.DataFrame) -> pd.DataFrame:
    """补充箱级 Lift：某箱逾期率 ÷ 该样本组整体逾期率（整体为各箱汇总）。"""
    result = stats.sort_values("bin_order").reset_index(drop=True).copy()
    if len(result) == 0:
        return result
    lift_denoms = [
        ("1m30p_cnt", "1m30p_cnt_mature"),
        ("1m30p_amt", "1m30p_amt_exposure"),
        ("3m30p_cnt", "3m30p_cnt_mature"),
        ("3m30p_amt", "3m30p_amt_exposure"),
    ]
    for prefix, denom_col in lift_denoms:
        bad = pd.to_numeric(result[f"{prefix}_bad"], errors="coerce").fillna(0.0)
        denom = pd.to_numeric(result[denom_col], errors="coerce").fillna(0.0)
        overall_rate = bad.sum() / denom.sum()
        result[f"{prefix}_lift"] = safe_div(bad / denom, overall_rate)
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
def check_monotonicity(
    stats: pd.DataFrame,
    rate_cols: Sequence[str],
    sample_group: str,
) -> pd.DataFrame:
    """检查风险率是否随风险等级非递减。"""
    ordered = stats.sort_values("bin_order").reset_index(drop=True)
    rows = []

    for rate_col in rate_cols:
        diff = ordered[rate_col].diff()
        violation = diff.lt(0).fillna(False)
        rows.append(
            {
                "sample_group": sample_group,
                "metric": rate_col,
                "is_monotonic_non_decreasing": not bool(violation.any()),
                "violation_cnt": int(violation.sum()),
                "violation_bins": ",".join(
                    ordered.loc[violation, "bin_order"].astype(str).tolist()
                ),
            }
        )
    return pd.DataFrame(rows)
def calc_iv_from_stats(
    stats: pd.DataFrame,
    bad_col: str = PRIMARY_BAD_COL,
    good_col: str = PRIMARY_GOOD_COL,
    eps: float = IV_SMOOTHING_EPS,
) -> float:
    """使用箱级好坏样本量计算 IV。"""
    bad = pd.to_numeric(stats[bad_col], errors="coerce").fillna(0).to_numpy(float)
    good = pd.to_numeric(stats[good_col], errors="coerce").fillna(0).to_numpy(float)
    if bad.sum() <= 0 or good.sum() <= 0:
        return np.nan

    bad_dist = (bad + eps) / (bad.sum() + eps * len(bad))
    good_dist = (good + eps) / (good.sum() + eps * len(good))
    return float(np.sum((bad_dist - good_dist) * np.log(bad_dist / good_dist)))
def two_proportion_pvalue(
    bad_1: float,
    mature_1: float,
    bad_2: float,
    mature_2: float,
) -> float:
    """不依赖 scipy 的双侧两比例 Z 检验 p 值。"""
    if mature_1 <= 0 or mature_2 <= 0:
        return np.nan

    p1 = bad_1 / mature_1
    p2 = bad_2 / mature_2
    pooled = (bad_1 + bad_2) / (mature_1 + mature_2)
    variance = pooled * (1 - pooled) * (1 / mature_1 + 1 / mature_2)
    if variance <= 0:
        return 1.0 if math.isclose(p1, p2) else 0.0

    z_value = abs(p1 - p2) / math.sqrt(variance)
    normal_cdf = 0.5 * (1 + math.erf(z_value / math.sqrt(2)))
    return float(2 * (1 - normal_cdf))
def calc_population_psi(
    train: pd.DataFrame,
    oot: pd.DataFrame,
    bin_col: str,
    final_edges: pd.DataFrame,
    eps: float = PSI_EPS,
) -> pd.DataFrame:
    """计算 Train 与 OOT 的最终箱分布 PSI。"""
    base = final_edges[["final_bin_order", bin_col]].drop_duplicates()
    train_count = train[bin_col].value_counts().rename("train_n")
    oot_count = oot[bin_col].value_counts().rename("oot_n")

    psi = (
        base.merge(train_count, left_on=bin_col, right_index=True, how="left")
        .merge(oot_count, left_on=bin_col, right_index=True, how="left")
        .fillna({"train_n": 0, "oot_n": 0})
        .sort_values("final_bin_order")
        .reset_index(drop=True)
    )

    psi["train_pct"] = safe_div(psi["train_n"], psi["train_n"].sum())
    psi["oot_pct"] = safe_div(psi["oot_n"], psi["oot_n"].sum())

    train_pct = psi["train_pct"].clip(lower=eps)
    oot_pct = psi["oot_pct"].clip(lower=eps)
    psi["psi_component"] = (oot_pct - train_pct) * np.log(oot_pct / train_pct)
    psi["psi_total"] = psi["psi_component"].sum()
    return psi
def calc_auc_ks(
    data: pd.DataFrame,
    score_col: str,
    label_col: str,
) -> pd.Series:
    """直接计算二分类 AUC 和 KS，不依赖 sklearn。"""
    work = data[[score_col, label_col]].copy()
    work[score_col] = pd.to_numeric(work[score_col], errors="coerce")
    work[label_col] = pd.to_numeric(work[label_col], errors="coerce")
    work = work.loc[
        work[score_col].notna() & work[label_col].isin([0, 1])
    ].copy()

    n = len(work)
    bad_count = int(work[label_col].eq(1).sum())
    good_count = int(work[label_col].eq(0).sum())
    bad_rate = safe_div(bad_count, n)

    if n == 0 or bad_count == 0 or good_count == 0:
        return pd.Series(
            {
                "n": n,
                "bad_cnt": bad_count,
                "good_cnt": good_count,
                "bad_rate": bad_rate,
                "auc": np.nan,
                "ks": np.nan,
            }
        )

    risk_score = work[score_col] if HIGH_SCORE_HIGH_RISK else -work[score_col]
    ranks = risk_score.rank(method="average")
    bad_rank_sum = ranks.loc[work[label_col].eq(1)].sum()
    auc = (
        bad_rank_sum - bad_count * (bad_count + 1) / 2
    ) / (bad_count * good_count)

    ordered = work.assign(_risk_score=risk_score).sort_values(
        "_risk_score",
        ascending=False,
    )
    cum_bad = ordered[label_col].eq(1).cumsum() / bad_count
    cum_good = ordered[label_col].eq(0).cumsum() / good_count
    ks = (cum_bad - cum_good).abs().max()

    return pd.Series(
        {
            "n": n,
            "bad_cnt": bad_count,
            "good_cnt": good_count,
            "bad_rate": bad_rate,
            "auc": auc,
            "ks": ks,
        }
    )
def calc_performance_table(data: pd.DataFrame) -> pd.DataFrame:
    """按 Train / OOT 计算 1M30+ 和 3M30+ 的 AUC、KS。"""
    rows = []
    for sample_group, group_data in data.groupby("sample_group", observed=True):
        if sample_group not in {"train", "oot"}:
            continue
        for label_col in ["duedate_1m_30", "duedate_3m_30"]:
            metrics = calc_auc_ks(group_data, SCORE_COL, label_col).to_dict()
            metrics.update(
                {
                    "sample_group": sample_group,
                    "label": label_col,
                }
            )
            rows.append(metrics)

    return pd.DataFrame(rows)[
        ["sample_group", "label", "n", "bad_cnt", "good_cnt", "bad_rate", "auc", "ks"]
    ]
def calc_portfolio_metrics(data: pd.DataFrame) -> Dict[str, float]:
    """计算一组样本的核心风险指标。"""
    work = add_risk_helper_columns(data)

    result: Dict[str, float] = {
        "n": len(work),
        "principal": float(work["_principal"].sum()),
    }

    for prefix, config in RISK_HELPER_CONFIG.items():
        helper = config["helper_prefix"]
        mature = int(work[f"{helper}_mature_cnt"].sum())
        bad = int(work[f"{helper}_bad_cnt"].sum())
        exposure = float(work[f"{helper}_amt_exposure"].sum())
        bad_amount = float(work[f"{helper}_amt_bad"].sum())

        result[f"{prefix}_cnt_mature"] = mature
        result[f"{prefix}_cnt_bad"] = bad
        result[f"{prefix}_cnt_bad_rate"] = safe_div(bad, mature)
        result[f"{prefix}_amt_exposure"] = exposure
        result[f"{prefix}_amt_bad"] = bad_amount
        result[f"{prefix}_amt_bad_rate"] = safe_div(bad_amount, exposure)

    return result
def prefix_metrics(metrics: Dict[str, float], prefix: str) -> Dict[str, float]:
    """给指标字典统一增加前缀。"""
    return {f"{prefix}_{key}": value for key, value in metrics.items()}
def calc_segment_metrics(
    data: pd.DataFrame,
    lower_threshold: Optional[float],
    upper_threshold: Optional[float],
) -> Dict[str, float]:
    """计算一个策略分数区间的样本和风险指标。"""
    score = data[SCORE_COL]
    mask = score.notna()

    if HIGH_SCORE_HIGH_RISK:
        if lower_threshold is not None:
            mask &= score.gt(lower_threshold)
        if upper_threshold is not None:
            mask &= score.le(upper_threshold)
    else:
        if lower_threshold is not None:
            mask &= score.lt(lower_threshold)
        if upper_threshold is not None:
            mask &= score.ge(upper_threshold)

    segment = data.loc[mask]
    metrics = calc_portfolio_metrics(segment)
    metrics["sample_pct"] = safe_div(len(segment), len(data))
    metrics["principal_pct"] = safe_div(
        metrics["principal"],
        data["principal"].fillna(0).sum(),
    )
    return metrics
def build_train_oot_compare(
    train_stats: pd.DataFrame,
    oot_stats: pd.DataFrame,
    final_edges: pd.DataFrame,
) -> pd.DataFrame:
    """生成最终箱 Train / OOT 对比表。"""
    key_cols = [FINAL_BIN_COL]
    compare_cols = [
        FINAL_BIN_COL,
        "n",
        "sample_pct",
        "1m30p_cnt_mature",
        "1m30p_cnt_bad",
        "1m30p_cnt_bad_rate",
        "3m30p_cnt_mature",
        "3m30p_cnt_bad",
        "3m30p_cnt_bad_rate",
        "1m30p_amt_exposure",
        "1m30p_amt_bad",
        "1m30p_amt_bad_rate",
        "3m30p_amt_exposure",
        "3m30p_amt_bad",
        "3m30p_amt_bad_rate",
    ]

    comparison = train_stats[compare_cols].merge(
        oot_stats[compare_cols],
        on=key_cols,
        how="outer",
        suffixes=("_train", "_oot"),
    )

    return final_edges[
        [
            "final_bin_order",
            FINAL_BIN_COL,
            "merged_from",
            "extreme_bin_role",
            "score_left",
            "score_right",
        ]
    ].merge(comparison, on=FINAL_BIN_COL, how="left")
