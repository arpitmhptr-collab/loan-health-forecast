-- Step 4: clean tables + portfolio metrics. Raw table loan_month is never changed.
-- Run from project root (takes several minutes; the roll-rate step is the heaviest):
--   duckdb data/processed/loans.duckdb < sql/04_metrics.sql | tee docs/metrics_build_output.txt
--
-- RULES (from Step 3 findings):
--  * current_upb = 0 with no exit code is Fannie Mae masking (first 6 months) -> NULL
--  * months_on_book (mob) = months from origination month to report month
--  * vintage = acquisition file; clean cohort = first seen within 3 months of origination
--  * static fields (orig_upb, orig_month, ...) taken from each loan's first month
--  * status XX = unknown: excluded from numerators and denominators
--  * default = first month with 6+ months delinquent (180+) OR exit code 02/03/09/15
--  * serious delinquency (SDQ) = 3+ months delinquent (90+)
--  * active row = not an exit row (zero_bal_code IS NULL)

SET memory_limit = '6GB';
SET preserve_insertion_order = false;
SET temp_directory = 'data/processed/tmp';

-- ---------------------------------------------------------------- 4.1 loan_static
DROP TABLE IF EXISTS loan_static;
CREATE TABLE loan_static AS
WITH f AS (
  SELECT loan_id,
         any_value(src_quarter) AS vintage,
         min(report_month) AS first_report,
         arg_min(orig_month, report_month) AS orig_month,
         arg_min(orig_upb, report_month) AS orig_upb,
         arg_min(orig_rate, report_month) AS orig_rate,
         arg_min(credit_score, report_month) AS credit_score,
         arg_min(orig_ltv, report_month) AS orig_ltv,
         arg_min(dti, report_month) AS dti,
         arg_min(state, report_month) AS state,
         arg_min(loan_purpose, report_month) AS loan_purpose,
         arg_min(occupancy, report_month) AS occupancy
  FROM loan_month GROUP BY loan_id)
SELECT *, date_diff('month', orig_month, first_report) AS lag_m,
       (date_diff('month', orig_month, first_report) <= 3) AS in_vintage
FROM f;

SELECT 'R1. loan_static reconciles to raw loan count' AS check_name;
SELECT (SELECT count(*) FROM loan_static) AS static_loans,
       (SELECT count(DISTINCT loan_id) FROM loan_month) AS raw_loans,
       (SELECT count(*) FROM loan_static) - (SELECT count(DISTINCT loan_id) FROM loan_month) AS diff_must_be_0;
SELECT vintage, count(*) AS loans, round(sum(orig_upb) / 1e9, 2) AS orig_upb_bn,
       count(*) FILTER (WHERE in_vintage) AS clean_cohort_loans,
       round(100.0 * count(*) FILTER (WHERE in_vintage) / count(*), 1) AS pct_clean
FROM loan_static GROUP BY 1 ORDER BY 1;

-- ---------------------------------------------------------------- 4.2 loan_month_clean
DROP TABLE IF EXISTS loan_month_clean;
CREATE TABLE loan_month_clean AS
SELECT m.loan_id, s.vintage, s.in_vintage, m.report_month,
       date_diff('month', s.orig_month, m.report_month) AS mob,
       m.dlq_months,
       CASE WHEN m.dlq_months IS NULL THEN 'BU Unknown'
            WHEN m.dlq_months = 0 THEN 'B0 Current'
            WHEN m.dlq_months = 1 THEN 'B1 30-59'
            WHEN m.dlq_months = 2 THEN 'B2 60-89'
            WHEN m.dlq_months BETWEEN 3 AND 5 THEN 'B3 90-179'
            ELSE 'B4 180+' END AS bucket,
       m.zero_bal_code,
       (m.zero_bal_code IS NULL) AS is_active,
       CASE WHEN m.current_upb = 0 AND m.zero_bal_code IS NULL THEN NULL
            ELSE m.current_upb END AS upb_clean,
       s.orig_upb
FROM loan_month m JOIN loan_static s USING (loan_id)
WHERE m.report_month >= s.orig_month;

