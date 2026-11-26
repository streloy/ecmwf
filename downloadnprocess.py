"""
Daily production entrypoint: download + process + map one day's ECMWF
forecast, then prune anything older than the retention window.

Combines download.py (fetch + crop to Bangladesh), process.py (zonal
stats -> weather_api MySQL, which also upserts apis_system_state for the
ECMWF source row), and ecmwf_map.py (daily forecast maps -> MAPS/) into a
single cron-friendly command, and removes stale data so ECMWF_RAW/,
ECMWF/, and the two apis_upazila_forecast_*_ecmwf tables don't grow
unbounded. If ECMWF/bd_<date>.grib2 already exists for the target date,
the download step is skipped and that file is processed directly:

    python downloadnprocess.py            # today's local forecast (cron case)
    python downloadnprocess.py 20261125    # backfill a specific local date
    python downloadnprocess.py --keep-days 14
    python downloadnprocess.py --dry-run   # download+process as normal, skip cleanup deletions

Retention is relative to the date being processed, not wall-clock "today",
so backfills don't prune data around the date you're explicitly fetching.
"""

import argparse
import glob
from datetime import datetime, timedelta
from pathlib import Path

import pymysql

import download
import process
import ecmwf_map

RETENTION_DAYS = 10


def cleanup_old_files(folder, pattern, cutoff_date):
    removed = []
    for path in glob.glob(str(folder / pattern)):
        date_str = Path(path).stem.split("_")[-1]
        try:
            file_date = datetime.strptime(date_str, "%Y%m%d").date()
        except ValueError:
            continue
        if file_date < cutoff_date:
            Path(path).unlink()
            removed.append(path)
    return removed


def cleanup_old_rows(cutoff_date):
    conn = pymysql.connect(**process.DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM apis_source WHERE name = %s", (process.SOURCE_NAME,))
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"apis_source has no row named {process.SOURCE_NAME!r}")
            source_id = row[0]

            counts = {}
            for table in ("apis_upazila_forecast_daily_ecmwf", "apis_upazila_forecast_steps_ecmwf"):
                cur.execute(
                    f"DELETE FROM {table} WHERE forecast_date < %s AND source_id = %s",
                    (cutoff_date, source_id),
                )
                counts[table] = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return counts


def get_or_download(local_date):
    """Reuse the crop already on disk for local_date, if present, instead of re-downloading."""
    cropped_path = download.CLIPPED_DIR / f"bd_{local_date:%Y%m%d}.grib2"
    if cropped_path.exists():
        print(f"  {cropped_path} already exists, skipping download")
        return cropped_path
    _, cropped_path = download.download_and_crop(local_date)
    return cropped_path


def cleanup(local_date, keep_days, dry_run):
    cutoff_date = local_date - timedelta(days=keep_days)
    print(f"=== Cleaning up data older than {cutoff_date} (keeping last {keep_days} days) ===")

    if dry_run:
        print("  (dry run, skipping cleanup)")
        return

    removed_raw = cleanup_old_files(download.RAW_DIR, "ifs_0p1_*.grib2", cutoff_date)
    removed_clipped = cleanup_old_files(download.CLIPPED_DIR, "bd_*.grib2", cutoff_date)
    for path in removed_raw + removed_clipped:
        print(f"  removed {path}")
    if not (removed_raw or removed_clipped):
        print("  no old files to remove")

    row_counts = cleanup_old_rows(cutoff_date)
    for table, n in row_counts.items():
        print(f"  removed {n} row(s) from {table}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", nargs="?", default=None,
                         help="local (Bangladesh) forecast date as YYYYMMDD; defaults to today")
    parser.add_argument("--keep-days", type=int, default=RETENTION_DAYS,
                         help=f"delete files/rows older than this many days (default {RETENTION_DAYS})")
    parser.add_argument("--dry-run", action="store_true",
                         help="compute everything but skip the MySQL write and the cleanup deletions")
    args = parser.parse_args()
    local_date = datetime.strptime(args.date, "%Y%m%d").date() if args.date else download.today_local()

    print(f"=== Downloading forecast for {local_date} ===")
    cropped_path = get_or_download(local_date)

    print(f"=== Processing {cropped_path} ===")
    process.process_file(str(cropped_path), replace=True, dry_run=args.dry_run)

    if not args.dry_run:
        print(f"=== Generating forecast maps from {cropped_path} ===")
        ecmwf_map.generate_maps(str(cropped_path))

    cleanup(local_date, args.keep_days, args.dry_run)


if __name__ == "__main__":
    main()
