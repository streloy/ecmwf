"""
Zonal-stats pipeline: ECMWF open-data GRIB2 (ECMWF/bd_<YYYYMMDD>.grib2, YYYYMMDD being
the local Bangladesh forecast date) + upazila boundaries (data/bd_adm3_boundary.topojson)
-> weather_api MySQL tables
apis_upazila_forecast_daily_ecmwf and apis_upazila_forecast_steps_ecmwf.

Mirrors the conventions of the `gen_upazila_forecast_ecmwf` Django management
command in the sibling weather_api project (same schema, same ECMWF source
row, same accumulated-precip delta handling), but reads GRIB2 via cfgrib
instead of NetCDF and derives upazila polygons from the local topojson
instead of a zipped shapefile + pyscissor.

All stored timestamps (forecast_date, step_start, step_end) are in
Bangladesh Standard Time (UTC+6, no DST), not the GRIB file's native UTC.
"Daily" rows are grouped by local calendar date (midnight-to-midnight BST),
not by a fixed step count, so this is correct regardless of which ECMWF
run cycle (00/06/12/18Z) produced the input file.

Populates exactly the requested parameters: temp, rf (precipitation),
windspd, windgust, winddir, rh (relative humidity), cldcvr (cloud cover).
Derived from GRIB fields 2t, tp, 10u, 10v, 10fg, 2d, tcc (see download.py).
rh uses the same `100 - 5*(temp - tempdew)` approximation as the sibling
weather_api Django app's gen_upazila_forecast_ecmwf command, for consistency
across sources. cldcvr converts ECMWF's 0-1 total cloud cover fraction to
the 0-8 Okta scale used by apis_parameter. tempdew itself is only an
intermediate and is not stored (not one of the requested output parameters).
"""

import argparse
import glob
import json
import os
from datetime import datetime
from pathlib import Path

import eccodes
import numpy as np
import pymysql
import xarray as xr
from dotenv import load_dotenv
from shapely.geometry import Polygon, MultiPolygon, box
from shapely.validation import make_valid
from shapely.strtree import STRtree

import pipeline_log

ECMWF_DIR = Path(__file__).resolve().parent
CLIPPED_DIR = ECMWF_DIR / "ECMWF"  # download.py writes crops here, named bd_<YYYYMMDD>.grib2 (local date)
GRIB_GLOB = str(CLIPPED_DIR / "bd_*.grib2")
TOPOJSON_PATH = str(ECMWF_DIR / "data" / "bd_adm3_boundary.topojson")

load_dotenv(ECMWF_DIR / ".env")

DB_CONFIG = dict(
    host=os.environ.get("DB_HOST", "127.0.0.1"),
    port=int(os.environ.get("DB_PORT", "3306")),
    user=os.environ.get("DB_USER", "root"),
    password=os.environ.get("DB_PASSWORD", ""),
    database=os.environ.get("DB_NAME", "weather_api"),
)

SOURCE_NAME = "ECMWF"
DAY_START_HOUR, DAY_END_HOUR = 6, 18  # local-time window counted as "day" for val_avg_day/night
BST_OFFSET = np.timedelta64(6, "h")  # Bangladesh Standard Time, UTC+6, no DST

PARAM_NAMES = ["rf", "temp", "windspd", "windgust", "winddir", "rh", "cldcvr"]
ACCUMULATED = {"rf"}


