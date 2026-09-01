# -*- coding: utf-8 -*-
"""一次性数据清理：新客四张表剔除未完成申请（0.Incomplete / 1.In Progress）。

覆盖：new_sample.csv / new_application_info.csv / new_mlt_score.csv /
new_worthiness_score.csv。判定依据 new_application_info.csv 的 application_status；
状态缺失按完成保留（与管线加载口径一致）。原文件备份为 *_original.csv（幂等：备份已存在则跳过）。
"""
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "res"
APP_INFO = RES / "new_application_info.csv"
TARGETS = [
    RES / "new_sample.csv",
    RES / "new_application_info.csv",
    RES / "new_mlt_score.csv",
    RES / "new_worthiness_score.csv",
]
INCOMPLETE_STATUSES = ["0.Incomplete", "1.In Progress"]
CHUNK = 200_000


def has_bom(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(3) == b"\xef\xbb\xbf"


# 1) 构建申请状态映射（以 application_info 为基准）
print("读取 new_application_info.csv 状态…")
status_map = {}
missing_status = 0
for chunk in pd.read_csv(
    APP_INFO, usecols=["application_id", "application_status"],
    dtype={"application_id": str}, chunksize=CHUNK,
):
    missing_status += int(chunk["application_status"].isna().sum())
    status_map.update(dict(zip(chunk["application_id"], chunk["application_status"])))
print(f"申请信息状态映射 {len(status_map):,} 个 id，状态缺失 {missing_status:,}（按完成保留）")

completed = {
    aid for aid, st in status_map.items()
    if pd.isna(st) or str(st) not in INCOMPLETE_STATUSES
}
print(f"完成申请 id：{len(completed):,}\n")

# 2) 逐文件过滤
for path in TARGETS:
    backup = path.with_name(path.stem + "_original.csv")
    if not backup.exists():
        shutil.copy2(path, backup)
        print(f"已备份 -> {backup.name}")
    else:
        print(f"备份已存在，跳过备份: {backup.name}")

    before = after = 0
    kept_parts = []
    enc = "utf-8-sig" if has_bom(path) else "utf-8"
    for chunk in pd.read_csv(path, dtype={"application_id": str}, chunksize=CHUNK):
        before += len(chunk)
        if "application_status" in chunk.columns:
            st = chunk["application_status"]
        else:
            st = chunk["application_id"].map(status_map)
        keep = st.isna() | ~st.astype("string").isin(INCOMPLETE_STATUSES)
        part = chunk.loc[keep]
        after += len(part)
        kept_parts.append(part)

    pd.concat(kept_parts, ignore_index=True).to_csv(path, index=False, encoding=enc)
    print(f"{path.name}: {before:,} -> {after:,}（剔除 {before - after:,}，{((before - after) / before):.2%}）")

print("\n全部完成，四张新客表均已仅含完成申请。")
