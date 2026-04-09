RegDashboard CSV Pack

Included files:
- index.html
- data/articles.csv

What changed:
- Added a "Download CSV" button in the site header that points to data/articles.csv
- Created data/articles.csv with these columns:
  Category
  Title
  Date
  Summary
  Article URL
  Source

How to apply:
1. Replace your current index.html with the included index.html
2. Add the included CSV file to your repo at data/articles.csv
3. Commit and push

Notes:
- The Date column uses the published_at value from items.json
- The CSV is UTF-8 with BOM so it should open cleanly in Excel
