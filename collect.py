import csv
import pickle
import sqlite3

from pathlib import Path


STORAGE_DIR = Path("./storage")
INPUT_CSV = Path("./storage/db/lakes.csv")
OUTPUT_CSV = Path("./storage/db/lakes_clean.csv")
SQLITE_DB = Path("./storage/db/lakes.sqlite")
D1_SQL = Path("./storage/db/lakes_d1.sql")


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
        """)
        conn.executemany("INSERT INTO lakes VALUES (?, ?)", rows)
        conn.commit()
        # VACUUM compacts pages and applies the page_size pragma to the whole file
        conn.execute("VACUUM")
    finally:
        conn.close()

    size_mb = SQLITE_DB.stat().st_size / 1024 / 1024
    print(f"Built {SQLITE_DB} with {len(rows)} rows ({size_mb:.1f} MB)")


def export_d1_sql():
    conn = sqlite3.connect(SQLITE_DB)
    try:
        rows = conn.execute("SELECT lake_id, lake_name FROM lakes").fetchall()
    finally:
        conn.close()

    # No BEGIN/COMMIT — D1 manages transactions itself and rejects explicit
    # transaction control. `wrangler d1 import` batches the inserts efficiently.
    with open(D1_SQL, 'w') as f:
        f.write("DROP TABLE IF EXISTS lakes;\n")
        f.write("CREATE TABLE lakes (lake_id TEXT NOT NULL, lake_name TEXT NOT NULL);\n")
        f.write("CREATE INDEX idx_lakes_name ON lakes(lake_name COLLATE NOCASE);\n")
        for lake_id, lake_name in rows:
            lid = lake_id.replace("'", "''")
            nm = lake_name.replace("'", "''")
            f.write(f"INSERT INTO lakes VALUES ('{lid}', '{nm}');\n")

    size_mb = D1_SQL.stat().st_size / 1024 / 1024
    print(f"Wrote {D1_SQL} ({len(rows)} rows, {size_mb:.1f} MB)")


if __name__ == '__main__':
    build_lakes_csv()
    write_clean_csv()
    build_sqlite()
    export_d1_sql()
