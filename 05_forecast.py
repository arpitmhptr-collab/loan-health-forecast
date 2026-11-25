"""Step 5: forecast defaults by vintage and validate out of time.

Run from the project root, after sql/05b_hazard_cells.sql:
    python python/05_forecast.py --cutoff 2023-06     # holdout: July 2023 to end of data
    python python/05_forecast.py --cutoff 2024-06     # holdout: July 2024 to end of data
    python python/05_forecast.py --forward 24         # forecast 24 months past the data (no actuals)

Methods (all use the same exposure projection, so differences come from the default hazard only):
  naive_run_rate  : each vintage keeps its own average default rate of the last 12 training months.
  age_curve       : SIMPLE textbook method. Default rate depends only on loan age (6-month bands),
                    pooled over all vintages and all calendar months in training.
  age_x_calendar  : IMPROVEMENT. rate = age effect x calendar effect x vintage level.
                    Age and calendar effects are fitted together (Poisson, iterative scaling), so the
                    2020-21 shock is attributed to calendar time and not to loan age. The future
                    calendar effect is held at its average of the last 6 training months. The vintage
                    level is calibrated on the last 12 training months.
Exits other than default use each vintage's last-12-month run-rate in every method.
"""
import argparse

import duckdb
import numpy as np
import pandas as pd

BAND = 6  # months of loan age per band
RUN_WINDOW = 12  # months used for run-rates and vintage calibration
GAMMA_WINDOW = 6  # months used for the future calendar level
METHODS = ["naive_run_rate", "age_curve", "age_x_calendar"]


def tidx(ts):
    return ts.dt.year * 12 + ts.dt.month


def load(con):
    df = con.execute(
        "SELECT vintage, report_month, mob, at_risk, defaults, other_exits FROM hz_cell"
    ).df()
    df["report_month"] = pd.to_datetime(df["report_month"])
    df["t"] = tidx(df["report_month"])
    df["band"] = df["mob"] // BAND
    return df


def fit_age_calendar(train, last_t):
    g = train.groupby(["band", "t"])[["at_risk", "defaults"]].sum().reset_index()
    bands = np.sort(g["band"].unique())
    ts = np.sort(g["t"].unique())
    bi = {b: i for i, b in enumerate(bands)}
    ti = {t: i for i, t in enumerate(ts)}
    N = np.zeros((len(bands), len(ts)))
    D = np.zeros_like(N)
    for b, t, n, d in g.itertuples(index=False):
        N[bi[b], ti[t]] = n
        D[bi[b], ti[t]] = d
    alpha = np.divide(D.sum(1), N.sum(1), out=np.zeros(len(bands)), where=N.sum(1) > 0)
    gamma = np.ones(len(ts))
    recent = ts >= last_t - GAMMA_WINDOW + 1
    for _ in range(2000):
        den_a = (N * gamma[None, :]).sum(1)
        a_new = np.divide(D.sum(1), den_a, out=np.zeros_like(alpha), where=den_a > 0)
        den_g = (N * a_new[:, None]).sum(0)
        g_new = np.divide(D.sum(0), den_g, out=np.ones_like(gamma), where=den_g > 0)
        s = g_new[recent].mean()  # normalise: future calendar level = 1
        g_new, a_new = g_new / s, a_new * s
        done = np.max(np.abs(g_new - gamma)) < 1e-10 and np.max(np.abs(a_new - alpha)) < 1e-12
        alpha, gamma = a_new, g_new
        if done:
            break
    full = np.zeros(bands.max() + 1)
    last = 0.0
    for b in range(bands.max() + 1):  # forward-fill any missing band
        if b in bi:
            last = alpha[bi[b]]
        full[b] = last
    gam = dict(zip(ts, gamma))
    return full, gam


def alpha_at(full, band):
    return full[min(band, len(full) - 1)]


def project(n0, hazard_def, hazard_exit):
    out_d, out_n = [], []
    n = n0
    for hd in hazard_def:
        hd = min(max(hd, 0.0), 1.0)
        d = n * hd
        e = n * hazard_exit
        out_n.append(n)
        out_d.append(d)
        n = max(n - d - e, 0.0)
    return np.array(out_d), np.array(out_n)


