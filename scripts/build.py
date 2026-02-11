from __future__ import annotations

import json
import os
import re
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urldefrag, urlparse, parse_qs

import feedparser
import requests
from bs4 import BeautifulSoup
from bs4 import XMLParsedAsHTMLWarning
from dateutil import parser as dtparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================
# CONFIG
# ============================

OUT_PATH = "docs/data/items.json"
WINDOW_DAYS = 14

# Performance controls
MAX_LISTING_LINKS = 180
GLOBAL_DETAIL_FETCH_CAP = 140
REQUEST_DELAY_SEC = 0.10

# Per-source detail caps
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
    "Finastra": 25,
    "TCS": 25,
    "OFAC": 20,
    "OCC": 20,
    "FDIC": 20,
    "FRB": 25,
    "Federal Register": 25,
}
DEFAULT_SOURCE_DETAIL_CAP = 15

# IMPORTANT: SEC requires a User-Agent that includes contact info (email).
# Set this env var in PowerShell:
#   $env:REGDASH_CONTACT="Jason Williams <[jasonw79118@gmail.com]>"
CONTACT = os.getenv("REGDASH_CONTACT", "").strip()

BASE_UA = "regdashboard/1.6 (+https://github.com/jasonw79118/regdashboard)"
UA = f"{BASE_UA} {CONTACT}".strip() if CONTACT else BASE_UA


@dataclass
class SourcePage:
    source: str
    url: str


START_PAGES: List[SourcePage] = [
    # --- Regulatory / Government ---
    SourcePage("OFAC", "https://ofac.treasury.gov/recent-actions"),

    SourcePage("IRS", "https://www.irs.gov/newsroom"),
    SourcePage("IRS", "https://www.irs.gov/newsroom/news-releases-for-current-month"),
    SourcePage("IRS", "https://www.irs.gov/newsroom/irs-tax-tips"),
    # Directory (HTML), not a feed
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

    # --- Information Security ---
    SourcePage("BleepingComputer", "https://www.bleepingcomputer.com/feed/"),
    SourcePage("Microsoft MSRC", "https://api.msrc.microsoft.com/update-guide/rss"),

    # --- Fintech Watch ---
    SourcePage("FIS", "https://www.investor.fisglobal.com/press-releases"),
    SourcePage("Fiserv", "https://investors.fiserv.com/news-events/news-releases"),
    SourcePage("Jack Henry", "https://ir.jackhenry.com/press-releases"),
    SourcePage("Temenos", "https://www.temenos.com/press-releases/"),
    SourcePage("Mambu", "https://mambu.com/en/insights/press"),
    SourcePage("Finastra", "https://www.prnewswire.com/news/Finastra/"),
    SourcePage("TCS", "https://www.tcs.com/who-we-are/newsroom"),

    # --- Payment Networks ---
    SourcePage("Visa", "https://investor.visa.com/news/default.aspx"),
    SourcePage("Mastercard", "https://investor.mastercard.com/investor-news/default.aspx"),
]


# ============================
# SESSION + RETRIES
# ============================

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
})

retry = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=0.8,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET",),
    raise_on_status=False,
)
adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)


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
        scheme = urlparse(url).scheme.lower()
        return scheme in ("http", "https")
    except Exception:
        return False

def looks_like_feed_url(url: str) -> bool:
    """
    STRICT feed detection:
    - Don't treat "contains rss" as a feed (fixes https://www.irs.gov/downloads/rss)
    """
    u = urlparse(url)
    path = (u.path or "").lower()
    qs = parse_qs(u.query or "")

    if path.endswith((".xml", ".rss", ".atom")):
        return True

    # common feed endpoints
    if path.endswith("/feed") or path.endswith("/feed/"):
        return True

    # SEC / EDGAR atom style, only if output=atom explicitly
    if qs.get("output", [""])[0].lower() == "atom":
        return True

    # federalregister API rss links usually end with .rss
    return False


# --- domain hygiene ---
DENY_DETAIL_HOST_SUBSTRINGS = [
    "sa.www4.irs.gov",          # IRS auth portals (403/401)
    "www.facebook.com",         # FRB social link noise
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
]

