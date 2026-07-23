# -*- coding: utf-8 -*-
"""
模型分数分箱与策略阈值分析（精简版）

保留的核心流程：
1. 加载并拼接样本、申请信息和主模型分；
2. 按月份切分 Train / OOT；
3. 在 Train 上进行 20 等频初分，并将同一边界复用到 OOT；
4. 按配置进行相邻箱合并；
5. 计算 1M30+ / 3M30+ 的笔数及金额风险指标；
6. 使用 PSI、AUC、KS 和单调性进行基础验证；
7. 基于最终箱边界生成一套自动通过 / 人工审核 / 拒绝策略；
8. 输出包含分箱过程、阈值选择依据和关键中间表的 Excel 报告。

运行方式：
    python binning_slim.py

输入目录：
    res/
输出目录：
    out/策略报告_单策略版.xlsx
"""

from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


# ============================================================
# 0. 配置
# ============================================================

DATA_DIR = Path("res")
OUT_DIR = Path("out")
REPORT_PATH = OUT_DIR / "策略报告_单策略版.xlsx"

SAMPLE_FILE = "sample.csv"
APPLICATION_FILE = "application_info.csv"
SCORE_FILE = "aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv"

RAW_SCORE_COL = "aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score"
SCORE_COL = "score_mlt"

TRAIN_END_MONTH = "2025-10"
OOT_START_MONTH = "2025-11"

INITIAL_BIN_COUNT = 20
INITIAL_BIN_COL = "score_mlt_bin20"
FINAL_BIN_COL = "score_mlt_final_bin"

# 当前模型按“高分高风险”处理。
HIGH_SCORE_HIGH_RISK = True

# 相邻初始箱合并为 8 个最终风险等级。
# 修改合箱方案时，只需要调整这里。
FINAL_BIN_RANGES: List[Tuple[int, int]] = [
    (1, 2),
    (3, 5),
    (6, 8),
    (9, 11),
    (12, 14),
    (15, 16),
    (17, 18),
    (19, 20),
]

# 默认策略的风险约束。
# auto_constraints：自动通过人群的累计风险和边际风险上限；
# accept_constraints：自动通过 + 人工审核人群的累计风险和边际风险上限。
# 首版只保留一套策略，后续只需在这里调整约束。
STRATEGY_CONFIG = {
    "strategy_name": "默认策略",
    "objective": "平衡通过率、整体风险和边际风险",
    "auto_constraints": {
        "max_cum_1m30p_cnt_bad_rate": 0.0090,
        "max_cum_3m30p_cnt_bad_rate": 0.0550,
        "max_marginal_3m30p_cnt_bad_rate": 0.0900,
    },
    "accept_constraints": {
        "max_cum_1m30p_cnt_bad_rate": 0.0130,
        "max_cum_3m30p_cnt_bad_rate": 0.0750,
        "max_marginal_3m30p_cnt_bad_rate": 0.1700,
    },
}

RISK_NUMERIC_COLS = [
    SCORE_COL,
    "duedate_1m_30",
    "duedate_3m_30",
    "principal",
    "estimate_principal_remaining_mob1",
    "estimate_principal_remaining_mob3",
    "dpd_days_ever_mob1",
    "dpd_days_ever_mob3",
]

REQUIRED_ANALYSIS_COLS = [
    "application_id",
    "user_id",
    "application_time",
    "application_month",
    SCORE_COL,
    "duedate_1m_30",
    "duedate_3m_30",
    "principal",
    "estimate_principal_remaining_mob1",
    "estimate_principal_remaining_mob3",
    "dpd_days_ever_mob1",
    "dpd_days_ever_mob3",
]


# ============================================================
# 1. 通用工具
# ============================================================

def clean_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """清理 UTF-8 BOM 和少数 CSV 表头乱码。"""
    result = frame.copy()
    result.columns = [str(col).lstrip("\ufeff").lstrip("ï»¿") for col in result.columns]
    return result


def read_csv_clean(path: Path) -> pd.DataFrame:
    """读取 CSV，并统一清理字段名。"""
    if not path.exists():
        raise FileNotFoundError(f"输入文件不存在: {path}")
    return clean_columns(pd.read_csv(path, low_memory=False))


def require_columns(frame: pd.DataFrame, columns: Iterable[str], context: str) -> None:
    """校验 DataFrame 是否包含必要字段。"""
    missing = [col for col in columns if col not in frame.columns]
    if missing:
        raise ValueError(f"{context} 缺少必要字段: {missing}")


def safe_div(numerator, denominator):
    """安全除法；分母为 0 时返回 NaN。"""
    num = np.asarray(numerator, dtype="float64")
    den = np.asarray(denominator, dtype="float64")
    result = np.full(np.broadcast(num, den).shape, np.nan, dtype="float64")
    np.divide(num, den, out=result, where=den != 0)

    if np.ndim(result) == 0:
        return float(result)
    if isinstance(numerator, pd.Series):
        return pd.Series(result, index=numerator.index)
    if isinstance(denominator, pd.Series):
        return pd.Series(result, index=denominator.index)
    return result


def flatten_dict(prefix: str, values: Dict[str, float]) -> Dict[str, float]:
    """将策略约束字典展开为平面字段。"""
    return {f"{prefix}_{key}": value for key, value in values.items()}


# ============================================================
# 2. 数据加载与样本切分
# ============================================================

