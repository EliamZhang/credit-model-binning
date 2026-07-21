# -*- coding: utf-8 -*-
"""
模型分数分箱与策略阈值分析

默认假设：模型分数越高，风险越高。

主要流程：
1. 加载并校验样本、申请信息和模型分数据；
2. 按时间切分 Train / OOT；
3. 在 Train 上进行等频初分，并将边界复用于 OOT；
4. 根据样本量、成熟量、坏样本量和风险单调性自动合并相邻箱；
5. 计算 1M5、3M30+ 的笔数和金额风险指标；
6. 验证 PSI、AUC、KS、跨月稳定性和边界取整影响；
7. 动态生成阈值曲线、三套探索性策略方案和敏感性分析；
8. 输出 Excel 策略报告。

运行方式：
    python binning.py

可选参数：
    python binning.py --data-dir ./res --out-dir ./out \
        --train-end-month 2026-03 --oot-start-month 2026-04

在 Notebook 中使用：
    from binning import PipelineConfig, run_pipeline
    results = run_pipeline(PipelineConfig())

说明：
- 本代码用于模型分层与策略分析，不应直接替代正式的策略审批流程。
- 默认策略风险约束根据本次 Train 风险水平动态生成，属于探索性分析口径；
  上线前应替换为业务确认后的风险、收益、EL 和人工审核产能约束。
"""

from __future__ import annotations

import argparse
import logging
import math
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency, norm

try:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError as exc:  # pragma: no cover - 运行环境依赖检查
    raise ImportError("缺少 openpyxl，请先执行：pip install openpyxl") from exc


# ============================================================
# 0. 配置区 —— 所有可调参数集中在这里
# ============================================================
#
# 【使用方式】
#   方式一（命令行）：直接修改下方默认值，然后运行 python binning.py
#   方式二（命令行传参）：
#       python binning.py --data-dir ./res --out-dir ./out \
#           --train-end-month 2026-03 --oot-start-month 2026-04 \
#           --initial-bins 20 --min-final-bins 5 --max-final-bins 8
#   方式三（Notebook 编程调用）：
#       from binning import PipelineConfig, run_pipeline
#       config = PipelineConfig(train_end_month="2026-03", initial_bins=20)
#       results = run_pipeline(config)
#
# 【参数分类速查】
#   - 文件路径：data_dir, out_dir, sample_file, application_file, score_file, report_name
#   - 字段映射：score_source_col, score_col, application_id_col, user_id_col
#   - 时间窗口：train_end_month, oot_start_month
#   - 分箱参数：initial_bins, min_final_bins, max_final_bins, min_bin_n, min_mature_n, min_bad_n
#   - 边界取整：rounded_decimals_preferred, rounded_decimals_max
#   - 缺失分数处理：score_missing_decision（可选值：自动通过 / 人工审核 / 拒绝）
#   - 策略方案预设：strategy_presets（名称、目标描述、风险倍数）
#   - 敏感性分析：manual_review_caps（人工审核产能上限）
#   - 验证阈值：psi_low_threshold, psi_high_threshold, auc_drop_threshold
#
# 【风险标的（RiskTarget）】
#   如需增删或修改风险指标，调整下方的 EARLY_TARGET / PRIMARY_TARGET 和 RISK_TARGETS。
#   每个 RiskTarget 包含：
#     - key:           指标缩写，用于内部列名
#     - display_name:  报告中的展示名
#     - label_col:     0/1 标签列名；如不存在，自动根据 dpd_col >= dpd_threshold 派生
#     - dpd_col:       对应 MOB 的 ever DPD 列名
#     - dpd_threshold: 坏样本的 DPD 天数阈值
#     - balance_col:   坏样本在对应 MOB 时点的剩余本金列名
#
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger("binning")


# --- 风险标的定义 ---

@dataclass(frozen=True)
class RiskTarget:
    """风险标的配置。"""

    key: str
    display_name: str
    label_col: str
    dpd_col: str
    dpd_threshold: int
    balance_col: str


EARLY_TARGET = RiskTarget(
    key="1m5p",
    display_name="1M5",
    label_col="duedate_1m_5",
    dpd_col="dpd_days_ever_mob1",
    dpd_threshold=5,
    balance_col="estimate_principal_remaining_mob1",
)

PRIMARY_TARGET = RiskTarget(
    key="3m30p",
    display_name="MOB3 30+",
    label_col="duedate_3m_30",
    dpd_col="dpd_days_ever_mob3",
    dpd_threshold=30,
    balance_col="estimate_principal_remaining_mob3",
)

# 需要分析的风险标的列表；增删标的只需修改这里
RISK_TARGETS: tuple[RiskTarget, ...] = (EARLY_TARGET, PRIMARY_TARGET)


# --- 主流程配置 ---

@dataclass
class PipelineConfig:
    """主流程配置 —— 所有可调参数及其默认值。"""

    # ========== 文件路径 ==========
    # 数据目录，默认在项目根目录下的 res 文件夹
    data_dir: Path = field(default_factory=lambda: BASE_DIR / "res")
    # 输出目录，默认在项目根目录下的 out 文件夹
    out_dir: Path = field(default_factory=lambda: BASE_DIR / "out")

    # 三个输入文件名（放在 data_dir 下）
    sample_file: str = "sample.csv"
    application_file: str = "application_info.csv"
    score_file: str = "aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv"

    # 输出的 Excel 报告文件名
    report_name: str = "策略报告.xlsx"

    # ========== 字段映射 ==========
    # score 表中的原始分数字段名
    score_source_col: str = "aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score"
    # 合并到分析表后使用的分数字段名
    score_col: str = "score_mlt"
    # 申请 ID 和用户 ID 字段名（用于关联和去重）
    application_id_col: str = "application_id"
    user_id_col: str = "user_id"

    # ========== 时间窗口 ==========
    # 格式：YYYY-MM；Train 截止月份（含），OOT 起始月份（含）
    train_end_month: str = "2026-03"
    oot_start_month: str = "2026-04"

    # ========== 分箱参数 ==========
    # 等频初分的箱数
    initial_bins: int = 20
    # 最终合箱后的最少 / 最多箱数
    min_final_bins: int = 5
    max_final_bins: int = 8
    # 单个箱的最小样本量 / 最小成熟样本量 / 最小坏样本数（低于阈值会触发合箱）
    min_bin_n: int = 1000
    min_mature_n: int = 1000
    min_bad_n: int = 30

    # ========== 边界取整 ==========
    # 优先尝试的小数位数 / 最大尝试的小数位数
    rounded_decimals_preferred: int = 4
    rounded_decimals_max: int = 8

    # ========== 缺失分数处理 ==========
    # 可选值："自动通过" / "人工审核" / "拒绝"
    score_missing_decision: str = "人工审核"

    # ========== 策略方案预设 ==========
    # 每项：(方案名称, 目标描述, 自动通过风险倍数, 可接受风险倍数, 边际风险倍数)
    # 风险倍数 = 该方案允许的最大风险率 / Train 总体风险率
    # 设为 None 则根据 Train 总体风险水平动态生成三套探索性方案
    strategy_configs: list[dict[str, Any]] | None = None

    # 当 strategy_configs 为 None 时自动生成的方案预设
    strategy_presets: tuple[tuple[str, str, float, float, float], ...] = (
        ("保守方案", "优先控制风险，覆盖低风险核心人群", 0.65, 0.80, 1.10),
        ("平衡方案", "平衡通过规模、累计风险和边际风险", 0.78, 0.98, 1.40),
        ("增长方案", "在总体风险附近扩大接纳规模", 0.90, 1.12, 1.80),
    )

    # ========== 阈值敏感性分析 ==========
    # 人工审核产能上限（占总体比例），会逐一扫描
    manual_review_caps: tuple[float, ...] = (0.20, 0.25, 0.30)

    # 接纳人群 3M30+ 风险倍数的扫描范围（相对于 Train 总体风险率）
    sensitivity_primary_cap_ratios: tuple[float, ...] = (0.80, 1.00, 1.20)

    # 分位点阈值曲线的细粒度（分位点数量）
    quantile_threshold_count: int = 100

    # ========== 验证阈值 ==========
    # PSI < psi_low  → 稳定；psi_low ≤ PSI < psi_high → 中等；PSI ≥ psi_high → 偏高
    psi_low_threshold: float = 0.10
    psi_high_threshold: float = 0.25
    # OOT AUC 相对 Train 下降超过此值则告警
    auc_drop_threshold: float = 0.05

    # ========== 其他 ==========
    verbose_tables: bool = False

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.out_dir = Path(self.out_dir)
        if self.initial_bins < 2:
            raise ValueError("initial_bins 必须至少为 2")
        if not 2 <= self.min_final_bins <= self.max_final_bins:
            raise ValueError("需要满足 2 <= min_final_bins <= max_final_bins")
        if self.max_final_bins > self.initial_bins:
            self.max_final_bins = self.initial_bins
        if self.score_missing_decision not in {"自动通过", "人工审核", "拒绝"}:
            raise ValueError("score_missing_decision 只能是：自动通过、人工审核、拒绝")

    @property
    def report_path(self) -> Path:
        return self.out_dir / self.report_name


@dataclass
class PipelineResults:
    """主流程返回对象，方便 Notebook 直接读取各类中间表。"""

    config: PipelineConfig
    data: pd.DataFrame
    train: pd.DataFrame
    oot: pd.DataFrame
    data_quality: pd.DataFrame
    key_checks: pd.Series
    split_summary: pd.DataFrame
    target_derivation_summary: pd.DataFrame

    initial_edges: np.ndarray
    initial_edge_table: pd.DataFrame
    initial_stats: pd.DataFrame
    initial_diagnosis: pd.DataFrame

    merge_map: pd.DataFrame
    merge_trace: pd.DataFrame
    final_edges: pd.DataFrame
    train_final: pd.DataFrame
    oot_final: pd.DataFrame
    all_final: pd.DataFrame
    train_final_stats: pd.DataFrame
    oot_final_stats: pd.DataFrame

    monotonicity_train: pd.DataFrame
    monotonicity_oot: pd.DataFrame
    psi: pd.DataFrame
    performance_by_group: pd.DataFrame
    monthly_stats: pd.DataFrame
    monthly_summary: pd.DataFrame
    monthly_performance: pd.DataFrame
    validation_decision: pd.Series

    rounded_edges: pd.DataFrame
    rounded_boundary_comparison: pd.DataFrame
    train_rounded: pd.DataFrame
    oot_rounded: pd.DataFrame
    rounded_train_stats: pd.DataFrame
    rounded_oot_stats: pd.DataFrame

    threshold_curve_final_bins: pd.DataFrame
    threshold_curve_quantile: pd.DataFrame
    strategy_configs: list[dict[str, Any]]
    strategy_plan: pd.DataFrame
    strategy_segment_report: pd.DataFrame
    strategy_recommendation: pd.Series
    threshold_sensitivity: pd.DataFrame
    threshold_sensitivity_matrix: pd.DataFrame

    adjacent_tests_early: pd.DataFrame
    adjacent_tests_primary: pd.DataFrame
    report_path: Path


# --- Excel 报告样式 ---
# 如需调整报告配色和字体，修改以下常量即可
HEADER_FILL = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
HEADER_FONT = Font(name="Microsoft YaHei", bold=True, color="FFFFFF", size=10)
TITLE_FILL = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid")
TITLE_FONT = Font(name="Microsoft YaHei", bold=True, size=12, color="1F4E79")
DATA_FONT = Font(name="Microsoft YaHei", size=9)
THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)
# 自动识别为百分比的列名关键字
PCT_KEYWORDS = [
    "rate",
    "pct",
    "share",
    "lift",
    "coverage",
    "portion",
    "gap",
    "delta",
]


# ============================================================
# 1. 通用辅助函数
# ============================================================


def configure_logging(level: int = logging.INFO) -> None:
    """初始化日志。"""

    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    LOGGER.setLevel(level)



def clean_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """清理 UTF-8 BOM、首尾空格和少数 CSV 头部乱码。"""

    out = frame.copy()
    out.columns = [
        str(col).lstrip("\ufeff").lstrip("ï»¿").strip()
        for col in out.columns
    ]
    return out



def read_csv_clean(path: Path, **kwargs: Any) -> pd.DataFrame:
    """读取 CSV，并给出更明确的文件错误。"""

    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    frame = pd.read_csv(path, low_memory=False, **kwargs)
    return clean_columns(frame)



def safe_div(num: Any, den: Any) -> Any:
    """支持标量、数组和 Series 的安全除法，分母为 0 时返回 NaN。"""

    num_arr = np.asarray(num, dtype="float64")
    den_arr = np.asarray(den, dtype="float64")
    result = np.full(np.broadcast(num_arr, den_arr).shape, np.nan, dtype="float64")
    np.divide(num_arr, den_arr, out=result, where=np.isfinite(den_arr) & (den_arr != 0))

    if np.ndim(result) == 0:
        return float(result)
    if isinstance(num, pd.Series):
        return pd.Series(result, index=num.index)
    if isinstance(den, pd.Series):
        return pd.Series(result, index=den.index)
    return result



def wilson_ci(
    numerator: pd.Series | Sequence[float],
    denominator: pd.Series | Sequence[float],
    alpha: float = 0.05,
) -> tuple[pd.Series, pd.Series]:
    """二项比例 Wilson 置信区间。"""

    numerator_s = pd.Series(numerator, dtype="float64")
    denominator_s = pd.Series(denominator, dtype="float64")
    z = norm.ppf(1 - alpha / 2)
    p = safe_div(numerator_s, denominator_s)

    lower = pd.Series(np.nan, index=numerator_s.index, dtype="float64")
    upper = pd.Series(np.nan, index=numerator_s.index, dtype="float64")
    valid = denominator_s.gt(0) & numerator_s.ge(0) & numerator_s.le(denominator_s)
    if not valid.any():
        return lower, upper

    den_valid = denominator_s.loc[valid]
    p_valid = p.loc[valid]
    adjusted_den = 1 + z**2 / den_valid
    center = (p_valid + z**2 / (2 * den_valid)) / adjusted_den
    margin = (
        z
        * np.sqrt((p_valid * (1 - p_valid) + z**2 / (4 * den_valid)) / den_valid)
        / adjusted_den
    )
    lower.loc[valid] = (center - margin).clip(lower=0)
    upper.loc[valid] = (center + margin).clip(upper=1)
    return lower, upper



def require_columns(frame: pd.DataFrame, columns: Iterable[str], context: str = "dataframe") -> None:
    """校验字段是否存在。"""

    missing = [col for col in columns if col not in frame.columns]
    if missing:
        raise ValueError(f"{context} 缺少必要字段：{missing}")



def get_first_value(
    frame: pd.DataFrame,
    mask: pd.Series,
    column: str,
    default: Any = np.nan,
) -> Any:
    """安全获取筛选结果的第一个值，避免空表直接 iloc[0]。"""

    if column not in frame.columns:
        return default
    selected = frame.loc[mask, column]
    if selected.empty:
        return default
    return selected.iloc[0]



def normalize_month(series: pd.Series) -> pd.Series:
    """将各种年月格式统一为 Period[M]。"""

    text = series.astype("string").str.strip()
    parsed = pd.to_datetime(text, errors="coerce")
    return parsed.dt.to_period("M")



def print_table(name: str, frame: pd.DataFrame | pd.Series, enabled: bool = False) -> None:
    """命令行按需打印表格，不覆盖 IPython 的 display。"""

    if not enabled:
        return
    print(f"\n===== {name} =====")
    print(frame.to_string())



def _row_conflict_count(frame: pd.DataFrame, key_cols: Sequence[str]) -> int:
    """统计重复键中存在非键字段冲突的键数量。"""

    non_key_cols = [col for col in frame.columns if col not in key_cols]
    if not non_key_cols:
        return 0
    duplicated = frame.loc[frame.duplicated(list(key_cols), keep=False)]
    if duplicated.empty:
        return 0
    conflicts = 0
    for _, group in duplicated.groupby(list(key_cols), dropna=False, sort=False):
        if any(group[col].nunique(dropna=False) > 1 for col in non_key_cols):
            conflicts += 1
    return conflicts



