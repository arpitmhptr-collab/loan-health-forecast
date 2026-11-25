"""Step 6: build ONE static dashboard page (dashboard/index.html) from the DuckDB result tables.

Run from the project root:
    python python/06_build_dashboard.py
    python python/06_build_dashboard.py --holdout 2024-06 --out dashboard/index.html

It reads only the aggregated tables we built (m_*, hz_cell, loan_static counts). No loan-level rows
are written to the page. Plotly is loaded from a CDN, so the HTML file stays small.
Light and dark themes are both built in (follows the device setting, with a toggle button).
"""
import argparse
import datetime as dt
import html
import json
import os

import duckdb

PLOTLY_URL = "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@3.8.2/plotly.min.js"
FONT = 'system-ui,-apple-system,"Segoe UI",sans-serif'

# Validated palette (categorical slots 1-5, ordinal blue ramp, sequential blue ramp) per mode.
THEME = {
    "light": dict(surface="#fcfcfb", ink="#0b0b0b", ink2="#52514e", muted="#898781", grid="#e1e0d9",
                  axis="#c3c2b7",
                  cat=["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"],
                  ordinal=["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"],
                  seq=["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"],
                  on_cat=["#ffffff", "#ffffff", "#ffffff", "#0b0b0b", "#ffffff"]),
    "dark": dict(surface="#1a1a19", ink="#f0efec", ink2="#c3c2b7", muted="#898781", grid="#2c2c2a",
                 axis="#383835",
                 cat=["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"],
                 ordinal=["#184f95", "#2a78d6", "#6da7ec", "#b7d3f6"],
                 seq=["#0d366b", "#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4", "#cde2fb"],
                 on_cat=["#ffffff", "#ffffff", "#ffffff", "#ffffff", "#ffffff"]),
}

BUCKET_LABEL = {"B0 Current": "Current", "B1 30-59": "30-59 days", "B2 60-89": "60-89 days",
                "B3 90-179": "90-179 days", "B4 180+": "180+ days"}
TO_ORDER = ["B0 Current", "B1 30-59", "B2 60-89", "B3 90-179", "B4 180+",
            "XP Exit prepaid/matured", "XC Exit credit event", "XO Exit other"]
TO_LABEL = {**BUCKET_LABEL, "XP Exit prepaid/matured": "Exit: prepaid / matured",
            "XC Exit credit event": "Exit: credit event", "XO Exit other": "Exit: other"}
MNAME = {"age_x_calendar": "age x calendar", "age_curve": "age-only", "naive_run_rate": "run-rate"}
OUTCOME_ORDER = ["Credit-event exit", "Prepaid/matured", "Other exit (06/16)", "Active and current",
                 "Active, still delinquent"]


# ------------------------------------------------------------------ small helpers
def esc(x):
    return html.escape(str(x))


def lum(hex_):
    h = hex_.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def text_on(hex_):
    return "#ffffff" if lum(hex_) < 0.18 else "#0b0b0b"


def rgba(hex_, a):
    h = hex_.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{a})"


def mlabel(d):
    return d.strftime("%b %Y")


def iso(d):
    return d.isoformat()


def table_view(caption, headers, rows):
    head = "".join(f'<th scope="col">{esc(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return (f'<details class="tv"><summary>View as table</summary><div class="tvwrap"><table>'
            f"<caption>{esc(caption)}</caption><thead><tr>{head}</tr></thead><tbody>{body}</tbody>"
            f"</table></div></details>")


def card(cid, title, sub, note, tv, height, label):
    return (f'<section class="card"><h3>{esc(title)}</h3><p class="sub">{esc(sub)}</p>'
            f'<div id="{cid}" class="chart" role="img" aria-label="{esc(label)}" '
            f'style="height:{height}px"></div><p class="note">{note}</p>{tv}</section>')


def base_layout(t, height, **over):
    th = THEME[t]
    lay = dict(
        height=height, paper_bgcolor=th["surface"], plot_bgcolor=th["surface"],
        font=dict(family=FONT, size=12, color=th["ink2"]),
        margin=dict(l=58, r=28, t=46, b=48),
        hoverlabel=dict(bgcolor=th["surface"], bordercolor=th["axis"], font=dict(family=FONT, color=th["ink"], size=12)),
        legend=dict(orientation="h", x=0, xanchor="left", y=1.0, yanchor="bottom",
                    font=dict(color=th["ink2"], size=12), bgcolor="rgba(0,0,0,0)"),
        xaxis=dict(showgrid=False, zeroline=False, linecolor=th["axis"], linewidth=1, ticks="",
                   tickfont=dict(color=th["muted"], size=11), automargin=True),
        yaxis=dict(gridcolor=th["grid"], gridwidth=1, zeroline=False, showline=False, ticks="",
                   tickfont=dict(color=th["muted"], size=11), automargin=True),
    )
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(lay.get(k), dict):
            lay[k] = {**lay[k], **v}
        else:
            lay[k] = v
    return lay


def axis_title(t, text):
    return dict(text=text, font=dict(size=11, color=THEME[t]["muted"]), standoff=8)


def line_trace(x, y, name, color, t, fmt="%{y:.2f}%", group=None, show=True, width=2):
    d = dict(type="scatter", mode="lines", x=x, y=y, name=name,
             line=dict(color=color, width=width, shape="linear"),
             hovertemplate=f"{fmt}<extra>{esc(name)}</extra>", showlegend=show)
    if group:
        d["legendgroup"] = group
    return d