SELECT 'R2. loan_month_clean vs raw: rows and balance' AS check_name;
SELECT (SELECT count(*) FROM loan_month) AS raw_rows,
       (SELECT count(*) FROM loan_month_clean) AS clean_rows,
       (SELECT count(*) FROM loan_month) - (SELECT count(*) FROM loan_month_clean) AS dropped_rows,
       (SELECT count(*) FROM loan_month m JOIN loan_static s USING (loan_id)
         WHERE m.report_month < s.orig_month) AS rows_before_origination_check;
SELECT round((SELECT sum(current_upb) FROM loan_month) / 1e9, 4) AS raw_sum_upb_bn,
       round((SELECT sum(upb_clean) FROM loan_month_clean) / 1e9, 4) AS clean_sum_upb_bn,
       (SELECT count(*) FROM loan_month_clean WHERE upb_clean IS NULL) AS masked_rows_set_to_null;

-- ---------------------------------------------------------------- 4.3 loan_summary
DROP TABLE IF EXISTS loan_summary;
CREATE TABLE loan_summary AS
WITH a AS (
  SELECT loan_id,
         min(report_month) FILTER (WHERE dlq_months >= 3) AS first_90,
         min(report_month) FILTER (WHERE dlq_months >= 6) AS first_180,
         min(report_month) FILTER (WHERE zero_bal_code IN ('02', '03', '09', '15')) AS credit_exit_month,
         min(report_month) FILTER (WHERE zero_bal_code IS NOT NULL) AS exit_month,
         max(zero_bal_code) AS exit_code,
         max(report_month) AS last_report,
         arg_max(dlq_months, report_month) AS last_status
  FROM loan_month_clean GROUP BY loan_id),
c AS (
  SELECT m.loan_id, true AS cured
  FROM loan_month_clean m JOIN a ON a.loan_id = m.loan_id
  WHERE a.first_90 IS NOT NULL AND m.report_month > a.first_90
    AND m.is_active AND m.dlq_months = 0
  GROUP BY m.loan_id),
b AS (
  SELECT s.*, a.* EXCLUDE (loan_id), coalesce(c.cured, false) AS cured_after_90,
         CASE WHEN a.first_180 IS NULL THEN a.credit_exit_month
              WHEN a.credit_exit_month IS NULL THEN a.first_180
              ELSE least(a.first_180, a.credit_exit_month) END AS default_month
  FROM loan_static s JOIN a USING (loan_id) LEFT JOIN c USING (loan_id))
SELECT *,
       date_diff('month', orig_month, default_month) AS default_mob,
       date_diff('month', orig_month, first_90) AS first_90_mob,
       date_diff('month', orig_month, exit_month) AS exit_mob,
       CASE WHEN default_month IS NULL THEN NULL
            WHEN first_180 = default_month THEN 'D180' ELSE 'credit_exit' END AS default_trigger
FROM b;

SELECT 'R3. loan_summary: counts reconcile to a direct count on loan_month_clean; compare % with Step 3 preview F' AS check_name;
SELECT s.vintage, count(*) AS loans,
       count(*) FILTER (WHERE s.default_month IS NOT NULL) AS defaults_summary,
       any_value(d.n) AS defaults_direct,
       count(*) FILTER (WHERE s.default_month IS NOT NULL) - any_value(d.n) AS diff_must_be_0,
       round(100.0 * count(*) FILTER (WHERE s.first_90 IS NOT NULL) / count(*), 3) AS pct_ever_90,
       round(100.0 * count(*) FILTER (WHERE s.first_180 IS NOT NULL) / count(*), 3) AS pct_ever_180,
       round(100.0 * count(*) FILTER (WHERE s.credit_exit_month IS NOT NULL) / count(*), 3) AS pct_credit_exit,
       round(100.0 * count(*) FILTER (WHERE s.default_month IS NOT NULL) / count(*), 3) AS pct_default
FROM loan_summary s
JOIN (SELECT vintage, count(DISTINCT loan_id) AS n FROM loan_month_clean
      WHERE dlq_months >= 6 OR zero_bal_code IN ('02', '03', '09', '15') GROUP BY 1) d USING (vintage)
GROUP BY s.vintage ORDER BY 1;

