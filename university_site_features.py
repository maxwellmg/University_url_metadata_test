"""
university_site_features.py
============================
Crawl a list of university websites and extract structural / metadata features
(no NLP) for downstream fraud-detection modeling.

Designed to run in a Jupyter notebook:

    from university_site_features import build_feature_dataframe
    df = build_feature_dataframe("university_urls.xlsx", url_column="url")

Or from the command line:

    python university_site_features.py university_urls.xlsx --url-column url

Responsible crawling built in:
  * respects robots.txt (skips disallowed paths; honors Crawl-delay)
  * rate-limited (default 1.5s between requests per site)
  * hard cap on pages per site (default 25)
  * identifies itself with a descriptive User-Agent
  * checkpoints per-site results to JSON so interrupted runs resume

Dependencies (pip install):
  required: requests beautifulsoup4 pandas lxml openpyxl
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
        "contact: <university url>)"
    ),
    "checkpoint_path": "site_features_checkpoint.json",
    "output_csv": "site_features.csv",
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
           "ssl_days_to_expiry": None}
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
# Polite crawler
# ---------------------------------------------------------------------------
class PoliteSession:
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
    rp = robotparser.RobotFileParser()
    robots_url = urljoin(base_url, "/robots.txt")
    try:
        resp = sess.get(robots_url)
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
        else:
            rp.parse([])  # no robots.txt -> everything allowed
    except Exception:
        rp.parse([])
    return rp


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
    rp = load_robots(start_url, sess)
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
    tmp.replace(path)


def _json_default(o):
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def _sanitize_for_json(d: dict) -> dict:
    return json.loads(json.dumps(d, default=_json_default))


def build_feature_dataframe(
    excel_path: str,
    url_column: str = "url",
    checkpoint_path: str | None = None,
    output_csv: str | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """
    Read URLs from an Excel file, crawl each site, and return a feature DataFrame.
    Resumes automatically from the checkpoint file if interrupted.
    """
    checkpoint_path = checkpoint_path or CONFIG["checkpoint_path"]
    output_csv = output_csv or CONFIG["output_csv"]

    urls_df = pd.read_excel(excel_path)
    if url_column not in urls_df.columns:
        raise KeyError(
            f"Column '{url_column}' not found. Available: {list(urls_df.columns)}"
        )
    urls = [normalize_url(u) for u in urls_df[url_column].dropna().astype(str)]
    if limit:
        urls = urls[:limit]

    done = _load_checkpoint(checkpoint_path)
    print(f"{len(urls)} URLs | {len(done)} already in checkpoint")

    for i, url in enumerate(urls, 1):
        if url in done:
            continue
        print(f"[{i}/{len(urls)}] crawling {url}")
        try:
            feats = crawl_site(url)
        except Exception as e:
            feats = {"input_url": url, "crawl_error": f"{type(e).__name__}: {e}"}
        done[url] = _sanitize_for_json(feats)
        _save_checkpoint(checkpoint_path, done)

    df = pd.DataFrame(list(done.values()))
    df = add_cross_site_features(df)
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
    ap = argparse.ArgumentParser(description="Extract structural website features for fraud modeling.")
    ap.add_argument("excel_path", help="Excel file containing university URLs")
    ap.add_argument("--url-column", default="url")
    ap.add_argument("--limit", type=int, default=None, help="only process first N URLs (for testing)")
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()
    if args.max_pages:
        CONFIG["max_pages_per_site"] = args.max_pages
    build_feature_dataframe(args.excel_path, url_column=args.url_column, limit=args.limit)
