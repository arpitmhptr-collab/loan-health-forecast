"""Load Fannie Mae SF Loan Performance CSVs into DuckDB table loan_month.

Run from the project root:
    caffeinate -i python python/01_load_raw.py

Optional args: python python/01_load_raw.py <raw_dir> <db_path>

Source layout: pipe-delimited, NO header, 114 columns (column 1 is blank).
Column position N in the Fannie Mae glossary = column N here.
All columns are read as text and converted in the SELECT, so a bad value
becomes NULL instead of crashing the load (Step 3 counts those NULLs).
"""
import glob
import os
import sys
import time

import duckdb

RAW = sys.argv[1] if len(sys.argv) > 1 else "data/raw"
DB = sys.argv[2] if len(sys.argv) > 2 else "data/processed/loans.duckdb"

# Row counts from `wc -l` (your Step 2 output). Used for reconciliation.
EXPECTED = {
    "2017Q1": 30547779,
    "2018Q1": 23902313,
    "2019Q1": 11962021,
    "2020Q1": 28284929,
}

N_COLS = 114  # 113 layout fields + 1 trailing delimiter

# position -> (output name, kind)   kind: s=text, d=double, i=int, m=MMYYYY date
KEEP = {
    2: ("loan_id", "s"),
    3: ("report_month", "m"),
    4: ("channel", "s"),
    5: ("seller_name", "s"),
    6: ("servicer_name", "s"),
    8: ("orig_rate", "d"),
    9: ("current_rate", "d"),
    10: ("orig_upb", "d"),
    12: ("current_upb", "d"),
    13: ("orig_term", "i"),
    14: ("orig_month", "m"),
    15: ("first_pay_month", "m"),
    16: ("loan_age", "i"),
    18: ("remaining_months", "i"),
    19: ("maturity_month", "m"),
    20: ("orig_ltv", "d"),
    21: ("orig_cltv", "d"),
    22: ("n_borrowers", "i"),
    23: ("dti", "d"),
    24: ("credit_score", "i"),
    26: ("first_time_buyer", "s"),
    27: ("loan_purpose", "s"),
    28: ("property_type", "s"),
    29: ("n_units", "i"),
    30: ("occupancy", "s"),
    31: ("state", "s"),
    34: ("mi_pct", "d"),
    35: ("amort_type", "s"),
    40: ("dlq_raw", "s"),
    42: ("mod_flag", "s"),
    44: ("zero_bal_code", "s"),
    45: ("zero_bal_month", "m"),
    46: ("upb_at_removal", "d"),
    51: ("last_paid_inst_month", "m"),
    52: ("foreclosure_month", "m"),
    53: ("disposition_month", "m"),
}


def select_list(quarter):
    parts = [f"'{quarter}' AS src_quarter"]
    for pos in sorted(KEEP):
        name, kind = KEEP[pos]
        f = f"f{pos}"
        if kind == "d":
            parts.append(f"TRY_CAST({f} AS DOUBLE) AS {name}")
        elif kind == "i":
            parts.append(f"TRY_CAST({f} AS INTEGER) AS {name}")
        elif kind == "m":
            parts.append(f"CAST(try_strptime({f}, '%m%Y') AS DATE) AS {name}")
        else:
            parts.append(f"{f} AS {name}")
        if name == "dlq_raw":
            parts.append("TRY_CAST(f40 AS INTEGER) AS dlq_months")
    return ",\n  ".join(parts)


def columns_clause():
    cols = ", ".join(f"'f{i}': 'VARCHAR'" for i in range(1, N_COLS + 1))
    return "{" + cols + "}"


def main():
    files = sorted(glob.glob(os.path.join(RAW, "*.csv")))
    if not files:
        sys.exit(f"No CSV files found in {RAW}")
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    con = duckdb.connect(DB)
    con.execute("SET memory_limit='6GB'")
    con.execute("SET preserve_insertion_order=false")
    tmp = os.path.join(os.path.dirname(DB) or ".", "tmp")
    con.execute(f"SET temp_directory='{tmp}'")
    con.execute("DROP TABLE IF EXISTS loan_month")

    cols = columns_clause()
    first = True
    for path in files:
        quarter = os.path.splitext(os.path.basename(path))[0]
        t0 = time.time()
        src = (
            f"FROM read_csv('{path}', delim='|', header=false, quote='', "
            f"escape='', null_padding=true, columns={cols})"
        )
        body = f"SELECT\n  {select_list(quarter)}\n{src}"
        if first:
            con.execute(f"CREATE TABLE loan_month AS {body}")
            first = False
        else:
            con.execute(f"INSERT INTO loan_month {body}")
        n, loans = con.execute(
            "SELECT count(*), count(DISTINCT loan_id) FROM loan_month "
            "WHERE src_quarter = ?",
            [quarter],
        ).fetchone()
        exp = EXPECTED.get(quarter)
        status = "n/a" if exp is None else ("OK" if exp == n else f"MISMATCH (wc -l = {exp:,})")
        print(
            f"{quarter}: rows={n:,} loans={loans:,} reconcile={status} "
            f"({time.time() - t0:.0f}s)",
            flush=True,
        )

    total = con.execute("SELECT count(*) FROM loan_month").fetchone()[0]
    exp_total = sum(EXPECTED.get(os.path.splitext(os.path.basename(p))[0], 0) for p in files)
    print(f"TOTAL rows in table = {total:,} | sum of wc -l = {exp_total:,}")
    con.close()


if __name__ == "__main__":
    main()
