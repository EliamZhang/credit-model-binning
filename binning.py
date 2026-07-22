# -*- coding: utf-8 -*-
"""
模型分数分箱 — 自动生成策略报告
运行方式: python binning.py
"""

from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from scipy.stats import norm
from scipy.stats import chi2_contingency
import statsmodels.api as sm

# 显示设置
pd.set_option('display.max_columns', None)
pd.set_option('display.max_rows', 100)
pd.set_option('display.width', 200)
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei']
plt.rcParams['axes.unicode_minus'] = False

DATA_DIR = Path('res')

# 时间切分配置：后续可按业务确认后调整
TRAIN_END_MONTH = '2026-03'
OOT_START_MONTH = '2026-04'

# 模型分方向：当前方法论按高分高风险处理
HIGH_SCORE_HIGH_RISK = True


# ============================================================
# 1. 加载数据
# ============================================================

def clean_columns(frame):
    """清理 UTF-8 BOM 以及少数 CSV 头部乱码。"""
    frame = frame.copy()
    frame.columns = [str(c).lstrip('\ufeff').lstrip('ï»¿') for c in frame.columns]
    return frame


def read_csv_clean(path, **kwargs):
    frame = pd.read_csv(path, low_memory=False, **kwargs)
    return clean_columns(frame)


# 主表：样本表
sample = read_csv_clean(DATA_DIR / 'sample.csv')
print('1/14 加载数据 ...')

# 申请信息表（已按 sample 对齐）
app = read_csv_clean(DATA_DIR / 'application_info.csv')

# 模型分表
mlt_score = read_csv_clean(DATA_DIR / 'aus_old_risk_bid_mltmodel_v1_2_20260325_lgb_score.csv')
apply_score = read_csv_clean(DATA_DIR / 'aus_old_risk_apply_appmodel_v20260318_v1_2_lgb_score.csv')
txn_score = read_csv_clean(DATA_DIR / 'aus_old_risk_bid_submodel_v20260323_v1_2_txn_lgb_score.csv')


# ============================================================
# 2. 拼接主表（与 scr/application_info_extract.sql 口径一致）
# ============================================================

# 以 sample 为底表，左连接各表
df = sample.merge(app, on=['application_id', 'user_id'], how='left')

# 模型分：去重后保留 score 列，重命名
# mlt_score 和 apply_score 当前各有少量重复 application_id，先取第一条
mlt_col = 'aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score'
apply_col = 'aus_old_risk_apply_appmodel_v20260318_v1_2_lgb_score'

mlt_dedup = mlt_score.drop_duplicates(subset='application_id', keep='first')
apply_dedup = apply_score.drop_duplicates(subset='application_id', keep='first')

df = df.merge(
    mlt_dedup[['application_id', mlt_col]],
    on='application_id', how='left'
).rename(columns={mlt_col: 'score_mlt'})

df = df.merge(
    apply_dedup[['application_id', apply_col]],
    on='application_id', how='left'
).rename(columns={apply_col: 'score_apply'})

# 交易特征子模型：保留 score + 交易特征
txn_feature_cols = [
    c for c in txn_score.columns
    if c not in (
        'application_id', 'user_id', 'sample_datetime',
        'feature_error', 'send_time',
        'transaction_date_max', 'balance_date_max'
    )
]
txn_dedup = txn_score.drop_duplicates(subset='application_id', keep='first')

df = df.merge(
    txn_dedup[['application_id'] + txn_feature_cols],
    on='application_id', how='left'
)

# 只保留分箱需要的字段
keep_cols = [
    'application_id', 'user_id',
    'application_time', 'application_date', 'application_month',
    'score_mlt', 'score_apply',
    *txn_feature_cols,
    'duedate_1m_30', 'duedate_3m_30',
    'principal', 'estimate_principal_remaining_mob1',
    'estimate_principal_remaining_mob3',
    'dpd_days_ever_mob1', 'dpd_days_ever_mob3',
    'application_status', 'assessment_status', 'status',
    'LTI', 'PTI', 'NSTI',
    'application_tag', 'user_tag', 'loan_tag',
]

keep_cols = [c for c in keep_cols if c in df.columns]
df = df[keep_cols].copy()

# 基础类型转换
date_cols = ['application_time', 'application_date']
for col in date_cols:
    if col in df.columns:
        df[col] = pd.to_datetime(df[col], errors='coerce')

numeric_cols = [
    'score_mlt', 'score_apply',
    'duedate_1m_30', 'duedate_3m_30',
    'principal', 'estimate_principal_remaining_mob1',
    'estimate_principal_remaining_mob3',
    'dpd_days_ever_mob1', 'dpd_days_ever_mob3',
    'LTI', 'PTI', 'NSTI',
]
numeric_cols = [c for c in numeric_cols if c in df.columns]
for col in numeric_cols:
    df[col] = pd.to_numeric(df[col], errors='coerce')

print(f'2/14 数据拼接完成，{len(df.columns)} 个字段，{len(df)} 行')


# ============================================================
# 2.1 数据校验
# ============================================================

required_cols = [
    'application_id', 'user_id', 'application_time', 'application_month',
    'score_mlt', 'score_apply',
    'duedate_1m_30', 'duedate_3m_30',
    'principal', 'estimate_principal_remaining_mob1',
    'estimate_principal_remaining_mob3',
    'dpd_days_ever_mob1', 'dpd_days_ever_mob3',
]
missing_required_cols = [c for c in required_cols if c not in df.columns]
if missing_required_cols:
    raise ValueError(f"关键字段缺失: {missing_required_cols}")


# ============================================================
# 3. 样本切分
# ============================================================

application_month_for_split = df['application_month'].astype('string')
train_mask = application_month_for_split.notna() & application_month_for_split.le(TRAIN_END_MONTH)
oot_mask = application_month_for_split.notna() & application_month_for_split.ge(OOT_START_MONTH)

df['sample_group'] = np.select(
    [
        train_mask.to_numpy(dtype=bool, na_value=False),
        oot_mask.to_numpy(dtype=bool, na_value=False),
    ],
    ['train', 'oot'],
    default='gap_or_unknown'
)

train_df = df.loc[df['sample_group'].eq('train')].copy()
oot_df = df.loc[df['sample_group'].eq('oot')].copy()

print(f'3/14 样本切分完成，Train {len(train_df)} 行，OOT {len(oot_df)} 行')


# ============================================================
# 4. 指标计算引擎：通用辅助函数
# ============================================================

def safe_div(num, den):
    """支持标量和 Series 的安全除法，分母为 0 时返回 NaN。"""
    num_arr = np.asarray(num, dtype='float64')
    den_arr = np.asarray(den, dtype='float64')
    result = np.full(np.broadcast(num_arr, den_arr).shape, np.nan, dtype='float64')
    np.divide(num_arr, den_arr, out=result, where=den_arr != 0)
    if np.ndim(result) == 0:
        return float(result)
    if isinstance(num, pd.Series):
        return pd.Series(result, index=num.index)
    if isinstance(den, pd.Series):
        return pd.Series(result, index=den.index)
    return result


def wilson_ci(numerator, denominator, alpha=0.05):
    """Wilson score interval for a binomial proportion."""
    numerator = pd.Series(numerator, dtype='float64')
    denominator = pd.Series(denominator, dtype='float64')
    z = norm.ppf(1 - alpha / 2)
    p = safe_div(numerator, denominator)

    lower = pd.Series(np.nan, index=numerator.index, dtype='float64')
    upper = pd.Series(np.nan, index=numerator.index, dtype='float64')
    valid = denominator.gt(0)

    denom = 1 + z ** 2 / denominator.loc[valid]
    center = (p.loc[valid] + z ** 2 / (2 * denominator.loc[valid])) / denom
    margin = (
        z
        * np.sqrt((p.loc[valid] * (1 - p.loc[valid]) + z ** 2 / (4 * denominator.loc[valid])) / denominator.loc[valid])
        / denom
    )
    lower.loc[valid] = (center - margin).clip(lower=0)
    upper.loc[valid] = (center + margin).clip(upper=1)
    return lower, upper


def require_columns(frame, columns, context='dataframe'):
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError(f"{context} 缺少必要字段: {missing}")


# ============================================================
# 4.1 分箱指标计算函数
# ============================================================

def calc_bin_stats(data, bin_col, score_col=None, id_col='application_id'):
    """
    按分箱计算完整风险指标。

    口径：
    - 笔数 1M30+/3M30+ 成熟样本：duedate_1m_30 / duedate_3m_30 in [0, 1]
    - 金额 1M30+/3M30+ 成熟本金：dpd_days_ever_mob1 / mob3 非空对应的 principal
    - 金额逾期本金：成熟且 dpd_days_ever_mob >= 30 的 estimate_principal_remaining_mob
    - 累计指标默认按 bin_order 从低风险端向高风险端累计
    """
    required = [
        id_col, bin_col,
        'duedate_1m_30', 'duedate_3m_30',
        'principal', 'estimate_principal_remaining_mob1',
        'estimate_principal_remaining_mob3',
        'dpd_days_ever_mob1', 'dpd_days_ever_mob3',
    ]
    if score_col is not None:
        required.append(score_col)
    require_columns(data, required, context='calc_bin_stats')

    work = data.copy()
    for col in [
        'duedate_1m_30', 'duedate_3m_30',
        'principal', 'estimate_principal_remaining_mob1',
        'estimate_principal_remaining_mob3',
        'dpd_days_ever_mob1', 'dpd_days_ever_mob3',
    ]:
        work[col] = pd.to_numeric(work[col], errors='coerce')

    work['_principal_fill0'] = work['principal'].fillna(0)
    work['_m1_mature_cnt_flag'] = work['duedate_1m_30'].isin([0, 1])
    work['_m1_bad_cnt_flag'] = work['duedate_1m_30'].eq(1)
    work['_m3_mature_cnt_flag'] = work['duedate_3m_30'].isin([0, 1])
    work['_m3_bad_cnt_flag'] = work['duedate_3m_30'].eq(1)

    work['_m1_mature_amt_flag'] = work['dpd_days_ever_mob1'].notna()
    work['_m1_bad_amt_flag'] = work['_m1_mature_amt_flag'] & work['dpd_days_ever_mob1'].ge(30)
    work['_m1_amt_exposure_value'] = np.where(work['_m1_mature_amt_flag'], work['_principal_fill0'], 0)
    work['_m1_amt_bad_value'] = np.where(
        work['_m1_bad_amt_flag'],
        work['estimate_principal_remaining_mob1'].fillna(0),
        0,
    )

    work['_m3_mature_amt_flag'] = work['dpd_days_ever_mob3'].notna()
    work['_m3_bad_amt_flag'] = work['_m3_mature_amt_flag'] & work['dpd_days_ever_mob3'].ge(30)
    work['_m3_amt_exposure_value'] = np.where(work['_m3_mature_amt_flag'], work['_principal_fill0'], 0)
    work['_m3_amt_bad_value'] = np.where(
        work['_m3_bad_amt_flag'],
        work['estimate_principal_remaining_mob3'].fillna(0),
        0,
    )

    group_cols = [bin_col]
    if 'bin_order' in work.columns and bin_col != 'bin_order':
        group_cols.append('bin_order')

    agg_dict = {
        'n': (id_col, 'count'),
        'application_id_nunique': (id_col, 'nunique'),
        'principal_amt': ('_principal_fill0', 'sum'),
        '1m30p_cnt_mature': ('_m1_mature_cnt_flag', 'sum'),
        '1m30p_cnt_bad': ('_m1_bad_cnt_flag', 'sum'),
        '3m30p_cnt_mature': ('_m3_mature_cnt_flag', 'sum'),
        '3m30p_cnt_bad': ('_m3_bad_cnt_flag', 'sum'),
        '1m30p_amt_exposure': ('_m1_amt_exposure_value', 'sum'),
        '1m30p_amt_bad': ('_m1_amt_bad_value', 'sum'),
        '1m30p_amt_bad_cnt': ('_m1_bad_amt_flag', 'sum'),
        '3m30p_amt_exposure': ('_m3_amt_exposure_value', 'sum'),
        '3m30p_amt_bad': ('_m3_amt_bad_value', 'sum'),
        '3m30p_amt_bad_cnt': ('_m3_bad_amt_flag', 'sum'),
    }
    if score_col is not None:
        agg_dict.update({
            'score_min': (score_col, 'min'),
            'score_max': (score_col, 'max'),
            'score_mean': (score_col, 'mean'),
        })

    bin_stats = work.groupby(group_cols, dropna=False, observed=True).agg(**agg_dict).reset_index()
    if 'bin_order' not in bin_stats.columns:
        bin_stats['bin_order'] = np.arange(1, len(bin_stats) + 1)
    bin_stats = bin_stats.sort_values('bin_order').reset_index(drop=True)

    total_n = bin_stats['n'].sum()
    bin_stats['total_n'] = total_n
    bin_stats['sample_pct_num'] = bin_stats['n']
    bin_stats['sample_pct_den'] = total_n
    bin_stats['sample_pct'] = safe_div(bin_stats['sample_pct_num'], bin_stats['sample_pct_den'])

    # 笔数口径
    for prefix in ['1m30p', '3m30p']:
        bin_stats[f'{prefix}_cnt_good'] = bin_stats[f'{prefix}_cnt_mature'] - bin_stats[f'{prefix}_cnt_bad']
        bin_stats[f'{prefix}_cnt_bad_rate_num'] = bin_stats[f'{prefix}_cnt_bad']
        bin_stats[f'{prefix}_cnt_bad_rate_den'] = bin_stats[f'{prefix}_cnt_mature']
        bin_stats[f'{prefix}_cnt_bad_rate'] = safe_div(
            bin_stats[f'{prefix}_cnt_bad_rate_num'],
            bin_stats[f'{prefix}_cnt_bad_rate_den'],
        )

    # 金额口径
    for prefix in ['1m30p', '3m30p']:
        bin_stats[f'{prefix}_amt_bad_rate_num'] = bin_stats[f'{prefix}_amt_bad']
        bin_stats[f'{prefix}_amt_bad_rate_den'] = bin_stats[f'{prefix}_amt_exposure']
        bin_stats[f'{prefix}_amt_bad_rate'] = safe_div(
            bin_stats[f'{prefix}_amt_bad_rate_num'],
            bin_stats[f'{prefix}_amt_bad_rate_den'],
        )

    # Lift
    overall_rates = {}
    for prefix in ['1m30p', '3m30p']:
        overall_rates[f'{prefix}_cnt'] = safe_div(
            bin_stats[f'{prefix}_cnt_bad'].sum(),
            bin_stats[f'{prefix}_cnt_mature'].sum(),
        )
        bin_stats[f'{prefix}_cnt_lift_num'] = bin_stats[f'{prefix}_cnt_bad_rate']
        bin_stats[f'{prefix}_cnt_lift_den'] = overall_rates[f'{prefix}_cnt']
        bin_stats[f'{prefix}_cnt_lift'] = safe_div(
            bin_stats[f'{prefix}_cnt_lift_num'],
            bin_stats[f'{prefix}_cnt_lift_den'],
        )

        overall_rates[f'{prefix}_amt'] = safe_div(
            bin_stats[f'{prefix}_amt_bad'].sum(),
            bin_stats[f'{prefix}_amt_exposure'].sum(),
        )
        bin_stats[f'{prefix}_amt_lift_num'] = bin_stats[f'{prefix}_amt_bad_rate']
        bin_stats[f'{prefix}_amt_lift_den'] = overall_rates[f'{prefix}_amt']
        bin_stats[f'{prefix}_amt_lift'] = safe_div(
            bin_stats[f'{prefix}_amt_lift_num'],
            bin_stats[f'{prefix}_amt_lift_den'],
        )

    # 笔数逾期率标准误和 Wilson 置信区间
    for prefix in ['1m30p', '3m30p']:
        rate_col = f'{prefix}_cnt_bad_rate'
        mature_col = f'{prefix}_cnt_mature'
        bin_stats[f'{prefix}_cnt_bad_rate_se'] = np.sqrt(
            bin_stats[rate_col]
            * (1 - bin_stats[rate_col])
            / bin_stats[mature_col].where(bin_stats[mature_col].gt(0), np.nan)
        )
        lower, upper = wilson_ci(bin_stats[f'{prefix}_cnt_bad'], bin_stats[mature_col])
        bin_stats[f'{prefix}_cnt_bad_rate_ci_lower'] = lower
        bin_stats[f'{prefix}_cnt_bad_rate_ci_upper'] = upper

    # 累计指标：从低风险端向高风险端逐箱累计
    bin_stats['cum_n'] = bin_stats['n'].cumsum()
    bin_stats['cum_principal'] = bin_stats['principal_amt'].cumsum()
    bin_stats['cum_pass_rate_num'] = bin_stats['cum_n']
    bin_stats['cum_pass_rate_den'] = total_n
    bin_stats['cum_pass_rate'] = safe_div(bin_stats['cum_pass_rate_num'], bin_stats['cum_pass_rate_den'])

    for prefix in ['1m30p', '3m30p']:
        bin_stats[f'cum_{prefix}_cnt_mature'] = bin_stats[f'{prefix}_cnt_mature'].cumsum()
        bin_stats[f'cum_{prefix}_cnt_bad'] = bin_stats[f'{prefix}_cnt_bad'].cumsum()
        bin_stats[f'cum_{prefix}_cnt_bad_rate_num'] = bin_stats[f'cum_{prefix}_cnt_bad']
        bin_stats[f'cum_{prefix}_cnt_bad_rate_den'] = bin_stats[f'cum_{prefix}_cnt_mature']
        bin_stats[f'cum_{prefix}_cnt_bad_rate'] = safe_div(
            bin_stats[f'cum_{prefix}_cnt_bad_rate_num'],
            bin_stats[f'cum_{prefix}_cnt_bad_rate_den'],
        )

        bin_stats[f'cum_{prefix}_amt_exposure'] = bin_stats[f'{prefix}_amt_exposure'].cumsum()
        bin_stats[f'cum_{prefix}_amt_bad'] = bin_stats[f'{prefix}_amt_bad'].cumsum()
        bin_stats[f'cum_{prefix}_amt_bad_cnt'] = bin_stats[f'{prefix}_amt_bad_cnt'].cumsum()
        bin_stats[f'cum_{prefix}_amt_bad_rate_num'] = bin_stats[f'cum_{prefix}_amt_bad']
        bin_stats[f'cum_{prefix}_amt_bad_rate_den'] = bin_stats[f'cum_{prefix}_amt_exposure']
        bin_stats[f'cum_{prefix}_amt_bad_rate'] = safe_div(
            bin_stats[f'cum_{prefix}_amt_bad_rate_num'],
            bin_stats[f'cum_{prefix}_amt_bad_rate_den'],
        )

    return bin_stats


# ============================================================
# 4.2 漏斗指标计算函数
# ============================================================

