"""
Download the ECMWF open-data IFS 0p1 forecast and crop it to Bangladesh.

Meant to run daily at 06:00 Bangladesh time (BST, UTC+6) via cron. At that
time, the most recently published run is the previous UTC day's 18Z cycle,
which in BST is already past midnight -- i.e. it IS the local forecast for
"today". So `python download.py 20261125` means: give me today's (local)
forecast, which is the run initialized 2026-11-24 18:00 UTC.

Run with no argument to default to "today" in Bangladesh local time (the
normal cron case). Pass YYYYMMDD explicitly to backfill/retrieve a specific
local forecast date.

Raw global file -> ECMWF_RAW/ifs_0p1_<YYYYMMDD>.grib2 (YYYYMMDD = local date)
Cropped file    -> ECMWF/bd_<YYYYMMDD>.grib2
"""

import argparse
import subprocess
from datetime import datetime, timedelta, time, timezone
from pathlib import Path

from ecmwf.opendata import Client

import pipeline_log

PARAMS = ["2t", "tp", "10u", "10v", "2d", "tcc", "10fg"]
STEPS = list(range(0, 145, 3))
BBOX = "81.0,99.0,13.0,31.0"  # lon_min,lon_max,lat_min,lat_max, covers Bangladesh
BST_OFFSET = timedelta(hours=6)  # Bangladesh Standard Time, UTC+6, no DST
RUN_HOUR_UTC = 18  # the 18Z cycle is already "tomorrow" in BST

ECMWF_DIR = Path(__file__).resolve().parent
RAW_DIR = ECMWF_DIR / "ECMWF_RAW"
CLIPPED_DIR = ECMWF_DIR / "ECMWF"


def today_local():
    return (datetime.now(timezone.utc) + BST_OFFSET).date()


def download_and_crop(local_date):
    """Download the run for local_date and crop it to Bangladesh. Returns (raw_path, cropped_path)."""
    run_datetime_utc = datetime.combine(local_date - timedelta(days=1), time(RUN_HOUR_UTC))

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    CLIPPED_DIR.mkdir(parents=True, exist_ok=True)
    target = RAW_DIR / f"ifs_0p1_{local_date:%Y%m%d}.grib2"
    cropped_target = CLIPPED_DIR / f"bd_{local_date:%Y%m%d}.grib2"

    client = Client(source="ecmwf", model="ifs", resol="0p1")  # or source="aws"/"azure"/"google"
    client.retrieve(
        type="fc",
        date=run_datetime_utc.date(),
        time=run_datetime_utc.hour,
        step=STEPS,
        param=PARAMS,
        target=str(target),
    )

    # Crop to Bangladesh so process.py only has to load the small grid
    subprocess.run(["cdo", f"sellonlatbox,{BBOX}", str(target), str(cropped_target)], check=True)

    pipeline_log.log_event("DOWNLOADLOG")
    return target, cropped_target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", nargs="?", default=None,
                         help="local (Bangladesh) forecast date as YYYYMMDD; defaults to today")
    args = parser.parse_args()
    local_date = datetime.strptime(args.date, "%Y%m%d").date() if args.date else today_local()
    download_and_crop(local_date)


if __name__ == "__main__":
    main()