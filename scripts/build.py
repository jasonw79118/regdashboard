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

# ✅ IMPORTANT:
# The dashboard JS typically expects docs/data/items.json to be a LIST of items.
# We will write list-only to OUT_PATH.
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

# ✅ IMPORTANT: base for regdashboard (your live site)
PUBLIC_BASE = "https://jasonw79118.github.io/regdashboard"

# ✅ RegDashboard MUST be a rolling window (2 weeks)
WINDOW_DAYS = 14

# Bump caps so Visa/Mastercard can resolve dates for more listing links
MAX_LISTING_LINKS = 260
GLOBAL_DETAIL_FETCH_CAP = 260
REQUEST_DELAY_SEC = 0.12

PER_SOURCE_DETAIL_CAP: Dict[str, int] = {
    "IRS": 70,
    "USDA Rural Development": 55,
    "Mastercard": 120,   # ✅ increased
    "Visa": 160,         # ✅ increased (Visa listing dates often missing)
    "FHLB MPF": 25,      # ✅ NEW: allow some detail fetches if needed
    "Fannie Mae": 35,
    "Freddie Mac": 10,
    "FIS": 25,
    "Fiserv": 25,
    "Jack Henry": 25,
    "Temenos": 25,
    "Mambu": 20,
    "Finastra": 20,
    "TCS": 25,
    "OFAC": 45,
    "Treasury": 60,
    "OCC": 25,
    "FDIC": 25,
    "FRB": 30,
    "NACHA": 25,
    "White House": 45,
    "Federal Register": 0,  # API only
    "BleepingComputer": 0,  # feed-only
    "Microsoft MSRC": 0,  # feed-only

    # New tiles/sources
    "CDIA": 25,
    "FASB": 25,
    "ABA": 25,
    "TBA": 25,
    "Wolters Kluwer": 25,
    "Bankers Online": 25,
}
DEFAULT_SOURCE_DETAIL_CAP = 15

UA = "regdashboard/4.2 (+https://github.com/jasonw79118/regdashboard)"


# ============================
# CATEGORY MAPPING (for tiles)
#   IMPORTANT: these strings must match your index.html tile keys EXACTLY.
# ============================

CATEGORY_BY_SOURCE: Dict[str, str] = {
    "OFAC": "OFAC",
    "Treasury": "OFAC",  # Treasury press releases show under OFAC tile
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

    # Legislative / Executive tiles
    "Senate Banking": "Legislative",
    "White House": "Executive",

    # Federal Register
    "Federal Register": "Federal Register",

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

    # Payment Card Networks tile
    "Visa": "Payment Card Networks",
    "Mastercard": "Payment Card Networks",

    # InfoSec tile
    "BleepingComputer": "IS",
    "Microsoft MSRC": "IS",

    # ✅ CHANGE: roll CDIA + FASB + others into Compliance Watch
    "CDIA": "Compliance Watch",
    "FASB": "Compliance Watch",
    "ABA": "Compliance Watch",
    "TBA": "Compliance Watch",
    "Wolters Kluwer": "Compliance Watch",
    "Bankers Online": "Compliance Watch",
}


# ============================
# FEDERAL REGISTER API (filters)
# ============================

FEDREG_API_BASE = "https://www.federalregister.gov/api/v1"

RAW_FEDREG_FILTERS: List[Dict[str, str]] = [
    {"kind": "topics", "value": "banks-banking"},
    {"kind": "topics", "value": "executive-orders"},
    {"kind": "topics", "value": "federal-reserve-system"},
    {"kind": "topics", "value": "national-banks"},
    {"kind": "topics", "value": "securities"},
    {"kind": "topics", "value": "mortgages"},
    {"kind": "topics", "value": "truth-lending"},
    {"kind": "topics", "value": "truth-savings"},

    {"kind": "agencies", "value": "consumer-financial-protection-bureau"},
    {"kind": "agencies", "value": "federal-deposit-insurance-corporation"},
    {"kind": "sections", "value": "business-and-industry"},

    {"kind": "topics", "value": "child-labor"},
    {"kind": "topics", "value": "credit"},
    {"kind": "topics", "value": "credit-unions"},
    {"kind": "topics", "value": "currency"},
    {"kind": "topics", "value": "economic-statistics"},
    {"kind": "topics", "value": "employment"},
    {"kind": "topics", "value": "employment-taxes"},
    {"kind": "topics", "value": "fair-housing"},
    {"kind": "topics", "value": "federal-home-loan-banks"},
    {"kind": "topics", "value": "flood-insurance"},
    {"kind": "topics", "value": "foreign-banking"},
    {"kind": "topics", "value": "government-sponsored-enterprise"},
    {"kind": "topics", "value": "holding-companies"},
    {"kind": "topics", "value": "housing"},
    {"kind": "topics", "value": "income-taxes"},
    {"kind": "topics", "value": "insurance"},
    {"kind": "topics", "value": "investment-companies"},
    {"kind": "topics", "value": "investments"},
    {"kind": "topics", "value": "justice-department"},
    {"kind": "topics", "value": "loan-programs"},
    {"kind": "topics", "value": "loan-programs-agriculture"},
    {"kind": "topics", "value": "loan-programs-business"},
    {"kind": "topics", "value": "loan-programs-communications"},
    {"kind": "topics", "value": "loan-programs-education"},
    {"kind": "topics", "value": "manufactured-home"},
    {"kind": "topics", "value": "mortgage-insurance"},
    {"kind": "topics", "value": "personally-identifiable-information"},
    {"kind": "topics", "value": "savings-associations"},
    {"kind": "topics", "value": "small-business"},
    {"kind": "topics", "value": "trust-and-trustees"},
]


def normalize_fedreg_slug(raw: str) -> str:
    s = (raw or "").strip()
    s = s.replace("_", "-")
    s = re.sub(r"\s+", "-", s)
    s = s.strip("-")
    s = s.lower()
    s = re.sub(r"-{2,}", "-", s)
    return s


def build_fedreg_filters() -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for f in RAW_FEDREG_FILTERS:
        kind = (f.get("kind") or "").strip().lower()
        val = normalize_fedreg_slug(f.get("value") or "")
        if kind not in {"topics", "agencies", "sections"}:
            continue
        if not val:
            continue
        key = (kind, val)
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "value": val})
    return out


FEDREG_FILTERS = build_fedreg_filters()


