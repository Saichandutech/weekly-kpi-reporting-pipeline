-- Reporting views. The report and any BI tool (Power BI / Tableau) read from these.

DROP VIEW IF EXISTS vw_weekly_orders_by_region;
CREATE VIEW vw_weekly_orders_by_region AS
SELECT
    o.week_start,
    r.region_name,
    SUM(o.order_type = 'New Connect')                              AS new_connects,
    SUM(o.order_type = 'Disconnect')                               AS disconnects,
    SUM(o.order_type = 'New Connect') - SUM(o.order_type = 'Disconnect') AS net_adds,
    SUM(o.order_type = 'Upgrade')                                  AS upgrades,
    SUM(o.order_type = 'Downgrade')                                AS downgrades,
    SUM(CASE WHEN o.order_type = 'Disconnect' THEN -o.mrc_change ELSE o.mrc_change END) AS mrr_change
FROM fact_orders o
JOIN dim_region r USING (region_code)
GROUP BY o.week_start, r.region_name;

DROP VIEW IF EXISTS vw_weekly_tickets_by_region;
CREATE VIEW vw_weekly_tickets_by_region AS
SELECT
    t.week_start,
    r.region_name,
    COUNT(*)                                                       AS tickets,
    ROUND(AVG(t.resolution_hours), 1)                              AS avg_resolution_hours,
    ROUND(100.0 * SUM(t.sla_met) / NULLIF(SUM(t.is_resolved), 0), 1) AS sla_met_pct,
    ROUND(100.0 * AVG(t.first_contact_resolved), 1)                AS fcr_pct
FROM fact_tickets t
JOIN dim_region r USING (region_code)
GROUP BY t.week_start, r.region_name;

DROP VIEW IF EXISTS vw_weekly_kpis;
CREATE VIEW vw_weekly_kpis AS
WITH orders AS (
    SELECT week_start,
           SUM(new_connects) AS new_connects, SUM(disconnects) AS disconnects,
           SUM(net_adds) AS net_adds, SUM(upgrades) AS upgrades, SUM(mrr_change) AS mrr_change
    FROM vw_weekly_orders_by_region GROUP BY week_start
),
tickets AS (
    SELECT week_start,
           COUNT(*)                                                   AS tickets,
           ROUND(AVG(resolution_hours), 1)                            AS avg_resolution_hours,
           ROUND(100.0 * SUM(sla_met) / NULLIF(SUM(is_resolved), 0), 1) AS sla_met_pct,
           ROUND(100.0 * AVG(first_contact_resolved), 1)              AS fcr_pct
    FROM fact_tickets GROUP BY week_start
),
running AS (
    SELECT o.*,
           b.starting_subscribers + SUM(o.net_adds)   OVER (ORDER BY o.week_start) AS ending_subscribers,
           b.starting_mrr         + SUM(o.mrr_change) OVER (ORDER BY o.week_start) AS ending_mrr
    FROM orders o CROSS JOIN baseline b
)
SELECT
    r.week_start,
    r.new_connects,
    r.disconnects,
    r.net_adds,
    r.upgrades,
    r.ending_subscribers,
    ROUND(r.ending_mrr, 0)                                              AS ending_mrr,
    ROUND(r.ending_mrr / r.ending_subscribers, 2)                       AS arpu,
    ROUND(100.0 * r.disconnects / LAG(r.ending_subscribers) OVER (ORDER BY r.week_start), 3) AS weekly_churn_pct,
    t.tickets,
    ROUND(1000.0 * t.tickets / r.ending_subscribers, 1)                 AS tickets_per_1k_subs,
    t.avg_resolution_hours,
    t.sla_met_pct,
    t.fcr_pct
FROM running r
JOIN tickets t USING (week_start)
ORDER BY r.week_start;