def decode_topojson(path):
    """Decode a quantized TopoJSON into {ADM3_PCODE: shapely geometry}."""
    with open(path) as f:
        topo = json.load(f)

    scale_x, scale_y = topo["transform"]["scale"]
    trans_x, trans_y = topo["transform"]["translate"]
    raw_arcs = topo["arcs"]

    def decode_arc(arc):
        coords = []
        x = y = 0
        for dx, dy in arc:
            x += dx
            y += dy
            coords.append((x * scale_x + trans_x, y * scale_y + trans_y))
        return coords

    arcs = [decode_arc(a) for a in raw_arcs]

    def arc_coords(idx):
        return arcs[idx] if idx >= 0 else list(reversed(arcs[~idx]))

    def ring_from_arcs(arc_indices):
        coords = []
        for idx in arc_indices:
            seg = arc_coords(idx)
            if coords and coords[-1] == seg[0]:
                coords.extend(seg[1:])
            else:
                coords.extend(seg)
        return coords

    def to_shapely(g):
        if g["type"] == "Polygon":
            rings = [ring_from_arcs(r) for r in g["arcs"]]
            poly = Polygon(rings[0], rings[1:])
        elif g["type"] == "MultiPolygon":
            polys = []
            for part in g["arcs"]:
                rings = [ring_from_arcs(r) for r in part]
                polys.append(Polygon(rings[0], rings[1:]))
            poly = MultiPolygon(polys)
        else:
            raise ValueError(f"unsupported geometry type: {g['type']}")
        return poly if poly.is_valid else make_valid(poly)

    geoms = topo["objects"][next(iter(topo["objects"]))]["geometries"]
    return {int(g["properties"]["ADM3_PCODE"]): to_shapely(g) for g in geoms}


# GRIB2 numeric parameter identities (discipline, parameterCategory, parameterNumber,
# typeOfLevel, level) for the fields we need. These are literal values encoded directly
# in each GRIB2 message (Section 4), independent of the local eccodes/defs-tables version
# -- unlike shortName/cfVarName/paramId, which are *resolved* from those tables and can
# differ across eccodes versions (e.g. an older eccodes may not recognize ECMWF's local-
# table codes for tp/tcc and fall back to an ambiguous "unknown" grouping, or may label
# wind gust "gust" instead of "10fg"). Filtering on the raw identity sidesteps all of that.
PARAM_IDENTITY = {
    "t2m": dict(discipline=0, parameterCategory=0, parameterNumber=0, typeOfLevel="heightAboveGround", level=2),
    "d2m": dict(discipline=0, parameterCategory=0, parameterNumber=6, typeOfLevel="heightAboveGround", level=2),
    "tp": dict(discipline=0, parameterCategory=1, parameterNumber=193, typeOfLevel="surface"),
    "tcc": dict(discipline=0, parameterCategory=6, parameterNumber=192, typeOfLevel="entireAtmosphere"),
    "u10": dict(discipline=0, parameterCategory=2, parameterNumber=2, typeOfLevel="heightAboveGround", level=10),
    "v10": dict(discipline=0, parameterCategory=2, parameterNumber=3, typeOfLevel="heightAboveGround", level=10),
    "gust": dict(discipline=0, parameterCategory=2, parameterNumber=22, typeOfLevel="heightAboveGround", level=10),
}


def open_param(grib_path, name):
    """Open the single GRIB2 field matching PARAM_IDENTITY[name]'s raw numeric identity."""
    ds = xr.open_dataset(
        grib_path, engine="cfgrib",
        backend_kwargs={"indexpath": "", "filter_by_keys": PARAM_IDENTITY[name]},
    )
    if not ds.data_vars:
        raise RuntimeError(f"no GRIB2 message matching {name!r} identity {PARAM_IDENTITY[name]} in {grib_path}")
    return ds, ds[next(iter(ds.data_vars))]


def distinct_param_ids(grib_path, name):
    """All distinct paramIds in the file matching PARAM_IDENTITY[name]'s raw numeric identity.

    Used for fields (wind gust) that ECMWF sometimes splits across more than one paramId
    within a single file (e.g. the first output step re-encoded separately) -- cfgrib can't
    merge those into one dataset (they have incompatible 'step' shapes), so each must be
    opened on its own and combined by valid_time instead.
    """
    identity = PARAM_IDENTITY[name]
    found = set()
    with open(grib_path, "rb") as f:
        while True:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                if all(eccodes.codes_get(gid, key) == value for key, value in identity.items()):
                    found.add(eccodes.codes_get(gid, "paramId"))
            finally:
                eccodes.codes_release(gid)
    return sorted(found)