# ============================
# HTTP SESSION
# ============================

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
)


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

    "Freddie Mac": {
        "allow_domains": {"www.globenewswire.com"},
        "allow_path_prefixes": {
            "/search/organization/",
            "/en/search/organization/",
            "/news-release/",
            "/en/news-release/",
        },
    },

    "USDA Rural Development": {
        "allow_domains": {"content.govdelivery.com", "www.rd.usda.gov"},
        "allow_path_prefixes": {"/accounts/USDARD/bulletins", "/bulletins/", "/newsroom/"},
    },

    "OFAC": {
        "allow_domains": {"ofac.treasury.gov"},
        "allow_path_prefixes": {"/recent-actions/"},
    },

    "Treasury": {
        "allow_domains": {"home.treasury.gov"},
        "allow_path_prefixes": {"/news/press-releases"},
    },

    "White House": {
        "allow_domains": {"www.whitehouse.gov"},
        "allow_path_prefixes": {
            "/news/",
            "/briefings-statements/",
            "/presidential-actions/",
            "/fact-sheets/",
            "/remarks/",
            "/research/",
            "/articles/",
        },
    },

    "Visa": {
        "allow_domains": {"usa.visa.com"},
        "allow_path_prefixes": {"/about-visa/newsroom/press-releases"},
    },

    "Mastercard": {
        "allow_domains": {"www.mastercard.com"},
        "allow_path_prefixes": {
            "/us/en/news-and-trends/press/",
            "/global/en/news-and-trends/press/",
            "/news-and-trends/press/",
            "/en/news-and-trends/press/",
            "/gb/en/news-and-trends/press/",
            "/mea/en/news-and-trends/press/",
        },
    },

    "Federal Register": {
        "allow_domains": {"www.federalregister.gov"},
        "allow_path_prefixes": {"/documents/"},
    },

    # --- Fintech Watch vendors ---
    "FIS": {"allow_domains": {"investor.fisglobal.com", "www.investor.fisglobal.com"}},
    "Fiserv": {"allow_domains": {"investors.fiserv.com"}},
    "Jack Henry": {"allow_domains": {"ir.jackhenry.com"}},
    "Temenos": {"allow_domains": {"www.temenos.com"}},
    "Mambu": {"allow_domains": {"mambu.com"}},
    "Finastra": {
        "allow_domains": {"www.finastra.com"},
        "allow_path_prefixes": {"/news-events/media-room", "/press-media/"},
    },
    "TCS": {"allow_domains": {"www.tcs.com"}},

    "FHLB MPF": {
        "allow_domains": {"www.fhlbmpf.com"},
        "allow_path_prefixes": {"/program-guidelines/mpf-program-updates"},
    },

    "CDIA": {
        "allow_domains": {"www.cdiaonline.org"},
        "allow_path_prefixes": {"/news", "/news-events-blogs", "/events", "/blog", "/"},
    },

    "FASB": {
        "allow_domains": {"www.fasb.org", "fasb.org"},
        "allow_path_prefixes": {"/news-and-meetings/in-the-news", "/news-and-meetings/"},
    },

    "ABA": {
        "allow_domains": {"www.aba.com", "bankingjournal.aba.com"},
        "allow_path_prefixes": {"/news-research/", "/"},
    },
    "TBA": {
        "allow_domains": {"www.texasbankers.com"},
        "allow_path_prefixes": {"/news/", "/"},
    },
    "Wolters Kluwer": {
        "allow_domains": {"www.wolterskluwer.com"},
        "allow_path_prefixes": {"/en/news", "/en-gb/news", "/en/news/"},
    },
    "Bankers Online": {
        "allow_domains": {"www.bankersonline.com"},
        "allow_path_prefixes": {"/topstory", "/"},
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
    has_html = "<html" in s or "<!doctype html" in s
    has_title = "<title" in s
    has_main = "<main" in s or 'role="main"' in s

    if "<title>404" in s or "<title>page not found" in s:
        return True
    if re.search(r">(\s*)page not found(\s*)<", s):
        return True
    if re.search(r">(\s*)404(\s*)<", s) and ("not found" in s):
        return True

    if has_html and (has_title or has_main):
        return False

    if ("page not found" in s or "404 not found" in s) and not has_html:
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


def parse_slash_date_best(s: str) -> Optional[datetime]:
    if not s:
        return None

    now = utc_now()
    dt_dayfirst = parse_date(s, dayfirst=True)
    dt_monthfirst = parse_date(s, dayfirst=False)

    cands = [d for d in [dt_dayfirst, dt_monthfirst] if d is not None]
    if not cands:
        return None

    not_far_future = [d for d in cands if d <= (now + timedelta(days=30))]
    if len(not_far_future) == 1:
        return not_far_future[0]
    if len(not_far_future) > 1:
        return min(not_far_future, key=lambda d: abs((now - d).total_seconds()))

    return min(cands, key=lambda d: abs((now - d).total_seconds()))


def in_window(dt: datetime, start: datetime, end: datetime) -> bool:
    return start <= dt <= end


# ============================
# ✅ Proxy helper (r.jina.ai)
# ============================

def _jina_proxy_url(url: str) -> str:
    u = url.strip()
    if u.startswith("https://"):
        return "https://r.jina.ai/https://" + u[len("https://"):]
    if u.startswith("http://"):
        return "https://r.jina.ai/http://" + u[len("http://"):]
    return "https://r.jina.ai/http://" + u


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
    if "globenewswire.com" in h:
        read_timeout = 40
    if "federalregister.gov" in h:
        read_timeout = 35

    try:
        time.sleep(REQUEST_DELAY_SEC)

        headers: Dict[str, str] = {}

        if "whitehouse.gov" in h:
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.whitehouse.gov/",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        if "globenewswire.com" in h:
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.globenewswire.com/",
            }
        if "ofac.treasury.gov" in h:
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://ofac.treasury.gov/",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        if "home.treasury.gov" in h:
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://home.treasury.gov/",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }

        browser_ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )

        if h == "usa.visa.com":
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://usa.visa.com/",
                "User-Agent": browser_ua,
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }

        if h == "www.mastercard.com":
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.mastercard.com/",
                "User-Agent": browser_ua,
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }

        r = SESSION.get(
            url,
            headers=headers if headers else None,
            timeout=(10, read_timeout),
            allow_redirects=True,
        )

        # ✅ retry via proxy for known botwalls
        if r.status_code == 403 and h in {"www.mastercard.com", "www.finastra.com", "ir.jackhenry.com"}:
            print(f"[warn] GET 403: {url} (retrying via proxy)", flush=True)
            proxy_url = _jina_proxy_url(url)
            try:
                time.sleep(REQUEST_DELAY_SEC)
                pr = SESSION.get(
                    proxy_url,
                    headers={"User-Agent": browser_ua, "Accept": "text/html,application/xhtml+xml,*/*"},
                    timeout=(10, max(read_timeout, 40)),
                    allow_redirects=True,
                )
                if pr.status_code < 400:
                    txtp = pr.text or ""
                    if not looks_like_error_html(txtp):
                        return txtp
                    else:
                        print(f"[warn] proxy returned error-like content: {url}", flush=True)
                else:
                    print(f"[warn] proxy GET {pr.status_code}: {proxy_url}", flush=True)
            except Exception as e:
                print(f"[warn] proxy GET failed: {proxy_url} :: {e}", flush=True)
            return None

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


