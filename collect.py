import csv
import pickle
import sqlite3

from pathlib import Path


STORAGE_DIR = Path("./storage")
INPUT_CSV = Path("./storage/db/lakes.csv")
OUTPUT_CSV = Path("./storage/db/lakes_clean.csv")
SQLITE_DB = Path("./storage/db/lakes.sqlite")


def build_lakes_csv():
    db = {}

    # Preserve whatever's already in lakes.csv so we don't lose past work
    if INPUT_CSV.exists():
        with open(INPUT_CSV, newline='') as f:
            for row in csv.DictReader(f):
                name = row.get('lake_name') or ''
                if name:
                    db.setdefault(row['lake_id'], set()).add(name)
        existing = sum(len(v) for v in db.values())
        print(f"Loaded {existing} existing rows from {INPUT_CSV}")

    # Merge in everything from the dt_* pickles
    for file in STORAGE_DIR.glob("dt_*"):
        with open(file, 'rb') as f:
            for result in pickle.load(f):
                for lake_id, names in result.items():
                    db.setdefault(lake_id, set()).update(n for n in names if n)

    INPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(INPUT_CSV, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["lake_id", "lake_name"])
        for lake_id in sorted(db):
            for name in sorted(db[lake_id]):
                writer.writerow([lake_id, name])
                written += 1
    print(f"Wrote {INPUT_CSV} ({written} rows total)")


def write_clean_csv():
    written = 0
    with open(INPUT_CSV, newline='') as fin, \
         open(OUTPUT_CSV, 'w', newline='') as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            if row['lake_name'] == 'no_data':
                continue
            writer.writerow(row)
            written += 1
    print(f"Wrote {written} rows to {OUTPUT_CSV} (no_data excluded)")


def build_sqlite():
    rows = []
    with open(OUTPUT_CSV, newline='') as f:
        for row in csv.DictReader(f):
            rows.append((row['lake_id'], row['lake_name']))

    if SQLITE_DB.exists():
        SQLITE_DB.unlink()

    SQLITE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_DB)
    try:
        # page_size must be set before any table is created; 64K pages = fewer Range
        # requests over HTTP. journal OFF for fast bulk insert (safe — fresh build).
        conn.executescript("""
            PRAGMA page_size = 65536;
            PRAGMA journal_mode = OFF;
            CREATE TABLE lakes (
                lake_id   TEXT NOT NULL,
                lake_name TEXT NOT NULL
            );
            CREATE INDEX idx_lakes_name ON lakes(lake_name COLLATE NOCASE);
            CREATE VIRTUAL TABLE lakes_fts USING fts5(
                lake_name,
                content='lakes',
                content_rowid='rowid',
                tokenize='trigram'
            );
        """)
        conn.executemany("INSERT INTO lakes VALUES (?, ?)", rows)
        conn.execute("INSERT INTO lakes_fts(lakes_fts) VALUES('rebuild')")
        conn.commit()
        # VACUUM compacts pages and applies the page_size pragma to the whole file
        conn.execute("VACUUM")
    finally:
        conn.close()

    size_mb = SQLITE_DB.stat().st_size / 1024 / 1024
    print(f"Built {SQLITE_DB} with {len(rows)} rows ({size_mb:.1f} MB)")


if __name__ == '__main__':
    build_lakes_csv()
    write_clean_csv()
    build_sqlite()
