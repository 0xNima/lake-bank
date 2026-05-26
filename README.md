# lake-bank

## What it does

`lake-bank` queries NASA Earthdata's CMR API for SWOT `SWOT_L2_HR_LakeSP_D` granules over a date range, downloads each granule's zipped shapefile, and extracts only the `lake_id` / `lake_name` attributes — **all on the fly, in memory**. The raw granule archives are never written to disk; only the much smaller extracted attribute mappings are persisted.

This is useful when you don't have the disk space to hoard the raw SWOT shapefiles (a single day's worth of granules can be tens of GB), but still want a complete index of lakes across long time windows.

## Requirements

- Python 3.9+ (capped by `geopandas`)
- A NASA Earthdata account: https://urs.earthdata.nasa.gov/
- A `~/.netrc` entry for the Earthdata host so the downloader can authenticate non-interactively:

  ```
  machine urs.earthdata.nasa.gov
      login YOUR_USERNAME
      password YOUR_PASSWORD
  ```

  Make sure the file is readable only by you: `chmod 600 ~/.netrc`.

- Dependencies from `requirements.txt`:

  ```
  pip install -r requirements.txt
  ```

## Usage

The pipeline has two steps. First, **fetch** the per-day pickle data:

```
python fetch.py
```

Edit the date window at the bottom of `fetch.py` (`overall_start` / `overall_end`) before running. The script walks the range one day at a time, fetches CMR granule URLs, downloads each granule, extracts lake attributes, and writes per-day pickle files under `storage/`:

- `storage/fl_<id>` — cached list of granule URLs for that day (so re-runs skip the CMR search)
- `storage/dt_<id>` — extracted lake attribute results
- `storage/failed_<id>` — URLs that failed, with their error, so you can retry them later
- `storage/edl_cookies.txt` — persisted Earthdata Login session cookie (avoids re-authing each run)

Already-processed days are skipped automatically. Interrupting with Ctrl+C will save whatever has completed so far before exiting.

Second, **collect** the per-day pickles into a consolidated index:

```
python collect.py
```

This merges every `dt_*` file into:

- `storage/db/lakes.csv` — every `(lake_id, lake_name)` observation
- `storage/db/lakes_clean.csv` — same, minus rows where the name is SWOT's `"no_data"` placeholder
- `storage/db/lakes.sqlite` — indexed SQLite database for fast case-insensitive prefix lookups
- `storage/db/lakes_d1.sql` — SQL dump for loading the same data into an edge SQLite store

The CSV merge is incremental — existing rows in `lakes.csv` are preserved across runs, so prior results stay even if old `dt_*` pickles are deleted.

## Picking a date window

SWOT has a **21-day repeat cycle**, so any given lake is revisited roughly every 21 days. A 21-day window will cover the planet once, but to be safe against missed revisits or partial coverage at the cycle boundary, a **42-day window** is recommended — that guarantees at least one full repeat plus margin, and is enough to collect virtually all lakes SWOT observes.

There's no need to pick larger windows than that for index-building purposes; you'll just download a lot of redundant data.

## Querying the data

Three ways to look up a lake by name without setting anything up locally:

1. **CSV files** — `storage/db/lakes.csv` and `storage/db/lakes_clean.csv` are tracked in the repo and usable directly from pandas, Excel, or any CSV tool.
2. **Browser UI** — https://0xnima.github.io/lake-bank/web/. Case-insensitive prefix search against the SQLite database, fetched over HTTP Range requests so the client only downloads the few KB of pages each query actually touches. No server, no install.
3. **JSON API** — https://lakes.swot-lake.workers.dev/?q=lake+michigan. Returns `{ query, count, results: [{ lake_id, lake_name }, ...] }`. Supports `?q=…` (required) and `?limit=…` (default 50, max 1000). CORS open, safe to call from a browser.

## Project layout

- `fetch.py` — download / extract pipeline. Authentication, OAuth-challenge auth class, per-day windowing, concurrent downloads, result persistence.
- `collect.py` — pickle → CSV → SQLite aggregator.
- `web/` — static browser UI (single `index.html` + `sql.js-httpvfs` worker / wasm assets).
- `api/` — Cloudflare Worker source backing the JSON API.
- `storage/` — generated data (pickles, CSVs, SQLite). Mostly gitignored except `storage/db/*.sqlite`.
