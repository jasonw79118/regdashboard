from __future__ import annotations

import json
import os
import re
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urldefrag, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from bs4 import XMLParsedAsHTMLWarning
from dateutil import parser as dtparser


# ============================
# CONFIG
# ============================

OUT_PATH = "docs/data/items.json"

# --- Copilot-friendly static exports (no JS required) ---
RAW_DIR = "docs/raw"
RAW_HTML_PATH = f"{RAW_DIR}/index.html"
RAW_MD_PATH = f"{RAW_DIR}/items.md"
RAW_TXT_PATH = f"{RAW_DIR}/items.txt"
RAW_NDJSON_PATH = f"{RAW_DIR}/items.ndjson"
RAW_ROBOTS_PATH = f"{RAW_DIR}/robots.txt"
RAW_SITEMAP_PATH = f"{RAW_DIR}/sitemap.xml"

# Public base (used for <base> tag + sitemap). Keep trailing /regdashboard
PUBLIC_BASE = "https://jasonw79118.github.io/regdashboard"

WINDOW_DAYS = 14

MAX_LISTING_LINKS = 180
GLOBAL_DETAIL_FETCH_CAP = 140
REQUEST_DELAY_SEC = 0.10  # slightly slower = fewer timeouts / blocks

PER_SOURCE_DETAIL_CAP: Dict[str, int] = {
    "IRS": 70,
    "USDA APHIS": 45,
    "Mastercard": 40,
    "Visa": 35,
    "Fannie Mae": 35,
    "Freddie Mac": 20,
    "FIS": 25,
    "Fiserv": 25,
    "Jack Henry": 25,
    "Temenos": 25,
    "Mambu": 20,
    "Finastra": 20,
    "TCS": 25,
    "OFAC": 20,
    "OCC": 20,
    "FDIC": 20,
    "FRB": 25,
}
DEFAULT_SOURCE_DETAIL_CAP = 15

UA = "regdashboard/1.8 (+https://github.com/jasonw79118/regdashboard)"


@dataclass
class SourcePage:
    source: str
    url: str


START_PAGES: List[SourcePage] = [
    SourcePage("OFAC", "https://ofac.treasury.gov/recent-actions"),

    SourcePage("IRS", "https://www.irs.gov/newsroom"),
    SourcePage("IRS", "https://www.irs.gov/newsroom/news-releases-for-current-month"),
    SourcePage("IRS", "https://www.irs.gov/newsroom/irs-tax-tips"),
    # IMPORTANT: this is an HTML directory page (NOT a feed). We will parse it for real feeds.
    SourcePage("IRS", "https://www.irs.gov/downloads/rss"),

    SourcePage("NACHA", "https://www.nacha.org/news"),
    SourcePage("NACHA", "https://www.nacha.org/rules"),

    SourcePage("OCC", "https://www.occ.gov/news-issuances/news-releases/index-news-releases.html"),
    SourcePage("FDIC", "https://www.fdic.gov/news/press-releases/"),

    SourcePage("FRB", "https://www.federalreserve.gov/newsevents/pressreleases.htm"),
    SourcePage("FRB", "https://www.federalreserve.gov/feeds/press_all.xml"),
    SourcePage("FRB", "https://www.federalreserve.gov/feeds/press_bcreg.xml"),

    SourcePage("FHLB MPF", "https://www.fhlbmpf.com/about-us/news"),

    SourcePage("Fannie Mae", "https://www.fanniemae.com/rss/rss.xml"),
    SourcePage("Fannie Mae", "https://www.fanniemae.com/newsroom/fannie-mae-news"),

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

    SourcePage("CISA KEV", "https://github.com/cryptogennepal/cve-kev-rss/"),
    SourcePage("BleepingComputer", "https://www.bleepingcomputer.com/feed/"),
    SourcePage("Microsoft MSRC", "https://api.msrc.microsoft.com/update-guide/rss"),

    SourcePage("FIS", "https://www.investor.fisglobal.com/press-releases"),
    SourcePage("Fiserv", "https://investors.fiserv.com/news-events/news-releases"),
    SourcePage("Jack Henry", "https://ir.jackhenry.com/press-releases"),
    SourcePage("Temenos", "https://www.temenos.com/press-releases/"),
    SourcePage("Mambu", "https://mambu.com/en/insights/press"),
    SourcePage("Finastra", "https://www.finastra.com/news-events/media-room"),
    SourcePage("TCS", "https://www.tcs.com/who-we-are/newsroom"),

    # ------------------------------------------------------------------
    # PAYMENT NETWORKS (FIX)
    # ------------------------------------------------------------------
    SourcePage("Visa", "https://usa.visa.com/about-visa/newsroom/press-releases-listing.html"),
    SourcePage("Mastercard", "https://www.mastercard.com/us/en/news-and-trends/press.html"),
]


SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
})


# ============================
# RULES: keep scrapes focused
# ============================

SOURCE_RULES: Dict[str, Dict[str, Any]] = {
    "IRS": {
        "allow_domains": {"www.irs.gov"},
        "allow_path_prefixes": {"/newsroom/"},
        "deny_domains": {"sa.www4.irs.gov"},
    },
    "FRB": {
        "deny_domains": {"www.facebook.com"},
    },
}

GLOBAL_DENY_DOMAINS = {
    "www.facebook.com",
}
GLOBAL_DENY_SCHEMES = {"mailto", "tel", "javascript"}


# ============================
# HELPERS
# ============================

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


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


def is_http_url(url: str) -> bool:
    try:
        u = urlparse(url)
        return u.scheme.lower() in ("http", "https")
    except Exception:
        return False


def scheme(url: str) -> str:
    try:
        return urlparse(url).scheme.lower()
    except Exception:
        return ""


def host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def path(url: str) -> str:
    try:
        return urlparse(url).path or "/"
    except Exception:
        return "/"


def allowed_for_source(source: str, url: str) -> bool:
    if not is_http_url(url):
        return False
    if scheme(url) in GLOBAL_DENY_SCHEMES:
        return False

    h = host(url)
    if h in GLOBAL_DENY_DOMAINS:
        return False

    rules = SOURCE_RULES.get(source, {})
    deny = set(rules.get("deny_domains", set()))
    if h in deny:
        return False

    allow_domains = rules.get("allow_domains")
    if allow_domains and h not in set(allow_domains):
        return False

    allow_paths = rules.get("allow_path_prefixes")
    if allow_paths:
        p = path(url)
        ok = any(p.startswith(pref) for pref in set(allow_paths))
        if not ok:
            return False

    return True


# STRICT-ish feed detection (kept)
FEED_SUFFIX_RE = re.compile(r"(\.rss|\.xml|\.atom)$", re.I)


def looks_like_feed_url(url: str) -> bool:
    u = url.strip()
    if not is_http_url(u):
        return False
    p = path(u).lower()
    if FEED_SUFFIX_RE.search(p):
        return True
    if p.endswith("/feed") or p.endswith("/feed/"):
        return True
    q = (urlparse(u).query or "").lower()
    if "output=atom" in q:
        return True
    return False


def polite_get(url: str, timeout: int = 25) -> Optional[str]:
    if not is_http_url(url):
        return None

    h = host(url)
    read_timeout = timeout
    if "fanniemae.com" in h:
        read_timeout = 40
    if "federalreserve.gov" in h:
        read_timeout = 35
    if "irs.gov" in h:
        read_timeout = 35

    try:
        time.sleep(REQUEST_DELAY_SEC)
        r = SESSION.get(url, timeout=(10, read_timeout), allow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[warn] GET failed: {url} :: {e}", flush=True)
        return None


def fetch_bytes(url: str, timeout: int = 25) -> Optional[bytes]:
    if not is_http_url(url):
        return None
    try:
        time.sleep(REQUEST_DELAY_SEC)
        r = SESSION.get(url, timeout=(10, timeout), allow_redirects=True)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"[warn] GET failed: {url} :: {e}", flush=True)
        return None


# ============================
# STATIC EXPORT HELPERS (NEW)
# ============================

