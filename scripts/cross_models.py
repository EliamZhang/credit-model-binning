# -*- coding: utf-8 -*-
"""
两模型交叉分析入口（配置驱动）。

用法：
    python scripts/cross_models.py --dataset laoke --model-a mlt --model-b worthiness --mode matrix
    python scripts/cross_models.py --dataset laoke --model-a mlt --model-b worthiness --mode cond

matrix：A 模型 7 档 × B 模型 7 档全局交叉（矩阵/条件增量/组合评分/二维策略）。
cond：在 A 模型各档内对 B 模型分做 3 等频条件子箱（21 组合格）。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline.settings as settings
from configs.datasets import DATASETS, REQUIRED_DATASET_KEYS
from configs.models import MODELS, REQUIRED_MODEL_KEYS
from pipeline import cross_analysis

# 既有场景的输出前缀（保持与历史报告/核对脚本一致）；新组合走通用命名。
REPORT_PREFIXES = {
    ("laoke", "mlt", "worthiness", "matrix"): "binning_cross_strategy_report",
    ("laoke", "mlt", "worthiness", "cond"): "binning_worthiness_cond_strategy_report",
}


def validate(dataset_key: str, model_a: str, model_b: str) -> None:
    for key in REQUIRED_DATASET_KEYS:
        if key not in DATASETS[dataset_key]:
            raise ValueError(f"数据集配置 {dataset_key} 缺少必填键: {key}")
    for key in REQUIRED_MODEL_KEYS:
        if key not in MODELS[model_a]:
            raise ValueError(f"模型配置 {model_a} 缺少必填键: {key}")
        if key not in MODELS[model_b]:
            raise ValueError(f"模型配置 {model_b} 缺少必填键: {key}")


def resolve_report_path(dataset_key: str, model_a: str, model_b: str, mode: str) -> Path:
    key = (dataset_key, model_a, model_b, mode)
    prefix = REPORT_PREFIXES.get(key)
    if prefix is None:
        prefix = (
            f"binning_cross_{model_a}_{model_b}_strategy_report"
            if mode == "matrix"
            else f"binning_cond_{model_a}_{model_b}_strategy_report"
        )
    return settings.OUT_DIR / f"{prefix}_{time.strftime('%Y%m%d')}.xlsx"


def run(dataset_key: str = "laoke", model_a: str = "mlt", model_b: str = "worthiness", mode: str = "matrix") -> None:
    validate(dataset_key, model_a, model_b)
    dataset_cfg = DATASETS[dataset_key]
    model_a_cfg = MODELS[model_a]
    model_b_cfg = MODELS[model_b]
    report_path = resolve_report_path(dataset_key, model_a, model_b, mode)
    if mode == "matrix":
        cross_analysis.run_matrix(dataset_cfg, model_a_cfg, model_b_cfg, report_path)
    elif mode == "cond":
        cross_analysis.run_cond(dataset_cfg, model_a_cfg, model_b_cfg, report_path)
    else:
        raise ValueError(f"未知 mode: {mode}（可选 matrix / cond）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="两模型交叉分析：matrix 全局交叉 / cond 条件子箱")
    parser.add_argument("--dataset", default="laoke", choices=sorted(DATASETS), help="样本集 key")
    parser.add_argument("--model-a", default="mlt", choices=sorted(MODELS), help="A 模型 key（交叉的行轴/条件分箱的基底模型）")
    parser.add_argument("--model-b", default="worthiness", choices=sorted(MODELS), help="B 模型 key")
    parser.add_argument("--mode", default="matrix", choices=["matrix", "cond"], help="分析模式")
    return parser


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    run(args.dataset, args.model_a, args.model_b, args.mode)


if __name__ == "__main__":
    main()
