# -*- coding: utf-8 -*-
"""
分箱管线编排：按 settings 的当前配置跑完"加载 → 切分 → 等频初分 → 自动合箱 →
验证 → 策略阈值 → Excel 报告"全流程（笔数口径 / 金额口径共用）。

运行前由入口脚本完成：settings.apply_dataset / apply_model / apply_metric，
并调用各模块 _sync_settings()。本模块函数体由 binning_mlt_cnt.py 的 main()
逐行移植而来，仅把函数调用改为跨模块限定引用。
"""
from pathlib import Path

import pandas as pd

import pipeline.settings as settings
from pipeline import bin_amt as _bin_amt
from pipeline.binning_cnt import (
    apply_edges,
    apply_merge_map,
    build_final_edge_table,
    build_initial_edge_table,
    build_merge_map,
    format_merge_ranges,
    learn_equal_freq_edges,
    selected_ranges_from_candidate_table,
)
from pipeline.binning_cnt import build_merge_candidate_score_table as _cnt_build_merge_candidate_score_table
from pipeline.binning_cnt import calc_complete_initial_stats as _cnt_calc_complete_initial_stats
from pipeline.common import _log_step
from pipeline.data_loading import load_analysis_data, split_train_oot
from pipeline.monthly import build_monthly_bin_stability as _cnt_build_monthly_bin_stability
from pipeline.monthly import build_monthly_stability_summary as _cnt_build_monthly_stability_summary
from pipeline.reporting import build_config_table
from pipeline.reporting import build_metric_dictionary as _cnt_build_metric_dictionary
from pipeline.reporting import build_online_execution_rules
from pipeline.reporting import build_overview as _cnt_build_overview
from pipeline.reporting import write_report
from pipeline.risk_metrics import build_train_oot_compare
from pipeline.risk_metrics import build_enriched_final_bin_report as _cnt_build_enriched_final_bin_report
from pipeline.risk_metrics import calc_bin_stats as _cnt_calc_bin_stats
from pipeline.risk_metrics import calc_performance_table, calc_population_psi, check_monotonicity
from pipeline.strategy import (
    build_strategy_estimated_flow_report,
    build_strategy_segment_report,
    build_threshold_curve,
)
from pipeline.strategy import build_binning_process_table as _cnt_build_binning_process_table
from pipeline.strategy import build_strategy_plan as _cnt_build_strategy_plan
from pipeline.strategy import build_threshold_sensitivity as _cnt_build_threshold_sensitivity
from pipeline.strategy import build_threshold_selection_table as _cnt_build_threshold_selection_table


def _pick(name: str, default):
    """amt 口径下若 bin_amt 提供同名覆盖实现则使用之，否则用共享实现。"""
    if settings.CURRENT_METRIC == "amt" and hasattr(_bin_amt, name):
        return getattr(_bin_amt, name)
    return default


def _sync_settings() -> None:
    settings.sync(globals())


_sync_settings()


