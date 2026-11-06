# Change Specification: `university_site_features.py` v2

**Repository:** `maxwellmg/University_url_metadata_test`
**Files in scope:** `university_site_features.py`, `site_feature_engineering.py`, `data_dictionary.csv`, `README.md`, `CHANGES.md`, `requirements.txt`, new `reference/accreditors.csv`, new `enrich_external.py`, new `tests/`
**Audience:** a coding agent implementing these changes without other context.
**Line numbers** refer to the current `main` branch and will drift as you edit. Search for the quoted code if a line has moved.

---

## 0. Ground rules for the implementer

1. **Home-only mode stays light.** `--home-only` must still make **one GET of the homepage per site**. The only extra HTTP requests allowed in home-only mode are the fallbacks in C09, and only when the first request fails. DNS, TLS-handshake and WHOIS/RDAP lookups are not page fetches and are allowed.
2. **Do not break multi-page mode.** Multi-page column names are canonical. Home-only mode must now write the *same* names (C01). Do not keep the old home-only names (`word_count`, `a_tag_count`, etc.). The downstream stats script already handles both the old and the new names.
3. **Every new column must be documented** in `data_dictionary.csv` (C28) with the same columns that file already uses: `variable,dataset,stage,data_type,description,modeling_notes`.
4. **List-valued columns are written as JSON arrays** (for example `["G-ABC123","GTM-XYZ"]`), never Python reprs. Booleans are written as `True`/`False`. Dates are ISO 8601 (`YYYY-MM-DD`, or full timestamps in UTC).
5. **Caps on list lengths** are config values (`CONFIG["max_list_items"]`, default 50, unless a change says otherwise), so a pathological page cannot bloat the CSV.
6. **Never store personal names** from WHOIS. Store organization names only. Contact details in C15 are institutional details published on the site; store them only for cross-site matching.
7. **Add `SCRIPT_VERSION = "2.0.0"`** near `CONFIG` and write it to every row (C21).
8. **Tests run offline** (C29). Mock `requests` and sockets; do not hit the network in the test suite.
9. Suggested order of work: C05 → C01 → C31 → C03 → C02 → C04 → C21 → C07 → C08 → C09 → C10–C19 → C22 → C20 → C06 → C28–C30 → P2 (C23–C27).

### Priority key

| Priority | Meaning |
| --- | --- |
| P0 | Fixes data that is currently wrong or silently missing. Required before the next run. |
| P1 | New capture in home-only mode at no extra page-fetch cost. Strongly recommended before the next run. |
| P2 | Targeted deep-dive mode for the subset flagged by the stats sheet. |
| P3 | Documentation, tests, dependencies. |

---

## P0: Correctness fixes

### C01. Home-only mode writes canonical columns through `aggregate_site_features`

**Problem.** The home-only branch of `crawl_site` (lines ~692–727) hand-copies a subset of page features under different names than multi-page mode, and it drops or zeroes several fields:

- `free_email_hits` is hard-coded to `0` (line ~721) even though `extract_page_features` computes it.
- `copyright_years` is parsed but `max_copyright_year` / `min_copyright_year` are never written. The README claims home-only captures copyright year.
- `mailto_on_domain_ratio`, `num_stylesheets_landing`, `avg_inline_styles_per_page`, `server_headers`, `has_last_modified_header` are never written.
- `structure_hash` (string) is written instead of `structure_hashes` (list). Cross-site similarity reads `structure_hashes`, so home-only sites always score 0 (see C04).
- Volume columns use names (`word_count`, `a_tag_count`, `internal_link_count`, `external_link_count`, `trusted_link_count`, `img_count`, `html_bytes`) that `site_feature_engineering.py` does not recognize. Most engineered features are therefore absent from `model_matrix.csv` for home-only runs.

**Change.** After a successful homepage fetch and parse, replace the hand-copied block with:

```python
page_feats = extract_page_features(resp.text, resp.url, site_domain)
result.update(aggregate_site_features([page_feats], site_domain, [dict(resp.headers)]))
result["pages_crawled"] = 1
result["dom_structure_diversity"] = None   # undefined for a single page
result["boilerplate_ratio"] = None         # aggregate already returns None when n < 3
result["css_content_hashes"] = []          # not fetched in home-only (see option below)
```

Keep setting `https_redirect` and `landing_status_code` as today.

**Optional config:** `CONFIG["home_only_fetch_css"] = False`. When `True`, fetch up to `max_css_files_per_site` stylesheets and fill `css_content_hashes` exactly as multi-page mode does (lines ~818–830). Default `False` keeps home-only to one page request.

**Acceptance criteria**
- A home-only row contains `total_words`, `total_a_tags`, `total_internal_links`, `total_external_links`, `trusted_outbound_links`, `total_images`, `total_html_bytes`, `avg_words_per_page`, `avg_images_per_page`, `img_alt_coverage`, `text_to_html_ratio`, `structure_hashes` (one-element list), `num_stylesheets_landing`, `avg_inline_styles_per_page`, `meta_generators`, `server_headers`, `has_last_modified_header`, `mailto_count`, `mailto_on_domain_ratio`, `free_email_hits`, `phone_number_hits`, `has_street_address`, `max_copyright_year`, `min_copyright_year`.
- None of the old names (`word_count`, `a_tag_count`, `internal_link_count`, `external_link_count`, `trusted_link_count`, `img_count`, `img_alt_count`, `html_bytes`, `text_bytes`, `structure_hash`) appear.
- A fixture page containing `admissions@gmail.com` yields `free_email_hits >= 1`.
- A fixture page containing `© 2019 Example College` yields `max_copyright_year == 2019`.

---

### C02. SSL certificate fields are empty when verification is off or fails

**Problem.** `ssl_features` (lines ~246–298):
- With `verify_ssl=False` (`--insecure`), it uses `CERT_NONE`. Python's `SSLSocket.getpeercert()` returns an **empty dict** when the certificate was not validated. So `ssl_ok=True` but `ssl_issuer_org=None`, `ssl_days_to_expiry=None`, and `ssl_org_validated=False` for every site. That last value is wrong: every site reads as a domain-validated (DV) certificate.
- With verification on, a failed verification records `ssl_cert_error` but captures no certificate fields. Expired and self-signed certificates are exactly the ones worth describing.