def calc_funnel_stats(data, group_col=None, id_col='application_id'):
    """计算申请、审批、自动/人工审批和成交漏斗。"""
    required = [id_col, 'application_status', 'assessment_status', 'status']
    require_columns(data, required, context='calc_funnel_stats')

    def one_group(g):
        apply_cnt = g[id_col].nunique()
        completed_application_cnt = g.loc[
            ~g['application_status'].isin(['0.Incomplete', '1.In Progress']),
            id_col,
        ].nunique()
        approved_application_cnt = g.loc[
            g['application_status'].astype(str).str[0].isin(['3', '4']),
            id_col,
        ].nunique()
        auto_approved_application_cnt = g.loc[
            g['application_status'].astype(str).str[0].isin(['3', '4'])
            & g['assessment_status'].astype(str).str.contains('Auto Approved', na=False),
            id_col,
        ].nunique()
        manual_approved_application_cnt = g.loc[
            g['application_status'].astype(str).str[0].isin(['3', '4'])
            & g['assessment_status'].astype(str).str.contains('Manual Approved', na=False),
            id_col,
        ].nunique()
        deal_sample_cnt = g.loc[
            g['application_status'].astype(str).str[0].isin(['3', '4'])
            & g['status'].isin(['Active_Account', 'Closed', 'Blocked']),
            id_col,
        ].nunique()

        return pd.Series({
            'apply_cnt': apply_cnt,
            'completed_application_cnt': completed_application_cnt,
            'approved_application_cnt': approved_application_cnt,
            'auto_approved_application_cnt': auto_approved_application_cnt,
            'manual_approved_application_cnt': manual_approved_application_cnt,
            'deal_sample_cnt': deal_sample_cnt,
            'completion_rate': safe_div(completed_application_cnt, apply_cnt),
            'approval_rate': safe_div(approved_application_cnt, completed_application_cnt),
            'auto_approval_rate': safe_div(auto_approved_application_cnt, completed_application_cnt),
            'manual_approval_rate': safe_div(manual_approved_application_cnt, completed_application_cnt),
            'auto_approval_share': safe_div(auto_approved_application_cnt, approved_application_cnt),
            'manual_approval_share': safe_div(manual_approved_application_cnt, approved_application_cnt),
            'deal_rate': safe_div(deal_sample_cnt, approved_application_cnt),
        })

    if group_col is None:
        return one_group(data).to_frame().T

    require_columns(data, [group_col], context='calc_funnel_stats group_col')
    rows = []
    for group_value, group_data in data.groupby(group_col, dropna=False, observed=True):
        row = one_group(group_data)
        row[group_col] = group_value
        rows.append(row)

    if not rows:
        return pd.DataFrame(columns=[group_col])
    return pd.DataFrame(rows)[[group_col] + [c for c in rows[0].index if c != group_col]].reset_index(drop=True)


metric_columns_preview = [
    'n', 'sample_pct', 'principal_amt',
    '1m30p_cnt_mature', '1m30p_cnt_bad_rate',
    '3m30p_cnt_mature', '3m30p_cnt_bad_rate',
    '1m30p_amt_exposure', '1m30p_amt_bad_rate',
    '3m30p_amt_exposure', '3m30p_amt_bad_rate',
    '1m30p_cnt_lift', '3m30p_cnt_lift',
    'cum_pass_rate', 'cum_1m30p_cnt_bad_rate', 'cum_3m30p_cnt_bad_rate',
]

print('4/14 指标计算函数已就绪')


# ============================================================
# 5. 等频初分：边界学习与复用
# ============================================================

def learn_equal_freq_edges(data, score_col, n_bins=20):
    """在训练样本上学习等频分箱边界，首尾扩展为 -inf / inf。"""
    require_columns(data, [score_col], context='learn_equal_freq_edges')
    score = pd.to_numeric(data[score_col], errors='coerce').dropna()
    if score.empty:
        raise ValueError(f"{score_col} 全为空，无法分箱")

    _, raw_edges = pd.qcut(score, q=n_bins, retbins=True, duplicates='drop')
    edges = np.asarray(raw_edges, dtype='float64')
    edges = np.unique(edges)
    if len(edges) < 2:
        raise ValueError(f"{score_col} 可用唯一值不足，无法形成有效分箱")

    edges[0] = -np.inf
    edges[-1] = np.inf
    return edges


def build_bin_edge_table(edges, bin_prefix='B'):
    """生成分箱边界配置表。"""
    rows = []
    for i in range(len(edges) - 1):
        bin_order = i + 1
        bin_label = f"{bin_prefix}{bin_order:02d}"
        rows.append({
            'bin_order': bin_order,
            'bin_label': bin_label,
            'score_left': edges[i],
            'score_right': edges[i + 1],
            'interval_rule': '(left, right]',
        })
    return pd.DataFrame(rows)


def apply_equal_freq_edges(data, score_col, edges, bin_col):
    """将已学习的边界套用到任意样本，保证 train / OOT 口径一致。"""
    require_columns(data, [score_col], context='apply_equal_freq_edges')
    out = data.copy()
    labels = list(range(1, len(edges)))
    bin_order = pd.cut(
        pd.to_numeric(out[score_col], errors='coerce'),
        bins=edges,
        labels=labels,
        include_lowest=True,
        right=True,
    )
    out['bin_order'] = bin_order.astype('Int64')
    label_map = {i: f"B{i:02d}" for i in labels}
    out[bin_col] = out['bin_order'].map(label_map)
    return out


def make_equal_freq_bins(data, score_col, n_bins=20, bin_col='bin20'):
    """学习等频边界并返回带分箱字段的数据、边界数组和边界表。"""
    edges = learn_equal_freq_edges(data, score_col=score_col, n_bins=n_bins)
    binned = apply_equal_freq_edges(data, score_col=score_col, edges=edges, bin_col=bin_col)
    edge_table = build_bin_edge_table(edges)
    edge_table = edge_table.rename(columns={'bin_label': bin_col})
    return binned, edges, edge_table


# ============================================================
# 5.1 主模型 score_mlt 20 等频初分
# ============================================================

SCORE_COL = 'score_mlt'
BIN20_COL = 'score_mlt_bin20'

train_binned_20, score_mlt_bin_edges, score_mlt_bin_edges_df = make_equal_freq_bins(
    train_df,
    score_col=SCORE_COL,
    n_bins=20,
    bin_col=BIN20_COL,
)

# 使用训练期边界套用全量和 OOT，不能在 OOT 上重新学习边界
df_binned_20 = apply_equal_freq_edges(
    df,
    score_col=SCORE_COL,
    edges=score_mlt_bin_edges,
    bin_col=BIN20_COL,
)
train_binned_20 = df_binned_20.loc[df_binned_20['sample_group'].eq('train')].copy()
oot_binned_20 = df_binned_20.loc[df_binned_20['sample_group'].eq('oot')].copy()

bin_stats_20 = calc_bin_stats(
    train_binned_20,
    bin_col=BIN20_COL,
    score_col=SCORE_COL,
)
bin_stats_20 = bin_stats_20.merge(
    score_mlt_bin_edges_df,
    on=['bin_order', BIN20_COL],
    how='left',
)

print(f'5/14 score_mlt 等频初分完成，{len(score_mlt_bin_edges) - 1} 箱')


# ============================================================
# 6. 20箱初步诊断
# ============================================================

DIAG_CONFIG = {
    'min_bin_n': 1000,
    'min_cnt_mature': 1000,
    'min_cnt_bad': 30,
    'amt_cnt_gap_threshold': 0.03,
}


def _format_flag_list(flags):
    return '；'.join(flags) if flags else 'OK'


def diagnose_bin_stats(bin_stats, config=None):
    """对初始分箱做样本量、bad量、倒挂、置信区间和金额口径诊断。"""
    cfg = dict(DIAG_CONFIG)
    if config:
        cfg.update(config)

    required = [
        'bin_order', 'n',
        '1m30p_cnt_mature', '1m30p_cnt_bad', '1m30p_cnt_bad_rate',
        '1m30p_cnt_bad_rate_ci_lower', '1m30p_cnt_bad_rate_ci_upper',
        '3m30p_cnt_mature', '3m30p_cnt_bad', '3m30p_cnt_bad_rate',
        '3m30p_cnt_bad_rate_ci_lower', '3m30p_cnt_bad_rate_ci_upper',
        '1m30p_amt_bad_rate', '3m30p_amt_bad_rate',
    ]
    require_columns(bin_stats, required, context='diagnose_bin_stats')

    diag = bin_stats.copy().sort_values('bin_order').reset_index(drop=True)
    diag['prev_1m30p_cnt_bad_rate'] = diag['1m30p_cnt_bad_rate'].shift(1)
    diag['prev_3m30p_cnt_bad_rate'] = diag['3m30p_cnt_bad_rate'].shift(1)
    diag['1m30p_cnt_rate_diff_prev'] = diag['1m30p_cnt_bad_rate'] - diag['prev_1m30p_cnt_bad_rate']
    diag['3m30p_cnt_rate_diff_prev'] = diag['3m30p_cnt_bad_rate'] - diag['prev_3m30p_cnt_bad_rate']

    diag['low_bin_n_flag'] = diag['n'].lt(cfg['min_bin_n'])
    diag['low_1m30p_mature_flag'] = diag['1m30p_cnt_mature'].lt(cfg['min_cnt_mature'])
    diag['low_3m30p_mature_flag'] = diag['3m30p_cnt_mature'].lt(cfg['min_cnt_mature'])
    diag['low_1m30p_bad_flag'] = diag['1m30p_cnt_bad'].lt(cfg['min_cnt_bad'])
    diag['low_3m30p_bad_flag'] = diag['3m30p_cnt_bad'].lt(cfg['min_cnt_bad'])

    diag['1m30p_inversion_flag'] = diag['1m30p_cnt_rate_diff_prev'].lt(0).fillna(False)
    diag['3m30p_inversion_flag'] = diag['3m30p_cnt_rate_diff_prev'].lt(0).fillna(False)

    diag['prev_1m30p_ci_upper'] = diag['1m30p_cnt_bad_rate_ci_upper'].shift(1)
    diag['prev_1m30p_ci_lower'] = diag['1m30p_cnt_bad_rate_ci_lower'].shift(1)
    diag['prev_3m30p_ci_upper'] = diag['3m30p_cnt_bad_rate_ci_upper'].shift(1)
    diag['prev_3m30p_ci_lower'] = diag['3m30p_cnt_bad_rate_ci_lower'].shift(1)
    diag['1m30p_ci_overlap_prev_flag'] = (
        diag['prev_1m30p_ci_upper'].notna()
        & diag['1m30p_cnt_bad_rate_ci_lower'].le(diag['prev_1m30p_ci_upper'])
        & diag['prev_1m30p_ci_lower'].le(diag['1m30p_cnt_bad_rate_ci_upper'])
    )
    diag['3m30p_ci_overlap_prev_flag'] = (
        diag['prev_3m30p_ci_upper'].notna()
        & diag['3m30p_cnt_bad_rate_ci_lower'].le(diag['prev_3m30p_ci_upper'])
        & diag['prev_3m30p_ci_lower'].le(diag['3m30p_cnt_bad_rate_ci_upper'])
    )

    diag['1m30p_amt_missing_flag'] = diag['1m30p_amt_bad_rate'].isna()
    diag['3m30p_amt_missing_flag'] = diag['3m30p_amt_bad_rate'].isna()
    diag['1m30p_amt_cnt_gap'] = (diag['1m30p_amt_bad_rate'] - diag['1m30p_cnt_bad_rate']).abs()
    diag['3m30p_amt_cnt_gap'] = (diag['3m30p_amt_bad_rate'] - diag['3m30p_cnt_bad_rate']).abs()
    diag['1m30p_amt_cnt_gap_flag'] = diag['1m30p_amt_cnt_gap'].gt(cfg['amt_cnt_gap_threshold']).fillna(False)
    diag['3m30p_amt_cnt_gap_flag'] = diag['3m30p_amt_cnt_gap'].gt(cfg['amt_cnt_gap_threshold']).fillna(False)

    flag_cols = [
        'low_bin_n_flag',
        'low_1m30p_mature_flag', 'low_3m30p_mature_flag',
        'low_1m30p_bad_flag', 'low_3m30p_bad_flag',
        '1m30p_inversion_flag', '3m30p_inversion_flag',
        '1m30p_ci_overlap_prev_flag', '3m30p_ci_overlap_prev_flag',
        '1m30p_amt_missing_flag', '3m30p_amt_missing_flag',
        '1m30p_amt_cnt_gap_flag', '3m30p_amt_cnt_gap_flag',
    ]
    diag['diagnosis_flag_cnt'] = diag[flag_cols].sum(axis=1)

    def collect_flags(row):
        flags = []
        if row['low_bin_n_flag']:
            flags.append('样本量不足')
        if row['low_1m30p_mature_flag']:
            flags.append('1M30成熟不足')
        if row['low_3m30p_mature_flag']:
            flags.append('3M30成熟不足')
        if row['low_1m30p_bad_flag']:
            flags.append('1M30 bad不足')
        if row['low_3m30p_bad_flag']:
            flags.append('3M30 bad不足')
        if row['1m30p_inversion_flag']:
            flags.append('1M30倒挂')
        if row['3m30p_inversion_flag']:
            flags.append('3M30倒挂')
        if row['1m30p_ci_overlap_prev_flag']:
            flags.append('1M30相邻CI重叠')
        if row['3m30p_ci_overlap_prev_flag']:
            flags.append('3M30相邻CI重叠')
        if row['1m30p_amt_missing_flag']:
            flags.append('1M30金额缺失')
        if row['3m30p_amt_missing_flag']:
            flags.append('3M30金额缺失')
        if row['1m30p_amt_cnt_gap_flag']:
            flags.append('1M30金额/笔数差异大')
        if row['3m30p_amt_cnt_gap_flag']:
            flags.append('3M30金额/笔数差异大')
        return _format_flag_list(flags)

    diag['diagnosis_flags'] = diag.apply(collect_flags, axis=1)
    diag['merge_priority_score'] = (
        diag['low_bin_n_flag'].astype(int) * 3
        + diag['low_1m30p_mature_flag'].astype(int) * 2
        + diag['low_3m30p_mature_flag'].astype(int) * 2
        + diag['low_1m30p_bad_flag'].astype(int) * 2
        + diag['low_3m30p_bad_flag'].astype(int) * 2
        + diag['1m30p_inversion_flag'].astype(int) * 3
        + diag['3m30p_inversion_flag'].astype(int) * 4
        + diag['1m30p_ci_overlap_prev_flag'].astype(int)
        + diag['3m30p_ci_overlap_prev_flag'].astype(int)
        + diag['1m30p_amt_missing_flag'].astype(int)
        + diag['3m30p_amt_missing_flag'].astype(int)
        + diag['1m30p_amt_cnt_gap_flag'].astype(int)
        + diag['3m30p_amt_cnt_gap_flag'].astype(int)
    )
    return diag


# ============================================================
# 6.1 生成 score_mlt 20箱诊断表
# ============================================================

bin_diagnosis_20 = diagnose_bin_stats(bin_stats_20)

diagnosis_summary = pd.Series({
    'bin_cnt': len(bin_diagnosis_20),
    '1m30p_inversion_cnt': int(bin_diagnosis_20['1m30p_inversion_flag'].sum()),
    '3m30p_inversion_cnt': int(bin_diagnosis_20['3m30p_inversion_flag'].sum()),
    'low_1m30p_mature_cnt': int(bin_diagnosis_20['low_1m30p_mature_flag'].sum()),
    'low_3m30p_mature_cnt': int(bin_diagnosis_20['low_3m30p_mature_flag'].sum()),
    'low_1m30p_bad_cnt': int(bin_diagnosis_20['low_1m30p_bad_flag'].sum()),
    'low_3m30p_bad_cnt': int(bin_diagnosis_20['low_3m30p_bad_flag'].sum()),
    '1m30p_ci_overlap_prev_cnt': int(bin_diagnosis_20['1m30p_ci_overlap_prev_flag'].sum()),
    '3m30p_ci_overlap_prev_cnt': int(bin_diagnosis_20['3m30p_ci_overlap_prev_flag'].sum()),
    '1m30p_amt_cnt_gap_cnt': int(bin_diagnosis_20['1m30p_amt_cnt_gap_flag'].sum()),
    '3m30p_amt_cnt_gap_cnt': int(bin_diagnosis_20['3m30p_amt_cnt_gap_flag'].sum()),
}, name='value')

inv_cnt = diagnosis_summary['3m30p_inversion_cnt']
print(f'6/14 20箱诊断完成，3M30倒挂 {inv_cnt} 箱')


# ============================================================
# 7. 相邻箱合并：映射、边界和单调性检查
# ============================================================

def build_adjacent_merge_map(bin_ranges, source_bin_col, target_bin_col='score_mlt_final_bin'):
    """根据相邻 20 箱范围生成最终分箱映射表。"""
    rows = []
    for final_order, (start_bin, end_bin) in enumerate(bin_ranges, start=1):
        final_bin = f"G{final_order:02d}"
        for bin_order in range(start_bin, end_bin + 1):
            rows.append({
                'bin_order': bin_order,
                source_bin_col: f"B{bin_order:02d}",
                'final_bin_order': final_order,
                target_bin_col: final_bin,
                'merged_from': f"B{start_bin:02d}-B{end_bin:02d}" if start_bin != end_bin else f"B{start_bin:02d}",
            })
    merge_map = pd.DataFrame(rows)
    if merge_map['bin_order'].duplicated().any():
        raise ValueError('合箱映射存在重复 bin_order')
    return merge_map.sort_values('bin_order').reset_index(drop=True)


def apply_merge_map(data, merge_map, source_bin_col, target_bin_col='score_mlt_final_bin'):
    """将 20 箱映射到最终风险等级。"""
    require_columns(data, [source_bin_col], context='apply_merge_map data')
    require_columns(merge_map, [source_bin_col, 'final_bin_order', target_bin_col], context='apply_merge_map merge_map')
    out = data.merge(
        merge_map[[source_bin_col, 'final_bin_order', target_bin_col]],
        on=source_bin_col,
        how='left',
    )
    out['bin_order'] = out['final_bin_order'].astype('Int64')
    return out


def build_final_edge_table(edge_table, merge_map, source_bin_col, target_bin_col='score_mlt_final_bin'):
    """从 20 箱边界和合箱映射生成最终等级边界表。"""
    require_columns(edge_table, ['bin_order', source_bin_col, 'score_left', 'score_right'], context='final edge source')
    merged_edges = edge_table.merge(
        merge_map[[source_bin_col, 'final_bin_order', target_bin_col, 'merged_from']],
        on=source_bin_col,
        how='left',
    )
    final_edges = (
        merged_edges.groupby(['final_bin_order', target_bin_col, 'merged_from'], observed=True)
        .agg(
            score_left=('score_left', 'first'),
            score_right=('score_right', 'last'),
            source_bin_start=('bin_order', 'min'),
            source_bin_end=('bin_order', 'max'),
        )
        .reset_index()
        .sort_values('final_bin_order')
        .reset_index(drop=True)
    )
    final_edges['interval_rule'] = '(left, right]'
    return final_edges


def check_monotonicity(stats, rate_cols):
    """检查最终箱风险率是否随风险等级非递减。"""
    rows = []
    ordered = stats.sort_values('bin_order').reset_index(drop=True)
    for col in rate_cols:
        diff = ordered[col].diff()
        violation_mask = diff.lt(0).fillna(False)
        rows.append({
            'metric': col,
            'is_monotonic_non_decreasing': not bool(violation_mask.any()),
            'violation_cnt': int(violation_mask.sum()),
            'violation_bins': ','.join(ordered.loc[violation_mask, 'bin_order'].astype(str).tolist()),
        })
    return pd.DataFrame(rows)


# ============================================================
# 7.1 生成主模型最终风险等级
# ============================================================