def fetch_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 35,
) -> Optional[Dict[str, Any]]:
    try:
        time.sleep(REQUEST_DELAY_SEC)
        r = SESSION.get(
            url,
            params=params or {},
            headers={"Accept": "application/json"},
            timeout=(10, timeout),
            allow_redirects=True,
        )
        if r.status_code >= 400:
            print(f"[warn] JSON GET {r.status_code}: {r.url}", flush=True)
            return None
        try:
            return r.json()
        except Exception:
            preview = (r.text or "")[:300].replace("\n", " ")
            print(f"[warn] JSON parse failed: {r.url} :: preview={preview}", flush=True)
            return None
    except Exception as e:
        print(f"[warn] JSON GET failed: {url} :: {e}", flush=True)
        return None


def fetch_json_status(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 35,
) -> Tuple[Optional[Dict[str, Any]], int, str]:
    try:
        time.sleep(REQUEST_DELAY_SEC)
        r = SESSION.get(
            url,
            params=params or {},
            headers={"Accept": "application/json"},
            timeout=(10, timeout),
            allow_redirects=True,
        )
        final_url = r.url
        status = int(getattr(r, "status_code", 0) or 0)

        if status >= 400:
            return None, status, final_url

        try:
            return r.json(), status, final_url
        except Exception:
            return None, 0, final_url
    except Exception:
        return None, 0, url


# ============================
# WINDOW (Rolling 2-week)
# ============================

def rolling_window_utc(now_utc: datetime) -> Tuple[datetime, datetime]:
    end = now_utc.replace(microsecond=0)
    start = (end - timedelta(days=WINDOW_DAYS)).replace(microsecond=0)
    return start, end


# ============================
# DATE PATTERNS
# ============================

MONTH_DATE_RE = re.compile(r"(?P<md>([A-Z][a-z]{2,9})\.?\s+\d{1,2},\s+\d{4})")
SLASH_DATE_RE = re.compile(r"(?P<sd>\b\d{1,2}/\d{1,2}/\d{2,4}\b)")
ISO_DATE_RE = re.compile(r"(?P<id>\b\d{4}-\d{2}-\d{2}\b)")

DAYFIRST_SOURCES: set[str] = set()


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
        sd = m.group("sd")
        if source == "Visa":
            dt = parse_slash_date_best(sd)
        else:
            dt = parse_date(sd, dayfirst=(source in DAYFIRST_SOURCES))
        if dt:
            return dt

    m = ISO_DATE_RE.search(text)
    if m:
        dt = parse_date(m.group("id"))
        if dt:
            return dt

    return None


# ============================
# NAV / PAGINATION / GENERIC LINK FILTERS
# ============================

NAV_TITLE_RE = re.compile(
    r"^\s*(home|current page|page\s*\d+|next|previous|prev|older|newer|"
    r"first|last|back|top|menu|breadcrumb|view all|all|show more|load more)\s*$",
    re.I,
)

GENERIC_TITLES = {
    "home",
    "news",
    "newsroom",
    "press releases",
    "press release",
    "recent postings",
    "date",
    "investor relations",
    "supervision & examination",
    "economics",
    "consumers & communities",
    "general licenses",
    "miscellaneous",
    "read more",
    "learn more",
}


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

    if source == "White House":
        if t.lower() in {"all", "featured", "news", "gallery", "livestream", "contact"}:
            return True

    return False


def is_generic_listing_or_home(source: str, title: str, url: str) -> bool:
    tl = (title or "").strip().lower()
    if tl in GENERIC_TITLES:
        return True

    u = urlparse(url)
    p = (u.path or "/").rstrip("/")

    if p == "":
        return True

    for hub in ["/newsroom", "/news", "/press-releases", "/pressreleases", "/media-room", "/media", "/about-us"]:
        if p.endswith(hub):
            return True

    if source == "USDA Rural Development":
        pl = p.lower()
        if pl.startswith("/bulletins/"):
            return False
        if pl.startswith("/accounts/usdard/bulletins"):
            return False
        if any(x in pl for x in ["/subscriptions/", "/subscriber/", "/preferences/"]):
            return True

    if source == "Freddie Mac":
        pl = p.lower()
        if pl.startswith("/search/organization/"):
            return False
        if pl.startswith("/en/search/organization/"):
            return False
        if pl.startswith("/news-release/"):
            return False
        if pl.startswith("/en/news-release/"):
            return False

    if source == "Treasury":
        if p.endswith("/news/press-releases"):
            return False

    if source == "Mastercard":
        if p.endswith("/news-and-trends/press"):
            return False

    if source == "FHLB MPF":
        if p.endswith("/program-guidelines/mpf-program-updates"):
            return False

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

        url = canonical_url(link)
        if not allowed_for_source(source, url):
            continue
        if is_probably_nav_link(source, title, url):
            continue
        if is_generic_listing_or_home(source, title, url):
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

        out.append(
            {
                "category": CATEGORY_BY_SOURCE.get(source, source),
                "source": source,
                "title": title,
                "published_at": iso_z(dt),
                "url": url,
                "summary": summary,
            }
        )

    return out


# ============================
# FEDERAL REGISTER API ITEMS
# ============================

