-- Warehouse schema (SQLite; types map directly to PostgreSQL)
CREATE TABLE IF NOT EXISTS dim_region (
    region_code  TEXT PRIMARY KEY,
    region_name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_date (
    date        TEXT PRIMARY KEY,
    week_start  TEXT NOT NULL,
    iso_week    INTEGER NOT NULL,
    month       TEXT NOT NULL,
    is_weekend  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_orders (
    order_id     TEXT PRIMARY KEY,
    order_date   TEXT NOT NULL REFERENCES dim_date(date),
    week_start   TEXT NOT NULL,
    region_code  TEXT NOT NULL REFERENCES dim_region(region_code),
    order_type   TEXT NOT NULL CHECK (order_type IN ('New Connect','Disconnect','Upgrade','Downgrade')),
    mrc_change   REAL NOT NULL,
    channel      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_tickets (
    ticket_id               TEXT PRIMARY KEY,
    opened_at               TEXT NOT NULL,
    opened_date             TEXT NOT NULL REFERENCES dim_date(date),
    week_start              TEXT NOT NULL,
    region_code             TEXT NOT NULL REFERENCES dim_region(region_code),
    category                TEXT NOT NULL,
    resolution_hours        REAL,          -- NULL while the ticket is still open
    is_resolved             INTEGER NOT NULL,
    sla_met                 INTEGER NOT NULL,  -- resolved within 24h
    first_contact_resolved  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS baseline (
    as_of_week            TEXT,
    starting_subscribers  INTEGER,
    starting_mrr          REAL
);

CREATE INDEX IF NOT EXISTS ix_orders_week  ON fact_orders(week_start, region_code);
CREATE INDEX IF NOT EXISTS ix_tickets_week ON fact_tickets(week_start, region_code);
