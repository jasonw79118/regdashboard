RegDashboard build.py patch

What changed:
- Added docs/raw/items-array.json output
- File is generated every time build.py runs
- Existing scrape and output logic remains unchanged

New output file:
- docs/raw/items-array.json

How it works:
- Keeps existing docs/data/items.json wrapper output
- Keeps existing docs/raw/items.ndjson output
- Adds a plain JSON array written from payload["items"]