def load_analysis_data() -> pd.DataFrame:
    """加载首版分箱真正需要的数据，删除未使用的其他模型表和交易特征表。"""
    print("1/8 加载数据 ...")

    sample = read_csv_clean(DATA_DIR / SAMPLE_FILE)
    application = read_csv_clean(DATA_DIR / APPLICATION_FILE)
    score = read_csv_clean(DATA_DIR / SCORE_FILE)

    require_columns(sample, ["application_id", "user_id"], "sample")
    require_columns(application, ["application_id", "user_id"], "application_info")
    require_columns(score, ["application_id", RAW_SCORE_COL], "score")

    # application_info 只补充 sample 中不存在的字段，避免出现 _x / _y。
    join_keys = ["application_id", "user_id"]
    application_extra_cols = [
        col for col in application.columns
        if col in join_keys or col not in sample.columns
    ]
    application_dedup = application[application_extra_cols].drop_duplicates(
        subset=join_keys,
        keep="first",
    )

    data = sample.merge(application_dedup, on=join_keys, how="left")

    score_dedup = (
        score[["application_id", RAW_SCORE_COL]]
        .drop_duplicates(subset="application_id", keep="first")
        .rename(columns={RAW_SCORE_COL: SCORE_COL})
    )
    data = data.merge(score_dedup, on="application_id", how="left")

    # application_month 缺失时，根据 application_time 补充。
    if "application_time" in data.columns:
        data["application_time"] = pd.to_datetime(data["application_time"], errors="coerce")

    if "application_month" not in data.columns:
        data["application_month"] = pd.Series(pd.NA, index=data.index, dtype="string")
    else:
        data["application_month"] = data["application_month"].astype("string").str.slice(0, 7)

    if "application_time" in data.columns:
        month_from_time = data["application_time"].dt.to_period("M").astype("string")
        data["application_month"] = data["application_month"].fillna(month_from_time)

    require_columns(data, REQUIRED_ANALYSIS_COLS, "拼接后的分析数据")

    for col in RISK_NUMERIC_COLS:
        data[col] = pd.to_numeric(data[col], errors="coerce")

    # 分箱分析只使用存在模型分的样本；缺失比例会在总览中单独展示。
    source_row_count = len(data)
    score_missing_count = int(data[SCORE_COL].isna().sum())
    data.attrs["source_row_count"] = source_row_count
    data.attrs["score_missing_count"] = score_missing_count

    data = data.loc[data[SCORE_COL].notna()].copy()
    if data.empty:
        raise ValueError(f"{SCORE_COL} 全为空，无法进行分箱")

    print(
        f"   原始 {source_row_count:,} 行；有效模型分 {len(data):,} 行；"
        f"模型分缺失 {score_missing_count:,} 行"
    )
    return data