**Change.** Rewrite `ssl_features(hostname)` to do two things on every site:
1. **Verified handshake** with `ssl.create_default_context()`. Record `ssl_verified` (bool) and, on failure, `ssl_cert_error` (string, truncated to 300 characters).
2. **Unverified retrieval** with `CERT_NONE` and `getpeercert(binary_form=True)`. Parse the DER with `cryptography.x509.load_der_x509_certificate` and fill the fields below. Use the `*_utc` datetime properties (`not_valid_before_utc`, `not_valid_after_utc`) from `cryptography>=42`.

Keep the function independent of `CONFIG["verify_ssl"]`: the HTTP request may be insecure, but the certificate report must always be complete.

| Column | Type | Definition |
| --- | --- | --- |
| `ssl_ok` | bool | A TLS handshake on port 443 succeeded (verified or not) and a certificate was received. |
| `ssl_verified` | bool | The certificate chain and hostname verified against the default trust store. |
| `ssl_cert_error` | str or null | Verification error message, if any. |
| `ssl_issuer_org` | str or null | Issuer organization (O). |
| `ssl_issuer_cn` | str or null | Issuer common name (CN). |
| `ssl_subject_org` | str or null | Subject organization (O). Present on organization-validated (OV) and extended-validation (EV) certs. |
| `ssl_subject_cn` | str or null | Subject common name. |
| `ssl_org_validated` | bool or null | `ssl_subject_org` is present. Null when no certificate was received. |
| `ssl_not_before` | ISO date | Certificate start date. |
| `ssl_not_after` | ISO date | Certificate end date. |
| `ssl_days_to_expiry` | int | Days from crawl to `ssl_not_after` (negative = expired). |
| `ssl_cert_age_days` | int | Days from `ssl_not_before` to crawl. |
| `ssl_validity_days` | int | `ssl_not_after - ssl_not_before` in days (about 90 suggests automated free issuance). |
| `ssl_san_domains` | list[str] | DNS names in the Subject Alternative Name extension (cap 50). |
| `ssl_san_count` | int | Number of SAN DNS names (uncapped count). |
| `ssl_san_other_registered_domains` | list[str] | Distinct registered domains in the SANs other than the site's own (cap 50). |
| `ssl_cert_sha256` | str | SHA-256 fingerprint of the DER certificate (hex). Cross-site key in C22. |

**Acceptance criteria**
- A test that serves a self-signed certificate (generate it in the test with `cryptography`) yields `ssl_ok=True`, `ssl_verified=False`, `ssl_cert_error` non-null, and non-null `ssl_not_after`, `ssl_cert_sha256`.
- Running with `--insecure` no longer produces `ssl_org_validated=False` for a site whose certificate has a subject O.

---

### C03. Failed rows keep their domain-level fields

**Problem.** In `build_feature_dataframe` (lines ~1271–1291), rows with a `crawl_error` have every column outside a hard-coded `keep_cols` set overwritten with `NA`. That erases `domain_age_days`, `whois_*`, `ssl_*` and `ssl_cert_error` for exactly the sites where they matter most: dead or broken sites.

**Change.** Replace the hard-coded set with a module-level constant and prefix list:

```python
DOMAIN_LEVEL_PREFIXES = (
    "ssl_", "whois_", "domain_", "dns_", "nameservers", "has_mx_record",
    "parking_", "parked_", "final_", "redirect", "error_", "primary_error_",
    "fetch", "http_fallback", "www_fallback", "bot_protection", "cdn_",
    "server_header", "response_time_ms", "landing_", "html_archive_path",
    "placeholder_page", "default_server_page",
)
DOMAIN_LEVEL_COLUMNS = {
    "input_url", "registered_domain", "tld", "crawl_timestamp", "crawl_error",
    "crawl_mode", "script_version", "crawl_duration_sec", "ssl_bypass_used",
    "landing_status_code", "https_redirect", "pages_crawled", "robots_blocked_count",
}
```

On failed rows, null only the columns that are **not** in `DOMAIN_LEVEL_COLUMNS`, do not start with a `DOMAIN_LEVEL_PREFIXES` entry, and are not the ID/URL columns. Page-level fields stay null (not 0) on failed rows.

**Acceptance criteria**
- For a mocked DNS failure, the output row has `crawl_error` set, `error_category="dns_failure"`, and any WHOIS/SSL values the lookups returned are preserved.

---

### C04. Cross-site features work in every mode and on resumed runs

