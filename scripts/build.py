import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import requests
from bs4 import BeautifulSoup
import feedparser
from dateutil import parser as dateparser

OUTPUT_PATH = "docs/data/items.json"
WINDOW_DAYS = 14
UA = "regdashboard/1.0 (+https://github.com/jasonw79118/regdashboard)"

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def in_window(dt: datetime, start: datetime, end: datetime) -> bool:
    return start <= dt <= end

def safe_get(url: str, timeout: int = 30) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r.text

def parse_date(text: str) -> Optional[datetime]:
    try:
        dt = dateparser.parse(text)
        if not dt:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def add_item(items: List[Dict[str, Any]], source: str, title: str, url: str,
             published_at: datetime, summary: str = ""):
    items.append({
        "source": source,
        "title": title.strip(),
        "url": url.strip(),
        "published_at": iso_z(published_at),
        "summary": summary.strip()
    })

def rss_source(source_name: str, feed_url: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    feed = feedparser.parse(feed_url)
    for e in feed.entries:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()

        dt = None
        if e.get("published"):
            dt = parse_date(e.get("published"))
        elif e.get("updated"):
            dt = parse_date(e.get("updated"))
        elif e.get("published_parsed"):
            dt = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)

        if not (title and link and dt):
            continue
        if not in_window(dt, start, end):
            continue

        summary = ""
        if e.get("summary"):
            summary = BeautifulSoup(e.get("summary"), "html.parser").get_text(" ", strip=True)

        add_item(out, source_name, title, link, dt, summary)
    return out

def fdic_press_releases(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    url = "https://www.fdic.gov/news/press-releases/"
    html = safe_get(url)
    soup = BeautifulSoup(html, "html.parser")

    out: List[Dict[str, Any]] = []
    # The page lists items with date text near links; we’ll scan for links that look like press release titles.
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 8:
            continue
        # Press release detail pages typically include "/news/press-releases/" or "/news/press-releases/"
        if "/news/press-releases/" not in href and not href.startswith("https://www.fdic.gov/news/press-releases/"):
            continue

        # try to find a date nearby (parent text often contains it)
        context = a.parent.get_text(" ", strip=True) if a.parent else ""
        # Example: "Feb 6, 2026 FDIC Extends Comment Period ..."
        m = re.search(r"([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})", context)
        if not m:
            continue
        dt = parse_date(m.group(1))
        if not dt or not in_window(dt, start, end):
            continue

        full_url = href
        if full_url.startswith("/"):
            full_url = "https://www.fdic.gov" + full_url

        add_item(out, "FDIC", text, full_url, dt, "")
    return out

def ofac_recent_actions(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """
    OFAC Recent Actions page has a clear "results list" under the main content.
    We intentionally scrape only the results rows and ignore all other links.
    """
    url = "https://ofac.treasury.gov/recent-actions"
    html = safe_get(url)
    soup = BeautifulSoup(html, "html.parser")
    out: List[Dict[str, Any]] = []

    # The actual results are usually rendered as a "views" list with repeating rows.
    # We target rows that contain BOTH:
    # - a detail link to /recent-actions/YYYYMMDD
    # - a date line like "February 10, 2026 - Sanctions List Updates"
    rows = soup.select(".views-row") or soup.select("article") or []

    for row in rows:
        a = row.find("a", href=True)
        if not a:
            continue

        href = a["href"]
        full_url = href
        if full_url.startswith("/"):
            full_url = "https://ofac.treasury.gov" + full_url

        # Keep only real recent-actions detail pages
        if "/recent-actions/" not in full_url:
            continue

        # Title should be the link text
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 6:
            # Sometimes the title is in attributes; use as fallback
            title = (a.get("title") or a.get("aria-label") or "").strip()

        if not title or len(title) < 6:
            continue

        # Find the date line inside the row
        row_text = row.get_text(" ", strip=True)
        m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})\s*-\s*", row_text)
        if not m:
            continue

        dt = parse_date(m.group(1))
        if not dt:
            continue

        if not in_window(dt, start, end):
            continue

        add_item(out, "OFAC", title, full_url, dt, "")

    return out


    # OFAC page includes result cards; grab any link + date-looking text around it.
    for a in soup.select("a[href]"):
        title = a.get_text(" ", strip=True)
        href = a.get("href", "")
        if not title or len(title) < 8:
            continue
        if not href.startswith("http"):
            if href.startswith("/"):
                href = "https://ofac.treasury.gov" + href
            else:
                continue

        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        # Look for "February 10, 2026" pattern
        m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", parent_text)
        if not m:
            continue
        dt = parse_date(m.group(1))
        if not dt or not in_window(dt, start, end):
            continue

        # Avoid nav/footer links by requiring OFAC recent-actions detail links often include /recent-actions/
        if "/recent-actions/" not in href:
            continue

        add_item(out, "OFAC", title, href, dt, "")
    return out

def federal_register_topics(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    topics = [
        ("Federal Register", "https://www.federalregister.gov/topics/banks-banking"),
        ("Federal Register", "https://www.federalregister.gov/topics/executive-orders"),
        ("Federal Register", "https://www.federalregister.gov/topics/federal-reserve-system"),
        ("Federal Register", "https://www.federalregister.gov/topics/national-banks"),
        ("Federal Register", "https://www.federalregister.gov/topics/securities"),
        ("Federal Register", "https://www.federalregister.gov/topics/mortgages"),
        ("Federal Register", "https://www.federalregister.gov/topics/truth-lending"),
        ("Federal Register", "https://www.federalregister.gov/topics/truth-savings"),
    ]
    out: List[Dict[str, Any]] = []

    for source_name, url in topics:
        html = safe_get(url)
        soup = BeautifulSoup(html, "html.parser")

        # Entries appear as document listings with links; we’ll grab listing titles and “Publication Date” nearby.
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            title = a.get_text(" ", strip=True)
            if not title or len(title) < 10:
                continue
            if not href.startswith("http"):
                if href.startswith("/"):
                    href = "https://www.federalregister.gov" + href
                else:
                    continue
            # Heuristic: document pages often contain "/documents/"
            if "/documents/" not in href:
                continue

            # Look for date in the nearest container
            container = a.find_parent(["li", "article", "div"])
            if not container:
                continue
            ctxt = container.get_text(" ", strip=True)
            # Federal Register listings often include "Publication Date" or a month/day/year
            m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", ctxt)
            if not m:
                continue
            dt = parse_date(m.group(1))
            if not dt or not in_window(dt, start, end):
                continue

            add_item(out, source_name, title, href, dt, "")
    return out

def main():
    end = utc_now()
    start = end - timedelta(days=WINDOW_DAYS)

    items: List[Dict[str, Any]] = []

    # RSS (easy wins)
    items += rss_source("FRB", "https://www.federalreserve.gov/feed.xml", start, end)

    # HTML scrapes (no RSS / retired RSS / easiest listings)
    items += fdic_press_releases(start, end)
    items += ofac_recent_actions(start, end)
    items += federal_register_topics(start, end)

    # De-dupe by URL
    seen = set()
    deduped = []
    for it in sorted(items, key=lambda x: x["published_at"], reverse=True):
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        deduped.append(it)

    data = {
        "window_start": iso_z(start),
        "window_end": iso_z(end),
        "items": deduped
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"[ok] wrote {OUTPUT_PATH} with {len(deduped)} items (window {data['window_start']} → {data['window_end']})")

if __name__ == "__main__":
    main()
