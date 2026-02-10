import json
from datetime import datetime, timedelta, timezone

now = datetime.now(timezone.utc)
window_start = now - timedelta(days=14)
window_end = now

data = {
    "window_start": window_start.isoformat().replace("+00:00", "Z"),
    "window_end": window_end.isoformat().replace("+00:00", "Z"),
    "items": [
        {
            "source": "TEST",
            "title": "RegDashboard build script is working",
            "published_at": window_end.isoformat().replace("+00:00", "Z"),
            "url": "https://github.com/jasonw79118/regdashboard",
            "summary": "This confirms the local build process works."
        }
    ]
}

with open("docs/data/items.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)

print("RegDashboard build complete.")
