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
from urllib.parse import urljoin, urldefrag, urlparse, parse_qs

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

PUBLIC_BASE = "https://jasonw79118.github.io/regdashboard"

WINDOW_DAYS = 14

MAX_LISTING_LINKS = 220
GLOBAL_DETAIL_FETCH_CAP = 160
REQUEST_DELAY_SEC = 0.12

PER_SOURCE_DETAIL_CAP: Dict[str, int] = {
    "IRS": 70,
    "USDA Rural Development": 55,
    "Mastercard": 45,
    "Visa": 45,
    "Fannie Mae": 35,
    "Freddie Mac": 25,
    "FIS": 25,
    "Fiserv": 25,
    "Jack Henry": 25,
    "Temenos": 25,
    "Mambu": 20,
    "Finastra": 20,
    "TCS": 25,
    "OFAC": 30,
    "OCC": 25,
    "FDIC": 25,
    "FRB": 30,
    "NACHA": 25,
    "Federal Register": 0,  # API only
    "BleepingComputer": 0,  # feed-only
    "Microsoft MSRC": 0,     # feed-only
}
DEFAULT_SOURCE_DETAIL_CAP = 15

UA = "regdashboard/3.0 (+https://github.com/jasonw79118/regdashboard)"


# ============================
# CATEGORY MAPPING (for tiles)
#   IMPORTANT: these strings must match docs/index.html tile keys EXACTLY.
# ============================

CATEGORY_BY_SOURCE: Dict[str, str] = {
    "OFAC": "OFAC",
    "IRS": "IRS",

    # Payments tile
    "NACHA": "Payments",
    "FRB": "Payments",

    # Banking tile
    "OCC": "Banking",
    "FDIC": "Banking",

    # Mortgage tile
    "FHLB MPF": "Mortgage",
    "Fannie Mae": "Mortgage",
    "Freddie Mac": "Mortgage",

    # Legislative tile
    "Senate Banking": "Legislative",
    "White House": "Legislative",
    "Federal Register": "Legislative",

    # USDA tile
    "USDA Rural Development": "USDA",

    # Fintech Watch tile
    "FIS": "Fintech Watch",
    "Fiserv": "Fintech Watch",
    "Jack Henry": "Fintech Watch",
    "Temenos": "Fintech Watch",
    "Mambu": "Fintech Watch",
    "Finastra": "Fintech Watch",
    "TCS": "Fintech Watch",

    # Payment Card Network tile
    "Visa": "Payment Card Network",
    "Mastercard": "Payment Card Network",

    # Information Security tile
    "BleepingComputer": "Information Security",
    "Microsoft MSRC": "Information Security",
}


# ============================
# FEDERAL REGISTER API (topics)
# ============================

FEDREG_API_BASE = "https://www.federalregister.gov/api/v1"
FEDREG_TOPICS = [
    "banks-banking",
    "executive-orders",
    "federal-reserve-system",
    "national-banks",
    "securities",
    "mortgages",
    "truth-lending",
    "truth-savings",
]


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
        "allow_path_prefixes": {"/newsroom/", "/downloads/rss", "/downloads/rss/"},
        "deny_domains": {"sa.www4.irs.gov"},
    },
    "FRB": {"deny_domains": {"www.facebook.com"}},
    "USDA Rural Development": {
        "allow_domains": {"www.rd.usda.gov"},
        # FIX: USDA uses both plural and singular here
        "allow_path_prefixes": {"/newsroom/news-releases", "/newsroom/news-release/"},
    },
    "OFAC": {"allow_domains": {"ofac.treasury.gov"}},
    "Visa": {"allow_domains": {"usa.visa.com"}},
    "Mastercard": {"allow_domains": {"www.mastercard.com"}},
    "Federal Register": {
        "allow_domains": {"www.federalregister.gov"},
        "allow_path_prefixes": {"/documents/"},
    },
}

