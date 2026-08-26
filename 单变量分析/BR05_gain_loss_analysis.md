# BR05 Dishonour Rule — Gain/Loss ($) Backtest

Handoff doc for BR05 (dishonour hard-decline rule), covering methodology, formulas, reusable
code, and the results as computed to date. Read the "Status vs. current live rule" section
first — the $ numbers below were **not** computed against the rule condition currently live in
`risk_policy.py`.

## 1. What BR05 is

`new/risk_policy.py` (BlackRules, hard decline):

```python
# ~line 162-170
if (risk_level in ['NE', 'NF']
        and is_valid_number(record['bank_txn_dishonour_lender_ratio_84d'])
        and record['bank_txn_dishonour_lender_ratio_84d'] >= 0):
    if record['bank_txn_dishonour_lender_ratio_84d'] >= 0.33:
        rule_json = {}
        rule_json['code'] = 'BR05'
        rule_json['detail'] = 'High Risk: High lender dishonour ratio in the past 84 days'
        BlackRules.append(rule_json)
```

**Current live condition (as of 2026-08-26):** `finv_risk_level in ['NE','NF']` AND
`bank_txn_dishonour_lender_ratio_84d >= 0.33`. Committed `f6278ff` on branch
`prod-new-20260713-v1.1.1`.

`bank_txn_dishonour_lender_ratio_84d` is produced by `new/txn_tool/txn_dishonour.py` — the
ratio of dishonoured-payment events to a specific lender out of that lender's total payment
attempts, over a trailing 84-day window.

## 2. Status vs. current live rule — READ THIS FIRST

The threshold has changed twice since the $ backtest below was computed. **The $ numbers in
Section 5 were computed against the *original* condition (`ND1–NF` tier, threshold `>0.17`),
not the current live one (`NE+NF`, threshold `>=0.33`).**

| | Population | Threshold | $ backtest run? |
|---|---|---|---|
| Analyzed below (2026-08-05/06) | `finv_risk_level in ['ND1','ND2','NE','NF']` | `>0.17` | Yes — Section 5 |
| Live in `risk_policy.py` today | `finv_risk_level in ['NE','NF']` | `>=0.33` | **No** |

Why the gap: the 0.17→0.26→0.33 threshold changes since were driven by a lift/coverage
argument on a genuine out-of-time sample (`new_sim_result_oot.xlsx`), not a dollar
re-validation — `duedate_3m_30` (the label this analysis anchors on, i.e. 3-month severe
default) is **right-censored on the OOT window** (null rate climbs from 0% to 27% across the
window; needs ~120-160 days to mature). Every dollar proxy tried on faster-maturing labels
(`fpd7`, `fpd15`, `duedate_1m_5`, `duedate_1m_30`, using full `funded_amount` as an optimistic
gain ceiling) came out net-negative at both 0.26 and 0.33. **The true dollar economics of the
current live 0.33/NE+NF rule are an open question**, expected to be revisitable once the OOT
window matures (~September 2026, i.e. imminently).

The code in Section 4 is written to be re-run as-is against a fresh `new_sim_result*.xlsx`
pull once that data is available — swap the `RULE_CONDITION` function and re-run.

## 3. Data source

`new/new_sim_result.xlsx` — historical applications (`flowTime` Sep 2025–Feb 2026), already
re-scored under the policy code that includes BR05/GR09, so `finv_hardrule_hit` /
`finv_softrule_hit` correctly show `"code": "BR05"` for every row matching the numeric
condition. `funded_amount`, `amt_*`, `duedate_*` are real historical outcomes (these
applicants were actually funded, before BR05 existed in prod), not simulated.

**Marginal population, not the raw rule-eligible population.** Some rows that trip BR05 also
independently trip another hard rule (BR01/BR02) — those contribute zero *marginal* effect
from BR05 specifically, since they'd be declined either way. Filter to rows where BR05 is the
**sole** hard-rule hit:

```python
df['finv_hardrule_hit'] parsed as JSON list of {"code": ...}
marginal = df[df['hardrule_codes'].apply(lambda codes: codes == ['BR05'])]
```

At the `>0.17`/ND1-NF condition this produced n=418 (of 427 raw-eligible; 9 excluded for
overlapping BR01/BR02).

## 4. Methodology and code

**Labels used:** `duedate_1m_5`, `duedate_1m_30`, `duedate_3m_30` (bad = 1). `duedate_3m_30`
(3-month, 30+ days past due) is the reference label — genuine severe default, not just
resolvable lateness — and is what any headline conclusion should be weighted toward.

**Guilty** = flagged applicant who actually went bad on the label. **Innocent** = flagged
applicant who repaid fine.