# 根据第6步诊断，先做 8 档相邻合箱：
# - B06-B07 是主要倒挂区域，并入 B08 平滑风险排序
# - B13-B14 轻微 1M30 倒挂，合入同一档
# - 高风险端 B17-B20 保留两档，便于策略阈值区分高风险尾部
FINAL_BIN_RANGES = [
    (1, 2),
    (3, 5),
    (6, 8),
    (9, 11),
    (12, 14),
    (15, 16),
    (17, 18),
    (19, 20),
]

FINAL_BIN_COL = 'score_mlt_final_bin'
score_mlt_final_merge_map = build_adjacent_merge_map(
    FINAL_BIN_RANGES,
    source_bin_col=BIN20_COL,
    target_bin_col=FINAL_BIN_COL,
)
score_mlt_final_edges_df = build_final_edge_table(
    score_mlt_bin_edges_df,
    score_mlt_final_merge_map,
    source_bin_col=BIN20_COL,
    target_bin_col=FINAL_BIN_COL,
)

train_final_binned = apply_merge_map(
    train_binned_20,
    score_mlt_final_merge_map,
    source_bin_col=BIN20_COL,
    target_bin_col=FINAL_BIN_COL,
)
oot_final_binned = apply_merge_map(
    oot_binned_20,
    score_mlt_final_merge_map,
    source_bin_col=BIN20_COL,
    target_bin_col=FINAL_BIN_COL,
)
df_final_binned = apply_merge_map(
    df_binned_20,
    score_mlt_final_merge_map,
    source_bin_col=BIN20_COL,
    target_bin_col=FINAL_BIN_COL,
)

bin_stats_final = calc_bin_stats(
    train_final_binned,
    bin_col=FINAL_BIN_COL,
    score_col=SCORE_COL,
)
bin_stats_final = bin_stats_final.merge(
    score_mlt_final_edges_df,
    left_on=['bin_order', FINAL_BIN_COL],
    right_on=['final_bin_order', FINAL_BIN_COL],
    how='left',
)

final_monotonicity_check = check_monotonicity(
    bin_stats_final,
    [
        '1m30p_cnt_bad_rate',
        '3m30p_cnt_bad_rate',
        '1m30p_amt_bad_rate',
        '3m30p_amt_bad_rate',
    ],
)

mono_ok = final_monotonicity_check['is_monotonic_non_decreasing'].all()
print(f'7/14 最终分箱完成，单调性 {"OK" if mono_ok else "存在倒挂"}')


# ============================================================
# 8. OOT 与跨月验证：PSI、AUC/KS、排序稳定性
# ============================================================

def calc_group_bin_stats(data, group_col, bin_col, score_col=None):
    """按月份或样本组分别计算最终箱指标。"""
    require_columns(data, [group_col, bin_col], context='calc_group_bin_stats')
    rows = []
    for group_value, group_data in data.groupby(group_col, dropna=False, observed=True):
        if group_data.empty:
            continue
        stats = calc_bin_stats(group_data, bin_col=bin_col, score_col=score_col)
        stats.insert(0, group_col, group_value)
        rows.append(stats)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def calc_population_psi(expected_data, actual_data, bin_col, base_bins, eps=1e-6):
    """计算 expected vs actual 的分箱分布 PSI。"""
    require_columns(expected_data, [bin_col], context='PSI expected')
    require_columns(actual_data, [bin_col], context='PSI actual')
    require_columns(base_bins, [bin_col, 'final_bin_order'], context='PSI base_bins')

    base = base_bins[['final_bin_order', bin_col]].drop_duplicates().sort_values('final_bin_order')
    expected_cnt = expected_data[bin_col].value_counts(dropna=False).rename('expected_cnt')
    actual_cnt = actual_data[bin_col].value_counts(dropna=False).rename('actual_cnt')
    psi = (
        base.merge(expected_cnt, left_on=bin_col, right_index=True, how='left')
            .merge(actual_cnt, left_on=bin_col, right_index=True, how='left')
            .fillna({'expected_cnt': 0, 'actual_cnt': 0})
    )
    psi['expected_pct'] = safe_div(psi['expected_cnt'], psi['expected_cnt'].sum())
    psi['actual_pct'] = safe_div(psi['actual_cnt'], psi['actual_cnt'].sum())
    expected_pct_clip = psi['expected_pct'].clip(lower=eps)
    actual_pct_clip = psi['actual_pct'].clip(lower=eps)
    psi['psi_component'] = (actual_pct_clip - expected_pct_clip) * np.log(actual_pct_clip / expected_pct_clip)
    psi['psi_total'] = psi['psi_component'].sum()
    return psi


def calc_auc_ks(data, score_col, label_col):
    """不用 sklearn，直接计算高分高风险模型的 AUC 和 KS。"""
    require_columns(data, [score_col, label_col], context='calc_auc_ks')
    work = data[[score_col, label_col]].copy()
    work[score_col] = pd.to_numeric(work[score_col], errors='coerce')
    work[label_col] = pd.to_numeric(work[label_col], errors='coerce')
    work = work.loc[work[score_col].notna() & work[label_col].isin([0, 1])].copy()
    n = len(work)
    bad_cnt = int(work[label_col].eq(1).sum())
    good_cnt = int(work[label_col].eq(0).sum())
    if n == 0 or bad_cnt == 0 or good_cnt == 0:
        return pd.Series({'n': n, 'bad_cnt': bad_cnt, 'good_cnt': good_cnt, 'bad_rate': safe_div(bad_cnt, n), 'auc': np.nan, 'ks': np.nan})

    ranks = work[score_col].rank(method='average')
    bad_rank_sum = ranks.loc[work[label_col].eq(1)].sum()
    auc = (bad_rank_sum - bad_cnt * (bad_cnt + 1) / 2) / (bad_cnt * good_cnt)

    ordered = work.sort_values(score_col, ascending=not HIGH_SCORE_HIGH_RISK)
    cum_bad = ordered[label_col].eq(1).cumsum() / bad_cnt
    cum_good = ordered[label_col].eq(0).cumsum() / good_cnt
    ks = (cum_bad - cum_good).abs().max()
    return pd.Series({'n': n, 'bad_cnt': bad_cnt, 'good_cnt': good_cnt, 'bad_rate': safe_div(bad_cnt, n), 'auc': auc, 'ks': ks})


def calc_perf_by_group(data, group_col, score_col, label_cols):
    """按样本组或月份计算 AUC/KS。"""
    rows = []
    for group_value, group_data in data.groupby(group_col, dropna=False, observed=True):
        for label_col in label_cols:
            row = calc_auc_ks(group_data, score_col=score_col, label_col=label_col)
            row[group_col] = group_value
            row['label'] = label_col
            rows.append(row)
    if not rows:
        return pd.DataFrame()
    first_cols = [group_col, 'label']
    result = pd.DataFrame(rows)
    return result[first_cols + [c for c in result.columns if c not in first_cols]].reset_index(drop=True)


def build_monthly_stability_summary(monthly_stats, month_col='application_month'):
    """汇总每月最终箱风险排序稳定性。"""
    rows = []
    rate_cols = [
        '1m30p_cnt_bad_rate',
        '3m30p_cnt_bad_rate',
        '1m30p_amt_bad_rate',
        '3m30p_amt_bad_rate',
    ]
    for month, stats in monthly_stats.groupby(month_col, dropna=False, observed=True):
        checks = check_monotonicity(stats, rate_cols)
        row = {
            month_col: month,
            'bin_cnt': stats[FINAL_BIN_COL].nunique(dropna=True),
            'n': stats['n'].sum(),
            'm1_mature': stats['1m30p_cnt_mature'].sum(),
            'm3_mature': stats['3m30p_cnt_mature'].sum(),
            'm1_bad_rate': safe_div(stats['1m30p_cnt_bad'].sum(), stats['1m30p_cnt_mature'].sum()),
            'm3_bad_rate': safe_div(stats['3m30p_cnt_bad'].sum(), stats['3m30p_cnt_mature'].sum()),
            'min_bin_n': stats['n'].min(),
            'min_m1_mature_per_bin': stats['1m30p_cnt_mature'].min(),
            'min_m3_mature_per_bin': stats['3m30p_cnt_mature'].min(),
        }
        for _, check_row in checks.iterrows():
            metric = check_row['metric']
            row[f'{metric}_violation_cnt'] = check_row['violation_cnt']
            row[f'{metric}_violation_bins'] = check_row['violation_bins']
        rows.append(row)
    return pd.DataFrame(rows).sort_values(month_col).reset_index(drop=True)


# ============================================================
# 8.1 OOT、跨月、PSI、AUC/KS 验证
# ============================================================

oot_bin_stats_final = calc_bin_stats(
    oot_final_binned,
    bin_col=FINAL_BIN_COL,
    score_col=SCORE_COL,
).merge(
    score_mlt_final_edges_df,
    left_on=['bin_order', FINAL_BIN_COL],
    right_on=['final_bin_order', FINAL_BIN_COL],
    how='left',
)

oot_monotonicity_check = check_monotonicity(
    oot_bin_stats_final,
    ['1m30p_cnt_bad_rate', '3m30p_cnt_bad_rate', '1m30p_amt_bad_rate', '3m30p_amt_bad_rate'],
)

compare_cols = [
    FINAL_BIN_COL, 'merged_from', 'score_left', 'score_right',
    'n', 'sample_pct',
    '1m30p_cnt_mature', '1m30p_cnt_bad_rate',
    '3m30p_cnt_mature', '3m30p_cnt_bad_rate',
    '1m30p_amt_bad_rate', '3m30p_amt_bad_rate',
]
train_oot_bin_compare = bin_stats_final[compare_cols].merge(
    oot_bin_stats_final[compare_cols],
    on=[FINAL_BIN_COL, 'merged_from', 'score_left', 'score_right'],
    how='outer',
    suffixes=('_train', '_oot'),
)

psi_final = calc_population_psi(
    train_final_binned,
    oot_final_binned,
    bin_col=FINAL_BIN_COL,
    base_bins=score_mlt_final_merge_map[[FINAL_BIN_COL, 'final_bin_order']].drop_duplicates(),
)

perf_by_group = calc_perf_by_group(
    df_final_binned,
    group_col='sample_group',
    score_col=SCORE_COL,
    label_cols=['duedate_1m_30', 'duedate_3m_30'],
)

monthly_bin_stats_final = calc_group_bin_stats(
    df_final_binned,
    group_col='application_month',
    bin_col=FINAL_BIN_COL,
    score_col=SCORE_COL,
)
monthly_stability_summary = build_monthly_stability_summary(monthly_bin_stats_final)
monthly_perf = calc_perf_by_group(
    df_final_binned,
    group_col='application_month',
    score_col=SCORE_COL,
    label_cols=['duedate_1m_30', 'duedate_3m_30'],
)

print(f'8/14 OOT验证完成，PSI={psi_final["psi_total"].iloc[0]:.4f}，Train AUC={perf_by_group.loc[perf_by_group["sample_group"].eq("train") & perf_by_group["label"].eq("duedate_3m_30"), "auc"].iloc[0]:.4f}，OOT AUC={perf_by_group.loc[perf_by_group["sample_group"].eq("oot") & perf_by_group["label"].eq("duedate_3m_30"), "auc"].iloc[0]:.4f}')


# ============================================================
# 9. 阈值曲线：累计风险与边际风险
# ============================================================

def _threshold_metric_snapshot(data, score_col, threshold, prev_threshold=None):
    """计算单个阈值下的累计通过人群和边际新增人群指标。"""
    score = pd.to_numeric(data[score_col], errors='coerce')
    if HIGH_SCORE_HIGH_RISK:
        pass_mask = score.le(threshold)
        marginal_mask = pass_mask if prev_threshold is None else score.gt(prev_threshold) & score.le(threshold)
    else:
        pass_mask = score.ge(threshold)
        marginal_mask = pass_mask if prev_threshold is None else score.lt(prev_threshold) & score.ge(threshold)

    pass_data = data.loc[pass_mask].copy()
    marginal_data = data.loc[marginal_mask].copy()

    def metric_prefix(frame, prefix):
        m1_mature = frame['duedate_1m_30'].isin([0, 1]).sum()
        m1_bad = frame['duedate_1m_30'].eq(1).sum()
        m3_mature = frame['duedate_3m_30'].isin([0, 1]).sum()
        m3_bad = frame['duedate_3m_30'].eq(1).sum()

        m1_amt_exposure = frame.loc[frame['dpd_days_ever_mob1'].notna(), 'principal'].fillna(0).sum()
        m1_amt_bad = frame.loc[
            frame['dpd_days_ever_mob1'].notna() & frame['dpd_days_ever_mob1'].ge(30),
            'estimate_principal_remaining_mob1',
        ].fillna(0).sum()
        m3_amt_exposure = frame.loc[frame['dpd_days_ever_mob3'].notna(), 'principal'].fillna(0).sum()
        m3_amt_bad = frame.loc[
            frame['dpd_days_ever_mob3'].notna() & frame['dpd_days_ever_mob3'].ge(30),
            'estimate_principal_remaining_mob3',
        ].fillna(0).sum()

        return {
            f'{prefix}_n': len(frame),
            f'{prefix}_principal': frame['principal'].fillna(0).sum(),
            f'{prefix}_1m30p_cnt_mature': m1_mature,
            f'{prefix}_1m30p_cnt_bad': m1_bad,
            f'{prefix}_1m30p_cnt_bad_rate': safe_div(m1_bad, m1_mature),
            f'{prefix}_3m30p_cnt_mature': m3_mature,
            f'{prefix}_3m30p_cnt_bad': m3_bad,
            f'{prefix}_3m30p_cnt_bad_rate': safe_div(m3_bad, m3_mature),
            f'{prefix}_1m30p_amt_exposure': m1_amt_exposure,
            f'{prefix}_1m30p_amt_bad': m1_amt_bad,
            f'{prefix}_1m30p_amt_bad_rate': safe_div(m1_amt_bad, m1_amt_exposure),
            f'{prefix}_3m30p_amt_exposure': m3_amt_exposure,
            f'{prefix}_3m30p_amt_bad': m3_amt_bad,
            f'{prefix}_3m30p_amt_bad_rate': safe_div(m3_amt_bad, m3_amt_exposure),
        }

    row = {'threshold': threshold, 'prev_threshold': prev_threshold}
    row.update(metric_prefix(pass_data, 'cum'))
    row.update(metric_prefix(marginal_data, 'marginal'))
    return row


def calc_threshold_curve(data, score_col, thresholds):
    """给定候选阈值，计算累计通过率和边际风险曲线。"""
    required = [
        score_col,
        'duedate_1m_30', 'duedate_3m_30',
        'principal', 'estimate_principal_remaining_mob1', 'estimate_principal_remaining_mob3',
        'dpd_days_ever_mob1', 'dpd_days_ever_mob3',
    ]
    require_columns(data, required, context='calc_threshold_curve')
    clean_thresholds = pd.Series(thresholds, dtype='float64').replace([np.inf, -np.inf], np.nan).dropna()
    clean_thresholds = np.sort(clean_thresholds.unique())
    if len(clean_thresholds) == 0:
        raise ValueError('候选阈值为空')

    rows = []
    prev_threshold = None
    total_n = len(data)
    total_principal = data['principal'].fillna(0).sum()
    for idx, threshold in enumerate(clean_thresholds, start=1):
        row = _threshold_metric_snapshot(data, score_col, threshold, prev_threshold=prev_threshold)
        row['threshold_order'] = idx
        row['cum_pass_rate'] = safe_div(row['cum_n'], total_n)
        row['cum_principal_pct'] = safe_div(row['cum_principal'], total_principal)
        row['marginal_sample_pct'] = safe_div(row['marginal_n'], total_n)
        row['marginal_principal_pct'] = safe_div(row['marginal_principal'], total_principal)
        rows.append(row)
        prev_threshold = threshold

    return pd.DataFrame(rows).sort_values('threshold_order').reset_index(drop=True)


def final_bin_threshold_table(final_edges, data, score_col):
    """使用最终箱右边界作为候选阈值；尾箱 inf 用样本最大分代替，保留全量通过点。"""
    require_columns(final_edges, ['final_bin_order', FINAL_BIN_COL, 'score_right', 'merged_from'], context='final_bin_threshold_table edges')
    require_columns(data, [score_col], context='final_bin_threshold_table data')
    table = final_edges[['final_bin_order', FINAL_BIN_COL, 'score_right', 'merged_from']].copy()
    max_score = pd.to_numeric(data[score_col], errors='coerce').max()
    table['threshold'] = table['score_right'].replace(np.inf, max_score)
    table = table.loc[table['threshold'].notna()].copy()
    return table.sort_values('final_bin_order').reset_index(drop=True)


def quantile_thresholds(data, score_col, n_quantiles=100):
    """使用分位点生成细粒度候选阈值。"""
    score = pd.to_numeric(data[score_col], errors='coerce').dropna()
    qs = np.linspace(0.01, 0.99, n_quantiles - 1)
    thresholds = score.quantile(qs).drop_duplicates().tolist()
    thresholds.append(score.max())
    return sorted(pd.Series(thresholds, dtype='float64').dropna().unique())


# ============================================================
# 9.1 主模型 score_mlt 阈值曲线
# ============================================================

final_bin_threshold_df = final_bin_threshold_table(score_mlt_final_edges_df, train_final_binned, SCORE_COL)

threshold_curve_final_bins = calc_threshold_curve(
    train_final_binned,
    score_col=SCORE_COL,
    thresholds=final_bin_threshold_df['threshold'],
)
threshold_curve_final_bins = threshold_curve_final_bins.merge(
    final_bin_threshold_df[[FINAL_BIN_COL, 'final_bin_order', 'threshold', 'score_right', 'merged_from']],
    left_on='threshold',
    right_on='threshold',
    how='left',
)

threshold_curve_quantile = calc_threshold_curve(
    train_final_binned,
    score_col=SCORE_COL,
    thresholds=quantile_thresholds(train_final_binned, SCORE_COL, n_quantiles=100),
)

print(f'9/14 阈值曲线计算完成，共 {len(threshold_curve_final_bins)} 个候选阈值')


# ============================================================
# 10. 策略方案：自动通过 / 人工审核 / 拒绝
# ============================================================

STRATEGY_CONFIGS = [
    {
        'strategy_name': '保守方案',
        'objective': '优先控制风险，自动通过只覆盖低风险核心人群',
        'auto_constraints': {
            'max_cum_1m30p_cnt_bad_rate': 0.0070,
            'max_cum_3m30p_cnt_bad_rate': 0.0450,
            'max_marginal_3m30p_cnt_bad_rate': 0.0700,
        },
        'accept_constraints': {
            'max_cum_1m30p_cnt_bad_rate': 0.0110,
            'max_cum_3m30p_cnt_bad_rate': 0.0630,
            'max_marginal_3m30p_cnt_bad_rate': 0.1200,
        },
    },
    {
        'strategy_name': '平衡方案',
        'objective': '平衡通过率、整体风险和边际风险',
        'auto_constraints': {
            'max_cum_1m30p_cnt_bad_rate': 0.0090,
            'max_cum_3m30p_cnt_bad_rate': 0.0550,
            'max_marginal_3m30p_cnt_bad_rate': 0.0900,
        },
        'accept_constraints': {
            'max_cum_1m30p_cnt_bad_rate': 0.0130,
            'max_cum_3m30p_cnt_bad_rate': 0.0750,
            'max_marginal_3m30p_cnt_bad_rate': 0.1700,
        },
    },
    {
        'strategy_name': '增长方案',
        'objective': '在风险底线内扩大接纳规模',
        'auto_constraints': {
            'max_cum_1m30p_cnt_bad_rate': 0.0110,
            'max_cum_3m30p_cnt_bad_rate': 0.0630,
            'max_marginal_3m30p_cnt_bad_rate': 0.1200,
        },
        'accept_constraints': {
            'max_cum_1m30p_cnt_bad_rate': 0.0155,
            'max_cum_3m30p_cnt_bad_rate': 0.0850,
            'max_marginal_3m30p_cnt_bad_rate': 0.2200,
        },
    },
]


