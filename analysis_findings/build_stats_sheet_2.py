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
  recrawl_candidates.csv        fetched homepages whose fields were not (fully) captured, or that
                                look built with JavaScript (the crawler does not run JavaScript)
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

Missing data:
  Every flag is 1 (raised), 0 (checked, not raised) or blank (could not be
  checked). A missing value is never read as "not raised". Each institution gets
  data_completeness_pct (share of the flags that apply to it that could be
  checked) and max_possible_score (its score if every unchecked flag were
  raised). An institution is tiered "Low" only if at least min_completeness of
  its flags were checked AND max_possible_score stays below high_threshold;
  otherwise it is "Insufficient data", never "Low". Placeholder text (N/A, -, null), unreadable numbers and malformed list
  values are read as missing and counted in section 0 of the Summary. Fetched
  homepages with missing fields are listed in recrawl_candidates.csv. Each
  optional section (clusters, anomaly, validation, charts, workbook) is isolated:
  if one fails, the rest of the sheet is still written and Run_info says why.

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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd

SCRIPT_VERSION = "1.1.0"

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
    "min_completeness": 0.7,        # share of applicable flags that must be checked to call a school "Low"
    "min_capture_pct": 0.75,        # fetched homepages with fewer core fields captured go to the re-crawl list
    "js_shell_min_kb": 20.0,        # page >= this size with < 50 words and < 5 links = possible JavaScript-built page
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
    "social_profile_urls", "image_phashes", "operator_link_types", "extraction_errors",
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
    "has_admissions_page", "has_financial_aid_page", "js_shell_suspected",
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
    "other": "unknown", "missing_url": "bad_url",
}

