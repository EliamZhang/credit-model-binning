"""一次性数据清理：把 sample.csv 过滤为仅含完成申请（剔除 0.Incomplete / 1.In Progress）。

执行前自动备份原始文件到 res/sample_original.csv；幂等，可重复运行。
"""
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "res"
SAMPLE = RES / "sample.csv"
APP_INFO = RES / "application_info.csv"
BACKUP = RES / "sample_original.csv"

INCOMPLETE_STATUSES = ["0.Incomplete", "1.In Progress"]

sample = pd.read_csv(SAMPLE)
app = pd.read_csv(APP_INFO, usecols=["application_id", "application_status"])

assert sample["application_id"].is_unique, "sample.application_id 存在重复"
assert app["application_id"].is_unique, "application_info.application_id 存在重复"

status_map = app.set_index("application_id")["application_status"]
sample["_application_status"] = sample["application_id"].map(status_map)

missing_status = sample["_application_status"].isna().sum()
if missing_status:
    print(f"警告：{missing_status} 笔申请在 application_info 中无状态（按完成保留，与 binning_mlt_cnt.py 口径一致）")

incomplete = sample["_application_status"].astype("string").isin(INCOMPLETE_STATUSES)
removed = int(incomplete.sum())
kept = sample.loc[~incomplete].drop(columns=["_application_status"])

print(f"原始样本量: {len(sample)}")
print(f"剔除未完成: {removed} ({removed / len(sample):.2%})")
print(f"保留完成申请: {len(kept)}")

shutil.copy2(SAMPLE, BACKUP)
print(f"已备份原始文件到 {BACKUP.name}")

kept.to_csv(SAMPLE, index=False)
print(f"已覆盖写回 {SAMPLE.name}，仅含完成申请")