- **Gain** (money protected by declining a guilty applicant) = the actual arrears dollar
  figure at that label's checkpoint: `amt_1m_5` / `amt_1m_30` / `amt_3m_30` respectively — **not**
  blindly the full `funded_amount`. Check this per column before trusting it: `amt_1m_5` and
  `amt_1m_30` turned out to equal `funded_amount` for 100% of guilty rows (i.e. not a real
  partial-arrears ledger at that early checkpoint — full principal is effectively still at
  risk), but `amt_3m_30` differs from `funded_amount` for 90.5% of guilty rows (aggregate
  ratio 79.4%) — it genuinely tracks partial recovery/repayment before default. Using full
  `funded_amount` for `amt_3m_30` would overstate Gain.
- **Loss** (revenue foregone by declining an innocent applicant) = fee/interest margin only,
  **not** their full `funded_amount` — a wrongly-declined good customer's principal was never
  at risk (they'd have repaid), only the fee/interest revenue on that loan is foregone.
  Fee-rate formula (Fundo's actual rates, confirmed with Henry 2026-08-05):
  - SACC tier (`funded_amount <= $2,000`): fee = `0.42 * funded_amount`
  - MACC tier (`funded_amount > $2,000`): fee = `0.28 * funded_amount`
  - **LACC tier (`funded_amount >= $5,000`) rate is unknown** — was never given because no
    LACC-sized loans were present in the analyzed population (max was $4,025). Do not reuse
    0.28 for LACC loans — confirm the real rate first if the population you're running this on
    includes any `funded_amount >= $5,000`.
- **Breakeven lift**: the lift (vs. the "All applications" base rate) at which Gain would
  exactly equal Loss. Solve `p * g = (1-p) * l` for breakeven precision `p`, where `g` = avg
  Gain per guilty, `l` = avg Loss per innocent; convert to lift by dividing by the population's
  base bad-rate. Compare against the rule's **observed** lift on the same population.

```python
"""
BR05 gain/loss $ backtest.

Reusable against any new_sim_result*.xlsx pull -- just point INPUT_XLSX at the new
file and re-run. RULE_CONDITION / LABELS / FEE_RATES are the only things that should
need editing if the rule definition or fee schedule changes.
"""
import json
import pandas as pd

INPUT_XLSX = "new_sim_result.xlsx"
LABELS = ["duedate_1m_5", "duedate_1m_30", "duedate_3m_30"]
AMT_COL = {  # actual-arrears column per label, falls back to funded_amount if absent
    "duedate_1m_5": "amt_1m_5",
    "duedate_1m_30": "amt_1m_30",
    "duedate_3m_30": "amt_3m_30",
}
FEE_RATES = {"SACC": 0.42, "MACC": 0.28}  # LACC unknown -- see caveat above
SACC_MAX = 2000  # funded_amount <= this -> SACC
LACC_MIN = 5000  # funded_amount >= this -> LACC (unsupported, will raise)


def parse_hardrule_codes(cell):
    """finv_hardrule_hit is a JSON string like '[{"code": "BR05", "detail": "..."}]'."""
    if pd.isna(cell) or cell in ("", "[]"):
        return []
    try:
        return [x["code"] for x in json.loads(cell)]
    except (ValueError, TypeError):
        return []


def rule_condition(row):
    """Current live BR05 condition -- swap this to re-target a different threshold/tier."""
    return (
        row["finv_risk_level"] in ("NE", "NF")
        and pd.notna(row["bank_txn_dishonour_lender_ratio_84d"])
        and row["bank_txn_dishonour_lender_ratio_84d"] >= 0.33
    )


def fee_rate(funded_amount):
    if funded_amount >= LACC_MIN:
        raise ValueError(
            f"funded_amount={funded_amount} is LACC-tier; fee rate not confirmed, "
            "do not guess -- ask before extending this formula to LACC loans."
        )
    return FEE_RATES["SACC"] if funded_amount <= SACC_MAX else FEE_RATES["MACC"]


def marginal_br05_population(df):
    """Rows where BR05 is the *sole* hard-rule hit -- isolates BR05's marginal effect
    from rows that would be declined by BR01/BR02 regardless."""
    df = df.copy()
    df["hardrule_codes"] = df["finv_hardrule_hit"].apply(parse_hardrule_codes)
    eligible = df[df.apply(rule_condition, axis=1)]
    marginal = eligible[eligible["hardrule_codes"].apply(lambda c: c == ["BR05"])]
    return marginal


def gain_loss_by_label(marginal, label):
    guilty = marginal[marginal[label] == 1]
    innocent = marginal[marginal[label] == 0]

    amt_col = AMT_COL[label]
    gain = guilty[amt_col].sum() if amt_col in marginal.columns else guilty["funded_amount"].sum()
    loss = innocent["funded_amount"].apply(fee_rate).mul(innocent["funded_amount"]).sum()
    net = gain - loss

    base_rate = marginal[label].mean()  # vs. this rule's own eligible/marginal population;
                                          # for a vs-total-book lift, use the unfiltered df's rate instead
    avg_gain = guilty[amt_col].mean() if len(guilty) else 0
    avg_loss = loss / len(innocent) if len(innocent) else 0
    # solve p*avg_gain = (1-p)*avg_loss for breakeven precision p
    breakeven_precision = avg_loss / (avg_gain + avg_loss) if (avg_gain + avg_loss) else float("nan")
    breakeven_lift = breakeven_precision / base_rate if base_rate else float("nan")
    observed_precision = len(guilty) / len(marginal) if len(marginal) else float("nan")
    observed_lift = observed_precision / base_rate if base_rate else float("nan")

    return {
        "label": label,
        "guilty_n": len(guilty),
        "innocent_n": len(innocent),
        "gain": round(gain, 2),
        "loss": round(loss, 2),
        "net": round(gain - loss, 2),
        "observed_lift": round(observed_lift, 3),
        "breakeven_lift": round(breakeven_lift, 3),
    }


if __name__ == "__main__":
    df = pd.read_excel(INPUT_XLSX)
    marginal = marginal_br05_population(df)
    print(f"Marginal BR05-eligible population: n={len(marginal)}")
    results = [gain_loss_by_label(marginal, label) for label in LABELS]
    print(pd.DataFrame(results).to_string(index=False))
```

Notes on the code:

- `fee_rate()` deliberately raises on any `funded_amount >= $5,000` (LACC) rather than
  guessing a rate — remove that guard only once a real LACC fee rate is confirmed.
- `rule_condition()` hardcodes the *current live* BR05 definition. To reproduce the historical
  Section 5 numbers exactly, swap it to `risk_level in ('ND1','ND2','NE','NF')` and
  `ratio_84d > 0.17`.
- `base_rate` above is computed against the rule's own marginal population (`lift_vs_segment`
  in the source analysis). For `lift_vs_total` (lift against the whole book's base rate, always
  larger since NE/NF is inherently higher-risk), pass the unfiltered `df[label].mean()` instead.

## 5. Results (historical run, `>0.17` / ND1-NF condition, n=418)

Source: `new/prod-new-20260713-v1.1.1_new_policy_iteration_report.xlsx`, sheet
`5_Gain_Loss_Analysis`.

| label | guilty n | innocent n | Gain ($, actual arrears) | Loss ($, fee margin) | Net ($) | Observed lift (vs. all) | Breakeven lift |
|---|---:|---:|---:|---:|---:|---:|---:|
| duedate_1m_5 | 64 | 354 | 61,000.00 | 136,559.50 | **-75,559.50** | 2.14 | 4.03 |
| duedate_1m_30 | 22 | 396 | 23,700.00 | 151,938.50 | **-128,238.50** | 2.52 | 12.57 |
| duedate_3m_30 | 97 | 321 | 76,702.45 | 122,220.00 | **-45,517.55** | 1.79 | 2.51 |

**Reading:** all three labels are net-negative under this cost model, `duedate_3m_30` least so.
None clear their own breakeven lift bar (observed lift is below breakeven lift in every row).
This is one reason the rule was subsequently narrowed (dropping ND1/ND2, tightening the
threshold) rather than left at `>0.17` / ND1-NF — see Section 2 for what's changed since and
what's still unresolved for the current live version.

**Not costed:** GR09 (grey-flag / manual-review band) — its real manual-review approval rate
is unknown, so there's no way to know how many flagged applicants would ultimately be funded
anyway.

## 6. Open items for whoever picks this up

1. **Re-run the $ backtest against the current live condition** (`NE+NF`, `>=0.33`) once
   `duedate_3m_30` has matured on the OOT window (`new_sim_result_oot.xlsx`, flowTime
   2026-03-01–04-30 — needs ~120-160 days, so check maturity from ~late July 2026 onward for
   the earliest rows, full window by ~September 2026). Use the code in Section 4 unchanged,
   just point `INPUT_XLSX` at a freshly-pulled OOT file.
2. **LACC fee rate** — confirm with the business before extending this formula to any
   population that includes `funded_amount >= $5,000` loans.
3. **GR09's $ value** — needs a manual-review approval rate assumption before it can be costed
   at all.