-- ---------------------------------------------------------------- 4.4 portfolio monthly
DROP TABLE IF EXISTS m_portfolio_monthly;
CREATE TABLE m_portfolio_monthly AS
SELECT report_month,
       count(*) AS rows_total,
       count(*) FILTER (WHERE NOT is_active) AS exit_rows,
       count(*) FILTER (WHERE is_active) AS active_loans,
       count(*) FILTER (WHERE is_active AND dlq_months IS NULL) AS active_unknown,
       count(*) FILTER (WHERE is_active AND dlq_months = 0) AS n_current,
       count(*) FILTER (WHERE is_active AND dlq_months = 1) AS n_30_59,
       count(*) FILTER (WHERE is_active AND dlq_months = 2) AS n_60_89,
       count(*) FILTER (WHERE is_active AND dlq_months BETWEEN 3 AND 5) AS n_90_179,
       count(*) FILTER (WHERE is_active AND dlq_months >= 6) AS n_180plus,
       round(100.0 * count(*) FILTER (WHERE is_active AND dlq_months >= 1)
             / nullif(count(*) FILTER (WHERE is_active AND dlq_months IS NOT NULL), 0), 3) AS pct_30plus,
       round(100.0 * count(*) FILTER (WHERE is_active AND dlq_months >= 3)
             / nullif(count(*) FILTER (WHERE is_active AND dlq_months IS NOT NULL), 0), 3) AS pct_90plus,
       round(100.0 * count(*) FILTER (WHERE is_active AND dlq_months >= 6)
             / nullif(count(*) FILTER (WHERE is_active AND dlq_months IS NOT NULL), 0), 3) AS pct_180plus,
       round(100.0 * sum(orig_upb) FILTER (WHERE is_active AND dlq_months >= 3)
             / nullif(sum(orig_upb) FILTER (WHERE is_active AND dlq_months IS NOT NULL), 0), 3) AS pct_90plus_origbal
FROM loan_month_clean GROUP BY 1 ORDER BY 1;

SELECT 'R4. portfolio monthly: totals reconcile to loan_month_clean' AS check_name;
SELECT (SELECT sum(rows_total) FROM m_portfolio_monthly) AS monthly_rows,
       (SELECT count(*) FROM loan_month_clean) AS clean_rows,
       (SELECT sum(active_loans + exit_rows - rows_total) FROM m_portfolio_monthly) AS active_plus_exit_minus_total,
       (SELECT sum(active_loans - active_unknown - n_current - n_30_59 - n_60_89 - n_90_179 - n_180plus)
          FROM m_portfolio_monthly) AS bucket_sum_diff;
SELECT 'Latest month snapshot' AS check_name;
SELECT * FROM m_portfolio_monthly ORDER BY report_month DESC LIMIT 1;

-- ---------------------------------------------------------------- 4.5 vintage curves
DROP TABLE IF EXISTS m_vintage_curve;
CREATE TABLE m_vintage_curve AS
WITH base AS (
  SELECT vintage, count(*) AS loans, sum(orig_upb) AS orig_upb,
         date_diff('month', max(orig_month), (SELECT max(report_month) FROM loan_month_clean)) AS horizon
  FROM loan_summary WHERE in_vintage GROUP BY 1),
grid AS (SELECT vintage, loans, orig_upb, unnest(range(0, horizon + 1)) AS mob FROM base),
d AS (SELECT vintage, default_mob AS mob, count(*) AS n, sum(orig_upb) AS ub
      FROM loan_summary WHERE in_vintage AND default_mob IS NOT NULL GROUP BY 1, 2),
s AS (SELECT vintage, first_90_mob AS mob, count(*) AS n
      FROM loan_summary WHERE in_vintage AND first_90_mob IS NOT NULL GROUP BY 1, 2),
p AS (SELECT vintage, exit_mob AS mob, count(*) AS n
      FROM loan_summary WHERE in_vintage AND exit_code = '01' GROUP BY 1, 2)
SELECT g.vintage, g.mob, g.loans,
       sum(coalesce(d.n, 0)) OVER w AS cum_default_loans,
       round(100.0 * sum(coalesce(d.n, 0)) OVER w / g.loans, 4) AS cum_default_pct,
       round(100.0 * sum(coalesce(d.ub, 0)) OVER w / g.orig_upb, 4) AS cum_default_pct_origbal,
       round(100.0 * sum(coalesce(s.n, 0)) OVER w / g.loans, 4) AS cum_ever90_pct,
       round(100.0 * sum(coalesce(p.n, 0)) OVER w / g.loans, 4) AS cum_prepaid_pct