GROUP_LABELS = {
    "reachable": "Reachable",
    "blocked": "Alive, blocks automated access",
    "broken_http": "Server returns an error",
    "unreachable_server": "Server unreachable or timed out",
    "dns_dead": "Domain does not resolve",
    "tls_failure": "TLS/SSL handshake failure",
    "bad_url": "Malformed or missing URL on record",
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
    "invalid_url": "Malformed URL", "missing_url": "No URL on record", "other": "Other / unrecognized error",
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

# ---- missing-data vocabulary -------------------------------------------------
NA_TOKENS = {"", "nan", "none", "null", "na", "n/a", "#n/a", "-", "--", "?", "undefined", "nat", "<na>"}
OK_STATUS = {"ok", "partly_malformed"}   # list captured (possibly with some bad items dropped)
_DOMAIN = re.compile(r"^(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")
KEY_PATTERNS = {  # list values that fail these are dropped and counted as malformed
    "ga_ua_ids": re.compile(r"^UA-\d{4,10}-\d{1,4}$"),
    "ga4_ids": re.compile(r"^G-[A-Z0-9]{6,14}$"),
    "gtm_ids": re.compile(r"^GTM-[A-Z0-9]{4,10}$"),
    "fb_pixel_ids": re.compile(r"^\d{5,20}$"),
    "structure_hashes": re.compile(r"^[0-9a-f]{40}$"),
    "css_content_hashes": re.compile(r"^[0-9a-f]{40}$"),
    "phone_numbers": re.compile(r"^\d{10}$"),
    "contact_emails": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    "free_email_addresses": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    "social_profile_urls": re.compile(r"^https?://\S+$"),
    "site_builders_detected": re.compile(r"^(wix|squarespace|weebly|godaddy|wordpress)$"),
    "css_frameworks": re.compile(r"^(bootstrap|tailwind|foundation|bulma|materialize)$"),
    "social_platforms_linked": re.compile(r"^(facebook|instagram|linkedin|x|youtube|tiktok)$"),
    **{c: _DOMAIN for c in ["external_domains", "trusted_link_domains", "accreditor_domains_linked",
                            "recognized_accreditor_domains_linked", "accredit_keyword_domains",
                            "unrecognized_accredit_domains", "mailto_domains", "ssl_san_domains",
                            "ssl_san_other_registered_domains", "third_party_script_domains", "nameservers",
                            "whois_name_servers"]},
}
KEY_NORMALIZE = {
    **{c: str.upper for c in ["ga_ua_ids", "ga4_ids", "gtm_ids"]},
    **{c: str.lower for c in ["structure_hashes", "css_content_hashes", "contact_emails", "free_email_addresses",
                              "site_builders_detected", "css_frameworks", "social_platforms_linked"]},
    **{c: (lambda x: x.lower().rstrip(".")) for c in ["external_domains", "trusted_link_domains", "accreditor_domains_linked",
                                                      "recognized_accreditor_domains_linked", "accredit_keyword_domains",
                                                      "unrecognized_accredit_domains", "mailto_domains", "ssl_san_domains",
                                                      "ssl_san_other_registered_domains", "third_party_script_domains",
                                                      "nameservers", "whois_name_servers"]},
}
# Fields every fetched homepage should have; used to measure capture per site.
CORE_PAGE_FIELDS = ["total_words", "total_a_tags", "total_internal_links", "total_external_links", "total_images",
                    "total_html_bytes", "phone_number_hits", "has_street_address", "uses_site_builder", "has_favicon"]
# Fields where blank is a legitimate value, not a capture failure.
SEMANTIC_NULL = {
    "img_alt_coverage": "the page has no images", "mailto_on_domain_ratio": "the page has no mailto links",
    "max_copyright_year": "no copyright year is shown", "min_copyright_year": "no copyright year is shown",
    "ssl_cert_error": "there is no certificate error", "crawl_error": "the crawl succeeded",
    "broken_link_rate_sampled": "no internal links were sampled", "landing_status_code": "no HTTP response was received",
}
FLAG_SCOPE = {  # which institutions a flag applies to (drives data_completeness_pct)
    "flag_unreachable": "domain", "flag_young_domain": "domain", "flag_whois_proxy": "domain",
    "flag_named_univ_not_edu": "domain", "flag_ssl_problem": "domain", "flag_domain_expiring": "domain",
    "flag_consumer_builder": "page", "flag_thin_homepage": "page", "flag_no_physical_presence": "page",
    "flag_shared_tracker": "page", "flag_shared_template": "page", "flag_redirect_off_domain": "page",
    "flag_parked_or_placeholder": "page", "flag_free_or_offdomain_email": "page",
    "flag_unrecognized_accreditor": "page", "flag_shared_contact_or_infra": "page",
    "flag_stale_copyright": "optional", "flag_missing_required_disclosures": "deep",
}
SCOPE_LABELS = {
    "domain": "Every institution",
    "page": "Institutions whose homepage was fetched",
    "deep": "Institutions in a deep (multi-page) crawl",
    "optional": "Not counted toward completeness (blank can mean nothing to check)",
}

FLAG_DEFINITIONS = {
    # flag column: (short label, definition, needs)
    "flag_unreachable": ("Site unreachable", "The URL on record is broken: DNS failure, connection failure or timeout, TLS failure, an HTTP error (404/410/5xx, redirect loop), or a malformed URL. 401/403/429/503 responses are treated as 'alive but blocking', not broken. Unclassified errors are left unchecked.", "always"),
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


def is_na_token(v) -> bool:
    if _isnan(v):
        return True
    return isinstance(v, str) and v.strip().lower() in NA_TOKENS


def parse_list_status(v, col=None):
    """Return (items, status). status: ok | missing | malformed | partly_malformed.
    'missing' means not captured; an empty list with 'ok' means captured, none found."""
    if isinstance(v, (list, tuple, set)):
        items = list(v)
    elif is_na_token(v):
        return [], "missing"
    elif isinstance(v, str):
        s = v.strip()
        if s[:1] in ("[", "("):
            items = None
            if s[-1:] in ("]", ")"):
                for parser in (json.loads, ast.literal_eval):
                    try:
                        out = parser(s)
                    except Exception:
                        continue
                    if isinstance(out, (list, tuple, set)):
                        items = list(out)
                        break
            if items is None:
                return [], "malformed"
        else:
            items = re.split(r"\s*[;|]\s*", s)   # tolerate 'a; b' or 'a | b'
    else:
        items = [v]
    items = [x for x in items if not is_na_token(x)]
    pat = KEY_PATTERNS.get(col)
    if pat is not None and items:
        norm = KEY_NORMALIZE.get(col, lambda x: x)
        vals = [norm(str(x).strip()) for x in items]
        good = [x for x in vals if pat.match(x)]
        if len(good) < len(vals):
            return good, ("partly_malformed" if good else "malformed")
        return good, "ok"
    return items, "ok"


def _as_bool(x) -> pd.Series:
    return pd.Series(x).astype("boolean").fillna(False).astype(bool)


def b01(cond, known) -> pd.Series:
    """1/0 where known, blank (NaN) where the input was missing."""
    return _as_bool(cond).astype(float).where(_as_bool(known))


def gt0(s: pd.Series) -> pd.Series:
    return b01(s > 0, s.notna())


def k1(positive, zero_known) -> pd.Series:
    """1 where positive evidence exists; 0 only where absence was actually checked; else NaN."""
    p, z = _as_bool(positive), _as_bool(zero_known)
    out = pd.Series(np.nan, index=p.index)
    out[z] = 0.0
    out[p] = 1.0
    return out


def any3(*parts) -> pd.Series:
    """Three-valued OR: 1 if any part is 1; 0 only if every part is known 0; else NaN."""
    m = pd.concat([pd.Series(p, dtype=float) for p in parts], axis=1)
    out = pd.Series(np.nan, index=m.index)
    out[m.notna().all(axis=1)] = 0.0
    out[(m == 1).any(axis=1)] = 1.0
    return out


def all3(*parts) -> pd.Series:
    """Three-valued AND: 0 if any part is 0; 1 only if every part is known 1; else NaN."""
    m = pd.concat([pd.Series(p, dtype=float) for p in parts], axis=1)
    out = pd.Series(np.nan, index=m.index)
    out[(m == 0).any(axis=1)] = 0.0
    out[(m == 1).all(axis=1)] = 1.0
    return out


def list_ok(df: pd.DataFrame, col: str) -> pd.Series:
    """True where a list column was actually captured (not missing / unparseable)."""
    if f"_lst_{col}" in df.columns:
        return df[f"_lst_{col}"].isin(OK_STATUS)
    return pd.Series(col in df.columns, index=df.index)


@contextmanager
def soft_fail(warn: list, label: str):
    """Run a block; on error, record a warning and carry on."""
    try:
        yield
    except Exception as e:
        warn.append(f"{label} skipped: {type(e).__name__}: {e}")


def _try(warn: list, label: str, fn, *args, default=None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        warn.append(f"{label} skipped: {type(e).__name__}: {e}")
        return default


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
    info = {}
    if isinstance(path_or_df, pd.DataFrame):
        df = path_or_df.copy()
    else:
        try:
            df = pd.read_csv(path_or_df, dtype={id_col: str}, low_memory=False)
        except pd.errors.EmptyDataError:
            raise ValueError(f"{path_or_df} is empty: no header row and no data.") from None
    if len(df) == 0:
        raise ValueError("The site features file has column headers but no rows.")
    cols_in = set(df.columns)
    if id_col not in df.columns:
        raise KeyError(f"ID column '{id_col}' not found. First columns: {list(df.columns)[:15]}. Use --id-column.")
    if url_col not in df.columns:
        if "input_url" in df.columns:
            warn.append(f"URL column '{url_col}' not found; using input_url.")
            df[url_col] = df["input_url"]
        else:
            raise KeyError(f"URL column '{url_col}' not found and there is no input_url column. Use --url-column.")
    info["n_rows_in"] = len(df)

    # placeholder text ('N/A', '-', 'null', ...) -> missing, in every non-list text column
    n_tok = 0
    for c in df.columns:
        if c in LIST_COLUMNS or pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_bool_dtype(df[c]):
            continue
        tok = df[c].map(lambda v: isinstance(v, str) and v.strip().lower() in NA_TOKENS)
        if tok.any():
            n_tok += int(tok.sum())
            df[c] = df[c].mask(tok)
    info["n_placeholder_cells"] = n_tok

    # IDs: keep rows with no ID, under a visible placeholder
    df[id_col] = clean_id(df[id_col])
    idmiss = df[id_col].isna() | (df[id_col] == "")
    info["n_missing_id"] = int(idmiss.sum())
    if idmiss.any():
        df.loc[idmiss, id_col] = [f"MISSING_ID_line{i + 2}" for i in df.index[idmiss]]
        warn.append(f"{int(idmiss.sum())} rows have no {id_col}; they were kept under placeholder IDs "
                    "(MISSING_ID_line<n>) and cannot be joined to institution data.")

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
        df["structure_hashes"] = df["structure_hash"].map(lambda v: np.nan if _isnan(v) else [str(v).strip()])

    # list columns: keep "not captured" separate from "none found"
    list_issues, status_cols = {}, {}
    for c in LIST_COLUMNS:
        if c in df.columns:
            parsed = [parse_list_status(v, c) for v in df[c]]
            df[c] = pd.Series([p[0] for p in parsed], index=df.index, dtype=object)
            st = pd.Series([p[1] for p in parsed], index=df.index)
            status_cols[f"_lst_{c}"] = st
            bad = int(st.isin(["malformed", "partly_malformed"]).sum())
            if bad:
                list_issues[c] = bad
    if status_cols:
        df = pd.concat([df, pd.DataFrame(status_cols, index=df.index)], axis=1)
    info["list_issues"] = list_issues
    if list_issues:
        warn.append("List values that could not be parsed or failed format checks were read as missing: "
                    + ", ".join(f"{k} ({v})" for k, v in list_issues.items()) + ".")

    # numbers and booleans: count values that could not be read
    coerced = {}
    for cols, fn in ((BOOL_COLUMNS, to01), (NUMERIC_COLUMNS, num)):
        for c in cols:
            if c in df.columns:
                before = df[c].notna()
                df[c] = fn(df[c])
                n = int((before & df[c].isna()).sum())
                if n:
                    coerced[c] = n
    info["coerced"] = coerced
    if coerced:
        top = sorted(coerced.items(), key=lambda kv: -kv[1])[:10]
        warn.append("Values that could not be read as numbers or true/false were read as missing: "
                    + ", ".join(f"{k} ({v})" for k, v in top) + ".")

    # URLs, domains, TLDs (fill gaps from the URL on record)
    if "input_url" not in df.columns:
        df["input_url"] = df[url_col]
    df["input_url"] = df["input_url"].where(df["input_url"].notna(), df[url_col])
    url_for_parse = df["input_url"]
    for col, fn in (("registered_domain", registered_domain_of), ("tld", tld_of)):
        cur = df[col].astype("object") if col in df.columns else pd.Series([None] * len(df), index=df.index, dtype="object")
        miss = cur.map(lambda v: _isnan(v) or str(v).strip() == "")
        cur = cur.where(~miss, url_for_parse.map(fn))
        df[col] = cur.map(lambda v: "" if _isnan(v) else str(v).lower().strip())
    df["tld_category"] = df["tld"].map(tld_category)

    dup = df[id_col].duplicated(keep="first")
    info["n_dup_id"] = int(dup.sum())
    if dup.any():
        warn.append(f"{int(dup.sum())} duplicate {id_col} rows dropped (kept first).")
        df = df[~dup].copy()

    return df.reset_index(drop=True), schema, cols_in, info


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
    try:
        header = list(pd.read_csv(path, nrows=0).columns)
    except pd.errors.EmptyDataError:
        raise ValueError(f"Institutions file {path} is empty.") from None
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
    try:
        kb = pd.read_csv(path, dtype=str)
    except pd.errors.EmptyDataError:
        raise ValueError(f"Known-bad file {path} is empty.") from None
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
    cat = pd.Series([classify_row(e, s, p) for e, s, p in zip(err, status, provided)], index=df.index, dtype=object)
    cat[df["input_url"].isna()] = "missing_url"
    df["availability_category"] = cat
    df["availability_group"] = df["availability_category"].map(GROUP_OF).fillna("unknown")
    if "bot_protection_detected" in df.columns:
        df.loc[df["bot_protection_detected"] == 1, "availability_group"] = "blocked"
    df["availability_label"] = df["availability_group"].map(GROUP_LABELS)
    df["is_broken"] = df["availability_group"].isin(BROKEN_GROUPS).astype(float)
    return df


# --------------------------------------------------------------------------
# Derived per-site metrics
# --------------------------------------------------------------------------
def add_metrics(df: pd.DataFrame, opts: dict, schema: str = "unknown") -> pd.DataFrame:
    pages = df["pages_crawled"] if "pages_crawled" in df.columns else pd.Series(np.nan, index=df.index)
    pages = pages.where(pages > 0)
    # a missing page count means 1 only for home-only crawls; otherwise per-page rates stay blank
    home_only = pd.Series(schema == "legacy_home_only" or "pages_crawled" not in df.columns, index=df.index)
    if "crawl_mode" in df.columns:
        home_only |= df["crawl_mode"].astype(str).eq("home_only")
    pages = pages.fillna(pd.Series(np.where(home_only, 1.0, np.nan), index=df.index))

    def per_page(col):
        if col not in df.columns:
            return pd.Series(np.nan, index=df.index)
        return df[col] / pages

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

    # ---- page capture: which core fields were actually captured on each fetched homepage ----
    fetched = df["availability_category"] == "ok"
    cap = {}
    for c in CORE_PAGE_FIELDS:
        if c in df.columns and df.loc[fetched, c].notna().any():   # skip fields this crawl never writes
            cap[c] = df[c].notna()
    if "_lst_structure_hashes" in df.columns and df.loc[fetched, "_lst_structure_hashes"].isin(OK_STATUS).any():
        cap["structure_hashes"] = df["_lst_structure_hashes"].isin(OK_STATUS)
    tkl = [c for c in TRACKER_COLUMNS if f"_lst_{c}" in df.columns]
    if tkl:
        st = pd.concat([df[f"_lst_{c}"].isin(OK_STATUS) for c in tkl], axis=1)
        if st[fetched].to_numpy().any():
            cap["tracker_ids"] = st.all(axis=1)
    C = pd.DataFrame(cap, index=df.index)
    n_core = C.shape[1]
    got = C.sum(axis=1) if n_core else pd.Series(0, index=df.index)
    df["has_content"] = ((df["availability_group"] == "reachable") & (got > 0)).astype(float)
    df["page_capture_pct"] = (got / n_core).round(3).where(fetched) if n_core else np.nan
    status = pd.Series("not reached", index=df.index, dtype=object)
    status[df["availability_category"] == "non_html"] = "no HTML page"
    status[df["availability_category"] == "extraction_failed"] = "extraction failed"
    if n_core:
        status[fetched & (got == 0)] = "no page fields"
        status[fetched & (got > 0) & (got < n_core)] = "partial"
        status[fetched & (got == n_core)] = "complete"
        names, arr = list(C.columns), C.to_numpy()
        df["missing_page_fields"] = ["; ".join(n for n, v in zip(names, row) if not v) if s in ("partial", "no page fields") else ""
                                     for s, row in zip(status, arr)]
    else:
        status[fetched] = "page fields not in file"
        df["missing_page_fields"] = ""
    df["capture_status"] = status

    # Pages built with JavaScript: the crawler sees a large file with almost no visible text or links.
    # Their text-based fields are unreliable, so text-based flags are left unchecked for them.
    if "js_shell_suspected" in df.columns and df["js_shell_suspected"].notna().any():
        js = df["js_shell_suspected"] == 1
    else:
        js = ((df["html_kb_per_page"] >= opts["js_shell_min_kb"]) & (df["words_per_page"] < 50)
              & (df["a_tags_per_page"].fillna(0) < 5))
    df["js_shell_suspected_any"] = (js & (df["has_content"] == 1)).astype(float)

    partial_low = (status == "partial") & (df["page_capture_pct"] < opts["min_capture_pct"])
    reason = pd.Series("", index=df.index, dtype=object)
    reason[df["js_shell_suspected_any"] == 1] = "possible JavaScript-built page: re-crawl with rendering"
    reason[partial_low] = "partial capture: " + df.loc[partial_low, "missing_page_fields"].astype(str)
    reason[status == "extraction failed"] = "HTML could not be parsed"
    reason[status == "no page fields"] = "page fetched but no fields captured"
    df["recrawl_reason"] = reason
    df["recrawl"] = (reason != "").astype(float)

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
        avail = m.notna().sum(axis=1)
        scaled = (m.sum(axis=1) / avail.replace(0, np.nan) * len(parts)).round(2)   # scale to the parts captured
        df["metadata_index"] = scaled.where((df["has_content"] == 1) & (avail >= min(3, len(parts))))
        df["metadata_index_max"] = len(parts)

    # trackers: "none found" only when every tracker field was captured
    content = df["has_content"] == 1
    tk = [c for c in TRACKER_COLUMNS if c in df.columns]
    if tk:
        df["tracker_ids"] = [sorted({str(x) for L in row for x in L}) for row in zip(*[df[c] for c in tk])]
        stk = pd.concat([list_ok(df, c) for c in tk], axis=1)
        df["trackers_captured_all"] = stk.all(axis=1)
        df["trackers_captured_any"] = stk.any(axis=1)
        n = df["tracker_ids"].map(len).astype(float)
        df["n_tracker_ids"] = n.where(content & df["trackers_captured_any"])
        df["has_any_tracker"] = k1(content & (n > 0), content & df["trackers_captured_all"])
    if "fb_pixel_ids" in df.columns:
        df["has_fb_pixel"] = k1(content & (df["fb_pixel_ids"].map(len) > 0), content & list_ok(df, "fb_pixel_ids"))

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


def shared_key_analysis(df, key_type, values_by_row, opts, owners, exclude_mask=None, id_col="UNITID", capture_rate=1.0):
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
        # estimate how many sites carry the key, allowing for sites whose key field was not captured
        # Estimate how many sites carry the key, allowing for sites whose key field was not captured.
        # The high end (about 2 standard errors) is used so that missing data cannot shrink a
        # vendor-wide key back under the cutoff. With no missing fields this is just the count.
        rate = max(capture_rate(k) if callable(capture_rate) else capture_rate, 0.05)
        n_obs = len(doms)
        est_hi = (n_obs + 2 * math.sqrt(n_obs * (1 - rate))) / rate
        if est_hi > opts["vendor_threshold"]:
            excluded.append({"key_type": key_type, "key": k, "n_domains": n_obs, "n_domains_estimated": round(n_obs / rate, 1),
                             "n_domains_estimated_high": round(est_hi, 1)})
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
            "review_note": "Large cluster: may be a web vendor or platform; check before treating it as one operator." if len(members) >= 10 else "",
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

    base = df["availability_category"] == "ok"
    src = {"template": "structure_hashes", "css_file": "css_content_hashes", "phone": "phone_numbers", "email": "contact_emails",
           "street_address": "street_addresses", "social_profile": "social_profile_urls",
           "third_party_script": "third_party_script_domains", "ssl_cert": "ssl_cert_sha256", "html_exact": "html_sha256",
           "redirect_target": "redirected_off_domain", "registrant_org": "whois_registrant_org"}

    def rate_of(cm):
        n = int(base.sum())
        return float((base & cm).sum()) / n if n else 1.0

    def capture_rate(kt):
        if kt == "tracker_id":   # each tracker ID comes from one field; use that field's capture rate
            rates = {c: rate_of(list_ok(df, c)) for c in TRACKER_COLUMNS if c in df.columns}

            def by_key(k):
                c = ("gtm_ids" if k.startswith("GTM-") else "ga_ua_ids" if k.startswith("UA-")
                     else "ga4_ids" if k.startswith("G-") else "fb_pixel_ids")
                return rates.get(c, 1.0)
            return by_key
        if src.get(kt) in LIST_COLUMNS:
            cm = list_ok(df, src[kt])
        elif src.get(kt) in df.columns:
            cm = df[src[kt]].notna()
        else:
            return 1.0
        return rate_of(cm)

    for kt, vals, mask in specs:
        (per_dom, clusters), edges, excluded = shared_key_analysis(df, kt, vals, opts, owners, mask, id_col, capture_rate(kt))
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
    excluded = pd.DataFrame(all_excluded, columns=["key_type", "key", "n_domains", "n_domains_estimated", "n_domains_estimated_high"])
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
def build_flags(df: pd.DataFrame, opts: dict, schema: str, warn: list):
    """Each flag is 1 (raised), 0 (checked, not raised) or NaN (could not be checked).
    A missing input never counts as 'not raised'. One failing flag never stops the sheet."""
    content = df["has_content"] == 1
    reached = df["availability_group"] == "reachable"
    idx = df.index
    js = (df["js_shell_suspected_any"] == 1) if "js_shell_suspected_any" in df.columns else pd.Series(False, index=idx)
    F = {}

    def put(name, fn):
        try:
            v = fn()
        except Exception as e:
            warn.append(f"Flag '{FLAG_DEFINITIONS[name][0]}' skipped: {type(e).__name__}: {e}")
            return
        if v is not None:
            F[name] = pd.Series(v, index=idx, dtype=float)

    put("flag_unreachable", lambda: df["is_broken"].where(df["availability_group"] != "unknown"))

    def young():
        if "domain_age_days" not in df.columns or df["domain_age_days"].notna().sum() == 0:
            return None
        a = df["domain_age_years"]
        return b01(a < opts["young_domain_years"], a.notna())
    put("flag_young_domain", young)

    def whois_proxy():
        if "whois_privacy_type" in df.columns:
            t = df["whois_privacy_type"].astype("string").str.lower()
            return b01(t == "proxy_service", t.notna())
        if "whois_registrant_org" in df.columns:
            org = df["whois_registrant_org"].map(lambda v: "" if _isnan(v) else norm_text(v))
            f = org.map(lambda o: 1.0 if o and any(p in o for p in PRIVACY_PROXY_PATTERNS) else (0.0 if o else np.nan))
            return f if f.notna().any() else None
        return None
    put("flag_whois_proxy", whois_proxy)

    def named_univ():
        if "inst_name" not in df.columns:
            return None
        named = df["inst_name"].map(lambda n: (not _isnan(n)) and bool(NAME_UNIV_RE.search(str(n))))
        tl = df["tld"].astype("string").str.lower().fillna("")
        public_ok = (tl == "edu") | tl.str.endswith("gov") | tl.str.endswith("us")
        return b01(named & ~public_ok, df["inst_name"].notna() & (tl != ""))
    put("flag_named_univ_not_edu", named_univ)

    def ssl_problem():
        if "ssl_ok" not in df.columns:
            return None
        prob = _as_bool(df["ssl_ok"] == 0)
        if "ssl_verified" in df.columns:
            prob |= _as_bool(df["ssl_verified"] == 0)
        if "ssl_cert_error" in df.columns:
            prob |= _as_bool(df["ssl_cert_error"].notna() & (df["ssl_cert_error"].astype("string").str.strip() != ""))
        if "ssl_days_to_expiry" in df.columns:
            prob |= _as_bool(df["ssl_days_to_expiry"] < 0)
        if "ssl_bypass_used" in df.columns:
            prob |= _as_bool(df["ssl_bypass_used"] == 1)
        return b01(prob, df["ssl_ok"].notna())
    put("flag_ssl_problem", ssl_problem)

    put("flag_consumer_builder", lambda: df["uses_site_builder"].where(content) if "uses_site_builder" in df.columns else None)

    def thin():
        w = df["words_per_page"].where(content)
        if w.notna().sum() == 0:
            return None
        thresh = w.groupby(df["peer_group"]).transform(
            lambda s: s.quantile(opts["thin_percentile"] / 100.0) if s.notna().sum() >= 5 else np.nan)
        df["thin_threshold_words"] = thresh
        return b01(w < thresh, w.notna() & thresh.notna()).where(~js)
    put("flag_thin_homepage", thin)

    def no_presence():
        if "phone_number_hits" not in df.columns or "has_street_address" not in df.columns:
            return None
        presence = any3(gt0(df["phone_number_hits"]), df["has_street_address"])
        return (1 - presence).where(content & ~js)
    put("flag_no_physical_presence", no_presence)

    def shared(kt, known):
        c = f"n_cross_owner_domains_sharing_{kt}"
        if c not in df.columns:
            return None
        return k1(content & (df[c] > 0), content & known)
    put("flag_shared_tracker", lambda: shared("tracker_id", df["trackers_captured_all"]) if "trackers_captured_all" in df.columns else None)
    put("flag_shared_template", lambda: shared("template", list_ok(df, "structure_hashes")).where(~js))
    put("flag_redirect_off_domain", lambda: df["redirected_off_domain"] if "redirected_off_domain" in df.columns else None)

    def parked():
        pk = [c for c in ["parked_domain_detected", "placeholder_page_detected", "default_server_page_detected",
                          "parking_nameserver_detected"] if c in df.columns and df[c].notna().any()]
        return any3(*[df[c] for c in pk]) if pk else None
    put("flag_parked_or_placeholder", parked)

    def stale():
        if "copyright_staleness_years" not in df.columns or df["copyright_staleness_years"].notna().sum() == 0:
            return None
        s = df["copyright_staleness_years"]
        return b01(s >= opts["stale_copyright_years"], s.notna())
    put("flag_stale_copyright", stale)

    def email():
        if schema == "legacy_home_only":
            return None   # free_email_hits is hard-coded to 0 there
        parts = []
        if "free_email_addresses" in df.columns:
            parts.append(b01(df["free_email_addresses"].map(len) > 0, list_ok(df, "free_email_addresses")))
        elif "free_email_hits" in df.columns:
            parts.append(gt0(df["free_email_hits"]))
        if "mailto_on_domain_ratio" in df.columns:
            r = df["mailto_on_domain_ratio"]
            off = pd.Series(np.nan, index=idx)
            off[r == 0] = 1.0
            off[r > 0] = 0.0
            if "mailto_count" in df.columns:
                off[df["mailto_count"] == 0] = 0.0   # no mailto links: nothing off-domain
            parts.append(off)
        parts = [p for p in parts if p.notna().any()]
        return any3(*parts).where(content) if parts else None
    put("flag_free_or_offdomain_email", email)

    put("flag_unrecognized_accreditor", lambda: k1(content & (df["unrecognized_accredit_domains"].map(len) > 0),
                                                   content & list_ok(df, "unrecognized_accredit_domains"))
        if "unrecognized_accredit_domains" in df.columns else None)

    def expiring():
        if "domain_days_to_expiry" not in df.columns:
            return None
        d = df["domain_days_to_expiry"]
        return b01(d < opts["domain_expiry_days"], d.notna())
    put("flag_domain_expiring", expiring)

    strong = {"phone": ("list", "phone_numbers"), "email": ("list", "contact_emails"),
              "street_address": ("list", "street_addresses"), "social_profile": ("list", "social_profile_urls"),
              "ssl_cert": ("scalar", "ssl_cert_sha256"), "html_exact": ("scalar", "html_sha256"),
              "near_dup_text": ("scalar", "text_simhash64"), "redirect_target": ("scalar", "redirected_off_domain")}

    def shared_infra():
        parts, names = [], []
        for kt, (kind, src) in strong.items():
            c = f"n_cross_owner_domains_sharing_{kt}"
            if c not in df.columns or src not in df.columns:
                continue
            known = list_ok(df, src) if kind == "list" else df[src].notna()
            parts.append(k1(content & (df[c] > 0), content & known))
            names.append(kt)
        if not parts:
            return None
        arr = pd.concat(parts, axis=1).to_numpy()
        df["shared_contact_or_infra_types"] = ["; ".join(k for k, v in zip(names, row) if v == 1) for row in arr]
        return any3(*parts)
    put("flag_shared_contact_or_infra", shared_infra)

    def disclosures():
        if not {"has_consumer_info_link", "has_security_report_link"}.issubset(df.columns):
            return None
        deep = df["crawl_mode"].eq("multi_page") if "crawl_mode" in df.columns else df.get("pages_crawled", pd.Series(np.nan, index=idx)).gt(1)
        return all3(1 - df["has_consumer_info_link"], 1 - df["has_security_report_link"]).where(deep & content)
    put("flag_missing_required_disclosures", disclosures)

    # ---- score, completeness, tier ----
    flags = pd.DataFrame(F, index=idx)
    fcols = list(flags.columns)
    deep_row = df["crawl_mode"].eq("multi_page") if "crawl_mode" in df.columns else df.get("pages_crawled", pd.Series(np.nan, index=idx)).gt(1)
    scope_mask = {"domain": pd.Series(True, index=idx), "page": reached, "deep": reached & deep_row,
                  "optional": pd.Series(False, index=idx)}
    applicable = pd.DataFrame({c: scope_mask[FLAG_SCOPE.get(c, "page")] for c in fcols}, index=idx)
    vals = flags.to_numpy(dtype=float) if fcols else np.empty((len(idx), 0))
    app = applicable.to_numpy(dtype=bool) if fcols else np.empty((len(idx), 0), dtype=bool)
    score = (vals == 1).sum(axis=1)
    n_app = app.sum(axis=1)
    n_chk = (app & ~np.isnan(vals)).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        comp = np.where(n_app > 0, n_chk / np.maximum(n_app, 1), np.nan)
    maxp = score + (n_app - n_chk)

    def tier(sc, cp, mx):
        if sc >= opts["high_threshold"]:
            return "High"
        if sc >= opts["medium_threshold"]:
            return "Medium"
        # "Low" is a claim about risk, so it needs enough data: most flags checked, and the
        # unchecked ones could not push the school to High even if every one were raised.
        if not (cp >= opts["min_completeness"]) or mx >= opts["high_threshold"]:
            return "Insufficient data"
        return "Low"

    labels = {c: FLAG_DEFINITIONS[c][0] for c in fcols}
    tiers = [tier(sc, cp, mx) for sc, cp, mx in zip(score, comp, maxp)]
    out = pd.DataFrame({
        "red_flag_score": score.astype(int),
        "n_flags_applicable": n_app.astype(int),
        "n_flags_evaluated": n_chk.astype(int),
        "data_completeness_pct": np.round(100 * comp, 1),
        "max_possible_score": maxp.astype(int),
        "triage_tier": tiers,
        "could_reach_high": [float(t != "High" and m >= opts["high_threshold"]) for t, m in zip(tiers, maxp)],
        "flag_reasons": ["; ".join(labels[c] for c, v in zip(fcols, row) if v == 1) for row in vals],
        "flags_not_checked": ["; ".join(labels[c] for c, v, a in zip(fcols, row, arow) if a and np.isnan(v))
                              for row, arow in zip(vals, app)],
    }, index=idx)
    df = pd.concat([df, flags, out], axis=1)
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
            X = X.loc[:, X.notna().any()]
            X = X[X.notna().mean(axis=1) >= 0.5]   # only sites with at least half the features captured
            X = X.fillna(X.median())
            if len(X) >= 50:
                model = IsolationForest(n_estimators=300, random_state=0, contamination="auto").fit(X)
                df.loc[X.index, "iforest_score"] = (-model.score_samples(X)).round(4)
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
        self.errors = []

    @contextmanager
    def guard(self, rank):
        """One failing section adds an error row instead of stopping the sheet."""
        try:
            yield
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            self.errors.append(f"Summary section {rank} could not be computed: {msg}")
            self.add(rank, f"{rank}. (section error)", "Section could not be computed", note=msg[:300])

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

    def rate(rank, sec, label, cond, mask=None, note=""):
        """count = 1s, denominator = rows where the value is known (missing never counts as 'no')."""
        c = pd.Series(cond, index=df.index, dtype=float)
        if mask is not None:
            c = c.where(mask)
        S.add(rank, sec, label, int((c == 1).sum()), int(c.notna().sum()), note=note)

    # 0 Data capture
    with S.guard(0):
        sec = "0. Data capture and completeness"
        li = inst_info or {}
        n_in = li.get("n_rows_in", N)
        S.add(0, sec, "Rows in the input file", n_in)
        if li.get("n_missing_id"):
            S.add(0, sec, "Rows with no UNITID (kept under placeholder IDs)", li["n_missing_id"], n_in,
                  note="These rows cannot be joined to institution data.")
        if li.get("n_dup_id"):
            S.add(0, sec, "Duplicate UNITID rows dropped", li["n_dup_id"], n_in)
        if li.get("n_placeholder_cells"):
            S.add(0, sec, "Cells holding placeholder text (N/A, -, null, ...) read as missing", li["n_placeholder_cells"])
        if li.get("coerced"):
            S.add(0, sec, "Values that could not be read as numbers or true/false (read as missing)", sum(li["coerced"].values()),
                  value="; ".join(f"{k} ({v})" for k, v in sorted(li["coerced"].items(), key=lambda kv: -kv[1])[:8]))
        if li.get("list_issues"):
            S.add(0, sec, "List values unparseable or failing format checks (read as missing)", sum(li["list_issues"].values()),
                  value="; ".join(f"{k} ({v})" for k, v in li["list_issues"].items()))
        fetched = df["availability_category"] == "ok"
        R = int(fetched.sum())
        cs = df["capture_status"]
        S.add(0, sec, "Homepages fetched (HTTP 200, HTML)", R, N)
        S.add(0, sec, "  ...no page fields captured (silent capture failure)", int((cs == "no page fields").sum()), R,
              note="Read as unknown, not as clean. Listed in recrawl_candidates.csv.")
        S.add(0, sec, "  ...some page fields missing", int((cs == "partial").sum()), R)
        S.add(0, sec, "  ...every core page field captured", int((cs == "complete").sum()), R)
        if (cs == "page fields not in file").any():
            S.add(0, sec, "  ...page fields not present in this file", int((cs == "page fields not in file").sum()), R)
        if (cs == "extraction failed").any():
            S.add(0, sec, "HTML fetched but could not be parsed", int((cs == "extraction failed").sum()), N)
        if "extraction_errors" in df.columns:
            eg = Counter(g for L in df["extraction_errors"] for g in L)
            S.add(0, sec, "Homepages where part of the extraction failed (crawler-reported)",
                  int((df["extraction_errors"].map(len) > 0).sum()), R, value="; ".join(f"{k} ({v})" for k, v in eg.most_common()))
        if "js_shell_suspected_any" in df.columns:
            S.add(0, sec, "Homepages that may be built with JavaScript (large page, little visible text)",
                  int((df["js_shell_suspected_any"] == 1).sum()), NC,
                  note="Text-based flags (thin page, no phone/address, shared template) are left unchecked for these. Re-crawl with a rendering browser.")
        if R and df.loc[fetched, "page_capture_pct"].notna().any():
            S.add(0, sec, "Mean share of core page fields captured on fetched homepages (%)",
                  value=round(100 * float(df.loc[fetched, "page_capture_pct"].mean()), 1))
            miss = Counter(f for st_, m in zip(cs, df["missing_page_fields"]) if st_ == "partial" for f in str(m).split("; ") if f)
            if miss:
                S.add(0, sec, "Fields most often missing on partly captured homepages",
                      value="; ".join(f"{k} ({v})" for k, v in miss.most_common(6)))
        ins = df["triage_tier"] == "Insufficient data"
        S.add(0, sec, "Too little data to score ('Insufficient data' tier)", int(ins.sum()), N,
              note=f"Fewer than {opts['medium_threshold']} flags raised, and either under {opts['min_completeness']:.0%} of the flags that apply were checked or the unchecked flags could push it to High. Not the same as low risk.")
        S.add(0, sec, "  ...of which the homepage was reached", int((ins & reach).sum()), int(ins.sum()))
        S.add(0, sec, "Institutions below High that would reach High if their unchecked flags were raised",
              int((df["could_reach_high"] == 1).sum()), N, note="Upper bound on risk hidden by missing data (see max_possible_score). By design none of them is tiered Low.")
        if df["data_completeness_pct"].notna().any():
            S.add(0, sec, "Mean share of applicable flags checked (%)", value=round(float(df["data_completeness_pct"].mean()), 1))
        S.add(0, sec, "Institutions listed for re-crawl", int(df["recrawl"].sum()), N, note="See recrawl_candidates.csv.")

    # 1 Availability
    with S.guard(1):
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
    with S.guard(2):
        sec = "2. Red-flag triage"
        S.add(2, sec, "Flags evaluated in this run", len(fcols), value="; ".join(FLAG_DEFINITIONS[c][0] for c in fcols),
              note="Score = number of flags raised. New flags switch on as spec v2 columns appear.")
        for t in ["High", "Medium", "Low", "Insufficient data"]:
            n = int((df["triage_tier"] == t).sum())
            rule = {"High": f">= {opts['high_threshold']} flags",
                    "Medium": f"{opts['medium_threshold']}-{opts['high_threshold'] - 1} flags",
                    "Low": f"0-{opts['medium_threshold'] - 1} flags, {opts['min_completeness']:.0%}+ of applicable flags checked, could not reach High",
                    "Insufficient data": f"0-{opts['medium_threshold'] - 1} flags, too few checked to rule out High"}[t]
            S.add(2, sec, f"Tier {t} ({rule}), all institutions", n, N)
        for t in ["High", "Medium", "Insufficient data"]:
            n = int(((df["triage_tier"] == t) & reach).sum())
            S.add(2, sec, f"Tier {t} among reachable sites", n, int(reach.sum()))
        S.add(2, sec, "Mean red-flag score (reachable sites)", value=round(float(df.loc[reach, "red_flag_score"].mean()), 2) if reach.any() else None)
        for c in fcols:
            ev = df[c].notna()
            S.add(2, sec, f"Flag: {FLAG_DEFINITIONS[c][0]}", int((df[c] == 1).sum()), int(ev.sum()), note="Denominator = institutions where this flag could be evaluated.")

    # 3 Validation
    with S.guard(3):
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
    with S.guard(4):
        sec = "4. Shared tracker IDs"
        if "has_any_tracker" in df.columns:
            rate(4, sec, "Sites with any analytics / tag / pixel ID", df["has_any_tracker"], content,
                 note="Denominator excludes homepages whose tracker fields were not captured.")
            allids = Counter(i for L in df.loc[content, "tracker_ids"] for i in L)
            S.add(4, sec, "Distinct tracker IDs found", len(allids))
            cl = clusters.get("tracker_id")
            if cl is not None and not cl.empty:
                S.add(4, sec, "Tracker-sharing clusters (2+ domains)", len(cl))
                S.add(4, sec, "Clusters spanning different owners (or owner unknown)", int(cl["cross_owner"].sum()))
                if "flag_shared_tracker" in df.columns:
                    rate(4, sec, "Institutions flagged for sharing a tracker across owners", df["flag_shared_tracker"])
                S.add(4, sec, "Largest cluster (domains)", int(cl["n_domains"].max()))
            else:
                S.add(4, sec, "Tracker-sharing clusters (2+ domains)", 0)
            if excluded is not None and not excluded.empty:
                ex = excluded[excluded["key_type"] == "tracker_id"]
                S.add(4, sec, f"Tracker IDs excluded as vendor/platform-wide (> {opts['vendor_threshold']} domains)", len(ex))
            if "trackers_captured_all" in df.columns:
                gap = int((content & ~df["trackers_captured_all"]).sum())
                if gap:
                    S.add(4, sec, "Homepages with tracker fields missing (sharing only partly checked)", gap, NC)
            if "inst_ope6" not in df.columns:
                S.add(4, sec, "Caveat", note="No OPEID6 owner data: branch campuses sharing a tracker count as 'cross-owner'. Provide --institutions to exclude same-owner pairs.")

    # 5 Templates
    with S.guard(5):
        sec = "5. Identical homepage templates"
        cl = clusters.get("template")
        if cl is not None:
            S.add(5, sec, "Template clusters (identical DOM structure, 2+ domains)", len(cl), note=f"Pages under {opts['template_min_words']} words excluded.")
            if not cl.empty:
                S.add(5, sec, "Clusters spanning different owners (or owner unknown)", int(cl["cross_owner"].sum()))
                S.add(5, sec, "Domains in template clusters", int(cl["n_domains"].sum()))
        if "structure_hashes" in df.columns:
            w5 = df["words_per_page"].where(content)
            small = w5 < opts["template_min_words"]
            S.add(5, sec, f"Near-empty homepages (< {opts['template_min_words']} words)", int(small.sum()), int(w5.notna().sum()),
                  note="Often parked, placeholder or error pages; review by hand.")
            h = df.loc[small, "structure_hashes"].map(lambda L: L[0] if L else None).dropna()
            vc = h.value_counts()
            S.add(5, sec, "  ...sharing an identical structure with another near-empty page", int(vc[vc > 1].sum()), int(small.sum()))
        elif cl is None:
            S.add(5, sec, "Not available", note="No structure hash column.")

    # 6 Shared URLs
    with S.guard(6):
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
    with S.guard(7):
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
    with S.guard(8):
        sec = "8. Top-level domain mix"
        for cat, n in df["tld_category"].value_counts().items():
            S.add(8, sec, f"Sites on {cat}", n, N)
        if "flag_named_univ_not_edu" in df.columns:
            S.add(8, sec, "'University' or 'College' in name, not on .edu", int((df["flag_named_univ_not_edu"] == 1).sum()), int(df["flag_named_univ_not_edu"].notna().sum()),
                  note=".edu requires accreditation from a recognized accreditor (domains registered before Oct 2001 are grandfathered).")

    # 9 WHOIS
    with S.guard(9):
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
    with S.guard(10):
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
    with S.guard(11):
        sec = "11. Homepage size vs. enrollment"
        if enroll_fit:
            S.add(11, sec, "Sites with enrollment and homepage text", enroll_fit["n"])
            S.add(11, sec, "Spearman correlation, log words vs. log enrollment", value=round(enroll_fit["spearman"], 3))
            S.add(11, sec, f"Far smaller than enrollment predicts (residual z < {opts['enrollment_residual_z']})",
                  int((df.get("low_size_for_enrollment", pd.Series(dtype=float)) == 1).sum()), enroll_fit["n"], note="See chart words_vs_enrollment.png.")
        else:
            S.add(11, sec, "Not run", note="Needs --institutions with an enrollment column (e.g., UGDS / latest.student.size).")

    # 12 Anomaly
    with S.guard(12):
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
    with S.guard(13):
        sec = "13. Physical-presence and contact signals"
        if NC:
            if "phone_number_hits" in df.columns:
                rate(13, sec, "Phone number on homepage", gt0(df["phone_number_hits"]), content)
            if "has_street_address" in df.columns:
                rate(13, sec, "Street address with ZIP on homepage", df["has_street_address"], content)
            if "mailto_count" in df.columns:
                rate(13, sec, "Any mailto: link on homepage", gt0(df["mailto_count"]), content)
            if "flag_no_physical_presence" in df.columns:
                rate(13, sec, "Neither phone nor street address", df["flag_no_physical_presence"])
            if "flag_free_or_offdomain_email" in df.columns:
                rate(13, sec, "Free-mail or off-domain contact email", df["flag_free_or_offdomain_email"])
            elif schema == "legacy_home_only":
                S.add(13, sec, "Free-mail contact email", note="Not reported: the current home-only crawl hard-codes free_email_hits to 0 (spec C01).")
            if "flag_stale_copyright" in df.columns:
                rate(13, sec, f"Copyright year {opts['stale_copyright_years']}+ years old", df["flag_stale_copyright"],
                     note="Denominator = homepages showing a copyright year.")
            elif schema == "legacy_home_only":
                S.add(13, sec, "Copyright-year staleness", note="Not reported: copyright years are parsed but not saved in home-only mode (spec C01).")
            if "title_name_overlap" in df.columns:
                t = df["title_name_overlap"]
                rate(13, sec, "Page title / site name shares under a third of the institution's name words", b01(t < 0.34, t.notna()))

    # 14 Thin homepages
    with S.guard(14):
        sec = "14. Thin homepages"
        w = df["words_per_page"].where(content)
        if w.notna().any():
            S.add(14, sec, "Median words on homepage", value=round(float(w.median()), 0))
            rate(14, sec, "Homepages under 100 words", b01(w < 100, w.notna()))
            if "flag_thin_homepage" in df.columns:
                rate(14, sec, f"Bottom {opts['thin_percentile']:g}% of peer group (flagged)", df["flag_thin_homepage"])
            if "total_internal_links" in df.columns:
                il = df["internal_links_per_page"].where(content)
                rate(14, sec, "Fewer than 5 internal links", b01(il < 5, il.notna()))

    # 15 Builders
    with S.guard(15):
        sec = "15. Consumer site builders"
        if "uses_site_builder" in df.columns and NC:
            rate(15, sec, "Consumer builder (Wix, Squarespace, Weebly, GoDaddy)", df["uses_site_builder"], content)
        if "site_builders_detected" in df.columns and NC:
            capb = content & list_ok(df, "site_builders_detected")
            bc = Counter(b for L in df.loc[capb, "site_builders_detected"] for b in L)
            for b, n in bc.most_common():
                S.add(15, sec, f"  {b}", n, int(capb.sum()))

    # 16 Link-outs
    with S.guard(16):
        sec = "16. Institutional link-outs"
        if NC:
            if "trusted_outbound_links" in df.columns:
                t = df["trusted_outbound_links"]
                rate(16, sec, "No links to accreditors or .gov sites", b01(t == 0, t.notna()), content,
                     note="Weak alone: many legitimate homepages do not link their accreditor.")
            if "num_subdomains_seen" in df.columns:
                sd = df["num_subdomains_seen"].where(content)
                rate(16, sec, "Links to 1 or fewer of its own hostnames", b01(sd <= 1, sd.notna()))
                if sd.notna().any():
                    S.add(16, sec, "Median own hostnames linked", value=float(sd.median()))
            if "flag_unrecognized_accreditor" in df.columns:
                rate(16, sec, "Links an unrecognized 'accreditation' domain", df["flag_unrecognized_accreditor"])
            if "recognized_accreditor_domains_linked" in df.columns:
                rate(16, sec, "Links a recognized accreditor",
                     b01(df["recognized_accreditor_domains_linked"].map(len) > 0, list_ok(df, "recognized_accreditor_domains_linked")), content)

    # 17 Tracking
    with S.guard(17):
        sec = "17. Recruitment-funnel tracking"
        if "has_fb_pixel" in df.columns and NC:
            rate(17, sec, "Meta (Facebook) pixel on homepage", df["has_fb_pixel"], content)
            if "flag_no_physical_presence" in df.columns:
                rate(17, sec, "Pixel AND no phone or street address", all3(df["has_fb_pixel"], df["flag_no_physical_presence"]), content)
            if "gtm_ids" in df.columns:
                rate(17, sec, "Google Tag Manager container present", b01(df["gtm_ids"].map(len) > 0, list_ok(df, "gtm_ids")), content)

    # 18 Metadata
    with S.guard(18):
        sec = "18. Metadata completeness"
        if "metadata_index" in df.columns and NC:
            mi = df["metadata_index"]
            mx = int(df["metadata_index_max"].iloc[0])
            S.add(18, sec, f"Mean metadata index (0-{mx})", value=round(float(mi.mean()), 2) if mi.notna().any() else None,
                  note="Scaled to the components captured; needs at least 3 of them.")
            rate(18, sec, "Index of 0 or 1", b01(mi <= 1, mi.notna()))
            for c, lab in [("has_favicon", "Favicon"), ("pct_pages_meta_description", "Meta description"), ("pct_pages_og_tags", "Open Graph tags"),
                           ("pct_pages_canonical", "Canonical link"), ("has_edu_schema_markup", "schema.org education markup")]:
                if c in df.columns:
                    rate(18, sec, f"  {lab}", gt0(df[c]), content)

    # 19 Alt text
    with S.guard(19):
        sec = "19. Image alt-text coverage"
        if "img_alt_coverage" in df.columns and df["img_alt_coverage"].where(content).notna().any():
            a = df["img_alt_coverage"].where(content)
            S.add(19, sec, "Median alt-text coverage (%)", value=round(100 * float(a.median()), 1), note="Homepages with images only.")
            rate(19, sec, "Under 50% of images have alt text", b01(a < 0.5, a.notna()))

    # 20 CMS
    with S.guard(20):
        sec = "20. CMS, generators and frameworks"
        if NC and "meta_generators" in df.columns:
            capg = content & list_ok(df, "meta_generators")
            gc = Counter(re.split(r"[\s/]", str(g))[0].lower() for L in df.loc[capg, "meta_generators"] for g in L)
            S.add(20, sec, "Sites declaring a meta generator", int((df.loc[capg, "meta_generators"].map(len) > 0).sum()), int(capg.sum()),
                  value="; ".join(f"{k} ({v})" for k, v in gc.most_common(8)))
        if NC and "css_frameworks" in df.columns:
            capf = content & list_ok(df, "css_frameworks")
            fc = Counter(f for L in df.loc[capf, "css_frameworks"] for f in L)
            S.add(20, sec, "Sites using a CSS framework", int((df.loc[capf, "css_frameworks"].map(len) > 0).sum()), int(capf.sum()),
                  value="; ".join(f"{k} ({v})" for k, v in fc.most_common()))

    # 21 HTTP landing
    with S.guard(21):
        sec = "21. Plain-HTTP landing"
        if "https_redirect" in df.columns and df["https_redirect"].notna().any():
            h = df["https_redirect"]
            S.add(21, sec, "Final page served over plain HTTP", int((h == 0).sum()), int(h.notna().sum()))

    # 22 Supervised readiness
    with S.guard(22):
        sec = "22. Supervised model readiness"
        if validation is not None and not validation.empty:
            nb = validation.groupby("label")["n_labelled"].max()
            for lab, n in nb.items():
                S.add(22, sec, f"Labelled institutions: {lab}", int(n), note="Roughly 50+ labelled bad schools are needed before a supervised model is worth fitting.")
        else:
            S.add(22, sec, "No labels supplied", note="A supervised model needs labelled known-bad institutions.")

    # 23 Coverage
    with S.guard(23):
        S.add(23, "23. Data coverage", "See the Coverage and Run_info tabs", note="Field completeness and known caveats. Read the Run_info warnings before quoting numbers.")

    # 24 Deep crawl
    with S.guard(24):
        sec = "24. Deep-crawl disclosures"
        for c, lab in [("has_net_price_calculator_link", "Net price calculator link"), ("has_consumer_info_link", "Consumer information link"),
                       ("has_security_report_link", "Annual security report link"), ("has_accreditation_page", "Accreditation page")]:
            if c in df.columns and df[c].notna().any():
                S.add(24, sec, lab, int((df[c] == 1).sum()), int(df[c].notna().sum()),
                      note="Net price calculator applies only to schools enrolling first-time, full-time undergraduates." if "net_price" in c else "")

    inst_info["_summary_errors"] = S.errors
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
    "data_completeness_pct": "Applicable flags checked (%)", "page_capture_pct": "Core page fields captured (share)",
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
    fetched = df["availability_category"] == "ok"
    watch = (["crawl_error", "landing_status_code", "tld", "registered_domain"] + NUMERIC_COLUMNS + BOOL_COLUMNS
             + TRACKER_COLUMNS + ["structure_hashes", "site_builders_detected", "meta_generators", "ssl_issuer_org", "ssl_cert_error",
                                  "whois_registrant_org", "page_title", "final_url", "error_category", "phone_numbers",
                                  "contact_emails", "text_simhash64", "html_sha256", "ssl_cert_sha256",
                                  "n_sites_sharing_tracker_id", "max_structure_similarity_other_site", "n_sites_sharing_css_file"])
    rows, seen = [], set()
    for c in watch:
        if c in seen:
            continue
        seen.add(c)
        legacy_name = next((o for o, n in LEGACY_ALIASES.items() if n == c), None)
        present = c in cols_in or (legacy_name in cols_in if legacy_name else False) or (c == "structure_hashes" and "structure_hash" in cols_in)
        all_pct = c_pct = miss_f = malformed = None
        if c in df.columns:
            if f"_lst_{c}" in df.columns:
                st = df[f"_lst_{c}"]
                nn = st.isin(OK_STATUS)
                malformed = int(st.isin(["malformed", "partly_malformed"]).sum())
            else:
                nn = df[c].map(lambda v: (len(v) > 0) if isinstance(v, list) else not _isnan(v)).astype(bool)
            all_pct = pct(int(nn.sum()), len(df))
            c_pct = pct(int(nn[content].sum()), int(content.sum())) if content.any() else None
            miss_f = None if c in SEMANTIC_NULL else int((fetched & ~nn).sum())   # blank is normal for these
        note = COVERAGE_NOTES_LEGACY.get(c, "") if schema == "legacy_home_only" else ""
        if c in SEMANTIC_NULL:
            note = (note + " " if note else "") + f"Blank can be legitimate here: {SEMANTIC_NULL[c]}."
        if not present:
            note = (note + " " if note else "") + "Not in this file."
        rows.append({"column": c, "present_in_file": present, "captured_pct_all_rows": all_pct,
                     "captured_pct_pages_with_content": c_pct, "missing_on_fetched_homepages": miss_f,
                     "malformed_values": malformed, "note": note.strip()})
    out = pd.DataFrame(rows)
    return out.sort_values(["present_in_file", "missing_on_fetched_homepages"], ascending=[False, False], na_position="last").reset_index(drop=True)


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
    with soft_fail(warn, "Chart 'availability'"):
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
    with soft_fail(warn, "Chart 'red-flag score distribution'"):
        reach = df["availability_group"].isin(["reachable", "blocked"])
        sub = df.loc[reach, ["red_flag_score", "triage_tier"]]
        if not sub.empty:
            insuff = sub["triage_tier"] == "Insufficient data"
            xs = list(range(0, int(sub["red_flag_score"].max()) + 1))
            checked = sub.loc[~insuff, "red_flag_score"].value_counts().reindex(xs, fill_value=0)
            unchecked = sub.loc[insuff, "red_flag_score"].value_counts().reindex(xs, fill_value=0)
            fig, ax = plt.subplots(figsize=(7.5, 3.9))
            colors = [PALETTE["seq_dark"] if x >= opts["high_threshold"] else PALETTE["seq_light"] for x in xs]
            ax.bar(xs, checked.values, width=0.7, color=colors, label="Enough flags checked")
            if unchecked.sum():
                ax.bar(xs, unchecked.values, width=0.7, bottom=checked.values, color=PALETTE["axis"],
                       edgecolor=PALETTE["surface"], linewidth=1.5, label="Too few flags checked to rule out High")
            for x, a, b in zip(xs, checked.values, unchecked.values):
                if a + b:
                    ax.text(x, a + b, f"{a + b:,}", ha="center", va="bottom", fontsize=8.5, color=PALETTE["ink2"])
            hi = int(checked[[x for x in xs if x >= opts["high_threshold"]]].sum())
            ax.set_title(f"{hi:,} reachable sites raise {opts['high_threshold']}+ red flags (High tier)")
            ax.set_xlabel("Red flags raised")
            ax.set_ylabel("Sites")
            ax.set_xticks(xs)
            ax.grid(axis="x", visible=False)
            if unchecked.sum():
                ax.legend(frameon=False, fontsize=8, loc="upper right")
            finish(fig, "red_flag_scores.png", "Dark bars = High tier. Gray = sites tiered 'Insufficient data'. Reachable and bot-blocking sites only.")

    # 3 flag prevalence
    with soft_fail(warn, "Chart 'flag prevalence'"):
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
    with soft_fail(warn, "Chart 'TLD mix'"):
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
    with soft_fail(warn, "Chart 'domain age'"):
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
    with soft_fail(warn, "Chart 'words vs enrollment'"):
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
    plt.close("all")
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

    df, schema, cols_in, load_info = load_site_features(site_features, id_col, url_col, warn)

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
    inst_info = dict(load_info)
    label_sets = {}
    if institutions is not None:
        inst, colmap = load_institutions(institutions, column_map or {}, warn)
        before = len(df)
        df = df.merge(inst, left_on=id_col, right_on="_id", how="left").drop(columns=["_id"])
        if len(df) != before:
            warn.append("Institutions join changed the row count; check for duplicate IDs in the institutions file.")
        matched = int(df[[c for c in df.columns if c.startswith("inst_")]].notna().any(axis=1).sum()) if any(c.startswith("inst_") for c in df.columns) else 0
        warn.append(f"Institutions file: matched {matched} of {len(df)} crawled UNITIDs.")
        if len(df) and matched / len(df) < 0.5:
            warn.append("Fewer than half the UNITIDs matched the institutions file: check both files use the same ID column and format.")
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

    # core steps (must succeed)
    df = add_availability(df)
    df = add_metrics(df, opts, schema)
    df["peer_group"] = _try(warn, "Peer groups", assign_peer_groups, df, opts["min_peer_n"],
                            default=pd.Series("All institutions", index=df.index))
    # optional steps: a failure is recorded in Run_info and the rest of the sheet is still built
    xs = _try(warn, "Cross-site sharing", run_cross_site, df, opts, id_col)
    if xs is None:
        clusters = {}
        edges = pd.DataFrame(columns=["key_type", "domain_a", "domain_b", "shared_key"])
        excluded = pd.DataFrame(columns=["key_type", "key", "n_domains", "n_domains_estimated", "n_domains_estimated_high"])
    else:
        df, clusters, edges, excluded = xs
    shared = _try(warn, "Shared-URL table", shared_url_groups, df, id_col, url_col, default=pd.DataFrame())
    df, fcols = build_flags(df.copy(), opts, schema, warn)
    df = _try(warn, "Peer anomaly score", add_anomaly, df.copy(), opts, iforest, warn, default=df).copy()
    enroll_fit = _try(warn, "Size-vs-enrollment fit", add_enrollment_residual, df, opts, default={}) or {}
    df = df.copy()
    validation = (_try(warn, "Validation", build_validation, df, fcols, label_sets, warn, default=pd.DataFrame())
                  if label_sets else pd.DataFrame())

    summary = build_summary(df, fcols, clusters, shared, excluded, validation, enroll_fit, opts, schema, id_col, url_col, inst_info)
    warn.extend(inst_info.get("_summary_errors", []))

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
            "landing_status_code", "crawl_error", "triage_tier", "red_flag_score", "data_completeness_pct",
            "n_flags_applicable", "n_flags_evaluated", "max_possible_score", "flag_reasons", "flags_not_checked",
            "capture_status", "page_capture_pct", "missing_page_fields", "js_shell_suspected_any"]
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
    rc_cols = [c for c in [id_col, url_col, "inst_name", "registered_domain", "recrawl_reason", "capture_status", "page_capture_pct",
                           "missing_page_fields", "triage_tier", "red_flag_score", "data_completeness_pct"] if c in df.columns]
    recrawl = df.loc[df["recrawl"] == 1, rc_cols].sort_values("page_capture_pct", na_position="first")

    dist = _try(warn, "Distributions", distributions, df, default=pd.DataFrame())
    xts = _try(warn, "Crosstabs", crosstabs, df, fcols, default={}) or {}
    tops = _try(warn, "Top values", top_values, df, default=pd.DataFrame())
    cov = _try(warn, "Coverage table", coverage, df, cols_in, schema, default=pd.DataFrame())

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
                               "applies_to": SCOPE_LABELS[FLAG_SCOPE.get(c, "page")],
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
    w(recrawl, "recrawl_candidates.csv")
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

    chart_files = (_try(warn, "Charts", make_charts, df, fcols, out, opts, warn, default=[]) or []) if charts else []

    readme = pd.DataFrame([
        {"tab": "Summary", "what it holds": "Headline statistics in ranked order (1 = most relevant to fraud screening). Section 0 reports data capture: read it first. percent = count / denominator, and denominators count only rows where the value is known."},
        {"tab": "Flag_definitions", "what it holds": "Every red flag, its rule, and whether it was active in this run."},
        {"tab": "Availability", "what it holds": "Every institution URL by failure cause. 'counts_as_broken' marks the categories in the broken total."},
        {"tab": "Institutions", "what it holds": "One row per UNITID: availability, flags (1 raised, 0 checked, blank = could not be checked), score, data_completeness_pct, max_possible_score, tier, capture status, anomaly scores, cluster IDs."},
        {"tab": "Deep_dive_candidates", "what it holds": "Reachable sites to crawl in depth. Has UNITID + URL columns, so it can be fed straight to the crawler."},
        {"tab": "Verify_status", "what it holds": "Broken URLs to verify by other means (Scorecard status, Wayback Machine, WHOIS)."},
        {"tab": "Recrawl_candidates", "what it holds": "Homepages that were fetched but whose fields were not (fully) captured, or that look built with JavaScript (recrawl_reason says which). Re-crawl them; the crawler skips URLs already in its checkpoint unless they are removed or passed with --recrawl-list (spec C31)."},
        {"tab": "Validation", "what it holds": "Flag rates among known-problem schools vs. the rest (lift), and score AUC."},
        {"tab": "Clusters_*", "what it holds": "Groups of domains linked by a shared key (tracker ID, template, ...). cross_owner = different OPEID6 or owner unknown."},
        {"tab": "Edges", "what it holds": "Domain pairs and the key they share; load into a network-graph tool."},
        {"tab": "Excluded_keys", "what it holds": "Keys shared by too many domains to count (web vendors, platforms). n_domains_estimated allows for sites whose key field was not captured."},
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
                  ("Recrawl_candidates", recrawl),
                  ("Validation", validation)]
        sheets += [(f"Clusters_{k}", v) for k, v in cluster_frames.items()]
        sheets += [("Edges", edges), ("Excluded_keys", excluded), ("Shared_URLs", shared), ("Distributions", dist)]
        sheets += [(f"XT_{k}", v) for k, v in xts.items()]
        sheets += [("Top_values", tops), ("Coverage", cov), ("Run_info", run_info)]
        _try(warn, "Excel workbook", write_excel, out / "stats_sheet.xlsx", sheets, warn)
        # refresh run_info warnings written after the workbook (chart/excel warnings)
    if warn:
        run_info = pd.concat([run_info[run_info["item"] != "Warning"], pd.DataFrame([{"item": "Warning", "value": x} for x in warn])], ignore_index=True)
        w(run_info, "run_info.csv")

    return {"summary": summary, "institutions": inst_table, "deep_dive_candidates": deep, "verify_status": verify,
            "recrawl_candidates": recrawl,
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
    try:
        res = build_stats(a.site_features, institutions=a.institutions, known_bad=a.known_bad, out_dir=a.out_dir,
                          id_col=a.id_column, url_col=a.url_column, column_map=_parse_map(a.map),
                          charts=not a.no_charts, excel=not a.no_excel, iforest=a.iforest, **overrides)
    except (ValueError, KeyError, FileNotFoundError, pd.errors.ParserError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    s = res["summary"]
    print(f"schema: {res['schema']}")
    print(f"institutions: {len(res['institutions'])}")
    b = s[s["metric"].str.startswith("BROKEN")]
    if len(b):
        print(f"broken URLs: {b['count'].iloc[0]} ({b['percent'].iloc[0]}%)")
    for t in ("High", "Medium", "Insufficient data"):
        r = s[s["metric"].str.startswith(f"Tier {t} (")]
        if len(r):
            print(f"tier {t}: {r['count'].iloc[0]}")
    print(f"deep-dive candidates: {len(res['deep_dive_candidates'])}; verify-status: {len(res['verify_status'])}; "
          f"re-crawl: {len(res['recrawl_candidates'])}")
    for x in res["warnings"]:
        print(f"warning: {x}")
    print(f"outputs: {Path(a.out_dir).resolve()}")


if __name__ == "__main__":
    main()