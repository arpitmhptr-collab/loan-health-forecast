# Loan portfolio health and default forecast

**Question:** How healthy is a mortgage portfolio, how many loans will default over the next two years, and where should a lender focus its collections effort?

**Live dashboard:** https://arpitmhptr-collab.github.io/loan-health-forecast/dashboard/

Data: Fannie Mae Single-Family Loan Performance (Primary dataset), first-quarter acquisition cohorts 2017Q1, 2018Q1, 2019Q1 and 2020Q1, followed monthly through June 2026.
Scale: 1,972,226 loans and 94.7 million loan-months. Tools: DuckDB and Python on a laptop, one static Plotly page.

---

## Business recommendation

**1. Forecast defaults with a model that includes calendar time, not loan age alone.**
All four vintages show their highest default rate in 2020, at very different loan ages. A forecast that uses only loan age over-predicted holdout defaults by 25.5% (cutoff June 2024) and 133.3% (cutoff June 2023). The age x calendar model was off by -8.3% and +18.8% on the same tests; the simple run-rate benchmark was off by +4.2% and +46.8%.
*Decision that changes:* provisioning and capacity plans should be built from the age x calendar model, with the calendar assumption stated and reviewed, instead of a standard age curve.

**2. Escalate at 60 days, and aim early outreach at the riskiest origination profiles.**
- A loan at 60-89 days past due moves to 90-179 days next month 50.0% of the time and returns to current 15.1% of the time. A loan at 30-59 days returns to current 39.2% of the time. The 60-day line is where outcomes turn.
- Since July 2023, borrowers with scores below 700 were 16% of loan-months but 39% of defaults. Score separates who becomes delinquent far more than who rolls from 60-89 to 90+ (about 32% to 38% in every score band).
*Decision that changes:* prioritise 30-59 day contact for under-700 and high-LTV loans, and apply the same escalation at 60 days to every borrower, because score does not tell you who will roll further at that stage.

**How to test it:** randomise early-stage outreach intensity across comparable under-700 loans for 3 to 6 months and compare the 30-59 to 60-89 roll rate and 1-month cure rate. These results are associations in historical data; they do not show that any attribute causes default.

---

## What is in the dashboard

| Section | What it shows |
|---|---|
| Portfolio health | Share of active loans 30+ and 90+ days past due, monthly |
| Vintages and calendar time | Cumulative default by months on book; default rate by calendar year |
| How delinquency moves | Roll-rate matrix, one-month cure rate, final outcome of loans that reached 90+ days |
| Forecast and backtest | Actual vs predicted monthly defaults, holdout errors, 24-month forecast |
| Where risk concentrates | Share of loans vs share of defaults by credit score; credit score x LTV |

Each chart has a "View as table" option and a light/dark toggle.

## Metric definitions

| Metric | Definition |
|---|---|
| Delinquency buckets | Current; 30-59; 60-89; 90-179; 180+ days past due, from the reported months past due |
| Serious delinquency (SDQ) | 90+ days past due |
| Default | First month at 180+ days past due, or an exit with zero-balance code 02, 03, 09 or 15 (code 16 reported separately) |
| Cure (1 month) | Loan is current the next month; partial improvement is not a cure |
| Vintage | Acquisition-quarter file; clean cohort = loans first reported within 3 months of origination (97.7% of loans) |
| Hazard | Defaults divided by loans at risk in that loan-month |
| Roll rate | Share of loans in a bucket that are in each bucket (or have exited) one month later |

## Forecast method

1. **Run-rate benchmark:** recent hazard held constant. 
2. **Age-only curve:** hazard by loan age, the textbook approach.
3. **Age x calendar model:** hazard = age effect x calendar-month effect x vintage level, fitted by iterative scaling on counts; the calendar effect is anchored to the last six months of training data.

All three use the same exposure projection (at-risk loans next month = at-risk - defaults - other exits). Validation is out-of-time: train to a cutoff, predict the following months, compare. Cutoffs: June 2023 and June 2024, plus a forward 24-month forecast.

## Limits to keep in mind

- Four first-quarter cohorts only, so the loans are seasoned (2017-2020 originations) and the results are not a view of the whole market.
- Counts of loans, not dollar losses. The source masks current balances for each loan's first six months, so original balance is used for exposure.
- The 2020-21 shift coincides with the pandemic period and related relief programs. The data here does not test why it happened.
- The forecast assumes calendar conditions stay near the last six months' level.
- Segment cuts look at one attribute at a time (plus credit x LTV), so they overlap.

## Reproduce

```
python python/01_load_raw.py
duckdb data/processed/loans.duckdb < sql/02_quality_checks.sql
duckdb data/processed/loans.duckdb < sql/04_metrics.sql
duckdb data/processed/loans.duckdb < sql/05b_hazard_cells.sql
python python/05_forecast.py --cutoff 2023-06
python python/05_forecast.py --cutoff 2024-06
python python/05_forecast.py --forward 24
duckdb data/processed/loans.duckdb < sql/05c_segments.sql
python python/06_build_dashboard.py
```

Raw files are not in this repository. Download the quarterly Primary files from Fannie Mae's Data Dynamics site (free registration), place them in `data/raw/`, and update the expected row counts in `01_load_raw.py`.

## Repository layout

```
sql/        quality checks, metric tables, hazard cells, segment cuts
python/     loader, forecast, dashboard builder
docs/       saved check outputs
dashboard/  index.html and plotly.min.js (the published page)
```

## Data and terms

Source: Fannie Mae Single-Family Loan Performance Data. Only aggregated results are published here; no loan-level data and no database file are included. This is an independent portfolio project, not affiliated with or endorsed by Fannie Mae.