def load_windgust(grib_path, target_valid_time):
    """Load 10m wind gust, aligned to target_valid_time.

    ECMWF sometimes splits this field across multiple GRIB paramIds within one file (e.g.
    the regular max-since-previous-output-step series plus a separately-encoded first
    step). All matching paramIds are merged here by valid_time; any step present in none
    of them is filled from the nearest available step (with a warning) rather than left
    as NaN.
    """
    param_ids = distinct_param_ids(grib_path, "gust")
    if not param_ids:
        raise RuntimeError(f"no wind gust field matching {PARAM_IDENTITY['gust']} found in {grib_path}")

    sources = []
    for param_id in param_ids:
        ds = xr.open_dataset(
            grib_path, engine="cfgrib",
            backend_kwargs={"indexpath": "", "filter_by_keys": {"paramId": param_id}},
        )
        if not ds.data_vars:
            continue
        var = next(iter(ds.data_vars))
        arr = ds[var].values
        vt = ds.valid_time.values
        if arr.ndim == 2:  # single step collapsed to a scalar valid_time coordinate
            arr = arr[np.newaxis, ...]
            vt = np.atleast_1d(vt)
        for t, arr2d in zip(vt, arr):
            sources.append((t, arr2d))

    by_time = {t: arr for t, arr in sources}
    shape2d = next(iter(by_time.values())).shape
    result = np.full((len(target_valid_time),) + shape2d, np.nan, dtype=np.float32)
    for i, t in enumerate(target_valid_time):
        if t in by_time:
            result[i] = by_time[t]

    missing = [i for i in range(len(target_valid_time)) if np.isnan(result[i]).all()]
    if missing:
        print(f"  WARNING: windgust missing for {len(missing)} step(s), "
              f"filling from nearest available step: {missing}")
        available = [i for i in range(len(target_valid_time)) if i not in missing]
        for i in missing:
            nearest = min(available, key=lambda a: abs(a - i))
            result[i] = result[nearest]

    return result


def load_grid(grib_path):
    """Load ECMWF GRIB2 fields and derive the 7 requested parameters."""
    ds_t2m, t2m_da = open_param(grib_path, "t2m")
    _, d2m_da = open_param(grib_path, "d2m")
    _, tp_da = open_param(grib_path, "tp")
    _, tcc_da = open_param(grib_path, "tcc")
    _, u10_da = open_param(grib_path, "u10")
    _, v10_da = open_param(grib_path, "v10")

    lats = ds_t2m.latitude.values
    lons = ds_t2m.longitude.values
    valid_time_utc = ds_t2m.valid_time.values

    rf = np.clip(tp_da.values * 1000.0, a_min=0, a_max=None)  # m -> mm, accumulated since run start
    temp = t2m_da.values - 273.15  # K -> C
    tempdew = d2m_da.values - 273.15  # K -> C, intermediate only (for rh)
    rh = np.clip(100.0 - 5.0 * (temp - tempdew), 0.0, 100.0)  # %
    cldcvr = np.clip(tcc_da.values * 8.0, 0.0, 8.0)  # fraction (0-1) -> Okta (0-8)

    u10 = u10_da.values
    v10 = v10_da.values
    windspd = np.sqrt(u10 ** 2 + v10 ** 2) * 3.6  # m/s -> km/h
    winddir = np.degrees(np.arctan2(-u10, -v10)) % 360.0  # meteorological "from" direction
    windgust = load_windgust(grib_path, valid_time_utc) * 3.6  # m/s -> km/h, matched by UTC valid_time

    # Everything downstream (storage, day grouping) uses Bangladesh local time (UTC+6).
    valid_time = valid_time_utc + BST_OFFSET
    forecast_run_date = (np.datetime64(ds_t2m.time.values) + BST_OFFSET).astype("datetime64[D]").astype(object)

    return {
        "lats": lats,
        "lons": lons,
        "valid_time": valid_time,
        "forecast_run_date": forecast_run_date,
        "rf": rf,
        "temp": temp,
        "windspd": windspd,
        "windgust": windgust,
        "winddir": winddir,
        "rh": rh,
        "cldcvr": cldcvr,
    }