GLOBAL_DENY_DOMAINS = {"www.facebook.com"}
GLOBAL_DENY_SCHEMES = {"mailto", "tel", "javascript"}


# ============================
# HELPERS
# ============================

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

LAST_RUN_PATH = "docs/data/last_run.json"

def _safe_central_tz():
    try:
        return ZoneInfo("America/Chicago")
    except Exception:
        return timezone(timedelta(hours=-6))

CENTRAL_TZ = _safe_central_tz()


def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def canonical_url(url: str) -> str:
    url, _frag = urldefrag(url)
    return url.strip()


def clean_text(s: str, max_len: int = 320) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s


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


def looks_like_error_html(html: str) -> bool:
    if not html:
        return True
    s = html.lower()
    if "404" in s and ("page not found" in s or "not found" in s):
        return True
    if "<title>404" in s:
        return True
    return False


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


def parse_date(s: str, *, dayfirst: bool = False) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = dtparser.parse(str(s), fuzzy=True, dayfirst=dayfirst)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def in_window(dt: datetime, start: datetime, end: datetime) -> bool:
    return start <= dt <= end


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
        if r.status_code >= 400:
            print(f"[warn] GET {r.status_code}: {url}", flush=True)
            return None
        txt = r.text or ""
        if looks_like_error_html(txt):
            print(f"[warn] looks-like-error HTML: {url}", flush=True)
            return None
        return txt
    except Exception as e:
        print(f"[warn] GET failed: {url} :: {e}", flush=True)
        return None


def fetch_bytes(url: str, timeout: int = 25) -> Optional[bytes]:
    if not is_http_url(url):
        return None
    try:
        time.sleep(REQUEST_DELAY_SEC)
        r = SESSION.get(url, timeout=(10, timeout), allow_redirects=True)
        if r.status_code >= 400:
            print(f"[warn] GET {r.status_code}: {url}", flush=True)
            return None
        return r.content
    except Exception as e:
        print(f"[warn] GET failed: {url} :: {e}", flush=True)
        return None


def fetch_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 35) -> Optional[Dict[str, Any]]:
    try:
        time.sleep(REQUEST_DELAY_SEC)
        r = SESSION.get(url, params=params or {}, timeout=(10, timeout), allow_redirects=True)
        if r.status_code >= 400:
            print(f"[warn] GET {r.status_code}: {r.url}", flush=True)
            return None
        return r.json()
    except Exception as e:
        print(f"[warn] JSON GET failed: {url} :: {e}", flush=True)
        return None


# ============================
# SCHEDULER GATE (GitHub Actions friendly)
# ============================

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


def should_run_daily_ct(target_hour: int = 7, window_minutes: int = 40) -> bool:
    now_ct = datetime.now(CENTRAL_TZ)
    today = now_ct.date().isoformat()

    if _load_last_run_date() == today:
        return False

    start = now_ct.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=window_minutes)
    return start <= now_ct <= end


def force_run_enabled() -> bool:
    return os.getenv("FORCE_RUN", "").strip().lower() in {"1", "true", "yes"}


def running_in_github_actions() -> bool:
    return os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"


# ============================
# DATE PATTERNS
# ============================

MONTH_DATE_RE = re.compile(r"(?P<md>([A-Z][a-z]{2,9})\.?\s+\d{1,2},\s+\d{4})")
SLASH_DATE_RE = re.compile(r"(?P<sd>\b\d{1,2}/\d{1,2}/\d{2,4}\b)")
ISO_DATE_RE = re.compile(r"(?P<id>\b\d{4}-\d{2}-\d{2}\b)")

DAYFIRST_SOURCES = {"Visa"}