def _fedreg_params_for_filter(
    kind: str,
    value: str,
    start_d: str,
    end_d: str,
    page: int,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "per_page": 200,
        "page": page,
        "order": "newest",
        "conditions[publication_date][gte]": start_d,
        "conditions[publication_date][lte]": end_d,
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

    if kind == "topics":
        params["conditions[topics][]"] = value
    elif kind == "agencies":
        params["conditions[agencies][]"] = value
    elif kind == "sections":
        params["conditions[sections][]"] = value
    else:
        params["conditions[term]"] = value

    return params


def items_from_federal_register_topics(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    start_d = start.date().isoformat()
    end_d = end.date().isoformat()
    endpoint = f"{FEDREG_API_BASE.rstrip('/')}/documents.json"

    seen_docnums: set[str] = set()

    for f in FEDREG_FILTERS:
        kind = f["kind"]
        value = f["value"]
        page = 1
        total_for_filter = 0
        tried_fallback = False

        while True:
            params = _fedreg_params_for_filter(kind, value, start_d, end_d, page)

            j, status, final_url = fetch_json_status(endpoint, params=params, timeout=45)

            if j is None and status == 400 and not tried_fallback:
                tried_fallback = True

                fallbacks: List[str] = []
                if kind == "topics":
                    fallbacks = ["sections", "agencies", "term"]
                elif kind == "sections":
                    fallbacks = ["topics", "agencies", "term"]
                elif kind == "agencies":
                    fallbacks = ["topics", "sections", "term"]
                else:
                    fallbacks = ["topics", "sections", "agencies"]

                fixed = False
                for nk in fallbacks:
                    params2 = _fedreg_params_for_filter(nk, value, start_d, end_d, page)
                    j2, status2, _u2 = fetch_json_status(endpoint, params=params2, timeout=45)
                    if j2 is not None and status2 < 400:
                        kind = nk
                        j = j2
                        status = status2
                        fixed = True
                        break

                if not fixed:
                    print(f"[warn] Federal Register filter '{value}' failed (400) for kinds tried; last={final_url}", flush=True)
                    break

            if not j:
                if status >= 400 and status != 400:
                    print(f"[warn] Federal Register JSON GET {status}: {final_url}", flush=True)
                break

            results = j.get("results") or []
            if not isinstance(results, list) or len(results) == 0:
                break

            for r in results:
                try:
                    title = clean_text(str(r.get("title") or ""), 220)
                    pub_s = str(r.get("publication_date") or "").strip()
                    url = str(r.get("html_url") or "").strip()
                    docnum = str(r.get("document_number") or "").strip()

                    if not title or not pub_s or not url:
                        continue

                    if docnum and docnum in seen_docnums:
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

                    out.append(
                        {
                            "category": CATEGORY_BY_SOURCE.get("Federal Register", "Federal Register"),
                            "source": "Federal Register",
                            "title": title,
                            "published_at": iso_z(dt),
                            "url": url,
                            "summary": abstract,
                        }
                    )

                    if docnum:
                        seen_docnums.add(docnum)

                    total_for_filter += 1
                except Exception:
                    continue

            page += 1
            if page > 20:
                break

        print(f"[api] Federal Register {kind.rstrip('s')} '{value}': kept {total_for_filter} items", flush=True)

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
# LISTING EXTRACTION (STRICTER)
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
    if "you have javascript disabled" in s:
        return True
    if "loading" in s and "press release" in s:
        return True
    if "select year" in s and "loading" in s:
        return True
    return False


def strip_nav_like(container: Any) -> None:
    for tag in container.find_all(["header", "footer", "nav", "aside"]):
        try:
            tag.decompose()
        except Exception:
            pass


def find_time_near_anchor(a: Any, source: str) -> Optional[datetime]:
    parent = a.find_parent(["li", "article", "div", "p", "section", "tr", "td"]) or a.parent
    if not parent:
        return None

    t = parent.find("time")
    if t:
        raw = (t.get("datetime") or t.get_text(" ", strip=True) or "").strip()
        if source == "Visa" and SLASH_DATE_RE.search(raw):
            dt = parse_slash_date_best(raw)
        else:
            dt = parse_date(raw, dayfirst=(source in DAYFIRST_SOURCES))
        if dt:
            return dt

    near = clean_text(parent.get_text(" ", strip=True) if parent else "", 900)
    return extract_any_date(near, source=source)


def is_likely_article_anchor(a: Any) -> bool:
    for tag in ["h1", "h2", "h3"]:
        if a.find_parent(tag) is not None:
            return True
    cls = " ".join(a.get("class", [])).lower()
    if any(k in cls for k in ["title", "headline", "card", "teaser", "post"]):
        return True
    p = a.find_parent(["article", "li"])
    if p is not None:
        return True
    return False


# ============================
# ✅ FINTECH WATCH FIXES (Finastra / Mambu / TCS / Jack Henry)
# ============================

GENERIC_CTA_TITLES = {"read the article", "read more", "learn more", "more", "details", "view", "view article"}


def _best_heading_near(a: Any) -> str:
    """
    If an anchor is a generic CTA (e.g., "Read the article"),
    try to capture the nearest headline text (h2/h3) in the same block.
    """
    wrap = a.find_parent(["li", "article", "div", "section"]) or a.parent
    if wrap:
        h = wrap.find(["h2", "h3", "h4"])
        if h:
            t = clean_text(h.get_text(" ", strip=True), 220)
            if t and t.lower() not in GENERIC_TITLES:
                return t
    return ""


def finastra_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup) or soup
    if not container:
        return []

    strip_nav_like(container)

    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen: set[str] = set()

    for a in container.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue

        url = canonical_url(urljoin(page_url, href))
        if not allowed_for_source("Finastra", url):
            continue

        t = clean_text((a.get_text(" ", strip=True) or "").strip(), 220)
        tl = t.lower()

        if not t or tl in GENERIC_CTA_TITLES:
            t2 = _best_heading_near(a)
            if not t2:
                continue
            t = t2

        if is_probably_nav_link("Finastra", t, url):
            continue
        if is_generic_listing_or_home("Finastra", t, url):
            continue
        if url in seen:
            continue
        seen.add(url)

        dt = find_time_near_anchor(a, "Finastra")
        if dt is None:
            wrap = a.find_parent(["li", "article", "div", "section"]) or a.parent
            if wrap:
                dt = extract_any_date(clean_text(wrap.get_text(" ", strip=True), 1000), source="Finastra")

        links.append((t, url, dt))
        if len(links) >= MAX_LISTING_LINKS:
            break

    return links


def tcs_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup) or soup
    if not container:
        return []

    strip_nav_like(container)

    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen: set[str] = set()

    for a in container.select('a[href*="/who-we-are/newsroom/press-release/"]'):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue

        url = canonical_url(urljoin(page_url, href))
        if not allowed_for_source("TCS", url):
            continue

        title = clean_text((a.get_text(" ", strip=True) or "").strip(), 220)
        if not title:
            continue

        if is_probably_nav_link("TCS", title, url):
            continue
        if is_generic_listing_or_home("TCS", title, url):
            continue

        if url in seen:
            continue
        seen.add(url)

        dt = find_time_near_anchor(a, "TCS")
        if dt is None:
            wrap = a.find_parent(["li", "div", "section"]) or a.parent
            if wrap:
                dt = extract_any_date(clean_text(wrap.get_text(" ", strip=True), 1000), source="TCS")

        links.append((title, url, dt))
        if len(links) >= MAX_LISTING_LINKS:
            break

    return links


def mambu_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup) or soup
    if not container:
        return []

    strip_nav_like(container)

    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen: set[str] = set()

    for a in container.select('a[href^="/en/insights/press/"], a[href*="/en/insights/press/"]'):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue

        url = canonical_url(urljoin(page_url, href))
        if not allowed_for_source("Mambu", url):
            continue

        title = clean_text((a.get_text(" ", strip=True) or "").strip(), 220)
        if not title or title.lower() in GENERIC_CTA_TITLES:
            title2 = _best_heading_near(a)
            if not title2:
                continue
            title = title2

        if is_probably_nav_link("Mambu", title, url):
            continue
        if is_generic_listing_or_home("Mambu", title, url):
            continue

        if url in seen:
            continue
        seen.add(url)

        dt = find_time_near_anchor(a, "Mambu")
        if dt is None:
            wrap = a.find_parent(["li", "article", "div", "section"]) or a.parent
            if wrap:
                dt = extract_any_date(clean_text(wrap.get_text(" ", strip=True), 1200), source="Mambu")

        links.append((title, url, dt))
        if len(links) >= MAX_LISTING_LINKS:
            break

    return links


