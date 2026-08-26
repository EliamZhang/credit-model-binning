# -*- coding: utf-8 -*-
"""数据加载与样本切分：CSV 读取/清洗、宽表拼接、Train/OOT 切分、历史实际审批漏斗。

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


from pipeline.common import read_csv_clean, require_columns, safe_div



def _actual_funnel_row(data: pd.DataFrame, sample_group: str) -> Dict[str, object]:
    """按 application_id 去重计算 application_info 历史实际审批漏斗。"""
    require_columns(
        data,
        ["application_id", "application_status", "assessment_status", "status"],
        "历史实际审批漏斗",
    )

    application_status = data["application_status"].astype("string")
    assessment_status = data["assessment_status"].astype("string")
    account_status = data["status"].astype("string")

    completed_mask = (
        application_status.notna()
        & ~application_status.isin(INCOMPLETE_STATUSES)
    )
    approved_mask = application_status.str.slice(0, 1).isin(["3", "4"]).fillna(False)
    auto_approved_mask = (
        approved_mask
        & assessment_status.str.contains("Auto Approved", na=False, regex=False)
    )
    manual_approved_mask = (
        approved_mask
        & assessment_status.str.contains("Manual Approved", na=False, regex=False)
    )
    deal_mask = account_status.isin(["Active_Account", "Closed", "Blocked"]).fillna(False)

    def unique_count(mask: Optional[pd.Series] = None) -> int:
        selected = data if mask is None else data.loc[mask]
        return int(selected["application_id"].nunique(dropna=True))

    apply_cnt = unique_count()
    completed_cnt = unique_count(completed_mask)
    approved_cnt = unique_count(approved_mask)
    auto_approved_cnt = unique_count(auto_approved_mask)
    manual_approved_cnt = unique_count(manual_approved_mask)
    deal_cnt = unique_count(deal_mask)

    return {
        "metric_scope": "历史实际审批漏斗（application_info）",
        "sample_group": sample_group,
        "actual_apply_cnt": apply_cnt,
        "actual_completed_application_cnt": completed_cnt,
        "actual_approved_application_cnt": approved_cnt,
        "actual_auto_approved_application_cnt": auto_approved_cnt,
        "actual_manual_approved_application_cnt": manual_approved_cnt,
        "actual_deal_sample_cnt": deal_cnt,
        "actual_completion_rate": safe_div(completed_cnt, apply_cnt),
        "actual_approval_rate": safe_div(approved_cnt, completed_cnt),
        "actual_auto_approval_rate": safe_div(auto_approved_cnt, completed_cnt),
        "actual_manual_approval_rate": safe_div(manual_approved_cnt, completed_cnt),
        "actual_auto_approval_share": safe_div(auto_approved_cnt, approved_cnt),
        "actual_manual_approval_share": safe_div(manual_approved_cnt, approved_cnt),
        "actual_deal_rate": safe_div(deal_cnt, approved_cnt),
    }
def build_actual_funnel_report(data: pd.DataFrame) -> pd.DataFrame:
    """分别输出 Train、OOT 与全量的历史实际审批漏斗。"""
    month = data["application_month"].astype("string")
    groups = [
        ("Train", data.loc[month.notna() & month.le(TRAIN_END_MONTH)]),
        ("OOT", data.loc[month.notna() & month.ge(OOT_START_MONTH)]),
        ("All", data),
    ]
    return pd.DataFrame([_actual_funnel_row(frame, label) for label, frame in groups])
def build_bin_actual_funnel_report(data: pd.DataFrame) -> pd.DataFrame:
    """按最终风险档输出历史实际审批漏斗，供箱级结果表下钻使用。"""
    require_columns(
        data,
        [FINAL_BIN_COL, "bin_order"],
        "箱级历史实际审批漏斗",
    )
    rows = []
    grouped = data.loc[data[FINAL_BIN_COL].notna()].groupby(
        ["bin_order", FINAL_BIN_COL],
        observed=True,
        sort=True,
    )
    for (bin_order, risk_bin), frame in grouped:
        row = _actual_funnel_row(frame, str(risk_bin))
        row.pop("metric_scope", None)
        row.pop("sample_group", None)
        row.update(
            {
                "bin_order": int(bin_order),
                FINAL_BIN_COL: str(risk_bin),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)
def drop_incomplete_applications(data: pd.DataFrame) -> pd.DataFrame:
    """剔除 application_status 为未完成的申请，返回剔除后的副本。"""
    require_columns(data, ["application_status"], "剔除未完成申请")
    incomplete = data["application_status"].astype("string").isin(INCOMPLETE_STATUSES)
    return data.loc[~incomplete].copy()
def load_analysis_data() -> pd.DataFrame:
    """加载首版分箱真正需要的数据，删除未使用的其他模型表和交易特征表。"""
    print("加载数据 ...")

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

    # 未完成申请（0.Incomplete / 1.In Progress）整体剔除，不进入任何分析。
    source_row_count = len(data)
    removed_incomplete_count = int(
        data["application_status"].astype("string").isin(INCOMPLETE_STATUSES).sum()
    )
    data = drop_incomplete_applications(data)

    # 历史实际审批漏斗独立于模型分，先基于剔除后的完整申请样本计算并保留。
    actual_funnel_report = build_actual_funnel_report(data)

    # 分箱与模型策略测算只使用存在模型分的样本；缺失比例在总览中单独展示。
    score_missing_count = int(data[SCORE_COL].isna().sum())

    data = data.loc[data[SCORE_COL].notna()].copy()
    if data.empty:
        raise ValueError(f"{SCORE_COL} 全为空，无法进行分箱")

    data.attrs["source_row_count"] = source_row_count
    data.attrs["removed_incomplete_count"] = removed_incomplete_count
    data.attrs["score_missing_count"] = score_missing_count
    data.attrs["actual_funnel_report"] = actual_funnel_report

    print(
        f"   原始 {source_row_count:,} 行；剔除未完成申请 {removed_incomplete_count:,} 行；"
        f"有效模型分 {len(data):,} 行；模型分缺失 {score_missing_count:,} 行"
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
        default="",
    )

    train = result.loc[result["sample_group"].eq("train")].copy()
    oot = result.loc[result["sample_group"].eq("oot")].copy()

    if train.empty:
        raise ValueError("Train 样本为空，请检查 TRAIN_END_MONTH 和 application_month")
    if oot.empty:
        raise ValueError("OOT 样本为空，请检查 OOT_START_MONTH 和 application_month")

    print(f"样本切分完成：Train {len(train):,} 行，OOT {len(oot):,} 行")
    return result, train, oot