def extract_any_date(text: str, source: str = "") -> Optional[datetime]:
    if not text:
        return None

    m = MONTH_DATE_RE.search(text)
    if m:
        dt = parse_date(m.group("md"))
        if dt:
            return dt

    m = SLASH_DATE_RE.search(text)
    if m:
        dt = parse_date(m.group("sd"), dayfirst=(source in DAYFIRST_SOURCES))
        if dt:
            return dt

    m = ISO_DATE_RE.search(text)
    if m:
        dt = parse_date(m.group("id"))
        if dt:
            return dt

    return None


# ============================
# NAV / PAGINATION + JUNK FILTERS
# ============================

NAV_TITLE_RE = re.compile(
    r"^\s*(home|current page|page\s*\d+|next|previous|prev|older|newer|"
    r"first|last|back|top|menu|breadcrumb|view all|all|show more|load more)\s*$",
    re.I,
)

JUNK_TITLE_RE = re.compile(
    r"^\s*("
    r"date|newsroom|news|press releases?|press release|recent postings|"
    r"home|investor relations|corporate|resources|supervision\s*&\s*examination|"
    r"economics|consumers\s*&\s*communities|multifamily division|"
    r"current page\s*\d+"
    r")\s*$",
    re.I,
)

def is_junk_title(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return True
    if JUNK_TITLE_RE.match(t):
        return True
    return False


def is_probably_nav_link(source: str, title: str, url: str) -> bool:
    t = (title or "").strip()
    if not t:
        return True

    if NAV_TITLE_RE.match(t):
        return True

    if re.fullmatch(r"[\d]+", t):
        return True
    if re.fullmatch(r"[«»‹›→←]+", t):
        return True

    u = urlparse(url)
    q = parse_qs(u.query or "")

    if any(k in q for k in ["page", "p", "start", "offset"]):
        if NAV_TITLE_RE.search(t) or re.fullmatch(r"\d+", t):
            return True

    if source == "OFAC":
        if "page" in q:
            return True
        if u.path.rstrip("/").endswith("/recent-actions"):
            return True
        if u.path.rstrip("/").endswith("/recent-actions/enforcement-actions"):
            return True

    return False


# ============================
# FEED DETECTION + DISCOVERY
# ============================

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

    b = fetch_bytes(feed_url, timeout=40)
    if not b:
        return out

    fp = feedparser.parse(b)
    if getattr(fp, "bozo", 0):
        bozo_ex = getattr(fp, "bozo_exception", None)
        if bozo_ex:
            print(f"[warn] feed bozo: {feed_url} :: {bozo_ex}", flush=True)

    for e in fp.entries:
        title = clean_text(e.get("title", ""), 220)
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        if is_junk_title(title):
            continue

        url = canonical_url(link)
        if not allowed_for_source(source, url):
            continue
        if is_probably_nav_link(source, title, url):
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
            "category": CATEGORY_BY_SOURCE.get(source, source),
            "source": source,
            "title": title,
            "published_at": iso_z(dt),
            "url": url,
            "summary": summary,
        })

    return out


# ============================
# FEDERAL REGISTER API ITEMS
# ============================

