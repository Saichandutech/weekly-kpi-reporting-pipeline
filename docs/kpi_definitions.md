# KPI Definitions

Every KPI in the report is calculated in SQL (`sql/02_views.sql`), so the Excel report, the email and any BI tool
reading the views all use the same definition. Weeks run **Monday to Sunday** and are labelled by their Monday date.

| KPI | Definition | Source | Good direction |
|---|---|---|---|
| **New connects** | Orders of type `New Connect` in the week | `fact_orders` | ▲ |
| **Disconnects** | Orders of type `Disconnect` in the week | `fact_orders` | ▼ |
| **Net subscriber adds** | New connects − disconnects | `fact_orders` | ▲ |
| **Ending subscribers** | Baseline subscribers + running total of net adds (`SUM() OVER (ORDER BY week_start)`) | `fact_orders`, `baseline` | ▲ |
| **Monthly recurring revenue (MRR)** | Baseline MRR + running total of MRR changes (new connects and upgrades add, disconnects and downgrades subtract) | `fact_orders`, `baseline` | ▲ |
| **ARPU** | Ending MRR ÷ ending subscribers | derived | ▲ |
| **Weekly churn %** | Disconnects ÷ previous week's ending subscribers (`LAG()`) | derived | ▼ |
| **Tickets per 1k subscribers** | Tickets opened ÷ ending subscribers × 1,000 | `fact_tickets` | ▼ |
| **Avg resolution time (hrs)** | Mean `resolution_hours` of tickets opened in the week (open tickets excluded) | `fact_tickets` | ▼ |
| **Tickets resolved within 24h** | Resolved tickets with `resolution_hours ≤ 24` ÷ resolved tickets | `fact_tickets` | ▲ |
| **First-contact resolution** | Tickets solved on first contact ÷ all tickets | `fact_tickets` | ▲ |

## Data rules applied before KPIs are calculated

- Orders on the `TEST` channel are excluded.
- Duplicate `order_id` / `ticket_id` rows are removed.
- Ticket timestamps arrive in US format (`MM/DD/YYYY HH:MM`) and are standardised to ISO.
- The load **stops** (and is logged as FAILED in `etl_runs`) if IDs are not unique, a region code is unknown,
  an order type is unexpected, or any day has no orders, which usually means a source file is missing.

## Call-out logic

A KPI is flagged when this week's value is more than **2 standard deviations** from its average over the previous
8 weeks. Cumulative KPIs (subscribers, MRR, ARPU) are skipped because they trend by design.

A region is flagged when its average weekly **disconnects** or **tickets** over the last 4 weeks are **15% or more**
above its average for the 8 weeks before that.
