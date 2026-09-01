# -*- coding: utf-8 -*-
"""一次性数据对齐：以 new_sample.csv 为主表，把其余新客表裁剪到同一 application_id 集。

覆盖：new_application_info.csv / new_mlt_score.csv / new_worthiness_score.csv。
规则：只保留主表内存在的 application_id；按 application_id 去重（保留第一条，与管线
加载口径一致）。幂等：主表不变时重跑结果不变。原始文件已在 _filter_new_completed.py
备份为 *_original.csv，此处直接覆盖目标文件。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "res"
SAMPLE = RES / "new_sample.csv"
TARGETS = [
    RES / "new_application_info.csv",
    RES / "new_mlt_score.csv",
    RES / "new_worthiness_score.csv",
]
CHUNK = 200_000


def has_bom(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(3) == b"\xef\xbb\xbf"


# 1) 主表 id 集
sample_ids = set()
for chunk in pd.read_csv(SAMPLE, usecols=["application_id"], dtype={"application_id": str}, chunksize=CHUNK):
    sample_ids.update(chunk["application_id"].dropna().astype(str))
print(f"主表 new_sample.csv：{len(sample_ids):,} 个 application_id\n")

# 2) 逐表对齐：过滤 + 全局去重（保留第一条，跨 chunk 有效）
for path in TARGETS:
    before = after = 0
    kept_parts = []
    seen: set[str] = set()
    enc = "utf-8-sig" if has_bom(path) else "utf-8"
    for chunk in pd.read_csv(path, dtype={"application_id": str}, chunksize=CHUNK):
        before += len(chunk)
        part = chunk[chunk["application_id"].astype(str).isin(sample_ids)]
        # 跨 chunk 去重：剔除此前已保留的 id
        part = part.loc[~part["application_id"].astype(str).isin(seen)]
        # 块内去重：同一 chunk 内的重复行需再按 id 去重（保留第一条）
        part = part.drop_duplicates(subset="application_id", keep="first")
        seen.update(part["application_id"].astype(str).dropna())
        after += len(part)
        kept_parts.append(part)

    pd.concat(kept_parts, ignore_index=True).to_csv(path, index=False, encoding=enc)
    print(f"{path.name}: {before:,} -> {after:,}（剔除主表外/重复 {before - after:,}）")

print("\n对齐完成：三张新客表均与 new_sample.csv 主键一致。")