def items_from_federal_register_topics(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """
    Uses FederalRegister.gov API v1:
      GET /documents.json
      with conditions[topics][]=<slug>
      and publication_date gte/lte
    """
    out: List[Dict[str, Any]] = []

    start_d = start.date().isoformat()
    end_d = end.date().isoformat()

    endpoint = f"{FEDREG_API_BASE.rstrip('/')}/documents.json"

    for topic in FEDREG_TOPICS:
        params: Dict[str, Any] = {
            "per_page": 200,
            "page": 1,
            "order": ["publication_date", "desc"],
            "conditions[publication_date][gte]": start_d,
            "conditions[publication_date][lte]": end_d,
            "conditions[topics][]": topic,
            "fields[]": [
                "title",
                "publication_date",
                "html_url",
                "document_number",
                "type",
                "abstract",
                "agencies",
            ],
        }

        j = fetch_json(endpoint, params=params, timeout=40)
        if not j:
            continue

        results = j.get("results") or []
        if not isinstance(results, list):
            continue

        for r in results:
            try:
                title = clean_text(str(r.get("title") or ""), 220)
                pub_s = str(r.get("publication_date") or "").strip()
                url = str(r.get("html_url") or "").strip()
                if not title or not pub_s or not url:
                    continue
                if is_junk_title(title):
                    continue

                dt = parse_date(pub_s)
                if not dt or not in_window(dt, start, end):
                    continue

                if url.startswith("/"):
                    url = "https://www.federalregister.gov" + url
                url = canonical_url(url)

                if not allowed_for_source("Federal Register", url):
                    continue

                abstract = clean_text(str(r.get("abstract") or ""), 380)

                out.append({
                    "category": CATEGORY_BY_SOURCE.get("Federal Register", "Legislative"),
                    "source": "Federal Register",
                    "title": title,
                    "published_at": iso_z(dt),
                    "url": url,
                    "summary": abstract,
                })
            except Exception:
                continue

        print(f"[api] Federal Register topic '{topic}': {len(results)} raw results", flush=True)

    return out


# ============================
# DETAIL PAGE EXTRACTION
# ============================

def extract_published_from_detail(detail_url: str, html: str, source: str = "") -> Tuple[Optional[datetime], str]:
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

    dt = extract_any_date(soup.get_text(" ", strip=True), source=source)
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
    if "loading" in s and "press release" in s:
        return True
    if "select year" in s and "loading" in s:
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
        if not href:
            continue
        if scheme(href) in GLOBAL_DENY_SCHEMES or href.startswith("#"):
            continue

        url = canonical_url(urljoin(page_url, href))
        if not allowed_for_source(source, url):
            continue

        raw_title = a.get_text(" ", strip=True) or ""
        if not raw_title:
            raw_title = (a.get("aria-label") or "").strip()
        if not raw_title:
            raw_title = (a.get("title") or "").strip()

        title = clean_text(raw_title, 220)
        if not title:
            continue

        if is_junk_title(title):
            continue

        if title.lower() in {"read more", "learn more", "more", "details"}:
            continue

        if is_probably_nav_link(source, title, url):
            continue

        parent = a.find_parent(["li", "article", "div", "p", "section", "tr", "td"]) or a.parent
        near = clean_text(parent.get_text(" ", strip=True) if parent else "", 900)
        dt = extract_any_date(near, source=source)

        # HARDENING: if no date in the surrounding block, skip obvious "directory" links
        if dt is None:
            up = urlparse(url)
            if up.path.rstrip("/").endswith(("/newsroom", "/press-releases", "/press", "/media-room", "/news")):
                continue

        links.append((title, url, dt))
        if len(links) >= MAX_LISTING_LINKS:
            break

    return links


# ============================
# SOURCES
# ============================

@dataclass
class SourcePage:
    source: str
    url: str


KNOWN_FEEDS: Dict[str, List[str]] = {
    "FRB": [
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "https://www.federalreserve.gov/feeds/press_bcreg.xml",
    ],
    "BleepingComputer": ["https://www.bleepingcomputer.com/feed/"],
    "Microsoft MSRC": ["https://api.msrc.microsoft.com/update-guide/rss"],
}

START_PAGES: List[SourcePage] = [
    # OFAC
    SourcePage("OFAC", "https://ofac.treasury.gov/recent-actions"),
    SourcePage("OFAC", "https://ofac.treasury.gov/recent-actions/enforcement-actions"),

    # IRS
    SourcePage("IRS", "https://www.irs.gov/newsroom"),
    SourcePage("IRS", "https://www.irs.gov/newsroom/news-releases-for-current-month"),
    SourcePage("IRS", "https://www.irs.gov/newsroom/irs-tax-tips"),
    SourcePage("IRS", "https://www.irs.gov/downloads/rss"),

    # USDA Rural Development
    SourcePage("USDA Rural Development", "https://www.rd.usda.gov/newsroom/news-releases"),

    # Banking regulators
    SourcePage("OCC", "https://www.occ.gov/news-issuances/news-releases/index-news-releases.html"),
    SourcePage("FDIC", "https://www.fdic.gov/news/press-releases/"),
    SourcePage("FRB", "https://www.federalreserve.gov/newsevents/pressreleases.htm"),

    # Mortgage / housing GSEs
    SourcePage("FHLB MPF", "https://www.fhlbmpf.com/about-us/news"),
    SourcePage("Fannie Mae", "https://www.fanniemae.com/rss/rss.xml"),
    SourcePage("Fannie Mae", "https://www.fanniemae.com/newsroom/fannie-mae-news"),
    SourcePage("Freddie Mac", "https://www.freddiemac.com/media-room"),

    # Legislative / exec
    SourcePage("Senate Banking", "https://www.banking.senate.gov/newsroom"),
    SourcePage("White House", "https://www.whitehouse.gov/presidential-actions/"),

    # Payments
    SourcePage("NACHA", "https://www.nacha.org/taxonomy/term/362"),

    # Fintech vendors
    SourcePage("FIS", "https://www.investor.fisglobal.com/press-releases"),
    SourcePage("Fiserv", "https://investors.fiserv.com/news-events/news-releases"),
    SourcePage("Jack Henry", "https://ir.jackhenry.com/press-releases"),
    SourcePage("Temenos", "https://www.temenos.com/press-releases/"),
    SourcePage("Mambu", "https://mambu.com/en/insights/press"),
    SourcePage("Finastra", "https://www.finastra.com/news-events/media-room"),
    SourcePage("TCS", "https://www.tcs.com/who-we-are/newsroom"),

    # Payment Networks
    SourcePage("Visa", "https://usa.visa.com/about-visa/newsroom/press-releases-listing.html"),
    SourcePage("Mastercard", "https://www.mastercard.com/us/en/news-and-trends/press.html"),

    # InfoSec (feed-only; page keeps loop stable)
    SourcePage("BleepingComputer", "https://www.bleepingcomputer.com/"),
    SourcePage("Microsoft MSRC", "https://api.msrc.microsoft.com/"),
]


# ============================
# STATIC EXPORTS (NO JS)
# ============================

def render_raw_html(payload: Dict[str, Any]) -> str:
    ws = str(payload.get("window_start", ""))
    we = str(payload.get("window_end", ""))
    gen_ct = escape(str(payload.get("generated_at_ct", "")))
    gen_utc = escape(str(payload.get("generated_at_utc", "")))
    items = payload.get("items", []) or []
    base_href = f"{PUBLIC_BASE.rstrip('/')}/raw/"

    parts: List[str] = []
    for it in items:
        cat = escape(str(it.get("category", "")))
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
                (f'    <span class="cat">{cat}</span>' if cat else ""),
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
    .meta {{ display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: #555; margin-bottom: 6px; }}
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
    <div class="small">Last updated: <code>{gen_ct}</code> (CT) — <code>{gen_utc}</code> (UTC)</div>
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
    gen_ct = str(payload.get("generated_at_ct", "")).strip()
    gen_utc = str(payload.get("generated_at_utc", "")).strip()
    items = payload.get("items", []) or []

    lines: List[str] = []
    lines.append("# RegDashboard — Export")
    lines.append("")
    lines.append(f"Window: `{ws}` → `{we}` (UTC)")
    lines.append(f"Last updated: `{gen_ct}` (CT) — `{gen_utc}` (UTC)")
    lines.append("")

    for it in items:
        title = (it.get("title") or "").strip()
        source = (it.get("source") or "").strip()
        category = (it.get("category") or "").strip()
        pub = (it.get("published_at") or "").strip()
        url = (it.get("url") or "").strip()
        summary = (it.get("summary") or "").strip()

        lines.append(f"## {title}")
        lines.append(f"- Source: {source}")
        lines.append(f"- Category: {category}")
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
        out.append(str(it.get("category", "")).strip())
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
    gen_ct = escape(str(payload.get("generated_at_ct", "")))
    gen_utc = escape(str(payload.get("generated_at_utc", "")))
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
    .meta {{ color: #444; font-size: 13px; margin-bottom: 10px; }}
    article {{ border-top: 1px solid #e5e5e5; padding-top: 12px; margin-top: 12px; }}
    .k {{ display: inline-block; min-width: 90px; color: #555; }}
    .v {{ color: #111; }}
    a {{ word-break: break-word; }}
  </style>
</head>
<body>
  <h1>RegDashboard — Print (All Items)</h1>
  <div class="meta">Window: <strong>{escape(ws)}</strong> → <strong>{escape(we)}</strong> (UTC)</div>
  <div class="meta">Last updated: <strong>{gen_ct}</strong> (CT) — <strong>{gen_utc}</strong> (UTC)</div>
"""
    parts: List[str] = [header]
    for it in items:
        cat = escape(str(it.get("category", "")).strip())
        src = escape(str(it.get("source", "")).strip())
        pub = escape(str(it.get("published_at", "")).strip())
        title = escape(str(it.get("title", "")).strip())
        url = str(it.get("url", "")).strip()
        url_esc = escape(url)
        summary = escape(str(it.get("summary", "") or "").strip())

        parts.append("<article>")
        parts.append(f"<div><span class='k'>Category</span><span class='v'>{cat}</span></div>")
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
# BUILD
# ============================

def build() -> None:
    now_utc = utc_now()
    now_ct = now_utc.astimezone(CENTRAL_TZ).replace(microsecond=0)

    window_start = now_utc - timedelta(days=WINDOW_DAYS)
    window_end = now_utc

    all_items: List[Dict[str, Any]] = []
    global_detail_fetches = 0
    per_source_detail_fetches: Dict[str, int] = {}

    pages_by_source: Dict[str, List[str]] = {}
    for sp in START_PAGES:
        pages_by_source.setdefault(sp.source, []).append(sp.url)

    for src in set(KNOWN_FEEDS.keys()) | {"Federal Register"}:
        pages_by_source.setdefault(src, [])

    for source, pages in pages_by_source.items():
        print(f"\n===== SOURCE: {source} =====", flush=True)
        source_items_before = len(all_items)

        # 0) Federal Register API (topics)
        if source == "Federal Register":
            got = items_from_federal_register_topics(window_start, window_end)
            if got:
                all_items.extend(got)
                print(f"[api] Federal Register: {len(got)} items (topics)", flush=True)
            else:
                print(f"[note] Federal Register: no qualifying items in window (or API issue).", flush=True)
            continue

        # 1) Known feeds
        for fu in KNOWN_FEEDS.get(source, []):
            got = items_from_feed(source, fu, window_start, window_end)
            if got:
                all_items.extend(got)
                print(f"[feed-known] {len(got)} items from {fu}", flush=True)

        # 2) Listing pages
        for page_url in pages:
            print(f"\n[source] {source} :: {page_url}", flush=True)

            if looks_like_feed_url(page_url):
                got = items_from_feed(source, page_url, window_start, window_end)
                all_items.extend(got)
                print(f"[feed-direct] {len(got)} items from {page_url}", flush=True)
                continue

            html = polite_get(page_url)
            if not html:
                print("[skip] no html", flush=True)
                continue

            if looks_js_rendered(html):
                print("[note] page looks JS-rendered (may have limited links)", flush=True)

            # Discover feeds
            feed_urls = discover_feeds(page_url, html)
            feed_items_total = 0
            for fu in feed_urls:
                got = items_from_feed(source, fu, window_start, window_end)
                if got:
                    all_items.extend(got)
                    feed_items_total += len(got)
                    print(f"[feed] {len(got)} items from {fu}", flush=True)
            print(f"[feed] total: {feed_items_total} | feeds found: {len(feed_urls)}", flush=True)

            # Listing links
            listing_links = main_content_links(source, page_url, html)
            print(f"[list] links captured: {len(listing_links)}", flush=True)

            src_used = per_source_detail_fetches.get(source, 0)
            src_cap = PER_SOURCE_DETAIL_CAP.get(source, DEFAULT_SOURCE_DETAIL_CAP)

            for title, url, dt in listing_links:
                if is_probably_nav_link(source, title, url):
                    continue
                if is_junk_title(title):
                    continue

                snippet = ""

                if dt is None and src_cap > 0:
                    if global_detail_fetches >= GLOBAL_DETAIL_FETCH_CAP:
                        continue
                    if src_used >= src_cap:
                        continue

                    detail_html = polite_get(url)
                    if not detail_html:
                        continue

                    global_detail_fetches += 1
                    src_used += 1
                    per_source_detail_fetches[source] = src_used

                    dt2, snippet2 = extract_published_from_detail(url, detail_html, source=source)
                    dt = dt2
                    snippet = snippet2

                if not dt:
                    continue
                if not in_window(dt, window_start, window_end):
                    continue

                all_items.append({
                    "category": CATEGORY_BY_SOURCE.get(source, source),
                    "source": source,
                    "title": title,
                    "published_at": iso_z(dt),
                    "url": url,
                    "summary": snippet,
                })

            print(f"[detail] {source}: used {src_used}/{src_cap} | global {global_detail_fetches}/{GLOBAL_DETAIL_FETCH_CAP}", flush=True)

        gained = len(all_items) - source_items_before
        if gained == 0:
            print(f"[note] {source}: no qualifying items in the last {WINDOW_DAYS} days (or blocked/changed).", flush=True)

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
        "generated_at_utc": iso_z(now_utc),
        "generated_at_ct": now_ct.isoformat(),
        "items": items,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    ensure_dir(RAW_DIR)
    ensure_dir(PRINT_DIR)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(RAW_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(render_raw_html(payload))

    with open(RAW_MD_PATH, "w", encoding="utf-8") as f:
        f.write(render_raw_md(payload))

    with open(RAW_TXT_PATH, "w", encoding="utf-8") as f:
        f.write(render_raw_txt(payload))

    with open(RAW_NDJSON_PATH, "w", encoding="utf-8") as f:
        for it in payload.get("items", []):
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    with open(PRINT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(render_print_html(payload))

    write_raw_aux_files()

    print(
        f"\n[ok] wrote {OUT_PATH} with {len(items)} items | detail fetches: {global_detail_fetches}\n"
        f"[ok] wrote raw exports: {RAW_HTML_PATH}, {RAW_MD_PATH}, {RAW_TXT_PATH}, {RAW_NDJSON_PATH}\n"
        f"[ok] wrote print export: {PRINT_HTML_PATH}\n"
        f"[ok] wrote crawler hints: {RAW_ROBOTS_PATH}, {RAW_SITEMAP_PATH}",
        flush=True
    )


if __name__ == "__main__":
    if running_in_github_actions():
        if force_run_enabled():
            print("[run] FORCE_RUN enabled -> building now", flush=True)
            build()
            _save_last_run_date(datetime.now(CENTRAL_TZ).date().isoformat())
        elif should_run_daily_ct(target_hour=7, window_minutes=40):
            build()
            _save_last_run_date(datetime.now(CENTRAL_TZ).date().isoformat())
        else:
            print("[skip] Not in 7:00 AM CT window or already ran today. Set FORCE_RUN=1 to override.", flush=True)
    else:
        print("[run] Local execution -> building now", flush=True)
        build()
        _save_last_run_date(datetime.now(CENTRAL_TZ).date().isoformat())