def end_label(x, y, text, color, t, pos="middle right"):
    th = THEME[t]
    return dict(type="scatter", mode="markers+text", x=[x], y=[y], text=[text], textposition=pos,
                textfont=dict(color=th["ink2"], size=11), cliponaxis=False, showlegend=False,
                marker=dict(size=9, color=color, line=dict(color=th["surface"], width=2)),
                hoverinfo="skip")


# ------------------------------------------------------------------ data
def q(con, sql):
    return con.execute(sql).fetchall()


def load(con, holdout):
    D = {}
    D["n_loans"] = q(con, "SELECT count(*) FROM loan_static")[0][0]
    D["n_clean"] = q(con, "SELECT count(*) FROM loan_static WHERE in_vintage")[0][0]
    D["n_rows"] = q(con, "SELECT count(*) FROM loan_month_clean")[0][0]
    D["pm"] = q(con, "SELECT report_month, active_loans, pct_30plus, pct_90plus, pct_180plus "
                     "FROM m_portfolio_monthly ORDER BY 1")
    D["vc"] = q(con, "SELECT vintage, mob, cum_default_pct, cum_ever90_pct, cum_prepaid_pct "
                     "FROM m_vintage_curve ORDER BY vintage, mob")
    D["hz"] = q(con, "SELECT vintage, CAST(year(report_month) AS INTEGER), "
                     "100.0 * sum(defaults) / sum(at_risk) FROM hz_cell GROUP BY 1, 2 ORDER BY 1, 2")
    D["actual"] = q(con, "SELECT report_month, sum(defaults) FROM hz_cell GROUP BY 1 ORDER BY 1")
    D["roll"] = q(con, "SELECT from_bucket, to_state, pct_of_from FROM m_roll_matrix WHERE calendar_year = 'ALL'")
    D["cure"] = q(con, "SELECT calendar_year, from_bucket, cure_pct_1m FROM m_cure_rate "
                       "WHERE calendar_year <> 'ALL' ORDER BY 1")
    D["out"] = q(con, "SELECT vintage, outcome, loans, pct_of_ever90 FROM m_sdq_outcomes ORDER BY vintage, outcome")
    D["seg"] = q(con, "SELECT segment, pct_of_loan_months, pct_of_defaults, default_annualized_pct, default_events "
                      "FROM m_segment_post_shock WHERE dimension = 'credit_band' ORDER BY segment")
    D["segx"] = q(con, "SELECT credit_band, ltv_band, default_annualized_pct, default_events "
                       "FROM m_segment_credit_ltv ORDER BY credit_band, ltv_band")
    try:
        D["fc"] = q(con, "SELECT run_label, report_month, method, sum(pred_defaults), sum(actual_defaults) "
                         "FROM m_forecast_monthly GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")
        D["val"] = q(con, "SELECT cutoff, vintage, method, actual_defaults, predicted_defaults, pct_error, "
                          "monthly_mape_pct FROM m_forecast_validation ORDER BY cutoff, vintage, method")
    except duckdb.Error:
        D["fc"], D["val"] = [], []
    D["holdout"] = holdout
    return D


# ------------------------------------------------------------------ figures
def fig_health(D, t):
    th = THEME[t]
    x = [iso(r[0]) for r in D["pm"]]
    y30 = [r[2] for r in D["pm"]]
    y90 = [r[3] for r in D["pm"]]
    data = [line_trace(x, y30, "30+ days past due", th["cat"][0], t),
            line_trace(x, y90, "90+ days past due", th["cat"][1], t),
            end_label(x[-1], y30[-1], f"{y30[-1]:.2f}%", th["cat"][0], t),
            end_label(x[-1], y90[-1], f"{y90[-1]:.2f}%", th["cat"][1], t)]
    lay = base_layout(t, 340, margin=dict(r=70),
                      hovermode="x unified",
                      xaxis=dict(showspikes=True, spikemode="across", spikethickness=1, spikecolor=th["axis"],
                                 spikedash="solid", spikesnap="cursor"),
                      yaxis=dict(ticksuffix="%", title=axis_title(t, "% of active loans")),
                      shapes=[dict(type="rect", xref="x", yref="paper", x0="2020-03-01", x1="2021-12-31", y0=0,
                                   y1=1, fillcolor=rgba(th["ink"], 0.06), line=dict(width=0), layer="below")],
                      annotations=[dict(xref="x", yref="paper", x="2020-03-01", y=0.98, xanchor="left",
                                        yanchor="top", text="2020-21", showarrow=False,
                                        font=dict(size=11, color=th["muted"]))])
    return dict(data=data, layout=lay)


