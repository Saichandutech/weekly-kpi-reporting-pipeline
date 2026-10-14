"""
Weekly KPI pipeline entry point.

    python run_pipeline.py                 # ETL + report for the latest complete week
    python run_pipeline.py --week 2026-09-14
    python run_pipeline.py --regenerate    # rebuild synthetic source data first

Schedule it with cron / Windows Task Scheduler, or the included GitHub Actions workflow.
"""
import argparse
import logging
import sys
from pathlib import Path

from pipeline import etl, report

ROOT = Path(__file__).resolve().parent


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--week", help="Week start date (Monday, YYYY-MM-DD). Default: latest week in the warehouse.")
    p.add_argument("--regenerate", action="store_true", help="Regenerate synthetic source files before running.")
    args = p.parse_args(argv)

    (ROOT / "output").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(ROOT / "output" / "pipeline.log")],
    )
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    log = logging.getLogger("pipeline")

    if args.regenerate:
        from pipeline import generate_sources
        generate_sources.main()

    etl.run()
    data = report.load(week=args.week)
    xlsx = report.write_excel(data)
    email = report.write_html(data)
    extracts = report.write_bi_extracts()
    log.info("Report week: %s", data.week)
    for note in data.callouts:
        log.info("Call-out: %s", note)
    log.info("Wrote %s", xlsx.relative_to(ROOT))
    log.info("Wrote %s", email.relative_to(ROOT))
    log.info("Wrote %d BI extracts to output/bi_extracts/", len(extracts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
