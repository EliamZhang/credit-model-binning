# -*- coding: utf-8 -*-
"""一次性数据补充：用 res/spark3-32295.csv（老客版补充信息，与当年补 new 表的
res/额外补充信息.csv 同构）为 old_application_info.csv 追加 30 个缺失字段列。

规则（与当年 new 表补充口径一致）：
- 只补申请表【没有】的字段；重叠列（user_id/duedate_*/total_income/net_surplus/loan_tag）
  一律保留申请表原值，不从 spark 覆盖（spark 的 duedate 存 '0'/'1'、old 存 '0.0'/'1.0'，
  数值一致但格式不同，覆盖会破坏现有口径）；
- 目标结构 = old 原 36 列 + spark 新增 30 列，表头与 new_application_info.csv 完全同序同构；
- spark 未覆盖的 application_id（280,462 行）新列为空；
- 写回前把原文件备份为 old_application_info_original.csv（幂等：备份已存在则视为已合并，
  直接进入自检，不重复覆盖）。

完成后自检（断言）：行数与主键集合不变；原 36 列与备份逐值全等；新增列与 spark 逐值全等。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "res"
OLD = RES / "old_application_info.csv"
BACKUP = RES / "old_application_info_original.csv"
SPARK = RES / "spark3-32295.csv"
REF = RES / "new_application_info.csv"  # 结构参照（表头同序）
CHUNK = 200_000


def has_bom(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(3) == b"\xef\xbb\xbf"


def read_str(path: Path, cols):
    enc = "utf-8-sig" if has_bom(path) else "utf-8"
    parts = []
    for chunk in pd.read_csv(path, usecols=cols, dtype=str, chunksize=CHUNK,
                             encoding=enc, keep_default_na=False):
        parts.append(chunk)
    return pd.concat(parts, ignore_index=True)


def read_hdr(path: Path):
    enc = "utf-8-sig" if has_bom(path) else "utf-8"
    return list(pd.read_csv(path, nrows=0, encoding=enc).columns)


BASE = BACKUP if BACKUP.exists() else OLD  # 合并前基准结构（备份缺失时即当前文件）
base_hdr = read_hdr(BASE)
spk_hdr = read_hdr(SPARK)
new_hdr = read_hdr(REF)
ADD = [c for c in spk_hdr if c not in base_hdr]  # spark 新增列（保持 spark 表头顺序）
assert len(ADD) == 30, f"期望 spark 相对基准新增 30 列，实际 {len(ADD)}"
assert new_hdr == base_hdr + ADD, "目标表头与 new_application_info.csv 不一致，先人工核对"

old = read_str(OLD, base_hdr)
old_aid = old["application_id"].astype(str)
assert old_aid.is_unique, "old 主键须唯一"

spk = read_str(SPARK, ["application_id"] + ADD)
spk_aid = spk["application_id"].astype(str)
assert spk_aid.is_unique, "spark 主键须唯一"

if not BACKUP.exists():
    matched = len(set(old_aid) & set(spk_aid))
    print(f"old_application_info.csv: {len(old):,} 行")
    print(f"spark3-32295.csv: {len(spk):,} 行，其中在 old 中 {matched:,} 行"
          f"（覆盖 {matched / len(old):.2%}），未覆盖 {len(old) - matched:,} 行新列为空")
    merged = old.merge(spk, on="application_id", how="left")
    assert len(merged) == len(old) and merged.columns.tolist() == new_hdr
    out_enc = "utf-8" if not has_bom(OLD) else "utf-8-sig"
    OLD.replace(BACKUP)
    merged.to_csv(OLD, index=False, encoding=out_enc)
    print(f"已备份原表为 {BACKUP.name}，已写回 {OLD.name}（{len(merged):,} 行 × {merged.shape[1]} 列，"
          f"编码 {'utf-8-sig' if out_enc == 'utf-8-sig' else 'utf-8'}）")
else:
    print(f"备份已存在（{BACKUP.name}），视为已合并，跳过合并直接自检")

# ---- 自检 1：读回产出与备份，原 36 列逐值全等、行数/主键集合不变 ----
back = read_str(BACKUP, base_hdr)
newf = read_str(OLD, base_hdr)
assert len(newf) == len(back)
assert set(newf["application_id"].astype(str)) == set(back["application_id"].astype(str))
bad = 0
cmp_df = newf.merge(back, on="application_id", suffixes=("_n", "_b"))
for c in base_hdr:
    if c == "application_id":
        continue
    a, b = c + "_n", c + "_b"
    bad += int((cmp_df[a] != cmp_df[b]).sum())
assert bad == 0, f"原列 {bad} 处与备份不一致"
print(f"自检1 通过：原 {len(base_hdr)} 列与备份逐值全等，行数与主键集合不变")

# ---- 自检 2：新增列与 spark 逐值全等（仅匹配行） ----
newf2 = read_str(OLD, ["application_id"] + ADD)
cmp2 = newf2.merge(spk, on="application_id", suffixes=("_n", "_s"))
bad2 = 0
for c in ADD:
    a, b = c + "_n", c + "_s"
    both = (cmp2[a] != "") & (cmp2[b] != "")
    neq = (cmp2.loc[both, a] != cmp2.loc[both, b]).sum()
    # 仅 spark 有值而产出为空的错位也要算
    miss = ((cmp2[a] == "") & (cmp2[b] != "")).sum()
    bad2 += int(neq) + int(miss)
assert bad2 == 0, f"新增列 {bad2} 处与 spark 不一致"
full = newf2["raw_interest_income_3m"] != ""
print(f"自检2 通过：新增列与 spark 逐值全等（匹配 {len(cmp2):,} 行）；"
      f"raw_interest_income_3m 非空 {full.sum():,} 行（{full.mean():.2%}）")
print(f"自检3：表头 = {OLD.name} 原 36 列 + 30 新列 = new_application_info.csv 66 列同序（已断言）")
