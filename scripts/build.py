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
from zoneinfo import ZoneInfo

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

# --- One big "print" page (single HTML file, no JS) ---
PRINT_DIR = "docs/print"
PRINT_HTML_PATH = f"{PRINT_DIR}/items.html"

# Public base used for <base> tag + sitemap links
PUBLIC_BASE = "https://jasonw79118.github.io/regdashboard"

WINDOW_DAYS = 14

MAX_LISTING_LINKS = 180
GLOBAL_DETAIL_FETCH_CAP = 140
REQUEST_DELAY_SEC = 0.10  # slightly slower = fewer timeouts / blocks

PER_SOURCE_DETAIL_CAP: Dict[str, int] = {
    "IRS": 70,
    "USDA APHIS": 45,
    "USDA": 30,
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
    "NACHA": 20,
    "Federal Register": 40,
    "White House": 20,
    "Senate Banking": 20,
    "FHLB MPF": 20,
    "CISA KEV": 20,
    "BleepingComputer": 20,
    "Microsoft MSRC": 20,
}
DEFAULT_SOURCE_DETAIL_CAP = 15

UA = "regdashboard/2.0 (+https://github.com/jasonw79118/regdashboard)"


# ============================
# SCHEDULER GATE (GitHub Actions friendly)
# - Schedule the workflow hourly (UTC)
# - This gate makes the build run once/day around 7:00 AM America/Chicago
# ============================

CENTRAL_TZ = ZoneInfo("America/Chicago")
LAST_RUN_PATH = "docs/data/last_run.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def _load_last_run_date() -> str:
    try:
        with open(LAST_RUN_PATH, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("date", "")
    except Exception:
        return ""


def _save_last_run_date(date_str: str) -> None:
    os.makedirs(os.path.dirname(LAST_RUN_PATH), exist_ok=True)
    with open(LAST_RUN_PATH, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "saved_at_utc": iso_z(utc_now())}, f)


def should_run_daily_ct(target_hour: int = 7, window_minutes: int = 20) -> bool:
    """
    Returns True only once per Central-time day, within a small window after 7:00 AM CT.
    Use with an hourly GitHub Actions schedule so DST doesn't require changing cron.
    """
    now_ct = datetime.now(CENTRAL_TZ)
    today = now_ct.date().isoformat()

    if _load_last_run_date() == today:
        return False

    start = now_ct.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=window_minutes)
    return start <= now_ct <= end


# ============================
# SOURCE STRATEGIES
# - Each source has ordered URL strategies.
# - The builder tries each URL strategy until it gathers items for that source.
# - If an RSS/Atom feed is removed / 404s, the next strategy still runs.
# ============================

@dataclass
class Strategy:
    kind: str  # "auto" | "feed" | "html"
    url: str


