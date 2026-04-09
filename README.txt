RegDashboard raw/index.html fix

Replace:
- raw/index.html

What changed:
- Fixed asset paths for raw page:
  - assets/style.css -> ../assets/style.css
  - assets/logo/... -> ../assets/logo/...
- Fixed CSV link:
  - data/articles.csv -> ../data/articles.csv
- Fixed data fetch path:
  - ../data/items.json
- Kept Download CSV button and fallback auto synopsis logic