def jackhenry_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup) or soup
    if not container:
        return []

    strip_nav_like(container)

    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen: set[str] = set()

    for a in container.select('a[href*="/news-releases/news-release-details/"]'):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue

        url = canonical_url(urljoin(page_url, href))
        if not allowed_for_source("Jack Henry", url):
            continue

        title = clean_text((a.get_text(" ", strip=True) or "").strip(), 220)
        if not title or title.lower() in GENERIC_CTA_TITLES:
            title2 = _best_heading_near(a)
            if title2:
                title = title2

        if not title:
            continue

        if is_probably_nav_link("Jack Henry", title, url):
            continue
        if is_generic_listing_or_home("Jack Henry", title, url):
            continue

        if url in seen:
            continue
        seen.add(url)

        dt = find_time_near_anchor(a, "Jack Henry")
        if dt is None:
            wrap = a.find_parent(["li", "article", "div", "section"]) or a.parent
            if wrap:
                dt = extract_any_date(clean_text(wrap.get_text(" ", strip=True), 1200), source="Jack Henry")

        links.append((title, url, dt))
        if len(links) >= MAX_LISTING_LINKS:
            break

    return links


# ============================
# FHLB MPF (Program Updates)
# ============================

FHLBMPF_LISTING_PATH = "/program-guidelines/mpf-program-updates"
FHLBMPF_DETAIL_PREFIX = "/program-guidelines/mpf-program-updates/"


def fhlbmpf_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup) or soup
    if not container:
        return []

    strip_nav_like(container)

    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen = set()

    for a in container.select(f'a[href^="{FHLBMPF_DETAIL_PREFIX}"]'):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue

        if href.rstrip("/") == FHLBMPF_LISTING_PATH.rstrip("/"):
            continue

        url = canonical_url(urljoin(page_url, href))
        if not allowed_for_source("FHLB MPF", url):
            continue

        raw_title = (a.get_text(" ", strip=True) or "").strip()
        if not raw_title:
            raw_title = (a.get("aria-label") or "").strip() or (a.get("title") or "").strip()

        title = clean_text(raw_title, 220)
        if not title or len(title) < 8:
            continue

        if title.lower() in {"read more", "learn more", "more", "details"}:
            continue
        if is_probably_nav_link("FHLB MPF", title, url):
            continue
        if is_generic_listing_or_home("FHLB MPF", title, url):
            continue

        if url in seen:
            continue
        seen.add(url)

        dt = find_time_near_anchor(a, "FHLB MPF")

        links.append((title, url, dt))
        if len(links) >= MAX_LISTING_LINKS:
            break

    return links


# ----------------------------
# OFAC: item pages are /recent-actions/YYYYMMDD
# ----------------------------
OFAC_ITEM_RE = re.compile(r"^/recent-actions/\d{8}(/)?$")
OFAC_URL_DATE_RE = re.compile(r"/recent-actions/(?P<ymd>\d{8})(?:/)?$")


def ofac_date_from_url(url: str) -> Optional[datetime]:
    try:
        m = OFAC_URL_DATE_RE.search(urlparse(url).path)
        if not m:
            return None
        ymd = m.group("ymd")
        dt = datetime.strptime(ymd, "%Y%m%d").replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def ofac_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup) or soup
    if not container:
        return []

    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen = set()

    for a in container.select('a[href^="/recent-actions/"]'):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue

        if not OFAC_ITEM_RE.match(href):
            continue

        url = canonical_url(urljoin(page_url, href))
        if not allowed_for_source("OFAC", url):
            continue

        raw_title = (a.get_text(" ", strip=True) or "").strip()
        title = clean_text(raw_title, 220)
        if not title or len(title) < 8:
            continue

        if url in seen:
            continue
        seen.add(url)

        dt = find_time_near_anchor(a, "OFAC")
        if dt is None:
            dt = ofac_date_from_url(url)

        if dt is None:
            wrap = a.find_parent(["div", "article", "section", "p"]) or a.parent
            if wrap:
                dt = extract_any_date(clean_text(wrap.get_text(" ", strip=True), 1000), source="OFAC")

        links.append((title, url, dt))
        if len(links) >= MAX_LISTING_LINKS:
            break

    return links


def whitehouse_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup) or soup
    if not container:
        return []

    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen = set()

    for a in container.select("h2 a[href], h3 a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue

        url = canonical_url(urljoin(page_url, href))
        if not allowed_for_source("White House", url):
            continue

        title = clean_text(a.get_text(" ", strip=True) or "", 220)
        if not title:
            continue
        if is_probably_nav_link("White House", title, url):
            continue
        if is_generic_listing_or_home("White House", title, url):
            continue

        if url in seen:
            continue
        seen.add(url)

        dt = find_time_near_anchor(a, "White House")
        if dt is None:
            wrap = a.find_parent(["div", "article", "li", "section"]) or a.parent
            if wrap:
                dt = extract_any_date(clean_text(wrap.get_text(" ", strip=True), 1000), source="White House")

        links.append((title, url, dt))
        if len(links) >= MAX_LISTING_LINKS:
            break

    return links


# ============================
# Mastercard / Visa / Treasury / Freddie / CDIA
# ============================

MASTERCARD_PR_PATH_RE = re.compile(
    r"^/(us|global|gb|mea)/en/news-and-trends/press/"
    r"(?P<year>\d{4})"
    r"(?:/[a-z]{3,12})?"
    r"/[a-z0-9\-%]+\.html$",
    re.I,
)

MC_MARKDOWN_LINK_RE = re.compile(
    r"\[([^\]]{8,220})\]\((https?://www\.mastercard\.com/[^\s)]+)\)",
    re.I,
)


def _mastercard_links_from_text(page_url: str, text: str) -> List[Tuple[str, str, Optional[datetime]]]:
    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen: set[str] = set()

    for m in MC_MARKDOWN_LINK_RE.finditer(text or ""):
        title = clean_text(m.group(1), 220)
        url = canonical_url(m.group(2))
        if not url:
            continue
        if not allowed_for_source("Mastercard", url):
            continue
        if not MASTERCARD_PR_PATH_RE.match(urlparse(url).path):
            continue
        if is_probably_nav_link("Mastercard", title, url):
            continue
        if is_generic_listing_or_home("Mastercard", title, url):
            continue
        if url in seen:
            continue
        seen.add(url)

        dt = extract_any_date(text, source="Mastercard")
        links.append((title, url, dt))
        if len(links) >= MAX_LISTING_LINKS:
            return links

    raw_url_re = re.compile(r"(https?://www\.mastercard\.com/[^\s\"')<>]+)", re.I)
    for m in raw_url_re.finditer(text or ""):
        url = canonical_url(m.group(1))
        if not url:
            continue
        if not allowed_for_source("Mastercard", url):
            continue
        if not MASTERCARD_PR_PATH_RE.match(urlparse(url).path):
            continue
        if url in seen:
            continue
        seen.add(url)
        links.append(("Mastercard press release", url, None))
        if len(links) >= MAX_LISTING_LINKS:
            break

    return links


def mastercard_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup) or soup
    if not container:
        return []

    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen = set()

    for a in container.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue

        u = urlparse(urljoin(page_url, href))
        if u.netloc.lower() != "www.mastercard.com":
            continue
        if not MASTERCARD_PR_PATH_RE.match(u.path):
            continue

        url = canonical_url(u.geturl())
        if not allowed_for_source("Mastercard", url):
            continue

        raw_title = (a.get_text(" ", strip=True) or "").strip()
        if not raw_title:
            raw_title = (a.get("aria-label") or "").strip() or (a.get("title") or "").strip()

        title = clean_text(raw_title, 220)
        if not title or len(title) < 10:
            continue

        if title.lower() in {"read more", "learn more", "more", "details"}:
            continue
        if is_probably_nav_link("Mastercard", title, url):
            continue
        if is_generic_listing_or_home("Mastercard", title, url):
            continue

        if url in seen:
            continue
        seen.add(url)

        dt = find_time_near_anchor(a, "Mastercard")
        if dt is None:
            wrap = a.find_parent(["li", "article", "div", "section"]) or a.parent
            if wrap:
                dt = extract_any_date(clean_text(wrap.get_text(" ", strip=True), 1000), source="Mastercard")

        links.append((title, url, dt))
        if len(links) >= MAX_LISTING_LINKS:
            return links

    if len(links) < 5:
        extra = _mastercard_links_from_text(page_url, html)
        for t, u, d in extra:
            if u in seen:
                continue
            seen.add(u)
            links.append((t, u, d))
            if len(links) >= MAX_LISTING_LINKS:
                break

    return links


