-- Step 5a: does CALENDAR TIME (e.g. COVID years) matter, or only loan AGE? Read-only.
-- Run from project root:
--   duckdb data/processed/loans.duckdb < sql/05a_calendar_check.sql | tee docs/calendar_check_output.txt
-- Hazard = events in a month / loans still at risk that month (average % per loan-month).
-- Clean cohort only (in_vintage). Default = 180+ or credit exit; first-90 = first month at 90+.

SET memory_limit = '6GB';
SET temp_directory = 'data/processed/tmp';

CREATE TEMP TABLE hz AS
SELECT m.vintage,
       year(m.report_month) AS cal_year,
       m.mob // 12 AS age_year,
       count(*) FILTER (WHERE s.default_month IS NULL OR m.report_month <= s.default_month) AS at_risk_def,
       count(*) FILTER (WHERE m.report_month = s.default_month) AS def_events,
       count(*) FILTER (WHERE s.first_90 IS NULL OR m.report_month <= s.first_90) AS at_risk_90,
       count(*) FILTER (WHERE m.report_month = s.first_90) AS ev_90
FROM loan_month_clean m JOIN loan_summary s USING (loan_id)
WHERE m.in_vintage
GROUP BY ALL;

SELECT 'R9. events in hazard table reconcile to loan_summary (clean cohort)' AS check_name;
SELECT h.vintage, h.def_events, s.n AS defaults_in_summary, h.def_events - s.n AS diff_must_be_0,
       h.ev_90, s.n90 AS ever90_in_summary, h.ev_90 - s.n90 AS diff90_must_be_0
FROM (SELECT vintage, sum(def_events) AS def_events, sum(ev_90) AS ev_90 FROM hz GROUP BY 1) h
JOIN (SELECT vintage, count(*) FILTER (WHERE default_month IS NOT NULL) AS n,
             count(*) FILTER (WHERE first_90 IS NOT NULL) AS n90
      FROM loan_summary WHERE in_vintage GROUP BY 1) s USING (vintage)
ORDER BY 1;

SELECT 'A. Roll-forward and cure rates by calendar year (% of loan-months, from m_roll_matrix)' AS check_name;
SELECT calendar_year,
       max(pct_of_from) FILTER (WHERE from_bucket = 'B0 Current' AND to_state = 'B1 30-59') AS current_to_30,
       max(pct_of_from) FILTER (WHERE from_bucket = 'B1 30-59' AND to_state = 'B2 60-89') AS b30_to_60,
       max(pct_of_from) FILTER (WHERE from_bucket = 'B2 60-89' AND to_state = 'B3 90-179') AS b60_to_90,
       max(pct_of_from) FILTER (WHERE from_bucket = 'B3 90-179' AND to_state = 'B4 180+') AS b90_to_180,
       max(pct_of_from) FILTER (WHERE from_bucket = 'B1 30-59' AND to_state = 'B0 Current') AS cure_30,
       max(pct_of_from) FILTER (WHERE from_bucket = 'B2 60-89' AND to_state = 'B0 Current') AS cure_60,
       max(pct_of_from) FILTER (WHERE from_bucket = 'B3 90-179' AND to_state = 'B0 Current') AS cure_90
FROM m_roll_matrix GROUP BY 1 ORDER BY 1;

SELECT 'B. First-90+ hazard, % per loan-month: rows = calendar year, columns = vintage' AS check_name;
PIVOT (SELECT vintage, cal_year, round(100.0 * sum(ev_90) / nullif(sum(at_risk_90), 0), 4) AS h
       FROM hz GROUP BY ALL)
ON vintage USING first(h) GROUP BY cal_year ORDER BY cal_year;

SELECT 'C. Default hazard, % per loan-month: rows = calendar year, columns = vintage' AS check_name;
PIVOT (SELECT vintage, cal_year, round(100.0 * sum(def_events) / nullif(sum(at_risk_def), 0), 4) AS h
       FROM hz GROUP BY ALL)
ON vintage USING first(h) GROUP BY cal_year ORDER BY cal_year;

SELECT 'D. Default hazard, % per loan-month: rows = loan age in years (0 = first 12 months), columns = vintage' AS check_name;
PIVOT (SELECT vintage, age_year, round(100.0 * sum(def_events) / nullif(sum(at_risk_def), 0), 4) AS h
       FROM hz GROUP BY ALL)
ON vintage USING first(h) GROUP BY age_year ORDER BY age_year;