def build_upazila_weights(upazila_geoms, lats, lons):
    """For each ADM3_PCODE, find overlapping grid cells and their intersection-area weights."""
    res_lat = abs(np.diff(lats).mean())
    res_lon = abs(np.diff(lons).mean())

    cell_boxes = []
    cell_rc = []
    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            cell_boxes.append(box(lon - res_lon / 2, lat - res_lat / 2, lon + res_lon / 2, lat + res_lat / 2))
            cell_rc.append((i, j))

    tree = STRtree(cell_boxes)

    weights = {}
    for pcode, geom in upazila_geoms.items():
        candidates = tree.query(geom, predicate="intersects")
        cells = []
        for idx in candidates:
            area = geom.intersection(cell_boxes[idx]).area
            if area > 0:
                r, c = cell_rc[idx]
                cells.append((r, c, area))
        if not cells:
            nearest_idx = tree.nearest(geom)
            r, c = cell_rc[nearest_idx]
            cells = [(r, c, 1.0)]
        weights[pcode] = cells
    return weights


def zonal_stats(arr2d, cells):
    vals = np.array([arr2d[r, c] for r, c, _ in cells])
    ws = np.array([w for _, _, w in cells])
    return float(vals.min()), float(np.average(vals, weights=ws)), float(vals.max())


def temporal_minmeanmax(arr3d, i0, i1):
    window = arr3d[i0:i1 + 1]
    return window.min(axis=0), window.mean(axis=0), window.max(axis=0)


def to_pydatetime(np_dt):
    return np_dt.astype("datetime64[s]").item()


def fetch_lookups(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT ADM3_PCODE, id FROM apis_upazila")
        upazila_ids = dict(cur.fetchall())

        cur.execute("SELECT name, id FROM apis_parameter WHERE name IN %s", (tuple(PARAM_NAMES),))
        parameter_ids = dict(cur.fetchall())
        missing = set(PARAM_NAMES) - set(parameter_ids)
        if missing:
            raise RuntimeError(f"apis_parameter is missing rows for: {sorted(missing)}")

        cur.execute("SELECT id FROM apis_source WHERE name = %s", (SOURCE_NAME,))
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"apis_source has no row named {SOURCE_NAME!r}")
        source_id = row[0]

    return upazila_ids, parameter_ids, source_id


def local_day_groups(valid_time, n_steps):
    """Group step-interval indices [0, n_steps) by the local calendar date of their start time."""
    local_dates = [to_pydatetime(vt).date() for vt in valid_time]
    groups = []
    for t in range(n_steps):
        if groups and local_dates[t] == groups[-1][0]:
            groups[-1][1].append(t)
        else:
            groups.append((local_dates[t], [t]))
    return [(idxs[0], idxs[-1] + 1) for _, idxs in groups]