def split_train_oot(data: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """按申请月份切分 Train 和 OOT。"""
    result = data.copy()
    month = result["application_month"].astype("string")

    train_mask = month.notna() & month.le(TRAIN_END_MONTH)
    oot_mask = month.notna() & month.ge(OOT_START_MONTH)

    result["sample_group"] = np.select(
        [
            train_mask.to_numpy(dtype=bool, na_value=False),
            oot_mask.to_numpy(dtype=bool, na_value=False),
        ],
        ["train", "oot"],
        default="gap_or_unknown",
    )

    train = result.loc[result["sample_group"].eq("train")].copy()
    oot = result.loc[result["sample_group"].eq("oot")].copy()

    if train.empty:
        raise ValueError("Train 样本为空，请检查 TRAIN_END_MONTH 和 application_month")
    if oot.empty:
        raise ValueError("OOT 样本为空，请检查 OOT_START_MONTH 和 application_month")

    print(f"2/8 样本切分完成：Train {len(train):,} 行，OOT {len(oot):,} 行")
    return result, train, oot


# ============================================================
# 3. 等频初分与相邻箱合并
# ============================================================

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
                "interval_rule": "(left, right]",
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


def validate_merge_ranges(
    ranges: Sequence[Tuple[int, int]],
    initial_bin_count: int,
) -> None:
    """检查合箱范围是否连续、无重叠，并覆盖所有初始箱。"""
    flattened: List[int] = []
    for start, end in ranges:
        if start > end:
            raise ValueError(f"无效合箱范围: ({start}, {end})")
        flattened.extend(range(start, end + 1))

    expected = list(range(1, initial_bin_count + 1))
    if flattened != expected:
        raise ValueError(
            "FINAL_BIN_RANGES 必须连续且完整覆盖所有初始箱。"
            f"当前实际初始箱数={initial_bin_count}，覆盖结果={flattened}"
        )


def build_merge_map(
    ranges: Sequence[Tuple[int, int]],
    initial_bin_count: int,
) -> pd.DataFrame:
    """生成初始箱到最终风险等级的映射。"""
    validate_merge_ranges(ranges, initial_bin_count)

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
    final_edges["interval_rule"] = "(left, right]"
    return final_edges


# ============================================================
# 4. 风险指标计算
# ============================================================

def add_risk_helper_columns(data: pd.DataFrame) -> pd.DataFrame:
    """生成分箱统计所需的成熟、逾期和金额字段。"""
    work = data.copy()

    work["_principal"] = work["principal"].fillna(0)

    work["_m1_mature_cnt"] = work["duedate_1m_30"].isin([0, 1])
    work["_m1_bad_cnt"] = work["duedate_1m_30"].eq(1)
    work["_m3_mature_cnt"] = work["duedate_3m_30"].isin([0, 1])
    work["_m3_bad_cnt"] = work["duedate_3m_30"].eq(1)

    m1_mature_amt = work["dpd_days_ever_mob1"].notna()
    m3_mature_amt = work["dpd_days_ever_mob3"].notna()
    m1_bad_amt = m1_mature_amt & work["dpd_days_ever_mob1"].ge(30)
    m3_bad_amt = m3_mature_amt & work["dpd_days_ever_mob3"].ge(30)

    work["_m1_amt_exposure"] = np.where(m1_mature_amt, work["_principal"], 0)
    work["_m3_amt_exposure"] = np.where(m3_mature_amt, work["_principal"], 0)
    work["_m1_amt_bad"] = np.where(
        m1_bad_amt,
        work["estimate_principal_remaining_mob1"].fillna(0),
        0,
    )
    work["_m3_amt_bad"] = np.where(
        m3_bad_amt,
        work["estimate_principal_remaining_mob3"].fillna(0),
        0,
    )
    return work


def calc_bin_stats(
    data: pd.DataFrame,
    bin_col: str,
    order_col: str,
    score_col: str = SCORE_COL,
) -> pd.DataFrame:
    """
    按分箱计算核心指标。

    每个率均保留可在 Excel 中复算的分子和分母：
    - 笔数逾期率 = bad / mature；
    - 金额逾期率 = bad amount / exposure；
    - 样本占比 = n / total_n。
    """
    required = [
        "application_id",
        bin_col,
        order_col,
        score_col,
        *RISK_NUMERIC_COLS[1:],
    ]
    require_columns(data, required, "calc_bin_stats")

    work = add_risk_helper_columns(data)
    stats = (
        work.groupby([bin_col, order_col], dropna=False, observed=True)
        .agg(
            n=("application_id", "count"),
            application_id_nunique=("application_id", "nunique"),
            principal_amt=("_principal", "sum"),
            score_min=(score_col, "min"),
            score_max=(score_col, "max"),
            score_mean=(score_col, "mean"),
            **{
                "1m30p_cnt_mature": ("_m1_mature_cnt", "sum"),
                "1m30p_cnt_bad": ("_m1_bad_cnt", "sum"),
                "3m30p_cnt_mature": ("_m3_mature_cnt", "sum"),
                "3m30p_cnt_bad": ("_m3_bad_cnt", "sum"),
                "1m30p_amt_exposure": ("_m1_amt_exposure", "sum"),
                "1m30p_amt_bad": ("_m1_amt_bad", "sum"),
                "3m30p_amt_exposure": ("_m3_amt_exposure", "sum"),
                "3m30p_amt_bad": ("_m3_amt_bad", "sum"),
            },
        )
        .reset_index()
        .rename(columns={order_col: "bin_order"})
        .sort_values("bin_order")
        .reset_index(drop=True)
    )

    total_n = stats["n"].sum()
    stats["total_n"] = total_n
    stats["sample_pct"] = safe_div(stats["n"], total_n)

    for prefix in ["1m30p", "3m30p"]:
        stats[f"{prefix}_cnt_good"] = (
            stats[f"{prefix}_cnt_mature"] - stats[f"{prefix}_cnt_bad"]
        )
        stats[f"{prefix}_cnt_bad_rate"] = safe_div(
            stats[f"{prefix}_cnt_bad"],
            stats[f"{prefix}_cnt_mature"],
        )
        stats[f"{prefix}_amt_bad_rate"] = safe_div(
            stats[f"{prefix}_amt_bad"],
            stats[f"{prefix}_amt_exposure"],
        )

    # 按低风险到高风险累计，直接用于阈值分析。
    stats["cum_n"] = stats["n"].cumsum()
    stats["cum_pass_rate"] = safe_div(stats["cum_n"], total_n)

    for prefix in ["1m30p", "3m30p"]:
        stats[f"cum_{prefix}_cnt_mature"] = stats[f"{prefix}_cnt_mature"].cumsum()
        stats[f"cum_{prefix}_cnt_bad"] = stats[f"{prefix}_cnt_bad"].cumsum()
        stats[f"cum_{prefix}_cnt_bad_rate"] = safe_div(
            stats[f"cum_{prefix}_cnt_bad"],
            stats[f"cum_{prefix}_cnt_mature"],
        )

        stats[f"cum_{prefix}_amt_exposure"] = stats[f"{prefix}_amt_exposure"].cumsum()
        stats[f"cum_{prefix}_amt_bad"] = stats[f"{prefix}_amt_bad"].cumsum()
        stats[f"cum_{prefix}_amt_bad_rate"] = safe_div(
            stats[f"cum_{prefix}_amt_bad"],
            stats[f"cum_{prefix}_amt_exposure"],
        )

    return stats


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


# ============================================================
# 5. OOT 基础验证
# ============================================================

def calc_population_psi(
    train: pd.DataFrame,
    oot: pd.DataFrame,
    bin_col: str,
    final_edges: pd.DataFrame,
    eps: float = 1e-6,
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


# ============================================================
# 6. 阈值曲线与策略结果
# ============================================================

def calc_portfolio_metrics(data: pd.DataFrame) -> Dict[str, float]:
    """计算一组样本的核心风险指标。"""
    work = add_risk_helper_columns(data)

    result: Dict[str, float] = {
        "n": len(work),
        "principal": float(work["_principal"].sum()),
    }

    for prefix, mature_col, bad_col, exposure_col, bad_amt_col in [
        ("1m30p", "_m1_mature_cnt", "_m1_bad_cnt", "_m1_amt_exposure", "_m1_amt_bad"),
        ("3m30p", "_m3_mature_cnt", "_m3_bad_cnt", "_m3_amt_exposure", "_m3_amt_bad"),
    ]:
        mature = int(work[mature_col].sum())
        bad = int(work[bad_col].sum())
        exposure = float(work[exposure_col].sum())
        bad_amount = float(work[bad_amt_col].sum())

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


def build_threshold_curve(
    train: pd.DataFrame,
    final_edges: pd.DataFrame,
) -> pd.DataFrame:
    """使用最终箱右边界生成可上线的候选阈值曲线。"""
    max_score = train[SCORE_COL].max()
    threshold_table = final_edges[
        ["final_bin_order", FINAL_BIN_COL, "score_right", "merged_from"]
    ].copy()
    threshold_table["threshold"] = threshold_table["score_right"].replace(np.inf, max_score)
    threshold_table = threshold_table.loc[threshold_table["threshold"].notna()].copy()
    threshold_table = threshold_table.sort_values("final_bin_order").reset_index(drop=True)

    total_n = len(train)
    total_principal = train["principal"].fillna(0).sum()
    score = train[SCORE_COL]

    rows = []
    previous_threshold: Optional[float] = None

    for threshold_order, threshold_row in threshold_table.iterrows():
        threshold = float(threshold_row["threshold"])

        if HIGH_SCORE_HIGH_RISK:
            cumulative_mask = score.le(threshold)
            marginal_mask = (
                cumulative_mask
                if previous_threshold is None
                else score.gt(previous_threshold) & score.le(threshold)
            )
        else:
            cumulative_mask = score.ge(threshold)
            marginal_mask = (
                cumulative_mask
                if previous_threshold is None
                else score.lt(previous_threshold) & score.ge(threshold)
            )

        cumulative_metrics = calc_portfolio_metrics(train.loc[cumulative_mask])
        marginal_metrics = calc_portfolio_metrics(train.loc[marginal_mask])

        row = {
            "threshold_order": threshold_order + 1,
            "threshold": threshold,
            "prev_threshold": previous_threshold,
            "final_bin_order": threshold_row["final_bin_order"],
            FINAL_BIN_COL: threshold_row[FINAL_BIN_COL],
            "merged_from": threshold_row["merged_from"],
        }
        row.update(prefix_metrics(cumulative_metrics, "cum"))
        row.update(prefix_metrics(marginal_metrics, "marginal"))
        row["cum_pass_rate"] = safe_div(cumulative_metrics["n"], total_n)
        row["cum_principal_pct"] = safe_div(
            cumulative_metrics["principal"],
            total_principal,
        )
        row["marginal_sample_pct"] = safe_div(marginal_metrics["n"], total_n)
        row["marginal_principal_pct"] = safe_div(
            marginal_metrics["principal"],
            total_principal,
        )
        rows.append(row)
        previous_threshold = threshold

    return pd.DataFrame(rows)


def select_threshold_under_constraints(
    curve: pd.DataFrame,
    constraints: Dict[str, float],
) -> Optional[pd.Series]:
    """选择满足风险约束且累计通过率最高的阈值。"""
    eligible = curve.copy()
    for constraint_name, maximum in constraints.items():
        metric = constraint_name.removeprefix("max_")
        require_columns(eligible, [metric], "策略阈值曲线")
        eligible = eligible.loc[eligible[metric].le(maximum)]

    if eligible.empty:
        return None

    return eligible.sort_values(
        ["cum_pass_rate", "threshold_order"],
        ascending=[False, False],
    ).iloc[0]


def build_strategy_plan(
    curve: pd.DataFrame,
    config: Dict,
) -> pd.DataFrame:
    """根据唯一一套配置生成自动通过、人工审核和拒绝阈值。"""
    auto_row = select_threshold_under_constraints(
        curve,
        config["auto_constraints"],
    )
    accept_row = select_threshold_under_constraints(
        curve,
        config["accept_constraints"],
    )

    base = {
        "strategy_name": config["strategy_name"],
        "objective": config["objective"],
    }

    if auto_row is None or accept_row is None:
        return pd.DataFrame([{**base, "status": "无满足约束的阈值"}])

    # 接纳阈值不能比自动通过阈值更严格。
    if accept_row["threshold_order"] < auto_row["threshold_order"]:
        accept_row = auto_row

    result = {
        **base,
        "status": "OK",
        "auto_pass_threshold": auto_row["threshold"],
        "auto_pass_bin": auto_row[FINAL_BIN_COL],
        "reject_threshold": accept_row["threshold"],
        "manual_review_upper_bin": accept_row[FINAL_BIN_COL],
        "auto_pass_rate": auto_row["cum_pass_rate"],
        "accepted_rate": accept_row["cum_pass_rate"],
        "manual_review_rate": (
            accept_row["cum_pass_rate"] - auto_row["cum_pass_rate"]
        ),
        "reject_rate": 1 - accept_row["cum_pass_rate"],
        "accepted_1m30p_cnt_bad_rate": accept_row["cum_1m30p_cnt_bad_rate"],
        "accepted_3m30p_cnt_bad_rate": accept_row["cum_3m30p_cnt_bad_rate"],
        "accepted_1m30p_amt_bad_rate": accept_row["cum_1m30p_amt_bad_rate"],
        "accepted_3m30p_amt_bad_rate": accept_row["cum_3m30p_amt_bad_rate"],
        "last_accepted_marginal_3m30p_cnt_bad_rate": accept_row[
            "marginal_3m30p_cnt_bad_rate"
        ],
    }
    return pd.DataFrame([result])


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


def build_strategy_segment_report(
    train: pd.DataFrame,
    oot: pd.DataFrame,
    strategy_plan: pd.DataFrame,
) -> pd.DataFrame:
    """验证唯一策略在 Train 和 OOT 中的三段表现。"""
    valid = strategy_plan.loc[strategy_plan["status"].eq("OK")]
    if valid.empty:
        return pd.DataFrame()

    strategy = valid.iloc[0]
    auto_threshold = float(strategy["auto_pass_threshold"])
    reject_threshold = float(strategy["reject_threshold"])
    segments = [
        ("自动通过", None, auto_threshold),
        ("人工审核", auto_threshold, reject_threshold),
        ("拒绝", reject_threshold, None),
    ]

    rows = []
    for sample_group, data in [("train", train), ("oot", oot)]:
        for decision, lower, upper in segments:
            metrics = calc_segment_metrics(data, lower, upper)
            rows.append(
                {
                    "sample_group": sample_group,
                    "strategy_name": strategy["strategy_name"],
                    "decision": decision,
                    "lower_threshold_exclusive": lower,
                    "upper_threshold_inclusive": upper,
                    **metrics,
                }
            )

    return pd.DataFrame(rows)




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

    for prefix in ["1m30p", "3m30p"]:
        rate_col = f"{prefix}_cnt_bad_rate"
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
        "1m30p_cnt_mature",
        "1m30p_cnt_bad",
        "1m30p_cnt_bad_rate",
        "1m30p_rate_diff_prev",
        "1m30p_inversion_flag",
        "3m30p_cnt_mature",
        "3m30p_cnt_bad",
        "3m30p_cnt_bad_rate",
        "3m30p_rate_diff_prev",
        "3m30p_inversion_flag",
        "1m30p_amt_exposure",
        "1m30p_amt_bad",
        "1m30p_amt_bad_rate",
        "3m30p_amt_exposure",
        "3m30p_amt_bad",
        "3m30p_amt_bad_rate",
        "cum_pass_rate",
        "cum_1m30p_cnt_bad_rate",
        "cum_3m30p_cnt_bad_rate",
        "final_bin_order",
        FINAL_BIN_COL,
        "merged_from",
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
            metric = constraint_name.removeprefix("max_")
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
        "cum_pass_rate",
        "cum_n",
        "cum_principal_pct",
        "cum_1m30p_cnt_mature",
        "cum_1m30p_cnt_bad",
        "cum_1m30p_cnt_bad_rate",
        "cum_3m30p_cnt_mature",
        "cum_3m30p_cnt_bad",
        "cum_3m30p_cnt_bad_rate",
        "cum_1m30p_amt_exposure",
        "cum_1m30p_amt_bad",
        "cum_1m30p_amt_bad_rate",
        "cum_3m30p_amt_exposure",
        "cum_3m30p_amt_bad",
        "cum_3m30p_amt_bad_rate",
        "marginal_sample_pct",
        "marginal_n",
        "marginal_3m30p_cnt_mature",
        "marginal_3m30p_cnt_bad",
        "marginal_3m30p_cnt_bad_rate",
        "auto_all_constraints_ok",
        "accept_all_constraints_ok",
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
        ("笔数风险", "1m30p_cnt_mature", "1M30+ 已成熟样本量", "duedate_1m_30 IN (0, 1)"),
        ("笔数风险", "1m30p_cnt_bad", "1M30+ 逾期样本量", "duedate_1m_30 = 1"),
        ("笔数风险", "1m30p_cnt_bad_rate", "1M30+ 笔数逾期率", "1m30p_cnt_bad / 1m30p_cnt_mature"),
        ("笔数风险", "3m30p_cnt_mature", "3M30+ 已成熟样本量", "duedate_3m_30 IN (0, 1)"),
        ("笔数风险", "3m30p_cnt_bad", "3M30+ 逾期样本量", "duedate_3m_30 = 1"),
        ("笔数风险", "3m30p_cnt_bad_rate", "3M30+ 笔数逾期率", "3m30p_cnt_bad / 3m30p_cnt_mature"),
        ("金额风险", "1m30p_amt_exposure", "1M30+ 已成熟样本的本金敞口", "SUM(principal) WHERE MOB1 已成熟"),
        ("金额风险", "1m30p_amt_bad", "MOB1 30+ 样本的剩余本金", "SUM(estimate_principal_remaining_mob1)"),
        ("金额风险", "1m30p_amt_bad_rate", "1M30+ 金额逾期率", "1m30p_amt_bad / 1m30p_amt_exposure"),
        ("金额风险", "3m30p_amt_exposure", "3M30+ 已成熟样本的本金敞口", "SUM(principal) WHERE MOB3 已成熟"),
        ("金额风险", "3m30p_amt_bad", "MOB3 30+ 样本的剩余本金", "SUM(estimate_principal_remaining_mob3)"),
        ("金额风险", "3m30p_amt_bad_rate", "3M30+ 金额逾期率", "3m30p_amt_bad / 3m30p_amt_exposure"),
        ("阈值", "cum_pass_rate", "从低风险端累计到当前阈值的通过率", "cum_n / total_n"),
        ("阈值", "marginal_sample_pct", "当前档位新增样本占比", "marginal_n / total_n"),
        ("阈值", "marginal_3m30p_cnt_bad_rate", "当前新增档位自身的 3M30+ 风险", "marginal_bad / marginal_mature"),
        ("阈值", "auto_all_constraints_ok", "该候选阈值是否满足自动通过全部约束", "全部自动通过检查项均为 True"),
        ("阈值", "accept_all_constraints_ok", "该候选阈值是否满足整体接纳全部约束", "全部整体接纳检查项均为 True"),
        ("验证", "PSI", "Train 与 OOT 的分箱分布稳定性", "SUM((OOT%-Train%) * LN(OOT%/Train%))"),
        ("验证", "AUC / KS", "模型风险区分能力指标", "分别衡量排序能力和好坏样本累计差异"),
    ]
    return pd.DataFrame(rows, columns=["category", "field", "definition", "calculation"])


# ============================================================
# 7. 报告数据整理与输出
# ============================================================

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
        ["final_bin_order", FINAL_BIN_COL, "merged_from", "score_left", "score_right"]
    ].merge(comparison, on=FINAL_BIN_COL, how="left")


