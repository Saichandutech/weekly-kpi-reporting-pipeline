"""
Build the weekly KPI report from the warehouse views:
  * output/weekly_kpi_report_<week>.xlsx  - KPI scorecard, trends (native Excel charts), region detail
  * output/weekly_kpi_email_<week>.html   - email-ready summary with charts embedded
Automatic call-outs flag any KPI that moved unusually versus its trailing 8-week range.
"""
from __future__ import annotations

import base64
import html
import io
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "warehouse" / "kpi_warehouse.db"
OUT = ROOT / "output"

# metric -> (label, format, higher_is_better)
KPIS = {
    "net_adds": ("Net subscriber adds", "{:,.0f}", True),
    "ending_subscribers": ("Ending subscribers", "{:,.0f}", True),
    "ending_mrr": ("Monthly recurring revenue", "${:,.0f}", True),
    "arpu": ("ARPU", "${:,.2f}", True),
    "weekly_churn_pct": ("Weekly churn", "{:.2f}%", False),
    "tickets_per_1k_subs": ("Tickets per 1k subscribers", "{:.1f}", False),
    "avg_resolution_hours": ("Avg resolution time (hrs)", "{:.1f}", False),
    "sla_met_pct": ("Tickets resolved within 24h", "{:.1f}%", True),
    "fcr_pct": ("First-contact resolution", "{:.1f}%", True),
}


@dataclass
class ReportData:
    week: str
    kpis: pd.DataFrame          # all weeks
    scorecard: pd.DataFrame     # this week vs last week vs 4-wk avg
    regions: pd.DataFrame       # region detail for this week with WoW
    callouts: list[str]


def load(db: Path = DB, week: str | None = None) -> ReportData:
    with sqlite3.connect(db) as con:
        kpis = pd.read_sql("SELECT * FROM vw_weekly_kpis", con)
        orders = pd.read_sql("SELECT * FROM vw_weekly_orders_by_region", con)
        tickets = pd.read_sql("SELECT * FROM vw_weekly_tickets_by_region", con)

    week = week or kpis["week_start"].max()
    if week not in set(kpis["week_start"]):
        raise ValueError(f"Week {week} not in warehouse (available: {kpis['week_start'].min()} to {kpis['week_start'].max()})")
    kpis = kpis[kpis["week_start"] <= week].reset_index(drop=True)
    cur, prev = kpis.iloc[-1], kpis.iloc[-2]
    last4 = kpis.iloc[-5:-1]

    rows = []
    for col, (label, fmt, _) in KPIS.items():
        delta = cur[col] - prev[col]
        rows.append({
            "kpi": label, "metric": col,
            "this_week": cur[col], "last_week": prev[col],
            "wow_change": delta,
            "wow_change_pct": 100 * delta / prev[col] if prev[col] else None,
            "prior_4wk_avg": last4[col].mean(),
        })
    scorecard = pd.DataFrame(rows)

    reg = orders.merge(tickets, on=["week_start", "region_name"])
    prev_week = kpis["week_start"].iloc[-2]
    reg_cur = reg[reg["week_start"] == week].set_index("region_name")
    reg_prev = reg[reg["week_start"] == prev_week].set_index("region_name")
    regions = reg_cur[["new_connects", "disconnects", "net_adds", "tickets", "avg_resolution_hours", "sla_met_pct"]].copy()
    regions["disconnects_wow"] = reg_cur["disconnects"] - reg_prev["disconnects"]
    regions["tickets_wow"] = reg_cur["tickets"] - reg_prev["tickets"]
    regions = regions.reset_index().sort_values("net_adds")

    return ReportData(week, kpis, scorecard, regions, _callouts(kpis, reg[reg["week_start"] <= week]))


