-- Step 5c: which borrower segments roll and default more in the POST-SHOCK window?
-- Window: report months from 2023-07 onward (after the 2020-21 shock has largely passed).
-- Run from project root:
--   duckdb data/processed/loans.duckdb < sql/05c_segments.sql | tee docs/segments_output.txt
--
-- Segment attributes are fixed at origination (credit score, LTV, DTI). All loans are used (not only the
-- clean cohort). Rates are pooled loan-months, so they are comparable ACROSS segments, not forecasts.
-- entry_rate    = % of current loan-months that are 30+ days delinquent next month
-- b1_roll/cure  = from 30-59: % worse next month / % fully current next month
-- b2_roll/cure  = from 60-89: % at 90+ next month / % fully current next month
-- default_annualized = 1-(1-h)^12 where h = default events / loans at risk per month

SET memory_limit = '6GB';
SET temp_directory = 'data/processed/tmp';
SET VARIABLE window_start = DATE '2023-07-01';

DROP TABLE IF EXISTS seg_cell;
CREATE TABLE seg_cell AS
WITH x AS (
  SELECT m.loan_id, m.report_month, m.is_active, m.bucket, m.dlq_months,
         lead(m.report_month) OVER w AS nxt_month,
         lead(m.bucket) OVER w AS nxt_bucket,
         lead(m.zero_bal_code) OVER w AS nxt_exit
  FROM loan_month_clean m
  WHERE m.report_month >= getvariable('window_start')
  WINDOW w AS (PARTITION BY m.loan_id ORDER BY m.report_month)),
y AS (
  SELECT x.*, s.default_month,
         CASE WHEN s.credit_score IS NULL THEN 'C0 unknown'
              WHEN s.credit_score < 660 THEN 'C1 <660'
              WHEN s.credit_score < 700 THEN 'C2 660-699'
              WHEN s.credit_score < 740 THEN 'C3 700-739'
              WHEN s.credit_score < 780 THEN 'C4 740-779'
              ELSE 'C5 780+' END AS credit_band,
         CASE WHEN s.orig_ltv IS NULL THEN 'L0 unknown'
              WHEN s.orig_ltv <= 60 THEN 'L1 <=60'
              WHEN s.orig_ltv <= 80 THEN 'L2 61-80'
              WHEN s.orig_ltv <= 90 THEN 'L3 81-90'
              ELSE 'L4 >90' END AS ltv_band,
         CASE WHEN s.dti IS NULL THEN 'D0 unknown'
              WHEN s.dti <= 30 THEN 'D1 <=30'
              WHEN s.dti <= 36 THEN 'D2 31-36'
              WHEN s.dti <= 43 THEN 'D3 37-43'
              ELSE 'D4 >43' END AS dti_band,
         (x.is_active AND x.dlq_months IS NOT NULL
          AND x.nxt_month = x.report_month + INTERVAL 1 MONTH
          AND (x.nxt_exit IS NOT NULL OR x.nxt_bucket <> 'BU Unknown')) AS used
  FROM x JOIN loan_summary s USING (loan_id))
SELECT credit_band, ltv_band, dti_band,
       count(*) AS loan_months,
       count(*) FILTER (WHERE default_month IS NULL OR report_month <= default_month) AS at_risk,
       count(*) FILTER (WHERE report_month = default_month) AS default_events,
       count(*) FILTER (WHERE used AND bucket = 'B0 Current') AS cur_rows,
       count(*) FILTER (WHERE used AND bucket = 'B0 Current' AND nxt_exit IS NULL
                          AND nxt_bucket <> 'B0 Current') AS cur_to_dlq,
       count(*) FILTER (WHERE used AND bucket = 'B1 30-59') AS b1_rows,
       count(*) FILTER (WHERE used AND bucket = 'B1 30-59' AND nxt_exit IS NULL
                          AND nxt_bucket IN ('B2 60-89', 'B3 90-179', 'B4 180+')) AS b1_roll,
       count(*) FILTER (WHERE used AND bucket = 'B1 30-59' AND nxt_exit IS NULL
                          AND nxt_bucket = 'B0 Current') AS b1_cure,
       count(*) FILTER (WHERE used AND bucket = 'B2 60-89') AS b2_rows,
       count(*) FILTER (WHERE used AND bucket = 'B2 60-89' AND nxt_exit IS NULL
                          AND nxt_bucket IN ('B3 90-179', 'B4 180+')) AS b2_roll,
       count(*) FILTER (WHERE used AND bucket = 'B2 60-89' AND nxt_exit IS NULL
                          AND nxt_bucket = 'B0 Current') AS b2_cure
FROM y GROUP BY ALL;

SELECT 'R12. segment cells reconcile to direct counts (window from 2023-07)' AS check_name;
SELECT (SELECT sum(loan_months) FROM seg_cell) AS cell_loan_months,
       (SELECT count(*) FROM loan_month_clean WHERE report_month >= getvariable('window_start')) AS direct_loan_months,
       (SELECT sum(loan_months) FROM seg_cell)
         - (SELECT count(*) FROM loan_month_clean WHERE report_month >= getvariable('window_start')) AS diff_must_be_0,
       (SELECT sum(default_events) FROM seg_cell) AS cell_defaults,
       (SELECT count(*) FROM loan_summary WHERE default_month >= getvariable('window_start')) AS direct_defaults,
       (SELECT sum(default_events) FROM seg_cell)
         - (SELECT count(*) FROM loan_summary WHERE default_month >= getvariable('window_start')) AS diff_defaults_must_be_0;
