"""
Generate daily forecast maps (GeoJSON contours + SVG colorbars) from an
ECMWF GRIB2 crop, mirroring the conventions of the WRF map generator at
/Users/saim/Projects/Python/weathermap/process_forecast_map.py (same
contourf -> geojsoncontour -> GeoJSON/SVG pipeline, same rainfall palette,
same temp/wind/humidity/cloud colormaps and levels where the parameter
overlaps).

Reuses process.py's load_grid()/local_day_groups() so the gridded values
plotted here match exactly what gets written to the database -- no
duplicate GRIB parsing or unit conversions.

Produces one map per day for the next N forecast days (default 5), for
every parameter available from the ECMWF crop: rf, tmin, tmax, rh, ws
(wind speed), gust (wind gust), wd (wind direction), cldcvr (cloud cover).

Run the same way as download.py / process.py:

    python ecmwf_map.py            # most recent bd_*.grib2 in ECMWF/
    python ecmwf_map.py 20261125   # the file for a specific local date
    python ecmwf_map.py 20261125 --days 7

Output: MAPS/<fdate>/<var>.F_<fdate>.S_<start>.E_<end>.geojson (+ .svg),
plus MAPS/<fdate>/info.<fdate>.json listing all generated files.
"""

import argparse
import json

import matplotlib
matplotlib.use("Agg")  # headless/cron-safe backend, must be set before pyplot import
import matplotlib.pyplot as plt
import geojsoncontour as gj
from scipy.ndimage import zoom

import process
import pipeline_log

MAPS_DIR = process.ECMWF_DIR / "MAPS"
DEFAULT_DAYS = 5

RF_COLORS = ["#C7EDFF", "#84CEFF", "#3FACFF", "#0A92FF", "#0676E7", "#06408F",
             "#1A9F22", "#22E50B", "#70FF0C", "#FFFF34", "#FEF206", "#FF6D06",
             "#FF8E2F", "#FFB978", "#FF5986", "#FF2660", "#D30606", "#980606",
             "#71068E", "#D706FF", "#F373FF", "#FFB9FF", "#FFFDFF", "#EAEAEA"]
RF_LEVELS = [0.1, 0.5, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90, 100, 125, 150, 200, 300]
TEMP_LEVELS = [-5, *range(0, 42, 3)]
WIND_LEVELS = [1, 2, 3, 5, 7, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120]

# key: output filename prefix / info.json key. source: grid dict key (defaults to key).
# agg: "accum" (end-start, for accumulated fields like rainfall), "min", "max", or "avg".
MAP_VARS = [
    dict(key="rf", agg="accum", colors=RF_COLORS, levels=RF_LEVELS, extend="max", vmin=1, zoom=True),
    dict(key="tmin", source="temp", agg="min", cmap="bwr", levels=TEMP_LEVELS, extend="both", zoom=True),
    dict(key="tmax", source="temp", agg="max", cmap="bwr", levels=TEMP_LEVELS, extend="both", zoom=True),
    dict(key="rh", agg="avg", cmap="summer_r", zoom=False),
    dict(key="ws", source="windspd", agg="avg", cmap="GnBu", levels=WIND_LEVELS, extend="max", zoom=True),
    dict(key="gust", source="windgust", agg="max", cmap="GnBu", levels=WIND_LEVELS, extend="max", zoom=True),
    dict(key="wd", source="winddir", agg="avg", cmap="hsv", levels=list(range(0, 361, 30)), zoom=True),
    dict(key="cldcvr", agg="avg", cmap="cool", levels=range(1, 9), zoom=True),
]


def daily_aggregate(arr3d, i0, i1, agg):
    if agg == "accum":
        return arr3d[i1] - arr3d[i0]
    window = arr3d[i0:i1 + 1]
    if agg == "min":
        return window.min(axis=0)
    if agg == "max":
        return window.max(axis=0)
    if agg == "avg":
        return window.mean(axis=0)
    raise ValueError(f"unknown agg {agg!r}")


