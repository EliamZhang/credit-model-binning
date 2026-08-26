# -*- coding: utf-8 -*-
"""通用工具：字段清理、安全除法、Wilson 置信区间、两比例检验辅助、日志步骤。

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
def wilson_ci(
    bad: np.ndarray,
    mature: np.ndarray,
    z: float = 1.96,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    笔数比例的 95% Wilson 置信区间（下界, 上界）。

    成熟量为 0 时返回 NaN；上界可用于尾部小样本箱的保守风险估计。
    """
    bad_arr = np.asarray(bad, dtype="float64")
    mature_arr = np.asarray(mature, dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        p = safe_div(bad_arr, mature_arr)
        denom = 1.0 + z**2 / mature_arr
        center = (p + z**2 / (2.0 * mature_arr)) / denom
        half = z * np.sqrt(
            np.maximum(p * (1.0 - p) / mature_arr + z**2 / (4.0 * mature_arr**2), 0.0)
        ) / denom
    low = np.maximum(center - half, 0.0)
    high = np.minimum(center + half, 1.0)
    return low, high
def flatten_dict(prefix: str, values: Dict[str, float]) -> Dict[str, float]:
    """将策略约束字典展开为平面字段。"""
    return {f"{prefix}_{key}": value for key, value in values.items()}
def remove_prefix(text: str, prefix: str) -> str:
    """兼容 Python 3.7 的字符串前缀移除。"""
    return text[len(prefix):] if text.startswith(prefix) else text
def _log_step(label: str, t_prev: float) -> float:
    t_now = time.time()
    elapsed = t_now - t_prev
    print(f"  [{label}] 耗时 {elapsed:.1f}s | 累计 {t_now - _log_step._t0:.1f}s")
    return t_now
