"""
Generate synthetic daily source extracts for a subscription internet provider.

Three systems, three file formats/conventions - as in real life:
  data/source/orders_YYYY-MM.csv      order-management system (one file per month)
  data/source/tickets.csv             support ticketing system
  data/source/regions.json            reference data
All data is synthetic.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "source"
START, END = "2026-03-30", "2026-10-11"   # 28 full weeks, Monday to Sunday
REGIONS = {"R1": "North Texas", "R2": "South Texas", "R3": "Midwest", "R4": "Southeast", "R5": "West"}
ORDER_TYPES = ["New Connect", "Disconnect", "Upgrade", "Downgrade"]


def main(seed: int = 3) -> None:
    rng = np.random.default_rng(seed)
    SRC.mkdir(parents=True, exist_ok=True)
    for old in SRC.glob("orders_*.csv"):
        old.unlink()
    days = pd.date_range(START, END, freq="D")

    rows, oid = [], 1
    for i, d in enumerate(days):
        weekend = d.dayofweek >= 5
        trend = 1 + i / len(days) * 0.15                       # gentle growth
        promo = 1.35 if pd.Timestamp("2026-08-17") <= d <= pd.Timestamp("2026-08-30") else 1.0
        for code in REGIONS:
            lam = {
                "New Connect": 26 * trend * promo,
                "Disconnect": 19 * (1.25 if code == "R2" and d >= pd.Timestamp("2026-09-14") else 1),
                "Upgrade": 9 * trend,
                "Downgrade": 4,
            }
            for otype, l in lam.items():
                for _ in range(rng.poisson(l * (0.6 if weekend else 1))):
                    mrc = {"New Connect": rng.choice([55, 75, 95, 130]), "Disconnect": rng.choice([55, 75, 95, 130]),
                           "Upgrade": rng.choice([20, 35]), "Downgrade": -rng.choice([20, 35])}[otype]
                    rows.append((f"ORD{oid:07d}", d.strftime("%Y-%m-%d"), code, otype, float(mrc),
                                 rng.choice(["Web", "Phone", "Retail", "Field Sales"], p=[0.45, 0.30, 0.15, 0.10])))
                    oid += 1
    orders = pd.DataFrame(rows, columns=["order_id", "order_date", "region_code", "order_type", "mrc_change", "channel"])
    # A few cancelled/test orders that the pipeline must exclude
    test_idx = rng.choice(len(orders), 40, replace=False)
    orders.loc[test_idx, "channel"] = "TEST"
    orders["month"] = orders["order_date"].str[:7]
    for m, part in orders.groupby("month"):
        part.drop(columns="month").to_csv(SRC / f"orders_{m}.csv", index=False)

    trows, tid = [], 1
    for d in days:
        for code in REGIONS:
            n = rng.poisson(48 * (1.4 if code == "R2" and d >= pd.Timestamp("2026-09-14") else 1))
            for _ in range(n):
                cat = rng.choice(["Connectivity", "Billing", "Equipment", "Install", "Other"], p=[0.38, 0.27, 0.15, 0.12, 0.08])
                hours = float(np.round(rng.gamma(2.0, 9 if cat == "Connectivity" else 6), 1))
                opened = d + pd.Timedelta(minutes=int(rng.integers(0, 1440)))
                trows.append((f"TCK{tid:07d}", opened.strftime("%m/%d/%Y %H:%M"), code, cat, hours,
                              "Y" if rng.random() < 0.68 else "N"))
                tid += 1
    tickets = pd.DataFrame(trows, columns=["ticket_id", "opened_at", "region", "category", "resolution_hours", "first_contact_resolved"])
    # Some tickets still open (no resolution time yet)
    tickets.loc[rng.choice(len(tickets), 300, replace=False), "resolution_hours"] = np.nan
    tickets.to_csv(SRC / "tickets.csv", index=False)

    (SRC / "regions.json").write_text(json.dumps([{"code": k, "name": v} for k, v in REGIONS.items()], indent=2))
    print(f"orders: {len(orders):,} rows in {orders['month'].nunique()} monthly files; tickets: {len(tickets):,} rows")


if __name__ == "__main__":
    main()