def _callouts(kpis: pd.DataFrame, reg: pd.DataFrame) -> list[str]:
    """Flag KPIs outside 2 standard deviations of the trailing 8 weeks, and regional shifts."""
    notes = []
    cur, hist = kpis.iloc[-1], kpis.iloc[-9:-1]
    for col, (label, fmt, higher_better) in KPIS.items():
        if col in ("ending_subscribers", "ending_mrr", "arpu"):
            continue  # cumulative metrics trend by design
        mu, sd = hist[col].mean(), hist[col].std()
        if sd and abs(cur[col] - mu) > 2 * sd:
            direction = "above" if cur[col] > mu else "below"
            good = (cur[col] > mu) == higher_better
            notes.append(f"{'✅' if good else '⚠️'} {label} was {fmt.format(cur[col])}, {direction} its normal "
                         f"8-week range (avg {fmt.format(mu)}).")

    # Regional: last 4 weeks vs the 8 weeks before, for disconnects and tickets
    weeks = sorted(reg["week_start"].unique())
    if len(weeks) >= 12:
        recent, base = weeks[-4:], weeks[-12:-4]
        for metric, label in (("disconnects", "disconnects"), ("tickets", "support tickets")):
            r = reg[reg["week_start"].isin(recent)].groupby("region_name")[metric].mean()
            b = reg[reg["week_start"].isin(base)].groupby("region_name")[metric].mean()
            change = (100 * (r - b) / b).sort_values(ascending=False)
            for region, pct in change.items():
                if pct >= 15:
                    notes.append(f"⚠️ {region}: weekly {label} are up {pct:.0f}% over the last 4 weeks "
                                 f"({r[region]:.0f}/wk vs {b[region]:.0f}/wk before).")
    if not notes:
        notes.append("All KPIs within their normal ranges this week.")
    return notes


# ---------------- Excel ----------------
HEAD = PatternFill("solid", fgColor="1F3A5F")
GOOD, BAD = PatternFill("solid", fgColor="DCFCE7"), PatternFill("solid", fgColor="FDE2E1")


def _style_header(ws, row: int = 1) -> None:
    for c in ws[row]:
        if c.value is not None:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), HEAD
            c.alignment = Alignment(horizontal="center", wrap_text=True)


def _autowidth(ws, minimum: int = 10) -> None:
    for i, col in enumerate(ws.columns, start=1):
        w = max((len(str(c.value)) for c in col if c.value is not None), default=0)
        ws.column_dimensions[get_column_letter(i)].width = min(max(w + 2, minimum), 45)