def build_overview(
    data: pd.DataFrame,
    train: pd.DataFrame,
    oot: pd.DataFrame,
    initial_bin_count: int,
    final_bin_count: int,
    psi: pd.DataFrame,
    performance: pd.DataFrame,
    monotonicity: pd.DataFrame,
    strategy_plan: pd.DataFrame,
) -> pd.DataFrame:
    """整理报告首页的核心结论，并按模块分组展示。"""
    source_row_count = int(data.attrs.get("source_row_count", len(data)))
    score_missing_count = int(data.attrs.get("score_missing_count", 0))

    rows = [
        ("样本", "原始样本量", source_row_count),
        ("样本", "有效模型分样本量", len(data)),
        ("样本", "模型分缺失量", score_missing_count),
        ("样本", "模型分缺失率", safe_div(score_missing_count, source_row_count)),
        ("样本", "Train 样本量", len(train)),
        ("样本", "OOT 样本量", len(oot)),
        ("时间切分", "Train 截止月份", TRAIN_END_MONTH),
        ("时间切分", "OOT 起始月份", OOT_START_MONTH),
        ("分箱", "初始箱数量", initial_bin_count),
        ("分箱", "最终箱数量", final_bin_count),
        ("稳定性", "最终箱 PSI", psi["psi_total"].iloc[0]),
    ]

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
        sample_check = monotonicity.loc[
            monotonicity["sample_group"].eq(sample_group)
        ]
        rows.append(
            (
                "单调性",
                f"{sample_group}_最终箱全部单调",
                bool(sample_check["is_monotonic_non_decreasing"].all()),
            )
        )

    valid_strategy = strategy_plan.loc[strategy_plan["status"].eq("OK")]
    if not valid_strategy.empty:
        row = valid_strategy.iloc[0]
        rows.extend(
            [
                ("策略", "策略名称", row["strategy_name"]),
                ("策略", "自动通过阈值", row["auto_pass_threshold"]),
                ("策略", "自动通过截止风险档", row["auto_pass_bin"]),
                ("策略", "人工审核上限/拒绝阈值", row["reject_threshold"]),
                ("策略", "人工审核截止风险档", row["manual_review_upper_bin"]),
                ("策略", "自动通过率", row["auto_pass_rate"]),
                ("策略", "人工审核率", row["manual_review_rate"]),
                ("策略", "拒绝率", row["reject_rate"]),
                ("策略风险", "接纳人群1M30+笔数逾期率", row["accepted_1m30p_cnt_bad_rate"]),
                ("策略风险", "接纳人群3M30+笔数逾期率", row["accepted_3m30p_cnt_bad_rate"]),
                ("策略风险", "接纳人群1M30+金额逾期率", row["accepted_1m30p_amt_bad_rate"]),
                ("策略风险", "接纳人群3M30+金额逾期率", row["accepted_3m30p_amt_bad_rate"]),
                ("策略风险", "最后接纳档边际3M30+", row["last_accepted_marginal_3m30p_cnt_bad_rate"]),
            ]
        )
    else:
        status = strategy_plan.iloc[0]["status"] if not strategy_plan.empty else "未生成"
        rows.append(("策略", "策略状态", status))

    return pd.DataFrame(rows, columns=["section", "metric", "value"])