def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def render_raw_html(payload: Dict[str, Any]) -> str:
    """
    A fully static HTML page containing the full item list in the initial HTML.
    No JavaScript required. Includes a <base> tag to help crawlers resolve links.
    """
    ws = str(payload.get("window_start", ""))
    we = str(payload.get("window_end", ""))
    items = payload.get("items", []) or []

    cards: List[str] = []
    for it in items:
        src = escape(str(it.get("source", "")))
        title = escape(str(it.get("title", "")))
        url = escape(str(it.get("url", "")))
        pub = escape(str(it.get("published_at", "")))
        summary = escape(str(it.get("summary", "") or ""))

        cards.append(
            "\n".join([
                '<article class="card">',
                '  <div class="meta">',
                f'    <span class="src">[{src}]</span>',
                f'    <span class="pub">{pub}</span>',
                "  </div>",
                f'  <h2 class="title"><a href="{url}">{title}</a></h2>',
                (f"  <p class='sum'>{summary}</p>" if summary else ""),
                f'  <p class="url">{url}</p>',
                "</article>",
            ])
        )

    body = "\n".join(cards) if cards else "<p>No items in window.</p>"

    base_href = f"{PUBLIC_BASE.rstrip('/')}/raw/"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>RegDashboard – Static Export</title>
  <meta name="description" content="Static export of RegDashboard items (no JavaScript required)." />
  <base href="{escape(base_href)}">
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; line-height: 1.35; }}
    header {{ margin-bottom: 18px; }}
    .small {{ color: #444; font-size: 13px; }}
    .links a {{ margin-right: 12px; }}
    .card {{ border: 1px solid #ddd; border-radius: 10px; padding: 14px; margin: 12px 0; }}
    .meta {{ display: flex; gap: 12px; font-size: 12px; color: #555; margin-bottom: 6px; }}
    .title {{ margin: 0 0 6px 0; font-size: 16px; }}
    .sum {{ margin: 0 0 6px 0; color: #222; }}
    .url {{ margin: 0; font-size: 12px; color: #666; word-break: break-word; }}
    a {{ text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 6px; }}
  </style>
</head>
<body>
  <header>
    <h1>RegDashboard — Static Export</h1>
    <div class="small">Window: <code>{escape(ws)}</code> → <code>{escape(we)}</code> (UTC)</div>
    <div class="small">Generated at build time (Copilot/indexer friendly). No JavaScript required.</div>
    <div class="small links">
      <a href="{escape(base_href)}index.html">index.html</a>
      <a href="{escape(base_href)}items.md">items.md</a>
      <a href="{escape(base_href)}items.txt">items.txt</a>
      <a href="{escape(base_href)}items.ndjson">items.ndjson</a>
      <a href="{escape(PUBLIC_BASE.rstrip('/') + '/')}">Back to app</a>
    </div>
  </header>

  {body}
</body>
</html>
"""


def render_raw_md(payload: Dict[str, Any]) -> str:
    ws = payload.get("window_start", "")
    we = payload.get("window_end", "")
    items = payload.get("items", []) or []

    lines: List[str] = []
    lines.append("# RegDashboard — Export")
    lines.append("")
    lines.append(f"Window: `{ws}` → `{we}` (UTC)")
    lines.append("")

    for it in items:
        title = (it.get("title") or "").strip()
        source = (it.get("source") or "").strip()
        pub = (it.get("published_at") or "").strip()
        url = (it.get("url") or "").strip()
        summary = (it.get("summary") or "").strip()

        lines.append(f"## {title}")
        lines.append(f"- Source: {source}")
        lines.append(f"- Published: {pub}")
        lines.append(f"- URL: {url}")
        if summary:
            lines.append("")
            lines.append(summary)
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_raw_txt(payload: Dict[str, Any]) -> str:
    items = payload.get("items", []) or []
    out: List[str] = []

    for it in items:
        out.append(str(it.get("source", "")).strip())
        out.append(str(it.get("published_at", "")).strip())
        out.append(str(it.get("title", "")).strip())
        out.append(str(it.get("url", "")).strip())
        summary = str(it.get("summary", "") or "").strip()
        if summary:
            out.append(summary)
        out.append("-" * 60)

    return "\n".join(out).strip() + "\n"


def write_raw_aux_files() -> None:
    """
    Extra crawler hints: robots.txt + sitemap.xml.
    (Not strictly required for Copilot, but helps some indexers.)
    """
    base = PUBLIC_BASE.rstrip("/")
    raw_base = f"{base}/raw"

    with open(RAW_ROBOTS_PATH, "w", encoding="utf-8") as f:
        f.write("User-agent: *\nAllow: /\n")

    with open(RAW_SITEMAP_PATH, "w", encoding="utf-8") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{raw_base}/index.html</loc></url>
  <url><loc>{raw_base}/items.md</loc></url>
  <url><loc>{raw_base}/items.txt</loc></url>
  <url><loc>{raw_base}/items.ndjson</loc></url>
</urlset>
""")


# ============================
# DATE PATTERNS
# ============================

MONTH_DATE_RE = re.compile(r"(?P<md>([A-Z][a-z]{{2,9}})\.?\s+\d{{1,2}},\s+\d{{4}})")
SLASH_DATE_RE = re.compile(r"(?P<sd>\b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\b)")
ISO_DATE_RE = re.compile(r"(?P<id>\b\d{{4}}-\d{{2}}-\d{{2}}\b)")


def extract_any_date(text: str) -> Optional[datetime]:
    if not text:
        return None
    m = MONTH_DATE_RE.search(text)
    if m:
        dt = parse_date(m.group("md"))
        if dt:
            return dt
    m = SLASH_DATE_RE.search(text)
    if m:
        dt = parse_date(m.group("sd"))
        if dt:
            return dt
    m = ISO_DATE_RE.search(text)
    if m:
        dt = parse_date(m.group("id"))
        if dt:
            return dt
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
        if "alternate" in rel and ("rss" in typ or "atom" in typ or href.lower().endswith((".xml", ".rss", ".atom"))):
            feeds.append(urljoin(page_url, href))

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if href.lower().endswith((".xml", ".rss", ".atom")):
            feeds.append(urljoin(page_url, href))

    out: List[str] = []
    seen = set()
    for f in feeds:
        f = canonical_url(f)
        if f not in seen and looks_like_feed_url(f):
            seen.add(f)
            out.append(f)
    return out


def items_from_feed(source: str, feed_url: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    b = fetch_bytes(feed_url, timeout=35)
    if not b:
        return out

    fp = feedparser.parse(b)

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

    snippet = ""
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        snippet = clean_text(meta_desc.get("content"), 380)

    t = soup.find("time")
    if t:
        dt = parse_date(t.get("datetime") or t.get_text(" ", strip=True))
        if dt:
            return dt, snippet

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

    dt = extract_any_date(soup.get_text(" ", strip=True))
    if dt:
        return dt, snippet

    return None, snippet


# ============================
# LISTING EXTRACTION
# ============================

def pick_container(soup: BeautifulSoup) -> Optional[Any]:
    return (
        soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find(id=re.compile(r"(main|content)", re.I))
        or soup.find("article")
        or soup.find("body")
    )


def looks_js_rendered(html: str) -> bool:
    s = (html or "").lower()
    if "select year" in s and "loading" in s:
        return True
    if "loading" in s and "news" in s and "default.aspx" in s:
        return True
    return False


def main_content_links(source: str, page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup)
    if not container:
        return []

    links: List[Tuple[str, str, Optional[datetime]]] = []
    for a in container.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        title = clean_text(a.get_text(" ", strip=True), 220)
        if not href or not title:
            continue

        if scheme(href) in GLOBAL_DENY_SCHEMES or href.startswith("#"):
            continue

        url = canonical_url(urljoin(page_url, href))
        if not allowed_for_source(source, url):
            continue

        parent = a.find_parent(["li", "article", "div", "p", "section"]) or a.parent
        near = clean_text(parent.get_text(" ", strip=True) if parent else "", 700)
        dt = extract_any_date(near)

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
    global_detail_fetches = 0
    per_source_detail_fetches: Dict[str, int] = {}

    for sp in START_PAGES:
        print(f"\n[source] {sp.source} :: {sp.url}", flush=True)

        # Feed URL directly
        if looks_like_feed_url(sp.url):
            got = items_from_feed(sp.source, sp.url, window_start, window_end)
            all_items.extend(got)
            print(f"[feed-direct] {len(got)} items from {sp.url}", flush=True)
            continue

        html = polite_get(sp.url)
        if not html:
            print("[skip] no html", flush=True)
            continue

        if looks_js_rendered(html):
            print("[note] page looks JS-rendered (may have no links in raw HTML)", flush=True)

        # Discover feeds from this HTML page
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

        # Listing extraction (HTML links)
        listing_links = main_content_links(sp.source, sp.url, html)
        print(f"[list] links captured: {len(listing_links)}", flush=True)

        src_used = per_source_detail_fetches.get(sp.source, 0)
        src_cap = PER_SOURCE_DETAIL_CAP.get(sp.source, DEFAULT_SOURCE_DETAIL_CAP)

        for title, url, dt in listing_links:
            snippet = ""

            # If no date near the link, do a detail fetch (bounded by caps)
            if dt is None:
                if global_detail_fetches >= GLOBAL_DETAIL_FETCH_CAP:
                    continue
                if src_used >= src_cap:
                    continue

                detail_html = polite_get(url)
                if not detail_html:
                    continue

                global_detail_fetches += 1
                src_used += 1
                per_source_detail_fetches[sp.source] = src_used

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

        print(f"[detail] {sp.source}: used {src_used}/{src_cap} | global {global_detail_fetches}/{GLOBAL_DETAIL_FETCH_CAP}", flush=True)

    # De-dupe by URL
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

    # Ensure output dirs exist
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    ensure_dir(RAW_DIR)

    # Write main JSON payload (used by the JS app)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # Write Copilot-friendly static exports (no JS required)
    with open(RAW_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(render_raw_html(payload))

    with open(RAW_MD_PATH, "w", encoding="utf-8") as f:
        f.write(render_raw_md(payload))

    with open(RAW_TXT_PATH, "w", encoding="utf-8") as f:
        f.write(render_raw_txt(payload))

    with open(RAW_NDJSON_PATH, "w", encoding="utf-8") as f:
        for it in payload.get("items", []):
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    # robots.txt + sitemap.xml for crawler discovery
    write_raw_aux_files()

    print(
        f"\n[ok] wrote {OUT_PATH} with {len(items)} items | detail fetches: {global_detail_fetches}\n"
        f"[ok] wrote static exports: {RAW_HTML_PATH}, {RAW_MD_PATH}, {RAW_TXT_PATH}, {RAW_NDJSON_PATH}\n"
        f"[ok] wrote crawler hints: {RAW_ROBOTS_PATH}, {RAW_SITEMAP_PATH}",
        flush=True
    )


if __name__ == "__main__":
    build()
