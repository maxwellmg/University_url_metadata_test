"""
university_site_features.py
============================
Crawl a list of university websites and extract structural / metadata features
(no NLP) for downstream fraud-detection modeling.

Designed to run in a Jupyter notebook:

    from university_site_features import build_feature_dataframe
    df = build_feature_dataframe("university_urls.csv", url_column="school.school_url")

Or from the command line:

    python university_site_features.py university_urls.csv --url-column school.school_url

Responsible crawling built in:
  * respects robots.txt (skips disallowed paths; honors Crawl-delay)
  * rate-limited (default 1.5s between requests per site)
  * hard cap on pages per site (default 25)
  * identifies itself with a descriptive User-Agent
  * checkpoints per-site results to JSON so interrupted runs resume

Dependencies (pip install):
  required: requests beautifulsoup4 pandas lxml
  optional: tldextract python-whois   (features degrade gracefully if absent)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import ssl
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib import robotparser

import pandas as pd
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    import tldextract
    _HAS_TLDEXTRACT = True
except ImportError:
    _HAS_TLDEXTRACT = False

try:
    import whois as whois_mod  # package name: python-whois
    _HAS_WHOIS = True
except ImportError:
    _HAS_WHOIS = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG = {
    "max_pages_per_site": 25,        # BFS page cap per site
    "request_delay_sec": 1.5,        # minimum seconds between requests to a site
    "request_timeout_sec": 15,
    "max_css_files_per_site": 5,     # CSS files fetched for hashing
    "broken_link_sample_size": 10,   # internal links spot-checked for 404s
    "user_agent": (
        "UniversityStructureResearchBot/1.0 "
        "(academic research on institutional website structure; "
        "contact: <university email>.edu)"
    ),
    "checkpoint_path": "site_features_checkpoint.json",
    "output_csv": "site_features.csv",
    # --- sitemap / URL-inventory module ---
    "collect_sitemap": True,          # fetch sitemap.xml and derive inventory features
    "max_sitemap_files": 10,          # sub-sitemaps fetched per site (index files recurse)
    "max_sitemap_urls": 50_000,       # stop collecting URLs past this (sets sitemap_truncated)
    "save_url_inventory_dir": None,   # e.g. "url_inventories/" to save each site's URL list (.txt.gz)
}

# Domains treated as legitimacy-signaling outbound links
TRUSTED_LINK_DOMAINS = {
    "ed.gov", "studentaid.gov", "chea.org", "nces.ed.gov",
    "msche.org", "hlcommission.org", "sacscoc.org", "neche.org",
    "nwccu.org", "wscuc.org", "accjc.org", "accsc.org", "deac.org",
    "abet.org", "aacsb.edu", "lcme.org", "abaannualreport.com",
    "ncaa.org", "naia.org", "commonapp.org", "fafsa.gov",
}

CSS_FRAMEWORK_PATTERNS = {
    "bootstrap": re.compile(r"bootstrap", re.I),
    "tailwind": re.compile(r"tailwind", re.I),
    "foundation": re.compile(r"foundation(\.min)?\.css", re.I),
    "bulma": re.compile(r"bulma", re.I),
    "materialize": re.compile(r"materialize", re.I),
}

SITE_BUILDER_PATTERNS = {
    "wix": re.compile(r"wix\.com|wixstatic", re.I),
    "squarespace": re.compile(r"squarespace", re.I),
    "weebly": re.compile(r"weebly", re.I),
    "godaddy": re.compile(r"godaddy|websitebuilder", re.I),
    "wordpress": re.compile(r"wp-content|wordpress", re.I),
}

RE_GA_UA = re.compile(r"\bUA-\d{4,10}-\d{1,4}\b")
RE_GA4 = re.compile(r"\bG-[A-Z0-9]{6,14}\b")
RE_GTM = re.compile(r"\bGTM-[A-Z0-9]{4,10}\b")
RE_FB_PIXEL = re.compile(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{5,20})['\"]")
RE_PHONE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}")
RE_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")
RE_STREET = re.compile(
    r"\b\d{1,6}\s+\w[\w\s.]{0,40}?"
    r"(?:Street|St\.?|Avenue|Ave\.?|Boulevard|Blvd\.?|Road|Rd\.?|Drive|Dr\.?|"
    r"Lane|Ln\.?|Way|Court|Ct\.?|Place|Pl\.?|Circle|Parkway|Pkwy\.?)\b",
    re.I,
)
RE_COPYRIGHT_YEAR = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", re.I)
RE_FREE_EMAIL = re.compile(
    r"@(gmail|yahoo|hotmail|outlook|aol|protonmail|icloud|mail)\.(com|ru|net)", re.I
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def registered_domain(url: str) -> str:
    """Return the registrable domain (example.edu) for a URL."""
    host = urlparse(url).netloc.lower().split(":")[0]
    if _HAS_TLDEXTRACT:
        ext = tldextract.extract(host)
        return ".".join(p for p in (ext.domain, ext.suffix) if p)
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def get_tld(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    if _HAS_TLDEXTRACT:
        return tldextract.extract(host).suffix
    return host.rsplit(".", 1)[-1] if "." in host else ""


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def dom_structure_hash(soup: BeautifulSoup) -> str:
    """Hash of the tag-name sequence only (content ignored)."""
    tags = [t.name for t in soup.find_all(True)]
    return hashlib.sha1("|".join(tags).encode()).hexdigest()


def text_shingles(text: str, k: int = 5) -> set:
    """Hashed k-word shingles for boilerplate estimation (counting, not NLP)."""
    words = text.split()
    return {
        hashlib.md5(" ".join(words[i:i + k]).encode()).hexdigest()[:12]
        for i in range(len(words) - k + 1)
    }


# ---------------------------------------------------------------------------
# Domain-level (non-crawl) features
# ---------------------------------------------------------------------------
def ssl_features(hostname: str, timeout: int = 10) -> dict:
    out = {"ssl_ok": False, "ssl_issuer_org": None, "ssl_org_validated": None,
           "ssl_days_to_expiry": None, "ssl_cert_error": None}
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as tls:
                cert = tls.getpeercert()
        out["ssl_ok"] = True
        issuer = {k: v for pair in cert.get("issuer", ()) for k, v in pair}
        subject = {k: v for pair in cert.get("subject", ()) for k, v in pair}
        out["ssl_issuer_org"] = issuer.get("organizationName")
        # OV/EV certs carry an organizationName in the *subject*; DV certs don't
        out["ssl_org_validated"] = "organizationName" in subject
        exp = cert.get("notAfter")
        if exp:
            exp_dt = datetime.strptime(exp, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            out["ssl_days_to_expiry"] = (exp_dt - datetime.now(timezone.utc)).days
    except ssl.SSLCertVerificationError as e:
        out["ssl_cert_error"] = str(e)
    except ssl.SSLError as e:
        out["ssl_cert_error"] = str(e)
    except Exception:
        pass
    return out


def whois_features(domain: str) -> dict:
    out = {"domain_age_days": None, "whois_privacy_protected": None,
           "whois_registrant_org": None}
    if not _HAS_WHOIS:
        return out
    try:
        w = whois_mod.whois(domain)
        created = w.creation_date
        if isinstance(created, list):
            created = min(d for d in created if d is not None)
        if isinstance(created, datetime):
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            out["domain_age_days"] = (datetime.now(timezone.utc) - created).days
        org = getattr(w, "org", None) or getattr(w, "registrant_org", None)
        out["whois_registrant_org"] = org
        blob = str(w.text if hasattr(w, "text") else w).lower()
        out["whois_privacy_protected"] = any(
            s in blob for s in ("privacy", "redacted", "proxy", "whoisguard")
        )
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Per-page feature extraction
# ---------------------------------------------------------------------------
def extract_page_features(html: str, page_url: str, site_domain: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.extract()
    visible_text = soup.get_text(" ", strip=True)

    # Re-parse with scripts for analytics/pixel detection (raw source scan)
    a_tags = BeautifulSoup(html, "lxml").find_all("a", href=True)

    internal_links, external_links, pdf_links = [], [], 0
    trusted_links, mailto_domains = 0, []
    subdomains = set()
    for a in a_tags:
        href = a["href"].strip()
        if href.startswith("mailto:"):
            addr = href[7:].split("?")[0]
            if "@" in addr:
                mailto_domains.append(addr.split("@", 1)[1].lower())
            continue
        if href.startswith(("javascript:", "#", "tel:")):
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        link_dom = registered_domain(absolute)
        if absolute.lower().split("?")[0].endswith(".pdf"):
            pdf_links += 1
        if link_dom == site_domain:
            internal_links.append(absolute)
            host = parsed.netloc.lower().split(":")[0]
            subdomains.add(host)
        else:
            external_links.append(absolute)
            if link_dom in TRUSTED_LINK_DOMAINS or link_dom.endswith(".gov"):
                trusted_links += 1

    imgs = soup.find_all("img")
    imgs_with_alt = sum(1 for i in imgs if (i.get("alt") or "").strip())

    full_soup = BeautifulSoup(html, "lxml")
    stylesheets = [
        l.get("href", "") for l in full_soup.find_all("link", rel=lambda v: v and "stylesheet" in v)
    ]
    frameworks = {
        name for name, pat in CSS_FRAMEWORK_PATTERNS.items()
        if any(pat.search(s) for s in stylesheets) or pat.search(html[:200000])
    }
    builders = {name for name, pat in SITE_BUILDER_PATTERNS.items() if pat.search(html[:200000])}

    gen = full_soup.find("meta", attrs={"name": re.compile("^generator$", re.I)})
    ldjson = full_soup.find_all("script", type="application/ld+json")
    has_edu_schema = any(
        "CollegeOrUniversity" in (s.string or "") or "EducationalOrganization" in (s.string or "")
        for s in ldjson
    ) or bool(full_soup.find(attrs={"itemtype": re.compile("CollegeOrUniversity|EducationalOrganization")}))

    copyright_years = [int(y) for y in RE_COPYRIGHT_YEAR.findall(visible_text)]

    return {
        "url": page_url,
        "structure_hash": dom_structure_hash(full_soup),
        "shingles": text_shingles(visible_text),
        "word_count": len(visible_text.split()),
        "a_tag_count": len(a_tags),
        "internal_links": internal_links,
        "external_link_count": len(external_links),
        "trusted_link_count": trusted_links,
        "pdf_link_count": pdf_links,
        "mailto_domains": mailto_domains,
        "subdomains": subdomains,
        "img_count": len(imgs),
        "img_alt_count": imgs_with_alt,
        "stylesheet_hrefs": [urljoin(page_url, s) for s in stylesheets if s],
        "inline_style_count": len(full_soup.find_all(style=True)),
        "css_frameworks": frameworks,
        "site_builders": builders,
        "meta_generator": (gen.get("content") if gen else None),
        "has_meta_description": bool(full_soup.find("meta", attrs={"name": re.compile("^description$", re.I)})),
        "has_og_tags": bool(full_soup.find("meta", attrs={"property": re.compile("^og:", re.I)})),
        "has_canonical": bool(full_soup.find("link", rel=lambda v: v and "canonical" in v)),
        "has_favicon": bool(full_soup.find("link", rel=lambda v: v and "icon" in " ".join(v).lower() if isinstance(v, list) else v and "icon" in v.lower())),
        "has_edu_schema": has_edu_schema,
        "ga_ua_ids": set(RE_GA_UA.findall(html)),
        "ga4_ids": set(RE_GA4.findall(html)),
        "gtm_ids": set(RE_GTM.findall(html)),
        "fb_pixel_ids": set(RE_FB_PIXEL.findall(html)),
        "phone_count": len(RE_PHONE.findall(visible_text)),
        "has_street_address": bool(RE_STREET.search(visible_text)) and bool(RE_ZIP.search(visible_text)),
        "free_email_hits": len(RE_FREE_EMAIL.findall(html)),
        "copyright_years": copyright_years,
        "html_bytes": len(html.encode("utf-8", errors="ignore")),
        "text_bytes": len(visible_text.encode("utf-8", errors="ignore")),
    }


# ---------------------------------------------------------------------------
# Sitemap / URL-inventory module
# ---------------------------------------------------------------------------
RE_SITEMAP_DIRECTIVE = re.compile(r"(?im)^\s*sitemap:\s*(\S+)")
RE_SM_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
RE_SM_LASTMOD = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.I | re.S)

# Path substrings signalling real institutional anatomy
EXPECTED_SECTIONS = [
    "admission", "registrar", "faculty", "catalog", "financial",
    "library", "research", "athletic", "alumni", "tuition",
]


def parse_sitemap_document(doc: str) -> tuple[list, list, bool]:
    """Return (loc URLs, lastmod strings, is_index) from sitemap XML text."""
    locs = RE_SM_LOC.findall(doc)
    lastmods = RE_SM_LASTMOD.findall(doc)
    is_index = "<sitemapindex" in doc.lower()
    return locs, lastmods, is_index


def _fetch_sitemap_doc(url: str, sess: PoliteSession):
    try:
        r = sess.get(url)
        if r.status_code != 200:
            return None
        content = r.content
        if url.lower().endswith(".gz") or content[:2] == b"\x1f\x8b":
            import gzip
            content = gzip.decompress(content)
        return content.decode("utf-8", errors="ignore")
    except Exception:
        return None


def collect_sitemap_inventory(base_url: str, robots_text: str, sess: PoliteSession) -> tuple[list, list, dict]:
    """
    Discover sitemaps (robots.txt Sitemap: directives, else default paths),
    recurse through index files, and return (urls, lastmods, meta).
    A handful of extra requests per site — pages are never fetched.
    """
    meta = {"sitemap_found": False, "sitemap_source": None,
            "sitemap_files_fetched": 0, "sitemap_truncated": False}
    candidates = RE_SITEMAP_DIRECTIVE.findall(robots_text)
    source = "robots" if candidates else "default_path"
    if not candidates:
        candidates = [urljoin(base_url, "/sitemap.xml"),
                      urljoin(base_url, "/sitemap_index.xml")]

    urls, lastmods = [], []
    queue, seen = deque(candidates), set()
    while queue and meta["sitemap_files_fetched"] < CONFIG["max_sitemap_files"] \
            and len(urls) < CONFIG["max_sitemap_urls"]:
        sm_url = queue.popleft()
        if sm_url in seen:
            continue
        seen.add(sm_url)
        doc = _fetch_sitemap_doc(sm_url, sess)
        meta["sitemap_files_fetched"] += 1
        if not doc:
            continue
        locs, mods, is_index = parse_sitemap_document(doc)
        if not locs:
            continue
        meta["sitemap_found"] = True
        meta["sitemap_source"] = source
        if is_index:
            queue.extend(locs)          # locs are sub-sitemap URLs
        else:
            urls.extend(locs)
            lastmods.extend(mods)       # only page-level lastmods, not index-level

    if len(urls) > CONFIG["max_sitemap_urls"]:
        urls = urls[: CONFIG["max_sitemap_urls"]]
        meta["sitemap_truncated"] = True
    if queue and meta["sitemap_files_fetched"] >= CONFIG["max_sitemap_files"]:
        meta["sitemap_truncated"] = True
    return urls, lastmods, meta


def sitemap_features_from_urls(urls: list, lastmods: list, site_domain: str) -> dict:
    """Derive inventory features from the URL list alone (no page fetches)."""
    same = [u for u in urls if registered_domain(u) == site_domain]
    out = {"sitemap_url_count": len(same)}
    if not same:
        return out

    paths = [urlparse(u).path for u in same]
    depths = [len([seg for seg in p.split("/") if seg]) for p in paths]
    top_dirs = {p.split("/")[1].lower() for p in paths
                if len(p.split("/")) > 1 and p.split("/")[1]}
    lowered = [u.lower() for u in same]

    out.update({
        "sitemap_max_depth": max(depths),
        "sitemap_avg_depth": sum(depths) / len(depths),
        "sitemap_n_top_dirs": len(top_dirs),
        "sitemap_pct_query_urls": sum("?" in u for u in same) / len(same),
        "sitemap_pdf_count": sum(u.split("?")[0].endswith(".pdf") for u in lowered),
        "sitemap_expected_sections": sorted(
            {s for s in EXPECTED_SECTIONS if any(s in u for u in lowered)}
        ),
    })
    out["sitemap_expected_section_count"] = len(out["sitemap_expected_sections"])

    # lastmod -> maintenance cadence
    dates = []
    for m in lastmods:
        try:
            dates.append(datetime.strptime(m.strip()[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    out["sitemap_pct_urls_with_lastmod"] = len(dates) / len(same)
    if dates:
        now = datetime.now(timezone.utc)
        out["sitemap_days_since_last_update"] = (now - max(dates)).days
        out["sitemap_lastmod_span_days"] = (max(dates) - min(dates)).days
    return out


def _save_url_inventory(site_domain: str, urls: list) -> None:
    """Optionally persist the discrete URL list per site (gzipped, one URL/line)."""
    out_dir = CONFIG.get("save_url_inventory_dir")
    if not out_dir or not urls:
        return
    import gzip
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    fp = Path(out_dir) / f"{site_domain.replace('.', '_')}.txt.gz"
    with gzip.open(fp, "wt", encoding="utf-8") as f:
        f.write("\n".join(urls))



    def __init__(self, delay: float):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": CONFIG["user_agent"]})
        self.delay = delay
        self._last_request = 0.0

    def _wait(self):
        elapsed = time.time() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.time()

    def get(self, url: str, **kw):
        self._wait()
        return self.session.get(url, timeout=CONFIG["request_timeout_sec"],
                                allow_redirects=True, **kw)

    def head(self, url: str, **kw):
        self._wait()
        return self.session.head(url, timeout=CONFIG["request_timeout_sec"],
                                 allow_redirects=True, **kw)


def load_robots(base_url: str, sess: PoliteSession):
    """Fetch robots.txt; return (parser, raw_text) — the text carries Sitemap: directives."""
    rp = robotparser.RobotFileParser()
    robots_text = ""
    try:
        resp = sess.get(urljoin(base_url, "/robots.txt"))
        if resp.status_code == 200:
            robots_text = resp.text
    except Exception:
        pass
    rp.parse(robots_text.splitlines())
    return rp, robots_text

def crawl_site(start_url: str) -> dict:
    """Crawl one site (BFS, capped) and return aggregated features."""
    start_url = normalize_url(start_url)
    site_domain = registered_domain(start_url)
    hostname = urlparse(start_url).netloc.split(":")[0]

    result = {
        "input_url": start_url,
        "registered_domain": site_domain,
        "tld": get_tld(start_url),
        "crawl_timestamp": datetime.now(timezone.utc).isoformat(),
        "crawl_error": None,
    }

    sess = PoliteSession(CONFIG["request_delay_sec"])
    rp, robots_text = load_robots(start_url, sess)
    crawl_delay = rp.crawl_delay(CONFIG["user_agent"])
    if crawl_delay:
        sess.delay = max(sess.delay, float(crawl_delay))

    pages, response_headers = [], []
    visited, queue = set(), deque([start_url])
    final_landing_url = None
    robots_blocked = 0

    while queue and len(pages) < CONFIG["max_pages_per_site"]:
        url = queue.popleft()
        canon = url.split("#")[0].rstrip("/")
        if canon in visited:
            continue
        visited.add(canon)

        if not rp.can_fetch(CONFIG["user_agent"], url):
            robots_blocked += 1
            continue
        try:
            resp = sess.get(url)
        except Exception as e:
            if not pages:  # landing page itself failed
                result["crawl_error"] = f"{type(e).__name__}: {e}"
            continue
        if final_landing_url is None:
            final_landing_url = resp.url
            result["https_redirect"] = resp.url.startswith("https://")
            result["landing_status_code"] = resp.status_code
        if resp.status_code != 200 or "text/html" not in resp.headers.get("Content-Type", ""):
            continue

        response_headers.append(dict(resp.headers))
        try:
            feats = extract_page_features(resp.text, resp.url, site_domain)
        except Exception:
            continue
        pages.append(feats)

        for link in feats["internal_links"]:
            c = link.split("#")[0].rstrip("/")
            if c not in visited and len(visited) + len(queue) < CONFIG["max_pages_per_site"] * 8:
                queue.append(link)

    result["pages_crawled"] = len(pages)
    result["robots_blocked_count"] = robots_blocked
    if not pages:
        if not result.get("crawl_error"):
            result["crawl_error"] = "no HTML pages retrieved"
        return result

    result.update(aggregate_site_features(pages, site_domain, response_headers))

    # Broken internal link spot check
    all_internal = {l.split("#")[0].rstrip("/") for p in pages for l in p["internal_links"]}
    unvisited = list(all_internal - visited)[: CONFIG["broken_link_sample_size"]]
    broken = 0
    for link in unvisited:
        try:
            r = sess.head(link)
            if r.status_code == 405:
                r = sess.get(link)
            if r.status_code >= 400:
                broken += 1
        except Exception:
            broken += 1
    result["broken_link_sample_n"] = len(unvisited)
    result["broken_link_rate_sampled"] = broken / len(unvisited) if unvisited else None

    # CSS content hashes (for cross-site template reuse detection)
    css_hrefs = []
    for p in pages:
        css_hrefs.extend(p["stylesheet_hrefs"])
    css_hashes = []
    for href in list(dict.fromkeys(css_hrefs))[: CONFIG["max_css_files_per_site"]]:
        try:
            r = sess.get(href)
            if r.status_code == 200:
                css_hashes.append(hashlib.sha1(r.content).hexdigest())
        except Exception:
            continue
    result["css_content_hashes"] = css_hashes

    # Sitemap / URL inventory (a few extra requests; no page fetches)
    if CONFIG.get("collect_sitemap", True):
        try:
            sm_urls, sm_lastmods, sm_meta = collect_sitemap_inventory(start_url, robots_text, sess)
            result.update(sm_meta)
            result.update(sitemap_features_from_urls(sm_urls, sm_lastmods, site_domain))
            _save_url_inventory(site_domain, sm_urls)
        except Exception:
            result["sitemap_found"] = False
        # crawl truncated by the page cap while the site is actually larger
        result["crawl_censored"] = bool(
            len(pages) >= CONFIG["max_pages_per_site"]
            and result.get("sitemap_url_count", 0) > CONFIG["max_pages_per_site"]
        )

    # Domain-level lookups
    result.update(ssl_features(hostname))
    result.update(whois_features(site_domain))
    return result


# ---------------------------------------------------------------------------
# Site-level aggregation
# ---------------------------------------------------------------------------
def aggregate_site_features(pages: list, site_domain: str, headers: list) -> dict:
    n = len(pages)
    total_words = sum(p["word_count"] for p in pages)
    total_imgs = sum(p["img_count"] for p in pages)
    total_alt = sum(p["img_alt_count"] for p in pages)
    total_html = sum(p["html_bytes"] for p in pages)
    total_text = sum(p["text_bytes"] for p in pages)

    # Boilerplate: fraction of landing-page shingles present on >=50% of pages
    boilerplate_ratio = None
    if n >= 3:
        counts = Counter()
        for p in pages:
            counts.update(p["shingles"])
        landing = pages[0]["shingles"]
        if landing:
            common = sum(1 for s in landing if counts[s] >= max(2, n // 2))
            boilerplate_ratio = common / len(landing)

    structure_hashes = [p["structure_hash"] for p in pages]
    unique_structures = len(set(structure_hashes))

    mailto = [d for p in pages for d in p["mailto_domains"]]
    mailto_on_domain = sum(1 for d in mailto if registered_domain("http://" + d) == site_domain)

    all_years = [y for p in pages for y in p["copyright_years"] if 1990 <= y <= 2100]
    frameworks = set().union(*(p["css_frameworks"] for p in pages))
    builders = set().union(*(p["site_builders"] for p in pages))
    subdomains = set().union(*(p["subdomains"] for p in pages))
    generators = {p["meta_generator"] for p in pages if p["meta_generator"]}

    server_headers = {h.get("Server") for h in headers if h.get("Server")}
    last_modified = [h.get("Last-Modified") for h in headers if h.get("Last-Modified")]

    return {
        # ---- volume / size ----
        "total_a_tags": sum(p["a_tag_count"] for p in pages),
        "total_internal_links": sum(len(p["internal_links"]) for p in pages),
        "total_external_links": sum(p["external_link_count"] for p in pages),
        "total_words": total_words,
        "avg_words_per_page": total_words / n,
        "total_images": total_imgs,
        "avg_images_per_page": total_imgs / n,
        "img_alt_coverage": total_alt / total_imgs if total_imgs else None,
        "total_html_bytes": total_html,
        "text_to_html_ratio": total_text / total_html if total_html else None,
        # ---- structure / template ----
        "unique_dom_structures": unique_structures,
        "dom_structure_diversity": unique_structures / n,
        "structure_hashes": structure_hashes,
        "boilerplate_ratio": boilerplate_ratio,
        "avg_inline_styles_per_page": sum(p["inline_style_count"] for p in pages) / n,
        "num_stylesheets_landing": len(pages[0]["stylesheet_hrefs"]),
        "css_frameworks": sorted(frameworks),
        "uses_css_framework": bool(frameworks),
        "site_builders_detected": sorted(builders),
        "uses_site_builder": bool(builders - {"wordpress"}),  # WP alone is weak signal
        # ---- metadata ----
        "meta_generators": sorted(generators),
        "pct_pages_meta_description": sum(p["has_meta_description"] for p in pages) / n,
        "pct_pages_og_tags": sum(p["has_og_tags"] for p in pages) / n,
        "pct_pages_canonical": sum(p["has_canonical"] for p in pages) / n,
        "has_favicon": any(p["has_favicon"] for p in pages),
        "has_edu_schema_markup": any(p["has_edu_schema"] for p in pages),
        "server_headers": sorted(server_headers),
        "has_last_modified_header": bool(last_modified),
        # ---- analytics / tracking (also cross-site clustering keys) ----
        "ga_ua_ids": sorted(set().union(*(p["ga_ua_ids"] for p in pages))),
        "ga4_ids": sorted(set().union(*(p["ga4_ids"] for p in pages))),
        "gtm_ids": sorted(set().union(*(p["gtm_ids"] for p in pages))),
        "fb_pixel_ids": sorted(set().union(*(p["fb_pixel_ids"] for p in pages))),
        # ---- legitimacy signals ----
        "pdf_link_count": sum(p["pdf_link_count"] for p in pages),
        "trusted_outbound_links": sum(p["trusted_link_count"] for p in pages),
        "num_subdomains_seen": len(subdomains),
        "mailto_count": len(mailto),
        "mailto_on_domain_ratio": mailto_on_domain / len(mailto) if mailto else None,
        "free_email_hits": sum(p["free_email_hits"] for p in pages),
        "phone_number_hits": sum(p["phone_count"] for p in pages),
        "has_street_address": any(p["has_street_address"] for p in pages),
        "max_copyright_year": max(all_years) if all_years else None,
        "min_copyright_year": min(all_years) if all_years else None,
    }


# ---------------------------------------------------------------------------
# Checkpointing + pipeline
# ---------------------------------------------------------------------------
def _load_checkpoint(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            # corrupt checkpoint -> back it up rather than crash
            p.rename(p.with_suffix(".corrupt.json"))
    return {}


def _save_checkpoint(path: str, data: dict):
    """Atomic write: temp file + rename, so a crash never corrupts the checkpoint."""
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=_json_default))
    try:
        tmp.replace(path)
    except PermissionError as e:
        # Some environments may lock or deny overwriting the checkpoint file.
        try:
            if Path(path).exists():
                Path(path).unlink()
            tmp.rename(path)
        except Exception as fallback_e:
            raise PermissionError(
                f"Unable to save checkpoint {path} from temp file {tmp}: {e}. "
                f"Fallback rename also failed: {fallback_e}. "
                "Check file permissions and locks."
            ) from fallback_e


def _json_default(o):
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def _sanitize_for_json(d: dict) -> dict:
    return json.loads(json.dumps(d, default=_json_default))


# ---------------------------------------------------------------------------
# INPUT SECTION: read the CSV file into a pandas DataFrame and keep the
# two columns that flow through to the final output:
#   * UNITID           — your unique institution key (join key for labels)
#   * school.school_url — the URL to crawl
# ---------------------------------------------------------------------------
def load_university_urls(
    csv_path: str,
    url_column: str = "school.school_url",
    id_column: str = "UNITID",
) -> pd.DataFrame:
    """Read the input CSV and return [id_column, url_column, normalized_url]."""
    # dtype=str on the ID column preserves leading zeros and institution identifiers.
    input_df = pd.read_csv(csv_path, dtype={id_column: str})
    missing = [c for c in (id_column, url_column) if c not in input_df.columns]
    if missing:
        raise KeyError(
            f"Column(s) {missing} not found in {csv_path}. "
            f"Available: {list(input_df.columns)}"
        )
    out = input_df[[id_column, url_column]].copy()
    out = out.dropna(subset=[url_column])
    out["normalized_url"] = out[url_column].astype(str).map(normalize_url)
    n_dupes = out["normalized_url"].duplicated().sum()
    if n_dupes:
        print(f"note: {n_dupes} rows share a URL with another UNITID; "
              "each URL is crawled once and features are joined back to every row.")
    return out


def build_feature_dataframe(
    csv_path: str,
    url_column: str = "school.school_url",
    id_column: str = "UNITID",
    checkpoint_path: str | None = None,
    output_csv: str | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """
    Read institutions from a CSV file, crawl each unique website once, and
    return a DataFrame of [UNITID, school.school_url, <features...>].
    Resumes automatically from the checkpoint file if interrupted.
    """
    checkpoint_path = checkpoint_path or CONFIG["checkpoint_path"]
    output_csv = output_csv or CONFIG["output_csv"]

    input_df = load_university_urls(csv_path, url_column=url_column, id_column=id_column)
    unique_urls = input_df["normalized_url"].drop_duplicates().tolist()
    if limit:
        unique_urls = unique_urls[:limit]

    done = _load_checkpoint(checkpoint_path)
    print(f"{len(input_df)} input rows | {len(unique_urls)} unique URLs to crawl | "
          f"{len(done)} already in checkpoint")

    try:
        for i, url in enumerate(unique_urls, 1):
            if url in done:
                continue
            print(f"[{i}/{len(unique_urls)}] crawling {url}")
            try:
                feats = crawl_site(url)
            except Exception as e:
                feats = {"input_url": url, "crawl_error": f"{type(e).__name__}: {e}"}
            done[url] = _sanitize_for_json(feats)
            _save_checkpoint(checkpoint_path, done)
    except KeyboardInterrupt:
        print("Interrupted by user; saving checkpoint before exit.")
        _save_checkpoint(checkpoint_path, done)
        raise
    except Exception as exc:
        print(f"Unexpected error encountered: {exc}. Saving checkpoint before exiting.")
        _save_checkpoint(checkpoint_path, done)
        raise

    feats_df = pd.DataFrame(list(done.values()))
    feats_df = add_cross_site_features(feats_df)

    # Join features back onto the input rows so UNITID and the original
    # website string are the leading columns of the final DataFrame.
    df = input_df.merge(feats_df, left_on="normalized_url", right_on="input_url", how="left")
    if limit:
        df = df[df["input_url"].notna()].reset_index(drop=True)
    df = df.drop(columns=["normalized_url"])

    df.to_csv(output_csv, index=False)
    print(f"Saved {len(df)} rows -> {output_csv}")
    return df


# ---------------------------------------------------------------------------
# Cross-site features (template / tracker reuse across the whole URL list)
# ---------------------------------------------------------------------------
def add_cross_site_features(df: pd.DataFrame) -> pd.DataFrame:
    """Flag sites sharing DOM templates, CSS files, or analytics IDs."""
    df = df.copy()

    def as_set(v):
        if isinstance(v, (list, set)):
            return set(v)
        return set()

    struct_sets = [as_set(v) for v in df.get("structure_hashes", pd.Series([[]] * len(df)))]
    css_sets = [as_set(v) for v in df.get("css_content_hashes", pd.Series([[]] * len(df)))]
    tracker_sets = []
    for _, row in df.iterrows():
        t = set()
        for col in ("ga_ua_ids", "ga4_ids", "gtm_ids", "fb_pixel_ids"):
            t |= as_set(row.get(col))
        tracker_sets.append(t)

    n = len(df)
    max_struct_sim = [0.0] * n
    shared_css = [0] * n
    shared_trackers = [0] * n
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            a, b = struct_sets[i], struct_sets[j]
            if a and b:
                jac = len(a & b) / len(a | b)
                max_struct_sim[i] = max(max_struct_sim[i], jac)
            if css_sets[i] & css_sets[j]:
                shared_css[i] += 1
            if tracker_sets[i] & tracker_sets[j]:
                shared_trackers[i] += 1

    df["max_structure_similarity_other_site"] = max_struct_sim
    df["n_sites_sharing_css_file"] = shared_css
    df["n_sites_sharing_tracker_id"] = shared_trackers
    return df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Command-line interface only — ignored when importing in Jupyter.
    ap = argparse.ArgumentParser(description="Extract structural website features for fraud modeling.")
    ap.add_argument("csv_path", help="CSV file with UNITID and school.school_url columns")
    ap.add_argument("--url-column", default="school.school_url")
    ap.add_argument("--id-column", default="UNITID")
    ap.add_argument("--limit", type=int, default=None, help="only process first N URLs (for testing)")
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()
    if args.max_pages:
        CONFIG["max_pages_per_site"] = args.max_pages
    build_feature_dataframe(args.csv_path, url_column=args.url_column,
                            id_column=args.id_column, limit=args.limit)
