"""
Extract -> Transform -> Validate -> Load.

Reads the three source systems, standardises them, runs data-quality gates,
and loads a small star schema into a SQLite warehouse. Every run is logged to
the etl_runs audit table. Loads are idempotent: re-running replaces data.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "source"
DB = ROOT / "warehouse" / "kpi_warehouse.db"
SCHEMA = ROOT / "sql" / "01_schema.sql"
VIEWS = ROOT / "sql" / "02_views.sql"
STARTING_SUBSCRIBERS = 150_000
STARTING_MRR = 12_750_000.0

log = logging.getLogger("etl")


class DataQualityError(RuntimeError):
    pass


# ---------- Extract ----------
def extract() -> dict[str, pd.DataFrame]:
    order_files = sorted(SRC.glob("orders_*.csv"))
    if not order_files:
        raise FileNotFoundError(f"No order files in {SRC}")
    orders = pd.concat([pd.read_csv(f) for f in order_files], ignore_index=True)
    tickets = pd.read_csv(SRC / "tickets.csv")
    regions = pd.DataFrame(json.loads((SRC / "regions.json").read_text()))
    log.info("Extracted %s orders from %s files, %s tickets, %s regions",
             f"{len(orders):,}", len(order_files), f"{len(tickets):,}", len(regions))
    return {"orders": orders, "tickets": tickets, "regions": regions}


# ---------- Transform ----------
def week_start(s: pd.Series) -> pd.Series:
    return (s - pd.to_timedelta(s.dt.dayofweek, unit="D")).dt.normalize()


def transform(raw: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    o = raw["orders"].copy()
    excluded = (o["channel"] == "TEST").sum()
    o = o[o["channel"] != "TEST"].drop_duplicates("order_id")
    o["order_date"] = pd.to_datetime(o["order_date"], format="%Y-%m-%d")
    o["week_start"] = week_start(o["order_date"]).dt.strftime("%Y-%m-%d")
    o["order_date"] = o["order_date"].dt.strftime("%Y-%m-%d")
    log.info("Excluded %s test orders", excluded)

    t = raw["tickets"].copy().drop_duplicates("ticket_id")
    t["opened_at"] = pd.to_datetime(t["opened_at"], format="%m/%d/%Y %H:%M")   # source uses US format
    t["week_start"] = week_start(t["opened_at"]).dt.strftime("%Y-%m-%d")
    t["opened_date"] = t["opened_at"].dt.strftime("%Y-%m-%d")
    t["opened_at"] = t["opened_at"].dt.strftime("%Y-%m-%d %H:%M")
    t["is_resolved"] = t["resolution_hours"].notna().astype(int)
    t["sla_met"] = (t["resolution_hours"] <= 24).astype(int)
    t["first_contact_resolved"] = (t["first_contact_resolved"].str.upper() == "Y").astype(int)
    t = t.rename(columns={"region": "region_code"})

    regions = raw["regions"].rename(columns={"code": "region_code", "name": "region_name"})

    dates = pd.date_range(min(o["order_date"].min(), t["opened_date"].min()),
                          max(o["order_date"].max(), t["opened_date"].max()), freq="D")
    dim_date = pd.DataFrame({"date": dates})
    dim_date["week_start"] = week_start(dim_date["date"]).dt.strftime("%Y-%m-%d")
    dim_date["iso_week"] = dim_date["date"].dt.isocalendar().week.astype(int)
    dim_date["month"] = dim_date["date"].dt.strftime("%Y-%m")
    dim_date["is_weekend"] = (dim_date["date"].dt.dayofweek >= 5).astype(int)
    dim_date["date"] = dim_date["date"].dt.strftime("%Y-%m-%d")

    return {
        "dim_region": regions,
        "dim_date": dim_date,
        "fact_orders": o[["order_id", "order_date", "week_start", "region_code", "order_type", "mrc_change", "channel"]],
        "fact_tickets": t[["ticket_id", "opened_at", "opened_date", "week_start", "region_code", "category",
                           "resolution_hours", "is_resolved", "sla_met", "first_contact_resolved"]],
    }


# ---------- Validate ----------
def validate(tables: dict[str, pd.DataFrame]) -> None:
    problems = []
    known = set(tables["dim_region"]["region_code"])
    for name in ("fact_orders", "fact_tickets"):
        df = tables[name]
        id_col = df.columns[0]
        if not df[id_col].is_unique:
            problems.append(f"{name}: duplicate {id_col}")
        unknown = set(df["region_code"]) - known
        if unknown:
            problems.append(f"{name}: unknown region codes {sorted(unknown)}")
    o = tables["fact_orders"]
    bad_types = set(o["order_type"]) - {"New Connect", "Disconnect", "Upgrade", "Downgrade"}
    if bad_types:
        problems.append(f"fact_orders: unexpected order types {sorted(bad_types)}")
    # Every day should have orders; a gap usually means a missing source file
    days_with_orders = o["order_date"].nunique()
    if days_with_orders < len(tables["dim_date"]):
        problems.append(f"fact_orders: only {days_with_orders} of {len(tables['dim_date'])} days have orders")
    if problems:
        raise DataQualityError("; ".join(problems))
    log.info("Data-quality gates passed")


# ---------- Load ----------
def load(tables: dict[str, pd.DataFrame], db: Path = DB) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as con:
        con.executescript(SCHEMA.read_text())
        for name, df in tables.items():
            con.execute(f"DELETE FROM {name}")
            df.to_sql(name, con, if_exists="append", index=False)
            log.info("Loaded %-13s %8s rows", name, f"{len(df):,}")
        con.execute("DELETE FROM baseline")
        con.execute("INSERT INTO baseline VALUES (?, ?, ?)",
                    (tables["dim_date"]["week_start"].min(), STARTING_SUBSCRIBERS, STARTING_MRR))
        con.executescript(VIEWS.read_text())


def record_run(status: str, detail: str, db: Path = DB) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE IF NOT EXISTS etl_runs (run_at TEXT, status TEXT, detail TEXT)")
        con.execute("INSERT INTO etl_runs VALUES (?, ?, ?)", (datetime.now().isoformat(timespec="seconds"), status, detail))


def run(db: Path = DB) -> dict[str, int]:
    try:
        tables = transform(extract())
        validate(tables)
        load(tables, db)
        counts = {k: len(v) for k, v in tables.items()}
        record_run("SUCCESS", json.dumps(counts), db)
        return counts
    except Exception as exc:
        record_run("FAILED", str(exc), db)
        log.exception("ETL failed")
        raise
