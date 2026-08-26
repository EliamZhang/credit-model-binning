# -*- coding: utf-8 -*-
"""
单模型分箱入口（配置驱动）。

用法：
    python scripts/bin_model.py --dataset laoke --model mlt --metric cnt
    python scripts/bin_model.py --dataset laoke --model mlt --metric amt
    python scripts/bin_model.py --dataset laoke --model worthiness --metric cnt

新增模型/样本：只需在 configs/models.py 与 configs/datasets.py 注册即可，
无需修改本文件与 pipeline 代码。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline.settings as settings
from configs.datasets import DATASETS, REQUIRED_DATASET_KEYS
from configs.models import MODELS, REQUIRED_MODEL_KEYS
from pipeline import (
    bin_amt,
    binning_cnt,
    data_loading,
    monthly,
    reporting,
    risk_metrics,
    strategy,
)
from pipeline.orchestration import run_binning


def apply_configs(dataset_key: str, model_key: str, metric: str) -> None:
    """把数据集/模型/口径配置注入 settings，并同步到全部管线模块。"""
    dataset_cfg = DATASETS[dataset_key]
    model_cfg = MODELS[model_key]
    for key in REQUIRED_DATASET_KEYS:
        if key not in dataset_cfg:
            raise ValueError(f"数据集配置 {dataset_key} 缺少必填键: {key}")
    for key in REQUIRED_MODEL_KEYS:
        if key not in model_cfg:
            raise ValueError(f"模型配置 {model_key} 缺少必填键: {key}")

    settings.apply_dataset(dataset_cfg)
    settings.apply_model(model_cfg)
    settings.apply_metric(metric)
    for module in (
        data_loading,
        risk_metrics,
        binning_cnt,
        strategy,
        monthly,
        reporting,
        bin_amt,
    ):
        module._sync_settings()


def resolve_report_path(dataset_key: str, model_key: str, metric: str) -> Path:
    """输出 Excel 路径：前缀取自模型配置，文件名日期为运行当天。"""
    model_cfg = MODELS[model_key]
    if metric == "amt":
        prefix = model_cfg.get("report_prefix_amt", f"{model_cfg['report_prefix']}_amt")
    else:
        prefix = model_cfg["report_prefix"]
    return settings.OUT_DIR / f"{prefix}_{time.strftime('%Y%m%d')}.xlsx"


def run(dataset_key: str = "laoke", model_key: str = "mlt", metric: str = "cnt") -> None:
    """按配置跑完整分箱管线并输出 Excel。"""
    apply_configs(dataset_key, model_key, metric)
    report_path = resolve_report_path(dataset_key, model_key, metric)
    run_binning(report_path)
    print(f"完成 => {report_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="单模型分箱：等频初分 → 自动合箱 → 策略阈值 → Excel 报告")
    parser.add_argument("--dataset", default="laoke", choices=sorted(DATASETS), help="样本集 key（见 configs/datasets.py）")
    parser.add_argument("--model", default="mlt", choices=sorted(MODELS), help="模型 key（见 configs/models.py）")
    parser.add_argument("--metric", default="cnt", choices=["cnt", "amt"], help="合箱口径：cnt 笔数 / amt 金额")
    return parser


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    run(args.dataset, args.model, args.metric)


if __name__ == "__main__":
    main()
