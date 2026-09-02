# -*- coding: utf-8 -*-
"""四类客群象限指标重算器：从 res/*.csv 按交叉报告分档重算四象限（双低 / 仅 mlt 低 / 仅 wth 低 / 双高）的
漏斗、风险、收入指标，先逐格核对 Excel 矩阵（样本量精确一致、比率 2 位小数一致）再输出象限表，
供 docs/四类客群矩阵_（新客mlt × 新客价值模型）.md 取数（数值纪律同 CLAUDE.md §7：不手抄）。

口径（与 pipeline/cross_analysis.py 及 scr/_gen_new_reports.py 一致）：
- 双分样本 = mlt 分 ∩ wth 分 ∩ application_info，Train = application_month ≤ 2025-10；
- 分档边界取自已评审 7 档（交叉报告附录，MLT_EDGES/WTH_EDGES 复用 _gen_new_reports.py）；
- 笔数逾期率 = duedate ∈ {0,1} 为成熟、=1 为坏；
- 金额逾期率 = dpd≥30 剩余本金 / 成熟样本本金敞口；
- 通过率 = application_status 首字符 3/4 / 完成（status 非空且非 0.Incomplete/1.In Progress，
  INCOMPLETE_STATUSES 见 pipeline/settings.py:32）；
  自动 = 含 "Auto Approved"、人工 = 含 "Manual Approved"；成交率 = status ∈ {Active_Account,Closed,Blocked} / 通过。
"""
import csv
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scr"))

import _gen_new_reports as gen

INCOMPLETE = ["0.Incomplete", "1.In Progress"]
QUADRANTS = [
    ("双低", lambda m, w: m <= 3 and w <= 3),
    ("仅 mlt 低", lambda m, w: m <= 3 and w > 3),
    ("仅 wth 低", lambda m, w: w <= 3 and m > 3),
    ("双高", lambda m, w: m > 3 and w > 3),
]
KEEP_FIELDS = [
    "application_month", "application_time", "application_status", "assessment_status",
    "status", "duedate_1m_30", "duedate_3m_30", "dpd_days_ever_mob1",
    "dpd_days_ever_mob3", "estimate_principal_remaining_mob1",
    "estimate_principal_remaining_mob3", "principal",
] + gen.INCOME_FIELDS


def read_app_rows():
    out = {}
    with open(ROOT / "res/new_application_info.csv", encoding="utf-8-sig") as f:
        r = csv.reader(f)
        hdr = next(r)
        idx = {c: hdr.index(c) for c in KEEP_FIELDS}
        for row in r:
            out[row[hdr.index("application_id")]] = {c: row[idx[c]] for c in KEEP_FIELDS}
    return out


def read_sample_labels():
    """duedate 标签以 sample 文件为准（管线里 application_info 只补充 sample 没有的字段，
    见 pipeline/data_loading.py load_analysis_data；两文件标签不一致约 5k/12k 行）。"""
    out = {}
    with open(ROOT / "res/new_sample.csv", encoding="utf-8-sig") as f:
        r = csv.reader(f)
        hdr = next(r)
        aidx = hdr.index("application_id")
        i1, i3 = hdr.index("duedate_1m_30"), hdr.index("duedate_3m_30")
        for row in r:
            out[row[aidx]] = (row[i1], row[i3])
    return out


def build_rows():
    mlt, wth = gen._read_scores()
    app = read_app_rows()
    labels = read_sample_labels()
    rows = []
    for aid in set(mlt) & set(wth) & set(app):
        a = dict(app[aid])
        if aid in labels:
            a["duedate_1m_30"], a["duedate_3m_30"] = labels[aid]
        m = a["application_month"]
        if not m:
            m = a["application_time"][:7] if a["application_time"] else ""
        if not m:
            continue
        mb = gen._bin_of(mlt[aid], gen.MLT_EDGES)
        wb = gen._bin_of(wth[aid], gen.WTH_EDGES)
        rows.append((aid, mb, wb, m <= "2025-10", a))
    return rows


def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def funnel_rates(sub):
    completed = [
        r for r in sub
        if r["application_status"] not in ("", "None") and r["application_status"] not in INCOMPLETE
    ]
    approved = [r for r in completed if r["application_status"][:1] in ("3", "4")]
    auto = [r for r in approved if "Auto Approved" in r["assessment_status"]]
    manual = [r for r in approved if "Manual Approved" in r["assessment_status"]]
    deal = [r for r in approved if r["status"] in ("Active_Account", "Closed", "Blocked")]
    nc, na, nauto, nmanual, ndeal = len(completed), len(approved), len(auto), len(manual), len(deal)
    return {
        "approval": na / nc if nc else None,
        "auto": nauto / nc if nc else None,
        "manual": nmanual / nc if nc else None,
        "deal": ndeal / na if na else None,
    }