def select_threshold_under_constraints(curve, constraints):
    """在候选曲线中选择满足约束且通过率最高的阈值。"""
    eligible = curve.copy()
    for constraint_name, max_value in constraints.items():
        metric = constraint_name.removeprefix('max_')
        require_columns(eligible, [metric], context='select_threshold_under_constraints')
        eligible = eligible.loc[eligible[metric].le(max_value)]
    if eligible.empty:
        return None
    return eligible.sort_values(['cum_pass_rate', 'threshold_order'], ascending=[False, False]).iloc[0]


def calc_score_segment_metrics(data, score_col, lower_threshold=None, upper_threshold=None):
    """计算分数区间内人群指标；高分高风险时区间为 (lower, upper]。"""
    require_columns(data, [score_col], context='calc_score_segment_metrics')
    score = pd.to_numeric(data[score_col], errors='coerce')
    mask = score.notna()
    if lower_threshold is not None:
        mask &= score.gt(lower_threshold) if HIGH_SCORE_HIGH_RISK else score.lt(lower_threshold)
    if upper_threshold is not None:
        mask &= score.le(upper_threshold) if HIGH_SCORE_HIGH_RISK else score.ge(upper_threshold)

    segment = data.loc[mask].copy()
    total_n = len(data)
    total_principal = data['principal'].fillna(0).sum()

    m1_mature = segment['duedate_1m_30'].isin([0, 1]).sum()
    m1_bad = segment['duedate_1m_30'].eq(1).sum()
    m3_mature = segment['duedate_3m_30'].isin([0, 1]).sum()
    m3_bad = segment['duedate_3m_30'].eq(1).sum()
    m1_amt_exposure = segment.loc[segment['dpd_days_ever_mob1'].notna(), 'principal'].fillna(0).sum()
    m1_amt_bad = segment.loc[
        segment['dpd_days_ever_mob1'].notna() & segment['dpd_days_ever_mob1'].ge(30),
        'estimate_principal_remaining_mob1',
    ].fillna(0).sum()
    m3_amt_exposure = segment.loc[segment['dpd_days_ever_mob3'].notna(), 'principal'].fillna(0).sum()
    m3_amt_bad = segment.loc[
        segment['dpd_days_ever_mob3'].notna() & segment['dpd_days_ever_mob3'].ge(30),
        'estimate_principal_remaining_mob3',
    ].fillna(0).sum()

    return pd.Series({
        'n': len(segment),
        'sample_pct': safe_div(len(segment), total_n),
        'principal': segment['principal'].fillna(0).sum(),
        'principal_pct': safe_div(segment['principal'].fillna(0).sum(), total_principal),
        '1m30p_cnt_mature': m1_mature,
        '1m30p_cnt_bad': m1_bad,
        '1m30p_cnt_bad_rate': safe_div(m1_bad, m1_mature),
        '3m30p_cnt_mature': m3_mature,
        '3m30p_cnt_bad': m3_bad,
        '3m30p_cnt_bad_rate': safe_div(m3_bad, m3_mature),
        '1m30p_amt_exposure': m1_amt_exposure,
        '1m30p_amt_bad_rate': safe_div(m1_amt_bad, m1_amt_exposure),
        '3m30p_amt_exposure': m3_amt_exposure,
        '3m30p_amt_bad_rate': safe_div(m3_amt_bad, m3_amt_exposure),
    })


def make_strategy_plan(curve, configs):
    """按配置生成三段式策略阈值。"""
    rows = []
    for cfg in configs:
        auto_row = select_threshold_under_constraints(curve, cfg['auto_constraints'])
        accept_row = select_threshold_under_constraints(curve, cfg['accept_constraints'])
        if auto_row is None or accept_row is None:
            rows.append({
                'strategy_name': cfg['strategy_name'],
                'objective': cfg['objective'],
                'status': '无满足约束的阈值',
            })
            continue

        if accept_row['threshold'] < auto_row['threshold']:
            accept_row = auto_row

        rows.append({
            'strategy_name': cfg['strategy_name'],
            'objective': cfg['objective'],
            'status': 'OK',
            'auto_pass_threshold': auto_row['threshold'],
            'auto_pass_bin': auto_row[FINAL_BIN_COL],
            'reject_threshold': accept_row['threshold'],
            'manual_review_upper_bin': accept_row[FINAL_BIN_COL],
            'auto_pass_rate': auto_row['cum_pass_rate'],
            'accepted_rate': accept_row['cum_pass_rate'],
            'manual_review_rate': accept_row['cum_pass_rate'] - auto_row['cum_pass_rate'],
            'reject_rate': 1 - accept_row['cum_pass_rate'],
            'accepted_1m30p_cnt_bad_rate': accept_row['cum_1m30p_cnt_bad_rate'],
            'accepted_3m30p_cnt_bad_rate': accept_row['cum_3m30p_cnt_bad_rate'],
            'accepted_1m30p_amt_bad_rate': accept_row['cum_1m30p_amt_bad_rate'],
            'accepted_3m30p_amt_bad_rate': accept_row['cum_3m30p_amt_bad_rate'],
            'last_accepted_marginal_1m30p_cnt_bad_rate': accept_row['marginal_1m30p_cnt_bad_rate'],
            'last_accepted_marginal_3m30p_cnt_bad_rate': accept_row['marginal_3m30p_cnt_bad_rate'],
        })
    return pd.DataFrame(rows)


def make_strategy_segment_report(data, strategy_plan, score_col, sample_group_name):
    """输出每个方案的自动通过、人工审核、拒绝三段指标。"""
    rows = []
    for _, strategy in strategy_plan.loc[strategy_plan['status'].eq('OK')].iterrows():
        auto_threshold = strategy['auto_pass_threshold']
        reject_threshold = strategy['reject_threshold']
        segment_defs = [
            ('自动通过', None, auto_threshold),
            ('人工审核', auto_threshold, reject_threshold),
            ('拒绝', reject_threshold, None),
        ]
        for decision, lower, upper in segment_defs:
            metrics = calc_score_segment_metrics(data, score_col, lower_threshold=lower, upper_threshold=upper)
            row = metrics.to_dict()
            row.update({
                'sample_group': sample_group_name,
                'strategy_name': strategy['strategy_name'],
                'decision': decision,
                'lower_threshold_exclusive': lower,
                'upper_threshold_inclusive': upper,
            })
            rows.append(row)
    result = pd.DataFrame(rows)
    first_cols = ['sample_group', 'strategy_name', 'decision', 'lower_threshold_exclusive', 'upper_threshold_inclusive']
    return result[first_cols + [c for c in result.columns if c not in first_cols]]


# ============================================================
# 10.1 生成主模型三套策略方案
# ============================================================

strategy_plan = make_strategy_plan(threshold_curve_final_bins, STRATEGY_CONFIGS)
strategy_segment_train = make_strategy_segment_report(
    train_final_binned,
    strategy_plan,
    score_col=SCORE_COL,
    sample_group_name='train',
)
strategy_segment_oot = make_strategy_segment_report(
    oot_final_binned,
    strategy_plan,
    score_col=SCORE_COL,
    sample_group_name='oot',
)
strategy_segment_report = pd.concat([strategy_segment_train, strategy_segment_oot], ignore_index=True)

strategy_segment_preview_cols = [
    'sample_group', 'strategy_name', 'decision',
    'lower_threshold_exclusive', 'upper_threshold_inclusive',
    'n', 'sample_pct',
    '1m30p_cnt_mature', '1m30p_cnt_bad_rate',
    '3m30p_cnt_mature', '3m30p_cnt_bad_rate',
    '1m30p_amt_bad_rate', '3m30p_amt_bad_rate',
]

rec = strategy_plan.loc[strategy_plan['strategy_name'].eq('平衡方案')]
print(f'10/14 策略方案生成完成，推荐平衡方案：自动通过 {rec["auto_pass_rate"].iloc[0]:.1%}，人工审核 {rec["manual_review_rate"].iloc[0]:.1%}')


# ============================================================
# 10.2 策略推荐结论
# ============================================================

strategy_recommendation = pd.Series({
    'recommended_strategy': '平衡方案',
    'reason': '相较保守方案显著提升自动通过率；相较增长方案保留更充足的拒绝尾部，边际3M30风险控制更稳',
    'auto_pass_rule': 'score_mlt <= 0.097461',
    'manual_review_rule': '0.097461 < score_mlt <= 0.189375',
    'reject_rule': 'score_mlt > 0.189375',
    'train_auto_pass_rate': strategy_plan.loc[strategy_plan['strategy_name'].eq('平衡方案'), 'auto_pass_rate'].iloc[0],
    'train_manual_review_rate': strategy_plan.loc[strategy_plan['strategy_name'].eq('平衡方案'), 'manual_review_rate'].iloc[0],
    'train_reject_rate': strategy_plan.loc[strategy_plan['strategy_name'].eq('平衡方案'), 'reject_rate'].iloc[0],
    'train_accepted_3m30p_cnt_bad_rate': strategy_plan.loc[strategy_plan['strategy_name'].eq('平衡方案'), 'accepted_3m30p_cnt_bad_rate'].iloc[0],
    'oot_auto_pass_3m30p_cnt_bad_rate': strategy_segment_report.loc[
        strategy_segment_report['sample_group'].eq('oot')
        & strategy_segment_report['strategy_name'].eq('平衡方案')
        & strategy_segment_report['decision'].eq('自动通过'),
        '3m30p_cnt_bad_rate',
    ].iloc[0],
    'oot_manual_review_3m30p_cnt_bad_rate': strategy_segment_report.loc[
        strategy_segment_report['sample_group'].eq('oot')
        & strategy_segment_report['strategy_name'].eq('平衡方案')
        & strategy_segment_report['decision'].eq('人工审核'),
        '3m30p_cnt_bad_rate',
    ].iloc[0],
    'note': '当前方案仍需结合人工审核产能、收益/EL、以及OOT 3M30金额口径成熟后复核',
}, name='value')


# ============================================================
# 11.1 相邻箱显著性检验
# ============================================================

def adjacent_proportion_tests(bin_stats, prefix):
    """对相邻箱的笔数 bad rate 做两比例 z-test 和卡方检验。"""
    bad_col = f'{prefix}_cnt_bad'
    good_col = f'{prefix}_cnt_good'
    mature_col = f'{prefix}_cnt_mature'
    rate_col = f'{prefix}_cnt_bad_rate'
    require_columns(bin_stats, ['bin_order', bad_col, good_col, mature_col, rate_col], context='adjacent_proportion_tests')

    ordered = bin_stats.sort_values('bin_order').reset_index(drop=True)
    rows = []
    for i in range(1, len(ordered)):
        prev = ordered.iloc[i - 1]
        curr = ordered.iloc[i]
        bad1, n1 = prev[bad_col], prev[mature_col]
        bad2, n2 = curr[bad_col], curr[mature_col]
        rate1, rate2 = prev[rate_col], curr[rate_col]
        diff = rate2 - rate1

        if n1 > 0 and n2 > 0:
            pooled_rate = safe_div(bad1 + bad2, n1 + n2)
            se = np.sqrt(pooled_rate * (1 - pooled_rate) * (1 / n1 + 1 / n2)) if pooled_rate == pooled_rate else np.nan
            z_stat = safe_div(diff, se) if se and se > 0 else np.nan
            z_p_value = 2 * (1 - norm.cdf(abs(z_stat))) if z_stat == z_stat else np.nan
            try:
                chi2_stat, chi2_p_value, _, _ = chi2_contingency(
                    [[bad1, prev[good_col]], [bad2, curr[good_col]]],
                    correction=False,
                )
            except ValueError:
                chi2_stat, chi2_p_value = np.nan, np.nan
        else:
            z_stat, z_p_value, chi2_stat, chi2_p_value = np.nan, np.nan, np.nan, np.nan

        rows.append({
            'metric': prefix,
            'left_bin_order': prev['bin_order'],
            'right_bin_order': curr['bin_order'],
            'left_rate': rate1,
            'right_rate': rate2,
            'rate_diff': diff,
            'direction_ok': diff >= 0,
            'z_stat': z_stat,
            'z_p_value': z_p_value,
            'chi2_stat': chi2_stat,
            'chi2_p_value': chi2_p_value,
            'significant_5pct': bool(z_p_value < 0.05) if z_p_value == z_p_value else False,
            'merge_hint': '建议合并' if (diff < 0 or not (z_p_value < 0.05)) else '可保留',
        })
    return pd.DataFrame(rows)


adjacent_sig_1m30p = adjacent_proportion_tests(bin_stats_20, '1m30p')
adjacent_sig_3m30p = adjacent_proportion_tests(bin_stats_20, '3m30p')
adjacent_sig_tests = pd.concat([adjacent_sig_1m30p, adjacent_sig_3m30p], ignore_index=True)

print(f'11/14 相邻箱显著性检验完成，建议合并 {int(adjacent_sig_tests["merge_hint"].eq("建议合并").sum())} 对')


# ============================================================
# 11.2 6/7/8/9 档候选合箱方案比较
# ============================================================

CANDIDATE_FINAL_BIN_RANGES = {
    '6档方案': [(1, 2), (3, 5), (6, 8), (9, 14), (15, 18), (19, 20)],
    '7档方案': [(1, 2), (3, 5), (6, 8), (9, 11), (12, 14), (15, 18), (19, 20)],
    '8档方案': FINAL_BIN_RANGES,
    '9档方案': [(1, 2), (3, 5), (6, 8), (9, 11), (12, 14), (15, 16), (17, 18), (19, 19), (20, 20)],
}


def evaluate_merge_candidate(candidate_name, ranges):
    target_col = f"{candidate_name}_final_bin"
    merge_map = build_adjacent_merge_map(ranges, source_bin_col=BIN20_COL, target_bin_col=target_col)
    edge_table = build_final_edge_table(score_mlt_bin_edges_df, merge_map, source_bin_col=BIN20_COL, target_bin_col=target_col)
    train_candidate = apply_merge_map(train_binned_20, merge_map, source_bin_col=BIN20_COL, target_bin_col=target_col)
    oot_candidate = apply_merge_map(oot_binned_20, merge_map, source_bin_col=BIN20_COL, target_bin_col=target_col)
    df_candidate = apply_merge_map(df_binned_20, merge_map, source_bin_col=BIN20_COL, target_bin_col=target_col)

    train_stats = calc_bin_stats(train_candidate, bin_col=target_col, score_col=SCORE_COL)
    oot_stats = calc_bin_stats(oot_candidate, bin_col=target_col, score_col=SCORE_COL)
    train_mono = check_monotonicity(train_stats, ['1m30p_cnt_bad_rate', '3m30p_cnt_bad_rate', '1m30p_amt_bad_rate', '3m30p_amt_bad_rate'])
    oot_mono = check_monotonicity(oot_stats, ['1m30p_cnt_bad_rate', '3m30p_cnt_bad_rate', '1m30p_amt_bad_rate'])
    psi = calc_population_psi(
        train_candidate,
        oot_candidate,
        bin_col=target_col,
        base_bins=merge_map[[target_col, 'final_bin_order']].drop_duplicates().rename(columns={target_col: target_col}),
    )

    monthly_stats = calc_group_bin_stats(df_candidate, group_col='application_month', bin_col=target_col, score_col=SCORE_COL)
    original_final_col = globals().get('FINAL_BIN_COL')
    globals()['FINAL_BIN_COL'] = target_col
    try:
        monthly_summary = build_monthly_stability_summary(monthly_stats)
    finally:
        globals()['FINAL_BIN_COL'] = original_final_col

    summary = {
        'candidate_name': candidate_name,
        'bin_cnt': len(ranges),
        'min_train_n': train_stats['n'].min(),
        'min_train_1m_mature': train_stats['1m30p_cnt_mature'].min(),
        'min_train_3m_mature': train_stats['3m30p_cnt_mature'].min(),
        'min_train_1m_bad': train_stats['1m30p_cnt_bad'].min(),
        'min_train_3m_bad': train_stats['3m30p_cnt_bad'].min(),
        'train_violation_cnt': int(train_mono['violation_cnt'].sum()),
        'oot_1m_cnt_violation_cnt': int(oot_mono.loc[oot_mono['metric'].eq('1m30p_cnt_bad_rate'), 'violation_cnt'].iloc[0]),
        'oot_3m_cnt_violation_cnt': int(oot_mono.loc[oot_mono['metric'].eq('3m30p_cnt_bad_rate'), 'violation_cnt'].iloc[0]),
        'psi_total': psi['psi_total'].iloc[0],
        'months_with_1m_cnt_violation': int(monthly_summary['1m30p_cnt_bad_rate_violation_cnt'].gt(0).sum()),
        'months_with_3m_cnt_violation': int(monthly_summary['3m30p_cnt_bad_rate_violation_cnt'].gt(0).sum()),
        'train_3m_bad_rate_first_bin': train_stats['3m30p_cnt_bad_rate'].iloc[0],
        'train_3m_bad_rate_last_bin': train_stats['3m30p_cnt_bad_rate'].iloc[-1],
        'oot_3m_bad_rate_first_bin': oot_stats['3m30p_cnt_bad_rate'].iloc[0],
        'oot_3m_bad_rate_last_bin': oot_stats['3m30p_cnt_bad_rate'].iloc[-1],
    }
    return summary, {
        'merge_map': merge_map,
        'edge_table': edge_table,
        'train_stats': train_stats,
        'oot_stats': oot_stats,
        'train_monotonicity': train_mono,
        'oot_monotonicity': oot_mono,
        'psi': psi,
        'monthly_summary': monthly_summary,
    }


candidate_merge_details = {}
candidate_merge_rows = []
for candidate_name, ranges in CANDIDATE_FINAL_BIN_RANGES.items():
    summary, details = evaluate_merge_candidate(candidate_name, ranges)
    candidate_merge_rows.append(summary)
    candidate_merge_details[candidate_name] = details

candidate_merge_compare = pd.DataFrame(candidate_merge_rows)
candidate_merge_compare['candidate_score'] = (
    candidate_merge_compare['bin_cnt']
    - candidate_merge_compare['train_violation_cnt'] * 10
    - candidate_merge_compare['oot_3m_cnt_violation_cnt'] * 5
    - candidate_merge_compare['months_with_3m_cnt_violation'] * 0.5
    - candidate_merge_compare['psi_total'] * 100
)
candidate_merge_compare = candidate_merge_compare.sort_values('candidate_score', ascending=False).reset_index(drop=True)

print(f'12/14 候选合箱方案比较完成，推荐 {candidate_merge_compare.iloc[0]["candidate_name"]}')


# ============================================================
# 11.3 边界取整敏感性分析
# ============================================================