def run_binning(report_path: Path) -> None:
    """完整分箱管线；输出写入 report_path。"""
    import time

    # 输出路径注入 settings 并同步到引用它的模块（reporting 在保存时读取）。
    settings.REPORT_PATH = Path(report_path)
    import pipeline.reporting as reporting

    reporting._sync_settings()
    _sync_settings()

    # 金额口径下切换到 bin_amt 的覆盖实现。
    calc_complete_initial_stats = _pick("calc_complete_initial_stats", _cnt_calc_complete_initial_stats)
    build_merge_candidate_score_table = _pick("build_merge_candidate_score_table", _cnt_build_merge_candidate_score_table)
    build_strategy_plan = _pick("build_strategy_plan", _cnt_build_strategy_plan)
    build_threshold_sensitivity = _pick("build_threshold_sensitivity", _cnt_build_threshold_sensitivity)
    build_binning_process_table = _pick("build_binning_process_table", _cnt_build_binning_process_table)
    build_threshold_selection_table = _pick("build_threshold_selection_table", _cnt_build_threshold_selection_table)
    build_overview = _pick("build_overview", _cnt_build_overview)
    build_metric_dictionary = _pick("build_metric_dictionary", _cnt_build_metric_dictionary)
    build_monthly_stability_summary = _pick("build_monthly_stability_summary", _cnt_build_monthly_stability_summary)
    build_monthly_bin_stability = _pick("build_monthly_bin_stability", _cnt_build_monthly_bin_stability)
    build_enriched_final_bin_report = _pick("build_enriched_final_bin_report", _cnt_build_enriched_final_bin_report)
    calc_bin_stats = _pick("calc_bin_stats", _cnt_calc_bin_stats)

    _t = _log_step._t0 = time.time()

    data = load_analysis_data()
    actual_funnel_report = data.attrs.get("actual_funnel_report", pd.DataFrame())

    all_data, train, oot = split_train_oot(data)
    _t = _log_step("1/9 数据加载与时间切分", _t)

    # 1) 完整 Train 学习初始边界，并复用到 OOT。
    edges = learn_equal_freq_edges(train, SCORE_COL, INITIAL_BIN_COUNT)
    actual_initial_bin_count = len(edges) - 1
    initial_edges = build_initial_edge_table(edges)

    if actual_initial_bin_count < MIN_FINAL_BIN_COUNT:
        raise ValueError(
            f"模型分唯一值不足，实际仅形成 {actual_initial_bin_count} 个初始箱，"
            f"小于最小最终箱数 {MIN_FINAL_BIN_COUNT}"
        )

    all_binned = apply_edges(all_data, SCORE_COL, edges, INITIAL_BIN_COL)
    train_binned = all_binned.loc[all_binned["sample_group"].eq("train")].copy()
    oot_binned = all_binned.loc[all_binned["sample_group"].eq("oot")].copy()

    train_initial_stats = calc_complete_initial_stats(
        train_binned,
        initial_edges,
    )
    _t = _log_step(f"2/9 Train 等频初分：{actual_initial_bin_count} 箱", _t)

    # 2) 基于完整 Train 自动选择合箱；OOT 不参与。
    merge_candidates, merge_steps, protected_boundaries = build_merge_candidate_score_table(
        train_initial_stats,
        actual_initial_bin_count,
        STRATEGY_CONFIG,
    )
    selected_merge_ranges = selected_ranges_from_candidate_table(merge_candidates)

    merge_map = build_merge_map(selected_merge_ranges, actual_initial_bin_count)
    final_edges = build_final_edge_table(
        initial_edges,
        merge_map,
        actual_initial_bin_count,
    )
    _t = _log_step(
        f"3/9 自动合箱完成：{len(final_edges)} 档，方案={format_merge_ranges(selected_merge_ranges)}",
        _t,
    )

    # 3) 将最终合箱映射应用到所有样本。
    train_final = apply_merge_map(train_binned, merge_map)
    oot_final = apply_merge_map(oot_binned, merge_map)
    all_final = apply_merge_map(all_binned, merge_map)

    def final_stats(frame: pd.DataFrame) -> pd.DataFrame:
        return calc_bin_stats(
            frame,
            bin_col=FINAL_BIN_COL,
            order_col="bin_order",
        ).merge(
            final_edges,
            left_on=["bin_order", FINAL_BIN_COL],
            right_on=["final_bin_order", FINAL_BIN_COL],
            how="left",
        )

    final_train_stats = final_stats(train_final)
    final_oot_stats = final_stats(oot_final)
    _t = _log_step("4/9 生成 Train/OOT 最终箱统计", _t)

    # 4) 最终验证。
    rate_cols = ALL_RISK_RATE_COLS
    monotonicity = pd.concat(
        [
            check_monotonicity(final_train_stats, rate_cols, "train"),
            check_monotonicity(final_oot_stats, rate_cols, "oot"),
        ],
        ignore_index=True,
    )

    psi = calc_population_psi(train_final, oot_final, FINAL_BIN_COL, final_edges)
    performance = calc_performance_table(all_final)
    train_oot_compare = build_train_oot_compare(
        final_train_stats,
        final_oot_stats,
        final_edges,
    )
    monthly_stability = build_monthly_bin_stability(all_final)
    monthly_stability_summary = build_monthly_stability_summary(monthly_stability)
    _t = _log_step(
        f"5/9 OOT 单调性/PSI/AUC/KS 验证：PSI={psi['psi_total'].iloc[0]:.4f}",
        _t,
    )

    # 5) 使用完整 Train 生成策略阈值。
    threshold_curve = build_threshold_curve(train_final, final_edges)
    strategy_plan = build_strategy_plan(threshold_curve, STRATEGY_CONFIG)
    threshold_sensitivity = build_threshold_sensitivity(threshold_curve, strategy_plan)
    strategy_segments = build_strategy_segment_report(
        train_final,
        oot_final,
        strategy_plan,
    )
    strategy_estimated_flow = build_strategy_estimated_flow_report(
        train_final,
        oot_final,
        strategy_plan,
    )
    binning_process = build_binning_process_table(train_initial_stats, merge_map)
    threshold_selection = build_threshold_selection_table(
        threshold_curve,
        strategy_plan,
        STRATEGY_CONFIG,
    )
    final_train_report = build_enriched_final_bin_report(
        final_train_stats,
        train_final,
        strategy_plan,
        psi,
    )
    final_oot_report = build_enriched_final_bin_report(
        final_oot_stats,
        oot_final,
        strategy_plan,
        psi,
    )
    _t = _log_step("6/9 历史实际审批漏斗与模型策略测算流量", _t)

    selected_candidate_rows = merge_candidates.loc[
        merge_candidates.get("selected", pd.Series(False, index=merge_candidates.index)).eq(True)
    ]
    selected_candidate = (
        selected_candidate_rows.iloc[0] if not selected_candidate_rows.empty else None
    )

    overview = build_overview(
        all_data,
        train_final,
        oot_final,
        actual_initial_bin_count,
        len(final_edges),
        selected_merge_ranges,
        selected_candidate,
        protected_boundaries,
        psi,
        performance,
        monotonicity,
        strategy_plan,
        actual_funnel_report,
        strategy_estimated_flow,
    )
    config_table = build_config_table(
        selected_merge_ranges,
        protected_boundaries,
    )
    online_execution_rules = build_online_execution_rules()
    metric_dictionary = build_metric_dictionary()

    write_report(
        overview=overview,
        binning_process=binning_process,
        final_train_stats=final_train_report,
        final_oot_stats=final_oot_report,
        train_oot_compare=train_oot_compare,
        actual_funnel_report=actual_funnel_report,
        strategy_estimated_flow=strategy_estimated_flow,
        threshold_selection=threshold_selection,
        strategy_plan=strategy_plan,
        threshold_sensitivity=threshold_sensitivity,
        strategy_segments=strategy_segments,
        performance=performance,
        psi=psi,
        monotonicity=monotonicity,
        monthly_stability=monthly_stability,
        monthly_stability_summary=monthly_stability_summary,
        merge_candidates=merge_candidates,
        merge_steps=merge_steps,
        config_table=config_table,
        online_execution_rules=online_execution_rules,
        metric_dictionary=metric_dictionary,
    )

    _t = _log_step("7/9 写入 Excel 报告", _t)
    _t = _log_step("8/9 报告格式化完成", _t)
    _log_step(f"9/9 完成 => {report_path}", _t)