def fig_vintage(D, t):
    th = THEME[t]
    by = {}
    for v, mob, cd, e90, pp in D["vc"]:
        by.setdefault(v, []).append((mob, cd))
    data, shapes = [], []
    vints = sorted(by)
    common = min(max(m for m, _ in by[v]) for v in vints)
    for i, v in enumerate(vints):
        xs = [m for m, _ in by[v]]
        ys = [c for _, c in by[v]]
        data.append(line_trace(xs, ys, v, th["cat"][i], t, fmt="%{y:.2f}%"))
    for i, v in enumerate(vints):
        xs = [m for m, _ in by[v]]
        ys = [c for _, c in by[v]]
        pos = "top right" if v in ("2019Q1", "2020Q1") else "bottom right"
        data.append(end_label(xs[-1], ys[-1], f"{v}  {ys[-1]:.2f}%", th["cat"][i], t, pos))
    shapes.append(dict(type="line", xref="x", yref="paper", x0=common, x1=common, y0=0, y1=1,
                       line=dict(color=th["axis"], width=1)))
    lay = base_layout(t, 380, margin=dict(r=110), hovermode="x unified",
                      xaxis=dict(title=axis_title(t, "Months since origination"), range=[0, max(
                          max(m for m, _ in by[v]) for v in vints) + 12],
                                 showspikes=True, spikemode="across", spikethickness=1, spikecolor=th["axis"],
                                 spikedash="solid"),
                      yaxis=dict(ticksuffix="%", title=axis_title(t, "Cumulative default, % of loans")),
                      shapes=shapes,
                      annotations=[dict(xref="x", yref="paper", x=common, y=0.02, xanchor="right", yanchor="bottom",
                                        text=f"Month {common}: all four observed", showarrow=False,
                                        font=dict(size=11, color=th["muted"]))])
    return dict(data=data, layout=lay), common, by


def fig_calendar(D, t):
    th = THEME[t]
    by = {}
    for v, y, h in D["hz"]:
        by.setdefault(v, []).append((y, h))
    vints = sorted(by)
    data = []
    for i, v in enumerate(vints):
        data.append(line_trace([y for y, _ in by[v]], [h for _, h in by[v]], v, th["cat"][i], t,
                               fmt="%{y:.3f}%"))
    pk = max(((h, v, y) for v in vints for y, h in by[v]))
    data.append(dict(type="scatter", mode="markers+text", x=[pk[2]], y=[pk[0]], showlegend=False,
                     text=[f"{pk[1]} peak {pk[0]:.2f}%"], textposition="middle right", cliponaxis=False,
                     textfont=dict(color=th["ink2"], size=11), hoverinfo="skip",
                     marker=dict(size=9, color=th["cat"][vints.index(pk[1])], line=dict(color=th["surface"], width=2))))
    years = sorted({y for v in vints for y, _ in by[v]})
    lay = base_layout(t, 340, margin=dict(r=110), hovermode="x unified",
                      xaxis=dict(dtick=1, range=[years[0] - 0.3, years[-1] + 0.3],
                                 showspikes=True, spikemode="across", spikethickness=1, spikecolor=th["axis"],
                                 spikedash="solid"),
                      yaxis=dict(ticksuffix="%", title=axis_title(t, "Defaults per month, % of at-risk loans")))
    return dict(data=data, layout=lay), by, years


def fig_roll(D, t):
    th = THEME[t]
    rows = [r for r in TO_ORDER[:5]]
    val = {(a, b): c for a, b, c in D["roll"]}
    z = [[val.get((r, c), 0.0) for c in TO_ORDER] for r in rows]
    n = len(th["seq"])
    scale = [[i / (n - 1), c] for i, c in enumerate(th["seq"])]
    SHORT = {"B0 Current": "Current", "B1 30-59": "30-59", "B2 60-89": "60-89", "B3 90-179": "90-179",
             "B4 180+": "180+", "XP Exit prepaid/matured": "Exit:<br>prepaid", "XC Exit credit event": "Exit:<br>credit",
             "XO Exit other": "Exit:<br>other"}
    data = [dict(type="heatmap", z=z, x=[SHORT[c] for c in TO_ORDER], y=[BUCKET_LABEL[r] for r in rows],
                 colorscale=scale, zmin=0, zmax=100, xgap=2, ygap=2,
                 hovertemplate="From %{y} to %{x}: %{z:.2f}%<extra></extra>",
                 colorbar=dict(thickness=10, len=0.9, ticksuffix="%", outlinewidth=0,
                               tickfont=dict(color=th["muted"], size=11)))]
    ann = []
    for i, r in enumerate(rows):
        for j, c in enumerate(TO_ORDER):
            v = z[i][j]
            if v >= 1:
                frac = min(max(v / 100, 0), 1)
                col = th["seq"][min(int(frac * (n - 1) + 0.5), n - 1)]
                ann.append(dict(x=SHORT[c], y=BUCKET_LABEL[r], text=f"{v:.1f}%", showarrow=False,
                                font=dict(size=11, color=text_on(col))))
    lay = base_layout(t, 340, margin=dict(t=70, l=100, r=20),
                      xaxis=dict(side="top", tickfont=dict(color=th["ink2"], size=11), tickangle=0),
                      yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)", tickfont=dict(color=th["ink2"], size=12),
                                 title=axis_title(t, "This month")),
                      annotations=ann)
    return dict(data=data, layout=lay), z