def should_skip_detail(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    for bad in DENY_DETAIL_HOST_SUBSTRINGS:
        if bad in host:
            return True
    return False


def polite_get(url: str, timeout: int = 25) -> Optional[str]:
    if not is_http_url(url):
        return None
    if should_skip_detail(url):
        return None

    host = urlparse(url).netloc.lower()

    # per-domain tuning
    connect_timeout = 10
    read_timeout = timeout
    if "ofac.treasury.gov" in host:
        read_timeout = 40
    if "aphis.usda.gov" in host:
        read_timeout = 40
    if "fanniemae.com" in host:
        read_timeout = 40
    if "investor" in host or "investors." in host:
        read_timeout = 35
    if "federalreserve.gov" in host:
        read_timeout = 35

    try:
        time.sleep(REQUEST_DELAY_SEC)
        r = SESSION.get(url, timeout=(connect_timeout, read_timeout), allow_redirects=True)
        if r.status_code >= 400:
            raise requests.HTTPError(f"HTTP {r.status_code}")
        return r.text
    except Exception as e:
        print(f"[warn] GET failed: {url} :: {e}", flush=True)
        return None


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
    if looks_like_feed_url(page_url):
        return [page_url]

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
# SEC: BETTER THAN EDGAR ATOM (403 FIX)
# ============================

SEC_FORMS_DEFAULT = ["8-K"]

def sec_headers() -> Dict[str, str]:
    # SEC expects declared UA with contact info; enforce if you want:
    # if not CONTACT: raise RuntimeError("Set REGDASH_CONTACT for SEC requests.")
    return {
        "User-Agent": UA,
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "application/json,text/html,*/*",
        "Connection": "keep-alive",
    }

def pad_cik(cik: str) -> str:
    s = re.sub(r"\D", "", str(cik))
    return s.zfill(10)

def sec_recent_filings(source: str, cik: str, start: datetime, end: datetime, forms: Optional[List[str]] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """
    Pull recent filings via SEC submissions JSON:
      https://data.sec.gov/submissions/CIK##########.json
    """
    forms = forms or SEC_FORMS_DEFAULT
    cik10 = pad_cik(cik)
    cik_int = str(int(cik10))  # used in Archives path

    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    try:
        time.sleep(REQUEST_DELAY_SEC)
        r = SESSION.get(url, headers=sec_headers(), timeout=(10, 35))
        if r.status_code >= 400:
            raise requests.HTTPError(f"HTTP {r.status_code}")
        data = r.json()
    except Exception as e:
        print(f"[warn] SEC submissions failed: {url} :: {e}", flush=True)
        return []

    recent = (((data or {}).get("filings") or {}).get("recent") or {})
    forms_list = recent.get("form") or []
    dates_list = recent.get("filingDate") or []
    acc_list = recent.get("accessionNumber") or []
    prim_list = recent.get("primaryDocument") or []

    out: List[Dict[str, Any]] = []
    for i in range(min(len(forms_list), len(dates_list), len(acc_list), len(prim_list))):
        form = str(forms_list[i] or "")
        if form not in forms:
            continue

        dt = parse_date(dates_list[i])
        if not dt or not in_window(dt, start, end):
            continue

        acc = str(acc_list[i] or "")
        prim = str(prim_list[i] or "")
        if not acc or not prim:
            continue

        acc_nodash = acc.replace("-", "")
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{prim}"

        title = f"{source} {form} filing"
        out.append({
            "source": source,
            "title": title,
            "published_at": iso_z(dt),
            "url": filing_url,
            "summary": f"SEC filing ({form}) on {dt.date().isoformat()}",
        })

        if len(out) >= limit:
            break

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

def main_content_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("main") or soup.find("article") or soup.find("body")
    if not container:
        return []

    links: List[Tuple[str, str, Optional[datetime]]] = []
    for a in container.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        title = clean_text(a.get_text(" ", strip=True), 220)
        if not href or not title:
            continue
        if href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue

        url = canonical_url(urljoin(page_url, href))
        if not is_http_url(url):
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

    # --- Add SEC-based items for sources that keep 403'ing on PR pages ---
    # Mastercard CIK 0001141391, Visa CIK 0001403161
    # (These give you “something reliable” even if newsroom is blocked.)
    all_items.extend(sec_recent_filings("Mastercard", "0001141391", window_start, window_end, forms=["8-K"], limit=25))
    all_items.extend(sec_recent_filings("Visa", "0001403161", window_start, window_end, forms=["8-K"], limit=25))

    for sp in START_PAGES:
        print(f"\n[source] {sp.source} :: {sp.url}", flush=True)

        if looks_like_feed_url(sp.url):
            got = items_from_feed(sp.source, sp.url, window_start, window_end)
            all_items.extend(got)
            print(f"[feed-direct] {len(got)} items from {sp.url}", flush=True)
            continue

        html = polite_get(sp.url)
        if not html:
            print("[skip] no html", flush=True)
            continue

        # FEEDS discovered from this HTML page
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

        # LISTING EXTRACTION
        listing_links = main_content_links(sp.url, html)
        print(f"[list] links captured: {len(listing_links)}", flush=True)

        src_used = per_source_detail_fetches.get(sp.source, 0)
        src_cap = PER_SOURCE_DETAIL_CAP.get(sp.source, DEFAULT_SOURCE_DETAIL_CAP)

        for title, url, dt in listing_links:
            snippet = ""

            # Don’t waste detail budget on known-bad domains
            if should_skip_detail(url):
                continue

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

    # DE-DUPE by URL
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

    print(f"\n[ok] wrote {OUT_PATH} with {len(items)} items | detail fetches: {global_detail_fetches}", flush=True)


if __name__ == "__main__":
    build()
