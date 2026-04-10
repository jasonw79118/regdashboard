RegDashboard docs-based CSV fix

Your GitHub Pages publish source is the docs folder.

Replace these files in your repo:
- docs/index.html
- docs/raw/index.html
- docs/data/articles.csv

Do NOT replace the repo-root index.html or raw/index.html for this fix.
Those are not what the live site is serving.

What this fixes:
- Main live page gets Download CSV and Open CSV links
- raw live page gets Download CSV and Open CSV links
- docs/data/articles.csv is created at the path GitHub Pages actually serves

Live CSV URL after publish:
https://jasonw79118.github.io/regdashboard/data/articles.csv