def visa_date_from_listing_context(a: Any) -> Optional[datetime]:
    if not a:
        return None

    head = a.find_parent(["h1", "h2", "h3"]) or a

    try:
        checked = 0
        for sib in head.previous_siblings:
            if checked >= 25:
                break
            checked += 1

            txt = ""
            if isinstance(sib, str):
                txt = sib.strip()
            else:
                try:
                    txt = (sib.get_text(" ", strip=True) or "").strip()
                except Exception:
                    txt = ""

            if not txt:
                continue

            m = SLASH_DATE_RE.search(txt)
            if m:
                return parse_slash_date_best(m.group("sd"))

            dt = extract_any_date(txt, source="Visa")
            if dt:
                return dt
    except Exception:
        pass

    return None


def visa_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup) or soup
    if not container:
        return []

    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen = set()

    selectors = [
        'a[href*="/about-visa/newsroom/press-releases.releaseId."]',
        'a[href*="/about-visa/newsroom/press-releases/"]',
        'a[href*="press-releases.releaseId."]',
        'a[href*="/press-releases.releaseId."]',
    ]

    for sel in selectors:
        for a in container.select(sel):
            href = (a.get("href") or "").strip()
            if not href or href.startswith("#"):
                continue

            url = canonical_url(urljoin(page_url, href))
            if not allowed_for_source("Visa", url):
                continue

            raw_title = (a.get_text(" ", strip=True) or "").strip()
            if not raw_title:
                raw_title = (a.get("aria-label") or "").strip() or (a.get("title") or "").strip()
            title = clean_text(raw_title, 220)
            if not title or len(title) < 8:
                continue

            if title.lower() in {"read more", "learn more", "more", "details"}:
                continue
            if is_probably_nav_link("Visa", title, url):
                continue
            if is_generic_listing_or_home("Visa", title, url):
                continue

            if url in seen:
                continue
            seen.add(url)

            dt = find_time_near_anchor(a, "Visa")
            if dt is None:
                dt = visa_date_from_listing_context(a)

            links.append((title, url, dt))
            if len(links) >= MAX_LISTING_LINKS:
                return links

    return links


TREASURY_PR_PATH_RE = re.compile(r"^/news/press-releases/[a-z0-9\-]+$", re.I)


def treasury_date_from_listing_context(a: Any) -> Optional[datetime]:
    if not a:
        return None

    wrap = a.find_parent(["li", "article", "div", "section"]) or a.parent
    if wrap:
        blob = clean_text(wrap.get_text(" ", strip=True), 900)
        dt = extract_any_date(blob, source="Treasury")
        if dt:
            return dt

    head = a.find_parent(["h1", "h2", "h3", "h4"]) or a
    try:
        checked = 0
        for sib in head.previous_siblings:
            if checked >= 30:
                break
            checked += 1
            txt = ""
            if isinstance(sib, str):
                txt = sib.strip()
            else:
                try:
                    txt = (sib.get_text(" ", strip=True) or "").strip()
                except Exception:
                    txt = ""
            if not txt:
                continue
            dt = extract_any_date(txt, source="Treasury")
            if dt:
                return dt
    except Exception:
        pass

    return None


def treasury_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup) or soup
    if not container:
        return []

    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen = set()

    for a in container.select('a[href^="/news/press-releases/"]'):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        if not TREASURY_PR_PATH_RE.match(href):
            continue

        url = canonical_url(urljoin(page_url, href))
        if not allowed_for_source("Treasury", url):
            continue

        raw_title = (a.get_text(" ", strip=True) or "").strip()
        if not raw_title:
            raw_title = (a.get("aria-label") or "").strip() or (a.get("title") or "").strip()

        title = clean_text(raw_title, 220)
        if not title or len(title) < 8:
            continue

        if title.lower() in {"read more", "learn more", "more", "details"}:
            continue
        if is_probably_nav_link("Treasury", title, url):
            continue
        if is_generic_listing_or_home("Treasury", title, url):
            continue

        if url in seen:
            continue
        seen.add(url)

        dt = find_time_near_anchor(a, "Treasury")
        if dt is None:
            dt = treasury_date_from_listing_context(a)

        links.append((title, url, dt))
        if len(links) >= MAX_LISTING_LINKS:
            break

    if not links:
        for a in container.select("h2 a[href], h3 a[href]"):
            href = (a.get("href") or "").strip()
            if not href or not href.startswith("/news/press-releases/"):
                continue
            if not TREASURY_PR_PATH_RE.match(href):
                continue

            url = canonical_url(urljoin(page_url, href))
            if not allowed_for_source("Treasury", url):
                continue

            title = clean_text(a.get_text(" ", strip=True) or "", 220)
            if not title:
                continue

            if url in seen:
                continue
            seen.add(url)

            dt = find_time_near_anchor(a, "Treasury")
            if dt is None:
                dt = treasury_date_from_listing_context(a)

            links.append((title, url, dt))
            if len(links) >= MAX_LISTING_LINKS:
                break

    return links