SOURCES: Dict[str, List[Strategy]] = {
    "OFAC": [
        Strategy("html", "https://ofac.treasury.gov/recent-actions"),
        # backup: often similar content
        Strategy("html", "https://ofac.treasury.gov/sanctions-programs-and-country-information"),
    ],
    "IRS": [
        Strategy("html", "https://www.irs.gov/newsroom"),
        Strategy("html", "https://www.irs.gov/newsroom/news-releases-for-current-month"),
        Strategy("html", "https://www.irs.gov/newsroom/irs-tax-tips"),
        # directory page (sometimes useful, sometimes empty/changed) — safe to try
        Strategy("html", "https://www.irs.gov/downloads/rss"),
    ],
    "USDA APHIS": [
        Strategy("html", "https://www.aphis.usda.gov/news"),
        # USDA-wide feeds as fallback (ensures USDA content even if APHIS page shifts)
        Strategy("feed", "https://www.usda.gov/rss/home.xml"),
        Strategy("feed", "https://www.usda.gov/rss/latest-releases.xml"),
    ],
    "USDA": [
        Strategy("feed", "https://www.usda.gov/rss/home.xml"),
        Strategy("feed", "https://www.usda.gov/rss/latest-releases.xml"),
        Strategy("html", "https://www.usda.gov/media/press-releases"),
    ],
    "NACHA": [
        Strategy("html", "https://www.nacha.org/news"),
        Strategy("html", "https://www.nacha.org/rules"),
    ],
    "OCC": [
        Strategy("html", "https://www.occ.gov/news-issuances/news-releases/index-news-releases.html"),
    ],
    "FDIC": [
        Strategy("html", "https://www.fdic.gov/news/press-releases/"),
    ],
    "FRB": [
        Strategy("feed", "https://www.federalreserve.gov/feeds/press_all.xml"),
        Strategy("feed", "https://www.federalreserve.gov/feeds/press_bcreg.xml"),
        Strategy("html", "https://www.federalreserve.gov/newsevents/pressreleases.htm"),
    ],
    "FHLB MPF": [
        Strategy("html", "https://www.fhlbmpf.com/about-us/news"),
    ],
    "Fannie Mae": [
        Strategy("feed", "https://www.fanniemae.com/rss/rss.xml"),
        Strategy("html", "https://www.fanniemae.com/newsroom/fannie-mae-news"),
    ],
    "Freddie Mac": [
        Strategy("html", "https://www.freddiemac.com/media-room"),
    ],
    "Senate Banking": [
        Strategy("html", "https://www.banking.senate.gov/newsroom"),
    ],
    "White House": [
        Strategy("html", "https://www.whitehouse.gov/presidential-actions/"),
    ],
    "Federal Register": [
        Strategy("html", "https://www.federalregister.gov/topics/banks-banking"),
        Strategy("html", "https://www.federalregister.gov/topics/executive-orders"),
        Strategy("html", "https://www.federalregister.gov/topics/federal-reserve-system"),
        Strategy("html", "https://www.federalregister.gov/topics/national-banks"),
        Strategy("html", "https://www.federalregister.gov/topics/securities"),
        Strategy("html", "https://www.federalregister.gov/topics/mortgages"),
        Strategy("html", "https://www.federalregister.gov/topics/truth-lending"),
        Strategy("html", "https://www.federalregister.gov/topics/truth-savings"),
    ],
    "CISA KEV": [
        Strategy("html", "https://github.com/cryptogennepal/cve-kev-rss/"),
    ],
    "BleepingComputer": [
        Strategy("feed", "https://www.bleepingcomputer.com/feed/"),
    ],
    "Microsoft MSRC": [
        Strategy("feed", "https://api.msrc.microsoft.com/update-guide/rss"),
    ],
    "FIS": [
        Strategy("html", "https://www.investor.fisglobal.com/press-releases"),
    ],
    "Fiserv": [
        Strategy("html", "https://investors.fiserv.com/news-events/news-releases"),
    ],
    "Jack Henry": [
        Strategy("html", "https://ir.jackhenry.com/press-releases"),
    ],
    "Temenos": [
        Strategy("html", "https://www.temenos.com/press-releases/"),
    ],
    "Mambu": [
        Strategy("html", "https://mambu.com/en/insights/press"),
    ],
    "Finastra": [
        Strategy("html", "https://www.finastra.com/news-events/media-room"),
    ],
    "TCS": [
        Strategy("html", "https://www.tcs.com/who-we-are/newsroom"),
    ],
    "Visa": [
        Strategy("html", "https://usa.visa.com/about-visa/newsroom/press-releases-listing.html"),
        Strategy("html", "https://usa.visa.com/about-visa/newsroom.html"),
    ],
    "Mastercard": [
        Strategy("html", "https://www.mastercard.com/us/en/news-and-trends/press.html"),
        Strategy("html", "https://www.mastercard.com/us/en/news-and-trends.html"),
    ],
}


