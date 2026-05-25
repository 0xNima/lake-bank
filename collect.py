import csv
import pickle

from pathlib import Path


STORAGE_DIR = Path("./storage")
INPUT_CSV = Path("./storage/db/lakes.csv")
OUTPUT_CSV = Path("./storage/db/lakes_clean.csv")


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


if __name__ == '__main__':
    build_lakes_csv()
    write_clean_csv()