SELECT (SELECT sum(cur_rows) FROM seg_cell) AS cell_current_rows,
       (SELECT count(*) FROM loan_month_clean WHERE report_month >= getvariable('window_start')
          AND report_month < (SELECT max(report_month) FROM loan_month_clean)
          AND is_active AND bucket = 'B0 Current') AS direct_current_rows_before_last_month,
       (SELECT count(*) FROM loan_month_clean WHERE report_month >= getvariable('window_start')
          AND report_month < (SELECT max(report_month) FROM loan_month_clean)
          AND is_active AND bucket = 'B0 Current') - (SELECT sum(cur_rows) FROM seg_cell) AS rows_excluded_gaps_or_unknown;

DROP TABLE IF EXISTS m_segment_post_shock;
CREATE TABLE m_segment_post_shock AS
WITH l AS (UNPIVOT seg_cell ON credit_band, ltv_band, dti_band INTO NAME dimension VALUE segment),
a AS (SELECT dimension, segment, sum(loan_months) AS loan_months, sum(at_risk) AS at_risk,
             sum(default_events) AS default_events, sum(cur_rows) AS cur_rows, sum(cur_to_dlq) AS cur_to_dlq,
             sum(b1_rows) AS b1_rows, sum(b1_roll) AS b1_roll, sum(b1_cure) AS b1_cure,
             sum(b2_rows) AS b2_rows, sum(b2_roll) AS b2_roll, sum(b2_cure) AS b2_cure
      FROM l GROUP BY ALL),
t AS (SELECT sum(at_risk) AS tot_at_risk, sum(default_events) AS tot_ev FROM seg_cell)
SELECT dimension, segment, loan_months,
       round(100.0 * loan_months / sum(loan_months) OVER (PARTITION BY dimension), 2) AS pct_of_loan_months,
       round(100.0 * cur_to_dlq / nullif(cur_rows, 0), 3) AS entry_rate_pct,
       b1_rows, round(100.0 * b1_roll / nullif(b1_rows, 0), 2) AS b1_roll_pct,
       round(100.0 * b1_cure / nullif(b1_rows, 0), 2) AS b1_cure_pct,
       b2_rows, round(100.0 * b2_roll / nullif(b2_rows, 0), 2) AS b2_roll_to_90_pct,
       round(100.0 * b2_cure / nullif(b2_rows, 0), 2) AS b2_cure_pct,
       default_events,
       round(100.0 * default_events / sum(default_events) OVER (PARTITION BY dimension), 2) AS pct_of_defaults,
       round(100.0 * (1 - power(1 - default_events * 1.0 / nullif(at_risk, 0), 12)), 3) AS default_annualized_pct,
       round((default_events * 1.0 / nullif(at_risk, 0)) / (t.tot_ev * 1.0 / t.tot_at_risk), 2) AS default_index_vs_portfolio
FROM a, t ORDER BY dimension, segment;

SELECT 'Portfolio total in the window' AS check_name;
SELECT sum(loan_months) AS loan_months, sum(default_events) AS default_events,
       round(100.0 * sum(cur_to_dlq) / sum(cur_rows), 3) AS entry_rate_pct,
       round(100.0 * sum(b1_roll) / sum(b1_rows), 2) AS b1_roll_pct,
       round(100.0 * sum(b1_cure) / sum(b1_rows), 2) AS b1_cure_pct,
       round(100.0 * sum(b2_roll) / sum(b2_rows), 2) AS b2_roll_to_90_pct,
       round(100.0 * sum(b2_cure) / sum(b2_rows), 2) AS b2_cure_pct,
       round(100.0 * (1 - power(1 - sum(default_events) * 1.0 / sum(at_risk), 12)), 3) AS default_annualized_pct
FROM seg_cell;

SELECT 'One-way segment cuts (check sample sizes: default_events, b2_rows)' AS check_name;
SELECT * FROM m_segment_post_shock;

DROP TABLE IF EXISTS m_segment_credit_ltv;
CREATE TABLE m_segment_credit_ltv AS
SELECT credit_band, ltv_band, sum(loan_months) AS loan_months, sum(default_events) AS default_events,
       sum(b2_rows) AS b2_rows,
       CASE WHEN sum(default_events) >= 30 THEN
            round(100.0 * (1 - power(1 - sum(default_events) * 1.0 / sum(at_risk), 12)), 3) END AS default_annualized_pct,
       CASE WHEN sum(b2_rows) >= 200 THEN round(100.0 * sum(b2_roll) / sum(b2_rows), 2) END AS b2_roll_to_90_pct
FROM seg_cell GROUP BY ALL;

SELECT 'Annualized default %, credit band (rows) x LTV band (columns); blank = fewer than 30 default events' AS check_name;
PIVOT m_segment_credit_ltv ON ltv_band USING first(default_annualized_pct) GROUP BY credit_band ORDER BY credit_band;
SELECT 'Default events behind the table above (sample size)' AS check_name;
PIVOT m_segment_credit_ltv ON ltv_band USING first(default_events) GROUP BY credit_band ORDER BY credit_band;
SELECT '60-89 -> 90+ roll %, credit band x LTV band; blank = fewer than 200 loan-months' AS check_name;
PIVOT m_segment_credit_ltv ON ltv_band USING first(b2_roll_to_90_pct) GROUP BY credit_band ORDER BY credit_band;

DROP TABLE seg_cell;