def build_rounded_final_edges(final_edges, decimals):
    """将最终箱内部边界按小数位取整，并保留首尾无穷边界。"""
    require_columns(final_edges, ['final_bin_order', FINAL_BIN_COL, 'score_left', 'score_right', 'merged_from'], context='build_rounded_final_edges')
    rounded = final_edges.copy().sort_values('final_bin_order').reset_index(drop=True)
    inner_right = rounded['score_right'].replace(np.inf, np.nan).dropna().round(decimals)
    if not inner_right.is_monotonic_increasing or inner_right.duplicated().any():
        raise ValueError(f"取整到 {decimals} 位后边界不再严格递增")
    edges = [-np.inf] + inner_right.tolist() + [np.inf]
    rounded['score_left_rounded'] = edges[:-1]
    rounded['score_right_rounded'] = edges[1:]
    rounded['round_decimals'] = decimals
    return rounded


def apply_rounded_final_edges(data, rounded_edges, score_col, bin_col):
    labels = rounded_edges[FINAL_BIN_COL].tolist()
    edges = [rounded_edges['score_left_rounded'].iloc[0]] + rounded_edges['score_right_rounded'].tolist()
    out = data.copy()
    out[bin_col] = pd.cut(
        pd.to_numeric(out[score_col], errors='coerce'),
        bins=edges,
        labels=labels,
        include_lowest=True,
        right=True,
    ).astype('string')
    order_map = dict(zip(labels, rounded_edges['final_bin_order']))
    out['bin_order'] = out[bin_col].map(order_map).astype('Int64')
    return out


def evaluate_rounded_boundaries(decimals):
    rounded_edges = build_rounded_final_edges(score_mlt_final_edges_df, decimals=decimals)
    rounded_col = f'score_mlt_final_bin_round_{decimals}'
    train_rounded = apply_rounded_final_edges(train_df, rounded_edges, SCORE_COL, rounded_col)
    oot_rounded = apply_rounded_final_edges(oot_df, rounded_edges, SCORE_COL, rounded_col)
    train_stats = calc_bin_stats(train_rounded, bin_col=rounded_col, score_col=SCORE_COL)
    oot_stats = calc_bin_stats(oot_rounded, bin_col=rounded_col, score_col=SCORE_COL)
    train_mono = check_monotonicity(train_stats, ['1m30p_cnt_bad_rate', '3m30p_cnt_bad_rate', '1m30p_amt_bad_rate', '3m30p_amt_bad_rate'])
    oot_mono = check_monotonicity(oot_stats, ['1m30p_cnt_bad_rate', '3m30p_cnt_bad_rate', '1m30p_amt_bad_rate'])

    current_bins = train_final_binned[[SCORE_COL, FINAL_BIN_COL]].copy()
    rounded_bins = train_rounded[[SCORE_COL, rounded_col]].copy()
    shifted_n = int((current_bins[FINAL_BIN_COL].astype('string').reset_index(drop=True) != rounded_bins[rounded_col].astype('string').reset_index(drop=True)).sum())

    train_compare = bin_stats_final[['bin_order', 'n', '3m30p_cnt_bad_rate']].merge(
        train_stats[['bin_order', 'n', '3m30p_cnt_bad_rate']],
        on='bin_order',
        suffixes=('_current', '_rounded'),
    )
    train_compare['n_delta'] = train_compare['n_rounded'] - train_compare['n_current']
    train_compare['3m30p_rate_delta'] = train_compare['3m30p_cnt_bad_rate_rounded'] - train_compare['3m30p_cnt_bad_rate_current']

    return {
        'round_decimals': decimals,
        'shifted_n': shifted_n,
        'shifted_pct': safe_div(shifted_n, len(train_df)),
        'max_abs_bin_n_delta': train_compare['n_delta'].abs().max(),
        'max_abs_3m30p_rate_delta': train_compare['3m30p_rate_delta'].abs().max(),
        'train_violation_cnt': int(train_mono['violation_cnt'].sum()),
        'oot_1m_cnt_violation_cnt': int(oot_mono.loc[oot_mono['metric'].eq('1m30p_cnt_bad_rate'), 'violation_cnt'].iloc[0]),
        'oot_3m_cnt_violation_cnt': int(oot_mono.loc[oot_mono['metric'].eq('3m30p_cnt_bad_rate'), 'violation_cnt'].iloc[0]),
    }, rounded_edges, train_compare


rounded_boundary_details = {}
rounded_boundary_rows = []
for decimals in [4, 3]:
    summary, rounded_edges, train_compare = evaluate_rounded_boundaries(decimals)
    rounded_boundary_rows.append(summary)
    rounded_boundary_details[decimals] = {
        'rounded_edges': rounded_edges,
        'train_compare': train_compare,
    }

rounded_boundary_compare = pd.DataFrame(rounded_boundary_rows)

print(f'13/14 边界取整分析完成，4位小数迁移 {rounded_boundary_rows[0]["shifted_pct"]:.2%} 样本')


# ============================================================
# 11.4 分箱优化结论
# ============================================================

binning_optimization_decision = pd.Series({
    'primary_candidate': '8档方案',
    'backup_candidate': '7档方案',
    'primary_reason': '8档方案 train 四类风险率全部单调，OOT 3M30笔数单调，PSI 很低，同时保留更好的高风险尾部分辨率',
    'backup_reason': '7档方案 OOT 1M30/3M30 都无倒挂，月度1M30倒挂略少；若偏好更简洁稳健，可作为备选',
    'significance_takeaway': '相邻显著性检验支持合并 B05-B07、B09-B11、B13-B14、B17-B18 等相邻箱；当前8档合箱方向与检验结论一致',
    'boundary_recommendation': '上线边界建议使用4位小数；3位小数会迁移约7.67%训练样本，并引入 OOT 3M30 倒挂',
    'recommended_edges': 'G01<=0.0378, G02<=0.0595, G03<=0.0777, G04<=0.0975, G05<=0.1413, G06<=0.1894, G07<=0.2469, G08>0.2469',
    'need_recheck': '边界取整后应复跑策略方案；若业务更看重简洁，可对7档方案单独生成阈值曲线与策略方案',
}, name='value')


# ============================================================
# 12. 4位小数边界复算：分箱、验证、阈值和策略
# ============================================================

score_mlt_final_edges_rounded4_df = rounded_boundary_details[4]['rounded_edges'].copy()
ROUNDED4_BIN_COL = 'score_mlt_final_bin_rounded4'

train_final_binned_rounded4 = apply_rounded_final_edges(
    train_df,
    score_mlt_final_edges_rounded4_df,
    SCORE_COL,
    ROUNDED4_BIN_COL,
)
oot_final_binned_rounded4 = apply_rounded_final_edges(
    oot_df,
    score_mlt_final_edges_rounded4_df,
    SCORE_COL,
    ROUNDED4_BIN_COL,
)
df_final_binned_rounded4 = apply_rounded_final_edges(
    df,
    score_mlt_final_edges_rounded4_df,
    SCORE_COL,
    ROUNDED4_BIN_COL,
)

bin_stats_final_rounded4 = calc_bin_stats(
    train_final_binned_rounded4,
    bin_col=ROUNDED4_BIN_COL,
    score_col=SCORE_COL,
)
oot_bin_stats_final_rounded4 = calc_bin_stats(
    oot_final_binned_rounded4,
    bin_col=ROUNDED4_BIN_COL,
    score_col=SCORE_COL,
)

rounded4_train_monotonicity_check = check_monotonicity(
    bin_stats_final_rounded4,
    ['1m30p_cnt_bad_rate', '3m30p_cnt_bad_rate', '1m30p_amt_bad_rate', '3m30p_amt_bad_rate'],
)
rounded4_oot_monotonicity_check = check_monotonicity(
    oot_bin_stats_final_rounded4,
    ['1m30p_cnt_bad_rate', '3m30p_cnt_bad_rate', '1m30p_amt_bad_rate'],
)

rounded4_bin_compare = bin_stats_final[[
    'bin_order', FINAL_BIN_COL, 'n', 'sample_pct',
    '1m30p_cnt_bad_rate', '3m30p_cnt_bad_rate',
    '1m30p_amt_bad_rate', '3m30p_amt_bad_rate',
]].merge(
    bin_stats_final_rounded4[[
        'bin_order', ROUNDED4_BIN_COL, 'n', 'sample_pct',
        '1m30p_cnt_bad_rate', '3m30p_cnt_bad_rate',
        '1m30p_amt_bad_rate', '3m30p_amt_bad_rate',
    ]],
    on='bin_order',
    suffixes=('_exact', '_rounded4'),
)
for col in ['n', 'sample_pct', '1m30p_cnt_bad_rate', '3m30p_cnt_bad_rate', '1m30p_amt_bad_rate', '3m30p_amt_bad_rate']:
    rounded4_bin_compare[f'{col}_delta'] = rounded4_bin_compare[f'{col}_rounded4'] - rounded4_bin_compare[f'{col}_exact']

rounded4_threshold_curve_final_bins = calc_threshold_curve(
    train_final_binned_rounded4,
    score_col=SCORE_COL,
    thresholds=score_mlt_final_edges_rounded4_df['score_right_rounded'].replace(np.inf, train_final_binned_rounded4[SCORE_COL].max()),
)
rounded4_threshold_curve_final_bins = rounded4_threshold_curve_final_bins.merge(
    score_mlt_final_edges_rounded4_df[[FINAL_BIN_COL, 'final_bin_order', 'score_right_rounded', 'merged_from']],
    left_on='threshold',
    right_on='score_right_rounded',
    how='left',
)
rounded4_threshold_curve_final_bins = rounded4_threshold_curve_final_bins.rename(columns={FINAL_BIN_COL: ROUNDED4_BIN_COL})

strategy_plan_rounded4 = make_strategy_plan(
    rounded4_threshold_curve_final_bins.rename(columns={ROUNDED4_BIN_COL: FINAL_BIN_COL}),
    STRATEGY_CONFIGS,
)
strategy_segment_train_rounded4 = make_strategy_segment_report(
    train_final_binned_rounded4,
    strategy_plan_rounded4,
    score_col=SCORE_COL,
    sample_group_name='train_rounded4',
)
strategy_segment_oot_rounded4 = make_strategy_segment_report(
    oot_final_binned_rounded4,
    strategy_plan_rounded4,
    score_col=SCORE_COL,
    sample_group_name='oot_rounded4',
)
strategy_segment_report_rounded4 = pd.concat([strategy_segment_train_rounded4, strategy_segment_oot_rounded4], ignore_index=True)

strategy_plan_compare_rounded4 = strategy_plan[[
    'strategy_name', 'auto_pass_threshold', 'reject_threshold',
    'auto_pass_rate', 'manual_review_rate', 'reject_rate',
    'accepted_1m30p_cnt_bad_rate', 'accepted_3m30p_cnt_bad_rate',
]].merge(
    strategy_plan_rounded4[[
        'strategy_name', 'auto_pass_threshold', 'reject_threshold',
        'auto_pass_rate', 'manual_review_rate', 'reject_rate',
        'accepted_1m30p_cnt_bad_rate', 'accepted_3m30p_cnt_bad_rate',
    ]],
    on='strategy_name',
    suffixes=('_exact', '_rounded4'),
)
for col in ['auto_pass_threshold', 'reject_threshold', 'auto_pass_rate', 'manual_review_rate', 'reject_rate', 'accepted_1m30p_cnt_bad_rate', 'accepted_3m30p_cnt_bad_rate']:
    strategy_plan_compare_rounded4[f'{col}_delta'] = strategy_plan_compare_rounded4[f'{col}_rounded4'] - strategy_plan_compare_rounded4[f'{col}_exact']

rounded4_recalc_decision = pd.Series({
    'rounded4_train_monotonic': bool(rounded4_train_monotonicity_check['is_monotonic_non_decreasing'].all()),
    'rounded4_oot_3m30p_cnt_monotonic': bool(rounded4_oot_monotonicity_check.loc[rounded4_oot_monotonicity_check['metric'].eq('3m30p_cnt_bad_rate'), 'is_monotonic_non_decreasing'].iloc[0]),
    'max_abs_train_bin_n_delta': rounded4_bin_compare['n_delta'].abs().max(),
    'max_abs_train_3m30p_rate_delta': rounded4_bin_compare['3m30p_cnt_bad_rate_delta'].abs().max(),
    'recommended_auto_pass_rule': 'score_mlt <= 0.0975',
    'recommended_manual_review_rule': '0.0975 < score_mlt <= 0.1894',
    'recommended_reject_rule': 'score_mlt > 0.1894',
    'recommendation': '4位小数边界复算影响很小，可作为上线配置版本；策略推荐继续使用平衡方案',
}, name='value')

print(f'14/14 4位小数复算完成，最大箱样本变化 {rounded4_recalc_decision["max_abs_train_bin_n_delta"]:.0f}')


# ============================================================
# 13. 阈值敏感性分析：人工审核产能 + 风险上限扫描
# ============================================================

THRESHOLD_SENSITIVITY_CONFIG = {
    'manual_review_caps': [0.10, 0.15, 0.20, 0.25, 0.30],
    'accepted_3m30p_caps': [0.06, 0.07, 0.08, 0.09],
    'accepted_1m30p_cap': 0.015,
    'accepted_marginal_3m30p_cap': 0.22,
    'auto_cap_ratio': 0.75,
}


def search_threshold_pair(
    curve,
    max_manual_review_rate,
    max_accepted_3m30p_cnt_bad_rate,
    max_accepted_1m30p_cnt_bad_rate=0.015,
    max_accepted_marginal_3m30p_cnt_bad_rate=0.22,
    auto_cap_ratio=0.75,
    bin_col=None,
):
    """在自动通过阈值和接纳上限阈值之间搜索最优三段式策略。"""
    required = [
        'threshold_order', 'threshold', 'cum_pass_rate',
        'cum_1m30p_cnt_bad_rate', 'cum_3m30p_cnt_bad_rate',
        'marginal_3m30p_cnt_bad_rate',
    ]
    require_columns(curve, required, context='search_threshold_pair')
    work = curve.copy().sort_values('threshold_order').reset_index(drop=True)

    auto_1m_cap = max_accepted_1m30p_cnt_bad_rate * auto_cap_ratio
    auto_3m_cap = max_accepted_3m30p_cnt_bad_rate * auto_cap_ratio
    auto_marginal_3m_cap = max_accepted_marginal_3m30p_cnt_bad_rate * auto_cap_ratio

    accept_candidates = work.loc[
        work['cum_1m30p_cnt_bad_rate'].le(max_accepted_1m30p_cnt_bad_rate)
        & work['cum_3m30p_cnt_bad_rate'].le(max_accepted_3m30p_cnt_bad_rate)
        & work['marginal_3m30p_cnt_bad_rate'].le(max_accepted_marginal_3m30p_cnt_bad_rate)
    ].copy()
    if accept_candidates.empty:
        return None

    rows = []
    for _, accept in accept_candidates.iterrows():
        min_auto_pass_rate = max(0, accept['cum_pass_rate'] - max_manual_review_rate)
        auto_candidates = work.loc[
            work['threshold_order'].le(accept['threshold_order'])
            & work['cum_pass_rate'].ge(min_auto_pass_rate)
            & work['cum_1m30p_cnt_bad_rate'].le(auto_1m_cap)
            & work['cum_3m30p_cnt_bad_rate'].le(auto_3m_cap)
            & work['marginal_3m30p_cnt_bad_rate'].le(auto_marginal_3m_cap)
        ].copy()
        if auto_candidates.empty:
            continue
        auto = auto_candidates.sort_values(['cum_pass_rate', 'threshold_order'], ascending=[False, False]).iloc[0]
        manual_review_rate = accept['cum_pass_rate'] - auto['cum_pass_rate']
        rows.append({
            'max_manual_review_rate': max_manual_review_rate,
            'max_accepted_3m30p_cnt_bad_rate': max_accepted_3m30p_cnt_bad_rate,
            'max_accepted_1m30p_cnt_bad_rate': max_accepted_1m30p_cnt_bad_rate,
            'max_accepted_marginal_3m30p_cnt_bad_rate': max_accepted_marginal_3m30p_cnt_bad_rate,
            'auto_pass_threshold': auto['threshold'],
            'reject_threshold': accept['threshold'],
            'auto_pass_rate': auto['cum_pass_rate'],
            'accepted_rate': accept['cum_pass_rate'],
            'manual_review_rate': manual_review_rate,
            'reject_rate': 1 - accept['cum_pass_rate'],
            'auto_1m30p_cnt_bad_rate': auto['cum_1m30p_cnt_bad_rate'],
            'auto_3m30p_cnt_bad_rate': auto['cum_3m30p_cnt_bad_rate'],
            'accepted_1m30p_cnt_bad_rate': accept['cum_1m30p_cnt_bad_rate'],
            'accepted_3m30p_cnt_bad_rate': accept['cum_3m30p_cnt_bad_rate'],
            'last_accepted_marginal_3m30p_cnt_bad_rate': accept['marginal_3m30p_cnt_bad_rate'],
            'auto_threshold_order': auto['threshold_order'],
            'accept_threshold_order': accept['threshold_order'],
        })
        if bin_col is not None and bin_col in work.columns:
            rows[-1]['auto_pass_bin'] = auto[bin_col]
            rows[-1]['manual_review_upper_bin'] = accept[bin_col]

    if not rows:
        return None
    result = pd.DataFrame(rows)
    return result.sort_values(
        ['accepted_rate', 'auto_pass_rate', 'manual_review_rate'],
        ascending=[False, False, True],
    ).iloc[0]


def scan_threshold_sensitivity(curve, config, bin_col=None, curve_name='curve'):
    """扫描人工审核产能和接纳风险上限，输出最优阈值矩阵明细。"""
    rows = []
    for manual_cap in config['manual_review_caps']:
        for accepted_3m_cap in config['accepted_3m30p_caps']:
            best = search_threshold_pair(
                curve,
                max_manual_review_rate=manual_cap,
                max_accepted_3m30p_cnt_bad_rate=accepted_3m_cap,
                max_accepted_1m30p_cnt_bad_rate=config['accepted_1m30p_cap'],
                max_accepted_marginal_3m30p_cnt_bad_rate=config['accepted_marginal_3m30p_cap'],
                auto_cap_ratio=config['auto_cap_ratio'],
                bin_col=bin_col,
            )
            if best is None:
                rows.append({
                    'curve_name': curve_name,
                    'max_manual_review_rate': manual_cap,
                    'max_accepted_3m30p_cnt_bad_rate': accepted_3m_cap,
                    'max_accepted_1m30p_cnt_bad_rate': config['accepted_1m30p_cap'],
                    'max_accepted_marginal_3m30p_cnt_bad_rate': config['accepted_marginal_3m30p_cap'],
                    'status': '无可行方案',
                })
            else:
                row = best.to_dict()
                row['curve_name'] = curve_name
                row['status'] = 'OK'
                rows.append(row)
    result = pd.DataFrame(rows)
    first_cols = ['curve_name', 'status', 'max_manual_review_rate', 'max_accepted_3m30p_cnt_bad_rate']
    return result[first_cols + [c for c in result.columns if c not in first_cols]].reset_index(drop=True)


def build_sensitivity_matrix(scan_result):
    """把扫描明细整理成便于业务阅读的矩阵。"""
    work = scan_result.copy()
    def fmt(row):
        if row['status'] != 'OK':
            return '不可行'
        return (
            f"接纳{row['accepted_rate']:.1%} / "
            f"自通{row['auto_pass_rate']:.1%} / "
            f"审核{row['manual_review_rate']:.1%} / "
            f"3M{row['accepted_3m30p_cnt_bad_rate']:.1%}"
        )
    work['方案摘要'] = work.apply(fmt, axis=1)
    return work.pivot(
        index='max_manual_review_rate',
        columns='max_accepted_3m30p_cnt_bad_rate',
        values='方案摘要',
    ).reset_index()