def build_rows(grid, weights, upazila_ids, parameter_ids, source_id):
    valid_time = grid["valid_time"]
    n_steps = len(valid_time) - 1  # number of step intervals
    forecast_date = grid["forecast_run_date"]
    day_groups = local_day_groups(valid_time, n_steps)

    step_rows = []
    daily_rows = []

    for param in PARAM_NAMES:
        parameter_id = parameter_ids[param]
        accumulated = param in ACCUMULATED
        arr3d = grid[param]

        # --- per-step rows (one row per 3h interval) ---
        for t in range(n_steps):
            step_start = to_pydatetime(valid_time[t])
            step_end = to_pydatetime(valid_time[t + 1])

            if accumulated:
                delta2d = arr3d[t + 1] - arr3d[t]
                min2d = mean2d = max2d = delta2d
            else:
                min2d, mean2d, max2d = temporal_minmeanmax(arr3d, t, t + 1)

            for pcode, upazila_id in upazila_ids.items():
                cells = weights.get(pcode)
                if not cells:
                    continue
                val_min, _, _ = zonal_stats(min2d, cells)
                _, val_avg, _ = zonal_stats(mean2d, cells)
                _, _, val_max = zonal_stats(max2d, cells)
                step_rows.append((
                    forecast_date, step_start, step_end,
                    val_min, val_avg, val_max,
                    parameter_id, source_id, upazila_id,
                ))

        # --- daily rows, one per local (BST) calendar date ---
        for i0, i1 in day_groups:
            day_start = to_pydatetime(valid_time[i0])
            day_end = to_pydatetime(valid_time[i1])

            if accumulated:
                delta2d = arr3d[i1] - arr3d[i0]
                min2d = mean2d = max2d = delta2d
            else:
                min2d, mean2d, max2d = temporal_minmeanmax(arr3d, i0, i1)

            # day/night split on local (BST) hour-of-day, using the per-step mean field
            day_idxs, night_idxs = [], []
            for t in range(i0, i1):
                local_hour = to_pydatetime(valid_time[t]).hour
                (day_idxs if DAY_START_HOUR <= local_hour < DAY_END_HOUR else night_idxs).append(t)

            if not accumulated and day_idxs:
                day_mean2d = arr3d[day_idxs].mean(axis=0)
            else:
                day_mean2d = mean2d
            if not accumulated and night_idxs:
                night_mean2d = arr3d[night_idxs].mean(axis=0)
            else:
                night_mean2d = mean2d

            for pcode, upazila_id in upazila_ids.items():
                cells = weights.get(pcode)
                if not cells:
                    continue
                val_min, _, _ = zonal_stats(min2d, cells)
                _, val_avg, _ = zonal_stats(mean2d, cells)
                _, _, val_max = zonal_stats(max2d, cells)
                _, val_avg_day, _ = zonal_stats(day_mean2d, cells)
                _, val_avg_night, _ = zonal_stats(night_mean2d, cells)
                daily_rows.append((
                    forecast_date, day_start, day_end,
                    val_min, val_avg, val_max, val_avg_day, val_avg_night,
                    parameter_id, source_id, upazila_id,
                ))

    return step_rows, daily_rows