def _globenewswire_find_date_near(a: Any, source: str) -> Optional[datetime]:
    if not a:
        return None

    dt = find_time_near_anchor(a, source)
    if dt:
        return dt

    cur = a
    for _ in range(0, 5):
        cur = cur.parent if getattr(cur, "parent", None) is not None else None
        if not cur or not getattr(cur, "get_text", None):
            break

        try:
            for sel in [
                ".date",
                ".release-date",
                ".releaseDate",
                ".timestamp",
                ".time",
                "[class*='date']",
                "[class*='time']",
                "[class*='timestamp']",
            ]:
                el = cur.select_one(sel)
                if el and getattr(el, "get_text", None):
                    dt2 = extract_any_date(clean_text(el.get_text(" ", strip=True), 240), source=source)
                    if dt2:
                        return dt2
        except Exception:
            pass

        try:
            blob = clean_text(cur.get_text(" ", strip=True), 1200)
            dt2 = extract_any_date(blob, source=source)
            if dt2:
                return dt2
        except Exception:
            pass

    return None


def freddiemac_globenewswire_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup) or soup
    if not container:
        return []

    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen = set()

    for a in container.select('a[href*="/news-release/"], a[href*="/en/news-release/"]'):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue

        url = canonical_url(urljoin(page_url, href))
        if not allowed_for_source("Freddie Mac", url):
            continue

        raw_title = (a.get_text(" ", strip=True) or "").strip()
        if not raw_title:
            raw_title = (a.get("aria-label") or "").strip() or (a.get("title") or "").strip()
        if not raw_title:
            continue

        title = clean_text(raw_title, 220)
        if not title or len(title) < 8:
            continue

        if title.lower() in {"read more", "learn more", "more", "details"}:
            continue
        if is_probably_nav_link("Freddie Mac", title, url):
            continue
        if is_generic_listing_or_home("Freddie Mac", title, url):
            continue

        if url in seen:
            continue
        seen.add(url)

        dt = _globenewswire_find_date_near(a, "Freddie Mac")
        links.append((title, url, dt))

        if len(links) >= MAX_LISTING_LINKS:
            break

    return links


def cdia_links(page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup) or soup
    if not container:
        return []

    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen = set()

    for a in container.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue

        url = canonical_url(urljoin(page_url, href))
        if not allowed_for_source("CDIA", url):
            continue

        t = (a.get_text(" ", strip=True) or "").strip()
        tl = t.lower()

        if tl in {"read more", "learn more", "more", ""}:
            wrap = a.find_parent(["article", "div", "section", "li"]) or a.parent
            if not wrap:
                continue

            h = wrap.find(["h1", "h2", "h3", "h4"])
            if h:
                title = clean_text(h.get_text(" ", strip=True), 220)
            else:
                blob = clean_text(wrap.get_text(" ", strip=True), 500)
                title = clean_text(blob.split("…")[0], 220)

            if not title or title.lower() in GENERIC_TITLES:
                continue
        else:
            title = clean_text(t, 220)

        if is_probably_nav_link("CDIA", title, url):
            continue
        if is_generic_listing_or_home("CDIA", title, url):
            continue
        if url in seen:
            continue
        seen.add(url)

        dt = find_time_near_anchor(a, "CDIA")
        links.append((title, url, dt))

        if len(links) >= MAX_LISTING_LINKS:
            break

    return links


def main_content_links(source: str, page_url: str, html: str) -> List[Tuple[str, str, Optional[datetime]]]:
    if source == "OFAC":
        return ofac_links(page_url, html)
    if source == "Treasury":
        return treasury_links(page_url, html)
    if source == "White House":
        return whitehouse_links(page_url, html)
    if source == "Mastercard":
        return mastercard_links(page_url, html)
    if source == "Visa":
        return visa_links(page_url, html)
    if source == "Freddie Mac":
        return freddiemac_globenewswire_links(page_url, html)
    if source == "CDIA":
        return cdia_links(page_url, html)
    if source == "FHLB MPF":
        return fhlbmpf_links(page_url, html)

    if source == "Finastra":
        return finastra_links(page_url, html)
    if source == "TCS":
        return tcs_links(page_url, html)
    if source == "Mambu":
        return mambu_links(page_url, html)
    if source == "Jack Henry":
        return jackhenry_links(page_url, html)

    soup = BeautifulSoup(html, "html.parser")
    container = pick_container(soup)
    if not container:
        return []

    strip_nav_like(container)

    links: List[Tuple[str, str, Optional[datetime]]] = []
    seen = set()

    for a in container.find_all("a", href=True):
        if not is_likely_article_anchor(a):
            continue

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

        if title.lower() in {"read more", "learn more", "more", "details"}:
            continue
        if is_probably_nav_link(source, title, url):
            continue
        if is_generic_listing_or_home(source, title, url):
            continue

        if url in seen:
            continue
        seen.add(url)

        dt = find_time_near_anchor(a, source)
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
    "Fiserv": ["https://investors.fiserv.com/newsroom/rss"],
}


