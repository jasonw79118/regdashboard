RegDashboard CSV Pack v2

What changed:
- Added a visible Download CSV button in index.html.
- Added an Open CSV link that points to data/articles.csv.
- Added client-side CSV generation so the Download CSV button works even if the static CSV file is missing or not yet published.
- Filled blank summaries with a generic auto synopsis in the CSV output.

Files to place in your repo:
- index.html
- data/articles.csv

Expected static CSV URL after push:
- https://jasonw79118.github.io/regdashboard/data/articles.csv

Important:
- The Download CSV button does not rely on the static file. It builds the CSV directly from loaded dashboard data.
- The Open CSV link does require data/articles.csv to exist in the published repo.