def risk_rates(sub):
    def cnt(due_col):
        vals = [to_float(r[due_col]) for r in sub]
        mature = [v for v in vals if v in (0, 1)]
        return len(mature), sum(1 for v in mature if v == 1)

    def amt(dpd_col, rem_col):
        exposure = 0.0
        bad = 0.0
        for r in sub:
            dpd = to_float(r[dpd_col])
            p = to_float(r["principal"]) or 0.0
            if dpd is not None:
                exposure += p
                if dpd >= 30:
                    rem = to_float(r[rem_col]) or 0.0
                    bad += rem
        return exposure, bad

    m1, b1 = cnt("duedate_1m_30")
    m3, b3 = cnt("duedate_3m_30")
    e1, a1 = amt("dpd_days_ever_mob1", "estimate_principal_remaining_mob1")
    e3, a3 = amt("dpd_days_ever_mob3", "estimate_principal_remaining_mob3")
    return {
        "1m30p_mature": m1, "1m30p_bad": b1, "1m30p_rate": b1 / m1 if m1 else None,
        "3m30p_mature": m3, "3m30p_bad": b3, "3m30p_rate": b3 / m3 if m3 else None,
        "1m30p_amt_rate": a1 / e1 if e1 else None,
        "3m30p_amt_rate": a3 / e3 if e3 else None,
    }


def income_medians(sub):
    acc = {f: [] for f in gen.INCOME_FIELDS}
    for r in sub:
        for f in gen.INCOME_FIELDS:
            v = to_float(r[f])
            if v is not None:
                acc[f].append(v)
    return {f: gen._median(vals) if vals else None for f, vals in acc.items()}


def cell_key(rows, g, mb, wb):
    return [(aid, mb2, wb2, g2, a) for (aid, mb2, wb2, g2, a) in rows if g2 == g and mb2 == mb and wb2 == wb]


def verify_vs_excel(rows):
    """逐格核对：样本量精确一致；比率指标 2 位小数与 Excel 矩阵一致（n≥100 才显示）。"""
    wb = gen.load(gen.CROSS_XLSX)
    ok = True
    for sheet, g in [("02_交叉矩阵_Train", 1), ("03_交叉矩阵_OOT", 0)]:
        tbl = gen.find_table(wb[sheet], "new_mlt_bin_order")
        for t in tbl:
            mb, wb2 = t["new_mlt_bin_order"], t["new_wth_bin_order"]
            if not (isinstance(mb, int) and mb > 0 and isinstance(wb2, int) and wb2 > 0):
                continue
            mine = cell_key(rows, g, mb, wb2)
            n = len(mine)
            if n != int(t["n"]):
                print(f"[FAIL] {sheet} 格({mb},{wb2}) n 重算={n} Excel={t['n']}")
                ok = False
                continue
            fr = funnel_rates([r[4] for r in mine])
            rr = risk_rates([r[4] for r in mine])
            for key, val in [
                ("actual_approval_rate", fr["approval"]), ("actual_deal_rate", fr["deal"]),
                ("1m30p_cnt_bad_rate", rr["1m30p_rate"]), ("3m30p_cnt_bad_rate", rr["3m30p_rate"]),
                ("1m30p_amt_bad_rate", rr["1m30p_amt_rate"]), ("3m30p_amt_bad_rate", rr["3m30p_amt_rate"]),
            ]:
                exl = t[key]
                if val is None or exl is None:
                    if n >= 100 and not (val is None and exl is None):
                        print(f"[FAIL] {sheet} 格({mb},{wb2}) {key} 重算=None Excel={exl}")
                        ok = False
                    continue
                if round(val * 100, 2) != round(float(exl) * 100, 2):
                    print(f"[FAIL] {sheet} 格({mb},{wb2}) {key} 重算={val:.6f} Excel={exl}")
                    ok = False
    print("逐格核对（样本量精确 + 6 项比率 2 位小数）:", "全部一致" if ok else "存在不一致")
    return ok


def quadrant_stats(rows, g):
    sub = [r for r in rows if r[3] == g]
    total = len(sub)
    out = {}
    for name, cond in QUADRANTS:
        q = [r for r in sub if cond(r[1], r[2])]
        fr = funnel_rates([r[4] for r in q])
        rr = risk_rates([r[4] for r in q])
        im = income_medians([r[4] for r in q])
        out[name] = {"n": len(q), "sample_pct": len(q) / total, **fr, **rr, **im}
    return out, total


def verify_vs_report(quad, group_name):
    """象限样本量与 06_二维策略模拟 四象限表一致（报告六（三）数值）。"""
    wb = gen.load(gen.CROSS_XLSX)
    tbl = gen.find_table(wb["06_二维策略模拟"], "sample_group")
    ok = True
    for t in tbl:
        if t["sample_group"] != group_name:
            continue
        # 列名适配：样本量/占比/1M30+/3M30+
        cols = {k: t[k] for k in t if k not in ("sample_group",)}
        print("  Excel 四象限行:", cols)
    # 用报告数值硬校验
    expected = {
        "train": {"双低": 66565, "仅 mlt 低": 96289, "仅 wth 低": 14862, "双高": 229418},
        "oot": {"双低": 24174, "仅 mlt 低": 32446, "仅 wth 低": 4705, "双高": 68057},
    }[group_name]
    for name, n in expected.items():
        if quad[name]["n"] != n:
            print(f"[FAIL] {group_name} {name} n 重算={quad[name]['n']} 报告={n}")
            ok = False
    print(f"{group_name} 四象限样本量与报告一致:", ok)
    return ok