def plot_and_save(lons, lats, data2d, spec, out_dir, json_suffix, cmap_suffix):
    x, y, z = (zoom(lons, 4), zoom(lats, 4), zoom(data2d, 4)) if spec.get("zoom") else (lons, lats, data2d)

    kwargs = {}
    if "colors" in spec:
        kwargs["colors"] = spec["colors"]
    else:
        kwargs["cmap"] = spec["cmap"]
    for opt in ("levels", "extend", "vmin"):
        if opt in spec:
            kwargs[opt] = spec[opt]

    ax = plt.axes()
    cont = plt.contourf(x, y, z, **kwargs)
    geojson_str = gj.contourf_to_geojson(cont)

    key = spec["key"]
    geojson_path = out_dir / f"{key}{json_suffix}"
    svg_path = out_dir / f"{key}{cmap_suffix}"
    geojson_path.write_text(geojson_str)

    cbar = plt.colorbar(orientation="horizontal")
    cbar.outline.set_visible(False)
    ax.remove()
    plt.savefig(svg_path, pad_inches=0, bbox_inches="tight")
    plt.close()

    return geojson_path.name, svg_path.name


def generate_maps(grib_path, num_days=DEFAULT_DAYS):
    grid = process.load_grid(grib_path)
    valid_time = grid["valid_time"]
    n_steps = len(valid_time) - 1
    day_groups = process.local_day_groups(valid_time, n_steps)[:num_days]
    if len(day_groups) < num_days:
        print(f"  WARNING: only {len(day_groups)} day(s) available in this file (requested {num_days})")

    fdate = f"{grid['forecast_run_date']:%Y%m%d}"
    out_dir = MAPS_DIR / fdate
    out_dir.mkdir(parents=True, exist_ok=True)

    finfo = {"fdate": fdate, **{spec["key"]: [] for spec in MAP_VARS}}

    for i0, i1 in day_groups:
        start_dt = process.to_pydatetime(valid_time[i0])
        end_dt = process.to_pydatetime(valid_time[i1])
        json_suffix = f".F_{fdate}.S_{start_dt:%Y%m%d%H}.E_{end_dt:%Y%m%d%H}.geojson"
        cmap_suffix = f".F_{fdate}.S_{start_dt:%Y%m%d%H}.E_{end_dt:%Y%m%d%H}.svg"

        for spec in MAP_VARS:
            data2d = daily_aggregate(grid[spec.get("source", spec["key"])], i0, i1, spec["agg"])
            geojson_name, svg_name = plot_and_save(
                grid["lons"], grid["lats"], data2d, spec, out_dir, json_suffix, cmap_suffix
            )
            finfo[spec["key"]].append({
                "file": geojson_name,
                "cmap": svg_name,
                "start": f"{start_dt:%Y-%m-%d %H:%M}",
                "end": f"{end_dt:%Y-%m-%d %H:%M}",
            })
        print(f"  day {start_dt:%Y-%m-%d} -> {end_dt:%Y-%m-%d}: {len(MAP_VARS)} maps")

    info_path = out_dir / f"info.{fdate}.json"
    info_path.write_text(json.dumps(finfo))
    pipeline_log.log_event("MAPPROCESSLOG")
    return out_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", nargs="?", default=None,
                         help="forecast date as YYYYMMDD (Bangladesh local date); "
                              "selects the matching bd_*.grib2 file")
    parser.add_argument("--grib", default=None,
                         help=f"path to the cropped GRIB2 file (default: most recent {process.GRIB_GLOB})")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                         help=f"number of forecast days to map (default {DEFAULT_DAYS})")
    args = parser.parse_args()
    grib_path = args.grib or (process.find_grib_for_date(args.date) if args.date else process.latest_grib_file())

    print(f"Generating {args.days}-day forecast maps from {grib_path} ...")
    out_dir = generate_maps(grib_path, args.days)
    print(f"Done. Maps written to {out_dir}")


if __name__ == "__main__":
    main()
