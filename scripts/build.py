from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urldefrag

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser


# ============================
# CONFIG (performance + window)
# ============================

OUT_PATH = "docs/data/items.json"
WINDOW_DAYS = 14

# Performance controls (keeps runs from “hanging”)
MAX_LISTING_LINKS = 80        # max links collected per start page
MAX_DETAIL_FETCHES = 25       # max detail pages fetched per entire run
REQUEST_DELAY_SEC = 0.10      # small politeness delay

UA = "regdashboard/1.2 (+https://github.com/jasonw79118/regdashboard)"


@dataclass
class SourcePage:
    source: str
    url: str


START_PAGES: List[SourcePage] = [
    SourcePage("OFAC", "https://ofac.treasury.gov/recent-actions"),

    SourcePage("IRS", "https://www.irs.gov/newsroom"),
    SourcePage("IRS", "https://www.irs.gov/newsroom/news-releases-for-current-month"),
    SourcePage("IRS", "https://www.irs.gov/newsroom/irs-newswire-rss-feed"),

    SourcePage("NACHA", "https://www.nacha.org/news"),
    SourcePage("NACHA", "https://www.nacha.org/rules"),

    SourcePage("OCC", "https://www.occ.gov/news-issuances/news-releases/index-news-releases.html"),

    SourcePage("FDIC", "https://www.fdic.gov/news/press-releases/"),

    SourcePage("FRB", "https://www.federalreserve.gov/news-events/press-releases.htm"),
    SourcePage("FRB", "https://www.federalreserve.gov/feed.xml"),

    SourcePage("FHLB MPF", "https://www.fhlbmpf.com/about-us/news"),

    SourcePage("Fannie Mae", "https://www.fanniemae.com/newsroom"),

    SourcePage("Freddie Mac", "https://www.freddiemac.com/media-room"),

    SourcePage("USDA APHIS", "https://www.aphis.usda.gov/news"),

    SourcePage("Senate Banking", "https://www.banking.senate.gov/newsroom"),

    SourcePage("White House", "https://www.whitehouse.gov/presidential-actions/"),

    SourcePage("Federal Register", "https://www.federalregister.gov/topics/banks-banking"),
    SourcePage("Federal Register", "https://www.federalregister.gov/topics/executive-orders"),
    SourcePage("Federal Register", "https://www.federalregister.gov/topics/federal-reserve-system"),
    SourcePage("Federal Register", "https://www.federalregister.gov/topics/national-banks"),
    SourcePage("Federal Register", "https://www.federalregister.gov/topics/securities"),
    SourcePage("Federal Register", "https://www.federalregister.gov/topics/mortgages"),
    SourcePage("Federal Register", "https://www.federalregister.gov/topics/truth-lending"),
    SourcePage("Federal Register", "https://www.federalregister.gov/topics/truth-savings"),
]


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})


# ============================
# HELPERS
# ============================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def iso_z(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")

def clean_text(s: str, max_len: int = 320) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s