def build_config_table() -> pd.DataFrame:
    """输出便于后续修改和版本管理的参数表。"""
    rows = [
        {"config_group": "基础配置", "config_name": "DATA_DIR", "config_value": str(DATA_DIR)},
        {"config_group": "基础配置", "config_name": "TRAIN_END_MONTH", "config_value": TRAIN_END_MONTH},
        {"config_group": "基础配置", "config_name": "OOT_START_MONTH", "config_value": OOT_START_MONTH},
        {"config_group": "基础配置", "config_name": "INITIAL_BIN_COUNT", "config_value": INITIAL_BIN_COUNT},
        {"config_group": "基础配置", "config_name": "HIGH_SCORE_HIGH_RISK", "config_value": HIGH_SCORE_HIGH_RISK},
        {"config_group": "合箱配置", "config_name": "FINAL_BIN_RANGES", "config_value": str(FINAL_BIN_RANGES)},
    ]

    flattened = {
        **flatten_dict("auto", STRATEGY_CONFIG["auto_constraints"]),
        **flatten_dict("accept", STRATEGY_CONFIG["accept_constraints"]),
    }
    for name, value in flattened.items():
        rows.append(
            {
                "config_group": STRATEGY_CONFIG["strategy_name"],
                "config_name": name,
                "config_value": value,
            }
        )

    return pd.DataFrame(rows)