def fig_cure(D, t):
    th = THEME[t]
    by = {}
    for yr, b, c in D["cure"]:
        by.setdefault(b, []).append((int(yr), c))
    names = [("B1 30-59", "From 30-59 days"), ("B2 60-89", "From 60-89 days"), ("B3 90-179", "From 90-179 days")]
    data = []
    for i, (b, nm) in enumerate(names):
        data.append(line_trace([y for y, _ in by[b]], [c for _, c in by[b]], nm, th["cat"][i], t, fmt="%{y:.1f}%"))
    for i, (b, nm) in enumerate(names):
        y, c = by[b][-1]
        data.append(end_label(y, c, f"{c:.1f}%", th["cat"][i], t))
    years = [y for y, _ in by["B1 30-59"]]
    lay = base_layout(t, 330, margin=dict(r=70), hovermode="x unified",
                      xaxis=dict(dtick=1, range=[years[0] - 0.3, years[-1] + 0.3], showspikes=True,
                                 spikemode="across", spikethickness=1, spikecolor=th["axis"], spikedash="solid"),
                      yaxis=dict(ticksuffix="%", rangemode="tozero",
                                 title=axis_title(t, "% fully current next month")))
    return dict(data=data, layout=lay), by


def fig_outcomes(D, t):
    th = THEME[t]
    vints = sorted({r[0] for r in D["out"]})
    val = {(r[0], r[1].split(" ", 1)[1]): r[3] for r in D["out"]}
    data = []
    for i, name in enumerate(OUTCOME_ORDER):
        xs = [val.get((v, name), 0.0) for v in vints]
        data.append(dict(type="bar", orientation="h", name=name, x=xs, y=vints,
                         marker=dict(color=th["cat"][i], line=dict(color=th["surface"], width=2), cornerradius=4),
                         text=[f"{p:.0f}%" if p >= 9 else "" for p in xs], textposition="inside",
                         insidetextanchor="start", textfont=dict(color=th["on_cat"][i], size=11),
                         hovertemplate="%{y}: %{x:.1f}% of loans that reached 90+<extra>" + esc(name) + "</extra>"))
    lay = base_layout(t, 310, barmode="stack", bargap=0.5, margin=dict(l=70, t=62),
                      legend=dict(y=1.0, traceorder="normal"),
                      xaxis=dict(range=[0, 100], ticksuffix="%", showgrid=False),
                      yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)", tickfont=dict(color=th["ink2"], size=12)))
    return dict(data=data, layout=lay), vints, val


def fig_forecast(D, t):
    th = THEME[t]
    act = [(r[0], r[1]) for r in D["actual"] if r[0] >= dt.date(2023, 1, 1)]
    data = [line_trace([iso(d) for d, _ in act], [v for _, v in act], "Actual defaults", th["cat"][0], t,
                       fmt="%{y:,.0f}", group="a")]
    methods = [("age_x_calendar", "Age x calendar model", 1), ("naive_run_rate", "Run-rate benchmark", 2)]
    hold = f"holdout_{D['holdout']}"
    shapes, ann = [], []
    for m, nm, ci in methods:
        first = True
        for run in (hold, "forward"):
            pts = [(r[1], r[3]) for r in D["fc"] if r[0] == run and r[2] == m]
            if pts:
                data.append(line_trace([iso(d) for d, _ in pts], [v for _, v in pts], nm, th["cat"][ci], t,
                                       fmt="%{y:,.0f}", group=m, show=first))
                first = False
    fwd = [r[1] for r in D["fc"] if r[0] == "forward"]
    holds = [r[1] for r in D["fc"] if r[0] == hold]
    if holds:
        s = min(holds)
        shapes.append(dict(type="line", xref="x", yref="paper", x0=iso(s), x1=iso(s), y0=0, y1=1,
                           line=dict(color=th["axis"], width=1)))
        ann.append(dict(xref="x", yref="paper", x=iso(s), y=0.98, xanchor="left", yanchor="top",
                        text=f"Backtest: model trained to {(s - dt.timedelta(days=1)).strftime('%b %Y')}",
                        showarrow=False, font=dict(size=11, color=th["muted"])))
    if fwd:
        s = min(fwd)
        e = max(fwd)
        shapes.append(dict(type="rect", xref="x", yref="paper", x0=iso(s), x1=iso(e), y0=0, y1=1,
                           fillcolor=rgba(th["ink"], 0.06), line=dict(width=0), layer="below"))
        ann.append(dict(xref="x", yref="paper", x=iso(s), y=0.98, xanchor="left", yanchor="top",
                        text="Forecast", showarrow=False, font=dict(size=11, color=th["muted"])))
    lay = base_layout(t, 360, hovermode="x unified", shapes=shapes, annotations=ann,
                      xaxis=dict(showspikes=True, spikemode="across", spikethickness=1, spikecolor=th["axis"],
                                 spikedash="solid"),
                      yaxis=dict(rangemode="tozero", title=axis_title(t, "Defaults per month (loans)")))
    return dict(data=data, layout=lay)


def band_name(s):
    return s.split(" ", 1)[1].replace("-", "–")


def fig_seg_share(D, t):
    th = THEME[t]
    segs = [r for r in D["seg"] if not r[0].startswith("C0")]
    xs = [band_name(r[0]) for r in segs]
    data = []
    for i, (nm, k) in enumerate([("Share of loan-months", 1), ("Share of defaults", 2)]):
        ys = [r[k] for r in segs]
        data.append(dict(type="bar", name=nm, x=xs, y=ys,
                         marker=dict(color=th["cat"][i], line=dict(color=th["surface"], width=2), cornerradius=4),
                         text=[f"{v:.0f}%" for v in ys], textposition="outside", cliponaxis=False,
                         textfont=dict(color=th["ink2"], size=11),
                         hovertemplate="%{x}: %{y:.1f}%<extra>" + nm + "</extra>"))
    lay = base_layout(t, 340, barmode="group", bargap=0.62, bargroupgap=0.04, margin=dict(t=62),
                      xaxis=dict(title=axis_title(t, "Credit score at origination"), tickfont=dict(color=th["ink2"], size=12)),
                      yaxis=dict(ticksuffix="%", rangemode="tozero", title=axis_title(t, "% of window total")))
    return dict(data=data, layout=lay), segs