# ============================================================
# 13.1 执行阈值敏感性扫描
# ============================================================

rounded4_curve_for_sensitivity = rounded4_threshold_curve_final_bins.rename(columns={ROUNDED4_BIN_COL: FINAL_BIN_COL})
threshold_sensitivity_rounded4 = scan_threshold_sensitivity(
    rounded4_curve_for_sensitivity,
    THRESHOLD_SENSITIVITY_CONFIG,
    bin_col=FINAL_BIN_COL,
    curve_name='rounded4_final_bins',
)
threshold_sensitivity_quantile = scan_threshold_sensitivity(
    threshold_curve_quantile,
    THRESHOLD_SENSITIVITY_CONFIG,
    bin_col=None,
    curve_name='quantile_curve',
)

threshold_sensitivity_matrix_rounded4 = build_sensitivity_matrix(threshold_sensitivity_rounded4)

threshold_sensitivity_final_vs_quantile = threshold_sensitivity_rounded4.merge(
    threshold_sensitivity_quantile,
    on=['max_manual_review_rate', 'max_accepted_3m30p_cnt_bad_rate'],
    suffixes=('_rounded4', '_quantile'),
)
threshold_sensitivity_final_vs_quantile['accepted_rate_gain_quantile'] = (
    threshold_sensitivity_final_vs_quantile['accepted_rate_quantile']
    - threshold_sensitivity_final_vs_quantile['accepted_rate_rounded4']
)
threshold_sensitivity_final_vs_quantile['auto_rate_gain_quantile'] = (
    threshold_sensitivity_final_vs_quantile['auto_pass_rate_quantile']
    - threshold_sensitivity_final_vs_quantile['auto_pass_rate_rounded4']
)

threshold_sensitivity_recommended_row = threshold_sensitivity_rounded4.loc[
    threshold_sensitivity_rounded4['status'].eq('OK')
    & threshold_sensitivity_rounded4['max_manual_review_rate'].eq(0.25)
    & threshold_sensitivity_rounded4['max_accepted_3m30p_cnt_bad_rate'].eq(0.08)
].iloc[0]

threshold_sensitivity_decision = pd.Series({
    'recommended_capacity_assumption': '人工审核产能按25%以内约束',
    'recommended_3m30p_cap': '接纳人群3M30+不超过8%',
    'recommended_auto_pass_rule': f"score_mlt <= {threshold_sensitivity_recommended_row['auto_pass_threshold']:.4f}",
    'recommended_manual_review_rule': f"{threshold_sensitivity_recommended_row['auto_pass_threshold']:.4f} < score_mlt <= {threshold_sensitivity_recommended_row['reject_threshold']:.4f}",
    'recommended_reject_rule': f"score_mlt > {threshold_sensitivity_recommended_row['reject_threshold']:.4f}",
    'accepted_rate': threshold_sensitivity_recommended_row['accepted_rate'],
    'auto_pass_rate': threshold_sensitivity_recommended_row['auto_pass_rate'],
    'manual_review_rate': threshold_sensitivity_recommended_row['manual_review_rate'],
    'reject_rate': threshold_sensitivity_recommended_row['reject_rate'],
    'accepted_3m30p_cnt_bad_rate': threshold_sensitivity_recommended_row['accepted_3m30p_cnt_bad_rate'],
    'note': '若人工产能可放宽到30%，可采用原平衡方案接纳到G06；若产能严格小于25%，建议接纳上限收至G05',
}, name='value')


# ============================================================
# 14. 生成策略报告 Excel（增强可读性版）
# ============================================================

import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
from openpyxl.comments import Comment
from openpyxl.formatting.rule import ColorScaleRule, DataBarRule

OUT_DIR = Path('out')
OUT_DIR.mkdir(exist_ok=True)
REPORT_PATH = OUT_DIR / '策略报告.xlsx'

# ---- 报告主题色 ----
NAVY = '1F4E79'
DARK_BLUE = '17365D'
WHITE = 'FFFFFF'
TEXT_DARK = '253746'
GRAY = '666666'
LIGHT_GRAY = 'F3F6F8'
ALT_ROW = 'F8FAFC'

TITLE_FILL = PatternFill(start_color='D6E4F0', end_color='D6E4F0', fill_type='solid')
TITLE_FONT = Font(name='Microsoft YaHei', bold=True, size=12, color=NAVY)
HEADER_FILL = PatternFill(start_color=NAVY, end_color=NAVY, fill_type='solid')
HEADER_FONT = Font(name='Microsoft YaHei', bold=True, color=WHITE, size=10)
DATA_FONT = Font(name='Microsoft YaHei', size=9, color=TEXT_DARK)
SUBTLE_FONT = Font(name='Microsoft YaHei', size=9, color=GRAY)

RISK_HEADER_FILL = PatternFill(start_color='C65911', end_color='C65911', fill_type='solid')
VOLUME_HEADER_FILL = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
STABILITY_HEADER_FILL = PatternFill(start_color='8064A2', end_color='8064A2', fill_type='solid')
DECISION_HEADER_FILL = PatternFill(start_color='548235', end_color='548235', fill_type='solid')
AMOUNT_HEADER_FILL = PatternFill(start_color='BF9000', end_color='BF9000', fill_type='solid')

PASS_FILL = PatternFill(start_color='E2F0D9', end_color='E2F0D9', fill_type='solid')
REVIEW_FILL = PatternFill(start_color='FFF2CC', end_color='FFF2CC', fill_type='solid')
REJECT_FILL = PatternFill(start_color='F4CCCC', end_color='F4CCCC', fill_type='solid')
WARN_FILL = PatternFill(start_color='FCE4D6', end_color='FCE4D6', fill_type='solid')
ERROR_FILL = PatternFill(start_color='F4CCCC', end_color='F4CCCC', fill_type='solid')

THIN_BORDER = Border(
    left=Side(style='thin', color='D9E2F3'),
    right=Side(style='thin', color='D9E2F3'),
    top=Side(style='thin', color='D9E2F3'),
    bottom=Side(style='thin', color='D9E2F3'),
)
MEDIUM_BOTTOM_BORDER = Border(bottom=Side(style='medium', color=NAVY))

PCT_KEYWORDS = [
    'rate', 'pct', 'share', 'portion', 'accepted', 'pass_rate',
    'review_rate', 'reject_rate', 'shifted_pct', 'gap', 'delta',
]
COUNT_KEYWORDS = [
    '_cnt', 'count', 'rows', 'nunique', 'mature', 'bad_cnt', 'good_cnt',
    'violation_cnt', 'bin_cnt', 'shifted_n', 'marginal_n',
]

# 关键字段说明会写入 Excel 表头批注，方便读者悬停查看。
METRIC_COMMENTS = {
    'n': '当前箱或当前分组内的样本量，口径为 application_id 记录数。',
    'application_id_nunique': '当前分组内去重后的 application_id 数量，用于检查重复样本。',
    'sample_pct': '样本占比 = 当前箱样本量 / 当前分析样本总量。',
    'score_left': '分箱左边界；当前区间规则为 (left, right]。',
    'score_right': '分箱右边界；当前区间规则为 (left, right]。',
    'score_min': '当前箱实际观测到的最小模型分。',
    'score_max': '当前箱实际观测到的最大模型分。',
    '1m30p_cnt_mature': '1M30+ 笔数口径成熟样本量：duedate_1m_30 取值为 0 或 1。',
    '1m30p_cnt_bad': '1M30+ 笔数口径坏样本量：duedate_1m_30 = 1。',
    '1m30p_cnt_bad_rate': '1M30+ 笔数逾期率 = 1M30+坏样本量 / 1M30+成熟样本量。',
    '3m30p_cnt_mature': '3M30+ 笔数口径成熟样本量：duedate_3m_30 取值为 0 或 1。',
    '3m30p_cnt_bad': '3M30+ 笔数口径坏样本量：duedate_3m_30 = 1。',
    '3m30p_cnt_bad_rate': '3M30+ 笔数逾期率 = 3M30+坏样本量 / 3M30+成熟样本量。',
    '1m30p_amt_bad_rate': '1M30+ 金额逾期率 = MOB1逾期剩余本金 / MOB1成熟样本对应本金。',
    '3m30p_amt_bad_rate': '3M30+ 金额逾期率 = MOB3逾期剩余本金 / MOB3成熟样本对应本金。',
    '1m30p_cnt_lift': '当前箱1M30+笔数逾期率 / 全体样本1M30+笔数逾期率。',
    '3m30p_cnt_lift': '当前箱3M30+笔数逾期率 / 全体样本3M30+笔数逾期率。',
    'cum_pass_rate': '从低风险端累计至当前阈值的样本占比。',
    'cum_1m30p_cnt_bad_rate': '从低风险端累计至当前阈值的1M30+笔数逾期率。',
    'cum_3m30p_cnt_bad_rate': '从低风险端累计至当前阈值的3M30+笔数逾期率。',
    'threshold': '策略阈值，模型分不高于该值的样本进入当前累计范围。',
    'marginal_sample_pct': '新增放宽一个分数区间所增加的样本占比。',
    'marginal_1m30p_cnt_bad_rate': '新增边际区间自身的1M30+笔数逾期率，而非累计逾期率。',
    'marginal_3m30p_cnt_bad_rate': '新增边际区间自身的3M30+笔数逾期率，而非累计逾期率。',
    'psi_component': '该风险等级对总体 PSI 的贡献。',
    'psi_total': 'Train 与 OOT 分箱分布的总体 PSI。',
    'auc': '模型排序能力指标；本代码按高分高风险方向计算。',
    'ks': '好坏样本累计分布的最大差异，用于评估模型区分度。',
    'diagnosis_flags': '对样本量、成熟样本、坏样本、倒挂、置信区间和金额口径的综合诊断。',
    'merge_priority_score': '合箱优先级评分，分数越高代表越需要优先检查或合并。',
    'candidate_score': '候选分箱方案综合评分，仅用于方案横向比较，仍需结合业务解释和OOT表现判断。',
    'z_p_value': '相邻箱两比例 Z 检验的 p 值。',
    'chi2_p_value': '相邻箱列联表卡方检验的 p 值。',
    'significant_5pct': '在 5% 显著性水平下，相邻箱风险差异是否显著。',
    'shifted_pct': '边界取整后发生分箱迁移的样本占比。',
    'auto_pass_rate': '自动通过人群占全体样本的比例。',
    'manual_review_rate': '人工审核人群占全体样本的比例。',
    'reject_rate': '拒绝人群占全体样本的比例。',
    'accepted_rate': '自动通过与人工审核合计的人群占比。',
    'accepted_3m30p_cnt_bad_rate': '接纳人群（自动通过+人工审核）的累计3M30+笔数逾期率。',
    'last_accepted_marginal_3m30p_cnt_bad_rate': '最后一个被接纳边际区间自身的3M30+笔数逾期率。',
}


REPORT_GUIDANCE = {
    'strategy_recommendation': {
        'focus': '先看推荐策略、阈值区间、接纳规模、人工审核占比和接纳人群3M30+风险；这是整份报告的决策入口。',
        'logic': '综合最终分箱、Train/OOT验证、边界取整复算和阈值敏感性结果形成推荐结论。',
        'caution': '推荐结论依赖当前人工审核产能和风险上限假设；上线前需由策略、风险与运营共同确认。',
        'key_columns': ['值'],
    },
    'rounded4_recalc': {
        'focus': '确认4位小数上线边界是否改变样本归属、风险表现和三段策略规模。',
        'logic': '将精确边界统一取4位小数后重新分箱、重算指标，再与精确边界结果对比。',
        'caution': '不能只比较阈值数值，应同时检查迁移样本占比及高风险边界附近的样本变化。',
    },
    'recommended_segment': {
        'focus': '分别比较自动通过、人工审核、拒绝三段的规模、成熟样本和1M30+/3M30+风险。',
        'logic': '按推荐方案的两个阈值切分样本，并在Train或OOT内分别汇总三段指标。',
        'caution': 'OOT部分可能受观察期未完全成熟影响；成熟样本量不足时不要直接比较绝对逾期率。',
        'key_columns': ['decision', 'sample_pct', '3m30p_cnt_bad_rate'],
    },
    'final_bins': {
        'focus': '重点检查各风险等级的样本占比、3M30+风险单调性、Lift和累计风险变化。',
        'logic': '先在Train学习20等频箱，再按相邻箱诊断合并为8档；同一边界直接复用于OOT。',
        'caution': '当前模型方向为高分高风险；区间规则为(left, right]，边界值归入右侧箱。',
        'key_columns': ['score_left', 'score_right', 'sample_pct', '3m30p_cnt_bad_rate', 'cum_pass_rate'],
    },
    'threshold_curve': {
        'focus': '观察阈值逐步放宽时，通过率提升与累计3M30+风险、边际3M30+风险的同步变化。',
        'logic': '依次将每个最终风险等级右边界作为阈值，从低风险端向高风险端累计计算。',
        'caution': '累计风险用于评估整体接纳质量；边际风险用于判断新增一档是否值得接纳，两者不可混用。',
        'key_columns': ['threshold', 'cum_pass_rate', 'cum_3m30p_cnt_bad_rate', 'marginal_3m30p_cnt_bad_rate'],
    },
    'train_oot_compare': {
        'focus': '关注各箱样本占比漂移、风险排序是否保持，以及Train/OOT逾期率差异最大的风险等级。',
        'logic': '使用Train学习的固定边界分别计算Train与OOT逐箱指标，再按最终风险等级对齐比较。',
        'caution': 'OOT成熟度、样本量和业务客群变化都会影响结果；需同时看成熟样本量和风险率。',
        'key_columns': ['sample_pct_train', 'sample_pct_oot', '3m30p_cnt_bad_rate_train', '3m30p_cnt_bad_rate_oot'],
    },
    'psi': {
        'focus': '先看总体PSI，再定位贡献最大的风险等级，并判断漂移是否集中在策略边界附近。',
        'logic': '以Train为基准分布、OOT为实际分布，按固定最终分箱计算各箱PSI贡献并求和。',
        'caution': 'PSI只反映分布变化，不直接代表模型失效；应与AUC/KS、风险排序和业务变化结合判断。',
        'key_columns': ['expected_pct', 'actual_pct', 'psi_component'],
    },
    'auc_ks': {
        'focus': '比较Train与OOT在1M30+、3M30+口径下的AUC、KS和坏样本量。',
        'logic': '仅使用模型分非空且标签为0/1的成熟样本，按高分高风险方向计算排序指标。',
        'caution': 'AUC/KS下降需要结合样本量、坏样本量和客群变化判断；少量成熟样本可能导致波动较大。',
        'key_columns': ['label', 'bad_rate', 'auc', 'ks'],
    },
    'monthly_auc_ks': {
        'focus': '识别AUC、KS或整体坏账率异常波动的月份，并与策略、渠道或数据变化交叉验证。',
        'logic': '按申请月份拆分成熟样本，分别计算1M30+和3M30+的AUC与KS。',
        'caution': '最近月份通常成熟度较低，不能与成熟月份直接横向比较。',
        'key_columns': ['bad_rate', 'auc', 'ks'],
    },
    'monthly_stability': {
        'focus': '检查每月风险等级是否保持单调、每箱成熟样本是否充足，以及异常月份数量。',
        'logic': '逐月套用固定最终边界，并统计各风险指标的倒挂次数和最小箱样本量。',
        'caution': '单月倒挂不一定需要立即调箱，应先判断是否由小样本、成熟度或偶发业务变化导致。',
        'key_columns': ['m1_bad_rate', 'm3_bad_rate', 'min_m3_mature_per_bin', '3m30p_cnt_bad_rate_violation_cnt'],
    },
    'diagnosis_summary': {
        'focus': '快速判断20等频初分中存在多少倒挂、成熟不足、坏样本不足和相邻置信区间重叠。',
        'logic': '基于预设诊断阈值，对每个初始箱执行样本量、风险排序、置信区间和金额口径检查。',
        'caution': '诊断阈值是分析参数，不等同于业务硬规则；应结合总体样本规模调整。',
    },
    'diagnosis_detail': {
        'focus': '优先检查merge_priority_score较高、3M30+倒挂或成熟样本不足的箱。',
        'logic': '逐箱计算与前一箱的风险差异，并汇总多类诊断标记形成合箱优先级。',
        'caution': '相邻置信区间重叠只表示差异证据不足，不代表两个箱业务上必须合并。',
        'key_columns': ['merge_priority_score', 'diagnosis_flags', '3m30p_cnt_bad_rate'],
    },
    'candidate_compare': {
        'focus': '对比6/7/8/9档方案在最小样本、单调性、OOT表现、PSI和月度稳定性上的平衡。',
        'logic': '对不同相邻合箱方案统一重算Train、OOT及跨月指标，再形成候选方案综合评分。',
        'caution': '综合评分用于缩小候选范围，最终档数还需兼顾策略可操作性和风险区分度。',
        'key_columns': ['bin_cnt', 'psi_total', 'oot_3m_cnt_violation_cnt', 'candidate_score'],
    },
    'binning_decision': {
        'focus': '查看最终选择的档数、选择理由、保留风险区分度以及后续需监控的问题。',
        'logic': '综合候选方案评估、相邻显著性、OOT稳定性和策略应用需求形成分箱结论。',
        'caution': '分箱结论应版本化管理；模型或客群发生明显变化时需要重新验证，而非沿用旧边界。',
    },
    'significance': {
        'focus': '重点看差异不显著且方向不稳定的相邻箱，作为进一步合并或观察的候选。',
        'logic': '对相邻箱坏样本率执行两比例Z检验和卡方检验，并结合单调方向生成合并提示。',
        'caution': '统计显著性受样本量影响；大样本下微小差异也可能显著，仍需判断业务意义。',
        'key_columns': ['metric', 'rate_diff', 'z_p_value', 'chi2_p_value', 'merge_hint'],
    },
    'rounding_compare': {
        'focus': '检查取整后迁移样本占比、最大箱规模变化、最大3M30+风险变化和OOT单调性。',
        'logic': '分别按3位和4位小数取整最终边界，并对全量样本重新分箱后与精确边界比较。',
        'caution': '边界取整看似微小，但若分数高度集中在边界附近，可能造成明显样本迁移。',
        'key_columns': ['shifted_pct', 'max_abs_3m30p_rate_delta', 'oot_3m_cnt_violation_cnt'],
    },
    'rounded4_bin_compare': {
        'focus': '逐箱定位4位小数取整造成的样本量和1M30+/3M30+风险差异。',
        'logic': '精确边界和4位小数边界分别计算同一批样本，再按箱序逐项相减。',
        'caution': '高风险尾部即使样本迁移不多，也可能对拒绝阈值和整体风险产生较大影响。',
        'key_columns': ['n_delta', '3m30p_cnt_bad_rate_exact', '3m30p_cnt_bad_rate_rounded4'],
    },
    'rounded4_strategy_compare': {
        'focus': '确认三套方案在边界取整前后的自动通过、人工审核、拒绝规模和接纳风险是否一致。',
        'logic': '在精确边界和4位小数边界下分别应用同一策略方案，并计算规模与风险差值。',
        'caution': '上线配置应以复算后的4位小数结果为准，不能直接复制精确边界下的指标。',
        'key_columns': ['auto_pass_threshold_rounded4', 'reject_threshold_rounded4', 'manual_review_rate_delta', 'accepted_3m30p_cnt_bad_rate_rounded4'],
    },
    'sensitivity_matrix': {
        'focus': '在不同人工审核产能与接纳3M30+风险上限下，比较可实现的接纳率和阈值组合。',
        'logic': '遍历产能上限和风险上限，对4位小数最终箱阈值组合进行约束搜索。',
        'caution': '矩阵结果是静态样本测算，不包含人工审核通过率、收益和运营时效等二次影响。',
        'key_columns': ['max_manual_review_rate'],
    },
    'sensitivity_detail': {
        'focus': '查看每组约束是否有可行解，以及对应的阈值、三段规模、接纳风险和边际风险。',
        'logic': '对每组约束返回满足条件的最优阈值组合；无可行组合时标记非OK状态。',
        'caution': 'accepted_3m30p_cnt_bad_rate是接纳人群累计风险，last_accepted_marginal风险代表最后一档新增风险。',
        'key_columns': ['status', 'auto_pass_threshold', 'reject_threshold', 'accepted_rate', 'accepted_3m30p_cnt_bad_rate'],
    },
    'sensitivity_decision': {
        'focus': '查看推荐采用的产能假设、风险上限、自动通过/人工审核/拒绝规则和备选方案。',
        'logic': '从敏感性扫描中提取人工审核不超过25%、接纳3M30+不超过8%的可行组合。',
        'caution': '若实际人工审核通过率、产能或风险偏好变化，应回到敏感性明细重新选择阈值。',
    },
    'quantile_compare': {
        'focus': '评估细粒度分位点阈值相较最终箱边界能否额外提升接纳率或自动通过率。',
        'logic': '在相同产能与风险约束下，分别用最终箱边界和细粒度分位点曲线搜索阈值。',
        'caution': '分位点阈值更精细，但上线解释、配置稳定性和版本管理成本更高。',
        'key_columns': ['accepted_rate_gain_quantile', 'auto_rate_gain_quantile'],
    },
    'plan_overview': {
        'focus': '横向比较增长、平衡、保守三套方案的阈值、三段规模、累计风险和最后接纳边际风险。',
        'logic': '基于最终阈值曲线选取不同风险偏好的自动通过与拒绝边界，并统一重算策略指标。',
        'caution': '方案名称只代表相对风险偏好；正式上线仍需结合收益、人工审核效果和业务目标。',
        'key_columns': ['auto_pass_threshold', 'reject_threshold', 'accepted_3m30p_cnt_bad_rate', 'last_accepted_marginal_3m30p_cnt_bad_rate'],
    },
    'segment_train': {
        'focus': '比较各方案在Train上的自动通过、人工审核、拒绝规模及三段风险梯度。',
        'logic': '按每套方案阈值切分Train样本，并分别计算三段风险和样本规模。',
        'caution': 'Train用于方案设计，不能替代OOT验证；过度追求Train单调可能造成过拟合。',
        'key_columns': ['sample_pct', '3m30p_cnt_bad_rate'],
    },
    'segment_oot': {
        'focus': '确认各方案在OOT上的三段规模和风险排序是否延续Train结论。',
        'logic': '直接套用Train阶段确定的策略阈值，对OOT样本重新切分和汇总。',
        'caution': '重点核对每段成熟样本量；观察期不足时3M30+风险可能为空或波动较大。',
        'key_columns': ['sample_pct', '3m30p_cnt_bad_rate'],
    },
}