def format_excel_report(path: Path) -> None:
    """设置基础格式，并突出分箱倒挂、约束结果和最终选中阈值。"""
    from openpyxl import load_workbook

    workbook = load_workbook(path)
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    selected_auto_fill = PatternFill("solid", fgColor="C6E0B4")
    selected_reject_fill = PatternFill("solid", fgColor="FCE4D6")
    warning_fill = PatternFill("solid", fgColor="FFF2CC")
    fail_fill = PatternFill("solid", fgColor="F4CCCC")

    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions

        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        for column_cells in sheet.columns:
            column_letter = get_column_letter(column_cells[0].column)
            max_length = 0
            for cell in column_cells:
                value = "" if cell.value is None else str(cell.value)
                max_length = max(max_length, len(value))
            sheet.column_dimensions[column_letter].width = min(max(max_length + 2, 10), 38)

        headers = {cell.column: str(cell.value) for cell in sheet[1]}
        header_to_col = {str(cell.value): cell.column for cell in sheet[1]}

        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                header = headers.get(cell.column, "").lower()
                if cell.value is None:
                    continue
                if any(key in header for key in ["rate", "pct"]):
                    cell.number_format = "0.00%"
                elif any(key in header for key in ["auc", "ks", "psi"]):
                    cell.number_format = "0.0000"
                elif any(key in header for key in ["threshold", "score_left", "score_right", "score_min", "score_max", "score_mean"]):
                    cell.number_format = "0.0000"
                elif isinstance(cell.value, (int, float)):
                    cell.number_format = "#,##0.00"

        # 分箱过程：突出风险倒挂的初始箱。
        if sheet.title == "02_分箱过程":
            inversion_cols = [
                header_to_col.get("1m30p_inversion_flag"),
                header_to_col.get("3m30p_inversion_flag"),
            ]
            inversion_cols = [col for col in inversion_cols if col is not None]
            for row_idx in range(2, sheet.max_row + 1):
                if any(sheet.cell(row_idx, col).value is True for col in inversion_cols):
                    for cell in sheet[row_idx]:
                        cell.fill = warning_fill

        # 阈值选择：突出最终选中阈值，并标记未通过约束的候选点。
        if sheet.title == "06_阈值选择过程":
            selected_col = header_to_col.get("selected_role")
            auto_ok_col = header_to_col.get("auto_all_constraints_ok")
            accept_ok_col = header_to_col.get("accept_all_constraints_ok")

            for row_idx in range(2, sheet.max_row + 1):
                selected_role = sheet.cell(row_idx, selected_col).value if selected_col else ""
                if selected_role and "自动通过" in str(selected_role):
                    for cell in sheet[row_idx]:
                        cell.fill = selected_auto_fill
                if selected_role and "拒绝阈值" in str(selected_role):
                    for cell in sheet[row_idx]:
                        cell.fill = selected_reject_fill

                for check_col in [auto_ok_col, accept_ok_col]:
                    if check_col and sheet.cell(row_idx, check_col).value is False:
                        sheet.cell(row_idx, check_col).fill = fail_fill

    workbook.save(path)