def fig_seg_ltv(D, t):
    th = THEME[t]
    credit = sorted({r[0] for r in D["segx"] if not r[0].startswith("C0")})
    ltv = sorted({r[1] for r in D["segx"] if not r[1].startswith("L0")})
    val = {(r[0], r[1]): r[2] for r in D["segx"]}
    data = []
    for i, l in enumerate(ltv):
        ys = [val.get((c, l)) for c in credit]
        data.append(dict(type="bar", name="LTV " + band_name(l).replace("<=", "≤ "), x=[band_name(c) for c in credit], y=ys,
                         marker=dict(color=th["ordinal"][i], line=dict(color=th["surface"], width=2), cornerradius=4),
                         hovertemplate="%{x}, " + band_name(l) + " LTV: %{y:.2f}% a year<extra></extra>"))
    lay = base_layout(t, 340, barmode="group", bargap=0.4, bargroupgap=0.04, margin=dict(t=62),
                      xaxis=dict(title=axis_title(t, "Credit score at origination"), tickfont=dict(color=th["ink2"], size=12)),
                      yaxis=dict(ticksuffix="%", rangemode="tozero", title=axis_title(t, "Annualized default rate")))
    return dict(data=data, layout=lay), credit, ltv, val


# ------------------------------------------------------------------ page
CSS = """
:root{color-scheme:light}
.viz-root{--plane:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;
--axis:#c3c2b7;--border:rgba(11,11,11,.10);--accent:#2a78d6}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;
--plane:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--accent:#3987e5}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--plane:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;
--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--accent:#3987e5}
html,body{margin:0;background:var(--plane)}
.viz-root{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:var(--plane);
min-height:100vh;padding:28px 20px 56px}
.wrap{max-width:1080px;margin:0 auto}
header{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}
h1{font-size:26px;line-height:1.2;margin:0 0 6px;font-weight:650}
.lede{margin:0;color:var(--ink2);max-width:760px;line-height:1.5;font-size:15px}
button.theme{background:var(--surface);color:var(--ink2);border:1px solid var(--border);border-radius:8px;
padding:8px 12px;font:inherit;font-size:13px;cursor:pointer}
button.theme:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.hero{margin:26px 0 14px;display:flex;flex-wrap:wrap;gap:20px 40px;align-items:flex-end}
.hero .num{font-size:64px;line-height:1;font-weight:650}
.hero .cap{max-width:420px;color:var(--ink2);font-size:15px;line-height:1.45;padding-bottom:6px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:0 0 26px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.tile .l{font-size:13px;color:var(--ink2)}.tile .v{font-size:28px;font-weight:650;margin-top:4px}
.tile .s{font-size:12px;color:var(--muted);margin-top:4px;line-height:1.4}
h2{font-size:18px;margin:34px 0 4px;font-weight:650}
.sec{margin:0 0 14px;color:var(--ink2);font-size:14px;line-height:1.5;max-width:820px}
.grid{display:grid;grid-template-columns:1fr;gap:14px}
@media (min-width:900px){.grid.two{grid-template-columns:1fr 1fr}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 16px 12px;min-width:0}
.card h3{font-size:15px;margin:0 0 2px;font-weight:650}
.card .sub{margin:0 0 6px;color:var(--ink2);font-size:13px;line-height:1.4}
.chart{width:100%}
.note{margin:6px 0 4px;color:var(--ink2);font-size:13px;line-height:1.5}
details.tv{margin-top:6px;border-top:1px solid var(--grid);padding-top:6px}
summary{cursor:pointer;color:var(--ink2);font-size:13px;padding:4px 0}
.tvwrap{overflow-x:auto;max-height:340px;overflow-y:auto}
table{border-collapse:collapse;font-size:12px;width:100%}
caption{text-align:left;color:var(--muted);padding:6px 0}
th,td{padding:5px 8px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap;
font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
th{color:var(--ink2);font-weight:600;position:sticky;top:0;background:var(--surface)}
.take{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:6px 18px}
.take li{margin:10px 0;line-height:1.5;font-size:14px;color:var(--ink)}
.meth{margin-top:30px;color:var(--ink2);font-size:13px;line-height:1.6}
.meth h3{font-size:14px;margin:14px 0 4px;color:var(--ink)}
footer{margin-top:26px;color:var(--muted);font-size:12px;line-height:1.5}
"""

JS = """
const FIGS = %%FIGS%%;
const CFG = {displaylogo:false, responsive:true, displayModeBar:false};
function mode(){const t=document.documentElement.getAttribute('data-theme');
 if(t==='dark'||t==='light') return t;
 return window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}
function draw(){const m=mode();
 for(const id in FIGS){const f=FIGS[id][m]; Plotly.react(id,f.data,f.layout,CFG);}}
function setTheme(t){document.documentElement.setAttribute('data-theme',t);
 try{localStorage.setItem('theme',t);}catch(e){}
 document.getElementById('themebtn').textContent=t==='dark'?'Light mode':'Dark mode';draw();}
(function(){let s=null;try{s=localStorage.getItem('theme');}catch(e){}
 if(s==='dark'||s==='light'){document.documentElement.setAttribute('data-theme',s);}
 document.getElementById('themebtn').textContent=mode()==='dark'?'Light mode':'Dark mode';
 document.getElementById('themebtn').addEventListener('click',function(){setTheme(mode()==='dark'?'light':'dark');});
 window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',draw);
 draw();})();
"""