STEPS_INSERT = """
    INSERT INTO apis_upazila_forecast_steps_ecmwf
        (forecast_date, step_start, step_end, val_min, val_avg, val_max, parameter_id, source_id, upazila_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

DAILY_INSERT = """
    INSERT INTO apis_upazila_forecast_daily_ecmwf
        (forecast_date, step_start, step_end, val_min, val_avg, val_max, val_avg_day, val_avg_night,
         parameter_id, source_id, upazila_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def write_rows(conn, step_rows, daily_rows, forecast_date, source_id, replace, chunk_size=5000):
    with conn.cursor() as cur:
        if replace:
            cur.execute(
                "DELETE FROM apis_upazila_forecast_steps_ecmwf WHERE forecast_date = %s AND source_id = %s",
                (forecast_date, source_id),
            )
            cur.execute(
                "DELETE FROM apis_upazila_forecast_daily_ecmwf WHERE forecast_date = %s AND source_id = %s",
                (forecast_date, source_id),
            )

        for i in range(0, len(step_rows), chunk_size):
            cur.executemany(STEPS_INSERT, step_rows[i:i + chunk_size])
        for i in range(0, len(daily_rows), chunk_size):
            cur.executemany(DAILY_INSERT, daily_rows[i:i + chunk_size])
    conn.commit()


def update_system_state(conn, source_id, local_date):
    """Upsert apis_system_state.last_update/updated_at for this source."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM apis_system_state WHERE source_id = %s", (source_id,))
        existing = cur.fetchone()
        now = datetime.now()
        if existing is None:
            cur.execute(
                "INSERT INTO apis_system_state (source_id, last_update, updated_at) VALUES (%s, %s, %s)",
                (source_id, local_date, now),
            )
        else:
            cur.execute(
                "UPDATE apis_system_state SET last_update = %s, updated_at = %s WHERE source_id = %s",
                (local_date, now, source_id),
            )
    conn.commit()


def latest_grib_file():
    matches = sorted(glob.glob(GRIB_GLOB))  # YYYYMMDD filename suffix sorts chronologically
    if not matches:
        raise FileNotFoundError(
            f"no file matching {GRIB_GLOB!r} found; run download.py first, or pass --grib explicitly"
        )
    return matches[-1]


def find_grib_for_date(date_str):
    """Path to the bd_<date_str>.grib2 file for the given local (Bangladesh) forecast date.

    download.py names its crop by the local date directly (see its docstring for the
    UTC-run <-> local-date mapping), so this is a plain filename lookup.
    """
    datetime.strptime(date_str, "%Y%m%d")  # validate format early, with a clear error
    path = CLIPPED_DIR / f"bd_{date_str}.grib2"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found; run download.py {date_str} first, or pass --grib explicitly"
        )
    return str(path)


def process_file(grib_path, topojson_path=TOPOJSON_PATH, replace=False, dry_run=False):
    """Run the full zonal-stats pipeline for one GRIB2 file. Returns the file's local forecast_date."""
    print(f"Decoding upazila boundaries from {topojson_path} ...")
    upazila_geoms = decode_topojson(topojson_path)
    print(f"  {len(upazila_geoms)} upazila polygons decoded")

    print(f"Loading GRIB2 grid from {grib_path} ...")
    grid = load_grid(grib_path)
    print(f"  grid {grid['lats'].size} x {grid['lons'].size}, "
          f"{len(grid['valid_time'])} valid times, run date {grid['forecast_run_date']}")

    print("Computing per-upazila grid-cell overlap weights ...")
    weights = build_upazila_weights(upazila_geoms, grid["lats"], grid["lons"])
    print(f"  weights built for {len(weights)} upazilas")

    conn = pymysql.connect(**DB_CONFIG)
    try:
        upazila_ids, parameter_ids, source_id = fetch_lookups(conn)
        missing_pcodes = set(weights) - set(upazila_ids)
        if missing_pcodes:
            print(f"  WARNING: {len(missing_pcodes)} topojson ADM3_PCODE(s) have no matching apis_upazila row, "
                  f"skipping: {sorted(missing_pcodes)[:10]}{'...' if len(missing_pcodes) > 10 else ''}")

        print("Building step and daily rows ...")
        step_rows, daily_rows = build_rows(grid, weights, upazila_ids, parameter_ids, source_id)
        print(f"  {len(step_rows)} step rows, {len(daily_rows)} daily rows")

        if dry_run:
            print("Dry run: skipping MySQL write.")
            return grid["forecast_run_date"]

        print(f"Writing to MySQL ({'replace' if replace else 'append'} mode) ...")
        write_rows(conn, step_rows, daily_rows, grid["forecast_run_date"], source_id, replace)
        update_system_state(conn, source_id, grid["forecast_run_date"])
        pipeline_log.log_event("PROCESSLOG")
        print("Done.")
    finally:
        conn.close()

    return grid["forecast_run_date"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", nargs="?", default=None,
                         help="forecast date as YYYYMMDD (Bangladesh local date); "
                              "selects the matching bd_*.grib2 file")
    parser.add_argument("--grib", default=None,
                         help=f"path to the cropped GRIB2 file (default: most recent {GRIB_GLOB})")
    parser.add_argument("--topojson", default=TOPOJSON_PATH, help="path to the upazila boundary topojson")
    parser.add_argument("--replace", action="store_true",
                         help="delete existing ECMWF rows for this forecast_date before inserting")
    parser.add_argument("--dry-run", action="store_true",
                         help="compute everything but skip the MySQL write")
    args = parser.parse_args()
    if args.grib is None:
        args.grib = find_grib_for_date(args.date) if args.date else latest_grib_file()

    process_file(args.grib, args.topojson, args.replace, args.dry_run)


if __name__ == "__main__":
    main()