def _is_pct_col(name):
    name_l = str(name).lower()
    return any(kw in name_l for kw in PCT_KEYWORDS) and 'threshold' not in name_l


def _is_count_col(name):
    name_l = str(name).lower()
    return name_l in {'n', 'total_n', 'bad_cnt', 'good_cnt'} or any(kw in name_l for kw in COUNT_KEYWORDS)


def _column_role(name):
    """根据字段名识别表头类别，用于颜色分组。"""
    name_l = str(name).lower()
    if any(kw in name_l for kw in ['threshold', 'decision', 'objective', 'status', 'rule', 'bin_order', 'bin']):
        return 'decision'
    if any(kw in name_l for kw in ['psi', 'oot', 'train', 'violation', 'gap', 'delta', 'p_value', 'significant', 'monotonic', 'shifted']):
        return 'stability'
    if any(kw in name_l for kw in ['amt', 'principal', 'exposure']):
        return 'amount'
    if any(kw in name_l for kw in ['bad_rate', 'lift', 'bad_cnt', 'risk']):
        return 'risk'
    if _is_pct_col(name_l) or _is_count_col(name_l) or any(kw in name_l for kw in ['mature', 'sample']):
        return 'volume'
    return 'default'


def _header_fill(name):
    role = _column_role(name)
    return {
        'risk': RISK_HEADER_FILL,
        'volume': VOLUME_HEADER_FILL,
        'stability': STABILITY_HEADER_FILL,
        'decision': DECISION_HEADER_FILL,
        'amount': AMOUNT_HEADER_FILL,
    }.get(role, HEADER_FILL)


def _number_format(name):
    name_l = str(name).lower()
    if _is_pct_col(name_l):
        return '0.00%'
    if 'lift' in name_l:
        return '0.00x'
    if _is_count_col(name_l):
        return '#,##0'
    if any(kw in name_l for kw in ['amount', 'amt', 'principal', 'exposure']):
        return '#,##0.00'
    if any(kw in name_l for kw in ['score', 'threshold', 'left', 'right']):
        return '0.0000'
    if any(kw in name_l for kw in ['auc', 'ks', 'psi', 'p_value', 'z_stat']):
        return '0.0000'
    return 'General'


def _metric_comment(name):
    name = str(name)
    if name in METRIC_COMMENTS:
        return METRIC_COMMENTS[name]

    # 对 Train/OOT、精确/取整等带后缀字段复用基础口径说明。
    for suffix in ['_train', '_oot', '_exact', '_rounded4', '_rounded3', '_quantile']:
        if name.endswith(suffix):
            base = name[:-len(suffix)]
            if base in METRIC_COMMENTS:
                suffix_text = {
                    '_train': '（Train样本）',
                    '_oot': '（OOT样本）',
                    '_exact': '（精确边界）',
                    '_rounded4': '（4位小数边界）',
                    '_rounded3': '（3位小数边界）',
                    '_quantile': '（分位点曲线）',
                }[suffix]
                return METRIC_COMMENTS[base] + suffix_text

    name_l = name.lower()
    if 'mature' in name_l:
        return '成熟样本或成熟金额口径。阅读风险率时应同时检查该字段是否充足。'
    if 'bad_rate' in name_l:
        return '逾期率/坏样本率字段。请同时核对对应分子、分母及观察期成熟度。'
    if 'delta' in name_l or 'gap' in name_l:
        return '两种口径或两个样本之间的差异值；正负方向需结合字段后缀理解。'
    if 'violation' in name_l:
        return '风险单调性或规则检查的违反次数/位置，数值越高越需要进一步排查。'
    return None


def _format_display_value(col_name, value):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return '空'
    if isinstance(value, (np.floating, float, np.integer, int)):
        value = float(value)
        if _is_pct_col(col_name):
            return f'{value:.2%}'
        if _is_count_col(col_name):
            return f'{value:,.0f}'
        return f'{value:.4f}'
    return str(value)


def _auto_insight(df):
    """从当前表格中提取少量自动提示，避免只给通用阅读说明。"""
    if df is None or df.empty:
        return '当前表无可展示数据，请检查样本筛选、标签成熟度或上游数据。'

    parts = []
    work = df.reset_index()

    if 'status' in work.columns:
        status = work['status'].astype(str).str.upper()
        non_ok = int((~status.eq('OK')).sum())
        if non_ok:
            parts.append(f'存在 {non_ok} 条非OK结果，需优先检查约束是否无解')

    if 'diagnosis_flags' in work.columns:
        flags = work['diagnosis_flags'].astype(str)
        flagged = int((~flags.eq('OK')).sum())
        if flagged:
            parts.append(f'共有 {flagged} 个初始箱触发至少一项诊断标记')

    if 'merge_priority_score' in work.columns:
        score = pd.to_numeric(work['merge_priority_score'], errors='coerce')
        if score.notna().any():
            parts.append(f'最高合箱优先级评分为 {score.max():.0f}')

    for rate_col in ['3m30p_cnt_bad_rate', 'accepted_3m30p_cnt_bad_rate', 'cum_3m30p_cnt_bad_rate']:
        if rate_col in work.columns:
            rate = pd.to_numeric(work[rate_col], errors='coerce')
            if rate.notna().any():
                parts.append(f'{rate_col} 范围为 {rate.min():.2%}–{rate.max():.2%}')
                break

    if {'3m30p_cnt_bad_rate_train', '3m30p_cnt_bad_rate_oot'}.issubset(work.columns):
        train_rate = pd.to_numeric(work['3m30p_cnt_bad_rate_train'], errors='coerce')
        oot_rate = pd.to_numeric(work['3m30p_cnt_bad_rate_oot'], errors='coerce')
        gap = (oot_rate - train_rate).abs()
        if gap.notna().any():
            parts.append(f'Train/OOT最大3M30+绝对差异为 {gap.max():.2%}')

    if 'psi_total' in work.columns:
        psi = pd.to_numeric(work['psi_total'], errors='coerce')
        if psi.notna().any():
            parts.append(f'总体PSI为 {psi.iloc[0]:.4f}')

    violation_cols = [c for c in work.columns if str(c).endswith('violation_cnt')]
    if violation_cols:
        total_violations = sum(pd.to_numeric(work[c], errors='coerce').fillna(0).sum() for c in violation_cols)
        if total_violations > 0:
            parts.append(f'表内累计记录 {int(total_violations)} 次单调性/规则违反')

    return '；'.join(parts[:3]) if parts else '请按“规模—风险—稳定性—可执行性”的顺序阅读，并重点核对高风险边界附近的变化。'


def auto_width(ws, min_w=10, max_w=42):
    """根据中英文字符宽度自动调整列宽。"""
    for col_cells in ws.columns:
        letter = get_column_letter(col_cells[0].column)
        best = min_w
        for cell in col_cells:
            if cell.value is not None:
                for line in str(cell.value).split('\n'):
                    width = sum(2 if ord(c) > 127 else 1 for c in line) + 3
                    best = max(best, width)
        ws.column_dimensions[letter].width = min(best, max_w)



def _apply_conditional_formatting(ws, header_row, data_start_row, data_end_row, column_names):
    if data_end_row < data_start_row:
        return

    for j, col_name in enumerate(column_names, start=2):
        col_letter = get_column_letter(j)
        cell_range = f'{col_letter}{data_start_row}:{col_letter}{data_end_row}'
        name_l = str(col_name).lower()

        # 风险率、PSI和差异值使用绿-黄-红渐变，帮助快速识别风险上升。
        if any(kw in name_l for kw in ['bad_rate', 'psi_component', 'risk', 'violation_cnt']):
            ws.conditional_formatting.add(
                cell_range,
                ColorScaleRule(
                    start_type='min', start_color='E2F0D9',
                    mid_type='percentile', mid_value=50, mid_color='FFF2CC',
                    end_type='max', end_color='F4CCCC',
                ),
            )
        elif any(kw in name_l for kw in ['auc', 'ks']):
            ws.conditional_formatting.add(
                cell_range,
                ColorScaleRule(
                    start_type='min', start_color='F4CCCC',
                    mid_type='percentile', mid_value=50, mid_color='FFF2CC',
                    end_type='max', end_color='E2F0D9',
                ),
            )
        elif any(kw in name_l for kw in ['delta', 'gap', 'shifted_pct']):
            ws.conditional_formatting.add(
                cell_range,
                ColorScaleRule(
                    start_type='min', start_color='E2F0D9',
                    mid_type='percentile', mid_value=50, mid_color='FFF2CC',
                    end_type='max', end_color='F4CCCC',
                ),
            )
        elif any(kw in name_l for kw in ['sample_pct', 'cum_pass_rate', 'auto_pass_rate', 'manual_review_rate', 'reject_rate', 'accepted_rate']):
            ws.conditional_formatting.add(
                cell_range,
                DataBarRule(start_type='num', start_value=0, end_type='max', color='5B9BD5', showValue=True),
            )



def _apply_text_highlights(cell, col_name):
    """对策略段、异常状态和诊断提示进行文本高亮。"""
    value = '' if cell.value is None else str(cell.value)
    name_l = str(col_name).lower()
    value_l = value.lower()

    if 'decision' in name_l:
        if 'auto' in value_l or '通过' in value:
            cell.fill = PASS_FILL
        elif 'manual' in value_l or '审核' in value:
            cell.fill = REVIEW_FILL
        elif 'reject' in value_l or '拒绝' in value:
            cell.fill = REJECT_FILL

    if 'status' in name_l and value and value.upper() != 'OK':
        cell.fill = ERROR_FILL
        cell.font = Font(name='Microsoft YaHei', size=9, bold=True, color='9C0006')

    if 'diagnosis_flags' in name_l and value and value != 'OK':
        cell.fill = WARN_FILL
        cell.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)

    if 'merge_hint' in name_l and '建议合并' in value:
        cell.fill = WARN_FILL
        cell.font = Font(name='Microsoft YaHei', size=9, bold=True, color='9C5700')

    if 'significant_5pct' in name_l and value in {'False', '0', '0.0'}:
        cell.fill = WARN_FILL



def write_block(ws, start_row, title, df, index_label='序号', guidance=None):
    """
    写入一个完整报告区块：标题、三类阅读说明、自动提示、表头与数据。

    guidance 支持：focus / logic / caution / key_columns。
    返回下一可用行号。
    """
    df = df.copy()
    ncols = max(len(df.columns) + 1, 2)
    guidance = guidance or {}

    # 标题行
    ws.merge_cells(start_row=start_row, start_column=1, end_row=start_row, end_column=ncols)
    tc = ws.cell(row=start_row, column=1, value=title)
    tc.fill = TITLE_FILL
    tc.font = TITLE_FONT
    tc.alignment = Alignment(horizontal='left', vertical='center')
    for c in range(1, ncols + 1):
        ws.cell(row=start_row, column=c).fill = TITLE_FILL
        ws.cell(row=start_row, column=c).border = MEDIUM_BOTTOM_BORDER
    ws.row_dimensions[start_row].height = 24

    notes = [
        ('重点关注', guidance.get('focus', '关注核心规模、风险指标、跨期变化以及需要策略判断的异常点。')),
        ('加工逻辑', guidance.get('logic', '按当前表对应的固定样本、字段口径和计算规则汇总生成。')),
        ('阅读注意', guidance.get('caution', '阅读比率时需同时核对分子、分母、时间窗口、成熟度和缺失样本处理。')),
        ('自动提示', _auto_insight(df)),
    ]

    row_cursor = start_row + 1
    guide_lines = [f'{i+1}. {text}' for i, (_, text) in enumerate(notes)]
    guide_text = '\n'.join(guide_lines)

    ws.merge_cells(start_row=row_cursor, start_column=1, end_row=row_cursor, end_column=ncols)
    cell = ws.cell(row=row_cursor, column=1, value=guide_text)
    cell.font = Font(name='Microsoft YaHei', size=9, color=TEXT_DARK)
    cell.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)
    cell.border = THIN_BORDER
    for c in range(1, ncols + 1):
        ws.cell(row=row_cursor, column=c).border = THIN_BORDER
    ws.row_dimensions[row_cursor].height = 72
    row_cursor += 1

    # 表头行
    hr = row_cursor
    ws.cell(row=hr, column=1, value=index_label)
    column_names = list(df.columns)
    for j, col_name in enumerate(column_names, start=2):
        ws.cell(row=hr, column=j, value=str(col_name))

    key_columns = {str(c) for c in guidance.get('key_columns', [])}
    for c in range(1, ncols + 1):
        cell = ws.cell(row=hr, column=c)
        col_name = index_label if c == 1 else column_names[c - 2]
        cell.fill = _header_fill(col_name)
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = THIN_BORDER
        if str(col_name) in key_columns:
            cell.font = Font(name='Microsoft YaHei', bold=True, color=WHITE, size=10, underline='single')
        comment_text = _metric_comment(col_name)
        if comment_text:
            cell.comment = Comment(comment_text, '策略报告')
    ws.row_dimensions[hr].height = 34

    # 数据行
    dr = hr + 1
    for i, (idx, row_data) in enumerate(df.iterrows()):
        r = dr + i
        if isinstance(idx, tuple):
            index_value = ' / '.join(str(x) for x in idx)
        elif isinstance(idx, float) and np.isnan(idx):
            index_value = i + 1
        else:
            index_value = idx
        ws.cell(row=r, column=1, value=index_value)

        for j, value in enumerate(row_data, start=2):
            cell = ws.cell(row=r, column=j)
            if pd.isna(value):
                cell.value = ''
            elif isinstance(value, (np.bool_, bool)):
                cell.value = bool(value)
            elif isinstance(value, (np.integer, int)):
                cell.value = int(value)
            elif isinstance(value, (np.floating, float)):
                cell.value = float(value)
            else:
                cell.value = value

        for c in range(1, ncols + 1):
            cell = ws.cell(row=r, column=c)
            col_name = index_label if c == 1 else column_names[c - 2]
            cell.font = DATA_FONT
            cell.border = THIN_BORDER
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=False)
            cell.number_format = _number_format(col_name)
            if i % 2 == 1:
                cell.fill = PatternFill(start_color=ALT_ROW, end_color=ALT_ROW, fill_type='solid')
            _apply_text_highlights(cell, col_name)

    data_end_row = dr + len(df) - 1
    _apply_conditional_formatting(ws, hr, dr, data_end_row, column_names)

    # 留出一行空白，区分不同表格区块。
    return dr + len(df) + 1



def series_block(ws, start_row, title, series, guidance=None):
    """将 Series 转置为两列 DataFrame 后写入，并保留统一阅读说明。"""
    df = pd.DataFrame({'指标': series.index.astype(str), '值': series.values})
    return write_block(
        ws,
        start_row,
        title,
        df.set_index('指标'),
        index_label='指标',
        guidance=guidance,
    )



def setup_sheet(ws, tab_color=None, freeze_panes=None):
    """统一工作表视图、打印与页眉页脚。"""
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = freeze_panes
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.page_margins.left = 0.25
    ws.page_margins.right = 0.25
    ws.page_margins.top = 0.50
    ws.page_margins.bottom = 0.50
    ws.sheet_view.zoomScale = 85
    ws.oddFooter.center.text = '模型分箱与策略阈值报告'
    ws.oddFooter.right.text = '第 &P 页 / 共 &N 页'
    if tab_color:
        ws.sheet_properties.tabColor = tab_color