def get_start_pages() -> List[SourcePage]:
    now_ct = utc_now().astimezone(CENTRAL_TZ)
    y = now_ct.year
    mc_year_pages = [
        f"https://www.mastercard.com/us/en/news-and-trends/press/{y}.html",
        f"https://www.mastercard.com/us/en/news-and-trends/press/{y-1}.html",
    ]

    pages = [
        # OFAC
        SourcePage("OFAC", "https://ofac.treasury.gov/recent-actions"),
        SourcePage("OFAC", "https://ofac.treasury.gov/recent-actions/enforcement-actions"),

        # Treasury Press Releases (OFAC tile)
        SourcePage("Treasury", "https://home.treasury.gov/news/press-releases"),

        # IRS
        SourcePage("IRS", "https://www.irs.gov/newsroom"),
        SourcePage("IRS", "https://www.irs.gov/newsroom/news-releases-for-current-month"),
        SourcePage("IRS", "https://www.irs.gov/downloads/rss"),

        # USDA RD
        SourcePage("USDA Rural Development", "https://www.rd.usda.gov/newsroom/news-releases"),

        # Banking regulators
        SourcePage("OCC", "https://www.occ.gov/news-issuances/news-releases/index-news-releases.html"),
        SourcePage("FDIC", "https://www.fdic.gov/news/press-releases/"),
        SourcePage("FRB", "https://www.federalreserve.gov/newsevents/pressreleases.htm"),

        # Mortgage / housing GSEs
        SourcePage("FHLB MPF", "https://www.fhlbmpf.com/program-guidelines/mpf-program-updates"),
        SourcePage("Fannie Mae", "https://www.fanniemae.com/rss/rss.xml"),
        SourcePage("Fannie Mae", "https://www.fanniemae.com/newsroom/fannie-mae-news"),
        SourcePage("Freddie Mac", "https://www.globenewswire.com/search/organization/Freddie%20Mac"),

        # Legislative / exec
        SourcePage("Senate Banking", "https://www.banking.senate.gov/newsroom"),
        SourcePage("White House", "https://www.whitehouse.gov/news/"),
        SourcePage("White House", "https://www.whitehouse.gov/presidential-actions/"),

        # Payments
        SourcePage("NACHA", "https://www.nacha.org/news"),

        # Fintech vendors
        SourcePage("FIS", "https://www.investor.fisglobal.com/press-releases/"),
        SourcePage("Fiserv", "https://investors.fiserv.com/newsroom/news-releases"),
        SourcePage("Jack Henry", "https://ir.jackhenry.com/press-releases"),
        SourcePage("Temenos", "https://www.temenos.com/news/press-releases/"),
        SourcePage("Mambu", "https://mambu.com/en/insights/press"),
        SourcePage("Finastra", "https://www.finastra.com/news-events/media-room"),
        SourcePage("TCS", "https://www.tcs.com/who-we-are/newsroom"),

        # Payment Networks
        SourcePage("Visa", "https://usa.visa.com/about-visa/newsroom/press-releases-listing.html"),

        # ✅ Mastercard
        SourcePage("Mastercard", "https://www.mastercard.com/us/en/news-and-trends/press.html"),
    ]

    for u in mc_year_pages:
        pages.append(SourcePage("Mastercard", u))

    pages.extend(
        [
            # InfoSec (feed-only)
            SourcePage("BleepingComputer", "https://www.bleepingcomputer.com/"),
            SourcePage("Microsoft MSRC", "https://api.msrc.microsoft.com/"),

            # CDIA
            SourcePage("CDIA", "https://www.cdiaonline.org/news-events-blogs"),

            # FASB
            SourcePage("FASB", "https://www.fasb.org/news-and-meetings/in-the-news"),

            # Compliance Watch sources
            SourcePage("ABA", "https://www.aba.com/news-research/all-news#sort=%40fcontentdate%20descending"),
            SourcePage("TBA", "https://www.texasbankers.com/news/"),
            SourcePage("Wolters Kluwer", "https://www.wolterskluwer.com/en/news?f:contenttype=News%20Page%7CPress%20Release%20Page"),
            SourcePage("Bankers Online", "https://www.bankersonline.com/topstory"),
        ]
    )

    return pages


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
            "\n".join(
                [
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
                ]
            )
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
        f.write(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{raw_base}/index.html</loc></url>
  <url><loc>{raw_base}/items.md</loc></url>
  <url><loc>{raw_base}/items.txt</loc></url>
  <url><loc>{raw_base}/items.ndjson</loc></url>
  <url><loc>{print_base}/items.html</loc></url>
</urlset>
"""
        )


# ============================
# BUILD
# ============================

def build() -> None:
    now_utc = utc_now()
    now_ct = now_utc.astimezone(CENTRAL_TZ).replace(microsecond=0)

    window_start, window_end = rolling_window_utc(now_utc)

    all_items: List[Dict[str, Any]] = []
    global_detail_fetches = 0
    per_source_detail_fetches: Dict[str, int] = {}

    pages_by_source: Dict[str, List[str]] = {}
    for sp in get_start_pages():
        pages_by_source.setdefault(sp.source, []).append(sp.url)

    for src in set(KNOWN_FEEDS.keys()) | {"Federal Register"}:
        pages_by_source.setdefault(src, [])

    for source, pages in pages_by_source.items():
        print(f"\n===== SOURCE: {source} =====", flush=True)
        source_items_before = len(all_items)

        if source == "Federal Register":
            got = items_from_federal_register_topics(window_start, window_end)
            if got:
                all_items.extend(got)
                print(f"[api] Federal Register: {len(got)} items (filters)", flush=True)
            else:
                print("[note] Federal Register: no qualifying items in window (or API issue).", flush=True)
            continue

        for fu in KNOWN_FEEDS.get(source, []):
            got = items_from_feed(source, fu, window_start, window_end)
            if got:
                all_items.extend(got)
                print(f"[feed-known] {len(got)} items from {fu}", flush=True)

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
                print("[note] page looks JS-rendered; using strict extraction (may be limited)", flush=True)

            feed_urls = discover_feeds(page_url, html)
            feed_items_total = 0
            for fu in feed_urls:
                got = items_from_feed(source, fu, window_start, window_end)
                if got:
                    all_items.extend(got)
                    feed_items_total += len(got)
                    print(f"[feed] {len(got)} items from {fu}", flush=True)
            print(f"[feed] total: {feed_items_total} | feeds found: {len(feed_urls)}", flush=True)

            listing_links = main_content_links(source, page_url, html)
            print(f"[list] links captured: {len(listing_links)}", flush=True)

            src_used = per_source_detail_fetches.get(source, 0)
            src_cap = PER_SOURCE_DETAIL_CAP.get(source, DEFAULT_SOURCE_DETAIL_CAP)

            for title, url, dt in listing_links:
                if is_probably_nav_link(source, title, url):
                    continue
                if is_generic_listing_or_home(source, title, url):
                    continue

                snippet = ""

                if source == "Visa" and dt is not None and (not in_window(dt, window_start, window_end)) and src_cap > 0:
                    if global_detail_fetches < GLOBAL_DETAIL_FETCH_CAP and src_used < src_cap:
                        detail_html = polite_get(url)
                        if detail_html:
                            global_detail_fetches += 1
                            src_used += 1
                            per_source_detail_fetches[source] = src_used

                            dt2, snippet2 = extract_published_from_detail(url, detail_html, source=source)
                            if dt2:
                                dt = dt2
                            if snippet2:
                                snippet = snippet2

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

                all_items.append(
                    {
                        "category": CATEGORY_BY_SOURCE.get(source, source),
                        "source": source,
                        "title": title,
                        "published_at": iso_z(dt),
                        "url": url,
                        "summary": snippet,
                    }
                )

            print(
                f"[detail] {source}: used {src_used}/{src_cap} | global {global_detail_fetches}/{GLOBAL_DETAIL_FETCH_CAP}",
                flush=True,
            )

        gained = len(all_items) - source_items_before
        if gained == 0:
            print("[note] no qualifying items in rolling window (or blocked/changed).", flush=True)

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

    # ✅ Full payload (used for exports + last_run.json)
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

    # ✅ FIX: write LIST ONLY to docs/data/items.json (what the dashboard JS expects)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    # ✅ Also write metadata payload to last_run.json (nice for debugging / display if you want)
    with open(LAST_RUN_PATH, "w", encoding="utf-8") as f:
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
        f"\n[ok] wrote {OUT_PATH} with {len(items)} items (list-only for dashboard JS)\n"
        f"[ok] wrote {LAST_RUN_PATH} (full payload)\n"
        f"[ok] wrote raw exports: {RAW_HTML_PATH}, {RAW_MD_PATH}, {RAW_TXT_PATH}, {RAW_NDJSON_PATH}\n"
        f"[ok] wrote print export: {PRINT_HTML_PATH}\n"
        f"[ok] wrote crawler hints: {RAW_ROBOTS_PATH}, {RAW_SITEMAP_PATH}\n"
        f"[ok] detail fetches: {global_detail_fetches}/{GLOBAL_DETAIL_FETCH_CAP}",
        flush=True,
    )


if __name__ == "__main__":
    build()