def _data_quality(db: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Recent ETL runs and current row counts, so readers can see the report came from a validated load."""
    with sqlite3.connect(db) as con:
        runs = pd.read_sql("SELECT run_at, status, detail FROM etl_runs ORDER BY run_at DESC LIMIT 10", con)
        counts = pd.DataFrame(
            [(t, con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
             for t in ("dim_region", "dim_date", "fact_orders", "fact_tickets")],
            columns=["table", "rows"],
        )
    return runs, counts


def write_excel(d: ReportData, out: Path = OUT, db: Path = DB) -> Path:
    path = out / f"weekly_kpi_report_{d.week}.xlsx"
    out.mkdir(parents=True, exist_ok=True)
    sc = d.scorecard.copy()
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        # Scorecard
        sheet = sc[["kpi", "this_week", "last_week", "wow_change", "wow_change_pct", "prior_4wk_avg"]]
        sheet.columns = ["KPI", "This week", "Last week", "WoW change", "WoW change %", "Prior 4-wk avg"]
        sheet.to_excel(xw, sheet_name="Scorecard", index=False, startrow=3)
        ws = xw.sheets["Scorecard"]
        ws["A1"], ws["A2"] = "Weekly KPI Report", f"Week starting {d.week}"
        ws["A1"].font, ws["A2"].font = Font(bold=True, size=16), Font(italic=True, color="555555")
        _style_header(ws, 4)
        for r, (_, row) in enumerate(sc.iterrows(), start=5):
            _, fmt, higher_better = KPIS[row["metric"]]
            num = {"${:,.0f}": '"$"#,##0', "${:,.2f}": '"$"#,##0.00', "{:.2f}%": "0.00", "{:.1f}%": "0.0",
                   "{:.1f}": "0.0", "{:,.0f}": "#,##0"}[fmt]
            for col in "BCDF":
                ws[f"{col}{r}"].number_format = num
            ws[f"E{r}"].number_format = '0.0"%"'
            if row["wow_change"] != 0:
                ws[f"D{r}"].fill = GOOD if (row["wow_change"] > 0) == higher_better else BAD
        start = len(sc) + 7
        ws[f"A{start}"] = "Call-outs"
        ws[f"A{start}"].font = Font(bold=True, size=12)
        for i, note in enumerate(d.callouts, start=1):
            ws[f"A{start + i}"] = note
        _autowidth(ws)
        ws.column_dimensions["A"].width = 32

        # Trend data + native charts
        trend = d.kpis[["week_start", "new_connects", "disconnects", "net_adds", "ending_subscribers",
                        "arpu", "tickets", "sla_met_pct", "avg_resolution_hours"]]
        trend.to_excel(xw, sheet_name="Trends", index=False)
        ws = xw.sheets["Trends"]
        _style_header(ws)
        _autowidth(ws)
        n = len(trend) + 1
        cats = Reference(ws, min_col=1, min_row=2, max_row=n)

        bar = BarChart(); bar.title = "New connects vs disconnects"; bar.height, bar.width = 7.5, 18
        bar.add_data(Reference(ws, min_col=2, max_col=3, min_row=1, max_row=n), titles_from_data=True)
        bar.set_categories(cats); ws.add_chart(bar, "K2")

        line = LineChart(); line.title = "Ending subscribers"; line.height, line.width = 7.5, 18
        line.add_data(Reference(ws, min_col=5, min_row=1, max_row=n), titles_from_data=True)
        line.set_categories(cats); line.legend = None
        for sr in line.series: sr.smooth = False
        ws.add_chart(line, "K18")

        line2 = LineChart(); line2.title = "Tickets resolved within 24h (%)"; line2.height, line2.width = 7.5, 18
        line2.add_data(Reference(ws, min_col=8, min_row=1, max_row=n), titles_from_data=True)
        line2.set_categories(cats); line2.legend = None
        for sr in line2.series: sr.smooth = False
        ws.add_chart(line2, "K34")

        # Region detail
        reg = d.regions.rename(columns=lambda c: c.replace("_", " ").title())
        reg.to_excel(xw, sheet_name="Regions", index=False)
        ws = xw.sheets["Regions"]
        _style_header(ws); _autowidth(ws)

        # Data quality: ETL audit trail + table row counts
        runs, counts = _data_quality(db)
        counts.to_excel(xw, sheet_name="Data Quality", index=False, startrow=2)
        runs.to_excel(xw, sheet_name="Data Quality", index=False, startrow=len(counts) + 6)
        ws = xw.sheets["Data Quality"]
        ws["A1"] = "Warehouse row counts"
        ws[f"A{len(counts) + 6}"] = "Recent ETL runs (all data-quality gates must pass for SUCCESS)"
        for cell in (ws["A1"], ws[f"A{len(counts) + 6}"]):
            cell.font = Font(bold=True, size=12)
        _style_header(ws, 3); _style_header(ws, len(counts) + 7)
        _autowidth(ws)
    return path


# ---------------- BI extracts ----------------
def write_bi_extracts(db: Path = DB, out: Path = OUT) -> list[Path]:
    """Export the reporting views as CSVs so Power BI / Tableau can use the same KPI definitions."""
    folder = out / "bi_extracts"
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    with sqlite3.connect(db) as con:
        for view in ("vw_weekly_kpis", "vw_weekly_orders_by_region", "vw_weekly_tickets_by_region"):
            path = folder / f"{view.removeprefix('vw_')}.csv"
            pd.read_sql(f"SELECT * FROM {view}", con).to_csv(path, index=False)
            written.append(path)
    return written


# ---------------- HTML email ----------------
def _chart_png(d: ReportData) -> str:
    k = d.kpis.tail(12)
    x = pd.to_datetime(k["week_start"]).dt.strftime("%b %d")
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.2), dpi=110)
    axes[0].bar(x, k["new_connects"], color="#2563eb", label="New connects")
    axes[0].bar(x, -k["disconnects"], color="#dc2626", label="Disconnects")
    axes[0].plot(x, k["net_adds"], color="#111827", marker="o", label="Net adds")
    axes[0].axhline(0, color="#9ca3af", linewidth=0.8)
    axes[0].set_title("Subscriber movement (last 12 weeks)", fontsize=10); axes[0].legend(fontsize=7, frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.28))
    axes[1].plot(x, k["sla_met_pct"], color="#059669", marker="o")
    axes[1].set_title("Tickets resolved within 24h (%)", fontsize=10)
    for ax in axes:
        ax.tick_params(axis="x", labelrotation=45, labelsize=7); ax.tick_params(axis="y", labelsize=8)
        ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png"); plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def write_html(d: ReportData, out: Path = OUT) -> Path:
    rows = ""
    for _, r in d.scorecard.iterrows():
        label, fmt, higher_better = KPIS[r["metric"]]
        delta = r["wow_change"]
        color = "#6b7280" if delta == 0 else ("#059669" if (delta > 0) == higher_better else "#dc2626")
        arrow = "▲" if delta > 0 else "▼" if delta < 0 else "–"
        if fmt.endswith("%"):  # percentage metrics: show the change in percentage points
            change = f"{abs(delta):.2f} pts" if ".2f" in fmt else f"{abs(delta):.1f} pts"
        else:
            pct = f" ({r['wow_change_pct']:+.1f}%)" if pd.notna(r["wow_change_pct"]) else ""
            change = fmt.format(abs(delta)) + pct
        rows += (f"<tr><td>{html.escape(label)}</td><td style='text-align:right;font-weight:600'>{fmt.format(r['this_week'])}</td>"
                 f"<td style='text-align:right'>{fmt.format(r['last_week'])}</td>"
                 f"<td style='text-align:right;color:{color}'>{arrow} {change}</td></tr>")
    reg_rows = "".join(
        f"<tr><td>{html.escape(r.region_name)}</td><td style='text-align:right'>{r.new_connects:,}</td>"
        f"<td style='text-align:right'>{r.disconnects:,}</td><td style='text-align:right'>{r.net_adds:,}</td>"
        f"<td style='text-align:right'>{r.tickets:,}</td><td style='text-align:right'>{r.sla_met_pct:.1f}%</td></tr>"
        for r in d.regions.itertuples()
    )
    notes = "".join(f"<li style='margin-bottom:6px'>{html.escape(n)}</li>" for n in d.callouts)
    th = "style='text-align:left;padding:6px 8px;background:#1f3a5f;color:#fff;font-weight:600'"
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weekly KPI Report – week of {d.week}</title>
<style>td{{padding:6px 8px;border-bottom:1px solid #e5e7eb}} table{{border-collapse:collapse;width:100%;font-size:14px}}</style></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#1f2937;max-width:760px;margin:0 auto;padding:20px 16px;background:#fff">
<h1 style="font-size:22px;margin:0">Weekly KPI Report</h1>
<p style="color:#6b7280;margin-top:4px">Week starting {d.week} · generated automatically from the KPI warehouse</p>
<h2 style="font-size:16px">Call-outs</h2><ul style="padding-left:20px">{notes}</ul>
<h2 style="font-size:16px">Scorecard</h2>
<div style="overflow-x:auto"><table><tr><th {th}>KPI</th><th {th}>This week</th><th {th}>Last week</th><th {th}>Change</th></tr>{rows}</table></div>
<h2 style="font-size:16px">Trends</h2><img alt="KPI trends" style="width:100%;height:auto" src="data:image/png;base64,{_chart_png(d)}">
<h2 style="font-size:16px">By region</h2>
<div style="overflow-x:auto"><table><tr><th {th}>Region</th><th {th}>New</th><th {th}>Disc.</th><th {th}>Net</th><th {th}>Tickets</th><th {th}>≤24h</th></tr>{reg_rows}</table></div>
<p style="color:#9ca3af;font-size:12px;margin-top:24px">Full detail in the attached Excel workbook. Synthetic data for demonstration.</p>
</body></html>"""
    path = out / f"weekly_kpi_email_{d.week}.html"
    out.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    return path