def forecast_all(df, cutoff_t, horizon):
    train = df[df["t"] <= cutoff_t]
    win = train[train["t"] > cutoff_t - RUN_WINDOW]
    pooled = train.groupby("band")[["at_risk", "defaults"]].sum()
    alpha_pool = (pooled["defaults"] / pooled["at_risk"]).reindex(
        range(int(pooled.index.max()) + 1)
    ).ffill().to_numpy()
    full, gam = fit_age_calendar(train, cutoff_t)
    gamma_future = np.mean([gam[t] for t in sorted(gam) if t > cutoff_t - GAMMA_WINDOW])
    rows, checks = [], []
    for v, tv in train.groupby("vintage"):
        last = tv[tv["t"] == cutoff_t]
        if last.empty or last["at_risk"].sum() == 0:
            continue
        n_t, d_t, e_t = last[["at_risk", "defaults", "other_exits"]].sum()
        n0 = n_t - d_t - e_t  # identity: population at the first forecast month
        age_t = float((last["mob"] * last["at_risk"]).sum() / n_t)
        w = win[win["vintage"] == v]
        h_run = w["defaults"].sum() / w["at_risk"].sum()
        h_exit = w["other_exits"].sum() / w["at_risk"].sum()
        expected = sum(
            r.at_risk * alpha_at(full, r.band) * gam[r.t] for r in w.itertuples(index=False)
        )
        kappa = w["defaults"].sum() / expected
        ks = np.arange(1, horizon + 1)
        bands = ((age_t + ks) // BAND).astype(int)
        hz = {
            "naive_run_rate": np.full(horizon, h_run),
            "age_curve": np.array([alpha_at(alpha_pool, b) for b in bands]),
            "age_x_calendar": np.array([alpha_at(full, b) for b in bands]) * gamma_future * kappa,
        }
        months = [pd.Timestamp(year=(cutoff_t + k - 1) // 12, month=(cutoff_t + k - 1) % 12 + 1, day=1)
                  for k in ks]
        for m, h in hz.items():
            d, n = project(n0, h, h_exit)
            for mo, di, ni, hi in zip(months, d, n, h):
                rows.append((v, mo, m, float(di), float(ni), float(hi)))
        checks.append((v, n0, kappa))
    out = pd.DataFrame(rows, columns=["vintage", "report_month", "method", "pred_defaults", "pred_at_risk", "pred_hazard"])
    return out, checks, gamma_future


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/processed/loans.duckdb")
    ap.add_argument("--cutoff", help="last training month, e.g. 2023-06")
    ap.add_argument("--forward", type=int, help="forecast N months past the end of the data")
    a = ap.parse_args()
    con = duckdb.connect(a.db)
    df = load(con)
    end_t = int(df["t"].max())
    loans = con.execute(
        "SELECT vintage, count(*) AS loans FROM loan_summary WHERE in_vintage GROUP BY 1"
    ).df().set_index("vintage")["loans"]

    con.execute("""CREATE TABLE IF NOT EXISTS m_forecast_monthly(
        run_label VARCHAR, vintage VARCHAR, report_month DATE, method VARCHAR,
        pred_defaults DOUBLE, pred_at_risk DOUBLE, pred_hazard_pct DOUBLE,
        actual_defaults DOUBLE, actual_at_risk DOUBLE)""")
    con.execute("""CREATE TABLE IF NOT EXISTS m_forecast_validation(
        cutoff DATE, vintage VARCHAR, method VARCHAR, loans BIGINT,
        holdout_months INTEGER, actual_defaults DOUBLE, predicted_defaults DOUBLE,
        pct_error DOUBLE, monthly_mape_pct DOUBLE,
        actual_cum_default_pct DOUBLE, predicted_cum_default_pct DOUBLE)""")

    if a.forward:
        cutoff_t, horizon, label = end_t, a.forward, "forward"
    else:
        y, m = map(int, a.cutoff.split("-"))
        cutoff_t, label = y * 12 + m, f"holdout_{a.cutoff}"
        horizon = end_t - cutoff_t
        if horizon <= 0:
            raise SystemExit("cutoff must be before the last month of data")

    pred, checks, gfut = forecast_all(df, cutoff_t, horizon)
    print(f"Run: {label} | training through {cutoff_t // 12}-{(cutoff_t - 1) % 12 + 1:02d} | "
          f"horizon {horizon} months | future calendar level (gamma) = {gfut:.3f}")

    act = df.groupby(["vintage", "report_month"])[["at_risk", "defaults"]].sum().reset_index()
    pred = pred.merge(act.rename(columns={"defaults": "actual_defaults", "at_risk": "actual_at_risk"}),
                      on=["vintage", "report_month"], how="left")
    if a.forward:
        pred[["actual_defaults", "actual_at_risk"]] = np.nan

    con.execute("DELETE FROM m_forecast_monthly WHERE run_label = ?", [label])
    out = pred.assign(run_label=label)
    out["pred_hazard_pct"] = out["pred_hazard"] * 100
    con.register("o", out)
    con.execute("""INSERT INTO m_forecast_monthly
        SELECT run_label, vintage, CAST(report_month AS DATE), method, pred_defaults, pred_at_risk,
               pred_hazard_pct, actual_defaults, actual_at_risk FROM o""")

    if a.forward:
        s = pred.groupby(["vintage", "method"])["pred_defaults"].sum().unstack()
        print("\nPredicted additional defaults over the next", horizon, "months (loans):")
        print(s.round(0).to_string())
        return

    # reconciliation of the population identity at the first forecast month
    first = pd.Timestamp(year=(cutoff_t) // 12, month=(cutoff_t) % 12 + 1, day=1)
    print("\nPopulation check at first forecast month (projected vs actual at-risk loans):")
    for v, n0, kappa in checks:
        r = act[(act.vintage == v) & (act.report_month == first)]
        actual_n = float(r["at_risk"].iloc[0]) if len(r) else float("nan")
        print(f"  {v}: projected {n0:,.0f} | actual {actual_n:,.0f} | diff {n0 - actual_n:,.0f} | calibration kappa {kappa:.3f}")

    rows = []
    cum_train = df[df["t"] <= cutoff_t].groupby("vintage")["defaults"].sum()
    for (v, m), g in pred.groupby(["vintage", "method"]):
        ad, pdf = g["actual_defaults"].sum(), g["pred_defaults"].sum()
        mape = float(np.mean(np.abs(g["pred_defaults"] - g["actual_defaults"]) / g["actual_defaults"].replace(0, np.nan)) * 100)
        rows.append((v, m, ad, pdf, mape, (cum_train[v] + ad) / loans[v] * 100, (cum_train[v] + pdf) / loans[v] * 100, loans[v]))
    res = pd.DataFrame(rows, columns=["vintage", "method", "actual_defaults", "predicted_defaults",
                                      "monthly_mape_pct", "actual_cum_default_pct", "predicted_cum_default_pct", "loans"])
    tot = res.groupby("method").agg(actual_defaults=("actual_defaults", "sum"), predicted_defaults=("predicted_defaults", "sum"),
                                    loans=("loans", "sum"), monthly_mape_pct=("monthly_mape_pct", "mean")).reset_index()
    tot["vintage"] = "ALL"
    tot["actual_cum_default_pct"] = [
        (cum_train.sum() + r.actual_defaults) / r.loans * 100 for r in tot.itertuples()]
    tot["predicted_cum_default_pct"] = [
        (cum_train.sum() + r.predicted_defaults) / r.loans * 100 for r in tot.itertuples()]
    res = pd.concat([res, tot[res.columns]], ignore_index=True)
    res["pct_error"] = (res["predicted_defaults"] - res["actual_defaults"]) / res["actual_defaults"] * 100
    res["cutoff"] = pd.Timestamp(year=cutoff_t // 12, month=(cutoff_t - 1) % 12 + 1, day=1)
    res["holdout_months"] = horizon
    con.execute("DELETE FROM m_forecast_validation WHERE cutoff = ?", [res["cutoff"].iloc[0].date()])
    con.register("r", res)
    con.execute("""INSERT INTO m_forecast_validation
        SELECT CAST(cutoff AS DATE), vintage, method, loans, holdout_months, actual_defaults,
               predicted_defaults, pct_error, monthly_mape_pct, actual_cum_default_pct,
               predicted_cum_default_pct FROM r""")

    show = res.copy()
    show["method"] = pd.Categorical(show["method"], METHODS)
    show = show.sort_values(["vintage", "method"])
    print("\nHoldout validation (defaults in the holdout window; error = predicted vs actual):")
    print(show[["vintage", "method", "actual_defaults", "predicted_defaults", "pct_error",
                "monthly_mape_pct", "actual_cum_default_pct", "predicted_cum_default_pct"]]
          .round(2).to_string(index=False))


if __name__ == "__main__":
    main()
