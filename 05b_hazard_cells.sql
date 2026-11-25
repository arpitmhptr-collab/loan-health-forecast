-- Step 5b: compact table the forecast reads. One row per vintage x calendar month x months-on-book.
-- Run from project root (a few minutes; one pass over the 94M-row table):
--   duckdb data/processed/loans.duckdb < sql/05b_hazard_cells.sql | tee docs/hazard_cells_output.txt
--
-- at_risk     = loans not yet defaulted and still in the data that month (default month included)
-- defaults    = loans whose default month (180+ days or credit exit) is that month
-- other_exits = loans leaving that month without a default (mostly prepaid/matured)
-- Identity used by the forecast: at_risk(next month) = at_risk - defaults - other_exits
-- Clean cohort only (in_vintage).

SET memory_limit = '6GB';
SET temp_directory = 'data/processed/tmp';

DROP TABLE IF EXISTS hz_cell;
CREATE TABLE hz_cell AS
SELECT m.vintage, m.report_month, m.mob,
       count(*) FILTER (WHERE s.default_month IS NULL OR m.report_month <= s.default_month) AS at_risk,
       count(*) FILTER (WHERE m.report_month = s.default_month) AS defaults,
       count(*) FILTER (WHERE NOT m.is_active
                          AND (s.default_month IS NULL OR m.report_month < s.default_month)) AS other_exits
FROM loan_month_clean m JOIN loan_summary s USING (loan_id)
WHERE m.in_vintage
GROUP BY ALL;

SELECT 'R10. hz_cell reconciles to loan_summary (clean cohort)' AS check_name;
SELECT h.vintage, h.cells, h.defaults, s.defaults AS defaults_in_summary,
       h.defaults - s.defaults AS diff_defaults_must_be_0,
       h.other_exits, s.other_exits AS exits_in_summary,
       h.other_exits - s.other_exits AS diff_exits_must_be_0
FROM (SELECT vintage, count(*) AS cells, sum(defaults) AS defaults, sum(other_exits) AS other_exits
      FROM hz_cell GROUP BY 1) h
JOIN (SELECT vintage,
             count(*) FILTER (WHERE default_month IS NOT NULL) AS defaults,
             count(*) FILTER (WHERE exit_code IS NOT NULL AND default_month IS NULL) AS other_exits
      FROM loan_summary WHERE in_vintage GROUP BY 1) s USING (vintage)
ORDER BY 1;

SELECT 'R11. population identity: at_risk(t+1) = at_risk(t) - defaults(t) - other_exits(t), checked after the first 6 months of each vintage' AS check_name;
WITH v AS (SELECT vintage, report_month, sum(at_risk) AS n, sum(defaults) AS d, sum(other_exits) AS e
           FROM hz_cell GROUP BY 1, 2),
j AS (SELECT a.vintage, a.report_month, a.n - a.d - a.e AS expected_next, b.n AS actual_next
      FROM v a JOIN v b ON a.vintage = b.vintage AND b.report_month = a.report_month + INTERVAL 1 MONTH
      WHERE a.report_month >= (SELECT min(report_month) FROM v v2 WHERE v2.vintage = a.vintage) + INTERVAL 6 MONTH)
SELECT vintage, count(*) AS months_checked, sum(abs(expected_next - actual_next)) AS total_abs_diff,
       max(abs(expected_next - actual_next)) AS max_abs_diff
FROM j GROUP BY 1 ORDER BY 1;

SELECT 'Preview: first cells' AS check_name;
SELECT * FROM hz_cell ORDER BY vintage, report_month, mob LIMIT 5;
