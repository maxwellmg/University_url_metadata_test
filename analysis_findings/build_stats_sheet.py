#!/usr/bin/env python3
"""
build_stats_sheet.py
====================
Build the fraud-screening stats sheet from the crawler's ``site_features.csv``.

Works with:
  * the CURRENT home-only output (legacy column names such as ``word_count``,
    ``structure_hash``; known gaps are detected and labelled, not reported as zeros)
  * multi-page output (``total_words``, ``structure_hashes``, ...)
  * the v2 output described in SCRIPT_CHANGE_SPEC.md (``error_category``,
    ``final_url``, ``phone_numbers``, ...). New columns switch on extra stats and
    red flags automatically; nothing needs to be edited here.

Optional inputs, joined on UNITID:
  * --institutions : a College Scorecard (or IPEDS) extract. Adds peer groups
    (ownership x predominant degree), enrollment, institution names, OPEID6 for
    "same owner" exclusions, and known-problem labels (HCM2, not operating).
    Column names are auto-detected (API-style ``school.name`` or CSV-style
    ``INSTNM``); override with --map field=column.
  * --known-bad : a CSV with a UNITID column (optional ``label`` column) listing
    institutions already known to be problematic, for validation.

Outputs (in --out-dir, default ``stats_output/``):
  stats_sheet.xlsx              every table below as a tab (if openpyxl is installed)
  summary.csv                   one row per headline statistic, in ranked order
  institution_flags.csv         one row per UNITID: availability, metrics, flags, score, tier
  deep_dive_candidates.csv      crawlable candidates (UNITID + URL) -> feed to the crawler
  verify_status_candidates.csv  unreachable sites to verify by other means
  availability.csv              counts by failure category
  validation.csv                flag lift and score AUC vs known-problem labels (if labels)
  clusters_<key>.csv            cross-site clusters (trackers, templates, ...)
  cross_site_edges.csv          domain-to-domain links, for network-graph tools
  excluded_shared_keys.csv      keys shared too widely to count (vendors, platforms)
  shared_urls.csv               several UNITIDs on one website
  distributions.csv             numeric summaries overall and by ownership
  crosstab_<name>.csv           category x ownership tables
  top_values.csv                most common builders, issuers, registrants, ...
  coverage.csv                  per-field availability and known caveats
  run_info.csv                  schema detected, row counts, warnings
  charts/*.png                  charts (if matplotlib is installed)

Command line:
  python build_stats_sheet.py site_features.csv \
      --institutions scorecard_extract.csv --known-bad known_bad.csv

Jupyter:
  from build_stats_sheet import build_stats
  res = build_stats("site_features.csv", institutions="scorecard_extract.csv")
  res["summary"]
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd

SCRIPT_VERSION = "1.0.0"

DEFAULTS = {
    "high_threshold": 3,            # red-flag score >= this -> High tier
    "medium_threshold": 2,          # red-flag score >= this -> Medium tier
    "young_domain_years": 3.0,      # domain younger than this -> flag
    "stale_copyright_years": 3,     # crawl year - copyright year >= this -> flag
    "domain_expiry_days": 60,       # domain expires within this many days -> flag
    "thin_percentile": 5.0,         # bottom X% of words-per-page within peer group -> flag
    "min_peer_n": 30,               # smallest peer group used before falling back
    "vendor_threshold": 25,         # a key shared by more domains than this is excluded
    "near_dup_bits": 3,             # SimHash Hamming distance for near-duplicate text
    "template_min_words": 50,       # pages smaller than this are not template-matched
    "anomaly_threshold": 2.0,       # mean robust-z deficit >= this -> "anomalous vs peers"
    "enrollment_residual_z": -2.0,  # words-vs-enrollment residual below this -> outlier
}

# --------------------------------------------------------------------------
# Column vocabularies
# --------------------------------------------------------------------------
LEGACY_ALIASES = {  # current home-only name -> canonical (multi-page / v2) name
    "word_count": "total_words",
    "a_tag_count": "total_a_tags",
    "internal_link_count": "total_internal_links",
    "external_link_count": "total_external_links",
    "trusted_link_count": "trusted_outbound_links",
    "img_count": "total_images",
    "html_bytes": "total_html_bytes",
}

LIST_COLUMNS = [
    "structure_hashes", "css_content_hashes", "css_frameworks", "site_builders_detected",
    "meta_generators", "server_headers", "ga_ua_ids", "ga4_ids", "gtm_ids", "fb_pixel_ids",
    "sitemap_expected_sections",
    # v2 (SCRIPT_CHANGE_SPEC.md)
    "redirect_status_codes", "fetch_attempts", "dns_resolved_ips", "nameservers",
    "whois_name_servers", "ssl_san_domains", "ssl_san_other_registered_domains",
    "phone_numbers", "contact_emails", "mailto_domains", "free_email_addresses",
    "street_addresses", "zip_codes", "external_domains", "trusted_link_domains",
    "accreditor_domains_linked", "recognized_accreditor_domains_linked",
    "accredit_keyword_domains", "unrecognized_accredit_domains", "accreditors_named",
    "stylesheet_hrefs", "third_party_script_domains", "social_platforms_linked",
    "social_profile_urls", "image_phashes", "operator_link_types",
]

BOOL_COLUMNS = [
    "ssl_bypass_used", "https_redirect", "has_favicon", "has_edu_schema_markup",
    "has_last_modified_header", "has_street_address", "uses_css_framework",
    "uses_site_builder", "ssl_ok", "ssl_org_validated", "whois_privacy_protected",
    "sitemap_found", "crawl_censored",
    # v2
    "ssl_verified", "redirected_off_domain", "http_fallback_used", "www_fallback_used",
    "https_available", "dns_resolves", "dns_resolves_www", "has_mx_record",
    "parking_nameserver_detected", "parked_domain_detected", "placeholder_page_detected",
    "default_server_page_detected", "bot_protection_detected", "has_hsts", "has_csp",
    "has_net_price_calculator_link", "has_consumer_info_link", "has_security_report_link",
    "has_title_ix_link", "has_accreditation_page", "has_privacy_policy_link",
    "has_tuition_page", "has_catalog_link", "has_faculty_directory",
    "has_admissions_page", "has_financial_aid_page",
]

NUMERIC_COLUMNS = [
    "pages_crawled", "landing_status_code", "total_words", "total_a_tags",
    "total_internal_links", "total_external_links", "trusted_outbound_links",
    "total_images", "total_html_bytes", "avg_words_per_page", "avg_images_per_page",
    "img_alt_coverage", "text_to_html_ratio", "pdf_link_count", "num_subdomains_seen",
    "mailto_count", "mailto_on_domain_ratio", "free_email_hits", "phone_number_hits",
    "max_copyright_year", "min_copyright_year", "pct_pages_meta_description",
    "pct_pages_og_tags", "pct_pages_canonical", "ssl_days_to_expiry", "domain_age_days",
    "broken_link_rate_sampled", "sitemap_url_count", "sitemap_expected_section_count",
    # v2
    "redirect_hops", "domain_days_to_expiry", "days_since_whois_update",
    "ssl_cert_age_days", "ssl_validity_days", "ssl_san_count", "response_time_ms",
    "last_modified_age_days", "content_length_bytes", "required_disclosures_found",
    "stock_image_markers",
]

TRACKER_COLUMNS = ["ga_ua_ids", "ga4_ids", "gtm_ids", "fb_pixel_ids"]

ERROR_CATEGORIES = {
    "ok", "dns_failure", "connect_timeout", "read_timeout", "connection_refused",
    "connection_reset", "network_unreachable", "ssl_error", "too_many_redirects",
    "invalid_url", "transfer_error", "http_401", "http_403", "http_404", "http_410",
    "http_429", "http_503", "http_5xx", "http_4xx_other", "http_3xx_unresolved",
    "non_html", "extraction_failed", "other",
}

GROUP_OF = {
    "ok": "reachable", "non_html": "reachable", "extraction_failed": "reachable",
    "http_401": "blocked", "http_403": "blocked", "http_429": "blocked", "http_503": "blocked",
    "http_404": "broken_http", "http_410": "broken_http", "http_5xx": "broken_http",
    "http_4xx_other": "broken_http", "http_3xx_unresolved": "broken_http",
    "too_many_redirects": "broken_http",
    "connection_refused": "unreachable_server", "connect_timeout": "unreachable_server",
    "read_timeout": "unreachable_server", "connection_reset": "unreachable_server",
    "network_unreachable": "unreachable_server", "transfer_error": "unreachable_server",
    "dns_failure": "dns_dead", "ssl_error": "tls_failure", "invalid_url": "bad_url",
    "other": "unknown",
}

GROUP_LABELS = {
    "reachable": "Reachable",
    "blocked": "Alive, blocks automated access",
    "broken_http": "Server returns an error",
    "unreachable_server": "Server unreachable or timed out",
    "dns_dead": "Domain does not resolve",
    "tls_failure": "TLS/SSL handshake failure",
    "bad_url": "Malformed URL on record",
    "unknown": "Unclassified error",
}
GROUP_ORDER = list(GROUP_LABELS)
BROKEN_GROUPS = {"broken_http", "unreachable_server", "dns_dead", "tls_failure", "bad_url"}

CATEGORY_LABELS = {
    "ok": "OK (200, HTML)", "non_html": "200 but not HTML", "extraction_failed": "HTML could not be parsed",
    "http_401": "401 Unauthorized", "http_403": "403 Forbidden", "http_429": "429 Too Many Requests",
    "http_503": "503 Service Unavailable", "http_404": "404 Not Found", "http_410": "410 Gone",
    "http_5xx": "Other 5xx server error", "http_4xx_other": "Other 4xx error",
    "http_3xx_unresolved": "Unresolved redirect (3xx)", "too_many_redirects": "Redirect loop",
    "connection_refused": "Connection refused", "connect_timeout": "Connection timed out",
    "read_timeout": "Response timed out", "connection_reset": "Connection reset / dropped",
    "network_unreachable": "Network unreachable", "transfer_error": "Transfer error",
    "dns_failure": "Domain name does not resolve (DNS)", "ssl_error": "TLS/SSL failure",
    "invalid_url": "Malformed URL", "other": "Other / unrecognized error",
}

OWNERSHIP_LABELS = {1: "Public", 2: "Private nonprofit", 3: "Private for-profit"}
PREDDEG_LABELS = {0: "Not classified", 1: "Certificate", 2: "Associate", 3: "Bachelor's", 4: "Graduate"}

INSTITUTION_FIELDS = {  # logical field -> candidate column names (first found wins)
    "id": ["UNITID", "unitid", "id"],
    "name": ["school.name", "INSTNM", "instnm", "name"],
    "ownership": ["school.ownership", "CONTROL", "control"],
    "pred_degree": ["school.degrees_awarded.predominant", "PREDDEG", "preddeg"],
    "enrollment": ["latest.student.size", "UGDS", "ugds", "student.size"],
    "ope6": ["ope6_id", "OPEID6", "opeid6", "school.ope6_id"],
    "operating": ["school.operating", "CURROPER", "curroper"],
    "hcm2": ["school.under_investigation", "HCM2", "hcm2"],
    "state": ["school.state", "STABBR", "stabbr"],
    "url": ["school.school_url", "INSTURL", "insturl"],
}

PRIVACY_PROXY_PATTERNS = [
    "domains by proxy", "whoisguard", "contact privacy", "privacyprotect", "privacy protect",
    "withheld for privacy", "perfect privacy", "whois privacy", "private by design",
    "identity protection service", "privacy service provided by", "domain privacy",
    "proxy protection", "privacydotlink", "redacted privacy",
]
GDPR_PATTERNS = ["redacted for privacy", "data protected", "not disclosed", "gdpr", "statutory masking", "redacted"]

PLACEHOLDER_ID_RE = re.compile(r"X{4,}", re.I)
NAME_UNIV_RE = re.compile(r"\b(universit|college|colegio|universidad)", re.I)
STOP_TOKENS = {"the", "of", "and", "at", "in", "for", "home", "page", "welcome", "to",
               "official", "site", "website", "inc", "llc", "campus"}

FLAG_DEFINITIONS = {
    # flag column: (short label, definition, needs)
    "flag_unreachable": ("Site unreachable", "The URL on record is broken: DNS failure, connection failure or timeout, TLS failure, an HTTP error (404/410/5xx, redirect loop), or a malformed URL. 401/403/429/503 responses are treated as 'alive but blocking', not broken.", "always"),
    "flag_young_domain": ("Young domain", "Domain registered less than {young_domain_years} years before the crawl (WHOIS).", "domain_age_days"),
    "flag_whois_proxy": ("Paid WHOIS privacy proxy", "Registrant is a paid privacy-proxy service (e.g., Domains By Proxy). Default GDPR redaction is NOT counted, because registrars apply it to most .com/.org records.", "whois_registrant_org or whois_privacy_type"),
    "flag_named_univ_not_edu": ("'University/College' name without .edu", "Institution name contains University/College but the site is not on .edu (state .us and .gov domains excluded).", "institutions file with names"),
    "flag_ssl_problem": ("Certificate problem", "No TLS on port 443, a certificate that fails verification or is expired, or the crawler had to bypass certificate verification.", "ssl_ok"),
    "flag_consumer_builder": ("Consumer site builder", "Homepage built on Wix, Squarespace, Weebly or GoDaddy Website Builder (WordPress alone is not counted).", "uses_site_builder"),
    "flag_thin_homepage": ("Thin homepage vs. peers", "Words per page in the bottom {thin_percentile}% of the institution's peer group (ownership x predominant degree when available).", "words"),
    "flag_no_physical_presence": ("No phone or street address", "Homepage shows neither a phone number nor a street address with ZIP code.", "phone_number_hits, has_street_address"),
    "flag_shared_tracker": ("Shares a tracker ID with an unrelated school", "Shares a Google Analytics, GA4, Tag Manager or Meta pixel ID with a site on another registered domain, under a different OPEID6 owner when owner data is available.", "tracker ID columns"),
    "flag_shared_template": ("Shares an identical homepage template", "Identical homepage DOM structure as a site on another registered domain with a different owner (pages under {template_min_words} words excluded).", "structure hash"),
    "flag_redirect_off_domain": ("Redirects to another domain", "The URL on record redirects to a different registered domain.", "v2: redirected_off_domain"),
    "flag_parked_or_placeholder": ("Parked or placeholder site", "Parked / for-sale page, 'coming soon' placeholder, default server page, or parking name servers.", "v2: parked_domain_detected etc."),
    "flag_stale_copyright": ("Stale copyright year", "Most recent copyright year on the homepage is {stale_copyright_years}+ years before the crawl.", "max_copyright_year (not saved by the current home-only crawl)"),
    "flag_free_or_offdomain_email": ("Free or off-domain contact email", "A free-mail address (Gmail, Yahoo, ...) appears, or all mailto addresses are on other domains.", "free_email_hits / mailto_on_domain_ratio (zeroed in the current home-only crawl)"),
    "flag_unrecognized_accreditor": ("Links an unrecognized accreditor", "Links to an 'accreditation' domain not on the recognized list (or one whose recognition was terminated).", "v2: unrecognized_accredit_domains"),
    "flag_domain_expiring": ("Domain expiring or expired", "Domain registration expires within {domain_expiry_days} days of the crawl (or already has).", "v2: domain_days_to_expiry"),
    "flag_shared_contact_or_infra": ("Shares contact details or infrastructure", "Shares a phone number, contact email, street address, TLS certificate, identical page, near-duplicate text, social profile or redirect target with an unrelated school.", "v2: phone_numbers, contact_emails, ..."),
    "flag_missing_required_disclosures": ("Missing required disclosures (deep crawl)", "Deep crawl found neither a consumer-information page nor an annual security report link.", "deep crawl: has_consumer_info_link, has_security_report_link"),
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _isnan(v) -> bool:
    try:
        return v is None or v is pd.NA or (isinstance(v, float) and math.isnan(v))
    except Exception:
        return False


def parse_listlike(v) -> list:
    """Lists come back from CSV as JSON ('["a"]') or Python repr ("['a']"); parse both."""
    if isinstance(v, (list, tuple, set)):
        return [x for x in v if not _isnan(x)]
    if _isnan(v):
        return []
    if isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in {"nan", "none", "null", "[]", "na"}:
            return []
        if s[0] in "[(" and s[-1] in "])":
            for parser in (json.loads, ast.literal_eval):
                try:
                    out = parser(s)
                except Exception:
                    continue
                if isinstance(out, (list, tuple, set)):
                    return [x for x in out if not _isnan(x)]
            return []
        return [s]
    return [v]


_TRUE = {"true", "1", "yes", "y", "t", "1.0"}
_FALSE = {"false", "0", "no", "n", "f", "0.0"}


def to01(series: pd.Series) -> pd.Series:
    """Booleans in any spelling -> 1.0 / 0.0 / NaN."""
    def conv(v):
        if _isnan(v):
            return np.nan
        if isinstance(v, (bool, np.bool_)):
            return float(v)
        if isinstance(v, (int, float, np.integer, np.floating)):
            return float(v != 0) if v in (0, 1) else np.nan
        if isinstance(v, str):
            t = v.strip().lower()
            if t in _TRUE:
                return 1.0
            if t in _FALSE:
                return 0.0
        return np.nan
    return series.map(conv).astype(float)


def num(series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def clean_id(series: pd.Series) -> pd.Series:
    s = series.astype("string").str.strip()
    return s.str.replace(r"\.0$", "", regex=True)


try:  # optional, offline suffix list
    import tldextract
    _TLDX = tldextract.TLDExtract(suffix_list_urls=())
except Exception:  # pragma: no cover
    _TLDX = None


def host_of(url) -> str:
    if _isnan(url):
        return ""
    u = str(url).strip()
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    try:
        return (urlparse(u).hostname or "").lower()
    except Exception:
        return ""


def registered_domain_of(url) -> str:
    h = host_of(url)
    if not h:
        return ""
    if _TLDX is not None:
        ext = _TLDX(h)
        return ".".join(p for p in (ext.domain, ext.suffix) if p) or h
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def tld_of(url) -> str:
    h = host_of(url)
    if not h:
        return ""
    if _TLDX is not None:
        return _TLDX(h).suffix
    return h.rsplit(".", 1)[-1] if "." in h else ""


def tld_category(tld: str) -> str:
    t = (tld or "").lower()
    if not t:
        return "unknown"
    if t == "edu":
        return ".edu"
    if t.endswith("gov") or t.endswith("us"):
        return ".gov / state .us"
    if t == "org":
        return ".org"
    if t in {"com", "net"}:
        return ".com / .net"
    return "other"


def pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def norm_text(s) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(s).lower()).strip()


def name_tokens(s) -> set:
    return {t for t in norm_text(s).split() if len(t) >= 3 and t not in STOP_TOKENS}


def robust_scale(x: pd.Series) -> float:
    x = x.dropna()
    if len(x) < 5:
        return np.nan
    med = x.median()
    mad = (x - med).abs().median() * 1.4826
    if mad and mad > 0:
        return mad
    iqr = (x.quantile(0.75) - x.quantile(0.25)) / 1.349
    return iqr if iqr and iqr > 0 else np.nan


def auc_mann_whitney(scores: pd.Series, labels: pd.Series):
    s = pd.DataFrame({"s": scores, "y": labels}).dropna()
    n1 = int((s.y == 1).sum())
    n0 = int((s.y == 0).sum())
    if n1 == 0 or n0 == 0:
        return None
    ranks = s.s.rank(method="average")
    return float((ranks[s.y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


class UnionFind:
    def __init__(self):
        self.p = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if rb < ra:
                ra, rb = rb, ra
            self.p[rb] = ra

    def groups(self):
        out = defaultdict(set)
        for k in list(self.p):
            out[self.find(k)].add(k)
        return out


# --------------------------------------------------------------------------
# Loading and normalization
# --------------------------------------------------------------------------
def load_site_features(path_or_df, id_col: str, url_col: str, warn: list):
    if isinstance(path_or_df, pd.DataFrame):
        df = path_or_df.copy()
    else:
        df = pd.read_csv(path_or_df, dtype={id_col: str}, low_memory=False)
    cols_in = set(df.columns)
    if id_col not in df.columns:
        raise KeyError(f"ID column '{id_col}' not found. Columns: {list(df.columns)[:20]}...")
    if url_col not in df.columns:
        if "input_url" in df.columns:
            warn.append(f"URL column '{url_col}' not found; using input_url.")
            df[url_col] = df["input_url"]
        else:
            raise KeyError(f"URL column '{url_col}' not found.")
    df[id_col] = clean_id(df[id_col])

    # schema detection
    if "error_category" in cols_in or "crawl_mode" in cols_in:
        schema = "v2"
    elif "word_count" in cols_in or "structure_hash" in cols_in:
        schema = "legacy_home_only"
    elif "total_words" in cols_in:
        schema = "multi_page_v1"
    else:
        schema = "unknown"

    # legacy -> canonical names
    for old, new in LEGACY_ALIASES.items():
        if old in df.columns and new not in df.columns:
            df[new] = df[old]
    if "structure_hashes" not in df.columns and "structure_hash" in df.columns:
        df["structure_hashes"] = df["structure_hash"].map(lambda v: [] if _isnan(v) else [str(v)])

    for c in LIST_COLUMNS:
        if c in df.columns:
            df[c] = df[c].map(parse_listlike)
    for c in BOOL_COLUMNS:
        if c in df.columns:
            df[c] = to01(df[c])
    for c in NUMERIC_COLUMNS:
        if c in df.columns:
            df[c] = num(df[c])

    # fill domain / tld for rows that failed early
    if "input_url" not in df.columns:
        df["input_url"] = df[url_col]
    url_for_parse = df["input_url"].where(df["input_url"].notna(), df[url_col])
    for col, fn in (("registered_domain", registered_domain_of), ("tld", tld_of)):
        cur = df[col].astype("object") if col in df.columns else pd.Series([None] * len(df), index=df.index, dtype="object")
        miss = cur.map(lambda v: _isnan(v) or str(v).strip() == "")
        cur = cur.where(~miss, url_for_parse.map(fn))
        df[col] = cur.map(lambda v: "" if _isnan(v) else str(v).lower().strip())
    df["tld_category"] = df["tld"].map(tld_category)

    dup = df[id_col].duplicated(keep="first")
    if dup.any():
        warn.append(f"{int(dup.sum())} duplicate {id_col} rows dropped (kept first).")
        df = df[~dup].copy()

    return df.reset_index(drop=True), schema, cols_in


def _resolve_columns(header: list, overrides: dict) -> dict:
    found = {}
    for field, cands in INSTITUTION_FIELDS.items():
        if field in overrides:
            if overrides[field] in header:
                found[field] = overrides[field]
            continue
        for c in cands:
            if c in header:
                found[field] = c
                break
    return found


def load_institutions(path, overrides: dict, warn: list):
    header = list(pd.read_csv(path, nrows=0).columns)
    cols = _resolve_columns(header, overrides)
    if "id" not in cols:
        raise KeyError(f"No UNITID/id column found in {path}. Use --map id=<column>.")
    inst = pd.read_csv(path, usecols=list(set(cols.values())), dtype=str, low_memory=False)
    out = pd.DataFrame({"_id": clean_id(inst[cols["id"]])})
    for field, col in cols.items():
        if field == "id":
            continue
        out[f"inst_{field}"] = inst[col]
    for f in ("ownership", "pred_degree", "enrollment", "operating", "hcm2", "ope6"):
        if f"inst_{f}" in out.columns and f != "ope6":
            out[f"inst_{f}"] = num(out[f"inst_{f}"])
    if "inst_ope6" in out.columns:
        out["inst_ope6"] = clean_id(out["inst_ope6"]).str.lstrip("0")
        out.loc[out["inst_ope6"] == "", "inst_ope6"] = pd.NA
    out = out.drop_duplicates("_id")
    missing = [f for f in INSTITUTION_FIELDS if f not in cols]
    if missing:
        warn.append("Institutions file: not found -> " + ", ".join(missing) + " (use --map field=column).")
    return out, cols


def load_known_bad(path, warn: list) -> pd.DataFrame:
    kb = pd.read_csv(path, dtype=str)
    idc = next((c for c in ["UNITID", "unitid", "id"] if c in kb.columns), None)
    if idc is None:
        raise KeyError(f"No UNITID column in {path}.")
    out = pd.DataFrame({"_id": clean_id(kb[idc])})
    out["label"] = kb["label"].fillna("Known problem list") if "label" in kb.columns else "Known problem list"
    return out.drop_duplicates()


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------
_DNS = ["nameresolutionerror", "failed to resolve", "name or service not known", "nodename nor servname",
        "getaddrinfo failed", "no address associated", "temporary failure in name resolution",
        "name resolution", "errno -2", "errno -3", "errno 11001", "errno 8]"]
_REFUSED = ["connectionrefusederror", "connection refused", "errno 111", "errno 61", "errno 10061", "actively refused"]
_UNREACH = ["network is unreachable", "no route to host", "errno 101", "errno 113", "errno 65"]
_SSL = ["sslerror", "certificate_verify_failed", "wrong_version_number", "handshake", "[ssl", "tlsv1", "ssl:"]
_RESET = ["remotedisconnected", "connection reset", "connection aborted", "protocolerror",
          "badstatusline", "connectionreseterror", "errno 104", "errno 54", "errno 10054"]
_URL = ["invalidurl", "locationparseerror", "missingschema", "invalidschema", "failed to parse",
        "invalid url", "no host supplied", "label empty or too long", "idna"]
_TRANSFER = ["chunkedencodingerror", "contentdecodingerror", "incompleteread"]


def classify_error_text(err: str) -> str:
    e = str(err).lower()
    if any(p in e for p in _DNS):
        return "dns_failure"
    if "connecttimeout" in e or ("timed out" in e and "connect" in e):
        return "connect_timeout"
    if "readtimeout" in e or "read timed out" in e:
        return "read_timeout"
    if "toomanyredirects" in e or "exceeded 30 redirects" in e:
        return "too_many_redirects"
    if any(p in e for p in _URL):
        return "invalid_url"
    if any(p in e for p in _REFUSED):
        return "connection_refused"
    if any(p in e for p in _UNREACH):
        return "network_unreachable"
    if any(p in e for p in _SSL):
        return "ssl_error"
    if any(p in e for p in _RESET):
        return "connection_reset"
    if any(p in e for p in _TRANSFER):
        return "transfer_error"
    if "timed out" in e or "timeout" in e:
        return "read_timeout"
    return "other"


def category_from_status(code) -> str:
    if _isnan(code):
        return "other"
    c = int(code)
    if c in (401, 403, 404, 410, 429, 503):
        return f"http_{c}"
    if 500 <= c < 600:
        return "http_5xx"
    if 400 <= c < 500:
        return "http_4xx_other"
    if 300 <= c < 400:
        return "http_3xx_unresolved"
    if 200 <= c < 300:
        return "non_html"
    return "other"


_STATUS_RE = re.compile(r"returned status (\d{3})")


def classify_row(err, status, provided) -> str:
    if isinstance(provided, str) and provided in ERROR_CATEGORIES:
        return provided
    if _isnan(err) or str(err).strip() == "":
        return "ok"
    e = str(err)
    m = _STATUS_RE.search(e)
    if m:
        return category_from_status(int(m.group(1)))
    if e.startswith("Page extraction failed"):
        return "extraction_failed"
    if e.startswith("no HTML pages retrieved"):
        if not _isnan(status):
            return category_from_status(status) if int(status) != 200 else "non_html"
        return "other"
    return classify_error_text(e)


def add_availability(df: pd.DataFrame) -> pd.DataFrame:
    err = df["crawl_error"] if "crawl_error" in df.columns else pd.Series([None] * len(df))
    status = df["landing_status_code"] if "landing_status_code" in df.columns else pd.Series([np.nan] * len(df))
    provided = df["error_category"] if "error_category" in df.columns else pd.Series([None] * len(df))
    df["availability_category"] = [classify_row(e, s, p) for e, s, p in zip(err, status, provided)]
    df["availability_group"] = df["availability_category"].map(GROUP_OF).fillna("unknown")
    if "bot_protection_detected" in df.columns:
        df.loc[df["bot_protection_detected"] == 1, "availability_group"] = "blocked"
    df["availability_label"] = df["availability_group"].map(GROUP_LABELS)
    df["is_broken"] = df["availability_group"].isin(BROKEN_GROUPS).astype(float)
    return df


# --------------------------------------------------------------------------
# Derived per-site metrics
# --------------------------------------------------------------------------
def add_metrics(df: pd.DataFrame, opts: dict) -> pd.DataFrame:
    pages = df["pages_crawled"] if "pages_crawled" in df.columns else pd.Series(np.nan, index=df.index)
    pages = pages.where(pages > 0)

    def per_page(col):
        if col not in df.columns:
            return pd.Series(np.nan, index=df.index)
        return df[col] / pages.fillna(1)

    if "avg_words_per_page" in df.columns:
        df["words_per_page"] = df["avg_words_per_page"].fillna(per_page("total_words"))
    else:
        df["words_per_page"] = per_page("total_words")
    df["a_tags_per_page"] = per_page("total_a_tags")
    df["internal_links_per_page"] = per_page("total_internal_links")
    df["external_links_per_page"] = per_page("total_external_links")
    df["images_per_page"] = per_page("total_images")
    df["html_kb_per_page"] = per_page("total_html_bytes") / 1024.0
    df["trusted_links_per_page"] = per_page("trusted_outbound_links")
    df["has_content"] = df["words_per_page"].notna().astype(float)

    if "domain_age_days" in df.columns:
        df["domain_age_years"] = df["domain_age_days"] / 365.25
    else:
        df["domain_age_years"] = np.nan

    # crawl year for staleness
    if "crawl_timestamp" in df.columns:
        ts = pd.to_datetime(df["crawl_timestamp"], errors="coerce", utc=True)
        df["crawl_year"] = ts.dt.year.fillna(datetime.now(timezone.utc).year)
    else:
        df["crawl_year"] = datetime.now(timezone.utc).year
    if "max_copyright_year" in df.columns:
        yr = df["max_copyright_year"].where(df["max_copyright_year"].between(1990, 2100))
        df["copyright_staleness_years"] = (df["crawl_year"] - yr).clip(lower=0)

    # metadata completeness (0-5), only where the page was parsed
    parts = []
    for c in ["has_favicon", "has_edu_schema_markup"]:
        if c in df.columns:
            parts.append(df[c])
    for c in ["pct_pages_meta_description", "pct_pages_og_tags", "pct_pages_canonical"]:
        if c in df.columns:
            parts.append((df[c] > 0).astype(float).where(df[c].notna()))
    if parts:
        m = pd.concat(parts, axis=1)
        df["metadata_index"] = m.sum(axis=1, min_count=1).where(df["has_content"] == 1)
        df["metadata_index_max"] = len(parts)

    # trackers
    tk = [c for c in TRACKER_COLUMNS if c in df.columns]
    if tk:
        df["tracker_ids"] = df[tk].apply(lambda r: sorted({str(x) for c in tk for x in r[c]}), axis=1)
        df["n_tracker_ids"] = df["tracker_ids"].map(len).where(df["has_content"] == 1)
    if "fb_pixel_ids" in df.columns:
        df["has_fb_pixel"] = (df["fb_pixel_ids"].map(len) > 0).astype(float).where(df["has_content"] == 1)

    # title vs institution name
    if "inst_name" in df.columns and ("page_title" in df.columns or "og_site_name" in df.columns):
        def overlap(row):
            nt = name_tokens(row.get("inst_name", ""))
            if not nt or _isnan(row.get("inst_name")):
                return np.nan
            txt = " ".join(str(row.get(c, "")) for c in ("page_title", "og_site_name") if not _isnan(row.get(c)))
            if not txt.strip():
                return np.nan
            return len(nt & name_tokens(txt)) / len(nt)
        df["title_name_overlap"] = df.apply(overlap, axis=1)
    return df


def assign_peer_groups(df: pd.DataFrame, min_n: int) -> pd.Series:
    content = df["has_content"] == 1
    levels = []
    if "inst_ownership_label" in df.columns and "inst_pred_degree_label" in df.columns:
        levels.append(df["inst_ownership_label"].fillna("Unknown ownership") + " | " + df["inst_pred_degree_label"].fillna("Unknown degree"))
    if "inst_ownership_label" in df.columns:
        levels.append(df["inst_ownership_label"].fillna("Unknown ownership"))
    levels.append(pd.Series("All institutions", index=df.index))
    chosen = pd.Series(pd.NA, index=df.index, dtype="object")
    for lv in levels:
        sizes = lv[content].value_counts()
        ok = lv.map(sizes).fillna(0) >= min_n
        take = chosen.isna() & ok
        chosen[take] = lv[take]
    chosen = chosen.fillna("All institutions")
    return chosen


# --------------------------------------------------------------------------
# Cross-site sharing (recomputed from raw lists)
# --------------------------------------------------------------------------
def _domain_owners(df: pd.DataFrame) -> dict:
    owners = defaultdict(set)
    if "inst_ope6" in df.columns:
        for d, o in zip(df["registered_domain"], df["inst_ope6"]):
            if not _isnan(d) and not _isnan(o) and str(o):
                owners[d].add(str(o))
    return owners


def _simhash_pairs(dom_hash: dict, max_bits: int):
    """Candidate pairs via 4 bands of 16 bits, then exact Hamming check."""
    buckets = defaultdict(set)
    ints = {}
    for d, h in dom_hash.items():
        try:
            v = int(str(h), 16)
        except ValueError:
            continue
        ints[d] = v
        for b in range(4):
            buckets[(b, (v >> (16 * b)) & 0xFFFF)].add(d)
    pairs = set()
    for members in buckets.values():
        if len(members) < 2 or len(members) > 500:
            continue
        m = sorted(members)
        for i in range(len(m)):
            for j in range(i + 1, len(m)):
                if bin(ints[m[i]] ^ ints[m[j]]).count("1") <= max_bits:
                    pairs.add((m[i], m[j]))
    return pairs


def shared_key_analysis(df, key_type, values_by_row, opts, owners, exclude_mask=None, id_col="UNITID"):
    """
    values_by_row: Series of lists of keys (aligned to df rows).
    Returns (per_domain DataFrame, clusters DataFrame, edges list, excluded list).
    """
    key_domains = defaultdict(set)
    for i, (dom, keys) in enumerate(zip(df["registered_domain"], values_by_row)):
        if _isnan(dom) or not dom or not keys:
            continue
        if exclude_mask is not None and bool(exclude_mask.iloc[i]):
            continue
        for k in keys:
            if _isnan(k):
                continue
            k = str(k).strip()
            if not k or (key_type == "tracker_id" and PLACEHOLDER_ID_RE.search(k)):
                continue
            key_domains[k].add(dom)

    excluded = []
    edges = []
    uf = UnionFind()
    neighbors = defaultdict(set)
    shared_keys_of = defaultdict(set)
    for k, doms in key_domains.items():
        if len(doms) < 2:
            continue
        if len(doms) > opts["vendor_threshold"]:
            excluded.append({"key_type": key_type, "key": k, "n_domains": len(doms)})
            continue
        ds = sorted(doms)
        for i in range(len(ds)):
            shared_keys_of[ds[i]].add(k)
            for j in range(i + 1, len(ds)):
                uf.union(ds[i], ds[j])
                neighbors[ds[i]].add(ds[j])
                neighbors[ds[j]].add(ds[i])
                edges.append({"key_type": key_type, "domain_a": ds[i], "domain_b": ds[j], "shared_key": k})
    return _finish_shared(df, key_type, uf, neighbors, shared_keys_of, owners, id_col), edges, excluded


def _finish_shared(df, key_type, uf, neighbors, shared_keys_of, owners, id_col="UNITID"):
    rows = []
    groups = uf.groups()
    cluster_of = {}
    for root, members in groups.items():
        if len(members) >= 2:
            cid = f"{key_type}:{min(members)}"
            for m in members:
                cluster_of[m] = (cid, len(members))
    for d, nb in neighbors.items():
        own = owners.get(d, set())
        cross = sum(1 for o in nb if not (own & owners.get(o, set())))
        cid, size = cluster_of.get(d, (None, 1))
        rows.append({"registered_domain": d, f"n_domains_sharing_{key_type}": len(nb),
                     f"n_cross_owner_domains_sharing_{key_type}": cross,
                     f"{key_type}_cluster_id": cid, f"{key_type}_cluster_size": size,
                     f"_{key_type}_keys": sorted(shared_keys_of.get(d, []))})
    per_domain = pd.DataFrame(rows)

    # cluster table
    crow = []
    for root, members in groups.items():
        if len(members) < 2:
            continue
        sub = df[df["registered_domain"].isin(members)]
        ope = set(sub["inst_ope6"].dropna().astype(str)) if "inst_ope6" in sub.columns else set()
        names = sorted(set(sub["inst_name"].dropna().astype(str))) if "inst_name" in sub.columns else []
        keys = sorted(set().union(*(shared_keys_of.get(m, set()) for m in members)))
        crow.append({
            "cluster_id": f"{key_type}:{min(members)}",
            "key_type": key_type,
            "n_domains": len(members),
            "n_unitids": int(len(sub)),
            "n_owner_groups_ope6": len(ope) if ope else None,
            "cross_owner": (len(ope) > 1) if ope else True,
            "owner_data_available": bool(ope),
            "median_words_per_page": float(sub["words_per_page"].median()) if sub["words_per_page"].notna().any() else None,
            "shared_keys": "; ".join(keys[:20]) + (" ..." if len(keys) > 20 else ""),
            "domains": "; ".join(sorted(members)[:40]) + (" ..." if len(members) > 40 else ""),
            "institution_names": "; ".join(names[:20]) + (" ..." if len(names) > 20 else ""),
            "unitids": "; ".join(sorted(sub[id_col].astype(str))[:40]),
        })
    clusters = pd.DataFrame(crow)
    if not clusters.empty:
        clusters = clusters.sort_values(["cross_owner", "n_domains"], ascending=[False, False]).reset_index(drop=True)
    return per_domain, clusters


def run_cross_site(df: pd.DataFrame, opts: dict, id_col: str):
    owners = _domain_owners(df)
    results = {}
    all_edges, all_excluded = [], []
    words = df["words_per_page"]

    specs = []  # (key_type, values Series, exclude mask)
    tk = [c for c in TRACKER_COLUMNS if c in df.columns]
    if tk:
        specs.append(("tracker_id", df["tracker_ids"], None))
    if "structure_hashes" in df.columns:
        small = words.isna() | (words < opts["template_min_words"])
        specs.append(("template", df["structure_hashes"], small))
    if "css_content_hashes" in df.columns:
        specs.append(("css_file", df["css_content_hashes"], None))
    simple_lists = {"phone": "phone_numbers", "email": "contact_emails", "street_address": "street_addresses",
                    "social_profile": "social_profile_urls", "third_party_script": "third_party_script_domains",
                    "image": None}
    for kt, col in simple_lists.items():
        if col and col in df.columns:
            specs.append((kt, df[col].map(lambda L: [str(x).lower() for x in L]), None))
    scalar = {"ssl_cert": "ssl_cert_sha256", "html_exact": "html_sha256"}
    for kt, col in scalar.items():
        if col in df.columns:
            specs.append((kt, df[col].map(lambda v: [] if _isnan(v) or str(v) == "" else [str(v)]), None))
    if "ssl_cert" in [s[0] for s in specs] and "ssl_san_other_registered_domains" in df.columns:
        big = df["ssl_san_other_registered_domains"].map(len) > 20
        specs = [(k, v, big if k == "ssl_cert" else m) for k, v, m in specs]
    if "final_registered_domain" in df.columns and "redirected_off_domain" in df.columns:
        vals = [[str(f)] if (r == 1 and not _isnan(f)) else [] for f, r in zip(df["final_registered_domain"], df["redirected_off_domain"])]
        specs.append(("redirect_target", pd.Series(vals, index=df.index), None))
    if "whois_registrant_org" in df.columns:
        def reg_key(v):
            if _isnan(v):
                return []
            t = norm_text(v)
            if not t or any(p in t for p in PRIVACY_PROXY_PATTERNS) or any(p in t for p in GDPR_PATTERNS) or "privacy" in t or "proxy" in t:
                return []
            return [t]
        specs.append(("registrant_org", df["whois_registrant_org"].map(reg_key), None))

    for kt, vals, mask in specs:
        (per_dom, clusters), edges, excluded = shared_key_analysis(df, kt, vals, opts, owners, mask, id_col)
        results[kt] = (per_dom, clusters)
        all_edges += edges
        all_excluded += excluded

    # near-duplicate text via SimHash
    if "text_simhash64" in df.columns:
        dom_hash = {}
        for d, h, w in zip(df["registered_domain"], df["text_simhash64"], words):
            if not _isnan(h) and str(h) and not _isnan(w) and w >= opts["template_min_words"]:
                dom_hash.setdefault(d, h)
        pairs = _simhash_pairs(dom_hash, opts["near_dup_bits"])
        uf, nb, sk = UnionFind(), defaultdict(set), defaultdict(set)
        for a, b in pairs:
            uf.union(a, b)
            nb[a].add(b)
            nb[b].add(a)
            sk[a].add(f"simhash~{b}")
            sk[b].add(f"simhash~{a}")
            all_edges.append({"key_type": "near_dup_text", "domain_a": a, "domain_b": b, "shared_key": "SimHash distance <= %d" % opts["near_dup_bits"]})
        results["near_dup_text"] = _finish_shared(df, "near_dup_text", uf, nb, sk, owners, id_col)

    # merge per-domain results back to rows
    for kt, (per_dom, _) in results.items():
        if per_dom.empty:
            df[f"n_domains_sharing_{kt}"] = 0
            df[f"n_cross_owner_domains_sharing_{kt}"] = 0
            df[f"{kt}_cluster_id"] = None
            continue
        per_dom = per_dom.drop(columns=[c for c in per_dom.columns if c.startswith("_")])
        per_dom["registered_domain"] = per_dom["registered_domain"].astype(str)
        df["registered_domain"] = df["registered_domain"].astype(str)
        df = df.merge(per_dom, on="registered_domain", how="left")
        for c in (f"n_domains_sharing_{kt}", f"n_cross_owner_domains_sharing_{kt}"):
            df[c] = df[c].fillna(0)
        df[f"{kt}_cluster_size"] = df[f"{kt}_cluster_size"].fillna(1)

    edges = pd.DataFrame(all_edges, columns=["key_type", "domain_a", "domain_b", "shared_key"])
    excluded = pd.DataFrame(all_excluded, columns=["key_type", "key", "n_domains"])
    if not excluded.empty:
        excluded = excluded.sort_values("n_domains", ascending=False)
    clusters = {kt: c for kt, (_, c) in results.items()}
    return df, clusters, edges, excluded


def shared_url_groups(df: pd.DataFrame, id_col: str, url_col: str) -> pd.DataFrame:
    rows = []
    for dom, sub in df.groupby("registered_domain"):
        if not dom or len(sub) < 2:
            continue
        ope = set(sub["inst_ope6"].dropna().astype(str)) if "inst_ope6" in sub.columns else set()
        rows.append({
            "registered_domain": dom,
            "n_unitids": len(sub),
            "n_distinct_urls": sub[url_col].astype(str).str.lower().str.rstrip("/").nunique(),
            "n_owner_groups_ope6": len(ope) if ope else None,
            "cross_owner": (len(ope) > 1) if ope else None,
            "unitids": "; ".join(sub[id_col].astype(str).tolist()[:50]),
            "institution_names": "; ".join(sub["inst_name"].dropna().astype(str).tolist()[:25]) if "inst_name" in sub.columns else "",
            "states": "; ".join(sorted(set(sub["inst_state"].dropna().astype(str)))) if "inst_state" in sub.columns else "",
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["n_unitids"], ascending=False).reset_index(drop=True)
    return out


# --------------------------------------------------------------------------
# Flags, score, tiers
# --------------------------------------------------------------------------
def build_flags(df: pd.DataFrame, opts: dict, schema: str, notes: dict) -> pd.DataFrame:
    content = df["has_content"] == 1
    F = {}

    F["flag_unreachable"] = df["is_broken"]

    if "domain_age_days" in df.columns and df["domain_age_days"].notna().any():
        F["flag_young_domain"] = (df["domain_age_years"] < opts["young_domain_years"]).astype(float).where(df["domain_age_years"].notna())

    if "whois_privacy_type" in df.columns:
        t = df["whois_privacy_type"].astype("string").str.lower()
        F["flag_whois_proxy"] = (t == "proxy_service").astype(float).where(t.notna())
    elif "whois_registrant_org" in df.columns:
        org = df["whois_registrant_org"].map(lambda v: "" if _isnan(v) else norm_text(v))
        f = org.map(lambda o: 1.0 if o and any(p in o for p in PRIVACY_PROXY_PATTERNS) else (0.0 if o else np.nan))
        if f.notna().any():
            F["flag_whois_proxy"] = f
            notes["flag_whois_proxy"] = "Evaluated only where WHOIS disclosed a registrant organization."

    if "inst_name" in df.columns:
        named = df["inst_name"].map(lambda n: (not _isnan(n)) and bool(NAME_UNIV_RE.search(str(n))))
        tl = df["tld"].astype("string").str.lower().fillna("")
        public_ok = (tl == "edu") | tl.str.endswith("gov") | tl.str.endswith("us")
        F["flag_named_univ_not_edu"] = (named & ~public_ok).astype(float).where(df["inst_name"].notna() & (tl != ""))

    if "ssl_ok" in df.columns:
        prob = (df["ssl_ok"] == 0)
        if "ssl_verified" in df.columns:
            prob |= (df["ssl_verified"] == 0)
        if "ssl_cert_error" in df.columns:
            prob |= df["ssl_cert_error"].notna() & (df["ssl_cert_error"].astype("string").str.strip() != "")
        if "ssl_days_to_expiry" in df.columns:
            prob |= (df["ssl_days_to_expiry"] < 0)
        if "ssl_bypass_used" in df.columns:
            prob |= (df["ssl_bypass_used"] == 1)
        F["flag_ssl_problem"] = prob.astype(float).where(df["ssl_ok"].notna())

    if "uses_site_builder" in df.columns:
        F["flag_consumer_builder"] = df["uses_site_builder"].where(content)

    # thin homepage vs peers
    w = df["words_per_page"].where(content)
    thresh = w.groupby(df["peer_group"]).transform(lambda s: s.quantile(opts["thin_percentile"] / 100.0) if s.notna().sum() >= 5 else np.nan)
    df["thin_threshold_words"] = thresh
    F["flag_thin_homepage"] = (w < thresh).astype(float).where(w.notna() & thresh.notna())

    if "phone_number_hits" in df.columns and "has_street_address" in df.columns:
        none = (df["phone_number_hits"].fillna(0) == 0) & (df["has_street_address"] == 0)
        F["flag_no_physical_presence"] = none.astype(float).where(content & df["has_street_address"].notna())

    if "n_cross_owner_domains_sharing_tracker_id" in df.columns:
        F["flag_shared_tracker"] = (df["n_cross_owner_domains_sharing_tracker_id"] > 0).astype(float).where(content)
    if "n_cross_owner_domains_sharing_template" in df.columns:
        F["flag_shared_template"] = (df["n_cross_owner_domains_sharing_template"] > 0).astype(float).where(content)

    # ---- v2 / future fields: switch on automatically when present ----
    if "redirected_off_domain" in df.columns:
        F["flag_redirect_off_domain"] = df["redirected_off_domain"]
    pk = [c for c in ["parked_domain_detected", "placeholder_page_detected", "default_server_page_detected", "parking_nameserver_detected"] if c in df.columns]
    if pk:
        m = pd.concat([df[c] for c in pk], axis=1)
        F["flag_parked_or_placeholder"] = m.max(axis=1, skipna=True).where(m.notna().any(axis=1))
    if "copyright_staleness_years" in df.columns and df["copyright_staleness_years"].notna().any():
        F["flag_stale_copyright"] = (df["copyright_staleness_years"] >= opts["stale_copyright_years"]).astype(float).where(df["copyright_staleness_years"].notna())
    if schema != "legacy_home_only":
        parts = []
        if "free_email_addresses" in df.columns:
            parts.append((df["free_email_addresses"].map(len) > 0).astype(float))
        elif "free_email_hits" in df.columns:
            parts.append((df["free_email_hits"] > 0).astype(float).where(df["free_email_hits"].notna()))
        if "mailto_on_domain_ratio" in df.columns:
            parts.append((df["mailto_on_domain_ratio"] == 0).astype(float).where(df["mailto_on_domain_ratio"].notna()))
        if parts:
            m = pd.concat(parts, axis=1)
            F["flag_free_or_offdomain_email"] = m.max(axis=1, skipna=True).where(content & m.notna().any(axis=1))
    if "unrecognized_accredit_domains" in df.columns:
        F["flag_unrecognized_accreditor"] = (df["unrecognized_accredit_domains"].map(len) > 0).astype(float).where(content)
    if "domain_days_to_expiry" in df.columns:
        F["flag_domain_expiring"] = (df["domain_days_to_expiry"] < opts["domain_expiry_days"]).astype(float).where(df["domain_days_to_expiry"].notna())
    strong = [k for k in ["phone", "email", "street_address", "ssl_cert", "html_exact", "near_dup_text", "social_profile", "redirect_target"]
              if f"n_cross_owner_domains_sharing_{k}" in df.columns]
    if strong:
        m = pd.concat([(df[f"n_cross_owner_domains_sharing_{k}"] > 0).astype(float) for k in strong], axis=1)
        F["flag_shared_contact_or_infra"] = m.max(axis=1).where(content)
        df["shared_contact_or_infra_types"] = m.apply(lambda r: "; ".join(k for k, v in zip(strong, r) if v == 1), axis=1)
    if {"has_consumer_info_link", "has_security_report_link"}.issubset(df.columns):
        deep = df["crawl_mode"].eq("multi_page") if "crawl_mode" in df.columns else df["pages_crawled"].gt(1)
        miss = (df["has_consumer_info_link"] == 0) & (df["has_security_report_link"] == 0)
        F["flag_missing_required_disclosures"] = miss.astype(float).where(deep & content)

    flags = pd.DataFrame(F, index=df.index)
    df = pd.concat([df, flags], axis=1)
    fcols = list(flags.columns)
    df["red_flag_score"] = flags.fillna(0).sum(axis=1).astype(int)
    df["n_flags_evaluated"] = flags.notna().sum(axis=1).astype(int)
    df["n_flags_possible"] = len(fcols)

    def tier(s):
        if s >= opts["high_threshold"]:
            return "High"
        if s >= opts["medium_threshold"]:
            return "Medium"
        return "Low"
    df["triage_tier"] = df["red_flag_score"].map(tier)

    labels = {c: FLAG_DEFINITIONS[c][0] for c in fcols}
    df["flag_reasons"] = flags.apply(lambda r: "; ".join(labels[c] for c in fcols if r[c] == 1), axis=1)
    return df, fcols


# --------------------------------------------------------------------------
# Peer-relative anomaly and size-vs-enrollment
# --------------------------------------------------------------------------
ANOMALY_FEATURES = {  # column: (label, log1p?)
    "words_per_page": ("Words", True),
    "a_tags_per_page": ("Links (all)", True),
    "internal_links_per_page": ("Internal links", True),
    "images_per_page": ("Images", True),
    "html_kb_per_page": ("Page size", True),
    "num_subdomains_seen": ("Subdomains linked", True),
    "trusted_links_per_page": ("Accreditor/.gov links", True),
    "img_alt_coverage": ("Alt-text coverage", False),
    "metadata_index": ("Metadata completeness", False),
}


def add_anomaly(df: pd.DataFrame, opts: dict, use_iforest: bool, warn: list) -> pd.DataFrame:
    content = df["has_content"] == 1
    deficits = {}
    for col, (label, lg) in ANOMALY_FEATURES.items():
        if col not in df.columns or df.loc[content, col].notna().sum() < 10:
            continue
        x = df[col].where(content)
        x = np.log1p(x.clip(lower=0)) if lg else x
        med = x.groupby(df["peer_group"]).transform("median")
        scale = x.groupby(df["peer_group"]).transform(robust_scale)
        z = (x - med) / scale
        deficits[label] = (-z).clip(lower=0)
    if len(deficits) >= 3:
        D = pd.DataFrame(deficits)
        enough = D.notna().sum(axis=1) >= 3
        df["anomaly_score"] = D.mean(axis=1, skipna=True).where(enough & content).round(3)
        df["anomaly_top_features"] = D.apply(
            lambda r: "; ".join(f"{k} (-{v:.1f} SD)" for k, v in r.dropna().sort_values(ascending=False).items() if v >= 1.0)[:200], axis=1
        ).where(enough & content)
        df["anomalous_vs_peers"] = (df["anomaly_score"] >= opts["anomaly_threshold"]).astype(float).where(df["anomaly_score"].notna())
    if use_iforest:
        try:
            from sklearn.ensemble import IsolationForest
            cols = [c for c in ANOMALY_FEATURES if c in df.columns]
            X = df.loc[content, cols].apply(lambda s: np.log1p(s.clip(lower=0)))
            X = X.fillna(X.median())
            if len(X) >= 50:
                model = IsolationForest(n_estimators=300, random_state=0, contamination="auto").fit(X)
                df.loc[content, "iforest_score"] = (-model.score_samples(X)).round(4)
                df["iforest_rank"] = df["iforest_score"].rank(ascending=False, method="min")
            else:
                warn.append("Isolation Forest skipped: fewer than 50 sites with content.")
        except ImportError:
            warn.append("Isolation Forest skipped: scikit-learn not installed.")
    return df


def add_enrollment_residual(df: pd.DataFrame, opts: dict) -> dict:
    out = {}
    if "inst_enrollment" not in df.columns:
        return out
    m = (df["has_content"] == 1) & (df["words_per_page"] > 0) & (df["inst_enrollment"] > 0)
    if m.sum() < 30:
        return out
    x = np.log10(df.loc[m, "inst_enrollment"].astype(float))
    y = np.log10(df.loc[m, "words_per_page"].astype(float))
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (slope * x + intercept)
    scale = robust_scale(resid) or resid.std()
    df.loc[m, "words_vs_enrollment_resid_z"] = ((resid - resid.median()) / scale).round(2)
    df["low_size_for_enrollment"] = (df["words_vs_enrollment_resid_z"] < opts["enrollment_residual_z"]).astype(float).where(df["words_vs_enrollment_resid_z"].notna())
    out.update(n=int(m.sum()), slope=float(slope), intercept=float(intercept),
               spearman=float(pd.Series(x).corr(pd.Series(y), method="spearman")))
    return out


# --------------------------------------------------------------------------
# Validation against known-problem labels
# --------------------------------------------------------------------------
EXTRA_MEASURES = {"anomalous_vs_peers": "Anomalous vs. peers (not scored)",
                  "low_size_for_enrollment": "Homepage far smaller than enrollment predicts (not scored)"}


def build_validation(df: pd.DataFrame, fcols: list, label_sets: dict, warn: list) -> pd.DataFrame:
    rows = []
    for label, ybad in label_sets.items():
        y = ybad.astype(float)
        n_bad = int((y == 1).sum())
        n_rest = int((y == 0).sum())
        if n_bad == 0:
            warn.append(f"Validation '{label}': no matching institutions in the crawl.")
            continue
        if n_bad < 20:
            warn.append(f"Validation '{label}': only {n_bad} labelled institutions; treat lift as indicative.")
        auc = auc_mann_whitney(df["red_flag_score"], y)
        rows.append({"label": label, "measure": "Red-flag score AUC", "n_labelled": n_bad, "n_rest": n_rest,
                     "rate_labelled_pct": None, "rate_rest_pct": None, "lift": None,
                     "value": round(auc, 3) if auc is not None else None,
                     "note": "0.5 = no better than chance; 1.0 = perfect separation."})
        hi_b = (df.loc[y == 1, "triage_tier"] == "High").mean()
        hi_r = (df.loc[y == 0, "triage_tier"] == "High").mean()
        rows.append({"label": label, "measure": "Share in High tier", "n_labelled": n_bad, "n_rest": n_rest,
                     "rate_labelled_pct": round(100 * hi_b, 1), "rate_rest_pct": round(100 * hi_r, 1),
                     "lift": round(hi_b / hi_r, 2) if hi_r > 0 else None, "value": None, "note": ""})
        for c in fcols + [x for x in ["anomalous_vs_peers", "low_size_for_enrollment"] if x in df.columns]:
            ev = df[c].notna()
            b = df.loc[ev & (y == 1), c]
            r = df.loc[ev & (y == 0), c]
            if len(b) == 0 or len(r) == 0:
                continue
            rb, rr = b.mean(), r.mean()
            note = ""
            if label.lower().startswith("not currently operating") and c == "flag_unreachable":
                note = "Expected: closed schools' sites go offline. Not evidence the flags detect fraud."
            elif rr == 0 and rb > 0:
                note = "Never seen outside the labelled group; lift undefined (very strong separation)."
            measure = FLAG_DEFINITIONS[c][0] if c in FLAG_DEFINITIONS else EXTRA_MEASURES.get(c, c)
            rows.append({"label": label, "measure": measure,
                         "n_labelled": int(len(b)), "n_rest": int(len(r)),
                         "rate_labelled_pct": round(100 * rb, 1), "rate_rest_pct": round(100 * rr, 1),
                         "lift": round(rb / rr, 2) if rr > 0 else None, "value": None, "note": note})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Summary sheet
# --------------------------------------------------------------------------
class Summary:
    def __init__(self):
        self.rows = []

    def add(self, rank, section, metric, count=None, denom=None, value=None, note=""):
        self.rows.append({
            "rank": rank, "section": section, "metric": metric,
            "count": None if count is None else int(count),
            "denominator": None if denom is None else int(denom),
            "percent": pct(count, denom) if (count is not None and denom) else None,
            "value": value, "note": note,
        })

    def frame(self):
        f = pd.DataFrame(self.rows)
        for c in ("count", "denominator"):
            f[c] = f[c].astype("Int64")
        return f


def build_summary(df, fcols, clusters, shared_urls, excluded, validation, enroll_fit, opts, schema, id_col, url_col, inst_info):
    S = Summary()
    N = len(df)
    content = df["has_content"] == 1
    NC = int(content.sum())
    reach = df["availability_group"] == "reachable"

    # 1 Availability
    sec = "1. Availability"
    S.add(1, sec, "Institutions (UNITIDs) in the crawl", N, None, note="Denominator for this section.")
    S.add(1, sec, "Unique URLs crawled", df["input_url"].nunique())
    S.add(1, sec, "Unique registered domains", df["registered_domain"].nunique())
    if inst_info.get("n_no_url") is not None:
        S.add(1, sec, "Institutions in the institutions file with no website on record", inst_info["n_no_url"], inst_info["n_inst"])
    if inst_info.get("n_not_crawled") is not None:
        S.add(1, sec, "Institutions with a URL on record but not in the crawl", inst_info["n_not_crawled"], inst_info["n_inst"])
    broken = int(df["is_broken"].sum())
    S.add(1, sec, "BROKEN: URL on record does not work", broken, N,
          note="DNS, connection, TLS, HTTP error or malformed URL. Excludes sites that answer but block bots.")
    for g in GROUP_ORDER:
        n = int((df["availability_group"] == g).sum())
        if n:
            S.add(1, sec, f"  {GROUP_LABELS[g]}", n, N)
    for cat, n in df["availability_category"].value_counts().items():
        S.add(1, sec, f"    {CATEGORY_LABELS.get(cat, cat)}", n, N, note=GROUP_LABELS[GROUP_OF.get(cat, 'unknown')])
    if "ssl_bypass_used" in df.columns:
        S.add(1, sec, "Loaded only after bypassing certificate verification", int((df["ssl_bypass_used"] == 1).sum()), N)
    if "http_fallback_used" not in df.columns:
        S.add(1, sec, "Caveat: HTTP-only sites", note="The crawler forces https://, so sites that serve only plain HTTP count as broken. Retest connection failures over http:// before reporting (spec C09).")
    for col, label in [("http_fallback_used", "Reached only via http:// fallback"), ("www_fallback_used", "Reached only via the www. variant"),
                       ("bot_protection_detected", "Bot-protection challenge detected"), ("dns_resolves", "Domain resolves in DNS"),
                       ("redirected_off_domain", "Redirects to a different domain"), ("parked_domain_detected", "Parked / for-sale page"),
                       ("placeholder_page_detected", "'Coming soon' / placeholder page"), ("default_server_page_detected", "Default web-server page"),
                       ("parking_nameserver_detected", "Name servers belong to a parking service")]:
        if col in df.columns and df[col].notna().any():
            S.add(1, sec, label, int((df[col] == 1).sum()), int(df[col].notna().sum()))
    if "final_registered_domain" in df.columns and "redirected_off_domain" in df.columns:
        top = df.loc[df["redirected_off_domain"] == 1, "final_registered_domain"].value_counts()
        multi = top[top > 1]
        if len(multi):
            S.add(1, sec, "Redirect targets receiving 2+ institutions' URLs", len(multi), value="; ".join(f"{d} ({n})" for d, n in multi.head(10).items()))

    # 2 Red-flag triage
    sec = "2. Red-flag triage"
    S.add(2, sec, "Flags evaluated in this run", len(fcols), value="; ".join(FLAG_DEFINITIONS[c][0] for c in fcols),
          note="Score = number of flags raised. New flags switch on as spec v2 columns appear.")
    for t in ["High", "Medium", "Low"]:
        n = int((df["triage_tier"] == t).sum())
        rule = {"High": f">= {opts['high_threshold']} flags", "Medium": f"{opts['medium_threshold']} flags", "Low": f"0-{opts['medium_threshold'] - 1} flags"}[t]
        S.add(2, sec, f"Tier {t} ({rule}), all institutions", n, N)
    for t in ["High", "Medium"]:
        n = int(((df["triage_tier"] == t) & reach).sum())
        S.add(2, sec, f"Tier {t} among reachable sites", n, int(reach.sum()))
    S.add(2, sec, "Mean red-flag score (reachable sites)", value=round(float(df.loc[reach, "red_flag_score"].mean()), 2) if reach.any() else None)
    for c in fcols:
        ev = df[c].notna()
        S.add(2, sec, f"Flag: {FLAG_DEFINITIONS[c][0]}", int((df[c] == 1).sum()), int(ev.sum()), note="Denominator = institutions where this flag could be evaluated.")

    # 3 Validation
    sec = "3. Validation against known-problem schools"
    if validation is None or validation.empty:
        S.add(3, sec, "Not run", note="Provide --institutions with HCM2 / operating columns, or --known-bad, to test whether the flags separate known-problem schools.")
    else:
        for label, sub in validation.groupby("label"):
            auc = sub.loc[sub["measure"] == "Red-flag score AUC", "value"]
            S.add(3, sec, f"{label}: red-flag score AUC", int(sub["n_labelled"].iloc[0]), None, value=auc.iloc[0] if len(auc) else None,
                  note="AUC 0.5 = chance. See the Validation tab for per-flag lift.")
            fl = sub.dropna(subset=["lift"]).sort_values("lift", ascending=False)
            fl = fl[fl["measure"] != "Share in High tier"]
            if len(fl):
                S.add(3, sec, f"{label}: highest-lift flags", value="; ".join(f"{m} (x{l})" for m, l in zip(fl["measure"].head(3), fl["lift"].head(3))))

    # 4 Shared trackers
    sec = "4. Shared tracker IDs"
    if "n_tracker_ids" in df.columns:
        S.add(4, sec, "Sites with any analytics / tag / pixel ID", int((df["n_tracker_ids"] > 0).sum()), NC)
        allids = Counter(i for L in df.loc[content, "tracker_ids"] for i in L)
        S.add(4, sec, "Distinct tracker IDs found", len(allids))
        cl = clusters.get("tracker_id")
        if cl is not None and not cl.empty:
            S.add(4, sec, "Tracker-sharing clusters (2+ domains)", len(cl))
            S.add(4, sec, "Clusters spanning different owners (or owner unknown)", int(cl["cross_owner"].sum()))
            S.add(4, sec, "Institutions flagged for sharing a tracker across owners", int((df.get("flag_shared_tracker", pd.Series(dtype=float)) == 1).sum()), NC)
            S.add(4, sec, "Largest cluster (domains)", int(cl["n_domains"].max()))
        else:
            S.add(4, sec, "Tracker-sharing clusters (2+ domains)", 0)
        if not excluded.empty:
            ex = excluded[excluded["key_type"] == "tracker_id"]
            S.add(4, sec, f"Tracker IDs excluded as vendor/platform-wide (> {opts['vendor_threshold']} domains)", len(ex))
        if "inst_ope6" not in df.columns:
            S.add(4, sec, "Caveat", note="No OPEID6 owner data: branch campuses sharing a tracker count as 'cross-owner'. Provide --institutions to exclude same-owner pairs.")

    # 5 Templates
    sec = "5. Identical homepage templates"
    cl = clusters.get("template")
    if cl is not None:
        S.add(5, sec, "Template clusters (identical DOM structure, 2+ domains)", len(cl), note=f"Pages under {opts['template_min_words']} words excluded.")
        if not cl.empty:
            S.add(5, sec, "Clusters spanning different owners (or owner unknown)", int(cl["cross_owner"].sum()))
            S.add(5, sec, "Domains in template clusters", int(cl["n_domains"].sum()))
        if "structure_hashes" in df.columns:
            small = content & (df["words_per_page"] < opts["template_min_words"])
            h = df.loc[small, "structure_hashes"].map(lambda L: L[0] if L else None).dropna()
            vc = h.value_counts()
            S.add(5, sec, f"Near-empty homepages (< {opts['template_min_words']} words)", int(small.sum()), NC,
                  note="Often parked, placeholder or error pages; review by hand.")
            S.add(5, sec, "  ...sharing an identical structure with another near-empty page", int(vc[vc > 1].sum()), int(small.sum()))
    else:
        S.add(5, sec, "Not available", note="No structure hash column.")

    # 6 Shared URLs
    sec = "6. Several institutions, one website"
    if shared_urls is not None and not shared_urls.empty:
        S.add(6, sec, "Registered domains used by 2+ UNITIDs", len(shared_urls))
        S.add(6, sec, "UNITIDs on a shared domain", int(shared_urls["n_unitids"].sum()), N)
        if shared_urls["cross_owner"].notna().any():
            S.add(6, sec, "Shared domains spanning different OPEID6 owners", int((shared_urls["cross_owner"] == True).sum()), len(shared_urls))  # noqa: E712
        else:
            S.add(6, sec, "Caveat", note="Without OPEID6 the table cannot separate branch campuses from unrelated institutions.")
    else:
        S.add(6, sec, "Registered domains used by 2+ UNITIDs", 0)

    # 7 Domain age
    sec = "7. Domain age"
    if df["domain_age_years"].notna().any():
        a = df["domain_age_years"]
        ev = int(a.notna().sum())
        S.add(7, sec, "Institutions with a WHOIS creation date", ev, N)
        for yrs in (1, 3, 5, 10):
            S.add(7, sec, f"Domain younger than {yrs} year{'s' if yrs > 1 else ''}", int((a < yrs).sum()), ev)
        S.add(7, sec, "Median domain age (years)", value=round(float(a.median()), 1))
        if schema == "legacy_home_only":
            S.add(7, sec, "Caveat", note="WHOIS fields were erased for failed crawls (spec C03), so broken sites have no domain age.")
    else:
        S.add(7, sec, "Not available", note="No domain_age_days values (python-whois not installed, or lookups failed).")
    if "domain_days_to_expiry" in df.columns and df["domain_days_to_expiry"].notna().any():
        d = df["domain_days_to_expiry"]
        S.add(7, sec, f"Domain expires within {opts['domain_expiry_days']} days (or expired)", int((d < opts["domain_expiry_days"]).sum()), int(d.notna().sum()))

    # 8 TLD mix
    sec = "8. Top-level domain mix"
    for cat, n in df["tld_category"].value_counts().items():
        S.add(8, sec, f"Sites on {cat}", n, N)
    if "flag_named_univ_not_edu" in df.columns:
        S.add(8, sec, "'University' or 'College' in name, not on .edu", int((df["flag_named_univ_not_edu"] == 1).sum()), int(df["flag_named_univ_not_edu"].notna().sum()),
              note=".edu requires accreditation from a recognized accreditor (domains registered before Oct 2001 are grandfathered).")

    # 9 WHOIS
    sec = "9. WHOIS privacy and registrants"
    if "whois_registrant_org" in df.columns or "whois_privacy_protected" in df.columns:
        if "whois_privacy_protected" in df.columns and df["whois_privacy_protected"].notna().any():
            p = df["whois_privacy_protected"]
            S.add(9, sec, "Any WHOIS privacy or redaction (raw flag)", int((p == 1).sum()), int(p.notna().sum()),
                  note="Includes default GDPR redaction; mostly reflects TLD. See the TLD crosstab.")
        if "flag_whois_proxy" in df.columns:
            f = df["flag_whois_proxy"]
            S.add(9, sec, "Registrant is a paid privacy-proxy service", int((f == 1).sum()), int(f.notna().sum()))
        cl = clusters.get("registrant_org")
        if cl is not None and not cl.empty:
            S.add(9, sec, "Registrant organizations behind 2+ domains", len(cl),
                  note="Reported, not scored: system offices and web vendors register for many schools.")
    else:
        S.add(9, sec, "Not available")

    # 10 SSL
    sec = "10. TLS certificates"
    if "ssl_ok" in df.columns and df["ssl_ok"].notna().any():
        ev = int(df["ssl_ok"].notna().sum())
        S.add(10, sec, "No TLS handshake on port 443", int((df["ssl_ok"] == 0).sum()), ev)
        if "ssl_verified" in df.columns:
            S.add(10, sec, "Certificate fails verification", int((df["ssl_verified"] == 0).sum()), int(df["ssl_verified"].notna().sum()))
        if "ssl_cert_error" in df.columns:
            ce = df["ssl_cert_error"].notna() & (df["ssl_cert_error"].astype("string").str.strip() != "")
            S.add(10, sec, "Certificate error recorded", int(ce.sum()), ev)
        if "ssl_days_to_expiry" in df.columns and df["ssl_days_to_expiry"].notna().any():
            d = df["ssl_days_to_expiry"]
            S.add(10, sec, "Certificate expired", int((d < 0).sum()), int(d.notna().sum()))
        missing_fields = (df["ssl_ok"] == 1) & df.get("ssl_days_to_expiry", pd.Series(np.nan, index=df.index)).isna() & df.get("ssl_issuer_org", pd.Series(np.nan, index=df.index)).isna()
        mf = int(missing_fields.sum())
        okn = int((df["ssl_ok"] == 1).sum())
        if okn and mf / okn > 0.5:
            S.add(10, sec, "WARNING: certificate details missing", mf, okn,
                  note="Handshake succeeded but issuer/expiry are empty: the crawl likely ran with --insecure (spec C02). Organization-validated share is unreliable and not reported.")
        elif "ssl_org_validated" in df.columns and df["ssl_org_validated"].notna().any():
            o = df["ssl_org_validated"].where(df["ssl_ok"] == 1)
            S.add(10, sec, "Organization-validated (OV/EV) certificate", int((o == 1).sum()), int(o.notna().sum()),
                  note="Domain-validated certificates are common among legitimate schools too; weak signal.")
        if "ssl_validity_days" in df.columns and df["ssl_validity_days"].notna().any():
            S.add(10, sec, "Short-lived certificate (<= 100 days, usually automated free issuance)", int((df["ssl_validity_days"] <= 100).sum()), int(df["ssl_validity_days"].notna().sum()))
    else:
        S.add(10, sec, "Not available")

    # 11 Size vs enrollment
    sec = "11. Homepage size vs. enrollment"
    if enroll_fit:
        S.add(11, sec, "Sites with enrollment and homepage text", enroll_fit["n"])
        S.add(11, sec, "Spearman correlation, log words vs. log enrollment", value=round(enroll_fit["spearman"], 3))
        S.add(11, sec, f"Far smaller than enrollment predicts (residual z < {opts['enrollment_residual_z']})",
              int((df.get("low_size_for_enrollment", pd.Series(dtype=float)) == 1).sum()), enroll_fit["n"], note="See chart words_vs_enrollment.png.")
    else:
        S.add(11, sec, "Not run", note="Needs --institutions with an enrollment column (e.g., UGDS / latest.student.size).")

    # 12 Anomaly
    sec = "12. Peer-relative anomaly"
    if "anomaly_score" in df.columns:
        ev = int(df["anomaly_score"].notna().sum())
        S.add(12, sec, f"Anomalous vs. peers (mean robust-z deficit >= {opts['anomaly_threshold']})", int((df["anomalous_vs_peers"] == 1).sum()), ev)
        both = (df["anomalous_vs_peers"] == 1) & (df["triage_tier"] == "High")
        S.add(12, sec, "Anomalous AND High tier (strongest candidates)", int(both.sum()), ev)
        S.add(12, sec, "Peer groups used", df.loc[content, "peer_group"].nunique(), value="; ".join(sorted(df.loc[content, "peer_group"].unique())[:12]))
    if "iforest_score" in df.columns:
        S.add(12, sec, "Isolation Forest scores computed", int(df["iforest_score"].notna().sum()), NC, note="Rank in institution_flags (iforest_rank).")

    # 13 Physical presence
    sec = "13. Physical-presence and contact signals"
    if NC:
        if "phone_number_hits" in df.columns:
            S.add(13, sec, "Phone number on homepage", int((df.loc[content, "phone_number_hits"] > 0).sum()), NC)
        if "has_street_address" in df.columns:
            S.add(13, sec, "Street address with ZIP on homepage", int((df.loc[content, "has_street_address"] == 1).sum()), NC)
        if "mailto_count" in df.columns:
            S.add(13, sec, "Any mailto: link on homepage", int((df.loc[content, "mailto_count"] > 0).sum()), NC)
        if "flag_no_physical_presence" in df.columns:
            S.add(13, sec, "Neither phone nor street address", int((df["flag_no_physical_presence"] == 1).sum()), int(df["flag_no_physical_presence"].notna().sum()))
        if "flag_free_or_offdomain_email" in df.columns:
            f = df["flag_free_or_offdomain_email"]
            S.add(13, sec, "Free-mail or off-domain contact email", int((f == 1).sum()), int(f.notna().sum()))
        elif schema == "legacy_home_only":
            S.add(13, sec, "Free-mail contact email", note="Not reported: the current home-only crawl hard-codes free_email_hits to 0 (spec C01).")
        if "flag_stale_copyright" in df.columns:
            f = df["flag_stale_copyright"]
            S.add(13, sec, f"Copyright year {opts['stale_copyright_years']}+ years old", int((f == 1).sum()), int(f.notna().sum()))
        elif schema == "legacy_home_only":
            S.add(13, sec, "Copyright-year staleness", note="Not reported: copyright years are parsed but not saved in home-only mode (spec C01).")
        if "title_name_overlap" in df.columns:
            t = df["title_name_overlap"]
            S.add(13, sec, "Page title / site name shares under a third of the institution's name words", int((t < 0.34).sum()), int(t.notna().sum()))

    # 14 Thin homepages
    sec = "14. Thin homepages"
    if NC:
        w = df.loc[content, "words_per_page"]
        S.add(14, sec, "Median words on homepage", value=round(float(w.median()), 0))
        S.add(14, sec, "Homepages under 100 words", int((w < 100).sum()), NC)
        S.add(14, sec, f"Bottom {opts['thin_percentile']:g}% of peer group (flagged)", int((df["flag_thin_homepage"] == 1).sum()), int(df["flag_thin_homepage"].notna().sum()))
        if "total_internal_links" in df.columns:
            S.add(14, sec, "Fewer than 5 internal links", int((df.loc[content, "internal_links_per_page"] < 5).sum()), NC)

    # 15 Builders
    sec = "15. Consumer site builders"
    if "site_builders_detected" in df.columns and NC:
        S.add(15, sec, "Consumer builder (Wix, Squarespace, Weebly, GoDaddy)", int((df.loc[content, "uses_site_builder"] == 1).sum()) if "uses_site_builder" in df.columns else None, NC)
        bc = Counter(b for L in df.loc[content, "site_builders_detected"] for b in L)
        for b, n in bc.most_common():
            S.add(15, sec, f"  {b}", n, NC)

    # 16 Link-outs
    sec = "16. Institutional link-outs"
    if NC:
        if "trusted_outbound_links" in df.columns:
            S.add(16, sec, "No links to accreditors or .gov sites", int((df.loc[content, "trusted_outbound_links"] == 0).sum()), NC,
                  note="Weak alone: many legitimate homepages do not link their accreditor.")
        if "num_subdomains_seen" in df.columns:
            s = df.loc[content, "num_subdomains_seen"]
            S.add(16, sec, "Links to 1 or fewer of its own hostnames", int((s <= 1).sum()), NC)
            S.add(16, sec, "Median own hostnames linked", value=float(s.median()))
        if "flag_unrecognized_accreditor" in df.columns:
            f = df["flag_unrecognized_accreditor"]
            S.add(16, sec, "Links an unrecognized 'accreditation' domain", int((f == 1).sum()), int(f.notna().sum()))
        if "recognized_accreditor_domains_linked" in df.columns:
            S.add(16, sec, "Links a recognized accreditor", int((df.loc[content, "recognized_accreditor_domains_linked"].map(len) > 0).sum()), NC)

    # 17 Tracking
    sec = "17. Recruitment-funnel tracking"
    if "has_fb_pixel" in df.columns and NC:
        S.add(17, sec, "Meta (Facebook) pixel on homepage", int((df["has_fb_pixel"] == 1).sum()), NC)
        if "flag_no_physical_presence" in df.columns:
            both = (df["has_fb_pixel"] == 1) & (df["flag_no_physical_presence"] == 1)
            S.add(17, sec, "Pixel AND no phone or street address", int(both.sum()), NC)
        if "gtm_ids" in df.columns:
            S.add(17, sec, "Google Tag Manager container present", int((df.loc[content, "gtm_ids"].map(len) > 0).sum()), NC)

    # 18 Metadata
    sec = "18. Metadata completeness"
    if "metadata_index" in df.columns and NC:
        mi = df["metadata_index"]
        mx = int(df["metadata_index_max"].iloc[0])
        S.add(18, sec, f"Mean metadata index (0-{mx})", value=round(float(mi.mean()), 2))
        S.add(18, sec, "Index of 0 or 1", int((mi <= 1).sum()), int(mi.notna().sum()))
        for c, lab in [("has_favicon", "Favicon"), ("pct_pages_meta_description", "Meta description"), ("pct_pages_og_tags", "Open Graph tags"),
                       ("pct_pages_canonical", "Canonical link"), ("has_edu_schema_markup", "schema.org education markup")]:
            if c in df.columns:
                S.add(18, sec, f"  {lab}", int((df.loc[content, c] > 0).sum()), NC)

    # 19 Alt text
    sec = "19. Image alt-text coverage"
    if "img_alt_coverage" in df.columns and df["img_alt_coverage"].notna().any():
        a = df["img_alt_coverage"].where(content)
        S.add(19, sec, "Median alt-text coverage (%)", value=round(100 * float(a.median()), 1))
        S.add(19, sec, "Under 50% of images have alt text", int((a < 0.5).sum()), int(a.notna().sum()))

    # 20 CMS
    sec = "20. CMS, generators and frameworks"
    if NC and "meta_generators" in df.columns:
        gc = Counter(re.split(r"[\s/]", str(g))[0].lower() for L in df.loc[content, "meta_generators"] for g in L)
        S.add(20, sec, "Sites declaring a meta generator", int((df.loc[content, "meta_generators"].map(len) > 0).sum()), NC,
              value="; ".join(f"{k} ({v})" for k, v in gc.most_common(8)))
    if NC and "css_frameworks" in df.columns:
        fc = Counter(f for L in df.loc[content, "css_frameworks"] for f in L)
        S.add(20, sec, "Sites using a CSS framework", int((df.loc[content, "css_frameworks"].map(len) > 0).sum()), NC,
              value="; ".join(f"{k} ({v})" for k, v in fc.most_common()))

    # 21 HTTP landing
    sec = "21. Plain-HTTP landing"
    if "https_redirect" in df.columns and df["https_redirect"].notna().any():
        h = df["https_redirect"]
        S.add(21, sec, "Final page served over plain HTTP", int((h == 0).sum()), int(h.notna().sum()))

    # 22 Supervised readiness
    sec = "22. Supervised model readiness"
    if validation is not None and not validation.empty:
        nb = validation.groupby("label")["n_labelled"].max()
        for lab, n in nb.items():
            S.add(22, sec, f"Labelled institutions: {lab}", int(n), note="Roughly 50+ labelled bad schools are needed before a supervised model is worth fitting.")
    else:
        S.add(22, sec, "No labels supplied", note="A supervised model needs labelled known-bad institutions.")

    # 23 Coverage
    S.add(23, "23. Data coverage", "See the Coverage and Run_info tabs", note="Field completeness and known caveats. Read the Run_info warnings before quoting numbers.")

    # 24 Deep crawl
    sec = "24. Deep-crawl disclosures"
    for c, lab in [("has_net_price_calculator_link", "Net price calculator link"), ("has_consumer_info_link", "Consumer information link"),
                   ("has_security_report_link", "Annual security report link"), ("has_accreditation_page", "Accreditation page")]:
        if c in df.columns and df[c].notna().any():
            S.add(24, sec, lab, int((df[c] == 1).sum()), int(df[c].notna().sum()),
                  note="Net price calculator applies only to schools enrolling first-time, full-time undergraduates." if "net_price" in c else "")

    return S.frame()


# --------------------------------------------------------------------------
# Supporting tables
# --------------------------------------------------------------------------
DIST_COLUMNS = {
    "words_per_page": "Words per page", "a_tags_per_page": "Links per page", "internal_links_per_page": "Internal links per page",
    "external_links_per_page": "External links per page", "images_per_page": "Images per page", "html_kb_per_page": "HTML KB per page",
    "text_to_html_ratio": "Text-to-HTML ratio", "img_alt_coverage": "Alt-text coverage", "num_subdomains_seen": "Own hostnames linked",
    "trusted_links_per_page": "Accreditor/.gov links per page", "phone_number_hits": "Phone numbers", "mailto_count": "mailto links",
    "pdf_link_count": "PDF links", "n_tracker_ids": "Tracker IDs", "metadata_index": "Metadata index", "domain_age_years": "Domain age (years)",
    "ssl_days_to_expiry": "Days to certificate expiry", "ssl_validity_days": "Certificate lifetime (days)", "response_time_ms": "Response time (ms)",
    "copyright_staleness_years": "Copyright staleness (years)", "red_flag_score": "Red-flag score", "anomaly_score": "Anomaly score",
}


def distributions(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    groups = [("All institutions", pd.Series(True, index=df.index))]
    if "inst_ownership_label" in df.columns:
        for g in sorted(df["inst_ownership_label"].dropna().unique()):
            groups.append((g, df["inst_ownership_label"] == g))
    for col, label in DIST_COLUMNS.items():
        if col not in df.columns:
            continue
        for gname, gm in groups:
            x = df.loc[gm, col].dropna()
            if x.empty:
                continue
            q = x.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
            rows.append({"metric": label, "column": col, "group": gname, "n": len(x), "mean": round(x.mean(), 3),
                         "p5": round(q[0.05], 3), "p25": round(q[0.25], 3), "median": round(q[0.5], 3),
                         "p75": round(q[0.75], 3), "p95": round(q[0.95], 3)})
    return pd.DataFrame(rows)


def crosstabs(df: pd.DataFrame, fcols: list) -> dict:
    out = {}
    by = "inst_ownership_label" if "inst_ownership_label" in df.columns else None
    def xt(row):
        if by is None:
            t = df[row].value_counts().rename("count").to_frame()
            t["percent"] = (100 * t["count"] / t["count"].sum()).round(1)
            return t.reset_index().rename(columns={"index": row})
        t = pd.crosstab(df[row], df[by].fillna("Unknown"), margins=True, margins_name="Total")
        return t.reset_index()
    out["availability"] = xt("availability_label")
    out["tld"] = xt("tld_category")
    out["tier"] = xt("triage_tier")
    if by:
        rates = []
        for c in fcols:
            r = df.groupby(df[by].fillna("Unknown"))[c].agg(lambda s: round(100 * s.mean(), 1) if s.notna().any() else None)
            r["All"] = round(100 * df[c].mean(), 1) if df[c].notna().any() else None
            r.name = FLAG_DEFINITIONS[c][0]
            rates.append(r)
        if rates:
            out["flag_rates_pct"] = pd.DataFrame(rates).reset_index().rename(columns={"index": "flag"})
    if "whois_privacy_protected" in df.columns and df["whois_privacy_protected"].notna().any():
        t = df.groupby("tld_category")["whois_privacy_protected"].agg(["count", "mean"]).reset_index()
        t["mean"] = (100 * t["mean"]).round(1)
        out["whois_privacy_by_tld"] = t.rename(columns={"count": "n_with_whois", "mean": "percent_privacy_or_redacted"})
    if "domain_age_days" in df.columns:
        t = df.groupby("tld_category")["domain_age_days"].agg(lambda s: round(100 * s.notna().mean(), 1)).reset_index()
        out["whois_coverage_by_tld"] = t.rename(columns={"domain_age_days": "percent_with_creation_date"})
    return out


def top_values(df: pd.DataFrame) -> pd.DataFrame:
    content = df["has_content"] == 1
    rows = []
    def add(field, counter, denom):
        for v, n in counter.most_common(25):
            rows.append({"field": field, "value": v, "n_sites": n, "percent": pct(n, denom)})
    nc = int(content.sum())
    for col in ["site_builders_detected", "css_frameworks", "meta_generators", "server_headers", "social_platforms_linked"]:
        if col in df.columns:
            add(col, Counter(x for L in df.loc[content, col] for x in L), nc)
    for col in ["ssl_issuer_org", "whois_registrant_org", "whois_registrar", "parking_provider", "cdn_vendor",
                "bot_protection_vendor", "final_registered_domain", "server_header"]:
        if col in df.columns:
            vals = df[col].dropna().astype(str)
            add(col, Counter(vals), len(df))
    if "unrecognized_accredit_domains" in df.columns:
        add("unrecognized_accredit_domains", Counter(x for L in df["unrecognized_accredit_domains"] for x in L), len(df))
    return pd.DataFrame(rows)


COVERAGE_NOTES_LEGACY = {
    "free_email_hits": "Hard-coded to 0 in the current home-only crawl (spec C01). Not used.",
    "max_copyright_year": "Not saved in home-only mode (spec C01).",
    "mailto_on_domain_ratio": "Not saved in home-only mode (spec C01).",
    "domain_age_days": "Erased on failed crawls (spec C03).",
    "whois_privacy_protected": "Includes default GDPR redaction; mostly reflects TLD (spec C12).",
    "ssl_org_validated": "Reads False for every site when crawled with --insecure (spec C02).",
    "n_sites_sharing_tracker_id": "Precomputed value is unreliable (spec C04); recomputed here.",
    "max_structure_similarity_other_site": "Always 0 in home-only mode (spec C04); recomputed here.",
    "n_sites_sharing_css_file": "CSS not fetched in home-only mode.",
}


def coverage(df: pd.DataFrame, cols_in: set, schema: str) -> pd.DataFrame:
    content = df["has_content"] == 1
    watch = (["crawl_error", "landing_status_code", "tld", "registered_domain"] + NUMERIC_COLUMNS + BOOL_COLUMNS
             + TRACKER_COLUMNS + ["structure_hashes", "site_builders_detected", "meta_generators", "ssl_issuer_org", "ssl_cert_error",
                                  "whois_registrant_org", "page_title", "final_url", "error_category", "phone_numbers",
                                  "contact_emails", "text_simhash64", "html_sha256", "ssl_cert_sha256",
                                  "n_sites_sharing_tracker_id", "max_structure_similarity_other_site", "n_sites_sharing_css_file"])
    rows = []
    seen = set()
    for c in watch:
        if c in seen:
            continue
        seen.add(c)
        legacy_name = next((o for o, n in LEGACY_ALIASES.items() if n == c), None)
        present = c in cols_in or (legacy_name in cols_in if legacy_name else False) or (c == "structure_hashes" and "structure_hash" in cols_in)
        if c in df.columns:
            s = df[c]
            nn = s.map(lambda v: (len(v) > 0) if isinstance(v, list) else not _isnan(v))
            all_pct = pct(int(nn.sum()), len(df))
            c_pct = pct(int(nn[content].sum()), int(content.sum())) if content.any() else None
        else:
            all_pct = c_pct = None
        note = COVERAGE_NOTES_LEGACY.get(c, "") if schema == "legacy_home_only" else ""
        if not present:
            note = (note + " " if note else "") + "Not in this file."
        rows.append({"column": c, "present_in_file": present, "non_null_pct_all": all_pct,
                     "non_null_pct_sites_with_content": c_pct, "note": note.strip()})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------
PALETTE = {
    "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
    "grid": "#e1e0d9", "axis": "#c3c2b7",
    "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
    "good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b",
    "seq_light": "#86b6ef", "seq_dark": "#184f95",
}
GROUP_COLORS = {"reachable": PALETTE["good"], "blocked": PALETTE["warning"], "broken_http": PALETTE["critical"],
                "unreachable_server": PALETTE["critical"], "dns_dead": PALETTE["critical"], "tls_failure": PALETTE["critical"],
                "bad_url": PALETTE["critical"], "unknown": PALETTE["muted"]}
STATE_LEGEND = [("Reachable", PALETTE["good"]), ("Alive, blocks automated access", PALETTE["warning"]),
                ("Broken (counted in the headline)", PALETTE["critical"]), ("Unclassified error", PALETTE["muted"])]


def make_charts(df, fcols, out_dir: Path, opts, warn):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        warn.append("Charts skipped: matplotlib not installed.")
        return []
    cdir = out_dir / "charts"
    cdir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({
        "figure.facecolor": PALETTE["surface"], "axes.facecolor": PALETTE["surface"], "savefig.facecolor": PALETTE["surface"],
        "axes.edgecolor": PALETTE["axis"], "axes.labelcolor": PALETTE["ink2"], "xtick.color": PALETTE["ink2"],
        "ytick.color": PALETTE["ink2"], "text.color": PALETTE["ink"], "font.size": 10, "axes.titlesize": 12,
        "axes.titleweight": "bold", "axes.titlelocation": "left", "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "axes.axisbelow": True, "grid.color": PALETTE["grid"], "grid.linewidth": 0.6,
        "font.family": ["system-ui", "Segoe UI", "Helvetica", "Arial", "DejaVu Sans", "sans-serif"],
    })
    warnings.filterwarnings("ignore", message=".*findfont.*")
    import logging
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    made = []
    N = len(df)

    def finish(fig, name, subtitle=None):
        if subtitle:
            fig.text(0.01, 0.005, subtitle, fontsize=8, color=PALETTE["muted"], ha="left", va="bottom")
        fig.tight_layout(rect=(0, 0.03 if subtitle else 0, 1, 1))
        p = cdir / name
        fig.savefig(p, dpi=160)
        plt.close(fig)
        made.append(str(p))

    # 1 availability
    cats = df.groupby(["availability_group", "availability_category"]).size().reset_index(name="n")
    cats["g_order"] = cats["availability_group"].map({g: i for i, g in enumerate(GROUP_ORDER)})
    cats = cats.sort_values(["g_order", "n"], ascending=[False, True])
    fig, ax = plt.subplots(figsize=(8.5, 0.38 * len(cats) + 1.4))
    y = np.arange(len(cats))
    ax.barh(y, cats["n"], height=0.62, color=[GROUP_COLORS[g] for g in cats["availability_group"]])
    ax.set_yticks(y, [CATEGORY_LABELS.get(c, c) for c in cats["availability_category"]])
    for yi, n in zip(y, cats["n"]):
        ax.text(n, yi, f"  {n:,} ({100 * n / N:.1f}%)", va="center", fontsize=8.5, color=PALETTE["ink2"])
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, cats["n"].max() * 1.25)
    broken = int(df["is_broken"].sum())
    ax.set_title(f"{broken:,} of {N:,} institution URLs ({100 * broken / N:.1f}%) are broken")
    ax.set_xlabel("Institutions")
    from matplotlib.patches import Patch
    used = {GROUP_COLORS[g] for g in cats["availability_group"]}
    handles = [Patch(color=c, label=l) for l, c in STATE_LEGEND if c in used]
    ax.legend(handles=handles, loc="lower right", frameon=False, fontsize=8)
    finish(fig, "availability.png", "403/429/503 answers count as alive: those sites block automated visitors rather than being down.")

    # 2 red-flag score distribution
    reach = df["availability_group"].isin(["reachable", "blocked"])
    sc = df.loc[reach, "red_flag_score"].value_counts().sort_index()
    if not sc.empty:
        fig, ax = plt.subplots(figsize=(7.5, 3.8))
        colors = [PALETTE["seq_dark"] if s >= opts["high_threshold"] else PALETTE["seq_light"] for s in sc.index]
        ax.bar(sc.index.astype(int), sc.values, width=0.7, color=colors)
        for x, v in zip(sc.index, sc.values):
            ax.text(x, v, f"{v:,}", ha="center", va="bottom", fontsize=8.5, color=PALETTE["ink2"])
        hi = int(sc[sc.index >= opts["high_threshold"]].sum())
        ax.set_title(f"{hi:,} reachable sites raise {opts['high_threshold']}+ red flags (High tier)")
        ax.set_xlabel("Red flags raised")
        ax.set_ylabel("Sites")
        ax.set_xticks(range(0, int(sc.index.max()) + 1))
        ax.grid(axis="x", visible=False)
        finish(fig, "red_flag_scores.png", "Dark bars = High tier. Reachable and bot-blocking sites only.")

    # 3 flag prevalence
    rates = []
    for c in fcols:
        ev = df[c].notna()
        if ev.sum():
            rates.append((FLAG_DEFINITIONS[c][0], 100 * df.loc[ev, c].mean(), int((df[c] == 1).sum()), int(ev.sum())))
    if rates:
        rates.sort(key=lambda r: r[1])
        fig, ax = plt.subplots(figsize=(8.5, 0.4 * len(rates) + 1.3))
        y = np.arange(len(rates))
        ax.barh(y, [r[1] for r in rates], height=0.62, color=PALETTE["series"][0])
        ax.set_yticks(y, [r[0] for r in rates])
        for yi, r in zip(y, rates):
            ax.text(r[1], yi, f"  {r[1]:.1f}%  ({r[2]:,} of {r[3]:,})", va="center", fontsize=8.5, color=PALETTE["ink2"])
        ax.set_xlim(0, max(r[1] for r in rates) * 1.45 + 1)
        ax.set_xlabel("Percent of institutions where the flag could be evaluated")
        ax.grid(axis="y", visible=False)
        ax.set_title(f"Most common flag: {rates[-1][0]} ({rates[-1][1]:.1f}%)")
        finish(fig, "flag_prevalence.png")

    # 4 TLD mix
    order = [".edu", ".org", ".com / .net", ".gov / state .us", "other", "unknown"]
    colmap = dict(zip(order, PALETTE["series"][:5] + [PALETTE["muted"]]))
    if "inst_ownership_label" in df.columns:
        t = pd.crosstab(df["inst_ownership_label"].fillna("Unknown"), df["tld_category"], normalize="index") * 100
        t = t[[c for c in order if c in t.columns]]
        fig, ax = plt.subplots(figsize=(8.5, 0.55 * len(t) + 1.8))
        left = np.zeros(len(t))
        for c in t.columns:
            ax.barh(t.index, t[c], left=left, height=0.6, color=colmap[c], label=c, edgecolor=PALETTE["surface"], linewidth=1.5)
            for i, (l, v) in enumerate(zip(left, t[c])):
                if v >= 7:
                    ax.text(l + v / 2, i, f"{v:.0f}%", ha="center", va="center", fontsize=8, color="#0b0b0b" if c in (".com / .net", ".gov / state .us", "other") else "white")
            left += t[c].values
        ax.set_xlim(0, 100)
        ax.set_xlabel("Percent of institutions")
        ax.grid(axis="y", visible=False)
        ax.legend(ncol=min(6, len(t.columns)), loc="upper center", bbox_to_anchor=(0.5, -0.22), frameon=False, fontsize=8)
        edu = 100 * (df["tld_category"] == ".edu").mean()
        ax.set_title(f"{edu:.0f}% of institutions are on .edu; the rest vary by ownership")
        finish(fig, "tld_mix.png")
    else:
        vc = df["tld_category"].value_counts().reindex([c for c in order if c in set(df["tld_category"])])
        fig, ax = plt.subplots(figsize=(7.5, 3.4))
        ax.barh(vc.index[::-1], vc.values[::-1], height=0.6, color=PALETTE["series"][0])
        for i, v in enumerate(vc.values[::-1]):
            ax.text(v, i, f"  {v:,} ({100 * v / N:.1f}%)", va="center", fontsize=8.5, color=PALETTE["ink2"])
        ax.set_xlim(0, vc.max() * 1.3)
        ax.grid(axis="y", visible=False)
        ax.set_title(f"{100 * vc.get('.edu', 0) / N:.0f}% of institution sites are on .edu")
        finish(fig, "tld_mix.png")

    # 5 domain age
    a = df["domain_age_years"].dropna()
    if len(a) >= 10:
        bins = [0, 1, 3, 5, 10, 20, 200]
        labels = ["< 1", "1-3", "3-5", "5-10", "10-20", "20+"]
        cut = pd.cut(a, bins=bins, labels=labels, right=False).value_counts().reindex(labels)
        fig, ax = plt.subplots(figsize=(7.5, 3.6))
        colors = [PALETTE["series"][1] if l in ("< 1", "1-3") else PALETTE["series"][0] for l in labels]
        ax.bar(labels, cut.values, width=0.7, color=colors)
        for x, v in enumerate(cut.values):
            ax.text(x, v, f"{v:,}", ha="center", va="bottom", fontsize=8.5, color=PALETTE["ink2"])
        young = int((a < opts["young_domain_years"]).sum())
        ax.set_title(f"{young:,} of {len(a):,} domains are under {opts['young_domain_years']:g} years old")
        ax.set_xlabel("Domain age (years)")
        ax.set_ylabel("Institutions")
        ax.grid(axis="x", visible=False)
        finish(fig, "domain_age.png", f"Orange = younger than 3 years (young-domain flag uses {opts['young_domain_years']:g}). Institutions with a WHOIS creation date only.")

    # 6 words vs enrollment
    if "words_vs_enrollment_resid_z" in df.columns:
        m = df["words_vs_enrollment_resid_z"].notna()
        sub = df[m]
        out = sub["low_size_for_enrollment"] == 1
        fig, ax = plt.subplots(figsize=(7.5, 5))
        ax.scatter(sub.loc[~out, "inst_enrollment"], sub.loc[~out, "words_per_page"], s=10, alpha=0.45,
                   color=PALETTE["series"][0], linewidths=0, label="Within expected range")
        ax.scatter(sub.loc[out, "inst_enrollment"], sub.loc[out, "words_per_page"], s=26, color=PALETTE["series"][1],
                   edgecolors=PALETTE["surface"], linewidths=1.2, label="Far smaller than enrollment predicts")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("Undergraduate enrollment (log scale)")
        ax.set_ylabel("Words on homepage (log scale)")
        ax.legend(frameon=False, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.13), ncol=2)
        ax.set_title(f"{int(out.sum()):,} homepages are far thinner than enrollment predicts")
        finish(fig, "words_vs_enrollment.png", f"Outlier = robust residual z < {opts['enrollment_residual_z']} from a log-log fit.")
    return made


# --------------------------------------------------------------------------
# Excel writer
# --------------------------------------------------------------------------
def _excel_safe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object:
            out[c] = out[c].map(lambda v: "; ".join(map(str, v[:50])) if isinstance(v, (list, tuple, set)) else v)
            out[c] = out[c].map(lambda v: v[:32000] if isinstance(v, str) else v)
    return out


def write_excel(path: Path, sheets: list, warn: list):
    try:
        import openpyxl  # noqa: F401
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        warn.append("Excel workbook skipped: openpyxl not installed (CSV files were written).")
        return False
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, frame in sheets:
            if frame is None or (isinstance(frame, pd.DataFrame) and frame.empty):
                continue
            _excel_safe(frame).to_excel(xw, sheet_name=name[:31], index=False)
            ws = xw.sheets[name[:31]]
            ws.freeze_panes = "A2"
            hdr_fill = PatternFill("solid", fgColor="F0EFEC")
            for cell in ws[1]:
                cell.font = Font(bold=True)
                cell.fill = hdr_fill
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            for i, col in enumerate(frame.columns, start=1):
                vals = [str(col)] + [str(v) for v in frame[col].head(200).tolist()]
                width = min(60, max(8, int(np.percentile([len(v) for v in vals], 90)) + 2))
                ws.column_dimensions[get_column_letter(i)].width = width
            ws.auto_filter.ref = ws.dimensions
    return True


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------
def build_stats(site_features, institutions=None, known_bad=None, out_dir="stats_output",
                id_col="UNITID", url_col="school.school_url", column_map=None,
                charts=True, excel=True, iforest=False, **overrides) -> dict:
    opts = {**DEFAULTS, **{k: v for k, v in overrides.items() if v is not None}}
    warn: list = []
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    df, schema, cols_in = load_site_features(site_features, id_col, url_col, warn)

    # schema-specific warnings
    if schema == "legacy_home_only":
        warn.append("Schema: current home-only output. free_email_hits (hard-coded 0), copyright years and mailto_on_domain_ratio are treated as not captured; cross-site sharing is recomputed from raw lists (spec C01, C04).")
        if "crawl_error" in df.columns:
            failed = df["crawl_error"].notna()
            if "domain_age_days" in df.columns and failed.any() and df.loc[failed, "domain_age_days"].notna().sum() == 0:
                warn.append("Failed crawls have no WHOIS/SSL data (erased by the crawler, spec C03).")
    if "ssl_ok" in df.columns and "ssl_issuer_org" in df.columns:
        ok = df["ssl_ok"] == 1
        if ok.sum() and df.loc[ok, "ssl_issuer_org"].isna().mean() > 0.5:
            warn.append("Most certificate details are empty although handshakes succeeded: the crawl probably ran with --insecure (spec C02). OV/DV share not reported.")

    # institutions join
    inst_info = {}
    label_sets = {}
    if institutions is not None:
        inst, colmap = load_institutions(institutions, column_map or {}, warn)
        before = len(df)
        df = df.merge(inst, left_on=id_col, right_on="_id", how="left").drop(columns=["_id"])
        assert len(df) == before
        matched = int(df[[c for c in df.columns if c.startswith("inst_")]].notna().any(axis=1).sum()) if any(c.startswith("inst_") for c in df.columns) else 0
        warn.append(f"Institutions file: matched {matched} of {len(df)} crawled UNITIDs.")
        inst_info["n_inst"] = len(inst)
        if "inst_url" in inst.columns:
            no_url = inst["inst_url"].isna() | (inst["inst_url"].astype(str).str.strip().isin(["", "nan", "None"]))
            inst_info["n_no_url"] = int(no_url.sum())
            inst_info["n_not_crawled"] = int((~no_url & ~inst["_id"].isin(df[id_col])).sum())
        if "inst_ownership" in df.columns:
            df["inst_ownership_label"] = df["inst_ownership"].map(OWNERSHIP_LABELS)
        if "inst_pred_degree" in df.columns:
            df["inst_pred_degree_label"] = df["inst_pred_degree"].map(PREDDEG_LABELS)
        if "inst_hcm2" in df.columns and df["inst_hcm2"].notna().any():
            label_sets["Heightened Cash Monitoring 2 (HCM2)"] = (df["inst_hcm2"] == 1).astype(float).where(df["inst_hcm2"].notna())
        if "inst_operating" in df.columns and df["inst_operating"].notna().any() and (df["inst_operating"] == 0).any():
            label_sets["Not currently operating"] = (df["inst_operating"] == 0).astype(float).where(df["inst_operating"].notna())
    if known_bad is not None:
        kb = load_known_bad(known_bad, warn)
        for lab, sub in kb.groupby("label"):
            label_sets[str(lab)] = df[id_col].isin(sub["_id"]).astype(float)

    df = add_availability(df)
    df = add_metrics(df, opts)
    df["peer_group"] = assign_peer_groups(df, opts["min_peer_n"])
    df, clusters, edges, excluded = run_cross_site(df, opts, id_col)
    shared = shared_url_groups(df, id_col, url_col)
    df, fcols = build_flags(df, opts, schema, {})
    df = add_anomaly(df.copy(), opts, iforest, warn)
    df = df.copy()
    enroll_fit = add_enrollment_residual(df, opts)
    df = df.copy()
    validation = build_validation(df, fcols, label_sets, warn) if label_sets else pd.DataFrame()

    summary = build_summary(df, fcols, clusters, shared, excluded, validation, enroll_fit, opts, schema, id_col, url_col, inst_info)

    # availability table
    av = (df.groupby(["availability_group", "availability_category"]).size().reset_index(name="count"))
    av["group_label"] = av["availability_group"].map(GROUP_LABELS)
    av["category_label"] = av["availability_category"].map(CATEGORY_LABELS)
    av["percent"] = (100 * av["count"] / len(df)).round(1)
    av["counts_as_broken"] = av["availability_group"].isin(BROKEN_GROUPS)
    av["g"] = av["availability_group"].map({g: i for i, g in enumerate(GROUP_ORDER)})
    av = av.sort_values(["g", "count"], ascending=[True, False]).drop(columns="g")[
        ["group_label", "category_label", "count", "percent", "counts_as_broken", "availability_group", "availability_category"]]

    # institution table
    lead = [id_col, url_col, "inst_name", "inst_state", "inst_ownership_label", "inst_pred_degree_label", "inst_enrollment", "inst_ope6",
            "registered_domain", "tld", "tld_category", "availability_label", "availability_category", "is_broken",
            "landing_status_code", "crawl_error", "triage_tier", "red_flag_score", "n_flags_evaluated", "flag_reasons"]
    metrics = ["words_per_page", "thin_threshold_words", "peer_group", "internal_links_per_page", "images_per_page",
               "num_subdomains_seen", "trusted_outbound_links", "phone_number_hits", "has_street_address", "mailto_count",
               "n_tracker_ids", "has_fb_pixel", "metadata_index", "domain_age_years", "ssl_ok", "ssl_bypass_used",
               "whois_registrant_org", "page_title", "title_name_overlap", "final_url", "redirected_off_domain",
               "copyright_staleness_years", "anomaly_score", "anomaly_top_features", "anomalous_vs_peers",
               "words_vs_enrollment_resid_z", "low_size_for_enrollment", "iforest_score", "iforest_rank",
               "shared_contact_or_infra_types"]
    share_cols = [c for c in df.columns if c.startswith("n_cross_owner_domains_sharing_") or c.endswith("_cluster_id")]
    cols = [c for c in lead + fcols + metrics + share_cols if c in df.columns]
    inst_table = df[cols].sort_values(["is_broken", "red_flag_score"], ascending=[True, False])

    # candidates
    cluster_member = pd.Series(False, index=df.index)
    for kt in ["tracker_id", "template", "phone", "email", "street_address", "ssl_cert", "html_exact", "near_dup_text", "social_profile", "redirect_target"]:
        c = f"n_cross_owner_domains_sharing_{kt}"
        if c in df.columns:
            cluster_member |= df[c] > 0
    reasons = []
    for i in df.index:
        r = []
        if df.at[i, "triage_tier"] == "High":
            r.append("High red-flag tier")
        if cluster_member.at[i]:
            r.append("Cross-owner cluster member")
        if "anomalous_vs_peers" in df.columns and df.at[i, "anomalous_vs_peers"] == 1 and df.at[i, "triage_tier"] == "Medium":
            r.append("Medium tier and anomalous vs. peers")
        reasons.append("; ".join(r))
    df["review_reason"] = reasons
    crawlable = df["availability_group"].isin(["reachable", "blocked"])
    cand_cols = [c for c in [id_col, url_col, "inst_name", "registered_domain", "review_reason", "triage_tier", "red_flag_score",
                             "flag_reasons", "anomaly_score", "tracker_id_cluster_id", "template_cluster_id",
                             "shared_contact_or_infra_types"] if c in df.columns]
    deep = df.loc[crawlable & (df["review_reason"] != ""), cand_cols].sort_values(["red_flag_score"], ascending=False)
    ver_cols = [c for c in [id_col, url_col, "inst_name", "registered_domain", "availability_label", "availability_category",
                            "crawl_error", "red_flag_score", "flag_reasons", "domain_age_years"] if c in df.columns]
    verify = df.loc[df["is_broken"] == 1, ver_cols].copy()
    verify["suggested_check"] = verify["availability_category"].map({
        "dns_failure": "Domain no longer resolves: check Scorecard operating status, Wayback Machine history, current WHOIS.",
        "connection_refused": "Retry over http:// and www.; then check operating status.",
        "connect_timeout": "Retry over http:// and www.; then check operating status.",
        "read_timeout": "Retry later; slow server.",
        "ssl_error": "Retry over http://; certificate or TLS configuration is broken.",
        "http_404": "URL on record points to a missing page; check the root domain.",
        "invalid_url": "URL on record is malformed; correct the source record.",
        "connection_reset": "Retry over http:// and www.; server dropped the connection.",
        "http_410": "Page reported as permanently gone; check operating status.",
        "http_5xx": "Server error; retry later, then check operating status.",
        "too_many_redirects": "Redirect loop; inspect the redirect chain by hand.",
        "network_unreachable": "Retry from another network.",
    }).fillna("Manual check")
    verify = verify.sort_values("red_flag_score", ascending=False)

    dist = distributions(df)
    xts = crosstabs(df, fcols)
    tops = top_values(df)
    cov = coverage(df, cols_in, schema)

    ts = pd.to_datetime(df["crawl_timestamp"], errors="coerce", utc=True) if "crawl_timestamp" in df.columns else pd.Series(dtype="datetime64[ns, UTC]")
    run_info = pd.DataFrame([
        {"item": "Script", "value": f"build_stats_sheet.py {SCRIPT_VERSION}"},
        {"item": "Run at (UTC)", "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")},
        {"item": "Input", "value": str(site_features) if not isinstance(site_features, pd.DataFrame) else "DataFrame"},
        {"item": "schema", "value": schema},
        {"item": "Institutions (rows)", "value": len(df)},
        {"item": "Crawl dates", "value": f"{ts.min():%Y-%m-%d} to {ts.max():%Y-%m-%d}" if ts.notna().any() else "unknown"},
        {"item": "Institutions file", "value": str(institutions) if institutions else "not provided"},
        {"item": "Known-bad file", "value": str(known_bad) if known_bad else "not provided"},
        {"item": "Flags evaluated", "value": ", ".join(fcols)},
        {"item": "Options", "value": json.dumps(opts)},
    ] + [{"item": "Warning", "value": w} for w in warn])

    flag_defs = pd.DataFrame([{"flag_column": c, "label": FLAG_DEFINITIONS[c][0],
                               "definition": FLAG_DEFINITIONS[c][1].format(**opts), "requires": FLAG_DEFINITIONS[c][2],
                               "active_in_this_run": c in fcols} for c in FLAG_DEFINITIONS])

    cluster_frames = {k: v for k, v in clusters.items() if v is not None and not v.empty}

    # ---- write CSVs ----
    def w(frame, name):
        if frame is not None and not frame.empty:
            _excel_safe(frame).to_csv(out / name, index=False)
    w(summary, "summary.csv")
    w(inst_table, "institution_flags.csv")
    w(deep, "deep_dive_candidates.csv")
    w(verify, "verify_status_candidates.csv")
    w(av, "availability.csv")
    w(validation, "validation.csv")
    for k, v in cluster_frames.items():
        w(v, f"clusters_{k}.csv")
    w(edges, "cross_site_edges.csv")
    w(excluded, "excluded_shared_keys.csv")
    w(shared, "shared_urls.csv")
    w(dist, "distributions.csv")
    for k, v in xts.items():
        w(v, f"crosstab_{k}.csv")
    w(tops, "top_values.csv")
    w(cov, "coverage.csv")
    w(run_info, "run_info.csv")
    w(flag_defs, "flag_definitions.csv")

    chart_files = make_charts(df, fcols, out, opts, warn) if charts else []

    readme = pd.DataFrame([
        {"tab": "Summary", "what it holds": "Headline statistics in ranked order (1 = most relevant to fraud screening). percent = count / denominator."},
        {"tab": "Flag_definitions", "what it holds": "Every red flag, its rule, and whether it was active in this run."},
        {"tab": "Availability", "what it holds": "Every institution URL by failure cause. 'counts_as_broken' marks the categories in the broken total."},
        {"tab": "Institutions", "what it holds": "One row per UNITID: availability, metrics, flags, red-flag score, tier, anomaly scores, cluster IDs."},
        {"tab": "Deep_dive_candidates", "what it holds": "Reachable sites to crawl in depth. Has UNITID + URL columns, so it can be fed straight to the crawler."},
        {"tab": "Verify_status", "what it holds": "Broken URLs to verify by other means (Scorecard status, Wayback Machine, WHOIS)."},
        {"tab": "Validation", "what it holds": "Flag rates among known-problem schools vs. the rest (lift), and score AUC."},
        {"tab": "Clusters_*", "what it holds": "Groups of domains linked by a shared key (tracker ID, template, ...). cross_owner = different OPEID6 or owner unknown."},
        {"tab": "Edges", "what it holds": "Domain pairs and the key they share; load into a network-graph tool."},
        {"tab": "Excluded_keys", "what it holds": "Keys shared by too many domains to count (web vendors, platforms)."},
        {"tab": "Shared_URLs", "what it holds": "Registered domains used by several UNITIDs."},
        {"tab": "Distributions", "what it holds": "Numeric summaries (p5-p95) overall and by ownership."},
        {"tab": "XT_*", "what it holds": "Crosstabs by ownership (needs --institutions)."},
        {"tab": "Top_values", "what it holds": "Most common builders, frameworks, certificate issuers, registrants."},
        {"tab": "Coverage", "what it holds": "How complete each field is, with known caveats for the current crawl output."},
        {"tab": "Run_info", "what it holds": "Schema detected, options, and warnings. Read the warnings before quoting numbers."},
    ])

    if excel:
        sheets = [("README", readme), ("Summary", summary), ("Flag_definitions", flag_defs), ("Availability", av),
                  ("Institutions", inst_table), ("Deep_dive_candidates", deep), ("Verify_status", verify),
                  ("Validation", validation)]
        sheets += [(f"Clusters_{k}", v) for k, v in cluster_frames.items()]
        sheets += [("Edges", edges), ("Excluded_keys", excluded), ("Shared_URLs", shared), ("Distributions", dist)]
        sheets += [(f"XT_{k}", v) for k, v in xts.items()]
        sheets += [("Top_values", tops), ("Coverage", cov), ("Run_info", run_info)]
        write_excel(out / "stats_sheet.xlsx", sheets, warn)
        # refresh run_info warnings written after the workbook (chart/excel warnings)
    if warn:
        run_info = pd.concat([run_info[run_info["item"] != "Warning"], pd.DataFrame([{"item": "Warning", "value": x} for x in warn])], ignore_index=True)
        w(run_info, "run_info.csv")

    return {"summary": summary, "institutions": inst_table, "deep_dive_candidates": deep, "verify_status": verify,
            "availability": av, "validation": validation, "clusters": cluster_frames, "edges": edges,
            "excluded_keys": excluded, "shared_urls": shared, "distributions": dist, "crosstabs": xts,
            "top_values": tops, "coverage": cov, "run_info": run_info, "flag_definitions": flag_defs,
            "charts": chart_files, "data": df, "warnings": warn, "schema": schema}


def _parse_map(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--map expects field=column, got '{it}'")
        k, v = it.split("=", 1)
        if k not in INSTITUTION_FIELDS:
            raise SystemExit(f"--map field must be one of {list(INSTITUTION_FIELDS)}")
        out[k] = v
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the fraud-screening stats sheet from site_features.csv.")
    ap.add_argument("site_features", help="site_features.csv from university_site_features.py")
    ap.add_argument("--institutions", help="College Scorecard / IPEDS extract keyed by UNITID (optional)")
    ap.add_argument("--known-bad", help="CSV of known-problem UNITIDs, optional 'label' column (optional)")
    ap.add_argument("--out-dir", default="stats_output")
    ap.add_argument("--id-column", default="UNITID")
    ap.add_argument("--url-column", default="school.school_url")
    ap.add_argument("--map", action="append", metavar="FIELD=COLUMN",
                    help=f"Override an institutions column. Fields: {', '.join(INSTITUTION_FIELDS)}")
    ap.add_argument("--no-charts", action="store_true")
    ap.add_argument("--no-excel", action="store_true")
    ap.add_argument("--iforest", action="store_true", help="Also fit an Isolation Forest (needs scikit-learn)")
    for k, v in DEFAULTS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", type=type(v), default=None, help=f"default {v}")
    a = ap.parse_args(argv)
    overrides = {k: getattr(a, k) for k in DEFAULTS}
    res = build_stats(a.site_features, institutions=a.institutions, known_bad=a.known_bad, out_dir=a.out_dir,
                      id_col=a.id_column, url_col=a.url_column, column_map=_parse_map(a.map),
                      charts=not a.no_charts, excel=not a.no_excel, iforest=a.iforest, **overrides)
    s = res["summary"]
    print(f"schema: {res['schema']}")
    print(f"institutions: {len(res['institutions'])}")
    b = s[s["metric"].str.startswith("BROKEN")]
    if len(b):
        print(f"broken URLs: {b['count'].iloc[0]} ({b['percent'].iloc[0]}%)")
    for t in ("High", "Medium"):
        r = s[s["metric"].str.startswith(f"Tier {t} (")]
        if len(r):
            print(f"tier {t}: {r['count'].iloc[0]}")
    print(f"deep-dive candidates: {len(res['deep_dive_candidates'])}; verify-status: {len(res['verify_status'])}")
    for x in res["warnings"]:
        print(f"warning: {x}")
    print(f"outputs: {Path(a.out_dir).resolve()}")


if __name__ == "__main__":
    main()
