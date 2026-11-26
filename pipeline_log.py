"""Shared append-only log for download.py / process.py / ecmwf_map.py.

Each stage calls log_event() on successful completion, writing a DATE line
(when the stage ran) followed by that stage's own completion label, e.g.:

    DATE 2026-11-26 06:00:05
    DOWNLOADLOG 2026-11-26 06:00:05
    DATE 2026-11-26 06:02:30
    PROCESSLOG 2026-11-26 06:02:30
"""

from datetime import datetime
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "pipeline.log"


def log_event(label):
    now = datetime.now()
    stamp = f"{now:%Y-%m-%d %H:%M:%S}"
    with open(LOG_PATH, "a") as f:
        f.write(f"DATE {stamp}\n")
        f.write(f"{label} {stamp}\n")