def write_report(
    overview: pd.DataFrame,
    binning_process: pd.DataFrame,
    final_train_stats: pd.DataFrame,
    final_oot_stats: pd.DataFrame,
    train_oot_compare: pd.DataFrame,
    threshold_selection: pd.DataFrame,
    strategy_plan: pd.DataFrame,
    strategy_segments: pd.DataFrame,
    performance: pd.DataFrame,
    psi: pd.DataFrame,
    monotonicity: pd.DataFrame,
    config_table: pd.DataFrame,
    metric_dictionary: pd.DataFrame,
) -> None:
    """输出单策略报告，并保留最关键的过程和中间结果。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(REPORT_PATH, engine="openpyxl") as writer:
        overview.to_excel(writer, sheet_name="01_总览", index=False)
        binning_process.to_excel(writer, sheet_name="02_分箱过程", index=False)
        final_train_stats.to_excel(writer, sheet_name="03_最终分箱_Train", index=False)
        final_oot_stats.to_excel(writer, sheet_name="04_最终分箱_OOT", index=False)
        train_oot_compare.to_excel(writer, sheet_name="05_Train_OOT对比", index=False)
        threshold_selection.to_excel(writer, sheet_name="06_阈值选择过程", index=False)
        strategy_plan.to_excel(writer, sheet_name="07_策略结果", index=False)
        strategy_segments.to_excel(writer, sheet_name="08_策略分段验证", index=False)
        performance.to_excel(writer, sheet_name="09_AUC_KS", index=False)
        psi.to_excel(writer, sheet_name="10_PSI", index=False)
        monotonicity.to_excel(writer, sheet_name="11_单调性", index=False)
        config_table.to_excel(writer, sheet_name="12_配置参数", index=False)
        metric_dictionary.to_excel(writer, sheet_name="13_指标说明", index=False)

    format_excel_report(REPORT_PATH)


# ============================================================
# 8. 主流程
# ============================================================

def main() -> None:
    data = load_analysis_data()
    source_row_count = data.attrs.get("source_row_count")
    score_missing_count = data.attrs.get("score_missing_count")

    all_data, train, oot = split_train_oot(data)

    # pandas 的 attrs 在部分切片/合并操作后可能丢失，因此显式保留。
    all_data.attrs["source_row_count"] = source_row_count
    all_data.attrs["score_missing_count"] = score_missing_count

    # 1) Train 学习初始边界，并复用到全量。
    edges = learn_equal_freq_edges(train, SCORE_COL, INITIAL_BIN_COUNT)
    actual_initial_bin_count = len(edges) - 1
    initial_edges = build_initial_edge_table(edges)

    if actual_initial_bin_count != INITIAL_BIN_COUNT:
        raise ValueError(
            f"由于模型分重复值，实际只形成 {actual_initial_bin_count} 个初始箱，"
            f"与配置 {INITIAL_BIN_COUNT} 不一致。请调整 INITIAL_BIN_COUNT 或 FINAL_BIN_RANGES。"
        )

    all_binned = apply_edges(all_data, SCORE_COL, edges, INITIAL_BIN_COL)
    train_binned = all_binned.loc[all_binned["sample_group"].eq("train")].copy()
    oot_binned = all_binned.loc[all_binned["sample_group"].eq("oot")].copy()

    initial_train_stats = calc_bin_stats(
        train_binned,
        bin_col=INITIAL_BIN_COL,
        order_col="initial_bin_order",
    ).merge(initial_edges, on=["bin_order", INITIAL_BIN_COL], how="left")

    print(f"3/8 等频初分完成：{actual_initial_bin_count} 箱")

    # 2) 相邻箱合并。
    merge_map = build_merge_map(FINAL_BIN_RANGES, actual_initial_bin_count)
    final_edges = build_final_edge_table(initial_edges, merge_map)

    train_final = apply_merge_map(train_binned, merge_map)
    oot_final = apply_merge_map(oot_binned, merge_map)
    all_final = apply_merge_map(all_binned, merge_map)

    final_train_stats = calc_bin_stats(
        train_final,
        bin_col=FINAL_BIN_COL,
        order_col="bin_order",
    ).merge(
        final_edges,
        left_on=["bin_order", FINAL_BIN_COL],
        right_on=["final_bin_order", FINAL_BIN_COL],
        how="left",
    )

    final_oot_stats = calc_bin_stats(
        oot_final,
        bin_col=FINAL_BIN_COL,
        order_col="bin_order",
    ).merge(
        final_edges,
        left_on=["bin_order", FINAL_BIN_COL],
        right_on=["final_bin_order", FINAL_BIN_COL],
        how="left",
    )

    print(f"4/8 相邻箱合并完成：{len(final_edges)} 档")

    # 3) 基础验证。
    rate_cols = [
        "1m30p_cnt_bad_rate",
        "3m30p_cnt_bad_rate",
        "1m30p_amt_bad_rate",
        "3m30p_amt_bad_rate",
    ]
    monotonicity = pd.concat(
        [
            check_monotonicity(final_train_stats, rate_cols, "train"),
            check_monotonicity(final_oot_stats, rate_cols, "oot"),
        ],
        ignore_index=True,
    )

    psi = calc_population_psi(
        train_final,
        oot_final,
        FINAL_BIN_COL,
        final_edges,
    )
    performance = calc_performance_table(all_final)
    train_oot_compare = build_train_oot_compare(
        final_train_stats,
        final_oot_stats,
        final_edges,
    )

    print(f"5/8 OOT 验证完成：PSI={psi['psi_total'].iloc[0]:.4f}")

    # 4) 阈值与策略。
    threshold_curve = build_threshold_curve(train_final, final_edges)
    strategy_plan = build_strategy_plan(threshold_curve, STRATEGY_CONFIG)
    strategy_segments = build_strategy_segment_report(
        train_final,
        oot_final,
        strategy_plan,
    )
    binning_process = build_binning_process_table(initial_train_stats, merge_map)
    threshold_selection = build_threshold_selection_table(
        threshold_curve,
        strategy_plan,
        STRATEGY_CONFIG,
    )

    print("6/8 阈值曲线、选择依据和默认策略生成完成")

    # 5) 报告。
    overview = build_overview(
        all_data,
        train_final,
        oot_final,
        actual_initial_bin_count,
        len(final_edges),
        psi,
        performance,
        monotonicity,
        strategy_plan,
    )
    config_table = build_config_table()
    metric_dictionary = build_metric_dictionary()

    write_report(
        overview=overview,
        binning_process=binning_process,
        final_train_stats=final_train_stats,
        final_oot_stats=final_oot_stats,
        train_oot_compare=train_oot_compare,
        threshold_selection=threshold_selection,
        strategy_plan=strategy_plan,
        strategy_segments=strategy_segments,
        performance=performance,
        psi=psi,
        monotonicity=monotonicity,
        config_table=config_table,
        metric_dictionary=metric_dictionary,
    )

    print("7/8 Excel 报告写入完成")
    print(f"8/8 运行结束：{REPORT_PATH}")


if __name__ == "__main__":
    main()