**Problem.** `add_cross_site_features` (lines ~1301–1339):
- Reads `structure_hashes` and `css_content_hashes`, which home-only mode never writes, so `max_structure_similarity_other_site` and `n_sites_sharing_css_file` are always 0 in home-only runs.
- `as_set()` only accepts real lists. Rows reloaded from the checkpoint CSV hold strings like `"['G-ABC']"`, so any site crawled in an earlier invocation (a resumed run, or the README's retry-with-`--insecure` step) silently contributes nothing to `n_sites_sharing_tracker_id`.
- Counts rows, not domains. Two input URLs on the same registered domain count as "sharing."
- O(n²) Python loop. Slow for ~6,000 institutions.

**Change.**
1. Parse list-like values with a shared helper (move `_parse_listlike` from `site_feature_engineering.py` into a small shared module or duplicate it). It must accept real lists, JSON arrays and Python-repr lists.
2. Build features at the **registered-domain** level: collapse rows by `registered_domain`, taking the union of each key set.
3. Use an **inverted index** (`key -> set(domains)`) instead of the pairwise loop.
4. Drop placeholder tracker IDs (regex `X{4,}` in the ID, e.g. `G-XXXXXXXXXX`) and keys present on more than `CONFIG["cross_site_vendor_threshold"]` (default 25) domains. Write the dropped keys to `cross_site_excluded_keys.csv` with columns `key_type,key,n_domains` so they can be reviewed (they are usually a web vendor or platform, which is itself useful context).
5. Form clusters with union-find over domains that share any key of a given type.

**Columns (per row, mapped back from its domain)**

| Column | Type | Definition |
| --- | --- | --- |
| `n_domains_sharing_tracker_id` | int | Distinct other registered domains sharing at least one GA/GA4/GTM/pixel ID. |
| `tracker_cluster_id` | str or null | Cluster label (the alphabetically first domain in the cluster). Null if unshared. |
| `n_domains_sharing_template` | int | Distinct other domains with an identical DOM structure hash. |
| `template_cluster_id` | str or null | As above, for templates. |
| `n_domains_sharing_css_file` | int | Distinct other domains sharing a CSS content hash (multi-page, or home-only with CSS fetch on). |
| `max_structure_similarity_other_site` | float | Keep. Jaccard over `structure_hashes`, now computed correctly. |
| `n_sites_sharing_css_file`, `n_sites_sharing_tracker_id` | int | Keep for backward compatibility; compute as the domain-level counts above. |

**Acceptance criteria**
- Two fixture rows on different domains with `ga4_ids='["G-ABC1234"]'` (string) get `n_domains_sharing_tracker_id == 1` and the same `tracker_cluster_id`.
- Two rows on the **same** registered domain sharing an ID get `0`.
- 6,000 synthetic domains finish cross-site computation in under 30 seconds.

---

### C05. Checkpoint integrity, mode-aware resume, and output-path flags

**Problem.**
- Lists are stringified with Python repr on save and never parsed on load (root cause of the C04 resume bug).
- Resume logic (`if url in done: continue`) ignores mode. A deep-dive run that reuses the home-only checkpoint skips every site.
- The CLI has no flags for the checkpoint or output path, so a second run of any kind overwrites the first.
- README says `site_features_checkpoint.json`; the code writes `site_features_checkpoint.csv`.

**Change.**
1. In `_save_checkpoint`, serialize `list`, `set`, `tuple` and `dict` values with `json.dumps(sorted(v) if isinstance(v, set) else v)`.
2. In `_load_checkpoint`, parse JSON-looking strings back to Python objects (fall back to `ast.literal_eval` for legacy Python-repr lists).
3. Treat a checkpoint row as done only if `row.get("crawl_mode") == current_mode`. Rows from another mode, or legacy rows with no `crawl_mode`, are re-crawled.
4. Add CLI flags: `--checkpoint PATH`, `--output PATH`, `--mode {home_only,multi_page}` (keep `--home-only` as an alias for `--mode home_only`), and `--retry-failed-only` (crawl only URLs whose checkpoint row has a `crawl_error`).
5. Write the final `site_features.csv` with JSON-encoded lists too.
6. Update README to say the checkpoint is a CSV.

**Acceptance criteria**
- Round trip: save a row with `ga4_ids=["G-1"]`, load it, and get a Python list back.
- Running `--mode multi_page --checkpoint deep_ckpt.csv` after a home-only run crawls every listed site.

---

### C06. Align `site_feature_engineering.py` with v2 columns

**Change.**
- Add every new list column from C02, C04, C07, C10, C13–C19 and C22 to `LIST_COLUMNS`.
- Add new booleans to `BOOL_COLUMNS`: `ssl_verified`, `redirected_off_domain`, `http_fallback_used`, `www_fallback_used`, `dns_resolves`, `dns_resolves_www`, `has_mx_record`, `parking_nameserver_detected`, `parked_domain_detected`, `placeholder_page_detected`, `default_server_page_detected`, `bot_protection_detected`, `has_hsts`, `has_csp`.
- Add new numerics to `PASSTHROUGH_NUMERIC`: `ssl_cert_age_days`, `ssl_validity_days`, `ssl_san_count`, `domain_days_to_expiry`, `days_since_whois_update`, `redirect_hops`, `response_time_ms`, `last_modified_age_days`, and every `n_domains_sharing_*` column.
- One-hot encode `error_category` (prefix `err_`) and `whois_privacy_type` (prefix `whois_privacy_`).
- Derive `n_external_domains = len(external_domains)`, `n_social_platforms = len(social_platforms_linked)`, `n_unrecognized_accredit_domains = len(unrecognized_accredit_domains)`.
- For `crawl_mode == "home_only"` rows, set `pages_hit_cap = 0` explicitly.
- `_parse_listlike` must accept JSON arrays (it already tries `json.loads`; keep it).

---

### C31. Capture integrity: partial extraction, explicit "none found", JavaScript pages, targeted re-crawl

**Problem.** Missed fields are common in homepage scraping, and the current code makes them hard to see or fix:
- `extract_page_features` is all-or-nothing. One exception in any regex or parser discards the whole page (`crawl_error = "Page extraction failed: ..."`), including the fields that did extract.
- "Checked, none found" and "not captured" look the same downstream. Lists default to `[]` whether or not the extractor ran, and several fields are null both when nothing was found and when nothing was attempted.
- `requests` does not run JavaScript. Sites built with JavaScript frameworks return a large HTML shell with almost no visible text or links, so word counts, links, phone numbers and addresses read as near zero. Downstream, these look like thin sites with no contact details.
- Resume logic skips any URL already in the checkpoint, so there is no way to re-crawl only the sites with capture gaps.
- Transient failures (one timeout, one 503) are recorded as final on the first attempt.

**Change.**

1. **Partial extraction.** Split `extract_page_features` into extractor groups and wrap each in its own `try/except`:

   | Group | Fields |
   | --- | --- |
   | `text` | visible text, `total_words`, `phone_number_hits`, `has_street_address`, ZIP, copyright years, C15 contact values |
   | `links` | anchors, internal/external links, PDF links, trusted links, mailto, subdomains, C16 domain lists, C19 social links |
   | `images` | image count, alt-text count |
   | `head` | meta description, Open Graph, canonical, favicon, meta generator, schema.org markup, C14 identity text |
   | `structure` | DOM hash, stylesheets, inline styles, CSS frameworks, site builders |
   | `trackers` | GA, GA4, GTM, Meta pixel IDs |
   | `fingerprints` | C18 hashes and SimHash |

   When a group raises, set that group's fields to null and append the group name to `extraction_errors`. The page counts as failed (`error_category = "extraction_failed"`) only if every group fails.

2. **Explicit values.** A group that ran writes `0`, `False` or `[]` for "none found". Null means "not captured": the group did not run, or it failed. These fields keep null as a legitimate "nothing to measure" value; document each in `data_dictionary.csv`:
   - `img_alt_coverage` (no images)
   - `mailto_on_domain_ratio` (no mailto links)
   - `max_copyright_year` / `min_copyright_year` (no year shown). Add `copyright_found` (bool) so "none shown" is explicit.
   - `ssl_cert_error`, `crawl_error`

   On failed rows, page-level fields stay null (C03).

3. **JavaScript-built pages.** Write `n_script_tags` (int) and `js_shell_suspected` (bool). The flag is true when all of these hold:
   - the HTML is at least 20 KB
   - the page has fewer than 50 visible words
   - it has fewer than 5 anchors
   - at least one of these is present: a root mount element (`id="root"`, `id="app"`, `id="__next"`, `id="___gatsby"`, an `ng-version` attribute, `data-reactroot`); a `<noscript>` element mentioning "JavaScript"; or 10 or more `<script>` tags.

4. **Optional rendering fallback.** Add `CONFIG["render_js"] = False` and a `--render-js` flag. When it is on and `js_shell_suspected` is true:
   - Re-fetch the page with Playwright (headless Chromium, same User-Agent, 20 s timeout, wait for network idle).
   - Re-run extraction on the rendered HTML and set `rendered_with_js = True`.

   This is the only extra page request allowed in home-only mode besides the C09 fallbacks. `playwright` is an optional dependency; if it is missing, log once and skip.

5. **Targeted re-crawl.** Add `--recrawl-list PATH`, a CSV with `UNITID` and `school.school_url`; the stats script's `recrawl_candidates.csv` has both. Listed URLs are crawled even if they are already in the checkpoint, and their new rows replace the old ones. It can be combined with `--render-js`.

6. **Retry transient failures.** Retry each request once, after 3 s, on:
   - connect or read timeouts
   - connection resets
   - HTTP 429, 502, 503 or 504

   Classify the error only after the retry. Record `n_attempts`.

| Column | Type | Definition |
| --- | --- | --- |
| `extraction_errors` | list[str] | Extractor groups that raised on this page (empty when all ran). |
| `copyright_found` | bool | A copyright year was found on the page. |
| `n_script_tags` | int | Number of `<script>` elements. |
| `js_shell_suspected` | bool | Page looks built with JavaScript (rule above). |
| `rendered_with_js` | bool | Fields came from a rendered fetch (step 4). |
| `n_attempts` | int | Requests made for the final response, including the retry. |

**How the stats script uses this.** `build_stats_sheet.py` reads `js_shell_suspected` when present, and infers it from page size, words and links when absent. For suspected JavaScript pages it leaves the thin-page, no-contact and shared-template flags unchecked and lists them in `recrawl_candidates.csv`. It counts `extraction_errors` by group in Summary section 0.

**Acceptance criteria**
- With the tracker regex monkeypatched to raise, a fixture page still yields words, links, images and head fields; tracker fields are null; `extraction_errors == ["trackers"]`; `crawl_error` is null.
- A page with no images yields `total_images == 0` and a null `img_alt_coverage`. A page with no copyright year yields `copyright_found == False`.
- A Next.js-style fixture (`<div id="__next"></div>`, 15 `<script>` tags, 40 KB, 10 visible words) yields `js_shell_suspected == True`. The same page with 800 visible words yields `False`.
- `--recrawl-list` re-crawls a URL that is already in the checkpoint and replaces its row. URLs not on the list are untouched.
- A mocked 503-then-200 sequence yields a successful row with `n_attempts == 2`.

---

## P1: New capture in home-only mode (no extra page fetches)

### C07. Final URL and redirect chain

**Why.** A URL on record that redirects to a different domain is one of the cheapest strong signals: parked or sold domains, a different institution, or a lead-generation site.

| Column | Type | Definition |
| --- | --- | --- |
| `final_url` | str | `resp.url` after redirects. |
| `final_registered_domain` | str | `registered_domain(final_url)`. |
| `redirected_off_domain` | bool | `final_registered_domain != registered_domain`. |
| `redirect_hops` | int | `len(resp.history)`. |
| `redirect_status_codes` | list[int] | Status codes of each hop in `resp.history`. |
| `landing_content_type` | str | `Content-Type` header of the final response. |

Record these for **every** response, including non-200 and non-HTML responses (the current non-200 branch returns before setting them).

---

### C08. Error taxonomy and bot-protection detection

**Why.** `crawl_error` is free text. Stakeholders need "dead" separated from "blocking our bot." A 403 from a bot-protection service means the site is alive.

**Change.** Add `classify_error(exc=None, status_code=None, content_type=None) -> str` and write:

| Column | Type | Definition |
| --- | --- | --- |
| `error_category` | enum str | Final outcome after any fallbacks (C09). Values below. |
| `primary_error_category` | enum str | Outcome of the **first** request, before fallbacks. |
| `error_exception_type` | str or null | Exception class name, e.g. `ConnectionError`. |
| `bot_protection_detected` | bool | Response looks like a bot challenge (rules below). |
| `bot_protection_vendor` | str or null | `cloudflare`, `akamai`, `imperva`, `sucuri`, `ddos-guard`, `other`. |
| `cdn_vendor` | str or null | From response headers: `cloudflare` (`cf-ray`), `akamai` (`x-akamai-*`, `akamai` in `server`), `cloudfront` (`x-amz-cf-id`), `fastly` (`x-served-by` + `fastly`), `other`, or null. |

**`error_category` values** (the stats script uses these exact strings):

| Value | Rule (check in this order) |
| --- | --- |
| `ok` | Status 200 and HTML. |
| `dns_failure` | Exception text contains `NameResolutionError`, `Failed to resolve`, `Name or service not known`, `nodename nor servname`, `getaddrinfo failed`, `No address associated`, or `Temporary failure in name resolution`. |
| `connect_timeout` | `ConnectTimeout` / `ConnectTimeoutError`, or "timed out" with "connect". |
| `read_timeout` | `ReadTimeout` / "Read timed out". |
| `connection_refused` | `ConnectionRefusedError`, "Connection refused", `[Errno 111]`, `[Errno 61]`. |
| `connection_reset` | `RemoteDisconnected`, "Connection reset", "Connection aborted", `ProtocolError`, `BadStatusLine`. |
| `network_unreachable` | "Network is unreachable", "No route to host". |
| `ssl_error` | `SSLError`, `CERTIFICATE_VERIFY_FAILED`, `WRONG_VERSION_NUMBER`, "handshake" (after the existing unverified retry also failed). |
| `too_many_redirects` | `TooManyRedirects`, "Exceeded 30 redirects". |
| `invalid_url` | `InvalidURL`, `LocationParseError`, `MissingSchema`, `InvalidSchema`, "Failed to parse". |
| `transfer_error` | `ChunkedEncodingError`, `ContentDecodingError`. |
| `http_401`, `http_403`, `http_404`, `http_410`, `http_429`, `http_503` | That final status code. |
| `http_5xx` | Any other 500–599. |
| `http_4xx_other` | Any other 400–499. |
| `http_3xx_unresolved` | Final status 300–399 (redirect without a usable location). |
| `non_html` | Status 200 but `Content-Type` lacks `text/html` and `application/xhtml+xml`. Also accept `application/xhtml+xml` as HTML for parsing (today it is treated as a failure). |
| `extraction_failed` | Fetched HTML but `extract_page_features` raised. |
| `other` | Anything else. |

**Bot-protection rules** (apply to any response, including 200): headers `cf-mitigated`, or `server: cloudflare` with status 403/503 and body containing `Just a moment`, `Attention Required`, `cf-chl`, or `challenge-platform`; Akamai `Reference #` + "Access Denied"; Imperva `Incapsula incident ID`; Sucuri `Sucuri WebSite Firewall`; DDoS-Guard `ddos-guard`. Keep the patterns in a dict so they are easy to extend.

---

### C09. HTTPS→HTTP and www fallbacks

**Why.** `normalize_url` forces `https://`. Sites that serve only HTTP, or only on the `www.` host, currently count as broken.

**Change.** Only when the first request fails at the connection level (`dns_failure`, `connect_timeout`, `read_timeout`, `connection_refused`, `connection_reset`, `network_unreachable`, `ssl_error`), try in order, stopping at the first attempt that returns any HTTP response:
1. The `www.` variant (add `www.` if absent, remove it if present) over HTTPS.
2. The original host over `http://`.

| Column | Type | Definition |
| --- | --- | --- |
| `fetch_attempts` | list[dict] | `[{"url": ..., "outcome": <error_category>}]` for each attempt. |
| `fetched_url` | str | The URL whose request produced the response used. |
| `www_fallback_used` | bool | Attempt 1 produced the response. |
| `http_fallback_used` | bool | Attempt 2 produced the response. |
| `https_available` | bool | Any HTTPS attempt returned a response. |

`error_category` is the outcome of the response finally used (or of the last attempt if all failed). `primary_error_category` keeps the first attempt's outcome. A site that succeeds via fallback has `crawl_error = null`.

---

### C10. DNS lookups

| Column | Type | Definition |
| --- | --- | --- |
| `dns_resolves` | bool | `socket.getaddrinfo(host, 443)` succeeds for the input host. |
| `dns_resolves_www` | bool | Same for the `www.` variant (or the bare host if the input had `www.`). |
| `dns_resolved_ips` | list[str] | Distinct IPs (cap 10). |
| `nameservers` | list[str] | NS records of the registered domain (requires `dnspython`; null if not installed). |
| `has_mx_record` | bool or null | Registered domain has an MX record (requires `dnspython`). |
| `parking_nameserver_detected` | bool or null | Any NS matches `PARKING_NS_PATTERNS`. |

`PARKING_NS_PATTERNS` starter list (substring match, lowercase; **verify and extend** before use): `sedoparking`, `parkingcrew`, `bodis`, `above.com`, `parklogic`, `dan.com`, `afternic`, `hugedomains`, `domainparking`, `parking`. `dnspython` is optional; degrade to null like `python-whois` does today.

---

### C11. Parked, placeholder and default-server pages

Run these detectors on the body of **any** HTML response (including non-200).

| Column | Type | Definition |
| --- | --- | --- |
| `parked_domain_detected` | bool | Body matches `PARKED_PATTERNS`. |
| `parking_provider` | str or null | Provider name from the matching pattern, if identifiable. |
| `parking_evidence` | str or null | The pattern that matched. |
| `placeholder_page_detected` | bool | "coming soon", "under construction", "website is under maintenance", "site not published" (Wix/Squarespace unpublished pages), "future home of". |
| `default_server_page_detected` | bool | "Apache2 Ubuntu Default Page", "Welcome to nginx!", "IIS Windows Server", "It works!", "Index of /", "cPanel" default page, "Plesk" default page. |

`PARKED_PATTERNS` starter list (case-insensitive): "this domain is for sale", "buy this domain", "domain may be for sale", "the domain .* is for sale", "make an offer on this domain", "parked free", "domain parking", and script/link hosts `sedo.com`, `bodis.com`, `parkingcrew.net`, `dan.com`, `afternic.com`, `hugedomains.com`, `above.com`. Keep patterns in dicts keyed by provider.

---

### C12. WHOIS expansion, privacy type and RDAP fallback

**Why.** Today's `whois_privacy_protected` flags any record containing "privacy", "redacted" or "proxy". Since GDPR, registrars redact most .com/.org records by default ("REDACTED FOR PRIVACY"), so the flag mostly measures TLD, not concealment.

| Column | Type | Definition |
| --- | --- | --- |
| `whois_source` | enum str | `whois`, `rdap`, or `none`. |
| `whois_error` | str or null | Error text if both lookups failed. |
| `whois_registrar` | str or null | Registrar name. |
| `whois_creation_date` | ISO date | Domain creation date. |
| `whois_expiration_date` | ISO date | Domain expiration date. |
| `whois_updated_date` | ISO date | Last update date. |
| `whois_registrant_country` | str or null | Country code, if disclosed. |
| `whois_name_servers` | list[str] | Name servers from the record. |
| `whois_registrant_org` | str or null | Keep. Organization only, never personal names. |
| `whois_privacy_type` | enum str | `proxy_service` (registrant org/name matches `PRIVACY_PROXY_PATTERNS`), `gdpr_redacted` (text contains "redacted for privacy", "data protected", "not disclosed", "gdpr masked", "statutory masking"), `none`, or null if no record. |
| `whois_privacy_protected` | bool | Keep for compatibility: `whois_privacy_type in {"proxy_service", "gdpr_redacted"}`. |
| `domain_age_days` | int | Keep. From `whois_creation_date`. |
| `domain_days_to_expiry` | int | Days from crawl to `whois_expiration_date`. |
| `days_since_whois_update` | int | Days since `whois_updated_date`. |

`PRIVACY_PROXY_PATTERNS` starter list: "domains by proxy", "whoisguard", "contact privacy", "privacyprotect", "privacy protect", "withheld for privacy", "perfect privacy", "whois privacy", "private by design", "identity protection service", "privacy service provided by".

**.edu note.** EDUCAUSE's WHOIS uses labels such as "Domain record activated:", "Domain record last updated:" and "Domain expires:". If `python-whois` returns no creation date for a .edu domain, parse those labels from the raw text with a regex (dates look like `23-May-1985`). Verify against a live .edu lookup before relying on it.

**RDAP fallback.** If WHOIS fails or returns no creation date, GET `https://rdap.org/domain/<domain>` (it redirects to the authoritative RDAP server), read `events` (`registration`, `expiration`, `last changed`) and registrant `entities` (organization only). A 404 means no RDAP for that TLD: set `whois_source="none"`.

**Rate limits.** WHOIS servers throttle. Cache results by registered domain within a run and add `CONFIG["whois_delay_sec"]` (default 1.0).

---

### C13. SSL expansion

Covered by the column table in C02 (`ssl_subject_org`, `ssl_san_domains`, `ssl_san_other_registered_domains`, `ssl_cert_age_days`, `ssl_validity_days`, `ssl_cert_sha256`). For cross-site matching (C22), skip certificates whose SANs span more than `CONFIG["shared_cert_max_domains"]` (default 20) registered domains, because CDN and shared-hosting certificates bundle unrelated customers.

---

### C14. Page identity text

Stored strings only; no language processing.

| Column | Type | Definition |
| --- | --- | --- |
| `page_title` | str | `<title>` text, whitespace-collapsed, cap 300 characters. |
| `meta_description_text` | str | `<meta name=description>` content, cap 500. |
| `og_site_name` | str | `<meta property=og:site_name>` content, cap 200. |
| `html_lang` | str | `<html lang>` attribute. |

The stats script compares `page_title` / `og_site_name` with the institution name from College Scorecard.

---

### C15. Contact detail values

| Column | Type | Definition |
| --- | --- | --- |
| `phone_numbers` | list[str] | Distinct `RE_PHONE` matches normalized to 10 digits (strip a leading `1`), cap 25. Drop `0000000000` and numbers whose area code starts with 0 or 1. |
| `contact_emails` | list[str] | Distinct lowercased addresses from `mailto:` links plus an email regex over visible text, cap 25. |
| `mailto_domains` | list[str] | Distinct domains of `mailto:` addresses, cap 25. |
| `free_email_addresses` | list[str] | Subset of `contact_emails` on free-mail domains (`RE_FREE_EMAIL` list), cap 25. |
| `street_addresses` | list[str] | Distinct `RE_STREET` matches normalized (lowercase, collapsed whitespace, `street→st`, `avenue→ave`, `boulevard→blvd`, `road→rd`, `drive→dr`, `suite→ste`, trailing punctuation stripped), cap 10. |
| `zip_codes` | list[str] | Distinct 5-digit ZIP matches, cap 10. |

---

### C16. External domains and accreditation link claims

**Why.** Claiming accreditation from an unrecognized body is a classic diploma-mill signal. Links are structural evidence and need no language processing.

New file `reference/accreditors.csv`, columns `name,domain,recognized,notes`. Starter rows (the implementer must check every row against the U.S. Department of Education's list of recognized accrediting agencies before use; this list is not exhaustive):

```csv
name,domain,recognized,notes
Higher Learning Commission,hlcommission.org,True,institutional (regional)
Middle States Commission on Higher Education,msche.org,True,institutional (regional)
New England Commission of Higher Education,neche.org,True,institutional (regional)
Northwest Commission on Colleges and Universities,nwccu.org,True,institutional (regional)
Southern Association of Colleges and Schools Commission on Colleges,sacscoc.org,True,institutional (regional)
WASC Senior College and University Commission,wscuc.org,True,institutional (regional)
Accrediting Commission for Community and Junior Colleges,accjc.org,True,institutional
Accrediting Commission of Career Schools and Colleges,accsc.org,True,institutional
Distance Education Accrediting Commission,deac.org,True,institutional
Accrediting Bureau of Health Education Schools,abhes.org,True,institutional
Council on Occupational Education,council.org,True,institutional
National Accrediting Commission of Career Arts and Sciences,naccas.org,True,institutional
Accrediting Council for Independent Colleges and Schools,acics.org,False,ED recognition terminated 2022-08-19
```

| Column | Type | Definition |
| --- | --- | --- |
| `external_domains` | list[str] | Distinct registered domains of external links on the page, cap 200. |
| `trusted_link_domains` | list[str] | Subset in `TRUSTED_LINK_DOMAINS` or ending `.gov`. |
| `accreditor_domains_linked` | list[str] | Subset matching any `domain` in `accreditors.csv`. |
| `recognized_accreditor_domains_linked` | list[str] | Subset where `recognized == True`. |
| `accredit_keyword_domains` | list[str] | External domains whose registered domain contains `accredit` or `accreditation`. |
| `unrecognized_accredit_domains` | list[str] | Domains in `accredit_keyword_domains` or `accreditor_domains_linked` that are **not** recognized. |
| `accreditors_named` | list[str] | `name` values from `accreditors.csv` found as case-insensitive substrings of the visible text. |

Load `TRUSTED_LINK_DOMAINS` additions from `accreditors.csv` rows where `recognized == True`, so the trusted list and the reference file stay in sync.

---

### C17. Response headers and timing

| Column | Type | Definition |
| --- | --- | --- |
| `server_header` | str or null | `Server` header of the landing response. |
| `x_powered_by` | str or null | `X-Powered-By` header. |
| `has_hsts` | bool | `Strict-Transport-Security` present. |
| `has_csp` | bool | `Content-Security-Policy` present. |
| `last_modified_header` | str or null | Raw `Last-Modified` value. |
| `last_modified_age_days` | int or null | Days since `Last-Modified`. |
| `response_time_ms` | int | `resp.elapsed` in milliseconds. |
| `content_length_bytes` | int | Length of `resp.content`. |

---

### C18. Content fingerprints

| Column | Type | Definition |
| --- | --- | --- |
| `html_sha256` | str | SHA-256 of the raw response body bytes. Identical pages across domains = cloned site. |
| `visible_text_sha256` | str | SHA-256 of whitespace-collapsed visible text. |
| `text_simhash64` | str | 64-bit SimHash of the page's 5-word shingles (reuse `text_shingles`), as 16 hex characters. Null when the page has fewer than 50 words. |
| `stylesheet_hrefs` | list[str] | Absolute stylesheet URLs, cap 20. |
| `third_party_script_domains` | list[str] | Distinct registered domains of `<script src>` hosts that are not the site's own, cap 50. |

SimHash: for each shingle, take a 64-bit hash (first 8 bytes of `hashlib.md5`), add +1/−1 per bit to a 64-slot vector, and set bit *i* where the sum is positive.

---

### C19. Social profile links

| Column | Type | Definition |
| --- | --- | --- |
| `social_platforms_linked` | list[str] | Platforms linked from the page: `facebook`, `instagram`, `linkedin`, `x` (twitter.com or x.com), `youtube`, `tiktok`. |
| `social_profile_urls` | list[str] | Normalized profile URLs (lowercase host, no query string, no trailing slash), cap 10. Cross-site key in C22. |

Exclude share-intent links (`facebook.com/sharer`, `twitter.com/intent`, `linkedin.com/shareArticle`).

---

### C20. Raw HTML archive and offline re-extraction

**Why.** Re-running the crawl is expensive. Saving each homepage lets new features be extracted later without touching the sites again.

**Change.**
- `CONFIG["save_html_dir"] = "html_archive/"` (default on; set `None` to disable).
- Save the body of every HTML response (including non-200) as `<html_archive_dir>/<registered_domain with dots as underscores>/<crawl_timestamp as YYYYMMDDTHHMMSSZ>.html.gz`, plus a sidecar `.json` with `final_url`, `status_code`, response headers and `fetch_attempts`.
- Column `html_archive_path` (str).
- New CLI mode `--from-archive DIR`: rebuild `site_features.csv` from the archive with no network calls for page features. Domain-level lookups (SSL, WHOIS, DNS) are skipped unless `--refresh-domain-lookups` is passed.
- Document a retention period for the archive in the README.

---

### C21. Run metadata

| Column | Type | Definition |
| --- | --- | --- |
| `crawl_mode` | enum str | `home_only` or `multi_page`. |
| `script_version` | str | `SCRIPT_VERSION`. |
| `crawl_duration_sec` | float | Wall-clock time spent on this site, including domain lookups. |

---

### C22. New cross-site keys and an operator cluster

Generalize C04's inverted-index function to `shared_key_features(df, key_type, column)` and run it for every key type below. Each produces `n_domains_sharing_<key_type>` and `<key_type>_cluster_id`.

| `key_type` | Source column | Notes |
| --- | --- | --- |
| `tracker_id` | `ga_ua_ids`, `ga4_ids`, `gtm_ids`, `fb_pixel_ids` | From C04. |
| `template` | `structure_hashes` | From C04. Exclude pages with fewer than 50 words (error pages collide). |
| `css_file` | `css_content_hashes` | From C04. |
| `phone` | `phone_numbers` | |
| `email` | `contact_emails` | |
| `street_address` | `street_addresses` | |
| `ssl_cert` | `ssl_cert_sha256` | Skip certificates per the C13 rule. |
| `html_exact` | `html_sha256` | |
| `near_dup_text` | `text_simhash64` | Link domains whose SimHashes differ by ≤ 3 bits. Use 4 bands of 16 bits as the candidate index (any two hashes within 3 bits share at least one identical band). |
| `redirect_target` | `final_registered_domain` | Only rows with `redirected_off_domain == True`. |
| `registrant_org` | `whois_registrant_org` | Lowercase and strip punctuation; skip values matching privacy patterns or containing "redacted". |
| `social_profile` | `social_profile_urls` | |
| `third_party_script` | `third_party_script_domains` | Vendor threshold applies; common CDNs will be excluded automatically. |

Also write `operator_cluster_id`, `operator_cluster_size` and `operator_link_types` (list of key types that link the site to its cluster), using union-find across the **strong** keys: `tracker_id`, `phone`, `email`, `street_address`, `ssl_cert`, `html_exact`, `near_dup_text`, `social_profile`, `redirect_target`. Leave `registrant_org` out of the operator cluster: state system offices and web vendors legitimately register domains for many schools, so it is reported as its own cluster type only.

All key types obey the `cross_site_vendor_threshold` exclusion and are listed in `cross_site_excluded_keys.csv` when excluded.

---

## P2: Targeted deep dive (multi-page, flagged subset only)

### C23. Targeted deep-dive runs

- `python university_site_features.py deep_dive_candidates.csv --mode multi_page --checkpoint site_features_deep_checkpoint.csv --output site_features_deep.csv`
- `deep_dive_candidates.csv` is produced by `build_stats_sheet.py` with the columns `UNITID` and `school.school_url`, so it is already valid input.
- All C07–C22 fields are computed on the landing page and, where meaningful, aggregated across pages (lists = union, booleans = any, counts = sum).
- Multi-page mode already respects `robots.txt` and `Crawl-delay`. Keep that.

### C24. Required-disclosure and key-page detection

Scan the `href` and anchor text of every link on every crawled page, plus sitemap URLs, case-insensitively.

| Column | Patterns (anchor text or URL) |
| --- | --- |
| `has_net_price_calculator_link` | "net price calculator", "netpricecalculator", "net-price-calculator", "/npc" |
| `has_consumer_info_link` | "consumer information", "consumer disclosures", "student consumer information", "heoa" |
| `has_security_report_link` | "annual security report", "clery", "campus safety report", "security and fire safety" |
| `has_title_ix_link` | "title ix" |
| `has_accreditation_page` | "accreditation", "accredited by" |
| `has_privacy_policy_link` | "privacy policy", "/privacy" |
| `has_tuition_page` | "tuition", "cost of attendance", "tuition and fees" |
| `has_catalog_link` | "catalog", "catalogue", "academic bulletin" |
| `has_faculty_directory` | "faculty directory", "faculty & staff", "faculty and staff", "/directory" |
| `has_admissions_page` | "admission" |
| `has_financial_aid_page` | "financial aid", "student aid" |

For each, also write `<column>_url` (first matching absolute URL). Add `required_disclosures_found` = count of the first three (net price calculator, consumer information, annual security report). These are federal requirements for Title IV institutions; the net price calculator applies only to institutions enrolling first-time, full-time undergraduates. The stats script applies those caveats.

### C25. Accreditation claims (deep)

On the landing page and any page matched by `has_accreditation_page`, compute `accreditors_named`, `accreditor_domains_linked`, `recognized_accreditor_domains_linked` and `unrecognized_accredit_domains` (C16 definitions), unioned across those pages. Add `accreditation_page_url`. Comparing these with the institution's accreditor of record (the Department of Education's accreditation database, DAPIP, joined by OPEID) happens in `enrich_external.py` (C27).

### C26. Image fingerprints and stock-photo markers (optional dependencies)

- Optional `imagehash` + `Pillow`. If absent, skip with null columns.
- Download up to `CONFIG["max_images_per_site"]` (default 10) images from crawled pages, preferring the largest declared `width`×`height`, skipping SVGs, icons under 100 px, and data URIs. Respect `robots.txt` and the per-site delay.
- `image_phashes` (list of 16-hex 64-bit perceptual hashes), `stock_image_markers` (count of image URLs or filenames containing `shutterstock`, `istock`, `gettyimages`, `depositphotos`, `adobestock`, `stock.adobe`, `dreamstime`, `123rf`, `alamy`, `unsplash`, `pexels`, `pixabay`).
- Cross-site key `image` in C22 with a Hamming threshold of ≤ 4 (bands of 8 bits × 8 for the candidate index).

### C27. `enrich_external.py` (new script)

Input: `site_features.csv` (or the deep file). Output: `external_enrichment.csv` keyed by `registered_domain`. Polite: ≥ 1 second between requests per service, exponential backoff on 429/5xx, on-disk cache (`enrichment_cache/`), resumable.

| Column | Source |
| --- | --- |
| `wayback_first_capture`, `wayback_last_capture`, `wayback_n_captures`, `wayback_n_capture_months` | Internet Archive CDX API: `https://web.archive.org/cdx/search/cdx?url=<domain>&output=json&fl=timestamp,statuscode&collapse=timestamp:6` |
| `ct_first_cert_date`, `ct_n_certs`, `ct_other_registered_domains` | Certificate Transparency via `https://crt.sh/?q=<domain>&output=json` (slow and sometimes unavailable; treat failures as null) |
| `dapip_accreditors`, `site_vs_dapip_match` | Optional: a local DAPIP export (`--dapip PATH`), joined on 6-digit OPEID from a Scorecard file (`--institutions PATH`). `site_vs_dapip_match` = any of the site's `recognized_accreditor_domains_linked` / `accreditors_named` corresponds to an accreditor of record. |

---

## P3: Documentation, tests, dependencies

### C28. Documentation
- `README.md`: update the home-only "captured / not captured" lists, the checkpoint file name, the new CLI flags, the archive and `--from-archive`, and the deep-dive workflow (C23).
- `data_dictionary.csv`: one row per new or changed column, plus a `stage` value of `cross-site` for C04/C22 columns.
- `CHANGES.md`: a v2.0.0 entry listing C01–C27.

### C29. Tests (`tests/`, pytest, offline)
- HTML fixtures: a normal university page; a parked domain page; a Wix page; a page with a Gmail admissions contact and `© 2019`; a page linking an unrecognized "accreditation" domain; a Cloudflare challenge page; an nginx default page.
- `classify_error` against real exception strings (capture them once from `requests` and store them in the test file).
- C31: one extractor group raising leaves the others intact; explicit `0`/`[]`/`False` vs null; a JavaScript-shell fixture; `--recrawl-list` replacing checkpoint rows; retry on a mocked 503.
- C01 column presence; C03 retention on failed rows; C04 clustering with stringified lists; C05 checkpoint round trip and mode-aware resume; C02 DER parsing with a generated self-signed certificate; C09 fallback order with mocked responses; C18 SimHash distance for near-identical texts.

### C30. `requirements.txt`
```
cryptography>=42          # required: certificate parsing (C02)
dnspython>=2.4            # optional: NS / MX lookups (C10)
imagehash>=4.3            # optional, deep mode: image fingerprints (C26)
playwright>=1.40          # optional: --render-js for JavaScript-built pages (C31)
Pillow>=10                # optional, deep mode (C26)
pytest>=8                 # dev
```

---

## Appendix A. Consolidated v2 columns added or renamed (home-only)

Canonical names now written by home-only (C01): `total_words`, `total_a_tags`, `total_internal_links`, `total_external_links`, `trusted_outbound_links`, `total_images`, `total_html_bytes`, `avg_words_per_page`, `avg_images_per_page`, `structure_hashes`, `num_stylesheets_landing`, `avg_inline_styles_per_page`, `server_headers`, `has_last_modified_header`, `mailto_on_domain_ratio`, `max_copyright_year`, `min_copyright_year`.

New: `crawl_mode`, `script_version`, `crawl_duration_sec`, `final_url`, `final_registered_domain`, `redirected_off_domain`, `redirect_hops`, `redirect_status_codes`, `landing_content_type`, `error_category`, `primary_error_category`, `error_exception_type`, `bot_protection_detected`, `bot_protection_vendor`, `cdn_vendor`, `fetch_attempts`, `fetched_url`, `www_fallback_used`, `http_fallback_used`, `https_available`, `dns_resolves`, `dns_resolves_www`, `dns_resolved_ips`, `nameservers`, `has_mx_record`, `parking_nameserver_detected`, `parked_domain_detected`, `parking_provider`, `parking_evidence`, `placeholder_page_detected`, `default_server_page_detected`, `whois_source`, `whois_error`, `whois_registrar`, `whois_creation_date`, `whois_expiration_date`, `whois_updated_date`, `whois_registrant_country`, `whois_name_servers`, `whois_privacy_type`, `domain_days_to_expiry`, `days_since_whois_update`, `ssl_verified`, `ssl_issuer_cn`, `ssl_subject_org`, `ssl_subject_cn`, `ssl_not_before`, `ssl_not_after`, `ssl_cert_age_days`, `ssl_validity_days`, `ssl_san_domains`, `ssl_san_count`, `ssl_san_other_registered_domains`, `ssl_cert_sha256`, `page_title`, `meta_description_text`, `og_site_name`, `html_lang`, `phone_numbers`, `contact_emails`, `mailto_domains`, `free_email_addresses`, `street_addresses`, `zip_codes`, `external_domains`, `trusted_link_domains`, `accreditor_domains_linked`, `recognized_accreditor_domains_linked`, `accredit_keyword_domains`, `unrecognized_accredit_domains`, `accreditors_named`, `server_header`, `x_powered_by`, `has_hsts`, `has_csp`, `last_modified_header`, `last_modified_age_days`, `response_time_ms`, `content_length_bytes`, `html_sha256`, `visible_text_sha256`, `text_simhash64`, `stylesheet_hrefs`, `third_party_script_domains`, `social_platforms_linked`, `social_profile_urls`, `html_archive_path`, `extraction_errors`, `copyright_found`, `n_script_tags`, `js_shell_suspected`, `rendered_with_js`, `n_attempts`.

Cross-site (C04, C22): `n_domains_sharing_<key_type>` and `<key_type>_cluster_id` for each key type in C22, plus `operator_cluster_id`, `operator_cluster_size`, `operator_link_types`.

## Appendix B. Definition of done

1. `pytest` passes offline.
2. `python university_site_features.py sample.csv --home-only --limit 20` produces every Appendix A column (null where a lookup failed), with lists as JSON arrays.
3. Interrupting that run and re-running the same command resumes without re-crawling, and the cross-site columns are identical to an uninterrupted run.
4. `python site_feature_engineering.py site_features.csv` runs and includes the new engineered columns.
5. `python build_stats_sheet.py site_features.csv` runs and reports `schema: v2` in its `run_info` output.
6. `data_dictionary.csv`, `README.md` and `CHANGES.md` are updated.
7. `python university_site_features.py recrawl_candidates.csv --home-only --recrawl-list recrawl_candidates.csv` refreshes exactly those rows, and a second run of `build_stats_sheet.py` shows fewer rows in Summary section 0's capture-gap lines.