def parse_date(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = dtparser.parse(str(s), fuzzy=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def in_window(dt: datetime, start: datetime, end: datetime) -> bool:
    return start <= dt <= end

def canonical_url(url: str) -> str:
    url, _frag = urldefrag(url)
    return url.strip()

def polite_get(url: str, timeout: int = 25) -> Optional[str]:
    """
    HARD TIMEOUTS so we never “hang forever”.
    timeout=(connect_timeout, read_timeout)
    """
    try:
        time.sleep(REQUEST_DELAY_SEC)
        r = SESSION.get(url, timeout=(10, timeout), allow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[warn] GET failed: {url} :: {e}", flush=True)
        return None


# ============================
# FEED DISCOVERY + PARSING
# ============================

def discover_feeds(page_url: str, html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    feeds: List[str] = []

    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel", [])).lower()
        typ = (link.get("type") or "").lower()
        href = link.get("href")
        if not href:
            continue
        if "alternate" in rel and ("rss" in typ or "atom" in typ or href.endswith(".xml")):
            feeds.append(urljoin(page_url, href))

    if page_url.endswith(".xml") or "feed" in page_url.lower():
        feeds.append(page_url)

    out = []
    seen = set()
    for f in feeds:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def items_from_feed(source: str, feed_url: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    fp = feedparser.parse(feed_url)

    for e in fp.entries:
        title = clean_text(e.get("title", ""), 220)
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue

        dt = None
        if e.get("published"):
            dt = parse_date(e.get("published"))
        elif e.get("updated"):
            dt = parse_date(e.get("updated"))
        elif e.get("published_parsed"):
            try:
                dt = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            except Exception:
                dt = None

        if not dt or not in_window(dt, start, end):
            continue

        summary = ""
        if e.get("summary"):
            summary = clean_text(BeautifulSoup(e["summary"], "html.parser").get_text(" ", strip=True), 380)

        out.append({
            "source": source,
            "title": title,
            "published_at": iso_z(dt),
            "url": canonical_url(link),
            "summary": summary,
        })

    return out


# ============================
# DETAIL PAGE EXTRACTION
# ============================

def extract_published_from_detail(detail_url: str, html: str) -> Tuple[Optional[datetime], str]:
    soup = BeautifulSoup(html, "html.parser")

    # Prefer meta description as snippet
    snippet = ""
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        snippet = clean_text(meta_desc.get("content"), 380)

    # <time datetime>
    t = soup.find("time")
    if t:
        dt = parse_date(t.get("datetime") or t.get_text(" ", strip=True))
        if dt:
            return dt, snippet

    # common meta publish tags
    meta_keys = [
        ("property", "article:published_time"),
        ("name", "article:published_time"),
        ("name", "pubdate"),
        ("name", "publish-date"),
        ("name", "date"),
        ("property", "og:updated_time"),
    ]
    for k, v in meta_keys:
        m = soup.find("meta", attrs={k: v})
        if m and m.get("content"):
            dt = parse_date(m.get("content"))
            if dt:
                return dt, snippet

    # JSON-LD datePublished/dateModified
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.get_text(strip=True) or "{}")
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            for k in ["datePublished", "dateModified"]:
                if k in obj:
                    dt = parse_date(obj.get(k))
                    if dt:
                        return dt, snippet

    # fallback: scan visible text for "February 10, 2026"
    text = soup.get_text(" ", strip=True)
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    if m:
        dt = parse_date(m.group(1))
        if dt:
            return dt, snippet

    return None, snippet


# ============================
# LISTING EXTRACTION  ✅ THIS IS IT
# ============================

def main_content_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    """
    LISTING EXTRACTION:
    - Collect a bunch of links from the page's main content.
    - Try to grab a date from nearby text if present.
    Returns: (title, absolute_url, date_or_none)
    """
    soup = BeautifulSoup(html, "html.parser")

    container = soup.find("main") or soup.find("article") or soup.find("body")
    if not container:
        return []

    links: List[Tuple[str, str, Optional[datetime]]] = []
    for a in container.find_all("a", href=True):
        href = a["href"].strip()
        title = clean_text(a.get_text(" ", strip=True), 220)
        if not href or not title:
            continue
        if href.startswith("javascript:") or href.startswith("#"):
            continue

        url = canonical_url(urljoin(page_url, href))

        parent = a.find_parent(["li", "article", "div", "p"]) or a.parent
        near = clean_text(parent.get_text(" ", strip=True) if parent else "", 500)

        dt = None
        m = re.search(r"([A-Z][a-z]{2,9}\s+\d{1,2},\s+\d{4})", near)
        if m:
            dt = parse_date(m.group(1))
        if not dt:
            m2 = re.search(r"(\d{4}-\d{2}-\d{2})", near)
            if m2:
                dt = parse_date(m2.group(1))

        links.append((title, url, dt))

        if len(links) >= MAX_LISTING_LINKS:
            break

    return links


# ============================
# BUILD
# ============================

def build():
    now = utc_now()
    window_start = now - timedelta(days=WINDOW_DAYS)
    window_end = now

    all_items: List[Dict[str, Any]] = []
    detail_fetches = 0

    for sp in START_PAGES:
        print(f"\n[source] {sp.source} :: {sp.url}", flush=True)

        html = polite_get(sp.url)
        if not html:
            print("[skip] no html", flush=True)
            continue

        # ---- FEEDS ----
        feed_urls = discover_feeds(sp.url, html)
        feed_items_total = 0

        for fu in feed_urls:
            try:
                got = items_from_feed(sp.source, fu, window_start, window_end)
                feed_items_total += len(got)
                all_items.extend(got)
                if got:
                    print(f"[feed] {len(got)} items from {fu}", flush=True)
            except Exception as e:
                print(f"[warn] feed parse failed: {fu} :: {e}", flush=True)

        print(f"[feed] total: {feed_items_total} | feeds found: {len(feed_urls)}", flush=True)

        # ---- LISTING EXTRACTION ----
        listing_links = main_content_links(sp.url, html)
        print(f"[list] links captured: {len(listing_links)} | detail fetches used: {detail_fetches}/{MAX_DETAIL_FETCHES}", flush=True)

        for title, url, dt in listing_links:
            snippet = ""

            if dt is None:
                if detail_fetches >= MAX_DETAIL_FETCHES:
                    continue
                detail_html = polite_get(url)
                if not detail_html:
                    continue
                detail_fetches += 1
                dt2, snippet2 = extract_published_from_detail(url, detail_html)
                dt = dt2
                snippet = snippet2

            if not dt:
                continue
            if not in_window(dt, window_start, window_end):
                continue

            all_items.append({
                "source": sp.source,
                "title": title,
                "published_at": iso_z(dt),
                "url": url,
                "summary": snippet,
            })

    # de-dupe by URL (keep newest; prefer summary)
    dedup: Dict[str, Dict[str, Any]] = {}
    for it in sorted(all_items, key=lambda x: x["published_at"], reverse=True):
        key = canonical_url(it["url"])
        if key not in dedup:
            dedup[key] = it
        else:
            if (not dedup[key].get("summary")) and it.get("summary"):
                dedup[key] = it

    items = list(dedup.values())
    items.sort(key=lambda x: x["published_at"], reverse=True)

    payload = {
        "window_start": iso_z(window_start),
        "window_end": iso_z(window_end),
        "items": items,
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n[ok] wrote {OUT_PATH} with {len(items)} items | detail fetches: {detail_fetches}", flush=True)


if __name__ == "__main__":
    build()
