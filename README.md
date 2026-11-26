# ecmwf

Downloads the ECMWF open-data IFS 0.1° forecast, crops it to Bangladesh, computes
per-upazila daily/step forecast statistics, generates daily forecast maps, and writes
everything into the `weather_api` MySQL database — a lightweight, GRIB2-based companion
to the existing `weather_api` Django app's NetCDF-based `gen_upazila_forecast_ecmwf`
management command (same schema, same conventions, independent pipeline).

## Setup

1. **Python environment**
   ```
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **System dependency**: [`cdo`](https://code.mpimet.mpg.de/projects/cdo) (Climate Data
   Operators), used to crop the global grid to Bangladesh. On macOS: `brew install cdo`.

3. **Database**: a MySQL/MariaDB server (this project targets a local XAMPP instance) with
   the `weather_api` schema. To bootstrap one from scratch:
   ```
   mysql -u root -p < example_database.sql
   ```
   This creates the `weather_api` database and seeds the reference tables this pipeline
   depends on (divisions, districts, the 544 upazilas, parameters, sources) plus the empty
   schema for the two ECMWF forecast tables.

4. **Environment variables**: copy `.env.example` to `.env` and fill in your DB credentials:
   ```
   cp .env.example .env
   ```
   `.env` is gitignored — never commit real credentials.

## Pipeline

```
download.py  ──>  ECMWF_RAW/ifs_0p1_<date>.grib2  ──(cdo crop)──>  ECMWF/bd_<date>.grib2
                                                                          │
                              ┌───────────────────────────────────────────┤
                              ▼                                           ▼
                      process.py                                  ecmwf_map.py
              (per-upazila zonal stats -> MySQL)          (gridded daily maps -> MAPS/)
```

`<date>` is always **YYYYMMDD in Bangladesh local time (BST, UTC+6)** — not the UTC
timestamp of the underlying model run. The ECMWF 18Z cycle is already past midnight in
Bangladesh, so "today's forecast" (BST) is the run initialized 18:00 UTC the day before.
This mapping is handled automatically; you only ever pass the local date you care about.

### `download.py`
Fetches the 7 GRIB parameters this pipeline needs (`2t`, `tp`, `10u`, `10v`, `2d`, `tcc`,
`10fg`) from the ECMWF open-data API and crops to Bangladesh via `cdo`.
```
python download.py            # today's local forecast (cron case)
python download.py 20261125   # backfill a specific local date
```
Raw global file → `ECMWF_RAW/ifs_0p1_<date>.grib2`. Cropped file → `ECMWF/bd_<date>.grib2`.

### `process.py`
Decodes `data/bd_adm3_boundary.topojson` into upazila polygons, computes grid-cell overlap
weights, and writes per-upazila **daily** and **3-hourly step** statistics for `temp`, `rf`
(rainfall), `windspd`, `windgust`, `winddir`, `rh`, `cldcvr` into
`apis_upazila_forecast_daily_ecmwf` / `apis_upazila_forecast_steps_ecmwf`. On success it
also upserts `apis_system_state.last_update`/`updated_at` for the ECMWF source row.
```
python process.py                    # most recent ECMWF/bd_*.grib2
python process.py 20261125           # a specific local date
python process.py 20261125 --replace # overwrite existing rows for that date
python process.py --dry-run          # compute everything, skip all MySQL writes
```

### `ecmwf_map.py`
Generates GeoJSON contour + SVG colorbar map pairs for the next 5 forecast days (rainfall,
temp min/max, humidity, wind speed/gust/direction, cloud cover), mirroring the conventions
of the WRF-based map generator. Reuses `process.py`'s grid-loading code directly, so map
values are guaranteed consistent with what's written to the database.
```
python ecmwf_map.py            # most recent ECMWF/bd_*.grib2
python ecmwf_map.py 20261125 --days 7
```
Output: `MAPS/<date>/<var>.F_<date>.S_<start>.E_<end>.{geojson,svg}` plus one
`MAPS/<date>/info.<date>.json` listing every generated file.

### `downloadnprocess.py`
The daily production entrypoint — runs all three stages above in sequence for one local
date, then prunes anything older than the retention window from `ECMWF_RAW/`, `ECMWF/`,
and the two forecast tables (scoped to the ECMWF source only). Skips the download step
entirely if the cropped file for that date already exists on disk.
```
python downloadnprocess.py                 # today's local forecast (cron case)
python downloadnprocess.py 20261125         # backfill a specific local date
python downloadnprocess.py --keep-days 14   # default retention is 10 days
python downloadnprocess.py --dry-run        # run normally, skip all writes/deletions
```
This is the script meant to run daily at **06:00 Bangladesh time** via cron.

### `pipeline_log.py`
Shared helper used by all three stages — appends a `DATE <timestamp>` / `<STAGE>LOG
<timestamp>` pair to `pipeline.log` whenever a stage completes successfully (skipped on
`--dry-run`). Labels: `DOWNLOADLOG`, `PROCESSLOG`, `MAPPROCESSLOG`.

## Project layout

```
data/                         upazila boundary topojson (source, tracked in git)
ECMWF_RAW/                    raw global GRIB2 downloads (generated, gitignored)
ECMWF/                        cropped Bangladesh GRIB2 files (generated, gitignored)
MAPS/                         generated forecast maps (generated, gitignored)
example_database.sql          schema + reference data to bootstrap weather_api
.env.example                  template for required DB_* environment variables
pipeline.log                  stage-completion log (generated, gitignored)
```