def pct(x, d=2):
    return f"{x:.{d}f}%"


def build(D, out_path):
    figs, sections = {}, []
    last = D["pm"][-1]
    last_m = last[0]
    peak = max(D["pm"], key=lambda r: r[3] if r[3] is not None else -1)

    # --- figures (both themes)
    def both(fn):
        light, dark = fn(D, "light"), fn(D, "dark")
        if isinstance(light, tuple):
            return {"light": light[0], "dark": dark[0]}, light[1:]
        return {"light": light, "dark": dark}, ()

    f, _ = both(fig_health); figs["c_health"] = f
    f, ex = both(fig_vintage); figs["c_vintage"] = f; common, vby = ex
    f, ex = both(fig_calendar); figs["c_calendar"] = f; hby, hyears = ex
    f, ex = both(fig_roll); figs["c_roll"] = f; zmat = ex[0]
    f, ex = both(fig_cure); figs["c_cure"] = f; cby = ex[0]
    f, ex = both(fig_outcomes); figs["c_outcomes"] = f; ovints, oval = ex
    figs["c_forecast"] = both(fig_forecast)[0]
    f, ex = both(fig_seg_share); figs["c_seg1"] = f; segs = ex[0]
    f, ex = both(fig_seg_ltv); figs["c_seg2"] = f; credit, ltv, xval = ex

    # --- derived numbers for text
    n_exit_credit = sum(r[2] for r in D["out"] if r[1].endswith("Credit-event exit"))
    n_ever90 = sum(r[2] for r in D["out"])
    pct_credit_exit = 100.0 * n_exit_credit / n_ever90 if n_ever90 else 0
    out_tot = {}
    for r in D["out"]:
        out_tot[r[1].split(" ", 1)[1]] = out_tot.get(r[1].split(" ", 1)[1], 0) + r[2]
    share = lambda k: 100.0 * out_tot.get(k, 0) / n_ever90 if n_ever90 else 0
    peaks = {v: max(yh, key=lambda a: a[1]) for v, yh in hby.items()}
    all_peak_years = sorted({p[0] for p in peaks.values()})
    low_credit = [s for s in segs if s[0][:2] in ("C1", "C2")]
    lc_lm, lc_df = sum(s[1] for s in low_credit), sum(s[2] for s in low_credit)

    # --- tiles
    tiles = [
        ("Loans analysed", f"{D['n_loans']:,}", f"4 acquisition cohorts (2017Q1-2020Q1); {D['n_rows'] / 1e6:.1f}M loan-months"),
        (f"Active loans, {mlabel(last_m)}", f"{last[1]:,}", f"{last[1] / D['n_loans'] * 100:.0f}% of loans analysed"),
        ("Peak 90+ days past due", pct(peak[3]), f"{mlabel(peak[0])}, % of active loans"),
        ("Reached 90+, then credit-event exit", pct(pct_credit_exit, 1), f"{n_exit_credit:,} of {n_ever90:,} loans (clean cohort)"),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="l">{esc(a)}</div><div class="v">{esc(b)}</div>'
                         f'<div class="s">{esc(c)}</div></div>' for a, b, c in tiles)

    # --- section 1: health
    tv = table_view("Monthly share of active loans past due",
                    ["Month", "Active loans", "30+ days %", "90+ days %", "180+ days %"],
                    [(mlabel(r[0]), f"{r[1]:,}", f"{r[2]:.3f}", f"{r[3]:.3f}", f"{r[4]:.3f}") for r in D["pm"]])
    c1 = card("c_health", "Delinquency over time", "Share of active loans past due, all four cohorts pooled",
              "Pooled across cohorts, so early years cover fewer loans (new cohorts enter 2017-2020). "
              "The shaded band marks 2020-21 as a visual guide only.", tv, 340,
              f"Line chart of 30+ and 90+ days past due, 2017 to {last_m.year}")

    # --- section 2: vintages and calendar
    marks = [12, 24, 36, 48, 60, 72, common]
    marks = sorted(set(m for m in marks if m <= common))
    vl = sorted(vby)
    rows = []
    for m in marks:
        row = [m]
        for v in vl:
            d = dict(vby[v])
            row.append(f"{d[m]:.2f}%" if m in d else "")
        rows.append(row)
    at_common = ", ".join(f"{v} {dict(vby[v])[common]:.2f}%" for v in vl if common in dict(vby[v]))
    tv = table_view("Cumulative default % at selected months since origination", ["Months"] + vl, rows)
    c2 = card("c_vintage", "Cumulative default by vintage", "Default = 180+ days past due or a credit-event exit; clean cohort",
              f"Vintages are observed for different lengths, so compare them at the same age. At month {common}: {esc(at_common)}.",
              tv, 380, "Line chart of cumulative default percent by months since origination for each vintage")
    rows = [[y] + [f"{dict(hby[v]).get(y, float('nan')):.3f}%" if y in dict(hby[v]) else "" for v in sorted(hby)] for y in hyears]
    tv = table_view("Default rate per loan-month by calendar year (% of at-risk loans)", ["Year"] + sorted(hby), rows)
    if len(all_peak_years) == 1:
        peak_txt = f"Every vintage peaks in {all_peak_years[0]}, at a different loan age."
    else:
        peak_txt = "Peak years by vintage: " + ", ".join(f"{v} {p[0]}" for v, p in sorted(peaks.items())) + "."
    c3 = card("c_calendar", "Default rate by calendar year", "Same calendar window, different loan ages",
              f"{esc(peak_txt)} This is why a forecast that only uses loan age fails (see forecast section).",
              tv, 340, "Line chart of default rate per loan-month by calendar year for each vintage")

    # --- section 3: flow
    val = {(a, b): c for a, b, c in D["roll"]}
    rows = [[BUCKET_LABEL[r]] + [f"{val.get((r, c), 0):.2f}%" for c in TO_ORDER] for r in TO_ORDER[:5]]
    tv = table_view("Where loans in each bucket are one month later (row %)", ["This month"] + [TO_LABEL[c] for c in TO_ORDER], rows)
    c4 = card("c_roll", "Roll-rate matrix", "Where loans go next month, all months pooled (rows add to 100%)",
              "Read across a row. Values under 1% are left blank on the chart and shown in the table view.", tv, 330,
              "Heatmap of next-month status for loans in each delinquency bucket")
    ys = sorted({y for b in cby for y, _ in cby[b]})
    rows = [[y] + [f"{dict(cby[b]).get(y, float('nan')):.1f}%" for b in ("B1 30-59", "B2 60-89", "B3 90-179")] for y in ys]
    tv = table_view("One-month cure rate by year", ["Year", "From 30-59 days", "From 60-89 days", "From 90-179 days"], rows)
    c5 = card("c_cure", "One-month cure rate", "Share of loan-months fully current the next month",
              "Partial improvements (for example 60-89 back to 30-59) are not counted as cures.", tv, 330,
              "Line chart of one-month cure rate by calendar year for three delinquency buckets")
    rows = [[v] + [f"{oval.get((v, n), 0):.1f}%" for n in OUTCOME_ORDER] for v in ovints]
    tv = table_view("Where loans that reached 90+ days ended up (% of those loans)", ["Vintage"] + OUTCOME_ORDER, rows)
    c6 = card("c_outcomes", "What happened to loans that reached 90+ days",
              f"Position as of {mlabel(last_m)}, clean cohort",
              "'Active and current' means the loan is paying again; 'still delinquent' is mostly loans that cured and slipped again.",
              tv, 310, "Stacked bar chart of final outcome for loans that reached 90 days past due, by vintage")

    # --- section 4: forecast
    hold_lbl = f"holdout_{D['holdout']}"
    fwd = {}
    for r in D["fc"]:
        if r[0] == "forward":
            fwd[r[2]] = fwd.get(r[2], 0) + (r[3] or 0)
    vrows = [r for r in D["val"] if r[1] == "ALL" and str(r[0])[:7] == D["holdout"]]
    err = {r[2]: r[5] for r in vrows}
    allv = [(str(r[0])[:7], r[1], r[2], r[3], r[4], r[5], r[6]) for r in D["val"]]
    tv_val = table_view("Out-of-time validation: defaults in the holdout window, predicted vs actual",
                        ["Training cutoff", "Vintage", "Method", "Actual", "Predicted", "Error %", "Monthly MAPE %"],
                        [(a, b, c, f"{d:,.0f}", f"{e:,.0f}", f"{f_:+.1f}", f"{g:.1f}") for a, b, c, d, e, f_, g in allv])
    fc_note = ""
    if err:
        fc_note = ("Backtest error (all vintages): " + "; ".join(f"{MNAME.get(k, k)} {v:+.1f}%" for k, v in sorted(err.items())) + ". ")
    if fwd:
        fc_note += "Next 24 months (additional defaults): " + "; ".join(f"{MNAME.get(k, k)} {v:,.0f}" for k, v in sorted(fwd.items())) + ". "
    fc_note += "Assumes calendar conditions stay at the last six months' level; counts loans, not dollar losses."
    c7 = card("c_forecast", "Default forecast and backtest", "Monthly defaults, four cohorts combined",
              esc(fc_note), tv_val, 360, "Line chart of actual monthly defaults with backtest and forward model forecasts")

    # --- section 5: segments
    rows = [[band_name(s[0]), f"{s[1]:.1f}%", f"{s[2]:.1f}%", f"{s[3]:.3f}%", f"{s[4]:,}"] for s in segs]
    tv = table_view("Credit-score bands, report months from July 2023",
                    ["Credit score", "Share of loan-months", "Share of defaults", "Annualized default rate", "Default events"], rows)
    c8 = card("c_seg1", "Who defaults: share of exposure vs share of defaults", "Report months from July 2023, by credit score at origination",
              f"Scores below 700 were {lc_lm:.0f}% of loan-months and {lc_df:.0f}% of defaults. "
              "Association only; it does not show what causes defaults.", tv, 340,
              "Grouped bar chart comparing share of loan-months with share of defaults by credit score band")
    rows = [[band_name(c)] + [("" if xval.get((c, l)) is None else f"{xval[(c, l)]:.3f}%") for l in ltv] for c in credit]
    tv = table_view("Annualized default rate by credit band and LTV band (blank = fewer than 30 default events)",
                    ["Credit score"] + [band_name(l) for l in ltv], rows)
    c9 = card("c_seg2", "Credit score and loan-to-value together", "Annualized default rate, report months from July 2023",
              "Credit score separates risk more than LTV in these data; both raise risk together.", tv, 340,
              "Grouped bar chart of annualized default rate by credit score band and LTV band")

    take = [
        f"In {mlabel(last_m)}, {pct(last[2])} of active loans were 30+ days past due and {pct(last[3])} were 90+ "
        f"(peak 90+: {pct(peak[3])} in {mlabel(peak[0])}).",
        peak_txt + " Loan age alone does not explain the timing.",
        f"Of {n_ever90:,} loans that reached 90+ days, {pct(pct_credit_exit, 1)} ended in a credit-event exit; "
        f"{pct(share('Active and current'), 0)} are current again and {pct(share('Prepaid/matured'), 0)} prepaid or matured.",
        f"From July 2023, credit scores below 700 were {lc_lm:.0f}% of loan-months but {lc_df:.0f}% of defaults.",
    ]
    take_html = "<ul class='take'>" + "".join(f"<li>{esc(x)}</li>" for x in take) + "</ul>"

    body = f"""
<div class="viz-root"><div class="wrap">
<header><div><h1>Loan portfolio health and default forecast</h1>
<p class="lede">Fannie Mae single-family loans acquired in the first quarter of 2017, 2018, 2019 and 2020,
followed month by month through {esc(mlabel(last_m))}. Delinquency, vintage curves, roll rates, a validated default forecast, and where risk concentrates.</p></div>
<button class="theme" id="themebtn" type="button">Dark mode</button></header>
<div class="hero"><div class="num">{esc(pct(last[3]))}</div>
<div class="cap">of active loans were 90+ days past due in {esc(mlabel(last_m))}; {esc(pct(last[2]))} were 30+ days past due.</div></div>
<div class="tiles">{tiles_html}</div>
<h2>Key takeaways</h2>{take_html}
<h2>1. Portfolio health</h2><div class="grid">{c1}</div>
<h2>2. Vintages and calendar time</h2>
<p class="sec">Vintage curves compare cohorts at the same loan age. The calendar view shows why age alone is not enough.</p>
<div class="grid two">{c2}{c3}</div>
<h2>3. How delinquency moves</h2>
<p class="sec">Where delinquent loans go next, how often they recover, and how loans that reached 90+ days ended up.</p>
<div class="grid two">{c4}{c5}</div><div class="grid" style="margin-top:14px">{c6}</div>
<h2>4. Forecast and backtest</h2>
<p class="sec">Trained on data up to a cutoff, then compared with what actually happened afterwards. Errors are shown as they came out.</p>
<div class="grid">{c7}</div>
<h2>5. Where risk concentrates</h2>
<p class="sec">Borrower attributes fixed at origination, measured on loan-months from July 2023 onward.</p>
<div class="grid two">{c8}{c9}</div>
<div class="meth"><h3>Definitions</h3>
Delinquency buckets use months past due as reported: 30-59, 60-89, 90-179 and 180+ days. Default is the first month at 180+ days
or an exit coded 02, 03, 09 or 15. The clean cohort is loans first seen within 3 months of origination
({D['n_clean'] / D['n_loans'] * 100:.1f}% of loans). Balances are masked by the source for the first six months, so rates are loan-count based.
<h3>Forecast method</h3>
Three methods share one exposure projection: a run-rate benchmark, an age-only curve, and an age x calendar model
(age effect x calendar effect x vintage level, fitted jointly). The age-only curve is excluded from the chart because it fails after the 2020-21 shift.
<h3>Limits</h3>
Four first-quarter cohorts only; counts of loans, not dollar losses; the forecast assumes recent calendar conditions persist.</div>
<footer>Source: Fannie Mae Single-Family Loan Performance Data (Primary dataset), reporting months through {esc(mlabel(last_m))}.
Aggregated results only; no loan-level data is shown. This is an independent portfolio project, not affiliated with or endorsed by Fannie Mae.
Built {dt.date.today().isoformat()} with DuckDB and Plotly.</footer>
</div></div>"""

    local_js = os.path.join(os.path.dirname(out_path) or ".", "plotly.min.js")
    plotly_src = "plotly.min.js" if os.path.exists(local_js) else PLOTLY_URL
    print("Plotly source:", plotly_src)
    figs_json = json.dumps(figs, separators=(",", ":")).replace("</", "<\\/")
    page = (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Loan portfolio health and default forecast</title><style>{CSS}</style></head><body>"
            f"{body}<script src=\"{plotly_src}\"></script><script>{JS.replace('%%FIGS%%', figs_json)}</script></body></html>")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return len(page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/processed/loans.duckdb")
    ap.add_argument("--out", default="dashboard/index.html")
    ap.add_argument("--holdout", default="2024-06", help="training cutoff of the backtest run to show")
    a = ap.parse_args()
    con = duckdb.connect(a.db, read_only=True)
    D = load(con, a.holdout)
    n = build(D, a.out)
    print(f"Wrote {a.out} ({n / 1024:.0f} KB). Open it in a browser to check.")
    print(f"Months of data: {D['pm'][0][0]} to {D['pm'][-1][0]} | loans: {D['n_loans']:,}")


if __name__ == "__main__":
    main()
