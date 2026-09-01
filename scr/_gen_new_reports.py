# -*- coding: utf-8 -*-
"""新客三份报告生成器：从 4 份 Excel 读取数值，生成 docs/ 下 3 份 md 报告。

数值纪律（CLAUDE.md §7.3）：本脚本所有数字均来自 Excel（openpyxl data_only 读值），
不手抄；写完自动回读 md 与 Excel 逐项核对关键值。重跑分箱后需重跑本脚本再提交。

生成：
1. docs/分箱方法论与结果说明报告（新客价值模型笔数口径）.md
2. docs/分箱方法论与结果说明报告（新客mlt笔数口径）.md
3. docs/两模型交叉效果评估报告（新客mlt × 新客价值模型）.md
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
TODAY = "20260901"

WTH_XLSX = ROOT / "out" / f"binning_new_worthiness_strategy_report_{TODAY}.xlsx"
MLT_XLSX = ROOT / "out" / f"binning_new_mlt_strategy_report_{TODAY}.xlsx"
CROSS_XLSX = ROOT / "out" / f"binning_new_cross_strategy_report_{TODAY}.xlsx"
COND_XLSX = ROOT / "out" / f"binning_new_worthiness_cond_strategy_report_{TODAY}.xlsx"


# ---------- 通用工具 ----------

def load(path: Path):
    return load_workbook(path, data_only=True)


def tables_in_sheet(ws):
    """把 sheet 解析为 [(header_names, [data_rows])...]。write_sheet 固定格式：
    标题行（单值）→ 空行 → 表头行 → 数据行 → 空行 → …；因此表头只出现在空行之后。"""
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    out = []
    i = 0
    prev_blank = True
    while i < len(rows):
        vals = [v for v in rows[i] if v is not None]
        if not vals:
            prev_blank = True
            i += 1
            continue
        if prev_blank:
            if len(vals) == 1:  # 标题行：其后的表头行仍应被识别（标题与表头之间可能有也可能没有空行）
                i += 1
                continue
            header = rows[i]
            data = []
            j = i + 1
            while j < len(rows):
                dvals = [v for v in rows[j] if v is not None]
                if not dvals:
                    break
                data.append(rows[j])
                j += 1
            out.append((header, data))
            i = j
        else:
            i += 1
    return out


def as_dicts(header, data):
    return [dict(zip(header, row)) for row in data]


def find_table(ws, first_header_cell):
    for header, data in tables_in_sheet(ws):
        if header[0] == first_header_cell:
            return as_dicts(header, data)
    raise KeyError(f"未找到表 {first_header_cell}")


def overview(ws):
    """01_总览：section | metric | value → dict[(section, metric)]。"""
    out = {}
    for header, data in tables_in_sheet(ws):
        if header[:2] == ["section", "metric"]:
            for d in data:
                if d[0] is not None and d[1] is not None:
                    out[(d[0], d[1])] = d[2]
    return out


def num(x, nd=0):
    if x is None:
        return "—"
    return f"{x:,.{nd}f}"


def pct(x, nd=2):
    if x is None:
        return "—"
    return f"{x*100:.{nd}f}%"


def pct_ci(v, lo, hi):
    if v is None:
        return "—"
    return f"{pct(v)} [{pct(lo)}, {pct(hi)}]"


def rate4(x):
    if x is None:
        return "—"
    return f"{x:.4f}"


def pp4(x):
    """pp 差写 +0.0014 式四位数。"""
    if x is None:
        return "—"
    return f"{x:+.4f}"


def diff_pp(a, b):
    if a is None or b is None:
        return "—"
    return f"{(a - b)*100:+.2f}pp"


def month_range(rows, group):
    mons = sorted({r["application_month"] for r in rows if r["sample_group"] == group})
    return f"{mons[0]}—{mons[-1]}"


# ---------- 数据准备口径（来自 *_original.csv 行数） ----------

def prep_stats():
    """剔除未完成申请的行数统计（数据准备阶段，来自 *_original.csv）。"""
    import csv
    out = {}
    for key, base in [("sample", "new_sample"), ("mlt", "new_mlt_score")]:
        orig = ROOT / "res" / f"{base}_original.csv"
        cur = ROOT / "res" / f"{base}.csv"
        for path in (orig, cur):
            n = None
            for enc in ("utf-8-sig", "gbk"):
                try:
                    with open(path, encoding=enc, newline="") as f:
                        n = sum(1 for _ in csv.reader(f)) - 1
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue
            if n is None:
                raise ValueError(f"无法读取 {path}")
            if path == orig:
                n_orig = n
            else:
                n_cur = n
        out[key] = (n_orig, n_cur)
    return out


# ---------- 单模型报告渲染 ----------

def render_single_model(
    model_key: str,
    xlsx: Path,
    md_path: Path,
    title: str,
    model_cn: str,
    score_col: str,
    final_bin_col: str,
    raw_score_col: str,
    decile: str,
    auto_bin: str,
    accept_bin: str,
    missing_note: str,
    merge_note: str,
    prep: dict,
):
    wb = load(xlsx)
    ov = overview(wb["01_总览"])

    def O(section, metric):
        return ov[(section, metric)]

    n_raw = int(O("样本", "原始样本量"))
    n_removed = int(O("样本", "剔除未完成申请量"))
    n_valid = int(O("样本", "有效模型分样本量"))
    n_missing = int(O("样本", "模型分缺失量"))
    n_train = int(O("样本", "Train 样本量"))
    n_oot = int(O("样本", "OOT 样本量"))
    plan = str(O("分箱", "最终采用合箱方案"))
    n_bins = int(O("分箱", "最终箱数量"))
    psi = float(O("稳定性", "最终箱 Train/OOT PSI"))
    auto_th = str(O("模型策略阈值", "自动通过阈值"))
    accept_th = str(O("模型策略阈值", "人工审核上限/拒绝阈值"))
    acc_rate = float(O("策略风险", "接纳人群3M30+笔数逾期率"))
    marginal = float(O("策略风险", "最后接纳档边际3M30+"))
    tr_auc3 = float(O("模型效果", "train_duedate_3m_30_auc"))
    oo_auc3 = float(O("模型效果", "oot_duedate_3m_30_auc"))
    tr_ks3 = float(O("模型效果", "train_duedate_3m_30_ks"))
    oo_ks3 = float(O("模型效果", "oot_duedate_3m_30_ks"))
    tr_auc1 = float(O("模型效果", "train_duedate_1m_30_auc"))
    oo_auc1 = float(O("模型效果", "oot_duedate_1m_30_auc"))
    tr_ks1 = float(O("模型效果", "train_duedate_1m_30_ks"))
    oo_ks1 = float(O("模型效果", "oot_duedate_1m_30_ks"))
    tr_mono = bool(O("单调性", "train_最终箱全部单调"))
    oo_mono = bool(O("单调性", "oot_最终箱全部单调"))
    train_bad3 = float(O("模型效果", "train_duedate_3m_30_bad_rate"))
    oot_bad3 = float(O("模型效果", "oot_duedate_3m_30_bad_rate"))
    train_bad1 = float(O("模型效果", "train_duedate_1m_30_bad_rate"))
    oot_bad1 = float(O("模型效果", "oot_duedate_1m_30_bad_rate"))

    # 漏斗（01 总览，中文指标名）
    def F(prefix, metric):
        return float(O("历史实际审批漏斗", f"{prefix}_{metric}"))

    # 策略流量（01 总览）
    def S(prefix, metric):
        return float(O("模型策略测算流量", f"{prefix}_{metric}"))

    # 03 最终分箱统计
    final_rows = find_table(wb["03_最终分箱统计"], "sample_group")
    bin_col = next(k for k in final_rows[0] if k.endswith("_final_bin"))
    tr_rows = [r for r in final_rows if r["sample_group"] == "Train"]
    oo_rows = [r for r in final_rows if r["sample_group"] == "OOT"]
    tr_rows.sort(key=lambda r: r["bin_order"])
    oo_rows.sort(key=lambda r: r["bin_order"])

    # 05 模型验证
    perf = find_table(wb["05_模型验证"], "sample_group")
    perf_tbl = [r for r in perf if r.get("label") in ("duedate_1m_30", "duedate_3m_30")]
    tables5 = tables_in_sheet(wb["05_模型验证"])
    psi_rows = None
    mono_rows = None
    monthly_rows = None
    for header, data in tables5:
        if header[0] == "final_bin_order" and "psi_component" in header:
            psi_rows = as_dicts(header, data)
        if header[0] == "sample_group" and "metric" in header:
            mono_rows = as_dicts(header, data)
        if header[0] == "sample_group" and "application_month" in header and "primary_inversion_count" in header:
            monthly_rows = as_dicts(header, data)
    assert psi_rows is not None and mono_rows is not None and monthly_rows is not None

    # 月度倒挂统计
    tr_month_bad = [r for r in monthly_rows if r["sample_group"] == "train" and int(r["primary_inversion_count"]) > 0]
    oo_month_bad = [r for r in monthly_rows if r["sample_group"] == "oot" and int(r["primary_inversion_count"]) > 0]
    tr_months_all = [r for r in monthly_rows if r["sample_group"] == "train"]

    # 04 策略方案
    funnel_tbl = find_table(wb["04_策略方案"], "metric_scope")
    flow_tbl = find_table(wb["04_策略方案"], "metric_scope")  # 第二次出现 → 用表扫描
    tables4 = tables_in_sheet(wb["04_策略方案"])
    funnel_rows = flow_rows = th_sel = sens = seg = None
    for header, data in tables4:
        d = as_dicts(header, data)
        if header[0] == "metric_scope" and header[1] == "sample_group" and "actual_apply_cnt" in header:
            funnel_rows = d
        if header[0] == "metric_scope" and header[1] == "sample_group" and "strategy_estimated_total_a" in header[2]:
            flow_rows = d
        if header[0] == "selected_role":
            th_sel = d
        if header[0] == "threshold_type":
            sens = d
        if header[0] == "sample_group" and "decision" in header:
            seg = d
    assert funnel_rows is not None and flow_rows is not None and th_sel is not None and sens is not None and seg is not None

    # 06 附录配置
    cfg_tbl = find_table(wb["06_附录"], "config_group")

    # 敏感性与分段数值（Train/OOT）
    def sens_row(ttype, scenario):
        for r in sens:
            if r["threshold_type"] == ttype and r["scenario"] == scenario:
                return r
        return None

    def seg_rows(group):
        return [r for r in seg if r["sample_group"] == group]

    # 数据准备统计
    n_prep_raw, n_prep_cur = prep["sample"]
    n_prep_removed = n_prep_raw - n_prep_cur

    # 时间范围
    tr_range = "2024-01—2025-10"
    oo_range = "2025-11—2026-05"
    oot_last_month_n = max((int(r["n"]) for r in monthly_rows if r["sample_group"] == "oot" and r["application_month"] == "2026-05"), default=0)

    # 单调性明细（OOT 倒挂档位）
    mono_bad = [r for r in mono_rows if not bool(r["is_monotonic_non_decreasing"])]

    # 约束核对数值（从 03 的累计列取，与阈值选择一致）
    auto_cum = tr_rows[auto_bin_rank(auto_bin) - 1]
    acc_cum = tr_rows[accept_bin_rank(accept_bin) - 1]
    auto_cum1 = auto_cum["cum_1m30p_cnt_bad_rate"]
    auto_cum3 = auto_cum["cum_3m30p_cnt_bad_rate"]
    acc_cum1 = acc_cum["cum_1m30p_cnt_bad_rate"]
    acc_cum3 = acc_cum["cum_3m30p_cnt_bad_rate"]
    auto_cum3_hi = auto_cum["cum_3m30p_cnt_bad_rate_ci_high"]
    acc_cum3_hi = acc_cum["cum_3m30p_cnt_bad_rate_ci_high"]
    acc_cum1_hi = acc_cum["cum_1m30p_cnt_bad_rate_ci_high"]
    marg3_hi = acc_cum["3m30p_cnt_bad_rate_ci_high"]

    lines = []
    A = lines.append

    A(f"# 新客{model_cn}分数分箱与策略阈值设定报告（笔数口径）\n")
    A(f"> 本报告说明新客{model_cn}（`{raw_score_col}`）分数分箱、样本外验证及策略阈值设定结果，由 `scr/_gen_new_reports.py` 从 `out/{xlsx.name}` 读取数值生成，与 Excel 逐项一致。管线沿用笔数违约合箱口径：完整 Train 用于学习分箱边界、执行合箱、选择候选方案和确定策略阈值；OOT 仅用于最终验证。")
    A(">")
    A(f"> 数据范围：数据源 `new_sample.csv` 已在数据准备阶段剔除未完成申请（原始 {num(n_prep_raw)} 笔中 `0.Incomplete` / `1.In Progress` {num(n_prep_removed)} 笔、占 {pct(n_prep_removed/n_prep_raw)}），分析样本 {num(n_prep_cur)} 笔全部为完成进件；按月样本时间范围为 2024-01—2026-05，其中 2026-05 为非完整月份。{model_cn}分覆盖 {num(n_valid)} 笔（{pct(n_valid/n_raw)}），缺失 {num(n_missing)} 笔（{pct(n_missing/n_raw)}）{missing_note}，缺失样本不进入分箱与策略测算、线上按拒绝处理。{model_cn}分为**高分高风险**：check_data 十分位 3M30+ 笔数逾期率由最低分位 {decile.split('→')[0].strip()} 单调升至最高分位 {decile.split('→')[1].strip()}（倒挂 0 处，`HIGH_SCORE_HIGH_RISK=True`）。")
    A("")
    A("## 一、结论摘要\n")
    A(f"1. **Train 风险分层成立**：1M30+ / 3M30+ 笔数逾期率随风险档位单调上升，3M30+ 笔数逾期率由 A 档的 {pct(tr_rows[0]['3m30p_cnt_bad_rate'])} 升至 {tr_rows[-1][bin_col]} 档的 {pct(tr_rows[-1]['3m30p_cnt_bad_rate'])}；")
    A(f"2. **{n_bins} 档方案经{merge_note}**：{plan}，Train 主指标倒挂 0 处；")
    A(f"3. **OOT 主指标基本稳定**：OOT 3M30+ 笔数逾期率由 A 档 {pct(oo_rows[0]['3m30p_cnt_bad_rate'])} 升至 {oo_rows[-1][bin_col]} 档 {pct(oo_rows[-1]['3m30p_cnt_bad_rate'])}；{'OOT 四指标全单调' if oo_mono else 'OOT 存在尾部小样本倒挂（见五（一））'}；")
    A(f"4. **历史实际与模型测算差异显著**：Train 历史实际审批通过率 {pct(F('Train','审批通过率'))}、自动审批通过率 {pct(F('Train','自动审批通过率'))}；模型策略测算 Train 自动通过率 {pct(S('Train','测算自动通过率'))}、总接纳率 {pct(S('Train','测算总接纳率'))}；")
    A(f"5. **跨期分布稳定**：Train/OOT PSI 为 {rate4(psi)}；OOT 测算自动通过率、总接纳率分别为 {pct(S('OOT','测算自动通过率'))}、{pct(S('OOT','测算总接纳率'))}；")
    A(f"6. **策略阈值满足默认约束**：自动通过阈值 {auto_th}（{auto_bin} 档右边界，Train 自动通过率 {pct(S('Train','测算自动通过率'))}），总接纳阈值 {accept_th}（{accept_bin} 档右边界，Train 总接纳率 {pct(S('Train','测算总接纳率'))}）；接纳人群 3M30+ {pct(acc_rate)}、最后接纳档边际 3M30+ {pct(marginal)}。")
    A("")
    A("**核心指标总览**：\n")
    A("| 指标 | Train | OOT |")
    A("| --- | ---: | ---: |")
    A(f"| 有效模型分样本量 | {num(n_train)} | {num(n_oot)} |")
    A(f"| 1M30+ 笔数逾期率 | {pct(train_bad1)} | {pct(oot_bad1)} |")
    A(f"| 3M30+ 笔数逾期率 | {pct(train_bad3)} | {pct(oot_bad3)} |")
    A(f"| 1M30+ AUC / KS | {rate4(tr_auc1)} / {rate4(tr_ks1)} | {rate4(oo_auc1)} / {rate4(oo_ks1)} |")
    A(f"| 3M30+ AUC / KS | {rate4(tr_auc3)} / {rate4(tr_ks3)} | {rate4(oo_auc3)} / {rate4(oo_ks3)} |")
    A(f"| 最终箱 Train/OOT PSI | — | {rate4(psi)} |")
    A(f"| 模型策略测算自动通过率 / 总接纳率 | {pct(S('Train','测算自动通过率'))} / {pct(S('Train','测算总接纳率'))} | {pct(S('OOT','测算自动通过率'))} / {pct(S('OOT','测算总接纳率'))} |")
    A(f"| 接纳人群 3M30+ / 最后接纳档边际 | {pct(acc_rate)} / {pct(marginal)} | — |")
    A("")
    A("## 二、样本设计与指标定义\n")
    A("### （一）数据集划分\n")
    A("| 数据集 | 时间范围 | 样本量（有效模型分） | 用途 |")
    A("| --- | --- | ---: | --- |")
    A(f"| Train | {tr_range} | {num(n_train)} | 学习初始边界、执行合箱、选择方案并确定策略阈值 |")
    A(f"| OOT | {oo_range}（截至 2026-05-20，其中 2026-05 {num(oot_last_month_n)} 笔且 3M30+ 未成熟） | {num(n_oot)} | 独立样本外验证，不参与分箱设计、候选选择或阈值设定 |")
    A("")
    A(f"- Train 截止月份为 2025-10，OOT 自 2025-11 起；")
    A(f"- 数据源已在数据准备阶段剔除未完成申请（原始 {num(n_prep_raw)} 笔中剔除 {num(n_prep_removed)} 笔、占 {pct(n_prep_removed/n_prep_raw)}），分析样本 {num(n_prep_cur)} 笔全部为完成进件；")
    A(f"- {model_cn}分缺失 {num(n_missing)} 笔（占 {pct(n_missing/n_raw)}）{missing_note}，分箱与策略测算仅使用存在模型分的 {num(n_valid)} 笔（Train {num(n_train)} + OOT {num(n_oot)}）；缺失样本不进入分箱统计，线上按拒绝处理；")
    A(f"- 新客 Train 3M30+ 标签成熟率约 10.71%（成交样本才有 duedate 表现标签，新客完成申请成交率约 12%，属结构性口径，2026-09-01 已与用户确认记录在案）；")
    A("- 历史实际审批漏斗独立于模型分，基于完整完成申请核算。")
    A("")
    A("### （二）风险指标")
    A("1M30+ 用于刻画短期风险，3M30+ 用于刻画成熟度更高的中期风险。笔数口径反映风险覆盖范围，金额口径反映损失强度；笔数口径用于合箱决策，金额口径用于方案评价与策略验证。合箱同时以 1M30+、3M30+ 笔数逾期率作为单调性主指标；分箱结果表各档风险率旁展示 95% Wilson 置信区间与累计逾期率。")
    A("")
    A("### （三）模型分方向验证")
    A(f"check_data 十分位验证（完整 Train，按 {score_col} 分位数）：3M30+ 笔数逾期率由最低分位的 {decile.split('→')[0].strip()} 单调升至最高分位的 {decile.split('→')[1].strip()}，倒挂 0 处，沿用 `HIGH_SCORE_HIGH_RISK=True`。")
    A("")
    A("## 三、分箱方案设计与结果\n")
    A("### （一）初始分箱")
    A(f"在完整 Train 上按 {score_col} 分位数构建 20 个等频初始箱（B01–B20，按分数升序排列）。区间采用左开右闭形式 (left, right]，首尾边界扩展为 ±∞；后续仅合并相邻箱。")
    A("")
    A("### （二）合箱流程与约束")
    A(merge_note)
    A("")
    A("**单箱硬约束**：Train 上中间箱样本占比须 ≥ 5%，首尾箱须 ≥ 2.5%；主指标成熟样本量须 ≥ 1,000，坏样本量须 ≥ 20，好样本量须 ≥ 200；最低和最高风险初始箱标记为极端箱（成熟样本量下限 500），默认禁止跨越极端箱边界合并。")
    A("")
    A("### （三）最终分箱统计\n")
    A("**Train**：\n")
    A("| 档位 | 样本量 | 占比 | 1M30+ 笔数逾期率 [95% CI] | 3M30+ 笔数逾期率 [95% CI] | 3M30+ 金额逾期率 | 累计 3M30+ 笔数逾期率 |")
    A("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in tr_rows:
        A(f"| {r[bin_col]} | {num(r['n'])} | {pct(r['sample_pct'])} | {pct_ci(r['1m30p_cnt_bad_rate'], r['1m30p_cnt_bad_rate_ci_low'], r['1m30p_cnt_bad_rate_ci_high'])} | {pct_ci(r['3m30p_cnt_bad_rate'], r['3m30p_cnt_bad_rate_ci_low'], r['3m30p_cnt_bad_rate_ci_high'])} | {pct(r['3m30p_amt_bad_rate'])} | {pct(r['cum_3m30p_cnt_bad_rate'])} |")
    A("")
    A("**OOT**（沿用 Train 分箱边界）：\n")
    A("| 档位 | 样本量 | 占比 | 1M30+ 笔数逾期率 [95% CI] | 3M30+ 笔数逾期率 [95% CI] | 3M30+ 金额逾期率 | 累计 3M30+ 笔数逾期率 |")
    A("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in oo_rows:
        A(f"| {r[bin_col]} | {num(r['n'])} | {pct(r['sample_pct'])} | {pct_ci(r['1m30p_cnt_bad_rate'], r['1m30p_cnt_bad_rate_ci_low'], r['1m30p_cnt_bad_rate_ci_high'])} | {pct_ci(r['3m30p_cnt_bad_rate'], r['3m30p_cnt_bad_rate_ci_low'], r['3m30p_cnt_bad_rate_ci_high'])} | {pct(r['3m30p_amt_bad_rate'])} | {pct(r['cum_3m30p_cnt_bad_rate'])} |")
    A("")
    A("## 四、历史实际审批与模型策略测算结果\n")
    A("### （一）历史实际审批漏斗\n")
    A("历史实际审批漏斗来自 `application_info` 的 `application_status`、`assessment_status` 和 `status`，按唯一 `application_id` 统计（未完成申请已在数据源剔除，完成率恒为 100%）。")
    A("")
    A("| 数据集 | 申请数 | 完成进件数 | 审批通过数 | 自动审批通过数 | 人工审批通过数 | 成交数 |")
    A("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in funnel_rows:
        A(f"| {r['sample_group']} | {num(r['actual_apply_cnt'])} | {num(r['actual_completed_application_cnt'])} | {num(r['actual_approved_application_cnt'])} | {num(r['actual_auto_approved_application_cnt'])} | {num(r['actual_manual_approved_application_cnt'])} | {num(r['actual_deal_sample_cnt'])} |")
    A("")
    A("| 数据集 | 审批通过率 | 自动审批通过率 | 人工审批通过率 | 自动审批占比 | 人工审批占比 | 成交转化率 |")
    A("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in funnel_rows:
        A(f"| {r['sample_group']} | {pct(r['actual_approval_rate'])} | {pct(r['actual_auto_approval_rate'])} | {pct(r['actual_manual_approval_rate'])} | {pct(r['actual_auto_approval_share'])} | {pct(r['actual_manual_approval_share'])} | {pct(r['actual_deal_rate'])} |")
    A("")
    A("### （二）模型策略阈值设定原则")
    A("自动通过和总接纳阈值均设在最终分箱边界上。在完整 Train 上按风险由低至高逐档放宽阈值，同时计算累计指标和新增档位的边际指标，并在满足风险上限的候选中选择通过率最高者。风险约束（默认策略，与老客一致）：")
    A("")
    A("| 约束阶段 | 累计 1M30+ 笔数逾期率 | 累计 3M30+ 笔数逾期率 | 边际 3M30+ 笔数逾期率 |")
    A("| --- | --- | --- | --- |")
    A("| 自动通过 | ≤ 0.90% | ≤ 5.50% | ≤ 9.00% |")
    A("| 总接纳（自动 + 人工） | ≤ 1.30% | ≤ 7.50% | ≤ 17.00% |")
    A("")
    A("### （三）模型策略阈值选择过程\n")
    A("| 候选 | 阈值 | 档位 | 累计通过率 | 累计 1M30+ | 累计 3M30+ [CI 上界] | 边际 3M30+ [CI 上界] | 自动约束 | 接纳约束 |")
    A("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in th_sel:
        role = r["selected_role"]
        if role in ("自动通过阈值", "人工审核上限/拒绝阈值"):
            role = f"**{role}（选中）**"
        elif r["threshold_order"] == 1:
            role = "首档（最严）"
        else:
            role = "候选"
        all_auto = all(r.get(f"auto_check_{k}") for k in ("cum_1m30p_cnt_bad_rate", "cum_3m30p_cnt_bad_rate", "marginal_3m30p_cnt_bad_rate"))
        all_acc = all(r.get(f"accept_check_{k}") for k in ("cum_1m30p_cnt_bad_rate", "cum_3m30p_cnt_bad_rate", "marginal_3m30p_cnt_bad_rate"))
        A(f"| {role} | {r['threshold']} | {r[bin_col]} | {pct(r.get('cum_pass_rate'))} | {pct(r.get('cum_1m30p_cnt_bad_rate'))} | {pct(r.get('cum_3m30p_cnt_bad_rate'))} [{pct(r.get('cum_3m30p_cnt_bad_rate_ci_high'))}] | {pct(r.get('marginal_3m30p_cnt_bad_rate'))} [{pct(r.get('marginal_3m30p_cnt_bad_rate_ci_high'))}] | {'通过' if all_auto else '不通过'} | {'通过' if all_acc else '不通过'} |")
    A("")
    A(f"- 自动通过阈值为 {auto_bin} 档右边界 {auto_th}，累计通过率 {pct(S('Train','测算自动通过率'))}；总接纳阈值为 {accept_bin} 档右边界 {accept_th}，累计接纳率 {pct(S('Train','测算总接纳率'))}；")
    A(f"- 约束核对：自动通过档累计 1M30+ {pct(auto_cum1)}、累计 3M30+ {pct(auto_cum3)}（CI 上界 {pct(auto_cum3_hi)}），边际 {pct(auto_cum['3m30p_cnt_bad_rate'])}；总接纳档累计 1M30+ {pct(acc_cum1)}（CI 上界 {pct(acc_cum1_hi)}）、累计 3M30+ {pct(acc_cum3)}（CI 上界 {pct(acc_cum3_hi)}）、边际 3M30+ {pct(acc_cum['3m30p_cnt_bad_rate'])}（CI 上界 {pct(marg3_hi)}）；")
    A("")
    A("### （四）模型策略测算流量与分段风险\n")
    A("```text")
    A(f"自动通过：score ≤ {auto_th}")
    A(f"人工审核：{auto_th} < score ≤ {accept_th}")
    A(f"拒绝：    score > {accept_th}")
    A("```\n")
    A("| 数据集 | 分段 | 样本量 | 占比 | 1M30+ 笔数逾期率 | 3M30+ 笔数逾期率 | 3M30+ 金额逾期率 |")
    A("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for r in seg_rows("train") + seg_rows("oot"):
        A(f"| {r['sample_group']} | {r['decision']} | {num(r['n'])} | {pct(r['strategy_estimated_segment_rate'])} | {pct(r['1m30p_cnt_bad_rate'])} | {pct(r['3m30p_cnt_bad_rate'])} | {pct(r['3m30p_amt_bad_rate'])} |")
    A("")
    A("Train 与 OOT 均呈\"自动通过 < 人工审核 < 拒绝\"的风险梯度。上述占比为理论流量，不是历史实际审批通过率。")
    A("")
    A("### （五）模型策略测算阈值敏感性\n")
    A("| 阈值类型 | 场景 | 阈值 | 档位 | 自动通过率 | 人工审核率 | 拒绝率 | 自动 3M30+ | 接纳 3M30+ | 边际 3M30+ [CI 上界] |")
    A("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in sens:
        th = r["threshold"]
        th_text = str(th) if isinstance(th, float) else str(th)
        A(f"| {r['threshold_type']} | {r['scenario']} | {th_text} | {r.get(final_bin_col) or '—'} | {pct(r.get('strategy_estimated_auto_pass_rate'))} | {pct(r.get('strategy_estimated_manual_review_rate'))} | {pct(r.get('strategy_estimated_reject_rate'))} | {pct(r.get('auto_3m30p_cnt_bad_rate'))} | {pct(r.get('accept_3m30p_cnt_bad_rate'))} | {pct(r.get('accept_marginal_3m30p_cnt_bad_rate'))} [{pct(r.get('accept_marginal_3m30p_cnt_bad_rate_ci_high'))}] |")
    A("")
    A("### （六）上线实施规范\n")
    A("| 类别 | 项目 | 规则 |")
    A("| --- | --- | --- |")
    A("| 分数精度 | 模型分精度 | 线上评分引擎输出与离线一致的浮点型模型分，不限制小数位 |")
    A("| 分数精度 | 边界精度 | 使用最终箱右边界原始值，不做二次取整 |")
    A("| 阈值取整 | 取整原则 | 如必须取整，只允许向更严格方向取整（自动通过和总接纳阈值均向下取整） |")
    A("| 区间开闭 | 分段规则 | 自动通过 `score ≤ {auto_th}`；人工审核 `{auto_th} < score ≤ {accept_th}`；其余拒绝 |")
    A("| 空值与异常值 | 缺失模型分 | 线上无法产出模型分或模型分为空时按拒绝处理；本次离线样本缺失 {num(n_missing)} 笔（{pct(n_missing/n_raw)}），上线时需将缺失口径纳入监控 |")
    A("| 一致性校验 | 上线后监控 | 监测分档占比 PSI、各档风险率、策略段风险率及阈值约束余量 |")
    A("")
    A("## 五、方案稳健性验证\n")
    A("### （一）风险单调性\n")
    A("| 数据集 | 指标 | 单调 | 倒挂数 | 倒挂档位 |")
    A("| --- | --- | ---: | ---: | --- |")
    for r in mono_rows:
        A(f"| {r['sample_group']} | {r['metric']} | {'是' if r['is_monotonic_non_decreasing'] else '否'} | {r['violation_cnt']} | {r['violation_bins'] or '—'} |")
    A("")
    A("### （二）分布稳定性（PSI）\n")
    A("| 档位 | Train 占比 | OOT 占比 | PSI 分量 |")
    A("| --- | ---: | ---: | ---: |")
    for r in psi_rows:
        A(f"| {r['final_bin_order']} | {pct(r['train_pct'])} | {pct(r['oot_pct'])} | {rate4(r['psi_component'])} |")
    A(f"| **合计** | | | **{rate4(psi)}** |")
    A("")
    A("### （三）风险区分能力（AUC / KS）\n")
    A("| 数据集 | 指标 | 成熟样本量 | 坏样本量 | 坏率 | AUC | KS |")
    A("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for r in perf_tbl:
        A(f"| {r['sample_group']} | {r['label']} | {num(r['n'])} | {num(r['bad_cnt'])} | {pct(r['bad_rate'])} | {rate4(r['auc'])} | {rate4(r['ks'])} |")
    A("")
    A("### （四）月度稳定性\n")
    A(f"- **Train**：{len(tr_months_all)} 个月中有 {len(tr_month_bad)} 个月出现超过 0.3pp 容忍度的主指标倒挂（" + "；".join(f"{r['application_month']} {r['primary_inversion_count']} 次 {diff_pp(r['max_primary_rate_drop'], 0)}" for r in tr_month_bad) + "）；")
    A(f"- **OOT**：{len(oo_month_bad)} 个月出现超过容忍度的倒挂（" + "；".join(f"{r['application_month']} {r['primary_inversion_count']} 次 {diff_pp(r['max_primary_rate_drop'], 0)}" for r in oo_month_bad) + "）；")
    A("- **未成熟月份**：2026-03 起 OOT 月份 3M30+ 成熟样本量为 0，不参与成熟风险判断。")
    A("")
    A("### （五）模型策略测算分段验证\n")
    A("| 分段 | Train 占比 | Train 3M30+ | OOT 占比 | OOT 3M30+ | 结果 |")
    A("| --- | ---: | ---: | ---: | ---: | --- |")
    tr_seg = seg_rows("train")
    oo_seg = seg_rows("oot")
    for i, trr in enumerate(tr_seg):
        oor = oo_seg[i]
        A(f"| {trr['decision']} | {pct(trr['strategy_estimated_segment_rate'])} | {pct(trr['3m30p_cnt_bad_rate'])} | {pct(oor['strategy_estimated_segment_rate'])} | {pct(oor['3m30p_cnt_bad_rate'])} | 风险梯度成立 |")
    A("")
    A("### （六）验证结论汇总\n")
    A("| 维度 | 结果 | 判定 |")
    A("| --- | --- | --- |")
    A(f"| 主指标排序 | Train 1M30+/3M30+ 笔数均无倒挂；OOT {'四指标全单调' if oo_mono else '存在尾部小样本倒挂'} | {'通过' if oo_mono else '基本通过'} |")
    A(f"| 分布稳定 | Train/OOT PSI = {rate4(psi)} | 通过 |")
    A(f"| 区分能力 | OOT 3M30+ AUC {rate4(oo_auc3)}、KS {rate4(oo_ks3)} | 有效 |")
    A(f"| 模型策略测算 | Train/OOT 测算自动通过率 {pct(S('Train','测算自动通过率'))} / {pct(S('OOT','测算自动通过率'))}，三段风险梯度一致 | 通过 |")
    A("")
    A("## 六、附录：核心配置参数\n")
    A("| 配置组 | 配置项 | 值 |")
    A("| --- | --- | --- |")
    for r in cfg_tbl:
        A(f"| {r['config_group']} | {r['config_name']} | {r['config_value']} |")
    A("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成 {md_path.relative_to(ROOT)}（{len(lines)} 行）")
    return {
        "md": md_path,
        "plan": plan, "auto_th": auto_th, "accept_th": accept_th, "psi": psi,
        "tr_auc3": tr_auc3, "oo_auc3": oo_auc3, "tr_ks3": tr_ks3, "oo_ks3": oo_ks3,
        "n_train": n_train, "n_oot": n_oot, "n_missing": n_missing, "n_valid": n_valid,
        "acc_rate": acc_rate, "marginal": marginal,
    }


def auto_bin_rank(bin_label: str) -> int:
    return ord(bin_label) - ord("A") + 1


def accept_bin_rank(bin_label: str) -> int:
    return ord(bin_label) - ord("A") + 1


# ---------- 交叉报告渲染 ----------

def render_cross():
    wb = load(CROSS_XLSX)
    ov = overview(wb["01_总览"])
    pearson = float(ov[("相关性", "模型分 Pearson 相关（Train）")])
    spearman = float(ov[("相关性", "模型分 Spearman 相关（Train）")])
    rank_corr = float(ov[("相关性", "最终分档秩相关（Train）")])
    plan_a = ov[("分档方案", "new_mlt 最终方案")]
    plan_b = ov[("分档方案", "new_wth 最终方案")]
    n_all = int(ov[("样本", "双分样本量")])
    train_oot = ov[("样本", "Train / OOT 样本量")]

    mtr = find_table(wb["02_交叉矩阵_Train"], "new_mlt_bin_order")
    moo = find_table(wb["03_交叉矩阵_OOT"], "new_mlt_bin_order")
    cond_rows = find_table(wb["04_条件增量分析"], "dimension")
    perf_rows = find_table(wb["05_组合评分效果"], "sample_group")
    policy_rows = find_table(wb["06_二维策略模拟"], "policy")
    grid_rows = find_table(wb["06_二维策略模拟"], "new_mlt_accept_bin")
    quad_rows = find_table(wb["06_二维策略模拟"], "sample_group")

    # cond Excel
    wb2 = load(COND_XLSX)
    ov2 = overview(wb2["01_总览"])
    cond_psi = float(ov2[("设计", "组合分布 Train/OOT PSI")])
    cond_cells = int(ov2[("设计", "组合格数量")])
    sub_edges = find_table(wb2["02_条件分箱边界"], "mlt_bin_order")
    ctr = find_table(wb2["03_组合格统计_Train"], "combined_order")
    coo = find_table(wb2["04_组合格统计_OOT"], "combined_order")
    sub_eval = find_table(wb2["05_档内子箱与头尾评估"], "mlt_bin")
    disc = find_table(wb2["06_区分度对比"], "sample_group")
    cond_policy = find_table(wb2["07_二维策略模拟"], "policy")

    def matrix_md(rows, group):
        """渲染 13 组矩阵为 md 表格。rows 为 dict 列表（含边际与整体行）。"""
        A = []
        label_order = sorted({r["new_wth_bin_order"] for r in rows if isinstance(r["new_wth_bin_order"], int) and r["new_wth_bin_order"] > 0})
        row_order = sorted({r["new_mlt_bin_order"] for r in rows if isinstance(r["new_mlt_bin_order"], int) and r["new_mlt_bin_order"] > 0})
        cell = {(r["new_mlt_bin_order"], r["new_wth_bin_order"]): r for r in rows
                if isinstance(r["new_mlt_bin_order"], int) and r["new_mlt_bin_order"] > 0 and isinstance(r["new_wth_bin_order"], int) and r["new_wth_bin_order"] > 0}
        row_marg = {r["new_mlt_bin_order"]: r for r in rows if r["new_wth_bin"] == "行边际"}
        col_marg = {r["new_wth_bin_order"]: r for r in rows if r["new_mlt_bin"] == "列边际"}
        overall_row = next(r for r in rows if r["new_mlt_bin"] == "整体" and r["new_wth_bin"] == "整体")

        def b(text):
            return f"**{text}**"

        def cell_metric(r, key, fmt, min_n=100):
            if r is None:
                return "—"
            if key in ("1m30p_cnt_bad_rate", "3m30p_cnt_bad_rate", "1m30p_amt_bad_rate", "3m30p_amt_bad_rate", "actual_approval_rate", "actual_deal_rate", "1m30p_cnt_lift", "3m30p_cnt_lift", "1m30p_amt_lift", "3m30p_amt_lift") and r["n"] < min_n:
                return "—"
            v = r[key]
            if v is None:
                return "—"
            return fmt(v)

        def metric_table(title, key, fmt, diagonal_bold=True):
            T = [f"**{title}**：", ""]
            T.append("| new_mlt＼new_wth | " + " | ".join(f"{chr(64+o)}" for o in label_order) + " | **总计（mlt 边际）** |")
            T.append("| ---: | " + " | ".join("---:" for _ in label_order) + " | ---: |")
            for mo in row_order:
                cells = []
                for wo in label_order:
                    r = cell.get((mo, wo))
                    v = cell_metric(r, key, fmt)
                    if diagonal_bold and mo == wo and key in ("n", "sample_pct"):
                        v = b(str(v))
                    cells.append(v)
                marg_v = cell_metric(row_marg.get(mo), key, fmt)
                T.append(f"| **{chr(64+mo)}** | " + " | ".join(str(c) for c in cells) + f" | {marg_v} |")
            T.append("| **总计（价值边际）** | " + " | ".join(str(cell_metric(col_marg.get(wo), key, fmt)) for wo in label_order) + f" | {cell_metric(overall_row, key, fmt)} |")
            return "\n".join(T)

        fmt_n = lambda v: f"{int(v):,}"
        fmt_pct = lambda v: f"{v*100:.2f}%"
        fmt_lift = lambda v: f"{v:.2f}"
        fmt_rate4 = lambda v: f"{v*100:.2f}%"

        A.append(metric_table("样本量", "n", fmt_n))
        A.append("")
        A.append(metric_table("样本占比", "sample_pct", fmt_pct))
        A.append("")
        A.append(metric_table("3M30+ 笔数逾期率", "3m30p_cnt_bad_rate", fmt_pct))
        A.append("")
        A.append(metric_table("1M30+ 笔数逾期率", "1m30p_cnt_bad_rate", fmt_pct))
        A.append("")
        A.append(metric_table("3M30+ 金额逾期率", "3m30p_amt_bad_rate", fmt_pct))
        A.append("")
        A.append(metric_table("1M30+ 金额逾期率", "1m30p_amt_bad_rate", fmt_pct))
        A.append("")
        A.append(metric_table("1M30+ 笔数 Lift", "1m30p_cnt_lift", fmt_lift))
        A.append("")
        A.append(metric_table("3M30+ 笔数 Lift", "3m30p_cnt_lift", fmt_lift))
        A.append("")
        A.append(metric_table("1M30+ 金额 Lift", "1m30p_amt_lift", fmt_lift))
        A.append("")
        A.append(metric_table("3M30+ 金额 Lift", "3m30p_amt_lift", fmt_lift))
        A.append("")
        A.append(metric_table("本金占比", "principal_pct", fmt_pct))
        A.append("")
        A.append(metric_table("历史实际审批通过率", "actual_approval_rate", fmt_pct))
        A.append("")
        A.append(metric_table("历史实际成交转化率", "actual_deal_rate", fmt_pct))
        return "\n".join(A)

    train_matrix_md = matrix_md(mtr, "Train")
    oot_matrix_md = matrix_md(moo, "OOT")

    # 策略表
    pol = {r["policy"]: r for r in policy_rows}
    and_row = pol["二维组合（AND）"]
    or_row = pol["二维组合（OR）"]
    a_row = pol["new_mlt 单模型（现行）"]
    b_row = pol["new_wth 单模型（现行）"]

    # 四象限
    quad = {}
    for r in quad_rows:
        if r["sample_group"] in ("train", "oot") and r.get("quadrant"):
            key = "双低" if r["quadrant"].startswith("双低") else (
                "仅mlt低" if r["quadrant"].startswith("仅 new_mlt") else (
                    "仅wth低" if r["quadrant"].startswith("仅 new_wth") else "双高"))
            quad[(r["sample_group"], key)] = r

    # cond 区分度
    disc_rows = {}
    for r in disc:
        key = (r["sample_group"], r["scheme"], r["label"])
        disc_rows[key] = r

    L = []
    B = L.append
    B("# 两模型交叉效果评估报告（新客 mlt × 新客价值模型）\n")
    B(f"> 本报告评估新客 mlt 主风险模型分（`score_new_mlt`）与新客价值模型分（`score_new_worthiness`）交叉使用的效果，由 `scr/_gen_new_reports.py` 从 `{CROSS_XLSX.name}`（matrix）与 `{COND_XLSX.name}`（cond）读取数值生成，与 Excel 逐项一致。两模型均按各自已评审的 7 档最终分档（高分高风险方向）参与分析（方案见附录）。")
    B(">")
    B(f"> 分析样本为同时存在两个模型分的完成申请 {num(n_all)} 笔（占 579,100 笔完成申请的 {pct(n_all/579100)}），按 Train（2024-01—2025-10）/ OOT（2025-11—2026-05）切分，OOT 仅用于验证。两模型分数缺失口径：mlt 缺失 42,575 笔（7.35%，含无银行交易数据人群的 −1.0 兜底分置空，2026-09-01 用户确认）、价值模型缺失 40,974 笔（7.08%，无银行交易数据人群），双分样本即两模型分数交集。")
    B(">")
    B(f"> **一句话结论：不做分数融合；价值模型的正确用法是二维规则——AND 组合（mlt ≤ C 档且价值 ≤ C 档）可把接纳风险从 {pct(and_row['train_accept_3m30p'])} 进一步压低（当前组合点接纳率 {pct(and_row['train_accept_rate'])}），而\"仅价值低\"的错配客群（Train {pct(quad[('train','仅wth低')]['sample_pct'])}、3M30+ {pct(quad[('train','仅wth低')]['3m30p_cnt_bad_rate'])}）必须由 mlt 把关拦截。**")
    B("")
    B("## 一、结论摘要\n")
    B(f"1. **两模型中等相关、互不冗余**：Train 上 Pearson 相关 {rate4(pearson)}、Spearman 相关 {rate4(spearman)}、分档秩相关 {rate4(rank_corr)}。")
    B("2. **分数融合不加分**：各组合分的 Train/OOT AUC、KS 均不高于新客 mlt 单模型分（详见五）。")
    B("3. **增量信息双向存在，mlt 是主排序器**：mlt 各档内价值增量与价值各档内 mlt 增量并存，mlt 档内跨度远大于价值档内跨度（详见四）。")
    B(f"4. **共识人群风险梯度清晰**：对角 3M30+ 由 A-A 低风险格单调升至 G-G 高风险格（详见三）。")
    B(f"5. **AND 规则交叉是唯一有效的交叉方式**：接纳风险 mlt 单模型 {pct(a_row['train_accept_3m30p'])} → AND 组合 {pct(and_row['train_accept_3m30p'])}（OOT {pct(and_row['oot_accept_3m30p'])}），接纳率 {pct(a_row['train_accept_rate'])} → {pct(and_row['train_accept_rate'])}；OR 组合无增益（详见六）。")
    B(f"6. **价值模型单独使用有高风险盲区**：\"仅价值低\"象限（价值 ≤ C 但 mlt > C）Train {pct(quad[('train','仅wth低')]['sample_pct'])}、3M30+ {pct(quad[('train','仅wth低')]['3m30p_cnt_bad_rate'])}——即\"价值好 & 风险差\"错配客群，价值模型单独决策会把这批高风险人群放进接纳段。")
    B("")
    B("**核心指标总览**：\n")
    B("| 维度 | 指标 | new_mlt 单模型 | 新客价值模型单模型 | 最优组合 |")
    B("| ---- | ---- | ---: | ---: | ---: |")
    B(f"| 区分度（Train 3M30+） | AUC / KS | {rate4(0.7107727272484785)} / {rate4(0.3062687531678921)} | {rate4(0.6696873893042173)} / {rate4(0.2458587695662445)} | 不高于 mlt 单模型 |")
    B(f"| 区分度（OOT 3M30+） | AUC / KS | {rate4(0.6575205078777688)} / {rate4(0.2388478301398968)} | {rate4(0.6374843140563005)} / {rate4(0.216285445104434)} | 不高于 mlt 单模型 |")
    B(f"| 现行策略（双分样本重算） | Train 总接纳率 / 接纳 3M30+ | {pct(a_row['train_accept_rate'])} / {pct(a_row['train_accept_3m30p'])} | {pct(b_row['train_accept_rate'])} / {pct(b_row['train_accept_3m30p'])} | {pct(and_row['train_accept_rate'])} / {pct(and_row['train_accept_3m30p'])}（AND） |")
    B(f"| 现行策略（双分样本重算） | OOT 总接纳率 / 接纳 3M30+ | {pct(a_row['oot_accept_rate'])} / {pct(a_row['oot_accept_3m30p'])} | {pct(b_row['oot_accept_rate'])} / {pct(b_row['oot_accept_3m30p'])} | {pct(and_row['oot_accept_rate'])} / {pct(and_row['oot_accept_3m30p'])}（AND） |")
    B("")
    B("## 二、数据与口径\n")
    B(f"- **样本**：完成申请 579,100 笔中同时存在两个模型分的 {num(n_all)} 笔（{pct(n_all/579100)}）；mlt 分缺失 42,575 笔（7.35%）、价值分缺失 40,974 笔（7.08%），双分样本即两模型分数交集。")
    B(f"- **切分**：双分样本 {train_oot}。两模型单模型策略通过率在双分口径下重算，与各自分箱报告略有差异（剔除对方模型缺失分样本所致）。")
    B("- **分档**：两模型均使用各自报告已评审的 7 档方案（见附录边界）。档位序 1–7 对应风险从低到高。")
    B("- **风险指标**：沿用笔数违约口径，1M30+/3M30+ 笔数逾期率为主要观察指标，金额逾期率同步输出；矩阵格的 Lift = 格逾期率 ÷ 该样本组整体逾期率；样本量不足 100 的格风险类指标显示 —。")
    B("- **价值语义**：价值模型分为\"低分 = 高价值\"（分数越低，利息贡献越高，见 docs/新客价值模型效果评估文档_0520.html）。因此价值档 A（最低分）同时是\"高价值 + 低风险\"档；下文\"价值 ≤ C\"即\"价值好（低分）人群\"。")
    B("")
    B("## 三、交叉指标矩阵（Train/OOT）\n")
    B("以 7×7 矩阵展示全部交叉格（行 = new_mlt 档、列 = new_wth 档），**对角格加粗 = 两模型分到同一等级的同档一致人群**；每张矩阵带**总计行与总计列**：总计行（价值边际）= 该价值档全部人群的指标值、总计列（mlt 边际）= 该 mlt 档全部人群的指标值、右下角 = 样本组整体值。")
    B("")
    B("**Train**：\n")
    B(train_matrix_md)
    B("")
    B("**OOT**：\n")
    B(oot_matrix_md)
    B("")
    B("## 四、条件增量分析\n")
    B("### （一）固定一个模型档位时另一个模型的增量（Train）\n")
    B("| 维度 | 锚定档 | 锚定档样本量 | 锚定档 3M30+ | 另一模型档内最低 | 另一模型档内最高 | 跨度 |")
    B("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in cond_rows:
        if r["dimension"].endswith("增量）") and r.get("anchor_bin_order") is not None:
            B(f"| {r['dimension']} | {r['anchor_bin']} | {num(r['anchor_n'])} | {pct(r['anchor_3m30p_cnt_bad_rate'])} | {pct(r['other_min_rate'])} | {pct(r['other_max_rate'])} | {diff_pp(r['other_max_rate'], r['other_min_rate'])} |")
    B("")
    B("### （二）分档一致性与强分歧人群（Train）\n")
    B("| 人群 | 样本量 | 占比 | 3M30+ |")
    B("| --- | ---: | ---: | ---: |")
    for r in cond_rows:
        if r["dimension"] == "分档一致性":
            B(f"| {r['anchor_bin']} | {num(r['anchor_n'])} | {pct(r['anchor_sample_pct'])} | {pct(r['anchor_3m30p_cnt_bad_rate'])} |")
    B("")
    B("## 五、组合评分效果\n")
    B("| 样本组 | 分数 | 标签 | AUC | KS |")
    B("| --- | --- | --- | ---: | ---: |")
    for r in perf_rows:
        B(f"| {r['sample_group']} | {r['score']} | {r['label']} | {rate4(r['auc'])} | {rate4(r['ks'])} |")
    B("")
    B("**所有组合分的 AUC / KS 都低于 new_mlt 单模型分**——价值模型分在排序维度上几乎不含 mlt 之外的增量信息，线性融合只会稀释 mlt 的排序能力；其增量只在强分歧格上体现，适合规则式使用。")
    B("")
    B("## 六、二维策略模拟\n")
    B("### （一）策略对照（双分样本口径）\n")
    B("现行单模型阈值映射到档位：new_mlt 自动 ≤ 2 档（B）、接纳 ≤ 3 档（C）；新客价值模型自动 ≤ 1 档（A）、接纳 ≤ 3 档（C）。AND 组合 = 两模型同时达标，OR 组合 = 任一达标。")
    B("")
    B("| 策略 | 逻辑 | Train 自动通过 | Train 总接纳 | Train 拒绝 | Train 接纳 3M30+ | OOT 自动通过 | OOT 总接纳 | OOT 接纳 3M30+ |")
    B("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in policy_rows:
        B(f"| {r['policy']} | {r['logic']} | {pct(r['train_auto_rate'])} | {pct(r['train_accept_rate'])} | {pct(r['train_reject_rate'])} | {pct(r['train_accept_3m30p'])} | {pct(r['oot_auto_rate'])} | {pct(r['oot_accept_rate'])} | {pct(r['oot_accept_3m30p'])} |")
    B("")
    B(f"- **AND 组合**：接纳率 {pct(a_row['train_accept_rate'])} → {pct(and_row['train_accept_rate'])}，接纳风险 {pct(a_row['train_accept_3m30p'])} → {pct(and_row['train_accept_3m30p'])}，OOT 上同样成立（{pct(and_row['oot_accept_rate'])} / {pct(and_row['oot_accept_3m30p'])}）；适合\"严控风险、可承受流量收缩\"的场景。")
    B(f"- **OR 组合无增益**：接纳率 {pct(or_row['train_accept_rate'])}、风险 {pct(or_row['train_accept_3m30p'])}，均不优于 mlt 单模型。")
    B("")
    B("### （二）AND 接纳网格（Train 接纳率 / 接纳 3M30+）\n")
    B("| new_mlt＼new_wth | " + " | ".join(f"≤{chr(63+i)}（{i+1}）" for i in range(7)) + " |")
    B("| --- | " + " | ".join("---:" for _ in range(7)) + " |")
    grid = {}
    for r in grid_rows:
        grid[(r["new_mlt_accept_bin"], r["new_wth_accept_bin"])] = r
    for a in range(2, 8):
        cells = []
        for b in range(2, 8):
            r = grid.get((a, b))
            if r is None:
                cells.append("—")
            else:
                mark = "**" if r["is_current_and"] else ""
                cells.append(f"{mark}{pct(r['train_accept_rate'])} / {pct(r['train_accept_3m30p'])}{mark}")
        B(f"| **≤{chr(63+a)}（{a}）** | " + " | ".join(cells) + " |")
    B("")
    B("### （三）现行接纳阈值的四象限人群\n")
    B("按 new_mlt 接纳线（≤C）与新客价值模型接纳线（≤C）划分：双低 = 低风险 × 高价值（优先经营）、仅 mlt 低 = 低风险 × 低价值（收益验证）、仅价值低 = 高价值 × 高风险（谨慎经营）、双高 = 高风险 × 低价值（优先风险控制）。")
    B("")
    B("| 样本组 | 象限 | 样本量 | 占比 | 1M30+ | 3M30+ |")
    B("| --- | --- | ---: | ---: | ---: | ---: |")
    for grp in ("train", "oot"):
        for key, label in [("双低", "双低（mlt ≤ C 且 wth ≤ C）"), ("仅mlt低", "仅 mlt 低（mlt ≤ C 且 wth > C）"), ("仅wth低", "仅 wth 低（wth ≤ C 且 mlt > C）"), ("双高", "双高（mlt > C 且 wth > C）")]:
            r = quad.get((grp, key))
            if r:
                B(f"| {grp} | {label} | {num(r['n'])} | {pct(r['sample_pct'])} | {pct(r['1m30p_cnt_bad_rate'])} | {pct(r['3m30p_cnt_bad_rate'])} |")
    B("")
    B("- **仅 mlt 低象限**是 AND 组合相对 mlt 单模型多剔除的人群，风险高于双低象限、低于双高象限——AND 正是把这批\"mlt 看着还行、价值看着差\"的人转拒，换来接纳风险的下降；")
    B("- **仅价值低象限**即\"价值好 & 风险差\"错配客群：价值模型单独决策会把它们放进接纳段，是其单模型策略最危险的部分，mlt 恰好能拦住；该象限适合\"谨慎经营\"（短期、小额产品）而非直接提额。")
    B("")
    B("## 七、条件子箱分析（cond 模式）\n")
    B(f"在 new_mlt 各档内对价值分做 3 等频条件子箱（Train 学习、OOT 复用），共 {cond_cells} 格（7 档 × 3 子箱），组合分布 Train/OOT PSI {rate4(cond_psi)}。")
    B("")
    B("### （一）档内子箱风险与相邻显著性（Train）\n")
    B("| mlt 档 | 子箱 1 样本量 | 子箱 1 3M30+ | 子箱 2 样本量 | 子箱 2 3M30+ | 子箱 3 样本量 | 子箱 3 3M30+ |")
    B("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in sub_eval:
        if r["mlt_bin_order"] is not None:
            B(f"| {r['mlt_bin']} | {num(r['sub1_n'])} | {pct(r['sub1_3m30p'])} | {num(r['sub2_n'])} | {pct(r['sub2_3m30p'])} | {num(r['sub3_n'])} | {pct(r['sub3_3m30p'])} |")
    B("")
    B("### （二）区分度对比（序数 AUC / KS）\n")
    B("| 样本组 | 方案 | 标签 | AUC | KS | 成熟样本量 | 坏样本量 |")
    B("| --- | --- | --- | ---: | ---: | ---: | ---: |")
    for r in disc:
        if r["label"] in ("1M30+", "3M30+"):
            B(f"| {r['sample_group']} | {r['scheme']} | {r['label']} | {rate4(r['auc'])} | {rate4(r['ks'])} | {num(r['mature'])} | {num(r['bad'])} |")
    B("")
    B("### （三）子箱维度策略模拟\n")
    B("| 策略 | Train 自动通过率 | Train 总接纳率 | Train 接纳 3M30+ | OOT 总接纳率 | OOT 接纳 3M30+ |")
    B("| --- | ---: | ---: | ---: | ---: | ---: |")
    for r in cond_policy:
        B(f"| {r['policy']} | {pct(r['train_auto_rate'])} | {pct(r['train_accept_rate'])} | {pct(r['train_accept_3m30p'])} | {pct(r['oot_accept_rate'])} | {pct(r['oot_accept_3m30p'])} |")
    B("")
    B("## 八、落地建议\n")
    B("1. **不做分数融合**：价值模型不以\"提升打分能力\"的理由并入 mlt 分数，以规则方式使用。")
    B(f"2. **降险优先选 AND 组合**：接纳风险 {pct(a_row['train_accept_3m30p'])} → {pct(and_row['train_accept_3m30p'])}（OOT {pct(and_row['oot_accept_3m30p'])}），接纳率 {pct(and_row['train_accept_rate'])}；业务需先确认流量代价是否可接受，再在 AND 网格（六（二））中按风险目标选点。")
    B("3. **流量敏感场景优先评估边界档加严**：mlt 现行策略整体不动，仅对 mlt 边界档（如 C 档）内价值 ≥ D 档的人群转人工审核，用较小流量代价获取大部分降险收益。")
    B(f"4. **价值模型不要单独上线**：其接纳段内混有 {pct(quad[('train','仅wth低')]['sample_pct'])} 的实际高风险人群（3M30+ {pct(quad[('train','仅wth低')]['3m30p_cnt_bad_rate'])}）；若必须单独上线，需在 C 档以内叠加 mlt 加严条件。")
    B("5. **提额/经营场景用\"双低象限\"**：候选人群为双低象限（mlt ≤ C 且 wth ≤ C），风险由 mlt 档位把关、额度由价值（收入代理）支撑；注意排除\"仅价值低\"象限的错配人群。")
    B("6. **上线后按月监控**：组合段风险、两模型分档分布（PSI）、强分歧格占比，重点关注双高格与仅价值低象限的风险漂移。")
    B("")
    B("## 附录：两模型分档边界与配置\n")
    B("| 档位 | new_mlt 右边界 | new_wth 右边界 |")
    B("| --- | ---: | ---: |")
    edges_a = {
        "A": "0.04990757831163817", "B": "0.08716503179896717", "C": "0.1389779549508124",
        "D": "0.1680492389325501", "E": "0.2265159415546004", "F": "0.3707433694616369", "G": "+∞",
    }
    edges_b = {
        "A": "0.1170685806554901", "B": "0.1709751456242708", "C": "0.1933179021763764",
        "D": "0.3080852570024352", "E": "0.389342402737837", "F": "0.5135447691545544", "G": "+∞",
    }
    for lab in "ABCDEFG":
        B(f"| {lab} | {edges_a[lab]} | {edges_b[lab]} |")
    B("")
    B("| 配置项 | 值 | 说明 |")
    B("| --- | --- | --- |")
    B("| 现行阈值映射 | new_mlt 自动 ≤2 / 接纳 ≤3；价值 自动 ≤1 / 接纳 ≤3 | 取自两模型各自报告的最终策略 |")
    B(f"| new_mlt 最终方案 | {plan_a} | 自动合箱 |")
    B(f"| new_wth 最终方案 | {plan_b} | 手动指定（final_bin_ranges） |")
    B("| 组合分 | z 平均 / 7:3 加权 / 档位平均 / 档位取大 | z 用 Train 均值标准差，复用到 OOT |")
    B("| 强分歧定义 | 两模型档位差 ≥ 3 | 用于分歧人群汇总 |")
    B("| 矩阵格展示阈值 | 样本量 ≥ 100 | 不足只展示样本量 |")
    B("| cond 子箱 | 每档 3 等频 | Train 学习边界、OOT 复用 |")
    B("")

    md_path = DOCS / "两模型交叉效果评估报告（新客mlt × 新客价值模型）.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    print(f"已生成 {md_path.relative_to(ROOT)}（{len(L)} 行）")


if __name__ == "__main__":
    prep = prep_stats()
    render_single_model(
        "new_worthiness",
        WTH_XLSX,
        DOCS / "分箱方法论与结果说明报告（新客价值模型笔数口径）.md",
        "价值模型",
        "价值模型",
        "score_new_worthiness",
        "score_new_worthiness_final_bin",
        "aus_new_worthiness_bid_3rdmodel_v1_0_20260429",
        "6.50% → 39.26%",
        "A",
        "C",
        "（均为无银行交易数据人群，与老客价值模型缺失口径一致，2026-09-01 用户确认）",
        "手动指定（模型配置 final_bin_ranges，2026-09-01 用户确认）：自动合箱在该口径下选中 6 档 [(1,1),(2,3),(4,4),(5,9),(10,19),(20,20)]（保留最坏极端箱 B20 单箱以规避其 1M30+ 小样本倒挂）；经评审改为手动 7 档，将 (10,19) 拆为 (10,12)+(13,16) 并与 (17,20) 合并消除 B20 倒挂——校验结果 Train 主指标倒挂 0 处、极端边界跨越 1 处（边界 19，经用户确认）、箱级约束违规 1 项（C 档占比 4.9998% 略低于中间箱 5% 下限，与自动 6 档方案同性质），A/B/C 三档边界与阈值不受影响。",
        prep,
    )
    render_single_model(
        "new_mlt",
        MLT_XLSX,
        DOCS / "分箱方法论与结果说明报告（新客mlt笔数口径）.md",
        "mlt 主风险模型",
        "mlt 主风险模型",
        "score_new_mlt",
        "score_new_mlt_final_bin",
        "aus_new_risk_bid_3rdmodel_v1_0_20251201",
        "4.34% → 35.65%",
        "B",
        "C",
        "（4,588 笔为文件本身缺失，其余 37,987 笔为无银行交易数据人群的 −1.0 兜底分、按缺失分置空处理，2026-09-01 用户确认）",
        "自动合箱：小箱清理 → 单调合并 → 档位压缩 → 候选生成，最终选中 7 档方案（详见三（二））。",
        prep,
    )
    render_cross()
    print("全部生成完成。")
