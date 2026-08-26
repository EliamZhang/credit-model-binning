# -*- coding: utf-8 -*-
"""月度稳定性：逐月箱级风险与相邻倒挂检查、月度汇总。

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


from pipeline.common import safe_div
from pipeline.risk_metrics import calc_bin_stats



def build_monthly_bin_stability(data: pd.DataFrame) -> pd.DataFrame:
    """按月份、样本组和最终风险档输出箱级稳定性指标。"""
    rows = []
    valid = data.loc[
        data["sample_group"].isin(["train", "oot"])
        & data["application_month"].notna()
        & data[FINAL_BIN_COL].notna()
    ].copy()

    for (sample_group, application_month), month_data in valid.groupby(
        ["sample_group", "application_month"],
        observed=True,
    ):
        stats = calc_bin_stats(
            month_data,
            bin_col=FINAL_BIN_COL,
            order_col="bin_order",
        )
        stats.insert(0, "application_month", application_month)
        stats.insert(0, "sample_group", sample_group)
        rows.append(stats)

    if not rows:
        return pd.DataFrame()

    result = pd.concat(rows, ignore_index=True)
    result["primary_rate_diff_prev"] = result.groupby(
        ["sample_group", "application_month"],
        observed=True,
    )[PRIMARY_RATE_COL].diff()
    if not HIGH_SCORE_HIGH_RISK:
        result["primary_rate_diff_prev"] = -result["primary_rate_diff_prev"]
    result["primary_inversion_flag"] = (
        result["primary_rate_diff_prev"] < -MONTHLY_INVERSION_TOLERANCE
    )
    return result
def build_monthly_stability_summary(monthly_stats: pd.DataFrame) -> pd.DataFrame:
    """汇总每个月的主风险指标单调性和样本表现。"""
    if monthly_stats.empty:
        return pd.DataFrame()

    return (
        monthly_stats.groupby(["sample_group", "application_month"], observed=True)
        .agg(
            n=("n", "sum"),
            mature_count=(PRIMARY_MATURE_COL, "sum"),
            bad_count=(PRIMARY_BAD_COL, "sum"),
            bin_count=(FINAL_BIN_COL, "nunique"),
            primary_inversion_count=("primary_inversion_flag", "sum"),
            max_primary_rate_drop=("primary_rate_diff_prev", lambda s: float((-s).clip(lower=0).max())),
        )
        .reset_index()
        .assign(
            primary_bad_rate=lambda frame: safe_div(
                frame["bad_count"], frame["mature_count"]
            ),
            primary_monotonic_ok=lambda frame: frame["primary_inversion_count"].eq(0),
        )
    )