FROM grid g
LEFT JOIN d ON d.vintage = g.vintage AND d.mob = g.mob
LEFT JOIN s ON s.vintage = g.vintage AND s.mob = g.mob
LEFT JOIN p ON p.vintage = g.vintage AND p.mob = g.mob
WINDOW w AS (PARTITION BY g.vintage ORDER BY g.mob)
ORDER BY g.vintage, g.mob;

SELECT 'R5. vintage curve end-point vs direct count (clean cohort, up to common horizon)' AS check_name;
SELECT c.vintage, max(c.mob) AS horizon_mob, max(c.loans) AS loans,
       max(c.cum_default_loans) AS curve_defaults,
       (SELECT count(*) FROM loan_summary s WHERE s.vintage = c.vintage AND s.in_vintage
         AND s.default_mob <= max(c.mob)) AS direct_defaults,
       (SELECT count(*) FROM loan_summary s WHERE s.vintage = c.vintage AND s.in_vintage
         AND s.default_mob > max(c.mob)) AS defaults_after_horizon,
       max(c.cum_default_pct) AS final_cum_default_pct,
       max(c.cum_ever90_pct) AS final_cum_ever90_pct,
       max(c.cum_prepaid_pct) AS final_cum_prepaid_pct
FROM m_vintage_curve c GROUP BY c.vintage ORDER BY 1;

-- ---------------------------------------------------------------- 4.6 roll rates
DROP TABLE IF EXISTS trans_base;
CREATE TABLE trans_base AS
WITH x AS (
  SELECT loan_id, report_month, is_active, bucket, dlq_months,
         lead(report_month) OVER w AS nxt_month,
         lead(dlq_months) OVER w AS nxt_dlq,
         lead(bucket) OVER w AS nxt_bucket,
         lead(zero_bal_code) OVER w AS nxt_exit
  FROM loan_month_clean
  WINDOW w AS (PARTITION BY loan_id ORDER BY report_month))
SELECT loan_id, report_month, bucket AS from_bucket,
       CASE WHEN dlq_months IS NULL THEN 'from status unknown (XX)'
            WHEN nxt_month IS NULL AND report_month = (SELECT max(report_month) FROM loan_month_clean)
              THEN 'end of data (censored)'
            WHEN nxt_month IS NULL THEN 'loan vanished without exit code'
            WHEN date_diff('month', report_month, nxt_month) <> 1 THEN 'gap in monthly history'
            WHEN nxt_exit IS NULL AND nxt_dlq IS NULL THEN 'next status unknown (XX)'
            ELSE NULL END AS excl_reason,
       CASE WHEN nxt_exit IS NOT NULL THEN
              CASE WHEN nxt_exit = '01' THEN 'XP Exit prepaid/matured'
                   WHEN nxt_exit IN ('02', '03', '09', '15') THEN 'XC Exit credit event'
                   ELSE 'XO Exit other' END
            ELSE nxt_bucket END AS to_state
FROM x WHERE is_active;

SELECT 'R6. roll-rate base: every active row is either used or excluded for a stated reason' AS check_name;
SELECT coalesce(excl_reason, 'USED in matrix') AS row_status, count(*) AS rows_
FROM trans_base GROUP BY 1 ORDER BY 2 DESC;
SELECT (SELECT count(*) FROM trans_base) AS trans_rows,
       (SELECT count(*) FROM loan_month_clean WHERE is_active) AS active_rows,
       (SELECT count(*) FROM trans_base) - (SELECT count(*) FROM loan_month_clean WHERE is_active) AS diff_must_be_0;

DROP TABLE IF EXISTS m_roll_matrix;
CREATE TABLE m_roll_matrix AS
WITH c AS (
  SELECT CAST(year(report_month) AS VARCHAR) AS calendar_year, from_bucket, to_state, count(*) AS loans
  FROM trans_base WHERE excl_reason IS NULL GROUP BY ALL
  UNION ALL
  SELECT 'ALL', from_bucket, to_state, count(*)
  FROM trans_base WHERE excl_reason IS NULL GROUP BY ALL)
SELECT *, round(100.0 * loans / sum(loans) OVER (PARTITION BY calendar_year, from_bucket), 3) AS pct_of_from
FROM c ORDER BY calendar_year, from_bucket, to_state;

