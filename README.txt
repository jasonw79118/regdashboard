RegDashboard safe CSV fix

This pack restores the original static export page and TXT links.

Replace/add only these files:
- docs/index.html
- docs/data/articles.csv
- docs/raw/index.html
- docs/raw/html

What this does:
- Adds Download CSV and Open CSV to the live app page only
- Keeps the original static export page intact so:
  - items.txt
  - items.md
  - items.ndjson
  remain linked exactly as before
- Adds docs/raw/html as a small redirect to docs/raw/index.html

Do not change build.py.
