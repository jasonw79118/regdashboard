RegDashboard dual CSV fix

Replace these files in your repo:
- index.html
- raw/index.html
- data/articles.csv

Why this pack:
- The live main page is still serving an index without CSV controls.
- The live raw page is still serving an older static export page.
- This pack updates BOTH entry points so the CSV is available either way.

What you should see after publish:
- Main page header: Download CSV, Open CSV, Static export (Copilot)
- Raw page header: Download CSV, Open CSV, Static export (Copilot)

CSV behavior:
- Keeps real summaries when present.
- Fills blanks with: Auto synopsis: {Source} item in {Category} — {Title}.