SELECT 'R7. roll matrix: ALL total = used rows; each from-bucket sums to 100%' AS check_name;
SELECT (SELECT sum(loans) FROM m_roll_matrix WHERE calendar_year = 'ALL') AS matrix_total,
       (SELECT count(*) FROM trans_base WHERE excl_reason IS NULL) AS used_rows,
       (SELECT max(abs(t - 100)) FROM (SELECT sum(pct_of_from) AS t FROM m_roll_matrix
         GROUP BY calendar_year, from_bucket)) AS max_pct_sum_error;
SELECT 'Roll matrix, all years (row % = where loans in each bucket went next month)' AS check_name;
PIVOT (SELECT from_bucket, to_state, pct_of_from FROM m_roll_matrix WHERE calendar_year = 'ALL')
ON to_state USING first(pct_of_from) GROUP BY from_bucket ORDER BY from_bucket;

-- ---------------------------------------------------------------- 4.7 cure and outcomes
DROP TABLE IF EXISTS m_cure_rate;
CREATE TABLE m_cure_rate AS
SELECT calendar_year, from_bucket, sum(loans) AS loans,
       sum(loans) FILTER (WHERE to_state = 'B0 Current') AS cured_next_month,
       round(100.0 * sum(loans) FILTER (WHERE to_state = 'B0 Current') / sum(loans), 2) AS cure_pct_1m
FROM m_roll_matrix
WHERE from_bucket IN ('B1 30-59', 'B2 60-89', 'B3 90-179', 'B4 180+')
GROUP BY ALL ORDER BY 1, 2;
SELECT 'One-month cure rate (all years)' AS check_name;
SELECT * FROM m_cure_rate WHERE calendar_year = 'ALL' ORDER BY from_bucket;

DROP TABLE IF EXISTS m_sdq_outcomes;
CREATE TABLE m_sdq_outcomes AS
WITH o AS (
  SELECT vintage, cured_after_90,
         CASE WHEN exit_code IN ('02', '03', '09', '15') THEN '1 Credit-event exit'
              WHEN exit_code = '01' THEN '2 Prepaid/matured'
              WHEN exit_code IS NOT NULL THEN '3 Other exit (06/16)'
              WHEN last_status = 0 THEN '4 Active and current'
              ELSE '5 Active, still delinquent' END AS outcome
  FROM loan_summary WHERE in_vintage AND first_90 IS NOT NULL)
SELECT vintage, outcome, count(*) AS loans,
       round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY vintage), 2) AS pct_of_ever90,
       count(*) FILTER (WHERE cured_after_90) AS of_which_returned_to_current_at_some_point
FROM o GROUP BY vintage, outcome ORDER BY vintage, outcome;

SELECT 'R8. outcomes of loans that ever reached 90+ (clean cohort); totals reconcile to loan_summary' AS check_name;
SELECT * FROM m_sdq_outcomes;
SELECT (SELECT sum(loans) FROM m_sdq_outcomes) AS outcome_total,
       (SELECT count(*) FROM loan_summary WHERE in_vintage AND first_90 IS NOT NULL) AS ever90_loans,
       (SELECT sum(loans) FROM m_sdq_outcomes) -
       (SELECT count(*) FROM loan_summary WHERE in_vintage AND first_90 IS NOT NULL) AS diff_must_be_0;

-- ---------------------------------------------------------------- wrap-up
DROP TABLE trans_base;
SELECT 'DONE. Tables built:' AS check_name;
SELECT 'loan_static' AS table_name, count(*) AS row_count FROM loan_static
UNION ALL SELECT 'loan_month_clean', count(*) FROM loan_month_clean
UNION ALL SELECT 'loan_summary', count(*) FROM loan_summary
UNION ALL SELECT 'm_portfolio_monthly', count(*) FROM m_portfolio_monthly
UNION ALL SELECT 'm_vintage_curve', count(*) FROM m_vintage_curve
UNION ALL SELECT 'm_roll_matrix', count(*) FROM m_roll_matrix
UNION ALL SELECT 'm_cure_rate', count(*) FROM m_cure_rate
UNION ALL SELECT 'm_sdq_outcomes', count(*) FROM m_sdq_outcomes;