def verify_md(quads):
    """读 docs/四类客群矩阵 md，逐项核对各客群表格数值与重算结果一致。"""
    text = (ROOT / "docs" / "四类客群矩阵_（新客mlt × 新客价值模型）.md").read_text(encoding="utf-8")
    seg2quad = {
        "稳健经营型优质客": "双低",
        "高薪高波动型周转客": "仅 wth 低",
        "低收入克制型刚需客": "仅 mlt 低",
        "现金流脆弱型谨慎客": "双高",
    }
    fails = []

    def check(seg, label, expected):
        if f"| {label}" not in text or expected not in text:
            fails.append(f"{seg} {label}: 期望 {expected}")

    for seg, qname in seg2quad.items():
        s = quads["train"][qname]
        check(seg, "样本占比", f"{s['sample_pct']*100:.2f}%")
        check(seg, "样本量（Train 双分样本）", f"{s['n']:,}")
        check(seg, "申请通过率", f"{s['approval']*100:.2f}%")
        check(seg, "自动/人工通过率", f"{s['auto']*100:.2f}%/{s['manual']*100:.2f}%")
        check(seg, "通过后成交率", f"{s['deal']*100:.2f}%")
        check(seg, "3M30+ 笔数逾期率（Train）", f"{s['3m30p_rate']*100:.2f}%")
        check(seg, "3M30+ 笔数逾期率（OOT 验证）", f"{quads['oot'][qname]['3m30p_rate']*100:.2f}%")
        check(seg, "3M30+ 金额逾期率（Train）", f"{s['3m30p_amt_rate']*100:.2f}%")
        check(seg, "收入中位数（元）", f"{s['total_income']:,.0f}")
        check(seg, "毛/净盈余中位数（元）", f"{s['gross_surplus']:,.0f}/{s['net_surplus']:,.0f}")
        check(seg, "3M30 成熟样本（Train）", f"{s['3m30p_mature']:,}")
    # 整体结论关键值
    for v in ["0.5799", "40.00%", "7.40%", "6.05%", "7.57%", "3.65%", "14.42%", "13.01%", "16.35%", "23.65%", "56.35%"]:
        if v not in text:
            fails.append(f"整体结论缺 {v}")
    print("md 逐项核对:", "全部一致" if not fails else f"{len(fails)} 处不一致")
    for f in fails[:30]:
        print("  [FAIL]", f)
    return not fails


def main():
    rows = build_rows()
    print(f"双分样本（含 application_info）: {len(rows)}")
    ntr = sum(1 for r in rows if r[3])
    print(f"Train={ntr} OOT={len(rows) - ntr}")
    verify_vs_excel(rows)
    # 收入中位数单元格级与生成器一致（生成器已逐格核对并发布）
    inc = gen.income_matrix_stats()
    for g, gname in [(1, "Train"), (0, "OOT")]:
        for mb in range(1, 8):
            for wb2 in range(1, 8):
                mine = income_medians([r[4] for r in cell_key(rows, g, mb, wb2)])
                for f in gen.INCOME_FIELDS:
                    a, b = mine[f], inc.get((g, "cell", (mb, wb2)), {}).get(f)
                    if a != b:
                        print(f"[FAIL] 收入 {gname} 格({mb},{wb2}) {f} 重算={a} 生成器={b}")

    quads = {}
    for g, gname in [(1, "train"), (0, "oot")]:
        quad, total = quadrant_stats(rows, g)
        verify_vs_report(quad, gname)
        quads[gname] = quad
        print(f"\n===== {gname}（总 {total:,}）=====")
        for name, s in quad.items():
            print(f"\n### {name}  n={s['n']:,}  占比={s['sample_pct']*100:.2f}%")
            print(f"  通过率={s['approval']*100:.2f}%  自动={s['auto']*100:.2f}%  人工={s['manual']*100:.2f}%  成交率={s['deal']*100:.2f}%")
            print(f"  1M30+={s['1m30p_rate']*100:.2f}%（成熟 {s['1m30p_mature']:,}/{s['1m30p_bad']:,}）  3M30+={s['3m30p_rate']*100:.2f}%（成熟 {s['3m30p_mature']:,}/{s['3m30p_bad']:,}）")
            print(f"  1M30+ 金额={s['1m30p_amt_rate']*100:.2f}%  3M30+ 金额={s['3m30p_amt_rate']*100:.2f}%")
            print(f"  收入={s['total_income']:,.0f}  支出={s['total_expenses']:,.0f}  毛盈余={s['gross_surplus']:,.0f}  净盈余={s['net_surplus']:,.0f}")
    verify_md(quads)


if __name__ == "__main__":
    main()
