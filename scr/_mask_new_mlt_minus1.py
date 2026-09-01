# -*- coding: utf-8 -*-
"""一次性数据准备：新客 mlt 分文件 -1.0 特殊值改空值（按缺失分处理）。

背景（2026-09-01 用户确认）：new_mlt_score.csv 有 6.61%（37,987 笔）分数恰为 -1.0，
与价值模型空值人群 99.1% 同批（无银行交易数据人群的兜底分）。若按正常最低分参与分箱
会落入 A 档（最安全档）被自动放行，而实际违约率 24.32%（A 档正常人群 1.83%）。
处理口径：-1.0 置空 → 管线按缺失分剔除并按拒绝处理，与价值模型缺失口径一致。

幂等：分数列已无 -1.0 时跳过写回；原文件备份为 new_mlt_score_nomask.csv（仅当不存在时）。
"""
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "res" / "new_mlt_score.csv"
BACKUP = ROOT / "res" / "new_mlt_score_nomask.csv"
SCORE_COL = "aus_new_risk_bid_3rdmodel_v1_0_20251201"
MASK_VALUE = -1.0


def has_bom(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(3) == b"\xef\xbb\xbf"


df = pd.read_csv(PATH, dtype={"application_id": str})
score = pd.to_numeric(df[SCORE_COL], errors="coerce")
n_mask = int((score == MASK_VALUE).sum())
print(f"{PATH.name}: 共 {len(df):,} 行，{SCORE_COL} == -1.0 共 {n_mask:,} 行（{n_mask / len(df):.2%}）")

if n_mask == 0:
    print("已处理过（无 -1.0），跳过写回。")
    sys.exit(0)

if not BACKUP.exists():
    shutil.copy2(PATH, BACKUP)
    print(f"已备份 -> {BACKUP.name}")
else:
    print(f"备份已存在，跳过备份: {BACKUP.name}")

df.loc[score == MASK_VALUE, SCORE_COL] = pd.NA
enc = "utf-8-sig" if has_bom(PATH) else "utf-8"
df.to_csv(PATH, index=False, encoding=enc)

# 验证
chk = pd.read_csv(PATH, dtype={"application_id": str})
chk_score = pd.to_numeric(chk[SCORE_COL], errors="coerce")
print(f"写回完成：{len(chk):,} 行，其中空分数 {int(chk_score.isna().sum()):,} 行（{chk_score.isna().mean():.2%}），残留 -1.0 {int((chk_score == MASK_VALUE).sum()):,}")