def deduplicate_latest(
    frame: pd.DataFrame,
    key_cols: Sequence[str],
    context: str,
    timestamp_candidates: Sequence[str] = (
        "updated_at",
        "update_time",
        "dw_upd_date",
        "send_time",
        "sample_datetime",
        "created_at",
        "dw_cre_date",
    ),
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    按可用时间字段稳定保留最新记录。

    如果重复记录完全一致，直接去重；如果存在冲突但没有任何可用时间字段，
    为避免随机保留错误记录，直接抛出异常。
    """

    require_columns(frame, key_cols, context=context)
    out = frame.copy()
    duplicated_rows = int(out.duplicated(list(key_cols), keep=False).sum())
    duplicated_keys = int(
        out.loc[out.duplicated(list(key_cols), keep=False), list(key_cols)]
        .drop_duplicates()
        .shape[0]
    )
    conflict_keys = _row_conflict_count(out, key_cols)

    summary = {
        "context": context,
        "rows_before": len(out),
        "duplicated_rows": duplicated_rows,
        "duplicated_keys": duplicated_keys,
        "conflicting_duplicate_keys": conflict_keys,
        "dedup_sort_column": "",
    }

    if duplicated_keys == 0:
        summary["rows_after"] = len(out)
        return out, summary

    available_ts = [col for col in timestamp_candidates if col in out.columns]
    if available_ts:
        sort_col = available_ts[0]
        parsed_sort = pd.to_datetime(out[sort_col], errors="coerce")
        out = out.assign(_dedup_sort_time=parsed_sort, _dedup_row_order=np.arange(len(out)))
        out = out.sort_values(
            [*key_cols, "_dedup_sort_time", "_dedup_row_order"],
            na_position="first",
            kind="mergesort",
        )
        out = out.drop_duplicates(list(key_cols), keep="last")
        out = out.drop(columns=["_dedup_sort_time", "_dedup_row_order"])
        summary["dedup_sort_column"] = sort_col
    elif conflict_keys == 0:
        out = out.drop_duplicates(list(key_cols), keep="first")
        summary["dedup_sort_column"] = "identical_rows"
    else:
        raise ValueError(
            f"{context} 存在 {conflict_keys} 个重复键且字段值冲突，但没有可用于选择最新记录的时间字段。"
            "请在源数据中提供 updated_at / send_time / sample_datetime 等字段，"
            "或在进入本脚本前明确完成去重。"
        )

    summary["rows_after"] = len(out)
    return out.reset_index(drop=True), summary


# ============================================================
# 2. 数据加载、拼接和风险标签准备
# ============================================================


def _ensure_application_month(frame: pd.DataFrame) -> pd.DataFrame:
    """补齐并规范 application_month。"""

    out = frame.copy()
    if "application_month" in out.columns:
        month = normalize_month(out["application_month"])
    elif "application_time" in out.columns:
        month = pd.to_datetime(out["application_time"], errors="coerce").dt.to_period("M")
    elif "application_date" in out.columns:
        month = pd.to_datetime(out["application_date"], errors="coerce").dt.to_period("M")
    else:
        raise ValueError("缺少 application_month、application_time 和 application_date，无法进行时间切分")
    out["application_month"] = month.astype("string")
    return out



def prepare_target_label(
    frame: pd.DataFrame,
    target: RiskTarget,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    校验或派生风险标签。

    优先使用现成的 0/1 标签；如果标签不存在，则根据对应 MOB 的 ever DPD 派生：
    DPD 非空视为已成熟，达到阈值为坏样本，否则为好样本。
    """

    out = frame.copy()
    require_columns(out, [target.dpd_col], context=f"{target.display_name} 标签准备")
    out[target.dpd_col] = pd.to_numeric(out[target.dpd_col], errors="coerce")

    source = "existing_label"
    invalid_existing = 0
    if target.label_col in out.columns:
        existing = pd.to_numeric(out[target.label_col], errors="coerce")
        invalid_mask = existing.notna() & ~existing.isin([0, 1])
        invalid_existing = int(invalid_mask.sum())
        if invalid_existing:
            LOGGER.warning(
                "%s 中有 %s 条非 0/1 值，已置为空并按 DPD 尝试补齐",
                target.label_col,
                invalid_existing,
            )
            existing = existing.mask(invalid_mask)

        derived = pd.Series(np.nan, index=out.index, dtype="float64")
        mature = out[target.dpd_col].notna()
        derived.loc[mature] = out.loc[mature, target.dpd_col].ge(target.dpd_threshold).astype(int)
        out[target.label_col] = existing.fillna(derived)
        filled_from_dpd = int(existing.isna().sum() - out[target.label_col].isna().sum())
        if filled_from_dpd:
            source = "existing_label_plus_dpd_fill"
    else:
        mature = out[target.dpd_col].notna()
        out[target.label_col] = np.nan
        out.loc[mature, target.label_col] = (
            out.loc[mature, target.dpd_col].ge(target.dpd_threshold).astype(int)
        )
        filled_from_dpd = int(mature.sum())
        source = "derived_from_dpd"
        LOGGER.warning(
            "未找到 %s，已根据 %s >= %s 自动派生 %s。",
            target.label_col,
            target.dpd_col,
            target.dpd_threshold,
            target.display_name,
        )

    out[target.label_col] = pd.to_numeric(out[target.label_col], errors="coerce")
    summary = {
        "target": target.display_name,
        "target_key": target.key,
        "label_col": target.label_col,
        "source": source,
        "invalid_existing_label_cnt": invalid_existing,
        "filled_or_derived_from_dpd_cnt": filled_from_dpd,
        "mature_cnt": int(out[target.label_col].isin([0, 1]).sum()),
        "bad_cnt": int(out[target.label_col].eq(1).sum()),
        "missing_cnt": int(out[target.label_col].isna().sum()),
    }
    return out, summary



def load_and_prepare_data(
    config: PipelineConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.DataFrame]:
    """加载源数据、稳定去重、拼接并准备指标字段。"""

    sample_path = config.data_dir / config.sample_file
    app_path = config.data_dir / config.application_file
    score_path = config.data_dir / config.score_file

    LOGGER.info("读取数据目录：%s", config.data_dir)
    sample = read_csv_clean(sample_path)
    app = read_csv_clean(app_path)
    score = read_csv_clean(score_path)

    id_cols = [config.application_id_col, config.user_id_col]
    require_columns(sample, id_cols, context="sample")
    require_columns(app, id_cols, context="application_info")
    require_columns(score, [config.application_id_col, config.score_source_col], context="score")

    sample, sample_dedup = deduplicate_latest(sample, id_cols, "sample")
    app, app_dedup = deduplicate_latest(app, id_cols, "application_info")
    score, score_dedup = deduplicate_latest(score, [config.application_id_col], "score")

    df = sample.merge(
        app,
        on=id_cols,
        how="left",
        validate="one_to_one",
        suffixes=("", "_app"),
    )

    score_keep = score[[config.application_id_col, config.score_source_col]].copy()
    df = df.merge(
        score_keep,
        on=config.application_id_col,
        how="left",
        validate="one_to_one",
    ).rename(columns={config.score_source_col: config.score_col})

    df = _ensure_application_month(df)

    date_cols = ["application_time", "application_date"]
    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    numeric_cols = {
        config.score_col,
        "principal",
        EARLY_TARGET.dpd_col,
        EARLY_TARGET.balance_col,
        PRIMARY_TARGET.dpd_col,
        PRIMARY_TARGET.balance_col,
        EARLY_TARGET.label_col,
        PRIMARY_TARGET.label_col,
        "LTI",
        "PTI",
        "NSTI",
    }
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    required_analysis_cols = [
        config.application_id_col,
        config.user_id_col,
        "application_month",
        config.score_col,
        "principal",
        EARLY_TARGET.dpd_col,
        EARLY_TARGET.balance_col,
        PRIMARY_TARGET.dpd_col,
        PRIMARY_TARGET.balance_col,
    ]
    require_columns(df, required_analysis_cols, context="拼接后分析数据")

    target_summaries: list[dict[str, Any]] = []
    for target in RISK_TARGETS:
        df, summary = prepare_target_label(df, target)
        target_summaries.append(summary)

    data_quality = pd.DataFrame(
        {
            "column": df.columns,
            "dtype": [str(df[col].dtype) for col in df.columns],
            "missing_cnt": [int(df[col].isna().sum()) for col in df.columns],
            "missing_rate": [float(df[col].isna().mean()) for col in df.columns],
            "nunique": [int(df[col].nunique(dropna=True)) for col in df.columns],
        }
    ).sort_values(["missing_rate", "column"], ascending=[False, True])

    dedup_summary = pd.DataFrame([sample_dedup, app_dedup, score_dedup])
    key_checks = pd.Series(
        {
            "sample_rows_after_dedup": len(sample),
            "application_rows_after_dedup": len(app),
            "score_rows_after_dedup": len(score),
            "analysis_rows": len(df),
            "application_id_nunique": df[config.application_id_col].nunique(dropna=True),
            "application_key_duplicate_cnt": int(df.duplicated(id_cols).sum()),
            "score_missing_cnt": int(df[config.score_col].isna().sum()),
            "score_missing_rate": float(df[config.score_col].isna().mean()),
            "application_month_min": df["application_month"].min(),
            "application_month_max": df["application_month"].max(),
        },
        name="value",
    )

    # 将去重摘要附加到数据质量表尾部，避免仅打印不落表。
    dedup_as_quality = dedup_summary.rename(columns={"context": "column"}).copy()
    dedup_as_quality["dtype"] = "dedup_summary"
    dedup_as_quality["missing_cnt"] = np.nan
    dedup_as_quality["missing_rate"] = np.nan
    dedup_as_quality["nunique"] = np.nan
    data_quality.attrs["dedup_summary"] = dedup_summary

    return df, data_quality.reset_index(drop=True), key_checks, pd.DataFrame(target_summaries)



def split_train_oot(
    data: pd.DataFrame,
    config: PipelineConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """按月切分 Train / OOT，并保留 gap_or_unknown 诊断。"""

    require_columns(data, ["application_month"], context="split_train_oot")
    train_end = pd.Period(config.train_end_month, freq="M")
    oot_start = pd.Period(config.oot_start_month, freq="M")
    if oot_start <= train_end:
        raise ValueError("oot_start_month 必须晚于 train_end_month")

    out = data.copy()
    month = normalize_month(out["application_month"])
    train_mask = month.notna() & month.le(train_end)
    oot_mask = month.notna() & month.ge(oot_start)
    out["sample_group"] = np.select(
        [train_mask.to_numpy(), oot_mask.to_numpy()],
        ["train", "oot"],
        default="gap_or_unknown",
    )

    train = out.loc[out["sample_group"].eq("train")].copy()
    oot = out.loc[out["sample_group"].eq("oot")].copy()
    if train.empty:
        raise ValueError("Train 样本为空，请检查时间配置和 application_month")
    if oot.empty:
        LOGGER.warning("OOT 样本为空，脚本会继续生成 Train 报告，但 OOT 验证项将为空。")

    summary_rows: list[dict[str, Any]] = []
    for group_name, group in out.groupby("sample_group", dropna=False, sort=True):
        row: dict[str, Any] = {
            "sample_group": group_name,
            "n": len(group),
            "application_id_nunique": group[config.application_id_col].nunique(dropna=True),
            "month_min": group["application_month"].min(),
            "month_max": group["application_month"].max(),
            "score_missing_cnt": int(group[config.score_col].isna().sum()),
            "score_missing_rate": float(group[config.score_col].isna().mean()),
        }
        for target in RISK_TARGETS:
            row[f"{target.key}_mature"] = int(group[target.label_col].isin([0, 1]).sum())
            row[f"{target.key}_bad"] = int(group[target.label_col].eq(1).sum())
            row[f"{target.key}_bad_rate"] = safe_div(
                row[f"{target.key}_bad"], row[f"{target.key}_mature"]
            )
        summary_rows.append(row)

    return out, train, oot, pd.DataFrame(summary_rows)

# ============================================================
# 3. 风险指标计算
# ============================================================


def add_metric_work_columns(data: pd.DataFrame) -> pd.DataFrame:
    """统一生成笔数和金额指标所需的中间字段。"""

    required = ["principal"]
    for target in RISK_TARGETS:
        required.extend([target.label_col, target.dpd_col, target.balance_col])
    require_columns(data, required, context="add_metric_work_columns")

    work = data.copy()
    work["principal"] = pd.to_numeric(work["principal"], errors="coerce")

    for target in RISK_TARGETS:
        key = target.key
        label = pd.to_numeric(work[target.label_col], errors="coerce")
        dpd = pd.to_numeric(work[target.dpd_col], errors="coerce")
        balance = pd.to_numeric(work[target.balance_col], errors="coerce")

        mature = label.isin([0, 1])
        bad = label.eq(1)
        principal_available = work["principal"].notna()
        balance_available = balance.notna()

        work[f"_{key}_cnt_mature"] = mature
        work[f"_{key}_cnt_bad"] = bad

        # 金额分母仅使用已成熟且 principal 非空的样本，不再将缺失本金静默填 0。
        work[f"_{key}_amt_exposure_available"] = mature & principal_available
        work[f"_{key}_amt_exposure_missing"] = mature & ~principal_available
        work[f"_{key}_amt_exposure_value"] = np.where(
            mature & principal_available,
            work["principal"],
            0.0,
        )

        # 金额分子使用坏样本对应 MOB 时点剩余本金；缺失余额单独计数，不再按 0 处理。
        # 同时校验标签与 DPD 是否一致，便于发现源数据口径问题。
        dpd_bad = dpd.ge(target.dpd_threshold)
        work[f"_{key}_label_dpd_mismatch"] = mature & dpd.notna() & bad.ne(dpd_bad)
        work[f"_{key}_amt_bad_balance_available"] = bad & balance_available
        work[f"_{key}_amt_bad_balance_missing"] = bad & ~balance_available
        work[f"_{key}_amt_bad_value"] = np.where(bad & balance_available, balance, 0.0)

    return work



def calc_frame_metrics(
    data: pd.DataFrame,
    prefix: str = "",
    id_col: str = "application_id",
) -> dict[str, Any]:
    """计算任意样本人群的风险指标。"""

    require_columns(data, [id_col, "principal"], context="calc_frame_metrics")
    work = add_metric_work_columns(data)
    result: dict[str, Any] = {
        f"{prefix}n": len(work),
        f"{prefix}application_id_nunique": int(work[id_col].nunique(dropna=True)),
        f"{prefix}principal_amt": float(work["principal"].sum(min_count=1))
        if work["principal"].notna().any()
        else np.nan,
        f"{prefix}principal_missing_cnt": int(work["principal"].isna().sum()),
    }

    for target in RISK_TARGETS:
        key = target.key
        mature = int(work[f"_{key}_cnt_mature"].sum())
        bad = int(work[f"_{key}_cnt_bad"].sum())
        exposure = float(work[f"_{key}_amt_exposure_value"].sum())
        bad_amt = float(work[f"_{key}_amt_bad_value"].sum())

        result.update(
            {
                f"{prefix}{key}_cnt_mature": mature,
                f"{prefix}{key}_cnt_bad": bad,
                f"{prefix}{key}_cnt_good": mature - bad,
                f"{prefix}{key}_cnt_bad_rate_num": bad,
                f"{prefix}{key}_cnt_bad_rate_den": mature,
                f"{prefix}{key}_cnt_bad_rate": safe_div(bad, mature),
                f"{prefix}{key}_amt_mature_cnt": int(
                    work[f"_{key}_amt_exposure_available"].sum()
                ),
                f"{prefix}{key}_amt_exposure_missing_cnt": int(
                    work[f"_{key}_amt_exposure_missing"].sum()
                ),
                f"{prefix}{key}_amt_exposure": exposure,
                f"{prefix}{key}_amt_bad_balance_available_cnt": int(
                    work[f"_{key}_amt_bad_balance_available"].sum()
                ),
                f"{prefix}{key}_amt_bad_balance_missing_cnt": int(
                    work[f"_{key}_amt_bad_balance_missing"].sum()
                ),
                f"{prefix}{key}_amt_bad": bad_amt,
                f"{prefix}{key}_amt_bad_rate_num": bad_amt,
                f"{prefix}{key}_amt_bad_rate_den": exposure,
                f"{prefix}{key}_amt_bad_rate": safe_div(bad_amt, exposure),
                f"{prefix}{key}_label_dpd_mismatch_cnt": int(
                    work[f"_{key}_label_dpd_mismatch"].sum()
                ),
            }
        )
    return result



def calc_bin_stats(
    data: pd.DataFrame,
    bin_col: str,
    score_col: str | None = None,
    id_col: str = "application_id",
    order_col: str = "bin_order",
    include_missing_bin: bool = False,
) -> pd.DataFrame:
    """
    按分箱计算完整风险指标。

    累计指标从低风险端向高风险端累计。默认不把分数缺失箱纳入累计风险曲线。
    """

    required = [id_col, bin_col, "principal"]
    if score_col is not None:
        required.append(score_col)
    require_columns(data, required, context="calc_bin_stats")

    work = data.copy()
    if not include_missing_bin:
        work = work.loc[work[bin_col].notna() & work[bin_col].ne("MISSING")].copy()
    if work.empty:
        return pd.DataFrame()

    work = add_metric_work_columns(work)
    group_cols = [bin_col]
    if order_col in work.columns:
        group_cols.append(order_col)

    agg_dict: dict[str, tuple[str, str]] = {
        "n": (id_col, "count"),
        "application_id_nunique": (id_col, "nunique"),
        "principal_amt": ("principal", "sum"),
        "principal_missing_cnt": ("principal", lambda s: int(s.isna().sum())),
    }
    for target in RISK_TARGETS:
        key = target.key
        agg_dict.update(
            {
                f"{key}_cnt_mature": (f"_{key}_cnt_mature", "sum"),
                f"{key}_cnt_bad": (f"_{key}_cnt_bad", "sum"),
                f"{key}_amt_mature_cnt": (f"_{key}_amt_exposure_available", "sum"),
                f"{key}_amt_exposure_missing_cnt": (
                    f"_{key}_amt_exposure_missing",
                    "sum",
                ),
                f"{key}_amt_exposure": (f"_{key}_amt_exposure_value", "sum"),
                f"{key}_amt_bad_balance_available_cnt": (
                    f"_{key}_amt_bad_balance_available",
                    "sum",
                ),
                f"{key}_amt_bad_balance_missing_cnt": (
                    f"_{key}_amt_bad_balance_missing",
                    "sum",
                ),
                f"{key}_amt_bad": (f"_{key}_amt_bad_value", "sum"),
                f"{key}_label_dpd_mismatch_cnt": (
                    f"_{key}_label_dpd_mismatch",
                    "sum",
                ),
            }
        )
    if score_col is not None:
        agg_dict.update(
            {
                "score_min": (score_col, "min"),
                "score_max": (score_col, "max"),
                "score_mean": (score_col, "mean"),
            }
        )

    stats = (
        work.groupby(group_cols, dropna=False, observed=True)
        .agg(**agg_dict)
        .reset_index()
    )
    if order_col not in stats.columns:
        stats[order_col] = np.arange(1, len(stats) + 1)
    stats = stats.sort_values(order_col).reset_index(drop=True)

    total_n = int(stats["n"].sum())
    total_principal = float(stats["principal_amt"].sum())
    stats["total_n"] = total_n
    stats["sample_pct_num"] = stats["n"]
    stats["sample_pct_den"] = total_n
    stats["sample_pct"] = safe_div(stats["n"], total_n)
    stats["principal_pct"] = safe_div(stats["principal_amt"], total_principal)

    for target in RISK_TARGETS:
        key = target.key
        stats[f"{key}_cnt_good"] = stats[f"{key}_cnt_mature"] - stats[f"{key}_cnt_bad"]
        stats[f"{key}_cnt_bad_rate_num"] = stats[f"{key}_cnt_bad"]
        stats[f"{key}_cnt_bad_rate_den"] = stats[f"{key}_cnt_mature"]
        stats[f"{key}_cnt_bad_rate"] = safe_div(
            stats[f"{key}_cnt_bad"], stats[f"{key}_cnt_mature"]
        )

        stats[f"{key}_amt_bad_rate_num"] = stats[f"{key}_amt_bad"]
        stats[f"{key}_amt_bad_rate_den"] = stats[f"{key}_amt_exposure"]
        stats[f"{key}_amt_bad_rate"] = safe_div(
            stats[f"{key}_amt_bad"], stats[f"{key}_amt_exposure"]
        )

        overall_cnt_rate = safe_div(
            stats[f"{key}_cnt_bad"].sum(), stats[f"{key}_cnt_mature"].sum()
        )
        overall_amt_rate = safe_div(
            stats[f"{key}_amt_bad"].sum(), stats[f"{key}_amt_exposure"].sum()
        )
        stats[f"{key}_cnt_lift_num"] = stats[f"{key}_cnt_bad_rate"]
        stats[f"{key}_cnt_lift_den"] = overall_cnt_rate
        stats[f"{key}_cnt_lift"] = safe_div(
            stats[f"{key}_cnt_bad_rate"], overall_cnt_rate
        )
        stats[f"{key}_amt_lift_num"] = stats[f"{key}_amt_bad_rate"]
        stats[f"{key}_amt_lift_den"] = overall_amt_rate
        stats[f"{key}_amt_lift"] = safe_div(
            stats[f"{key}_amt_bad_rate"], overall_amt_rate
        )

        stats[f"{key}_cnt_bad_rate_se"] = np.sqrt(
            stats[f"{key}_cnt_bad_rate"]
            * (1 - stats[f"{key}_cnt_bad_rate"])
            / stats[f"{key}_cnt_mature"].where(stats[f"{key}_cnt_mature"].gt(0))
        )
        lower, upper = wilson_ci(
            stats[f"{key}_cnt_bad"], stats[f"{key}_cnt_mature"]
        )
        stats[f"{key}_cnt_bad_rate_ci_lower"] = lower
        stats[f"{key}_cnt_bad_rate_ci_upper"] = upper

    stats["cum_n"] = stats["n"].cumsum()
    stats["cum_principal"] = stats["principal_amt"].cumsum()
    stats["cum_pass_rate_num"] = stats["cum_n"]
    stats["cum_pass_rate_den"] = total_n
    stats["cum_pass_rate"] = safe_div(stats["cum_n"], total_n)
    stats["cum_principal_pct"] = safe_div(stats["cum_principal"], total_principal)

    for target in RISK_TARGETS:
        key = target.key
        stats[f"cum_{key}_cnt_mature"] = stats[f"{key}_cnt_mature"].cumsum()
        stats[f"cum_{key}_cnt_bad"] = stats[f"{key}_cnt_bad"].cumsum()
        stats[f"cum_{key}_cnt_bad_rate_num"] = stats[f"cum_{key}_cnt_bad"]
        stats[f"cum_{key}_cnt_bad_rate_den"] = stats[f"cum_{key}_cnt_mature"]
        stats[f"cum_{key}_cnt_bad_rate"] = safe_div(
            stats[f"cum_{key}_cnt_bad"], stats[f"cum_{key}_cnt_mature"]
        )

        stats[f"cum_{key}_amt_exposure"] = stats[f"{key}_amt_exposure"].cumsum()
        stats[f"cum_{key}_amt_bad"] = stats[f"{key}_amt_bad"].cumsum()
        stats[f"cum_{key}_amt_bad_rate_num"] = stats[f"cum_{key}_amt_bad"]
        stats[f"cum_{key}_amt_bad_rate_den"] = stats[f"cum_{key}_amt_exposure"]
        stats[f"cum_{key}_amt_bad_rate"] = safe_div(
            stats[f"cum_{key}_amt_bad"], stats[f"cum_{key}_amt_exposure"]
        )

    return stats


# ============================================================
# 4. 等频初分与相邻箱自动合并
# ============================================================


def learn_equal_freq_edges(
    data: pd.DataFrame,
    score_col: str,
    n_bins: int = 20,
) -> tuple[np.ndarray, int]:
    """在 Train 上学习等频边界；分数重复时允许实际箱数减少。"""

    require_columns(data, [score_col], context="learn_equal_freq_edges")
    score = pd.to_numeric(data[score_col], errors="coerce").dropna()
    if score.empty:
        raise ValueError(f"{score_col} 全为空，无法分箱")
    if score.nunique() < 2:
        raise ValueError(f"{score_col} 唯一值不足 2 个，无法形成有效分箱")

    q = min(n_bins, int(score.nunique()))
    _, raw_edges = pd.qcut(score, q=q, retbins=True, duplicates="drop")
    edges = np.unique(np.asarray(raw_edges, dtype="float64"))
    if len(edges) < 3:
        raise ValueError(f"{score_col} 实际只能形成 1 个箱，无法继续分析")
    edges[0] = -np.inf
    edges[-1] = np.inf
    actual_bins = len(edges) - 1
    if actual_bins < n_bins:
        LOGGER.warning(
            "期望初分 %s 箱，因重复分数较多实际形成 %s 箱；后续合箱将按实际箱数动态处理。",
            n_bins,
            actual_bins,
        )
    return edges, actual_bins



def build_bin_edge_table(
    edges: Sequence[float],
    bin_prefix: str = "B",
    bin_col: str = "initial_bin",
) -> pd.DataFrame:
    """生成分箱边界表。"""

    rows = []
    for index in range(len(edges) - 1):
        order = index + 1
        rows.append(
            {
                "bin_order": order,
                bin_col: f"{bin_prefix}{order:02d}",
                "score_left": float(edges[index]),
                "score_right": float(edges[index + 1]),
                "interval_rule": "(left, right]",
            }
        )
    return pd.DataFrame(rows)



def apply_edges(
    data: pd.DataFrame,
    score_col: str,
    edges: Sequence[float],
    bin_col: str,
    bin_prefix: str = "B",
    missing_label: str = "MISSING",
) -> pd.DataFrame:
    """应用固定边界；分数缺失单独标记，不混入风险累计箱。"""

    require_columns(data, [score_col], context="apply_edges")
    out = data.copy()
    score = pd.to_numeric(out[score_col], errors="coerce")
    labels = list(range(1, len(edges)))
    cut_result = pd.cut(
        score,
        bins=np.asarray(edges, dtype="float64"),
        labels=labels,
        include_lowest=True,
        right=True,
    )
    out["bin_order"] = cut_result.astype("Int64")
    label_map = {order: f"{bin_prefix}{order:02d}" for order in labels}
    out[bin_col] = out["bin_order"].map(label_map).astype("string")
    out.loc[score.isna(), bin_col] = missing_label
    return out



def diagnose_bin_stats(
    stats: pd.DataFrame,
    config: PipelineConfig,
) -> pd.DataFrame:
    """对初始箱进行样本、成熟、坏样本、倒挂和置信区间诊断。"""

    if stats.empty:
        return pd.DataFrame()
    diag = stats.sort_values("bin_order").reset_index(drop=True).copy()

    diag["low_bin_n_flag"] = diag["n"].lt(config.min_bin_n)
    flag_cols = ["low_bin_n_flag"]

    for target in RISK_TARGETS:
        key = target.key
        rate_col = f"{key}_cnt_bad_rate"
        mature_col = f"{key}_cnt_mature"
        bad_col = f"{key}_cnt_bad"
        lower_col = f"{key}_cnt_bad_rate_ci_lower"
        upper_col = f"{key}_cnt_bad_rate_ci_upper"

        diag[f"low_{key}_mature_flag"] = diag[mature_col].lt(config.min_mature_n)
        diag[f"low_{key}_bad_flag"] = diag[bad_col].lt(config.min_bad_n)
        diag[f"{key}_rate_diff_prev"] = diag[rate_col].diff()
        diag[f"{key}_inversion_flag"] = diag[f"{key}_rate_diff_prev"].lt(0).fillna(False)
        diag[f"{key}_rate_missing_flag"] = diag[rate_col].isna()
        diag[f"{key}_ci_overlap_prev_flag"] = (
            diag[lower_col].notna()
            & diag[upper_col].notna()
            & diag[upper_col].shift(1).notna()
            & diag[lower_col].le(diag[upper_col].shift(1))
            & diag[lower_col].shift(1).le(diag[upper_col])
        )
        flag_cols.extend(
            [
                f"low_{key}_mature_flag",
                f"low_{key}_bad_flag",
                f"{key}_inversion_flag",
                f"{key}_rate_missing_flag",
                f"{key}_ci_overlap_prev_flag",
            ]
        )

    diag["diagnosis_flag_cnt"] = diag[flag_cols].sum(axis=1)

    def collect_flags(row: pd.Series) -> str:
        flags: list[str] = []
        if row["low_bin_n_flag"]:
            flags.append("样本量不足")
        for target in RISK_TARGETS:
            key = target.key
            if row[f"low_{key}_mature_flag"]:
                flags.append(f"{target.display_name}成熟不足")
            if row[f"low_{key}_bad_flag"]:
                flags.append(f"{target.display_name}坏样本不足")
            if row[f"{key}_inversion_flag"]:
                flags.append(f"{target.display_name}倒挂")
            if row[f"{key}_rate_missing_flag"]:
                flags.append(f"{target.display_name}风险率缺失")
            if row[f"{key}_ci_overlap_prev_flag"]:
                flags.append(f"{target.display_name}相邻CI重叠")
        return "；".join(flags) if flags else "OK"

    diag["diagnosis_flags"] = diag.apply(collect_flags, axis=1)
    return diag



def _groups_to_map(groups: Sequence[Sequence[int]], initial_bin_col: str, final_bin_col: str) -> pd.DataFrame:
    """将初始箱组合转换为最终合箱映射。"""

    rows: list[dict[str, Any]] = []
    for final_order, group in enumerate(groups, start=1):
        start_bin = min(group)
        end_bin = max(group)
        final_label = f"G{final_order:02d}"
        merged_from = (
            f"B{start_bin:02d}-B{end_bin:02d}"
            if start_bin != end_bin
            else f"B{start_bin:02d}"
        )
        for initial_order in group:
            rows.append(
                {
                    "initial_bin_order": initial_order,
                    initial_bin_col: f"B{initial_order:02d}",
                    "final_bin_order": final_order,
                    final_bin_col: final_label,
                    "merged_from": merged_from,
                }
            )
    return pd.DataFrame(rows).sort_values("initial_bin_order").reset_index(drop=True)



def apply_merge_map(
    data: pd.DataFrame,
    merge_map: pd.DataFrame,
    initial_bin_col: str,
    final_bin_col: str,
) -> pd.DataFrame:
    """将初始箱映射到最终风险等级，保留分数缺失箱。"""

    require_columns(data, [initial_bin_col], context="apply_merge_map data")
    require_columns(
        merge_map,
        [initial_bin_col, "final_bin_order", final_bin_col],
        context="apply_merge_map merge_map",
    )
    out = data.merge(
        merge_map[[initial_bin_col, "final_bin_order", final_bin_col]],
        on=initial_bin_col,
        how="left",
        validate="many_to_one",
    )
    missing_mask = out[initial_bin_col].eq("MISSING")
    out.loc[missing_mask, final_bin_col] = "MISSING"
    out["bin_order"] = out["final_bin_order"].astype("Int64")
    return out



def _stats_quality_penalty(stats: pd.DataFrame, config: PipelineConfig) -> tuple[float, dict[str, int]]:
    """计算当前合箱方案的质量惩罚；值越小越好。"""

    if stats.empty:
        return float("inf"), {"empty": 1}
    counts: dict[str, int] = {
        "low_n": int(stats["n"].lt(config.min_bin_n).sum()),
        "primary_inversion": 0,
        "early_inversion": 0,
        "missing_rate_bins": 0,
        "low_mature": 0,
        "low_bad": 0,
    }
    for target in RISK_TARGETS:
        key = target.key
        rate = stats[f"{key}_cnt_bad_rate"]
        inversion = int(rate.diff().lt(0).fillna(False).sum())
        missing_rate = int(rate.isna().sum())
        low_mature = int(stats[f"{key}_cnt_mature"].lt(config.min_mature_n).sum())
        low_bad = int(stats[f"{key}_cnt_bad"].lt(config.min_bad_n).sum())
        if target.key == PRIMARY_TARGET.key:
            counts["primary_inversion"] = inversion
        else:
            counts["early_inversion"] = inversion
        counts["missing_rate_bins"] += missing_rate
        counts["low_mature"] += low_mature
        counts["low_bad"] += low_bad

    penalty = (
        counts["primary_inversion"] * 100
        + counts["early_inversion"] * 45
        + counts["missing_rate_bins"] * 60
        + counts["low_n"] * 20
        + counts["low_mature"] * 12
        + counts["low_bad"] * 8
    )
    return float(penalty), counts



def _pair_separation_loss(stats: pd.DataFrame, pair_index: int) -> float:
    """衡量合并某对相邻箱造成的区分度损失，越小越适合合并。"""

    left = stats.iloc[pair_index]
    right = stats.iloc[pair_index + 1]
    loss = 0.0
    weights = {PRIMARY_TARGET.key: 0.70, EARLY_TARGET.key: 0.30}
    for target in RISK_TARGETS:
        key = target.key
        left_rate = left[f"{key}_cnt_bad_rate"]
        right_rate = right[f"{key}_cnt_bad_rate"]
        if pd.isna(left_rate) or pd.isna(right_rate):
            rate_gap = 0.0
        else:
            scale = max(float(left_rate), float(right_rate), 0.005)
            rate_gap = abs(float(right_rate) - float(left_rate)) / scale
        loss += weights[key] * rate_gap

    combined_n = max(float(left["n"] + right["n"]), 1.0)
    # 样本较少的相邻箱更适合合并。
    loss += 0.05 * math.log1p(combined_n)
    return float(loss)



def auto_merge_adjacent_bins(
    train_binned: pd.DataFrame,
    initial_bin_col: str,
    final_bin_col: str,
    score_col: str,
    actual_initial_bins: int,
    config: PipelineConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    自动合并相邻初始箱。

    合箱搜索只基于初始箱聚合统计进行，不在每个候选方案上重复扫描明细数据，
    因此在大样本下也能保持较好的运行效率。
    """

    if actual_initial_bins < 2:
        raise ValueError("初始箱不足 2 个，无法自动合箱")

    base_stats = calc_bin_stats(
        train_binned,
        initial_bin_col,
        score_col=score_col,
    )
    if base_stats.empty:
        raise ValueError("初始分箱统计为空，无法自动合箱")
    base_stats = base_stats.set_index("bin_order", drop=False)

    groups: list[list[int]] = [[order] for order in range(1, actual_initial_bins + 1)]
    trace_rows: list[dict[str, Any]] = []
    max_iterations = max(actual_initial_bins - max(2, config.min_final_bins), 0)

    def aggregate(candidate_groups: Sequence[Sequence[int]]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for final_order, group in enumerate(candidate_groups, start=1):
            source = base_stats.loc[list(group)]
            row: dict[str, Any] = {
                "bin_order": final_order,
                "n": int(source["n"].sum()),
            }
            for target in RISK_TARGETS:
                key = target.key
                mature = int(source[f"{key}_cnt_mature"].sum())
                bad = int(source[f"{key}_cnt_bad"].sum())
                row[f"{key}_cnt_mature"] = mature
                row[f"{key}_cnt_bad"] = bad
                row[f"{key}_cnt_bad_rate"] = safe_div(bad, mature)
            rows.append(row)
        return pd.DataFrame(rows)

    def evaluate(candidate_groups: Sequence[Sequence[int]]) -> tuple[pd.DataFrame, float, dict[str, int]]:
        stats = aggregate(candidate_groups)
        penalty, counts = _stats_quality_penalty(stats, config)
        return stats, penalty, counts

    current_stats, current_penalty, current_counts = evaluate(groups)

    for iteration in range(1, max_iterations + 1):
        current_k = len(groups)
        quality_ok = current_penalty == 0
        if quality_ok and current_k <= config.max_final_bins:
            break
        if current_k <= config.min_final_bins:
            LOGGER.warning(
                "已达到最少 %s 箱，但仍存在质量诊断项：%s",
                config.min_final_bins,
                current_counts,
            )
            break

        candidate_rows: list[dict[str, Any]] = []
        for pair_index in range(current_k - 1):
            candidate_groups = [list(group) for group in groups]
            merged_group = candidate_groups[pair_index] + candidate_groups[pair_index + 1]
            candidate_groups = (
                candidate_groups[:pair_index]
                + [merged_group]
                + candidate_groups[pair_index + 2 :]
            )
            candidate_stats, penalty, counts = evaluate(candidate_groups)
            separation_loss = _pair_separation_loss(current_stats, pair_index)
            objective = penalty * 10000 + separation_loss
            candidate_rows.append(
                {
                    "pair_index": pair_index,
                    "left_group": groups[pair_index],
                    "right_group": groups[pair_index + 1],
                    "candidate_groups": candidate_groups,
                    "candidate_stats": candidate_stats,
                    "penalty": penalty,
                    "counts": counts,
                    "separation_loss": separation_loss,
                    "objective": objective,
                }
            )

        best = min(candidate_rows, key=lambda row: row["objective"])
        previous_penalty = current_penalty
        groups = best["candidate_groups"]
        current_stats = best["candidate_stats"]
        current_penalty = float(best["penalty"])
        current_counts = dict(best["counts"])
        merged_values = best["left_group"] + best["right_group"]

        trace_rows.append(
            {
                "iteration": iteration,
                "bin_count_before": current_k,
                "bin_count_after": len(groups),
                "merged_initial_bins": f"B{min(merged_values):02d}-B{max(merged_values):02d}",
                "penalty_before": previous_penalty,
                "penalty_after": current_penalty,
                "primary_inversion_after": current_counts.get("primary_inversion", 0),
                "early_inversion_after": current_counts.get("early_inversion", 0),
                "low_n_after": current_counts.get("low_n", 0),
                "low_mature_after": current_counts.get("low_mature", 0),
                "low_bad_after": current_counts.get("low_bad", 0),
                "missing_rate_bins_after": current_counts.get("missing_rate_bins", 0),
                "separation_loss": best["separation_loss"],
            }
        )

    merge_map = _groups_to_map(groups, initial_bin_col, final_bin_col)
    return merge_map, pd.DataFrame(trace_rows)


def build_final_edge_table(
    initial_edges: pd.DataFrame,
    merge_map: pd.DataFrame,
    initial_bin_col: str,
    final_bin_col: str,
) -> pd.DataFrame:
    """从初始边界和合箱映射生成最终等级边界。"""

    merged = initial_edges.merge(
        merge_map[
            [initial_bin_col, "final_bin_order", final_bin_col, "merged_from"]
        ],
        on=initial_bin_col,
        how="inner",
        validate="one_to_one",
    )
    final_edges = (
        merged.sort_values("bin_order")
        .groupby(["final_bin_order", final_bin_col, "merged_from"], observed=True)
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
# 5. 稳定性、区分度和显著性验证
# ============================================================


def check_monotonicity(stats: pd.DataFrame, rate_cols: Sequence[str]) -> pd.DataFrame:
    """检查风险率是否随风险等级非递减；缺失率箱不再视为自动通过。"""

    rows: list[dict[str, Any]] = []
    if stats.empty:
        return pd.DataFrame(
            columns=[
                "metric",
                "is_assessable",
                "is_monotonic_non_decreasing",
                "violation_cnt",
                "violation_bins",
                "missing_rate_bin_cnt",
                "missing_rate_bins",
            ]
        )

    ordered = stats.sort_values("bin_order").reset_index(drop=True)
    for col in rate_cols:
        if col not in ordered.columns:
            rows.append(
                {
                    "metric": col,
                    "is_assessable": False,
                    "is_monotonic_non_decreasing": pd.NA,
                    "violation_cnt": np.nan,
                    "violation_bins": "",
                    "missing_rate_bin_cnt": len(ordered),
                    "missing_rate_bins": "字段不存在",
                }
            )
            continue

        rate = pd.to_numeric(ordered[col], errors="coerce")
        missing_mask = rate.isna()
        valid = ordered.loc[~missing_mask].copy()
        valid_rate = rate.loc[~missing_mask]
        diff = valid_rate.diff()
        violation_mask = diff.lt(0).fillna(False)
        violation_orders = valid.loc[violation_mask, "bin_order"].astype(str).tolist()
        missing_orders = ordered.loc[missing_mask, "bin_order"].astype(str).tolist()
        assessable = len(valid_rate) >= 2 and not missing_mask.any()
        monotonic = bool(not violation_mask.any()) if assessable else pd.NA

        rows.append(
            {
                "metric": col,
                "is_assessable": assessable,
                "is_monotonic_non_decreasing": monotonic,
                "violation_cnt": int(violation_mask.sum()),
                "violation_bins": ",".join(violation_orders),
                "missing_rate_bin_cnt": int(missing_mask.sum()),
                "missing_rate_bins": ",".join(missing_orders),
            }
        )
    return pd.DataFrame(rows)



def calc_population_psi(
    expected_data: pd.DataFrame,
    actual_data: pd.DataFrame,
    bin_col: str,
    base_bins: pd.DataFrame,
    eps: float = 1e-6,
) -> pd.DataFrame:
    """计算 PSI，并将分数缺失作为独立 MISSING 箱。"""

    require_columns(expected_data, [bin_col], context="PSI expected")
    require_columns(actual_data, [bin_col], context="PSI actual")
    require_columns(base_bins, [bin_col, "final_bin_order"], context="PSI base_bins")

    base = base_bins[["final_bin_order", bin_col]].drop_duplicates().copy()
    missing_row = pd.DataFrame({"final_bin_order": [0], bin_col: ["MISSING"]})
    base = pd.concat([missing_row, base], ignore_index=True).drop_duplicates(bin_col)
    base = base.sort_values("final_bin_order").reset_index(drop=True)

    expected_labels = expected_data[bin_col].fillna("MISSING").astype("string")
    actual_labels = actual_data[bin_col].fillna("MISSING").astype("string")
    expected_cnt = expected_labels.value_counts(dropna=False).rename("expected_cnt")
    actual_cnt = actual_labels.value_counts(dropna=False).rename("actual_cnt")

    psi = (
        base.merge(expected_cnt, left_on=bin_col, right_index=True, how="left")
        .merge(actual_cnt, left_on=bin_col, right_index=True, how="left")
        .fillna({"expected_cnt": 0, "actual_cnt": 0})
    )
    expected_total = psi["expected_cnt"].sum()
    actual_total = psi["actual_cnt"].sum()
    psi["expected_pct"] = safe_div(psi["expected_cnt"], expected_total)
    psi["actual_pct"] = safe_div(psi["actual_cnt"], actual_total)

    expected_clip = psi["expected_pct"].clip(lower=eps)
    actual_clip = psi["actual_pct"].clip(lower=eps)
    psi["psi_component"] = (actual_clip - expected_clip) * np.log(
        actual_clip / expected_clip
    )
    psi["psi_total"] = float(psi["psi_component"].sum())
    return psi



def calc_auc_ks(data: pd.DataFrame, score_col: str, label_col: str) -> pd.Series:
    """计算高分高风险模型的 AUC 和 KS。"""

    require_columns(data, [score_col, label_col], context="calc_auc_ks")
    work = data[[score_col, label_col]].copy()
    work[score_col] = pd.to_numeric(work[score_col], errors="coerce")
    work[label_col] = pd.to_numeric(work[label_col], errors="coerce")
    work = work.loc[work[score_col].notna() & work[label_col].isin([0, 1])].copy()

    n = len(work)
    bad_cnt = int(work[label_col].eq(1).sum())
    good_cnt = int(work[label_col].eq(0).sum())
    if n == 0 or bad_cnt == 0 or good_cnt == 0:
        return pd.Series(
            {
                "n": n,
                "bad_cnt": bad_cnt,
                "good_cnt": good_cnt,
                "bad_rate": safe_div(bad_cnt, n),
                "auc": np.nan,
                "ks": np.nan,
            }
        )

    ranks = work[score_col].rank(method="average")
    bad_rank_sum = ranks.loc[work[label_col].eq(1)].sum()
    auc = (bad_rank_sum - bad_cnt * (bad_cnt + 1) / 2) / (bad_cnt * good_cnt)

    ordered = work.sort_values(score_col, ascending=False)
    cum_bad = ordered[label_col].eq(1).cumsum() / bad_cnt
    cum_good = ordered[label_col].eq(0).cumsum() / good_cnt
    ks = (cum_bad - cum_good).abs().max()

    return pd.Series(
        {
            "n": n,
            "bad_cnt": bad_cnt,
            "good_cnt": good_cnt,
            "bad_rate": safe_div(bad_cnt, n),
            "auc": float(auc),
            "ks": float(ks),
        }
    )



def calc_perf_by_group(
    data: pd.DataFrame,
    group_col: str,
    score_col: str,
    targets: Sequence[RiskTarget] = RISK_TARGETS,
) -> pd.DataFrame:
    """按样本组或月份计算 AUC / KS。"""

    require_columns(data, [group_col], context="calc_perf_by_group")
    rows: list[dict[str, Any]] = []
    for group_value, group in data.groupby(group_col, dropna=False, observed=True):
        for target in targets:
            result = calc_auc_ks(group, score_col, target.label_col).to_dict()
            result[group_col] = group_value
            result["target"] = target.display_name
            result["target_key"] = target.key
            result["label_col"] = target.label_col
            rows.append(result)
    return pd.DataFrame(rows)



def calc_group_bin_stats(
    data: pd.DataFrame,
    group_col: str,
    bin_col: str,
    score_col: str,
) -> pd.DataFrame:
    """按月份或样本组分别计算最终箱指标。"""

    rows: list[pd.DataFrame] = []
    for group_value, group in data.groupby(group_col, dropna=False, observed=True):
        stats = calc_bin_stats(group, bin_col, score_col=score_col)
        if stats.empty:
            continue
        stats.insert(0, group_col, group_value)
        rows.append(stats)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()



def build_monthly_stability_summary(
    monthly_stats: pd.DataFrame,
    raw_data: pd.DataFrame,
    bin_col: str,
    month_col: str = "application_month",
) -> pd.DataFrame:
    """汇总每月风险排序、成熟度和分数缺失情况。"""

    if monthly_stats.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    rate_cols = [f"{target.key}_cnt_bad_rate" for target in RISK_TARGETS]
    for month, stats in monthly_stats.groupby(month_col, dropna=False, observed=True):
        checks = check_monotonicity(stats, rate_cols)
        raw_month = raw_data.loc[raw_data[month_col].eq(month)]
        row: dict[str, Any] = {
            month_col: month,
            "bin_cnt": int(stats[bin_col].nunique(dropna=True)),
            "n": int(stats["n"].sum()),
            "score_missing_cnt": int(raw_month[bin_col].eq("MISSING").sum()),
            "score_missing_rate": safe_div(
                int(raw_month[bin_col].eq("MISSING").sum()), len(raw_month)
            ),
            "min_bin_n": int(stats["n"].min()),
        }
        for target in RISK_TARGETS:
            key = target.key
            mature = stats[f"{key}_cnt_mature"].sum()
            bad = stats[f"{key}_cnt_bad"].sum()
            row[f"{key}_mature"] = int(mature)
            row[f"{key}_bad_rate"] = safe_div(bad, mature)
            row[f"min_{key}_mature_per_bin"] = int(stats[f"{key}_cnt_mature"].min())
            check = checks.loc[checks["metric"].eq(f"{key}_cnt_bad_rate")]
            row[f"{key}_monotonic_assessable"] = get_first_value(
                check, pd.Series(True, index=check.index), "is_assessable", False
            )
            row[f"{key}_violation_cnt"] = get_first_value(
                check, pd.Series(True, index=check.index), "violation_cnt", np.nan
            )
            row[f"{key}_missing_rate_bin_cnt"] = get_first_value(
                check, pd.Series(True, index=check.index), "missing_rate_bin_cnt", np.nan
            )
        rows.append(row)
    return pd.DataFrame(rows).sort_values(month_col).reset_index(drop=True)



def adjacent_proportion_tests(stats: pd.DataFrame, target: RiskTarget) -> pd.DataFrame:
    """对相邻箱坏样本率进行卡方检验。"""

    if stats.empty or len(stats) < 2:
        return pd.DataFrame()
    key = target.key
    rows: list[dict[str, Any]] = []
    ordered = stats.sort_values("bin_order").reset_index(drop=True)

    for index in range(len(ordered) - 1):
        left = ordered.iloc[index]
        right = ordered.iloc[index + 1]
        left_bad = int(left[f"{key}_cnt_bad"])
        left_good = int(left[f"{key}_cnt_mature"] - left_bad)
        right_bad = int(right[f"{key}_cnt_bad"])
        right_good = int(right[f"{key}_cnt_mature"] - right_bad)
        table = np.array([[left_bad, left_good], [right_bad, right_good]], dtype=float)

        if table.sum() == 0 or (table.sum(axis=0) == 0).any():
            chi2_value = np.nan
            p_value = np.nan
        else:
            try:
                chi2_value, p_value, _, _ = chi2_contingency(table, correction=False)
            except ValueError:
                chi2_value, p_value = np.nan, np.nan

        rows.append(
            {
                "target": target.display_name,
                "left_bin": left.iloc[0],
                "right_bin": right.iloc[0],
                "left_rate": left[f"{key}_cnt_bad_rate"],
                "right_rate": right[f"{key}_cnt_bad_rate"],
                "rate_diff": right[f"{key}_cnt_bad_rate"] - left[f"{key}_cnt_bad_rate"],
                "chi2": chi2_value,
                "p_value": p_value,
                "difference_significant_5pct": bool(p_value < 0.05)
                if pd.notna(p_value)
                else pd.NA,
            }
        )
    return pd.DataFrame(rows)



def build_validation_decision(
    psi: pd.DataFrame,
    mono_train: pd.DataFrame,
    mono_oot: pd.DataFrame,
    perf: pd.DataFrame,
    monthly_summary: pd.DataFrame,
    config: PipelineConfig,
) -> pd.Series:
    """根据实际结果动态生成验证结论。"""

    psi_total = float(psi["psi_total"].iloc[0]) if not psi.empty else np.nan

    def mono_value(table: pd.DataFrame, key: str) -> Any:
        row = table.loc[table["metric"].eq(f"{key}_cnt_bad_rate")]
        if row.empty:
            return pd.NA
        return row["is_monotonic_non_decreasing"].iloc[0]

    train_primary_mono = mono_value(mono_train, PRIMARY_TARGET.key)
    oot_primary_mono = mono_value(mono_oot, PRIMARY_TARGET.key)
    train_early_mono = mono_value(mono_train, EARLY_TARGET.key)
    oot_early_mono = mono_value(mono_oot, EARLY_TARGET.key)

    def optional_bool(value: Any) -> bool | None:
        if value is pd.NA or pd.isna(value):
            return None
        return bool(value)

    train_primary_mono_bool = optional_bool(train_primary_mono)
    oot_primary_mono_bool = optional_bool(oot_primary_mono)

    train_auc = get_first_value(
        perf,
        perf.get("sample_group", pd.Series(index=perf.index, dtype="object")).eq("train")
        & perf.get("target_key", pd.Series(index=perf.index, dtype="object")).eq(PRIMARY_TARGET.key),
        "auc",
    )
    oot_auc = get_first_value(
        perf,
        perf.get("sample_group", pd.Series(index=perf.index, dtype="object")).eq("oot")
        & perf.get("target_key", pd.Series(index=perf.index, dtype="object")).eq(PRIMARY_TARGET.key),
        "auc",
    )

    reasons: list[str] = []
    if pd.notna(psi_total):
        if psi_total < config.psi_low_threshold:
            reasons.append("Train/OOT 分布稳定")
        elif psi_total < config.psi_high_threshold:
            reasons.append("PSI 中等，需持续观察")
        else:
            reasons.append("PSI 偏高，需复核样本漂移")
    if train_primary_mono_bool is True:
        reasons.append("Train 的 MOB3 30+ 单调")
    elif train_primary_mono_bool is False:
        reasons.append("Train 的 MOB3 30+ 仍有倒挂")
    else:
        reasons.append("Train 的 MOB3 30+ 因缺失箱无法完整判断")
    if oot_primary_mono_bool is True:
        reasons.append("OOT 的 MOB3 30+ 单调")
    elif oot_primary_mono_bool is False:
        reasons.append("OOT 的 MOB3 30+ 存在倒挂")
    else:
        reasons.append("OOT 的 MOB3 30+ 暂无法完整判断")

    if pd.notna(train_auc) and pd.notna(oot_auc):
        auc_drop = float(train_auc - oot_auc)
        if auc_drop > config.auc_drop_threshold:
            reasons.append(f"OOT AUC 下降超过 {config.auc_drop_threshold}")
        else:
            reasons.append("OOT AUC 下降可控")
    else:
        auc_drop = np.nan

    monthly_primary_violations = (
        int(monthly_summary[f"{PRIMARY_TARGET.key}_violation_cnt"].fillna(0).gt(0).sum())
        if not monthly_summary.empty
        and f"{PRIMARY_TARGET.key}_violation_cnt" in monthly_summary.columns
        else np.nan
    )

    candidate_ok = (
        train_primary_mono_bool is True
        and (oot_primary_mono_bool is True or oot_primary_mono_bool is None)
        and (pd.isna(psi_total) or psi_total < config.psi_high_threshold)
    )
    recommendation = (
        "当前分箱可作为候选方案进入阈值与业务约束评估"
        if candidate_ok
        else "当前分箱仍需结合倒挂、漂移或成熟度问题进一步复核"
    )

    return pd.Series(
        {
            "psi_total": psi_total,
            "train_1m5_monotonic": train_early_mono,
            "train_3m30p_monotonic": train_primary_mono,
            "oot_1m5_monotonic": oot_early_mono,
            "oot_3m30p_monotonic": oot_primary_mono,
            "train_3m30p_auc": train_auc,
            "oot_3m30p_auc": oot_auc,
            "oot_3m30p_auc_drop": auc_drop,
            "months_with_3m30p_violation": monthly_primary_violations,
            "recommendation": recommendation,
            "reason": "；".join(reasons),
        },
        name="value",
    )

# ============================================================
# 6. 边界取整
# ============================================================


def build_rounded_final_edges(
    final_edges: pd.DataFrame,
    final_bin_col: str,
    preferred_decimals: int = 4,
    max_decimals: int = 8,
) -> tuple[pd.DataFrame, int]:
    """选择能够保持边界严格递增的最少小数位，并生成取整边界。"""

    require_columns(
        final_edges,
        ["final_bin_order", final_bin_col, "score_left", "score_right", "merged_from"],
        context="build_rounded_final_edges",
    )
    ordered = final_edges.sort_values("final_bin_order").reset_index(drop=True).copy()

    for decimals in range(preferred_decimals, max_decimals + 1):
        inner_right = ordered["score_right"].replace(np.inf, np.nan).dropna().round(decimals)
        if inner_right.is_monotonic_increasing and not inner_right.duplicated().any():
            edges = [-np.inf] + inner_right.tolist() + [np.inf]
            rounded = ordered.copy()
            rounded["score_left_rounded"] = edges[:-1]
            rounded["score_right_rounded"] = edges[1:]
            rounded["round_decimals"] = decimals
            rounded["interval_rule_rounded"] = "(left, right]"
            return rounded, decimals

    raise ValueError(
        f"边界在 {preferred_decimals}-{max_decimals} 位小数内均无法保持严格递增，请保留精确边界"
    )



def apply_final_edges(
    data: pd.DataFrame,
    rounded_edges: pd.DataFrame,
    score_col: str,
    source_label_col: str,
    output_bin_col: str,
) -> pd.DataFrame:
    """应用最终或取整后的边界。"""

    require_columns(
        rounded_edges,
        [source_label_col, "final_bin_order", "score_left_rounded", "score_right_rounded"],
        context="apply_final_edges",
    )
    out = data.copy()
    labels = rounded_edges[source_label_col].astype(str).tolist()
    edges = [rounded_edges["score_left_rounded"].iloc[0]] + rounded_edges[
        "score_right_rounded"
    ].tolist()
    score = pd.to_numeric(out[score_col], errors="coerce")
    out[output_bin_col] = pd.cut(
        score,
        bins=np.asarray(edges, dtype="float64"),
        labels=labels,
        include_lowest=True,
        right=True,
    ).astype("string")
    out.loc[score.isna(), output_bin_col] = "MISSING"
    order_map = dict(zip(labels, rounded_edges["final_bin_order"]))
    out["bin_order"] = out[output_bin_col].map(order_map).astype("Int64")
    return out



def compare_boundary_assignments(
    exact_data: pd.DataFrame,
    rounded_data: pd.DataFrame,
    exact_bin_col: str,
    rounded_bin_col: str,
    exact_stats: pd.DataFrame,
    rounded_stats: pd.DataFrame,
) -> pd.DataFrame:
    """比较精确边界与取整边界的样本迁移和风险变化。"""

    exact_labels = exact_data[exact_bin_col].astype("string").reset_index(drop=True)
    rounded_labels = rounded_data[rounded_bin_col].astype("string").reset_index(drop=True)
    shifted_n = int(exact_labels.ne(rounded_labels).sum())
    shifted_pct = safe_div(shifted_n, len(exact_labels))

    compare_cols = [
        "bin_order",
        "n",
        f"{EARLY_TARGET.key}_cnt_bad_rate",
        f"{PRIMARY_TARGET.key}_cnt_bad_rate",
    ]
    detail = exact_stats[compare_cols].merge(
        rounded_stats[compare_cols],
        on="bin_order",
        how="outer",
        suffixes=("_exact", "_rounded"),
    )
    detail["n_delta"] = detail["n_rounded"] - detail["n_exact"]
    for target in RISK_TARGETS:
        key = target.key
        detail[f"{key}_rate_delta"] = (
            detail[f"{key}_cnt_bad_rate_rounded"]
            - detail[f"{key}_cnt_bad_rate_exact"]
        )
    detail["shifted_n_total"] = shifted_n
    detail["shifted_pct_total"] = shifted_pct
    return detail


# ============================================================
# 7. 阈值曲线与策略方案
# ============================================================


def _threshold_metric_snapshot(
    data: pd.DataFrame,
    score_col: str,
    threshold: float,
    previous_threshold: float | None = None,
) -> dict[str, Any]:
    """计算一个阈值下的累计通过和边际新增人群指标。"""

    score = pd.to_numeric(data[score_col], errors="coerce")
    pass_mask = score.notna() & score.le(threshold)
    if previous_threshold is None:
        marginal_mask = pass_mask
    else:
        marginal_mask = score.notna() & score.gt(previous_threshold) & score.le(threshold)

    cumulative = data.loc[pass_mask].copy()
    marginal = data.loc[marginal_mask].copy()
    row: dict[str, Any] = {
        "threshold": float(threshold),
        "prev_threshold": previous_threshold,
    }
    row.update(calc_frame_metrics(cumulative, prefix="cum_"))
    row.update(calc_frame_metrics(marginal, prefix="marginal_"))
    return row



def calc_threshold_curve(
    data: pd.DataFrame,
    score_col: str,
    thresholds: Sequence[float],
) -> pd.DataFrame:
    """按升序候选阈值计算累计风险和边际风险曲线。"""

    require_columns(data, [score_col, "principal"], context="calc_threshold_curve")
    clean_thresholds = (
        pd.Series(thresholds, dtype="float64")
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
        .drop_duplicates()
        .sort_values()
        .tolist()
    )
    if not clean_thresholds:
        raise ValueError("候选阈值为空")

    score = pd.to_numeric(data[score_col], errors="coerce")
    scored_n = int(score.notna().sum())
    all_n = len(data)
    total_principal = pd.to_numeric(data["principal"], errors="coerce").sum(min_count=1)
    scored_principal = pd.to_numeric(
        data.loc[score.notna(), "principal"], errors="coerce"
    ).sum(min_count=1)

    rows: list[dict[str, Any]] = []
    previous: float | None = None
    for order, threshold in enumerate(clean_thresholds, start=1):
        row = _threshold_metric_snapshot(data, score_col, threshold, previous)
        row["threshold_order"] = order
        row["total_n"] = all_n
        row["scored_n"] = scored_n
        row["score_missing_n"] = all_n - scored_n
        row["score_coverage"] = safe_div(scored_n, all_n)
        row["cum_pass_rate"] = safe_div(row["cum_n"], all_n)
        row["cum_pass_rate_scored"] = safe_div(row["cum_n"], scored_n)
        row["marginal_sample_pct"] = safe_div(row["marginal_n"], all_n)
        row["cum_principal_pct"] = safe_div(row["cum_principal_amt"], total_principal)
        row["cum_principal_pct_scored"] = safe_div(
            row["cum_principal_amt"], scored_principal
        )
        rows.append(row)
        previous = threshold

    return pd.DataFrame(rows).sort_values("threshold_order").reset_index(drop=True)



def final_bin_threshold_table(
    final_edges: pd.DataFrame,
    data: pd.DataFrame,
    score_col: str,
    final_bin_col: str,
    right_edge_col: str = "score_right",
) -> pd.DataFrame:
    """使用最终箱右边界作为候选阈值，并正确处理尾箱无穷边界。"""

    require_columns(
        final_edges,
        ["final_bin_order", final_bin_col, right_edge_col, "merged_from"],
        context="final_bin_threshold_table",
    )
    max_score = pd.to_numeric(data[score_col], errors="coerce").max()
    if pd.isna(max_score):
        raise ValueError("训练样本分数全为空，无法生成阈值")

    table = final_edges[
        ["final_bin_order", final_bin_col, right_edge_col, "merged_from"]
    ].copy()
    table["threshold"] = table[right_edge_col].replace(np.inf, float(max_score))
    table = table.loc[table["threshold"].notna()].sort_values("final_bin_order")
    return table.reset_index(drop=True)



def quantile_thresholds(
    data: pd.DataFrame,
    score_col: str,
    n_quantiles: int = 100,
) -> list[float]:
    """生成细粒度分位点候选阈值。"""

    score = pd.to_numeric(data[score_col], errors="coerce").dropna()
    if score.empty:
        return []
    quantiles = np.linspace(0.01, 0.99, max(n_quantiles - 1, 1))
    values = score.quantile(quantiles).drop_duplicates().tolist()
    values.append(float(score.max()))
    return sorted(pd.Series(values, dtype="float64").dropna().unique().tolist())



def build_data_driven_strategy_configs(
    train: pd.DataFrame,
    presets: Sequence[tuple[str, str, float, float, float]] | None = None,
) -> list[dict[str, Any]]:
    """
    根据 Train 的总体风险率生成探索性约束。

    这些约束只用于自动生成分析方案，不能替代业务确认后的正式风险上限。
    """

    if presets is None:
        presets = (
            ("保守方案", "优先控制风险，覆盖低风险核心人群", 0.65, 0.80, 1.10),
            ("平衡方案", "平衡通过规模、累计风险和边际风险", 0.78, 0.98, 1.40),
            ("增长方案", "在总体风险附近扩大接纳规模", 0.90, 1.12, 1.80),
        )

    overall = calc_frame_metrics(train)
    early_rate = overall[f"{EARLY_TARGET.key}_cnt_bad_rate"]
    primary_rate = overall[f"{PRIMARY_TARGET.key}_cnt_bad_rate"]

    if pd.isna(early_rate) or early_rate <= 0:
        early_rate = 0.05
    if pd.isna(primary_rate) or primary_rate <= 0:
        primary_rate = 0.08

    configs: list[dict[str, Any]] = []
    for name, objective, auto_ratio, accept_ratio, marginal_ratio in presets:
        configs.append(
            {
                "strategy_name": name,
                "objective": objective,
                "constraint_source": "data_driven_exploratory",
                "auto_constraints": {
                    f"max_cum_{EARLY_TARGET.key}_cnt_bad_rate": min(
                        early_rate * auto_ratio, 1.0
                    ),
                    f"max_cum_{PRIMARY_TARGET.key}_cnt_bad_rate": min(
                        primary_rate * auto_ratio, 1.0
                    ),
                    f"max_marginal_{PRIMARY_TARGET.key}_cnt_bad_rate": min(
                        primary_rate * marginal_ratio, 1.0
                    ),
                },
                "accept_constraints": {
                    f"max_cum_{EARLY_TARGET.key}_cnt_bad_rate": min(
                        early_rate * accept_ratio, 1.0
                    ),
                    f"max_cum_{PRIMARY_TARGET.key}_cnt_bad_rate": min(
                        primary_rate * accept_ratio, 1.0
                    ),
                    f"max_marginal_{PRIMARY_TARGET.key}_cnt_bad_rate": min(
                        primary_rate * marginal_ratio, 1.0
                    ),
                },
            }
        )
    return configs



def select_threshold_under_constraints(
    curve: pd.DataFrame,
    constraints: Mapping[str, float],
) -> pd.Series | None:
    """选择满足全部约束且通过率最高的阈值。"""

    eligible = curve.copy()
    for constraint_name, max_value in constraints.items():
        metric = constraint_name.removeprefix("max_")
        require_columns(eligible, [metric], context="select_threshold_under_constraints")
        eligible = eligible.loc[eligible[metric].notna() & eligible[metric].le(max_value)]
    if eligible.empty:
        return None
    return eligible.sort_values(
        ["cum_pass_rate", "threshold_order"], ascending=[False, False]
    ).iloc[0]



def calc_score_segment_metrics(
    data: pd.DataFrame,
    score_col: str,
    decision: str,
    auto_threshold: float,
    reject_threshold: float,
    missing_decision: str,
) -> pd.Series:
    """计算自动通过、人工审核或拒绝区间指标，确保缺失分数有明确归属。"""

    require_columns(data, [score_col], context="calc_score_segment_metrics")
    score = pd.to_numeric(data[score_col], errors="coerce")
    missing_mask = score.isna()

    if decision == "自动通过":
        mask = score.notna() & score.le(auto_threshold)
    elif decision == "人工审核":
        mask = score.notna() & score.gt(auto_threshold) & score.le(reject_threshold)
    elif decision == "拒绝":
        mask = score.notna() & score.gt(reject_threshold)
    else:
        raise ValueError(f"未知决策：{decision}")

    if missing_decision == decision:
        mask |= missing_mask

    segment = data.loc[mask].copy()
    metrics = calc_frame_metrics(segment)
    total_n = len(data)
    total_principal = pd.to_numeric(data["principal"], errors="coerce").sum(min_count=1)
    metrics.update(
        {
            "decision": decision,
            "n": len(segment),
            "sample_pct": safe_div(len(segment), total_n),
            "principal_pct": safe_div(metrics.get("principal_amt"), total_principal),
            "score_missing_cnt": int(segment[score_col].isna().sum()),
            "score_missing_included": missing_decision == decision,
        }
    )
    return pd.Series(metrics)



def make_strategy_plan(
    data: pd.DataFrame,
    curve: pd.DataFrame,
    configs: Sequence[Mapping[str, Any]],
    score_col: str,
    final_bin_col: str,
    missing_decision: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """生成三段式策略方案及 Train 分段指标。"""

    plan_rows: list[dict[str, Any]] = []
    segment_rows: list[dict[str, Any]] = []

    for cfg in configs:
        auto_row = select_threshold_under_constraints(curve, cfg["auto_constraints"])
        accept_row = select_threshold_under_constraints(curve, cfg["accept_constraints"])

        if auto_row is None or accept_row is None:
            plan_rows.append(
                {
                    "strategy_name": cfg["strategy_name"],
                    "objective": cfg["objective"],
                    "constraint_source": cfg.get("constraint_source", "manual"),
                    "status": "无满足约束的阈值",
                }
            )
            continue

        if float(accept_row["threshold"]) < float(auto_row["threshold"]):
            accept_row = auto_row

        auto_threshold = float(auto_row["threshold"])
        reject_threshold = float(accept_row["threshold"])
        strategy_segments: dict[str, pd.Series] = {}
        for decision in ["自动通过", "人工审核", "拒绝"]:
            metrics = calc_score_segment_metrics(
                data,
                score_col,
                decision,
                auto_threshold,
                reject_threshold,
                missing_decision,
            )
            strategy_segments[decision] = metrics
            segment_row = metrics.to_dict()
            segment_row.update(
                {
                    "sample_group": "train",
                    "strategy_name": cfg["strategy_name"],
                    "auto_pass_threshold": auto_threshold,
                    "reject_threshold": reject_threshold,
                }
            )
            segment_rows.append(segment_row)

        accepted_data = pd.concat(
            [
                data.loc[
                    pd.to_numeric(data[score_col], errors="coerce").le(reject_threshold)
                    & pd.to_numeric(data[score_col], errors="coerce").notna()
                ],
                data.loc[data[score_col].isna()]
                if missing_decision in {"自动通过", "人工审核"}
                else data.iloc[0:0],
            ],
            ignore_index=False,
        ).drop_duplicates()
        accepted_metrics = calc_frame_metrics(accepted_data, prefix="accepted_")

        auto_metrics = strategy_segments["自动通过"]
        manual_metrics = strategy_segments["人工审核"]
        reject_metrics = strategy_segments["拒绝"]
        plan_row: dict[str, Any] = {
            "strategy_name": cfg["strategy_name"],
            "objective": cfg["objective"],
            "constraint_source": cfg.get("constraint_source", "manual"),
            "status": "OK",
            "auto_pass_threshold": auto_threshold,
            "auto_pass_bin": auto_row.get(final_bin_col, np.nan),
            "reject_threshold": reject_threshold,
            "manual_review_upper_bin": accept_row.get(final_bin_col, np.nan),
            "score_missing_decision": missing_decision,
            "auto_pass_rate": auto_metrics["sample_pct"],
            "manual_review_rate": manual_metrics["sample_pct"],
            "reject_rate": reject_metrics["sample_pct"],
            "accepted_rate": auto_metrics["sample_pct"] + manual_metrics["sample_pct"],
            "segment_rate_sum": auto_metrics["sample_pct"]
            + manual_metrics["sample_pct"]
            + reject_metrics["sample_pct"],
            "last_accepted_marginal_1m5_bad_rate": accept_row.get(
                f"marginal_{EARLY_TARGET.key}_cnt_bad_rate", np.nan
            ),
            "last_accepted_marginal_3m30p_bad_rate": accept_row.get(
                f"marginal_{PRIMARY_TARGET.key}_cnt_bad_rate", np.nan
            ),
        }
        plan_row.update(accepted_metrics)
        plan_rows.append(plan_row)

    return pd.DataFrame(plan_rows), pd.DataFrame(segment_rows)



def make_strategy_segment_report(
    data: pd.DataFrame,
    strategy_plan: pd.DataFrame,
    score_col: str,
    missing_decision: str,
    sample_group_name: str,
) -> pd.DataFrame:
    """在任意样本组复算已有策略方案的三段指标。"""

    rows: list[dict[str, Any]] = []
    if strategy_plan.empty or "status" not in strategy_plan.columns:
        return pd.DataFrame()
    for _, strategy in strategy_plan.loc[strategy_plan["status"].eq("OK")].iterrows():
        for decision in ["自动通过", "人工审核", "拒绝"]:
            metrics = calc_score_segment_metrics(
                data,
                score_col,
                decision,
                float(strategy["auto_pass_threshold"]),
                float(strategy["reject_threshold"]),
                missing_decision,
            )
            row = metrics.to_dict()
            row.update(
                {
                    "sample_group": sample_group_name,
                    "strategy_name": strategy["strategy_name"],
                    "decision": decision,
                    "auto_pass_threshold": strategy["auto_pass_threshold"],
                    "reject_threshold": strategy["reject_threshold"],
                }
            )
            rows.append(row)
    return pd.DataFrame(rows)



def choose_recommended_strategy(
    strategy_plan: pd.DataFrame,
    strategy_segments: pd.DataFrame,
) -> pd.Series:
    """动态推荐优先平衡方案；若不可行，则选择可行方案中通过率最高者。"""

    feasible = strategy_plan.loc[strategy_plan.get("status", pd.Series(dtype="object")).eq("OK")].copy()
    if feasible.empty:
        return pd.Series(
            {
                "recommended_strategy": "无可行方案",
                "recommendation": "当前约束下没有可行阈值，请放宽约束或检查成熟样本。",
            },
            name="value",
        )

    balanced = feasible.loc[feasible["strategy_name"].eq("平衡方案")]
    selected = (
        balanced.iloc[0]
        if not balanced.empty
        else feasible.sort_values("accepted_rate", ascending=False).iloc[0]
    )
    auto_threshold = float(selected["auto_pass_threshold"])
    reject_threshold = float(selected["reject_threshold"])
    segment_sum_ok = abs(float(selected.get("segment_rate_sum", np.nan)) - 1.0) < 1e-8

    return pd.Series(
        {
            "recommended_strategy": selected["strategy_name"],
            "auto_pass_rule": f"score_mlt <= {auto_threshold:.6f}",
            "manual_review_rule": f"{auto_threshold:.6f} < score_mlt <= {reject_threshold:.6f}，并包含分数缺失样本"
            if selected.get("score_missing_decision") == "人工审核"
            else f"{auto_threshold:.6f} < score_mlt <= {reject_threshold:.6f}",
            "reject_rule": f"score_mlt > {reject_threshold:.6f}",
            "auto_pass_rate": selected.get("auto_pass_rate", np.nan),
            "manual_review_rate": selected.get("manual_review_rate", np.nan),
            "reject_rate": selected.get("reject_rate", np.nan),
            "accepted_rate": selected.get("accepted_rate", np.nan),
            "accepted_1m5_bad_rate": selected.get(
                f"accepted_{EARLY_TARGET.key}_cnt_bad_rate", np.nan
            ),
            "accepted_3m30p_bad_rate": selected.get(
                f"accepted_{PRIMARY_TARGET.key}_cnt_bad_rate", np.nan
            ),
            "segment_rate_sum_is_one": segment_sum_ok,
            "recommendation": "该方案由本次数据和探索性约束动态计算；上线前仍需结合收益、EL、人工审核产能和业务风险偏好审批。",
        },
        name="value",
    )


def scan_threshold_sensitivity(
    data: pd.DataFrame,
    curve: pd.DataFrame,
    score_col: str,
    final_bin_col: str,
    missing_decision: str,
    manual_review_caps: Sequence[float],
    accepted_primary_caps: Sequence[float],
) -> pd.DataFrame:
    """扫描人工审核产能与接纳人群 3M30+ 风险上限。"""

    if curve.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    curve = curve.sort_values("threshold_order").reset_index(drop=True)
    total_n = len(data)

    score = pd.to_numeric(data[score_col], errors="coerce")
    missing_data = data.loc[score.isna()]
    missing_metrics = calc_frame_metrics(missing_data, prefix="missing_")
    missing_n = int(missing_metrics["missing_n"])
    missing_primary_mature = int(
        missing_metrics[f"missing_{PRIMARY_TARGET.key}_cnt_mature"]
    )
    missing_primary_bad = int(
        missing_metrics[f"missing_{PRIMARY_TARGET.key}_cnt_bad"]
    )
    missing_early_mature = int(
        missing_metrics[f"missing_{EARLY_TARGET.key}_cnt_mature"]
    )
    missing_early_bad = int(
        missing_metrics[f"missing_{EARLY_TARGET.key}_cnt_bad"]
    )

    missing_in_accepted = missing_decision in {"自动通过", "人工审核"}
    missing_in_manual = missing_decision == "人工审核"
    missing_in_auto = missing_decision == "自动通过"
    missing_in_reject = missing_decision == "拒绝"

    for manual_cap in manual_review_caps:
        for primary_cap in accepted_primary_caps:
            candidates: list[dict[str, Any]] = []
            for auto_index in range(len(curve)):
                auto_row = curve.iloc[auto_index]
                for accept_index in range(auto_index, len(curve)):
                    accept_row = curve.iloc[accept_index]
                    auto_scored_n = int(auto_row["cum_n"])
                    accepted_scored_n = int(accept_row["cum_n"])
                    manual_scored_n = accepted_scored_n - auto_scored_n
                    rejected_scored_n = int(accept_row["scored_n"] - accepted_scored_n)

                    auto_n = auto_scored_n + (missing_n if missing_in_auto else 0)
                    manual_n = manual_scored_n + (missing_n if missing_in_manual else 0)
                    reject_n = rejected_scored_n + (missing_n if missing_in_reject else 0)

                    accepted_primary_mature = int(
                        accept_row[f"cum_{PRIMARY_TARGET.key}_cnt_mature"]
                    ) + (missing_primary_mature if missing_in_accepted else 0)
                    accepted_primary_bad = int(
                        accept_row[f"cum_{PRIMARY_TARGET.key}_cnt_bad"]
                    ) + (missing_primary_bad if missing_in_accepted else 0)
                    accepted_primary_rate = safe_div(
                        accepted_primary_bad, accepted_primary_mature
                    )

                    accepted_early_mature = int(
                        accept_row[f"cum_{EARLY_TARGET.key}_cnt_mature"]
                    ) + (missing_early_mature if missing_in_accepted else 0)
                    accepted_early_bad = int(
                        accept_row[f"cum_{EARLY_TARGET.key}_cnt_bad"]
                    ) + (missing_early_bad if missing_in_accepted else 0)
                    accepted_early_rate = safe_div(
                        accepted_early_bad, accepted_early_mature
                    )

                    manual_rate = safe_div(manual_n, total_n)
                    if (
                        pd.notna(accepted_primary_rate)
                        and accepted_primary_rate <= primary_cap
                        and manual_rate <= manual_cap
                    ):
                        candidates.append(
                            {
                                "auto_pass_threshold": float(auto_row["threshold"]),
                                "reject_threshold": float(accept_row["threshold"]),
                                "auto_pass_bin": auto_row.get(final_bin_col, np.nan),
                                "manual_review_upper_bin": accept_row.get(
                                    final_bin_col, np.nan
                                ),
                                "auto_pass_rate": safe_div(auto_n, total_n),
                                "manual_review_rate": manual_rate,
                                "reject_rate": safe_div(reject_n, total_n),
                                "accepted_rate": safe_div(auto_n + manual_n, total_n),
                                "accepted_3m30p_cnt_bad_rate": accepted_primary_rate,
                                "accepted_1m5_cnt_bad_rate": accepted_early_rate,
                                "last_accepted_marginal_3m30p_cnt_bad_rate": accept_row.get(
                                    f"marginal_{PRIMARY_TARGET.key}_cnt_bad_rate",
                                    np.nan,
                                ),
                            }
                        )

            if candidates:
                selected = sorted(
                    candidates,
                    key=lambda row: (
                        row["accepted_rate"],
                        row["auto_pass_rate"],
                        -row["accepted_3m30p_cnt_bad_rate"],
                    ),
                    reverse=True,
                )[0]
                selected.update(
                    {
                        "max_manual_review_rate": manual_cap,
                        "max_accepted_3m30p_cnt_bad_rate": primary_cap,
                        "status": "OK",
                    }
                )
                rows.append(selected)
            else:
                rows.append(
                    {
                        "max_manual_review_rate": manual_cap,
                        "max_accepted_3m30p_cnt_bad_rate": primary_cap,
                        "status": "无可行方案",
                    }
                )
    return pd.DataFrame(rows)


def build_sensitivity_matrix(scan_result: pd.DataFrame) -> pd.DataFrame:
    """将敏感性扫描结果整理为 accepted_rate 矩阵。"""

    if scan_result.empty:
        return pd.DataFrame()
    matrix_source = scan_result.copy()
    matrix_source["accepted_rate_display"] = matrix_source.get("accepted_rate", np.nan)
    return matrix_source.pivot(
        index="max_manual_review_rate",
        columns="max_accepted_3m30p_cnt_bad_rate",
        values="accepted_rate_display",
    ).sort_index()


# ============================================================
# 8. Excel 报告
# ============================================================

# 报告展示层样式：只影响 Excel 阅读体验，不改变分箱、验证或阈值计算逻辑。
GUIDE_FILL = PatternFill(start_color="E2F0D9", end_color="E2F0D9", fill_type="solid")
WARNING_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
KEY_HEADER_FILL = PatternFill(start_color="2F75B5", end_color="2F75B5", fill_type="solid")
SUBTLE_FILL = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
NOTE_FONT = Font(name="Microsoft YaHei", size=10, color="404040")


def _is_pct_col(name: Any) -> bool:
    """识别需要按百分比展示的字段。"""

    text = str(name).lower()
    chinese_keywords = ["率", "占比", "比例", "覆盖"]
    return any(keyword in text for keyword in PCT_KEYWORDS) or any(
        keyword in str(name) for keyword in chinese_keywords
    )


def _excel_safe_value(value: Any) -> Any:
    """将 pandas / numpy 类型转换为 openpyxl 可写值。"""

    if value is pd.NA or value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, pd.Period):
        return str(value)
    if isinstance(value, (list, tuple, dict, set)):
        return str(value)
    return value


def auto_width(
    ws: openpyxl.worksheet.worksheet.Worksheet,
    min_w: int = 10,
    max_w: int = 46,
) -> None:
    """根据单元格内容自动设置列宽。"""

    for column_cells in ws.columns:
        letter = get_column_letter(column_cells[0].column)
        best = min_w
        for cell in column_cells:
            if cell.value is None:
                continue
            for line in str(cell.value).split("\n"):
                width = sum(2 if ord(char) > 127 else 1 for char in line) + 3
                best = max(best, width)
        ws.column_dimensions[letter].width = min(best, max_w)


def configure_report_sheet(
    ws: openpyxl.worksheet.worksheet.Worksheet,
    freeze_panes: str = "A3",
    zoom: int = 85,
) -> None:
    """统一设置报告工作表的基础阅读样式。"""

    ws.freeze_panes = freeze_panes
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = zoom


def write_note_block(
    ws: openpyxl.worksheet.worksheet.Worksheet,
    start_row: int,
    title: str,
    text: str,
    fill: PatternFill = GUIDE_FILL,
    end_column: int = 8,
) -> int:
    """写入一段合并单元格说明，用于阅读指引或风险提示。"""

    ws.merge_cells(
        start_row=start_row,
        start_column=1,
        end_row=start_row,
        end_column=end_column,
    )
    title_cell = ws.cell(row=start_row, column=1, value=title)
    title_cell.fill = TITLE_FILL
    title_cell.font = TITLE_FONT
    title_cell.alignment = Alignment(horizontal="left", vertical="center")
    for column in range(2, end_column + 1):
        ws.cell(row=start_row, column=column).fill = TITLE_FILL

    content_row = start_row + 1
    ws.merge_cells(
        start_row=content_row,
        start_column=1,
        end_row=content_row,
        end_column=end_column,
    )
    content_cell = ws.cell(row=content_row, column=1, value=text)
    content_cell.fill = fill
    content_cell.font = NOTE_FONT
    content_cell.alignment = Alignment(
        horizontal="left",
        vertical="top",
        wrap_text=True,
    )
    content_cell.border = THIN_BORDER
    ws.row_dimensions[content_row].height = max(42, 18 * (text.count("\n") + 1))
    for column in range(2, end_column + 1):
        ws.cell(row=content_row, column=column).fill = fill
        ws.cell(row=content_row, column=column).border = THIN_BORDER

    return content_row + 2


def write_block(
    ws: openpyxl.worksheet.worksheet.Worksheet,
    start_row: int,
    title: str,
    frame: pd.DataFrame,
    index_label: str = "序号",
    key_columns: Sequence[str] | None = None,
) -> int:
    """写入带标题的 DataFrame 区块，返回下一可用行号。"""

    df = frame.copy()
    key_column_set = set(key_columns or [])
    ncols = max(len(df.columns) + 1, 2)
    ws.merge_cells(start_row=start_row, start_column=1, end_row=start_row, end_column=ncols)
    title_cell = ws.cell(row=start_row, column=1, value=title)
    title_cell.fill = TITLE_FILL
    title_cell.font = TITLE_FONT
    title_cell.alignment = Alignment(horizontal="left", vertical="center")
    for column in range(2, ncols + 1):
        ws.cell(row=start_row, column=column).fill = TITLE_FILL

    header_row = start_row + 1
    ws.cell(row=header_row, column=1, value=index_label)
    for column_index, column_name in enumerate(df.columns, start=2):
        ws.cell(row=header_row, column=column_index, value=str(column_name))
    for column in range(1, ncols + 1):
        cell = ws.cell(row=header_row, column=column)
        column_name = index_label if column == 1 else str(df.columns[column - 2])
        cell.fill = KEY_HEADER_FILL if column_name in key_column_set else HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = THIN_BORDER

    data_start = header_row + 1
    for row_offset, (index_value, row_data) in enumerate(df.iterrows()):
        excel_row = data_start + row_offset
        if isinstance(index_value, tuple):
            index_display = " / ".join(str(item) for item in index_value)
        else:
            index_display = index_value
        ws.cell(row=excel_row, column=1, value=_excel_safe_value(index_display))

        for column_index, (column_name, value) in enumerate(row_data.items(), start=2):
            cell = ws.cell(row=excel_row, column=column_index, value=_excel_safe_value(value))
            if isinstance(value, (float, np.floating)) and pd.notna(value):
                if _is_pct_col(column_name):
                    cell.number_format = "0.00%"
                else:
                    cell.number_format = "0.000000"
        for column in range(1, ncols + 1):
            cell = ws.cell(row=excel_row, column=column)
            cell.font = DATA_FONT
            cell.border = THIN_BORDER
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    return data_start + len(df) + 1


def series_block(
    ws: openpyxl.worksheet.worksheet.Worksheet,
    start_row: int,
    title: str,
    series: pd.Series,
) -> int:
    frame = pd.DataFrame({"值": series.values}, index=series.index.astype(str))
    return write_block(ws, start_row, title, frame, index_label="指标")


def reorder_columns(frame: pd.DataFrame, preferred_columns: Sequence[str]) -> pd.DataFrame:
    """将核心字段移动到前面，其他字段完整保留在后面。"""

    if frame.empty:
        return frame.copy()
    preferred = [column for column in preferred_columns if column in frame.columns]
    remaining = [column for column in frame.columns if column not in preferred]
    return frame.loc[:, preferred + remaining].copy()


def detect_bin_column(frame: pd.DataFrame) -> str | None:
    """识别分箱名称列。"""

    suffix_priority = (
        "_final_bin_rounded",
        "_final_bin",
        "_bin_initial",
    )
    for suffix in suffix_priority:
        matched = [column for column in frame.columns if str(column).endswith(suffix)]
        if matched:
            return matched[0]
    generic = [column for column in frame.columns if "bin" in str(column).lower()]
    return generic[0] if generic else None


def prepare_bin_stats_view(frame: pd.DataFrame) -> pd.DataFrame:
    """将风险策略人员最常用的最终分箱字段前置。"""

    bin_col = detect_bin_column(frame)
    preferred = [
        bin_col,
        "bin_order",
        "score_min",
        "score_max",
        "score_mean",
        "n",
        "sample_pct",
        "principal_amt",
        "principal_pct",
        f"{EARLY_TARGET.key}_cnt_mature",
        f"{EARLY_TARGET.key}_cnt_bad",
        f"{EARLY_TARGET.key}_cnt_bad_rate",
        f"{PRIMARY_TARGET.key}_cnt_mature",
        f"{PRIMARY_TARGET.key}_cnt_bad",
        f"{PRIMARY_TARGET.key}_cnt_bad_rate",
        f"{EARLY_TARGET.key}_amt_bad_rate",
        f"{PRIMARY_TARGET.key}_amt_bad_rate",
        f"{EARLY_TARGET.key}_cnt_lift",
        f"{PRIMARY_TARGET.key}_cnt_lift",
        "cum_pass_rate",
        f"cum_{EARLY_TARGET.key}_cnt_bad_rate",
        f"cum_{PRIMARY_TARGET.key}_cnt_bad_rate",
        f"cum_{EARLY_TARGET.key}_amt_bad_rate",
        f"cum_{PRIMARY_TARGET.key}_amt_bad_rate",
    ]
    return reorder_columns(frame, [column for column in preferred if column])


def prepare_strategy_plan_view(frame: pd.DataFrame) -> pd.DataFrame:
    """将阈值、规模和风险字段前置，保留完整策略方案字段。"""

    preferred = [
        "strategy_name",
        "objective",
        "status",
        "auto_pass_threshold",
        "auto_pass_bin",
        "reject_threshold",
        "manual_review_upper_bin",
        "auto_pass_rate",
        "manual_review_rate",
        "reject_rate",
        "accepted_rate",
        f"accepted_{EARLY_TARGET.key}_cnt_bad_rate",
        f"accepted_{PRIMARY_TARGET.key}_cnt_bad_rate",
        f"accepted_{EARLY_TARGET.key}_amt_bad_rate",
        f"accepted_{PRIMARY_TARGET.key}_amt_bad_rate",
        "last_accepted_marginal_1m5_bad_rate",
        "last_accepted_marginal_3m30p_bad_rate",
        "score_missing_decision",
        "constraint_source",
    ]
    return reorder_columns(frame, preferred)


def prepare_strategy_segment_view(frame: pd.DataFrame) -> pd.DataFrame:
    """将 Train/OOT、方案、策略动作和核心风险字段前置。"""

    preferred = [
        "sample_group",
        "strategy_name",
        "decision",
        "auto_pass_threshold",
        "reject_threshold",
        "n",
        "sample_pct",
        f"{EARLY_TARGET.key}_cnt_mature",
        f"{EARLY_TARGET.key}_cnt_bad_rate",
        f"{PRIMARY_TARGET.key}_cnt_mature",
        f"{PRIMARY_TARGET.key}_cnt_bad_rate",
        f"{EARLY_TARGET.key}_amt_bad_rate",
        f"{PRIMARY_TARGET.key}_amt_bad_rate",
        "principal_amt",
        "principal_pct",
        "score_missing_cnt",
        "score_missing_included",
    ]
    return reorder_columns(frame, preferred)


def prepare_threshold_curve_view(frame: pd.DataFrame) -> pd.DataFrame:
    """将阈值曲线中的规模、累计风险和边际风险字段前置。"""

    bin_col = detect_bin_column(frame)
    preferred = [
        "threshold_order",
        "threshold",
        bin_col,
        "cum_pass_rate",
        "cum_pass_rate_scored",
        "marginal_sample_pct",
        f"cum_{EARLY_TARGET.key}_cnt_bad_rate",
        f"cum_{PRIMARY_TARGET.key}_cnt_bad_rate",
        f"marginal_{EARLY_TARGET.key}_cnt_bad_rate",
        f"marginal_{PRIMARY_TARGET.key}_cnt_bad_rate",
        f"cum_{EARLY_TARGET.key}_amt_bad_rate",
        f"cum_{PRIMARY_TARGET.key}_amt_bad_rate",
        f"marginal_{EARLY_TARGET.key}_amt_bad_rate",
        f"marginal_{PRIMARY_TARGET.key}_amt_bad_rate",
        "cum_n",
        "marginal_n",
        "score_coverage",
        "score_missing_n",
    ]
    return reorder_columns(frame, [column for column in preferred if column])


def prepare_split_summary_view(frame: pd.DataFrame) -> pd.DataFrame:
    """将样本范围、规模、分数覆盖和风险成熟度字段前置。"""

    preferred = [
        "sample_group",
        "month_min",
        "month_max",
        "n",
        "application_id_nunique",
        "score_missing_cnt",
        "score_missing_rate",
        f"{EARLY_TARGET.key}_mature",
        f"{EARLY_TARGET.key}_bad",
        f"{EARLY_TARGET.key}_bad_rate",
        f"{PRIMARY_TARGET.key}_mature",
        f"{PRIMARY_TARGET.key}_bad",
        f"{PRIMARY_TARGET.key}_bad_rate",
    ]
    return reorder_columns(frame, preferred)


def flatten_strategy_configs(configs: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """将嵌套策略约束展开为报告表。"""

    rows: list[dict[str, Any]] = []
    for cfg in configs:
        row: dict[str, Any] = {
            "strategy_name": cfg.get("strategy_name"),
            "objective": cfg.get("objective"),
            "constraint_source": cfg.get("constraint_source", "manual"),
        }
        for scope in ["auto_constraints", "accept_constraints"]:
            for metric, value in cfg.get(scope, {}).items():
                row[f"{scope}_{metric}"] = value
        rows.append(row)
    return pd.DataFrame(rows)


def build_metric_glossary() -> pd.DataFrame:
    """生成面向风险策略人员的核心指标阅读说明。"""

    return pd.DataFrame(
        [
            {
                "指标": "1M5",
                "怎么看": "MOB1 时曾达到 5+ DPD，用于观察较早期风险表现。",
            },
            {
                "指标": "MOB3 30+",
                "怎么看": "MOB3 时曾达到 30+ DPD，是本报告的主要风险标的。",
            },
            {
                "指标": "箱内风险率",
                "怎么看": "当前分数箱自身的坏样本率，用于判断风险是否随分数上升。",
            },
            {
                "指标": "累计通过率",
                "怎么看": "从低风险端累计接纳至当前阈值时，占全部样本的比例。",
            },
            {
                "指标": "累计风险率",
                "怎么看": "从低风险端累计接纳至当前阈值后的整体风险水平。",
            },
            {
                "指标": "边际风险率",
                "怎么看": "从上一个阈值放宽到当前阈值时，新增人群自身的风险。",
            },
            {
                "指标": "Train / OOT",
                "怎么看": "Train 用于学习分箱和候选阈值；OOT 用于验证跨期稳定性。",
            },
            {
                "指标": "PSI / AUC / KS",
                "怎么看": "分别观察分布漂移和模型区分能力，不能单独替代策略审批。",
            },
        ]
    )


def build_recommended_strategy_reason(
    strategy_plan: pd.DataFrame,
    recommendation: pd.Series,
) -> str:
    """说明为何推荐当前方案，避免把探索性推荐误解为唯一最优解。"""

    selected_name = recommendation.get("recommended_strategy", "无可行方案")
    if selected_name == "无可行方案":
        return "当前探索性约束下无可行方案，需要复核成熟样本或调整业务约束。"
    if selected_name == "平衡方案":
        return "平衡方案满足当前探索性约束，默认优先作为风险、规模和审核量之间的讨论基准。"

    feasible = strategy_plan.loc[
        strategy_plan.get("status", pd.Series(dtype="object")).eq("OK")
    ]
    if not feasible.empty:
        return "平衡方案未形成可行阈值，因此选择可行方案中接纳率较高的方案作为讨论起点。"
    return "当前方案由本次样本和探索性约束动态生成，需要业务侧进一步复核。"


def build_decision_summary(results: PipelineResults) -> pd.Series:
    """将报告第一页重构为风险策略人员可直接阅读的决策摘要。"""

    recommendation = results.strategy_recommendation
    validation = results.validation_decision
    validation_text = str(validation.get("recommendation", ""))
    if "候选方案" in validation_text:
        decision_status = "可进入策略讨论与业务约束评估"
    else:
        decision_status = "建议先复核分箱稳定性或成熟度问题"

    return pd.Series(
        {
            "报告定位": "探索性分箱与策略阈值分析，不直接替代正式上线审批",
            "策略讨论状态": decision_status,
            "推荐方案": recommendation.get("recommended_strategy", ""),
            "推荐逻辑": build_recommended_strategy_reason(
                results.strategy_plan,
                recommendation,
            ),
            "自动通过规则": recommendation.get("auto_pass_rule", ""),
            "人工审核规则": recommendation.get("manual_review_rule", ""),
            "拒绝规则": recommendation.get("reject_rule", ""),
            "自动通过率": recommendation.get("auto_pass_rate", np.nan),
            "人工审核率": recommendation.get("manual_review_rate", np.nan),
            "拒绝率": recommendation.get("reject_rate", np.nan),
            "总接纳率": recommendation.get("accepted_rate", np.nan),
            "接纳人群 1M5 风险率": recommendation.get(
                "accepted_1m5_bad_rate", np.nan
            ),
            "接纳人群 MOB3 30+ 风险率": recommendation.get(
                "accepted_3m30p_bad_rate", np.nan
            ),
            "分箱验证判断": validation.get("recommendation", ""),
            "关键验证原因": validation.get("reason", ""),
            "Train / OOT 时间": (
                f"Train ≤ {results.config.train_end_month}；"
                f"OOT ≥ {results.config.oot_start_month}"
            ),
            "模型分方向": "分数越高，风险越高",
            "分数缺失处理": results.config.score_missing_decision,
            "上线前必须确认": "正式风险上限、收益与 EL、人工审核产能、业务风险偏好",
        },
        name="value",
    )


def build_recommended_cross_sample_summary(results: PipelineResults) -> pd.DataFrame:
    """汇总推荐方案在 Train/OOT 的规模和接纳风险，便于跨期对比。"""

    recommended = results.strategy_recommendation.get("recommended_strategy")
    segments = results.strategy_segment_report.copy()
    if (
        not recommended
        or recommended == "无可行方案"
        or segments.empty
        or "strategy_name" not in segments.columns
    ):
        return pd.DataFrame()

    selected = segments.loc[segments["strategy_name"].eq(recommended)].copy()
    rows: list[dict[str, Any]] = []
    for sample_group, group in selected.groupby("sample_group", dropna=False, sort=False):
        row: dict[str, Any] = {
            "sample_group": sample_group,
            "strategy_name": recommended,
        }
        for decision, output_name in [
            ("自动通过", "auto_pass_rate"),
            ("人工审核", "manual_review_rate"),
            ("拒绝", "reject_rate"),
        ]:
            decision_row = group.loc[group["decision"].eq(decision)]
            row[output_name] = (
                decision_row["sample_pct"].iloc[0]
                if not decision_row.empty and "sample_pct" in decision_row.columns
                else np.nan
            )

        accepted_n = int(
            group.loc[
                group["decision"].isin(["自动通过", "人工审核"]),
                "n",
            ].sum()
        )
        row["accepted_rate"] = safe_div(accepted_n, int(group["n"].sum()))

        accepted = group.loc[group["decision"].isin(["自动通过", "人工审核"])]
        for target in RISK_TARGETS:
            key = target.key
            mature = accepted.get(f"{key}_cnt_mature", pd.Series(dtype="float64")).sum()
            bad = accepted.get(f"{key}_cnt_bad", pd.Series(dtype="float64")).sum()
            exposure = accepted.get(f"{key}_amt_exposure", pd.Series(dtype="float64")).sum()
            bad_amt = accepted.get(f"{key}_amt_bad", pd.Series(dtype="float64")).sum()
            row[f"accepted_{key}_cnt_bad_rate"] = safe_div(bad, mature)
            row[f"accepted_{key}_amt_bad_rate"] = safe_div(bad_amt, exposure)
        rows.append(row)

    return pd.DataFrame(rows)


def export_excel_report(results: PipelineResults) -> Path:
    """
    生成完整 Excel 策略报告。

    展示顺序面向风险策略人员调整为：
    结论 → 阈值 → 最终分箱 → 稳定性 → 分箱过程 → 数据质量 → 技术复核。
    底层计算结果和完整明细字段均保留。
    """

    results.config.out_dir.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    decision_summary = build_decision_summary(results)
    recommended_cross_sample = build_recommended_cross_sample_summary(results)
    strategy_plan_view = prepare_strategy_plan_view(results.strategy_plan)
    strategy_segment_view = prepare_strategy_segment_view(results.strategy_segment_report)
    final_curve_view = prepare_threshold_curve_view(results.threshold_curve_final_bins)
    quantile_curve_view = prepare_threshold_curve_view(results.threshold_curve_quantile)
    rounded_train_view = prepare_bin_stats_view(results.rounded_train_stats)
    rounded_oot_view = prepare_bin_stats_view(results.rounded_oot_stats)
    exact_train_view = prepare_bin_stats_view(results.train_final_stats)
    exact_oot_view = prepare_bin_stats_view(results.oot_final_stats)
    split_summary_view = prepare_split_summary_view(results.split_summary)

    config_series = pd.Series(
        {
            "data_dir": str(results.config.data_dir),
            "train_end_month": results.config.train_end_month,
            "oot_start_month": results.config.oot_start_month,
            "score_col": results.config.score_col,
            "score_direction": "高分高风险",
            "initial_bins_requested": results.config.initial_bins,
            "initial_bins_actual": len(results.initial_edges) - 1,
            "final_bins": results.final_edges["final_bin_order"].nunique(),
            "score_missing_decision": results.config.score_missing_decision,
            "rounded_decimals_used": int(results.rounded_edges["round_decimals"].iloc[0]),
            "report_path": str(results.report_path),
        },
        name="value",
    )

    # Sheet 1：先回答“怎么切、影响多大、是否可用”。
    ws = wb.create_sheet("1.策略结论")
    row = 1
    row = series_block(ws, row, "一、策略决策摘要", decision_summary)
    if not recommended_cross_sample.empty:
        row = write_block(
            ws,
            row,
            "二、推荐方案 Train / OOT 对比",
            recommended_cross_sample,
            key_columns=[
                "sample_group",
                "auto_pass_rate",
                "manual_review_rate",
                "reject_rate",
                "accepted_rate",
                f"accepted_{EARLY_TARGET.key}_cnt_bad_rate",
                f"accepted_{PRIMARY_TARGET.key}_cnt_bad_rate",
            ],
        )
    row = write_note_block(
        ws,
        row,
        "三、推荐阅读顺序",
        "1）先看本页的策略规则、通过规模和接纳风险；\n"
        "2）再看《2.阈值与策略》，比较保守、平衡、增长三套方案；\n"
        "3）看《3.最终分箱表现》，确认风险是否随分数单调上升；\n"
        "4）看《4.稳定性验证》，确认 OOT、PSI、AUC、KS 和跨月表现；\n"
        "5）分箱过程、数据质量和显著性检验主要用于技术复核。",
    )
    row = write_note_block(
        ws,
        row,
        "四、重要提示",
        "本报告中的保守、平衡、增长方案由 Train 风险水平和探索性约束动态生成，"
        "用于帮助比较风险与规模，不等于最终上线审批结果。上线前应替换或补充正式的"
        "风险上限、收益、EL、人工审核产能和业务风险偏好。",
        fill=WARNING_FILL,
    )
    row = series_block(ws, row, "五、分箱验证结论", results.validation_decision)
    row = write_block(ws, row, "六、样本范围与成熟度", split_summary_view)
    row = write_block(ws, row, "七、核心指标怎么看", build_metric_glossary())
    row = write_block(
        ws,
        row,
        "八、探索性策略约束",
        flatten_strategy_configs(results.strategy_configs),
    )
    row = series_block(ws, row, "九、运行配置", config_series)
    row = series_block(ws, row, "十、数据关键检查", results.key_checks)
    row = write_block(ws, row, "十一、风险标签来源", results.target_derivation_summary)
    auto_width(ws)
    configure_report_sheet(ws)

    # Sheet 2：策略方案优先，阈值明细和敏感性放在后面。
    ws = wb.create_sheet("2.阈值与策略")
    row = 1
    row = write_note_block(
        ws,
        row,
        "阅读提示",
        "先比较三套方案的自动通过率、人工审核率、拒绝率和接纳风险；"
        "再通过敏感性矩阵观察审核产能或风险上限变化时，接纳率如何变化。"
        "阈值曲线用于进一步定位具体分数边界。",
    )
    row = write_block(
        ws,
        row,
        "一、三套策略方案",
        strategy_plan_view,
        key_columns=[
            "strategy_name",
            "status",
            "auto_pass_threshold",
            "reject_threshold",
            "auto_pass_rate",
            "manual_review_rate",
            "reject_rate",
            "accepted_rate",
            f"accepted_{EARLY_TARGET.key}_cnt_bad_rate",
            f"accepted_{PRIMARY_TARGET.key}_cnt_bad_rate",
        ],
    )
    if not recommended_cross_sample.empty:
        row = write_block(
            ws,
            row,
            "二、推荐方案 Train / OOT 汇总",
            recommended_cross_sample,
        )
    row = write_block(
        ws,
        row,
        "三、Train / OOT 三段完整指标",
        strategy_segment_view,
        key_columns=[
            "sample_group",
            "strategy_name",
            "decision",
            "sample_pct",
            f"{EARLY_TARGET.key}_cnt_bad_rate",
            f"{PRIMARY_TARGET.key}_cnt_bad_rate",
        ],
    )
    row = write_block(ws, row, "四、阈值敏感性矩阵", results.threshold_sensitivity_matrix)
    row = write_block(ws, row, "五、阈值敏感性扫描", results.threshold_sensitivity)
    row = write_block(
        ws,
        row,
        "六、最终箱边界阈值曲线",
        final_curve_view,
        key_columns=[
            "threshold",
            "cum_pass_rate",
            f"cum_{EARLY_TARGET.key}_cnt_bad_rate",
            f"cum_{PRIMARY_TARGET.key}_cnt_bad_rate",
            f"marginal_{PRIMARY_TARGET.key}_cnt_bad_rate",
        ],
    )
    row = write_block(ws, row, "七、细粒度分位点阈值曲线", quantile_curve_view)
    auto_width(ws)
    configure_report_sheet(ws)

    # Sheet 3：展示上线候选取整边界下的最终风险分层。
    ws = wb.create_sheet("3.最终分箱表现")
    row = 1
    row = write_note_block(
        ws,
        row,
        "阅读提示",
        "优先查看取整边界下的 Train 和 OOT 表现：箱内风险率应整体随分数升高而上升；"
        "累计通过率用于观察规模，累计风险率用于评估放宽阈值后的整体风险。"
        "完整分子、分母、金额风险和置信区间字段仍保留在表格后部。",
    )
    row = write_block(ws, row, "一、上线候选取整边界", results.rounded_edges)
    row = write_block(
        ws,
        row,
        "二、取整边界 Train 分箱指标",
        rounded_train_view,
        key_columns=[
            detect_bin_column(rounded_train_view) or "",
            "score_min",
            "score_max",
            "n",
            "sample_pct",
            f"{EARLY_TARGET.key}_cnt_bad_rate",
            f"{PRIMARY_TARGET.key}_cnt_bad_rate",
            "cum_pass_rate",
            f"cum_{PRIMARY_TARGET.key}_cnt_bad_rate",
        ],
    )
    row = write_block(
        ws,
        row,
        "三、取整边界 OOT 分箱指标",
        rounded_oot_view,
        key_columns=[
            detect_bin_column(rounded_oot_view) or "",
            "score_min",
            "score_max",
            "n",
            "sample_pct",
            f"{EARLY_TARGET.key}_cnt_bad_rate",
            f"{PRIMARY_TARGET.key}_cnt_bad_rate",
            "cum_pass_rate",
            f"cum_{PRIMARY_TARGET.key}_cnt_bad_rate",
        ],
    )
    row = write_block(ws, row, "四、精确边界 Train 分箱指标（复核）", exact_train_view)
    row = write_block(ws, row, "五、精确边界 OOT 分箱指标（复核）", exact_oot_view)
    auto_width(ws)
    configure_report_sheet(ws)

    # Sheet 4：验证结果集中展示。
    ws = wb.create_sheet("4.稳定性验证")
    row = 1
    row = series_block(ws, row, "一、验证结论", results.validation_decision)
    row = write_block(ws, row, "二、PSI（含分数缺失箱）", results.psi)
    row = write_block(ws, row, "三、AUC / KS", results.performance_by_group)
    row = write_block(ws, row, "四、Train 单调性", results.monotonicity_train)
    row = write_block(ws, row, "五、OOT 单调性", results.monotonicity_oot)
    row = write_block(ws, row, "六、月度稳定性摘要", results.monthly_summary)
    row = write_block(ws, row, "七、月度 AUC / KS", results.monthly_performance)
    auto_width(ws)
    configure_report_sheet(ws)

    # Sheet 5：完整保留分箱和自动合箱底稿。
    ws = wb.create_sheet("5.分箱过程")
    row = 1
    row = write_note_block(
        ws,
        row,
        "阅读提示",
        "本页用于复核分箱是如何从初始等频箱逐步合并为最终风险等级。"
        "风险策略人员通常先看最终合箱映射和最终边界；分析人员可继续查看初始诊断和每一步合箱原因。",
    )
    row = write_block(ws, row, "一、初始等频边界", results.initial_edge_table)
    row = write_block(ws, row, "二、初始分箱指标", results.initial_stats)
    row = write_block(ws, row, "三、初始分箱诊断", results.initial_diagnosis)
    row = write_block(ws, row, "四、自动合箱过程", results.merge_trace)
    row = write_block(ws, row, "五、最终合箱映射", results.merge_map)
    row = write_block(ws, row, "六、最终精确边界", results.final_edges)
    auto_width(ws)
    configure_report_sheet(ws)

    # Sheet 6：数据口径和质量底稿后置。
    ws = wb.create_sheet("6.数据质量")
    row = 1
    row = write_block(ws, row, "一、样本切分", split_summary_view)
    dedup_summary = results.data_quality.attrs.get("dedup_summary", pd.DataFrame())
    if not dedup_summary.empty:
        row = write_block(ws, row, "二、源表去重检查", dedup_summary)
    row = write_block(ws, row, "三、字段质量", results.data_quality)
    auto_width(ws)
    configure_report_sheet(ws)

    # Sheet 7：边界取整和统计显著性作为技术复核附录。
    ws = wb.create_sheet("7.边界与显著性")
    row = 1
    row = write_note_block(
        ws,
        row,
        "阅读提示",
        "本页主要验证精确边界取整后是否引起明显样本迁移或风险变化，"
        "以及相邻箱风险差异是否具有统计显著性。它用于辅助判断，不应机械替代业务可解释性和策略约束。",
    )
    row = write_block(ws, row, "一、取整后的最终边界", results.rounded_edges)
    row = write_block(ws, row, "二、精确边界 vs 取整边界", results.rounded_boundary_comparison)
    row = write_block(ws, row, "三、1M5 相邻箱显著性", results.adjacent_tests_early)
    row = write_block(ws, row, "四、MOB3 30+ 相邻箱显著性", results.adjacent_tests_primary)
    auto_width(ws)
    configure_report_sheet(ws)

    wb.save(results.report_path)
    LOGGER.info("策略报告已生成：%s", results.report_path)
    return results.report_path

# ============================================================
# 9. 主流程
# ============================================================


def run_pipeline(config: PipelineConfig | None = None) -> PipelineResults:
    """执行完整分箱与策略分析。"""

    config = config or PipelineConfig()
    configure_logging()

    initial_bin_col = f"{config.score_col}_bin_initial"
    final_bin_col = f"{config.score_col}_final_bin"
    rounded_bin_col = f"{config.score_col}_final_bin_rounded"

    data_raw, data_quality, key_checks, target_derivation_summary = load_and_prepare_data(config)
    data, train, oot, split_summary = split_train_oot(data_raw, config)

    # 1. Train 上学习初始等频边界
    initial_edges, actual_initial_bins = learn_equal_freq_edges(
        train,
        config.score_col,
        config.initial_bins,
    )
    initial_edge_table = build_bin_edge_table(
        initial_edges,
        bin_col=initial_bin_col,
    )
    all_initial = apply_edges(
        data,
        config.score_col,
        initial_edges,
        initial_bin_col,
    )
    train_initial = all_initial.loc[all_initial["sample_group"].eq("train")].copy()
    oot_initial = all_initial.loc[all_initial["sample_group"].eq("oot")].copy()

    initial_stats = calc_bin_stats(
        train_initial,
        initial_bin_col,
        score_col=config.score_col,
        id_col=config.application_id_col,
    )
    initial_stats = initial_stats.merge(
        initial_edge_table,
        on=["bin_order", initial_bin_col],
        how="left",
        validate="one_to_one",
    )
    initial_diagnosis = diagnose_bin_stats(initial_stats, config)

    # 2. 自动相邻合箱
    merge_config = replace(
        config,
        max_final_bins=min(config.max_final_bins, actual_initial_bins),
        min_final_bins=max(2, min(config.min_final_bins, actual_initial_bins)),
    )
    merge_map, merge_trace = auto_merge_adjacent_bins(
        train_initial,
        initial_bin_col,
        final_bin_col,
        config.score_col,
        actual_initial_bins,
        merge_config,
    )
    final_edges = build_final_edge_table(
        initial_edge_table,
        merge_map,
        initial_bin_col,
        final_bin_col,
    )

    train_final = apply_merge_map(
        train_initial,
        merge_map,
        initial_bin_col,
        final_bin_col,
    )
    oot_final = apply_merge_map(
        oot_initial,
        merge_map,
        initial_bin_col,
        final_bin_col,
    )
    all_final = apply_merge_map(
        all_initial,
        merge_map,
        initial_bin_col,
        final_bin_col,
    )

    train_final_stats = calc_bin_stats(
        train_final,
        final_bin_col,
        score_col=config.score_col,
        id_col=config.application_id_col,
    ).merge(
        final_edges,
        left_on=["bin_order", final_bin_col],
        right_on=["final_bin_order", final_bin_col],
        how="left",
        validate="one_to_one",
    )

    oot_final_stats_base = calc_bin_stats(
        oot_final,
        final_bin_col,
        score_col=config.score_col,
        id_col=config.application_id_col,
    )
    if oot_final_stats_base.empty:
        oot_final_stats = pd.DataFrame()
    else:
        oot_final_stats = oot_final_stats_base.merge(
            final_edges,
            left_on=["bin_order", final_bin_col],
            right_on=["final_bin_order", final_bin_col],
            how="left",
            validate="one_to_one",
        )

    rate_cols = [f"{target.key}_cnt_bad_rate" for target in RISK_TARGETS]
    monotonicity_train = check_monotonicity(train_final_stats, rate_cols)
    monotonicity_oot = check_monotonicity(oot_final_stats, rate_cols)

    if oot.empty:
        psi = pd.DataFrame()
    else:
        psi = calc_population_psi(
            train_final,
            oot_final,
            final_bin_col,
            merge_map[[final_bin_col, "final_bin_order"]].drop_duplicates(),
        )

    perf_data = all_final.loc[all_final["sample_group"].isin(["train", "oot"])].copy()
    performance_by_group = calc_perf_by_group(
        perf_data,
        "sample_group",
        config.score_col,
    )
    monthly_stats = calc_group_bin_stats(
        all_final,
        "application_month",
        final_bin_col,
        config.score_col,
    )
    monthly_summary = build_monthly_stability_summary(
        monthly_stats,
        all_final,
        final_bin_col,
    )
    monthly_performance = calc_perf_by_group(
        all_final,
        "application_month",
        config.score_col,
    )
    validation_decision = build_validation_decision(
        psi,
        monotonicity_train,
        monotonicity_oot,
        performance_by_group,
        monthly_summary,
        config,
    )

    # 3. 边界取整并复算
    rounded_edges, decimals_used = build_rounded_final_edges(
        final_edges,
        final_bin_col,
        config.rounded_decimals_preferred,
        config.rounded_decimals_max,
    )
    LOGGER.info("最终边界采用 %s 位小数。", decimals_used)

    train_rounded = apply_final_edges(
        train,
        rounded_edges,
        config.score_col,
        final_bin_col,
        rounded_bin_col,
    )
    oot_rounded = apply_final_edges(
        oot,
        rounded_edges,
        config.score_col,
        final_bin_col,
        rounded_bin_col,
    )

    rounded_train_stats = calc_bin_stats(
        train_rounded,
        rounded_bin_col,
        score_col=config.score_col,
        id_col=config.application_id_col,
    )
    rounded_oot_stats = calc_bin_stats(
        oot_rounded,
        rounded_bin_col,
        score_col=config.score_col,
        id_col=config.application_id_col,
    )
    rounded_boundary_comparison = compare_boundary_assignments(
        train_final,
        train_rounded,
        final_bin_col,
        rounded_bin_col,
        train_final_stats,
        rounded_train_stats,
    )

    # 4. 使用取整后的最终边界生成阈值曲线
    threshold_edge_table = final_bin_threshold_table(
        rounded_edges.rename(columns={final_bin_col: rounded_bin_col}),
        train_rounded,
        config.score_col,
        rounded_bin_col,
        right_edge_col="score_right_rounded",
    )
    threshold_curve_final_bins = calc_threshold_curve(
        train_rounded,
        config.score_col,
        threshold_edge_table["threshold"].tolist(),
    ).merge(
        threshold_edge_table[
            [
                "final_bin_order",
                rounded_bin_col,
                "threshold",
                "score_right_rounded",
                "merged_from",
            ]
        ],
        on="threshold",
        how="left",
        validate="one_to_one",
    )

    quantile_values = quantile_thresholds(train_rounded, config.score_col, n_quantiles=config.quantile_threshold_count)
    threshold_curve_quantile = calc_threshold_curve(
        train_rounded,
        config.score_col,
        quantile_values,
    )

    # 5. 动态策略方案
    strategy_configs = config.strategy_configs or build_data_driven_strategy_configs(
        train_rounded, config.strategy_presets
    )
    strategy_plan, strategy_segment_train = make_strategy_plan(
        train_rounded,
        threshold_curve_final_bins,
        strategy_configs,
        config.score_col,
        rounded_bin_col,
        config.score_missing_decision,
    )
    strategy_segment_oot = make_strategy_segment_report(
        oot_rounded,
        strategy_plan,
        config.score_col,
        config.score_missing_decision,
        "oot",
    )
    strategy_segment_report = pd.concat(
        [strategy_segment_train, strategy_segment_oot],
        ignore_index=True,
        sort=False,
    )
    strategy_recommendation = choose_recommended_strategy(
        strategy_plan,
        strategy_segment_report,
    )

    overall_train_metrics = calc_frame_metrics(train_rounded)
    overall_primary_rate = overall_train_metrics[f"{PRIMARY_TARGET.key}_cnt_bad_rate"]
    if pd.isna(overall_primary_rate) or overall_primary_rate <= 0:
        accepted_primary_caps = (0.05, 0.08, 0.10)
    else:
        accepted_primary_caps = tuple(
            sorted(
                {
                    min(max(overall_primary_rate * ratio, 0.0001), 1.0)
                    for ratio in config.sensitivity_primary_cap_ratios
                }
            )
        )

    threshold_sensitivity = scan_threshold_sensitivity(
        train_rounded,
        threshold_curve_final_bins,
        config.score_col,
        rounded_bin_col,
        config.score_missing_decision,
        config.manual_review_caps,
        accepted_primary_caps,
    )
    threshold_sensitivity_matrix = build_sensitivity_matrix(threshold_sensitivity)

    adjacent_tests_early = adjacent_proportion_tests(
        rounded_train_stats,
        EARLY_TARGET,
    )
    adjacent_tests_primary = adjacent_proportion_tests(
        rounded_train_stats,
        PRIMARY_TARGET,
    )

    results = PipelineResults(
        config=config,
        data=all_final,
        train=train,
        oot=oot,
        data_quality=data_quality,
        key_checks=key_checks,
        split_summary=split_summary,
        target_derivation_summary=target_derivation_summary,
        initial_edges=initial_edges,
        initial_edge_table=initial_edge_table,
        initial_stats=initial_stats,
        initial_diagnosis=initial_diagnosis,
        merge_map=merge_map,
        merge_trace=merge_trace,
        final_edges=final_edges,
        train_final=train_final,
        oot_final=oot_final,
        all_final=all_final,
        train_final_stats=train_final_stats,
        oot_final_stats=oot_final_stats,
        monotonicity_train=monotonicity_train,
        monotonicity_oot=monotonicity_oot,
        psi=psi,
        performance_by_group=performance_by_group,
        monthly_stats=monthly_stats,
        monthly_summary=monthly_summary,
        monthly_performance=monthly_performance,
        validation_decision=validation_decision,
        rounded_edges=rounded_edges,
        rounded_boundary_comparison=rounded_boundary_comparison,
        train_rounded=train_rounded,
        oot_rounded=oot_rounded,
        rounded_train_stats=rounded_train_stats,
        rounded_oot_stats=rounded_oot_stats,
        threshold_curve_final_bins=threshold_curve_final_bins,
        threshold_curve_quantile=threshold_curve_quantile,
        strategy_configs=strategy_configs,
        strategy_plan=strategy_plan,
        strategy_segment_report=strategy_segment_report,
        strategy_recommendation=strategy_recommendation,
        threshold_sensitivity=threshold_sensitivity,
        threshold_sensitivity_matrix=threshold_sensitivity_matrix,
        adjacent_tests_early=adjacent_tests_early,
        adjacent_tests_primary=adjacent_tests_primary,
        report_path=config.report_path,
    )

    export_excel_report(results)

    print_table("样本切分", split_summary, config.verbose_tables)
    print_table("最终分箱", train_final_stats, config.verbose_tables)
    print_table("策略方案", strategy_plan, config.verbose_tables)
    print_table("策略推荐", strategy_recommendation, config.verbose_tables)

    return results


# ============================================================
# 10. 命令行入口
# ============================================================


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="模型分数分箱与策略阈值分析")
    parser.add_argument("--data-dir", type=Path, default=BASE_DIR / "res")
    parser.add_argument("--out-dir", type=Path, default=BASE_DIR / "out")
    parser.add_argument("--train-end-month", default="2026-03")
    parser.add_argument("--oot-start-month", default="2026-04")
    parser.add_argument("--initial-bins", type=int, default=20)
    parser.add_argument("--min-final-bins", type=int, default=5)
    parser.add_argument("--max-final-bins", type=int, default=8)
    parser.add_argument("--min-bin-n", type=int, default=1000)
    parser.add_argument("--min-mature-n", type=int, default=1000)
    parser.add_argument("--min-bad-n", type=int, default=30)
    parser.add_argument("--verbose-tables", action="store_true")
    return parser.parse_args(argv)



def main(argv: Sequence[str] | None = None) -> PipelineResults:
    args = parse_args(argv)
    config = PipelineConfig(
        data_dir=args.data_dir,
        out_dir=args.out_dir,
        train_end_month=args.train_end_month,
        oot_start_month=args.oot_start_month,
        initial_bins=args.initial_bins,
        min_final_bins=args.min_final_bins,
        max_final_bins=args.max_final_bins,
        min_bin_n=args.min_bin_n,
        min_mature_n=args.min_mature_n,
        min_bad_n=args.min_bad_n,
        verbose_tables=args.verbose_tables,
    )
    return run_pipeline(config)


if __name__ == "__main__":
    main()