def create_readme_sheet(wb):
    """创建报告阅读指南，集中解释口径、颜色和推荐阅读顺序。"""
    ws = wb.create_sheet('0.阅读指南')
    setup_sheet(ws, tab_color=NAVY)
    ws.sheet_view.zoomScale = 95

    ws.merge_cells('A1:H2')
    title_cell = ws['A1']
    title_cell.value = '模型分箱与策略阈值报告｜阅读指南'
    title_cell.fill = PatternFill(start_color=NAVY, end_color=NAVY, fill_type='solid')
    title_cell.font = Font(name='Microsoft YaHei', bold=True, size=18, color=WHITE)
    title_cell.alignment = Alignment(horizontal='left', vertical='center')
    for row in ws['A1:H2']:
        for cell in row:
            cell.fill = PatternFill(start_color=NAVY, end_color=NAVY, fill_type='solid')
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 16

    metric_df = pd.DataFrame({
        '指标': [
            '样本占比', '1M30+笔数逾期率', '3M30+笔数逾期率',
            '1M30+金额逾期率', '3M30+金额逾期率', 'Lift',
            '累计通过率', '边际风险', 'PSI', 'AUC/KS',
        ],
        '核心口径': [
            '当前箱n / 当前分析样本总n',
            'duedate_1m_30=1 / duedate_1m_30∈{0,1}',
            'duedate_3m_30=1 / duedate_3m_30∈{0,1}',
            'MOB1达到30+样本的剩余本金 / MOB1成熟样本本金',
            'MOB3达到30+样本的剩余本金 / MOB3成熟样本本金',
            '当前箱逾期率 / 整体逾期率',
            '从低风险端累计到当前阈值的样本占比',
            '新增一个分数区间自身的风险，而非累计风险',
            'Train与OOT固定分箱分布差异',
            '仅在模型分非空且标签成熟为0/1的样本上计算',
        ],
        '阅读注意': [
            '需关注单箱过小或高风险尾部样本不足',
            '最近月份可能未成熟，分母不足时结果为空',
            '3M观察期更长，OOT成熟度通常更受影响',
            '分母为成熟样本本金，分子为逾期剩余本金',
            '金额与笔数口径可能反映不同风险结构',
            'Lift>1表示高于总体风险，不等于绝对不可接受',
            '累计方向依赖高分高风险设定',
            '决定是否继续放宽阈值时优先关注',
            '分布漂移不等同于风险恶化，需要联动判断',
            '少量坏样本会造成较大波动',
        ],
    }).set_index('指标')
    next_row = write_block(
        ws,
        4,
        '一、关键指标口径',
        metric_df,
        index_label='指标',
        guidance={
            'focus': '阅读任何风险率时，先确认观察期、成熟样本、风险标的和分子分母。',
            'logic': '本报告当前统一使用1M30+与3M30+，同时提供笔数和金额两类口径。',
            'caution': '分母为0时安全除法返回空值；空值不能按0风险理解。',
            'key_columns': ['核心口径', '阅读注意'],
        },
    )

    logic_df = pd.DataFrame({
        '步骤': ['样本切分', '等频初分', '相邻合箱', 'OOT验证', '边界取整', '阈值选择'],
        '处理逻辑': [
            f'Train：application_month <= {TRAIN_END_MONTH}；OOT：application_month >= {OOT_START_MONTH}',
            '仅在Train上学习20等频边界，首尾扩展为负无穷和正无穷',
            '结合样本量、成熟度、倒挂、置信区间和金额/笔数差异合并相邻箱',
            'OOT直接套用Train边界，不在OOT重新学习分箱',
            '对最终边界取4位小数后重新分箱并完整复算',
            '在风险上限、规模和人工审核产能约束下确定自动通过/审核/拒绝阈值',
        ],
        '特殊注意': [
            'gap_or_unknown样本不进入Train或OOT主验证',
            '分数唯一值不足时实际箱数可能少于20',
            '最终合箱必须保持相邻，避免不可上线的非连续区间',
            '最近OOT月份需检查MOB3成熟度',
            '边界附近分数集中时，小数取整可能导致明显迁移',
            '累计风险与最后接纳边际风险需要同时满足要求',
        ],
    }).set_index('步骤')
    write_block(
        ws,
        next_row,
        '二、核心加工流程',
        logic_df,
        index_label='步骤',
        guidance={
            'focus': '理解边界在哪里学习、在哪里复用，以及阈值结果如何形成。',
            'logic': '整体流程为“Train学习—相邻合并—OOT验证—边界取整复算—阈值敏感性”。',
            'caution': '若调整样本窗口、模型方向或逾期口径，需从分箱学习阶段开始重新运行。',
            'key_columns': ['处理逻辑', '特殊注意'],
        },
    )

    ws.column_dimensions['A'].width = 18
    for col in range(2, 9):
        ws.column_dimensions[get_column_letter(col)].width = 18
    return ws


# ============================================================
# 写入 Excel
# ============================================================

wb = openpyxl.Workbook()
wb.remove(wb.active)
create_readme_sheet(wb)

# ==================== Sheet 1: 策略推荐结论 ====================
ws1 = wb.create_sheet('1.策略推荐结论')
setup_sheet(ws1, tab_color='548235')
r = 1

r = series_block(
    ws1, r, '一、最终推荐结论', strategy_recommendation,
    guidance=REPORT_GUIDANCE['strategy_recommendation'],
)
r = series_block(
    ws1, r, '二、4位小数边界复算结论', rounded4_recalc_decision,
    guidance=REPORT_GUIDANCE['rounded4_recalc'],
)

recommended = '平衡方案'
seg_cols = [
    'decision', 'n', 'sample_pct',
    '1m30p_cnt_mature', '1m30p_cnt_bad_rate',
    '3m30p_cnt_mature', '3m30p_cnt_bad_rate',
    '1m30p_amt_bad_rate', '3m30p_amt_bad_rate',
]

for label, seg_df in [('Train（4位小数）', strategy_segment_report_rounded4), ('OOT（4位小数）', strategy_segment_report_rounded4)]:
    mask = seg_df['strategy_name'].eq(recommended)
    if 'train' in label.lower():
        mask &= seg_df['sample_group'].astype(str).str.lower().str.contains('train', na=False)
    else:
        mask &= seg_df['sample_group'].astype(str).str.lower().str.contains('oot', na=False)
    subset = seg_df.loc[mask, seg_cols].set_index('decision')
    r = write_block(
        ws1, r, f'三、推荐方案「{recommended}」三段指标 — {label}', subset,
        guidance=REPORT_GUIDANCE['recommended_segment'],
    )

auto_width(ws1)

# ==================== Sheet 2: 最终分箱与验证 ====================
ws2 = wb.create_sheet('2.最终分箱与验证')
setup_sheet(ws2, tab_color='4472C4')
r = 1

bin_cols = [
    'merged_from', 'score_left', 'score_right', 'n', 'sample_pct',
    'score_min', 'score_max', '1m30p_cnt_mature', '1m30p_cnt_bad',
    '1m30p_cnt_bad_rate', '1m30p_cnt_lift', '3m30p_cnt_mature',
    '3m30p_cnt_bad', '3m30p_cnt_bad_rate', '3m30p_cnt_lift',
    '1m30p_amt_bad_rate', '3m30p_amt_bad_rate',
    'cum_pass_rate', 'cum_1m30p_cnt_bad_rate', 'cum_3m30p_cnt_bad_rate',
]
r = write_block(
    ws2, r, '一、8档最终风险等级（精确边界）',
    bin_stats_final.set_index('bin_order')[bin_cols],
    guidance=REPORT_GUIDANCE['final_bins'],
)

tc_cols = [c for c in [
    'threshold', FINAL_BIN_COL, 'merged_from',
    'cum_n', 'cum_pass_rate',
    'cum_1m30p_cnt_mature', 'cum_1m30p_cnt_bad_rate',
    'cum_3m30p_cnt_mature', 'cum_3m30p_cnt_bad_rate',
    'cum_1m30p_amt_bad_rate', 'cum_3m30p_amt_bad_rate',
    'marginal_n', 'marginal_sample_pct',
    'marginal_1m30p_cnt_bad_rate', 'marginal_3m30p_cnt_bad_rate',
] if c in threshold_curve_final_bins.columns]
r = write_block(
    ws2, r, '二、阈值曲线（各等级右边界作为阈值）',
    threshold_curve_final_bins.set_index('threshold_order')[tc_cols],
    guidance=REPORT_GUIDANCE['threshold_curve'],
)

compare_cols = [c for c in [
    'n_train', 'n_oot', 'sample_pct_train', 'sample_pct_oot',
    '1m30p_cnt_mature_train', '1m30p_cnt_mature_oot',
    '1m30p_cnt_bad_rate_train', '1m30p_cnt_bad_rate_oot',
    '3m30p_cnt_mature_train', '3m30p_cnt_mature_oot',
    '3m30p_cnt_bad_rate_train', '3m30p_cnt_bad_rate_oot',
    '1m30p_amt_bad_rate_train', '1m30p_amt_bad_rate_oot',
    '3m30p_amt_bad_rate_train', '3m30p_amt_bad_rate_oot',
] if c in train_oot_bin_compare.columns]
r = write_block(
    ws2, r, '三、Train vs OOT 逐箱对比',
    train_oot_bin_compare.set_index(FINAL_BIN_COL)[compare_cols],
    guidance=REPORT_GUIDANCE['train_oot_compare'],
)

psi_label = FINAL_BIN_COL if FINAL_BIN_COL in psi_final.columns else 'bin'
r = write_block(
    ws2, r,
    f'四、PSI 分布稳定性（总 PSI = {psi_final["psi_total"].iloc[0]:.6f}）',
    psi_final.set_index(psi_label)[[c for c in ['expected_cnt', 'expected_pct', 'actual_cnt', 'actual_pct', 'psi_component'] if c in psi_final.columns]],
    guidance=REPORT_GUIDANCE['psi'],
)

r = write_block(
    ws2, r, '五、AUC/KS 汇总',
    perf_by_group.set_index('sample_group')[[c for c in ['label', 'n', 'bad_cnt', 'bad_rate', 'auc', 'ks'] if c in perf_by_group.columns]],
    guidance=REPORT_GUIDANCE['auc_ks'],
)

mp_cols = [c for c in ['label', 'n', 'bad_cnt', 'bad_rate', 'auc', 'ks'] if c in monthly_perf.columns]
r = write_block(
    ws2, r, '六、逐月 AUC/KS',
    monthly_perf.set_index('application_month')[mp_cols],
    guidance=REPORT_GUIDANCE['monthly_auc_ks'],
)

m_cols = [c for c in [
    'bin_cnt', 'n', 'm1_mature', 'm3_mature', 'm1_bad_rate', 'm3_bad_rate',
    'min_bin_n', 'min_m1_mature_per_bin', 'min_m3_mature_per_bin',
    '1m30p_cnt_bad_rate_violation_cnt', '3m30p_cnt_bad_rate_violation_cnt',
] if c in monthly_stability_summary.columns]
r = write_block(
    ws2, r, '七、月度分箱稳定性',
    monthly_stability_summary.set_index('application_month')[m_cols],
    guidance=REPORT_GUIDANCE['monthly_stability'],
)

auto_width(ws2)

# ==================== Sheet 3: 分箱优化过程 ====================
ws3 = wb.create_sheet('3.分箱优化过程')
setup_sheet(ws3, tab_color='8064A2')
r = 1

r = series_block(
    ws3, r, '一、20等频箱诊断摘要', diagnosis_summary,
    guidance=REPORT_GUIDANCE['diagnosis_summary'],
)

diag_cols = [c for c in [
    BIN20_COL, 'n', '1m30p_cnt_mature', '1m30p_cnt_bad', '1m30p_cnt_bad_rate',
    '1m30p_cnt_rate_diff_prev', '3m30p_cnt_mature', '3m30p_cnt_bad',
    '3m30p_cnt_bad_rate', '3m30p_cnt_rate_diff_prev',
    'merge_priority_score', 'diagnosis_flags',
] if c in bin_diagnosis_20.columns]
r = write_block(
    ws3, r, '二、20等频箱初步诊断',
    bin_diagnosis_20.set_index('bin_order')[diag_cols],
    guidance=REPORT_GUIDANCE['diagnosis_detail'],
)

cand_cols = [c for c in [
    'bin_cnt', 'min_train_n', 'min_train_1m_mature', 'min_train_3m_mature',
    'min_train_1m_bad', 'min_train_3m_bad', 'train_violation_cnt',
    'oot_1m_cnt_violation_cnt', 'oot_3m_cnt_violation_cnt', 'psi_total',
    'months_with_1m_cnt_violation', 'months_with_3m_cnt_violation', 'candidate_score',
] if c in candidate_merge_compare.columns]
r = write_block(
    ws3, r, '三、6/7/8/9 档候选分箱方案对比',
    candidate_merge_compare.set_index('candidate_name')[cand_cols],
    guidance=REPORT_GUIDANCE['candidate_compare'],
)

r = series_block(
    ws3, r, '四、分箱优化结论', binning_optimization_decision,
    guidance=REPORT_GUIDANCE['binning_decision'],
)

sig_cols = [c for c in [
    'metric', 'left_bin_order', 'right_bin_order', 'left_rate', 'right_rate',
    'rate_diff', 'direction_ok', 'z_stat', 'z_p_value', 'chi2_p_value',
    'significant_5pct', 'merge_hint',
] if c in adjacent_sig_tests.columns]
r = write_block(
    ws3, r, '五、相邻箱显著性检验（仅显示建议合并的箱）',
    adjacent_sig_tests.loc[adjacent_sig_tests['merge_hint'].eq('建议合并'), sig_cols].reset_index(drop=True),
    guidance=REPORT_GUIDANCE['significance'],
)

auto_width(ws3)

# ==================== Sheet 4: 边界与阈值敏感性 ====================
ws4 = wb.create_sheet('4.边界与阈值敏感性')
setup_sheet(ws4, tab_color='C65911')
r = 1

round_cols = [c for c in [
    'shifted_n', 'shifted_pct', 'max_abs_bin_n_delta',
    'max_abs_3m30p_rate_delta', 'train_violation_cnt',
    'oot_1m_cnt_violation_cnt', 'oot_3m_cnt_violation_cnt',
] if c in rounded_boundary_compare.columns]
r = write_block(
    ws4, r, '一、3/4 位小数边界取整对比',
    rounded_boundary_compare.set_index('round_decimals')[round_cols],
    guidance=REPORT_GUIDANCE['rounding_compare'],
)

r4_bin_name = ROUNDED4_BIN_COL if 'ROUNDED4_BIN_COL' in dir() else 'score_mlt_final_bin_rounded4'
r4_cols = [c for c in [
    r4_bin_name,
    'n_exact', 'n_rounded4', 'n_delta', 'sample_pct_exact', 'sample_pct_rounded4',
    '1m30p_cnt_bad_rate_exact', '1m30p_cnt_bad_rate_rounded4',
    '3m30p_cnt_bad_rate_exact', '3m30p_cnt_bad_rate_rounded4',
    '1m30p_amt_bad_rate_exact', '1m30p_amt_bad_rate_rounded4',
    '3m30p_amt_bad_rate_exact', '3m30p_amt_bad_rate_rounded4',
] if c in rounded4_bin_compare.columns]
r = write_block(
    ws4, r, '二、精确边界 vs 4位小数边界逐箱对比',
    rounded4_bin_compare.set_index('bin_order')[r4_cols],
    guidance=REPORT_GUIDANCE['rounded4_bin_compare'],
)

sc_cols = [c for c in [
    'auto_pass_threshold_exact', 'auto_pass_threshold_rounded4',
    'reject_threshold_exact', 'reject_threshold_rounded4',
    'auto_pass_rate_exact', 'auto_pass_rate_rounded4', 'auto_pass_rate_delta',
    'manual_review_rate_exact', 'manual_review_rate_rounded4', 'manual_review_rate_delta',
    'reject_rate_exact', 'reject_rate_rounded4', 'reject_rate_delta',
    'accepted_1m30p_cnt_bad_rate_exact', 'accepted_1m30p_cnt_bad_rate_rounded4',
    'accepted_3m30p_cnt_bad_rate_exact', 'accepted_3m30p_cnt_bad_rate_rounded4',
] if c in strategy_plan_compare_rounded4.columns]
r = write_block(
    ws4, r, '三、精确边界 vs 4位小数策略方案对比',
    strategy_plan_compare_rounded4.set_index('strategy_name')[sc_cols],
    guidance=REPORT_GUIDANCE['rounded4_strategy_compare'],
)

r = write_block(
    ws4, r, '四、阈值敏感性矩阵（人工审核产能 × 接纳风险上限）',
    threshold_sensitivity_matrix_rounded4.set_index('max_manual_review_rate'),
    guidance=REPORT_GUIDANCE['sensitivity_matrix'],
)

sd_cols = [c for c in [
    'max_manual_review_rate', 'max_accepted_3m30p_cnt_bad_rate', 'status',
    'auto_pass_threshold', 'reject_threshold', 'auto_pass_rate',
    'accepted_rate', 'manual_review_rate', 'reject_rate',
    'auto_3m30p_cnt_bad_rate', 'accepted_3m30p_cnt_bad_rate',
    'last_accepted_marginal_3m30p_cnt_bad_rate',
] if c in threshold_sensitivity_rounded4.columns]
r = write_block(
    ws4, r, '五、阈值敏感性扫描明细',
    threshold_sensitivity_rounded4[sd_cols].reset_index(drop=True),
    guidance=REPORT_GUIDANCE['sensitivity_detail'],
)

r = series_block(
    ws4, r, '六、阈值敏感性推荐结论', threshold_sensitivity_decision,
    guidance=REPORT_GUIDANCE['sensitivity_decision'],
)

qv_cols = [c for c in [
    'accepted_rate_rounded4', 'accepted_rate_quantile', 'accepted_rate_gain_quantile',
    'auto_pass_rate_rounded4', 'auto_pass_rate_quantile', 'auto_rate_gain_quantile',
] if c in threshold_sensitivity_final_vs_quantile.columns]
r = write_block(
    ws4, r, '七、分位点曲线 vs 最终箱边界对比',
    threshold_sensitivity_final_vs_quantile.set_index(['max_manual_review_rate', 'max_accepted_3m30p_cnt_bad_rate'])[qv_cols],
    guidance=REPORT_GUIDANCE['quantile_compare'],
)

auto_width(ws4)

# ==================== Sheet 5: 三套策略方案对比 ====================
ws5 = wb.create_sheet('5.三套策略方案对比')
setup_sheet(ws5, tab_color='BF9000')
r = 1

plan_cols = [c for c in [
    'objective', 'auto_pass_bin', 'auto_pass_threshold',
    'manual_review_upper_bin', 'reject_threshold',
    'auto_pass_rate', 'manual_review_rate', 'reject_rate',
    'accepted_1m30p_cnt_bad_rate', 'accepted_3m30p_cnt_bad_rate',
    'accepted_1m30p_amt_bad_rate', 'accepted_3m30p_amt_bad_rate',
    'last_accepted_marginal_1m30p_cnt_bad_rate', 'last_accepted_marginal_3m30p_cnt_bad_rate',
] if c in strategy_plan.columns]
r = write_block(
    ws5, r, '一、三套策略方案总览',
    strategy_plan.set_index('strategy_name')[plan_cols],
    guidance=REPORT_GUIDANCE['plan_overview'],
)

seg_out_cols = [c for c in seg_cols if c != 'decision']

train_mask_all = strategy_segment_report['sample_group'].eq('train')
if train_mask_all.any():
    r = write_block(
        ws5, r, '二、各方案三段指标 — Train',
        strategy_segment_report.loc[train_mask_all].set_index(['strategy_name', 'decision'])[seg_out_cols],
        guidance=REPORT_GUIDANCE['segment_train'],
    )

oot_mask_all = strategy_segment_report['sample_group'].eq('oot')
if oot_mask_all.any():
    r = write_block(
        ws5, r, '三、各方案三段指标 — OOT',
        strategy_segment_report.loc[oot_mask_all].set_index(['strategy_name', 'decision'])[seg_out_cols],
        guidance=REPORT_GUIDANCE['segment_oot'],
    )

auto_width(ws5)

# ---- 保存 ----
wb.save(REPORT_PATH)
print(f'\n策略报告已生成: {REPORT_PATH}')