# ============================
# HTTP SESSION
# ============================

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
    "Visa": {
        "allow_domains": {"usa.visa.com"},
        "allow_path_prefixes": {"/about-visa/newsroom/"},
    },
    "Mastercard": {
        "allow_domains": {"www.mastercard.com"},
        "allow_path_prefixes": {"/us/en/news-and-trends/"},
    },
    "OFAC": {
        "allow_domains": {"ofac.treasury.gov"},
    },
    "USDA APHIS": {
        "allow_domains": {"www.aphis.usda.gov"},
    },
    "USDA": {
        "allow_domains": {"www.usda.gov"},
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


def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


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


# STRICT-ish feed detection
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
    """
    Returns response text for 200 OK pages, else None.
    Logs 404s and other errors.
    """
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
        if r.status_code == 404:
            print(f"[warn] GET 404: {url}", flush=True)
            return None
        r.raise_for_status()
        # sanity check: avoid obviously-empty bodies
        if not (r.text or "").strip():
            return None
        return r.text
    except Exception as e:
        print(f"[warn] GET failed: {url} :: {e}", flush=True)
        return None


def fetch_bytes(url: str, timeout: int = 25) -> Optional[bytes]:
    """
    Returns response bytes for 200 OK, else None.
    Logs 404s and other errors.
    """
    if not is_http_url(url):
        return None
    try:
        time.sleep(REQUEST_DELAY_SEC)
        r = SESSION.get(url, timeout=(10, timeout), allow_redirects=True)
        if r.status_code == 404:
            print(f"[warn] GET 404: {url}", flush=True)
            return None
        r.raise_for_status()
        if not r.content:
            return None
        return r.content
    except Exception as e:
        print(f"[warn] GET failed: {url} :: {e}", flush=True)
        return None


# ============================
# STATIC EXPORTS (NO JS)
# ============================

def render_raw_html(payload: Dict[str, Any]) -> str:
    ws = str(payload.get("window_start", ""))
    we = str(payload.get("window_end", ""))
    items = payload.get("items", []) or []

    base_href = f"{PUBLIC_BASE.rstrip('/')}/raw/"

    parts: List[str] = []
    for it in items:
        src = escape(str(it.get("source", "")))
        title = escape(str(it.get("title", "")))
        url = escape(str(it.get("url", "")))
        pub = escape(str(it.get("published_at", "")))
        summary = escape(str(it.get("summary", "") or ""))

        parts.append(
            "\n".join([
                '<article class="card">',
                '  <div class="meta">',
                f'    <span class="src">[{src}]</span>',
                f'    <span class="pub">{pub}</span>',
                "  </div>",
                f'  <h2 class="title"><a href="{url}">{title}</a></h2>',
                (f'  <p class="sum">{summary}</p>' if summary else ""),
                f'  <p class="url">{url}</p>',
                "</article>",
            ])
        )

    body = "\n".join(parts) if parts else "<p>No items in window.</p>"

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
    <div class="small">Generated at build time. No JavaScript required.</div>
    <div class="small links">
      <a href="./items.md">items.md</a>
      <a href="./items.txt">items.txt</a>
      <a href="./items.ndjson">items.ndjson</a>
      <a href="../">Back to app</a>
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


def render_print_html(payload: Dict[str, Any]) -> str:
    ws = str(payload.get("window_start", ""))
    we = str(payload.get("window_end", ""))
    items = payload.get("items", []) or []

    header = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>RegDashboard – Print (All Items)</title>
  <meta name="description" content="Single-file print view of all RegDashboard items. No JavaScript." />
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 28px; line-height: 1.35; }}
    h1 {{ margin: 0 0 6px 0; }}
    .meta {{ color: #444; font-size: 13px; margin-bottom: 18px; }}
    article {{ border-top: 1px solid #e5e5e5; padding-top: 12px; margin-top: 12px; }}
    .k {{ display: inline-block; min-width: 90px; color: #555; }}
    .v {{ color: #111; }}
    a {{ word-break: break-word; }}
  </style>
</head>
<body>
  <h1>RegDashboard — Print (All Items)</h1>
  <div class="meta">Window: <strong>{escape(ws)}</strong> → <strong>{escape(we)}</strong> (UTC)</div>
  <div class="meta">This page is fully static HTML (no JavaScript). Newest items appear first.</div>
"""

    parts: List[str] = [header]

    for it in items:
        src = escape(str(it.get("source", "")).strip())
        pub = escape(str(it.get("published_at", "")).strip())
        title = escape(str(it.get("title", "")).strip())
        url = str(it.get("url", "")).strip()
        url_esc = escape(url)
        summary = escape(str(it.get("summary", "") or "").strip())

        parts.append("<article>")
        parts.append(f"<div><span class='k'>Source</span><span class='v'>{src}</span></div>")
        parts.append(f"<div><span class='k'>Published</span><span class='v'>{pub}</span></div>")
        parts.append(f"<div><span class='k'>Title</span><span class='v'><a href='{url_esc}'>{title}</a></span></div>")
        parts.append(f"<div><span class='k'>URL</span><span class='v'>{url_esc}</span></div>")
        if summary:
            parts.append(f"<div style='margin-top:6px'><span class='k'>Summary</span><span class='v'>{summary}</span></div>")
        parts.append("</article>")

    parts.append("</body></html>\n")
    return "\n".join(parts)


def write_raw_aux_files() -> None:
    base = PUBLIC_BASE.rstrip("/")
    raw_base = f"{base}/raw"
    print_base = f"{base}/print"

    with open(RAW_ROBOTS_PATH, "w", encoding="utf-8") as f:
        f.write("User-agent: *\nAllow: /\n")

    with open(RAW_SITEMAP_PATH, "w", encoding="utf-8") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{raw_base}/index.html</loc></url>
  <url><loc>{raw_base}/items.md</loc></url>
  <url><loc>{raw_base}/items.txt</loc></url>
  <url><loc>{raw_base}/items.ndjson</loc></url>
  <url><loc>{print_base}/items.html</loc></url>
</urlset>
""")


# ============================
# DATE PATTERNS
# ============================

MONTH_DATE_RE = re.compile(r"(?P<md>([A-Z][a-z]{2,9})\.?\s+\d{1,2},\s+\d{4})")
SLASH_DATE_RE = re.compile(r"(?P<sd>\b\d{1,2}/\d{1,2}/\d{2,4}\b)")
ISO_DATE_RE = re.compile(r"(?P<id>\b\d{4}-\d{2}-\d{2}\b)")


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
        if f not in seen and (looks_like_feed_url(f) or f.lower().endswith((".xml", ".rss", ".atom"))):
            seen.add(f)
            out.append(f)
    return out


def items_from_feed(source: str, feed_url: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    b = fetch_bytes(feed_url, timeout=35)
    if not b:
        return out

    fp = feedparser.parse(b)

    # Feed sanity: if it's totally broken, skip it.
    if getattr(fp, "bozo", False) and not getattr(fp, "entries", None):
        bozo_exc = getattr(fp, "bozo_exception", None)
        print(f"[warn] feed bozo/no-entries: {feed_url} :: {bozo_exc}", flush=True)
        return out

    entries = fp.entries or []
    if not entries:
        return out

    for e in entries:
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

        # Try to find a date higher up (helps OFAC and similar "card" layouts)
        probe = parent
        dt: Optional[datetime] = None
        for _ in range(3):
            if not probe:
                break
            near_txt = clean_text(probe.get_text(" ", strip=True), 900)
            dt = extract_any_date(near_txt)
            if dt:
                break
            probe = probe.parent

        links.append((title, url, dt))
        if len(links) >= MAX_LISTING_LINKS:
            break

    return links


# ============================
# STRATEGY RUNNER
# ============================

def run_strategy(
    source: str,
    strategy: Strategy,
    window_start: datetime,
    window_end: datetime,
    per_source_detail_fetches: Dict[str, int],
    global_detail_fetches_ref: List[int],  # single-item list as mutable int
) -> List[Dict[str, Any]]:
    """
    Executes one strategy URL and returns items found.
    Uses:
      - feed-direct parsing for feed URLs
      - otherwise: HTML -> discover feeds -> listing scrape -> detail fetch as needed
    """
    url = strategy.url
    print(f"[try] {source} :: {strategy.kind} :: {url}", flush=True)

    # Feed strategy (or auto-detected feed URL)
    if strategy.kind == "feed" or looks_like_feed_url(url):
        got = items_from_feed(source, url, window_start, window_end)
        print(f"[feed-direct] {len(got)} items from {url}", flush=True)
        return got

    # HTML strategy
    html = polite_get(url)
    if not html:
        print("[skip] no html", flush=True)
        return []

    if looks_js_rendered(html):
        print("[note] page looks JS-rendered (raw HTML may have few/no links)", flush=True)

    # Discover feeds from this HTML page
    feed_urls = discover_feeds(url, html)
    feed_items_total = 0
    all_items: List[Dict[str, Any]] = []

    for fu in feed_urls:
        try:
            got = items_from_feed(source, fu, window_start, window_end)
            feed_items_total += len(got)
            all_items.extend(got)
            if got:
                print(f"[feed] {len(got)} items from {fu}", flush=True)
        except Exception as e:
            print(f"[warn] feed parse failed: {fu} :: {e}", flush=True)

    print(f"[feed] total: {feed_items_total} | feeds found: {len(feed_urls)}", flush=True)

    # Listing extraction (HTML links)
    listing_links = main_content_links(source, url, html)
    print(f"[list] links captured: {len(listing_links)}", flush=True)

    src_used = per_source_detail_fetches.get(source, 0)
    src_cap = PER_SOURCE_DETAIL_CAP.get(source, DEFAULT_SOURCE_DETAIL_CAP)

    for title, link_url, dt in listing_links:
        snippet = ""

        # If no date near the link, do a detail fetch (bounded by caps)
        if dt is None:
            if global_detail_fetches_ref[0] >= GLOBAL_DETAIL_FETCH_CAP:
                continue
            if src_used >= src_cap:
                continue

            detail_html = polite_get(link_url)
            if not detail_html:
                continue

            global_detail_fetches_ref[0] += 1
            src_used += 1
            per_source_detail_fetches[source] = src_used

            dt2, snippet2 = extract_published_from_detail(link_url, detail_html)
            dt = dt2
            snippet = snippet2

        if not dt:
            continue
        if not in_window(dt, window_start, window_end):
            continue

        all_items.append({
            "source": source,
            "title": title,
            "published_at": iso_z(dt),
            "url": canonical_url(link_url),
            "summary": snippet,
        })

    print(f"[detail] {source}: used {src_used}/{src_cap} | global {global_detail_fetches_ref[0]}/{GLOBAL_DETAIL_FETCH_CAP}", flush=True)
    return all_items


# ============================
# BUILD
# ============================

def build() -> None:
    now = utc_now()
    window_start = now - timedelta(days=WINDOW_DAYS)
    window_end = now

    all_items: List[Dict[str, Any]] = []
    per_source_detail_fetches: Dict[str, int] = {}
    global_detail_fetches_ref = [0]

    # For each source, try each strategy URL until we actually collect items.
    # If a strategy 404s or yields 0 items (or dead feed), we continue.
    for source, strategies in SOURCES.items():
        print(f"\n[source] {source}", flush=True)
        source_items: List[Dict[str, Any]] = []

        for strat in strategies:
            got = run_strategy(
                source=source,
                strategy=strat,
                window_start=window_start,
                window_end=window_end,
                per_source_detail_fetches=per_source_detail_fetches,
                global_detail_fetches_ref=global_detail_fetches_ref,
            )

            source_items.extend(got)

            # If we got anything at all for this source, we consider it "working" and stop
            # (prevents wasting requests; de-dupe later anyway).
            if source_items:
                break

        if source_items:
            all_items.extend(source_items)
            print(f"[ok] {source}: gathered {len(source_items)} items (stopped after first successful strategy)", flush=True)
        else:
            print(f"[skip] {source}: no items from any strategy", flush=True)

    # De-dupe by canonical URL
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
    ensure_dir(PRINT_DIR)

    # Write main JSON payload (used by the JS app)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # Write Copilot-friendly raw exports (no JS required)
    with open(RAW_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(render_raw_html(payload))

    with open(RAW_MD_PATH, "w", encoding="utf-8") as f:
        f.write(render_raw_md(payload))

    with open(RAW_TXT_PATH, "w", encoding="utf-8") as f:
        f.write(render_raw_txt(payload))

    with open(RAW_NDJSON_PATH, "w", encoding="utf-8") as f:
        for it in payload.get("items", []):
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    # Write one giant print HTML file (best chance for Copilot ingestion)
    with open(PRINT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(render_print_html(payload))

    # robots.txt + sitemap.xml for crawler discovery
    write_raw_aux_files()

    print(
        f"\n[ok] wrote {OUT_PATH} with {len(items)} items | detail fetches: {global_detail_fetches_ref[0]}\n"
        f"[ok] wrote raw exports: {RAW_HTML_PATH}, {RAW_MD_PATH}, {RAW_TXT_PATH}, {RAW_NDJSON_PATH}\n"
        f"[ok] wrote print export: {PRINT_HTML_PATH}\n"
        f"[ok] wrote crawler hints: {RAW_ROBOTS_PATH}, {RAW_SITEMAP_PATH}",
        flush=True
    )


if __name__ == "__main__":
    # With GitHub Actions scheduled hourly, this will run once/day near 7:00 AM Central.
    if should_run_daily_ct(target_hour=7, window_minutes=20):
        build()
        _save_last_run_date(datetime.now(CENTRAL_TZ).date().isoformat())
    else:
        print("[skip] Not in 7:00 AM CT window or already ran today.", flush=True)
