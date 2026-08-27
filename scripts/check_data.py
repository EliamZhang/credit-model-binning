# -*- coding: utf-8 -*-
"""
新数据质量检查（数据入库后的第一道工序）。

用法：
    python scripts/check_data.py --dataset laoke --model mlt
    python scripts/check_data.py --dataset <d> --model <m>   # 新样本/新模型数据

按 configs 读取样本集与模型配置对应的三张表（sample / application_info / 模型分文件），
逐项检查并输出分级结论：PASS（全部通过）/ WARN（有提示项，需向用户报告并确认）
/ BLOCK（有阻断项，停下等用户补数据）。

检查报告写入 out/data_check_<dataset>_<model>_YYYYMMDD.txt 并在控制台打印。
检查口径与 CLAUDE.md 2.1 / 2.3 节一致；阈值基线参考老客（价值模型 score 缺失 6.68%
为已知结构性原因：无银行交易数据）。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from configs.datasets import DATASETS
from configs.models import MODELS

REQUIRED_FIELDS = {
    "sample": ["application_id", "user_id"],
    "application": [
        "application_id", "user_id", "application_time",
        "duedate_1m_30", "duedate_3m_30", "principal",
        "estimate_principal_remaining_mob1", "estimate_principal_remaining_mob3",
        "dpd_days_ever_mob1", "dpd_days_ever_mob3",
        "status", "application_status", "assessment_status",
    ],
    "score": ["application_id", "<score_col>"],
}

# 分级阈值（CLAUDE.md 2.3 的口径）。
SCORE_MISSING_WARN = 0.05
SCORE_MISSING_BLOCK = 0.20
KEY_MISSING_WARN = 0.05
DUP_RATE_WARN = 0.005
SCORE_DUP_RATE_WARN = 0.01
COVERAGE_BLOCK = 0.80
COVERAGE_WARN = 0.95
TRAIN_MATURITY_WARN = 0.35  # 明显低于老客基线（约 42%）才警告


def pct(numerator, denominator):
    return numerator / denominator if denominator else np.nan


class Report:
    def __init__(self):
        self.blocks = []
        self.warns = []
        self.passes = []

    def add(self, level, item, detail):
        (self.blocks if level == "BLOCK" else self.warns if level == "WARN" else self.passes).append((item, detail))

    def summary(self):
        if self.blocks:
            return "BLOCK（存在阻断项，停下等用户补数据）"
        if self.warns:
            return "WARN（存在提示项，向用户报告并确认后继续）"
        return "PASS（全部检查通过）"

    def render(self):
        lines = []
        for level, items in [("BLOCK", self.blocks), ("WARN", self.warns), ("PASS", self.passes)]:
            for item, detail in items:
                lines.append(f"[{level}] {item}: {detail}")
        return "\n".join(lines)


def check(dataset_key: str, model_key: str) -> Report:
    dataset_cfg = DATASETS[dataset_key]
    model_cfg = MODELS[model_key]
    data_dir = Path(dataset_cfg["data_dir"])
    report = Report()

    # 1) 文件存在与可读。
    files = {
        "sample": data_dir / dataset_cfg["sample_file"],
        "application": data_dir / dataset_cfg["application_file"],
        "score": data_dir / model_cfg["score_file"],
    }
    frames = {}
    for name, path in files.items():
        if not path.exists():
            report.add("BLOCK", f"{name} 文件不存在", str(path))
            continue
        try:
            frame = pd.read_csv(path, low_memory=False)
            frames[name] = frame
            report.add("PASS", f"{name} 文件可读", f"{len(frame):,} 行 × {len(frame.columns)} 列")
        except Exception as exc:  # noqa: BLE001
            report.add("BLOCK", f"{name} 文件读取失败", str(exc))
    if len(frames) < 3:
        return report

    sample, application, score = frames["sample"], frames["application"], frames["score"]
    score_col = model_cfg["raw_score_col"]

    # 2) 必需字段存在性。
    for table_name, frame, fields in [
        ("sample", sample, REQUIRED_FIELDS["sample"]),
        ("application", application, REQUIRED_FIELDS["application"]),
        ("score", score, [score_col]),
    ]:
        missing = [f for f in fields if f not in frame.columns]
        if missing:
            report.add("BLOCK", f"{table_name} 缺字段", "、".join(missing))
        else:
            report.add("PASS", f"{table_name} 必需字段齐全", "")

    # 3) 主键唯一性。
    for name, frame in [("sample", sample), ("application", application), ("score", score)]:
        total = len(frame)
        dup = int(frame["application_id"].duplicated().sum())
        rate = pct(dup, total)
        if name == "score":
            threshold = SCORE_DUP_RATE_WARN
        else:
            threshold = DUP_RATE_WARN
        if pd.isna(rate) or rate > threshold:
            report.add("WARN", f"{name} application_id 重复率偏高", f"{rate:.2%}（{dup:,}/{total:,}；管线保留第一条）")
        else:
            report.add("PASS", f"{name} 主键唯一性", f"重复率 {rate:.2%}")

    # 4) 关键字段缺失率。
    # 标签/敞口类字段（duedate/dpd/principal/remaining）只在成熟样本有值：
    # 老客基线缺失率约 66%–69% 属正常（缺失 = 未成熟），阈值放宽到 90%，
    # 真正的问题（成熟样本也无值）由 Train 成熟率检查与管线兜底。
    for col in ["duedate_1m_30", "duedate_3m_30", "principal",
                "estimate_principal_remaining_mob1", "estimate_principal_remaining_mob3",
                "dpd_days_ever_mob1", "dpd_days_ever_mob3"]:
        if col not in application.columns:
            continue
        rate = pct(int(application[col].isna().sum()), len(application))
        if pd.isna(rate) or rate > 0.90:
            report.add("WARN", f"application.{col} 缺失率异常高",
                       f"{rate:.2%}（缺失多为未成熟样本，老客基线约 66%–69%；超过 90% 需确认）")
        else:
            report.add("PASS", f"application.{col} 缺失率", f"{rate:.2%}（缺失 = 未成熟样本，属正常）")
    application_time_rate = pct(int(application["application_time"].isna().sum()), len(application)) \
        if "application_time" in application.columns else np.nan
    if not pd.isna(application_time_rate) and application_time_rate > KEY_MISSING_WARN:
        report.add("WARN", "application.application_time 缺失率偏高", f"{application_time_rate:.2%}")
    elif not pd.isna(application_time_rate):
        report.add("PASS", "application.application_time 缺失率", f"{application_time_rate:.2%}")

    # 5) 样本口径的模型分覆盖率与缺失率（与管线 01_总览 口径一致）。
    score_dedup = score.drop_duplicates(subset="application_id", keep="first")
    merged = sample.merge(score_dedup[["application_id", score_col]], on="application_id", how="left")
    coverage = pct(int(pd.to_numeric(merged[score_col], errors="coerce").notna().sum()), len(sample))
    if pd.isna(coverage) or coverage < COVERAGE_BLOCK:
        report.add("BLOCK", "样本底表的模型分覆盖率过低", f"{coverage:.2%}")
    elif not pd.isna(coverage) and coverage < COVERAGE_WARN:
        report.add("WARN", "样本底表的模型分覆盖率偏低", f"{coverage:.2%}（老客 mlt 为 100.00%、价值模型 93.32%）")
    else:
        report.add("PASS", "样本底表的模型分覆盖率", f"{coverage:.2%}")

    # 样本口径的模型分缺失率（原始文件缺失率含未完成申请，仅供参考，不参与分级）。
    raw_score_rate = pct(int(score[score_col].isna().sum()), len(score))
    sample_score_rate = 1 - coverage if not pd.isna(coverage) else np.nan
    if not pd.isna(sample_score_rate) and sample_score_rate > SCORE_MISSING_BLOCK:
        report.add("BLOCK", "样本口径模型分缺失率过高", f"{sample_score_rate:.2%}")
    elif not pd.isna(sample_score_rate) and sample_score_rate > SCORE_MISSING_WARN:
        report.add("WARN", "样本口径模型分缺失率偏高",
                   f"{sample_score_rate:.2%}（老客价值模型 6.68% 为无银行交易数据所致；需向用户确认原因）")
    else:
        report.add("PASS", "样本口径模型分缺失率", f"{sample_score_rate:.2%}（原始文件缺失率 {raw_score_rate:.2%}）")

    # 6) 分数分布与方向验证（十分位 3M30+）。
    if score_col in score.columns and "duedate_3m_30" in application.columns:
        app_dedup = application.drop_duplicates(subset=["application_id", "user_id"], keep="first")
        joined = score_dedup[["application_id", score_col]].merge(
            app_dedup[["application_id", "duedate_3m_30"]], on="application_id", how="inner"
        )
        joined[score_col] = pd.to_numeric(joined[score_col], errors="coerce")
        joined["duedate_3m_30"] = pd.to_numeric(joined["duedate_3m_30"], errors="coerce")
        valid = joined.dropna(subset=[score_col])
        report.add("PASS", "模型分取值范围", f"{valid[score_col].min():.4f} ~ {valid[score_col].max():.4f}，唯一值 {valid[score_col].nunique():,} 个")
        try:
            valid["dec"] = pd.qcut(valid[score_col], 10, labels=False, duplicates="drop")
            rates = []
            for d in sorted(valid["dec"].dropna().unique().astype(int)):
                sub = valid.loc[valid["dec"].eq(d)]
                mature = int(sub["duedate_3m_30"].isin([0, 1]).sum())
                bad = int(sub["duedate_3m_30"].eq(1).sum())
                rates.append(bad / mature if mature else np.nan)
            rates = [r for r in rates if not pd.isna(r)]
            if len(rates) >= 3:
                inversions = int((np.diff(rates) < 0).sum())
                if inversions >= 3:
                    report.add("WARN", "十分位 3M30+ 方向多次倒挂", f"{inversions} 处（方向验证需与用户确认）")
                else:
                    report.add("PASS", "十分位 3M30+ 方向",
                               f"{rates[0]:.2%} → {rates[-1]:.2%}（倒挂 {inversions} 处）")
        except ValueError:
            report.add("WARN", "模型分唯一值过少，无法做十分位方向验证", f"唯一值 {valid[score_col].nunique():,}")
    else:
        report.add("WARN", "无法做方向验证", "缺少 duedate_3m_30 标签或分数列")

    # 7) 月份覆盖与切分。
    month_col = "application_month"
    if month_col in application.columns:
        month = application[month_col].astype("string").str.slice(0, 7)
        train_end = dataset_cfg["train_end_month"]
        oot_start = dataset_cfg["oot_start_month"]
        train_n = int((month.notna() & month.le(train_end)).sum())
        oot_n = int((month.notna() & month.ge(oot_start)).sum())
        unknown_n = int(month.notna().sum()) - train_n - oot_n
        report.add("PASS", "Train/OOT 切分样本量",
                   f"Train ≤{train_end}: {train_n:,}；OOT ≥{oot_start}: {oot_n:,}；范围外 {unknown_n:,}")
        if "duedate_3m_30" in application.columns:
            # 完成申请口径（剔除未完成申请）的 Train 成熟率，与管线口径一致。
            incomplete = dataset_cfg.get("incomplete_statuses", [])
            completed = application.copy()
            if "application_status" in completed.columns:
                completed = completed.loc[
                    ~completed["application_status"].astype("string").isin(incomplete)
                ]
            train_app = completed.loc[month.notna() & month.le(train_end)]
            mature = int(pd.to_numeric(train_app["duedate_3m_30"], errors="coerce").isin([0, 1]).sum())
            maturity = pct(mature, len(train_app))
            if pd.isna(maturity) or maturity < TRAIN_MATURITY_WARN:
                report.add("WARN", "Train 3M30+ 标签成熟率偏低", f"{maturity:.2%}（老客基线约 42%）")
            else:
                report.add("PASS", "Train 3M30+ 标签成熟率（完成申请口径）", f"{maturity:.2%}")
    else:
        report.add("WARN", "缺少 application_month 列", "无法检查月份切分")

    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="新数据质量检查（数据入库后的第一道工序）")
    parser.add_argument("--dataset", default="laoke", choices=sorted(DATASETS))
    parser.add_argument("--model", default="mlt", choices=sorted(MODELS))
    args = parser.parse_args(argv)

    report = check(args.dataset, args.model)
    out_path = Path("out") / f"data_check_{args.dataset}_{args.model}_{time.strftime('%Y%m%d')}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"数据质量检查：dataset={args.dataset}（{DATASETS[args.dataset]['name']}）"
        f" model={args.model}（{MODELS[args.model]['name']}）\n"
        f"结论：{report.summary()}\n\n"
        f"{report.render()}\n"
    )
    out_path.write_text(text, encoding="utf-8")
    print(text)
    return report.summary()


if __name__ == "__main__":
    main()
