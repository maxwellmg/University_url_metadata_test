# University Website Structural Features for Fraud Detection

A two-script pipeline that crawls a list of university websites and turns their
HTML structure and metadata into a model-ready feature matrix — **no NLP**.
The premise: diploma mills and fraudulent institutions leave structural
fingerprints (thin sites, cloned templates, shared trackers, missing
legitimacy signals) that can be measured without reading a word of content.

```
university_urls.csv
        │
        ▼
university_site_features.py     (polite crawler + raw feature extraction)
        │
        ├── site_features_checkpoint.json   (auto-resume state)
        └── site_features.csv               (~55 raw columns, one row per site)
                │
                ▼
site_feature_engineering.py    (first-pass feature engineering)
                │
                └── model_matrix.csv        (numeric, NaN-free, model-ready)
```

## Installation

```bash
pip install -r requirements.txt
```

`tldextract` and `python-whois` are optional. Without them, domain-age and
registrant-privacy features are left null (and picked up by the missingness
indicators downstream), and multi-part TLDs like `.ac.uk` are parsed naively.

## Quick start

**Jupyter (recommended):**

```python
from university_site_features import build_feature_dataframe, CONFIG
from site_feature_engineering import build_model_matrix, summarize

# Step 1: crawl (start small to sanity-check)
df = build_feature_dataframe("university_urls.csv",
                             url_column="school.school_url",
                             id_column="UNITID",
                             limit=5)

# Step 2: engineer features — accepts the DataFrame directly or the saved CSV
X, report = build_model_matrix(df)
summarize(report)

X_model = X.drop(columns=["input_url", "registered_domain"])
```

**Command line:**

```bash
python university_site_features.py university_urls.csv --limit 5
python site_feature_engineering.py site_features.csv --output model_matrix.csv
```

The CSV file needs an institution key column and a URL column (defaults:
`UNITID` and `school.school_url`; scheme optional — `www.example.edu` is
fine). `UNITID` is read as a string to preserve leading zeros and is carried
through both output files as the join key for labels and Scorecard/SEVIS data.
Duplicate URLs are crawled once and their features joined back to every
matching input row.

## Script 1: `university_site_features.py`

Breadth-first crawls each site (capped, default 25 pages) and records raw
features in six groups:

| Group | Examples |
|---|---|
| Volume / size | pages crawled, total & avg words per page, `<a>` tag counts, image counts, alt-text coverage, text-to-HTML ratio |
| Structure / template | DOM tag-sequence hashes, structure diversity, boilerplate ratio (shingle overlap across pages), stylesheet counts, CSS framework & site-builder detection |
| Metadata | `meta generator`, description/OG/canonical coverage, favicon, schema.org `CollegeOrUniversity` markup, server headers |
| Tracking | Google Analytics (UA + GA4), GTM, and Facebook pixel IDs |
| Legitimacy signals | PDF link count, outbound links to `.gov`/accreditors, subdomain count, on-domain vs. free-email addresses, phone/street-address presence, copyright-year staleness, sampled broken-link rate |
| Domain-level | TLD, HTTPS redirect, SSL issuer & DV-vs-OV validation, domain age, WHOIS privacy protection |
| Sitemap / URL inventory | true site-wide page count (uncensored by the crawl cap), path depth & breadth, expected institutional sections (admissions, registrar, faculty, ...), site-wide PDF count, lastmod maintenance cadence, `crawl_censored` flag |

The sitemap module discovers sitemaps via robots.txt `Sitemap:` directives or
default paths, recurses through index files (capped), and derives features
from the URL list alone — no page fetches. Set
`CONFIG["save_url_inventory_dir"]` to also save each site's discrete URL list
as a gzipped text file, and `CONFIG["collect_sitemap"] = False` to disable the
module entirely.

After all sites finish, three **cross-site** features are computed over the
whole list — the fraud-network clustering signals:

- `max_structure_similarity_other_site` — Jaccard similarity of DOM template hashes vs. every other site
- `n_sites_sharing_css_file` — sites sharing an identical CSS file (by content hash)
- `n_sites_sharing_tracker_id` — sites sharing a GA/GTM/pixel ID

### Responsible crawling

- Respects `robots.txt` disallow rules and honors `Crawl-delay`
- Rate-limited (default 1.5 s between requests per site) with a hard page cap
- Descriptive User-Agent including a contact email — **edit `CONFIG["user_agent"]` before running**
- Checkpoints after every site with atomic writes (temp file + rename), so an
  interrupted run resumes by rerunning the same command; corrupt checkpoints
  are renamed aside, never silently overwritten

### Home-Page-Only Mode (`--home-only`)

By default, the crawler performs a full breadth-first crawl of interior pages
(up to the `max_pages_per_site` cap). To minimize bot detection and reduce
crawl time, use the `--home-only` flag to scrape **only the home page**, skip
robots.txt entirely, and make exactly one GET request per site.

**Trade-off: Fields Captured vs. Not Captured**

**CAPTURED (home page only):**
- Metadata on landing page: word count, `<a>` tags, images, alt-text coverage
- Links on landing page: internal, external, PDF, trusted-outbound link counts
- Page quality: HTML/text bytes, text-to-HTML ratio, favicon, meta descriptions/OG tags, canonical links
- Template signals: DOM structure hash, CSS frameworks, site-builder detection, meta generator
- Analytics: Google Analytics (UA + GA4), GTM, Facebook pixel IDs (from landing page only)
- Legitimacy: phone numbers, street addresses, e-mail domains, copyright year
- Domain-level: TLD, HTTPS redirect status, SSL certificate metadata, SSL bypass flag
- Whois: domain age, registrant privacy, registrant org

**NOT CAPTURED (multi-page aggregations):**
- Multi-page volume metrics: `total_a_tags`, `total_words`, `avg_words_per_page`, `total_images`, `avg_images_per_page`
- Multi-page structure: `unique_dom_structures`, `dom_structure_diversity`, `boilerplate_ratio`, `avg_inline_styles_per_page`
- Multi-page consistency: aggregated image alt-text coverage
- Response headers: `server_headers`, `has_last_modified_header`
- Analytics aggregation: GA/GTM/pixel IDs aggregated across pages
- Legitimacy aggregation: `pdf_link_count`, `trusted_outbound_links`, `mailto_on_domain_ratio`, `free_email_hits`, `phone_number_hits`, copyright-year range
- Sitemap & URL inventory: `sitemap_found`, `sitemap_url_count`, path depth/breadth, expected institutional sections, maintenance cadence, crawl censorship flag
- Link quality: `broken_link_rate_sampled` (internal links checked for 404s)
- Template reuse: `css_content_hashes` (CSS files collected and hashed for cross-site matching)
- Subdomains: `num_subdomains_seen` (detected across only 1 page, not multiple)

**Use home-only mode if:**
- You want to minimize bot detection risk (1 request per site vs. 10–25+ per multi-page crawl)
- You prioritize crawl speed over rich aggregated features
- Primary institutions are small or single-page sites
- You're doing a quick screening pass before detailed manual review

**Use multi-page mode (default) if:**
- You need fraud detection signals from site-wide structure (template cloning, boilerplate detection, cross-page consistency)
- You want inventory validation (sitemap cross-reference)
- You're modeling against a labeled dataset that benefits from rich aggregations
- Your target institutions typically have 5+ pages of content

**Command-line usage:**
```bash
# Home-page-only (fast, minimal bot detection)
python university_site_features.py urls.csv --home-only --limit 100

# Full crawl (slow, feature-rich, respects robots.txt)
python university_site_features.py urls.csv --limit 100
```

### Configuration

Edit the `CONFIG` dict at the top of the script (or mutate it in the notebook
before calling): `max_pages_per_site`, `request_delay_sec`,
`request_timeout_sec`, `broken_link_sample_size`, `checkpoint_path`,
`output_csv`, `user_agent`, `collect_sitemap`, `max_sitemap_files`,
`max_sitemap_urls`, `save_url_inventory_dir`.

**Runtime budget:** ~45–60 s per site at default settings. For hundreds of
URLs, run it in a terminal/`nohup` session rather than a notebook cell, or
lower `max_pages_per_site`.

## Script 2: `site_feature_engineering.py`

Transforms the raw output into a numeric matrix:

- **Derived features** — TLD dummy flags (`is_edu_tld`, etc.), domain age in
  years, copyright staleness, tracker counts, per-page rates (PDFs/page,
  trusted links/page), external-link share, trusted-share-of-external, and
  `crawl_coverage_ratio` (pages crawled / sitemap census — an uncensored
  size signal)
- **Transforms** — `log1p` on heavy-tailed volume counts; booleans → 0/1
- **Missingness as signal** — every imputed column gets a paired `*_missing`
  indicator before median imputation (failed WHOIS/SSL lookups are themselves
  informative for this problem)
- **Failed crawls kept** — flagged via `crawl_failed`, never dropped
- **Small-crawl guard** — within-site ratios (boilerplate, DOM diversity) are
  nulled below `min_pages_for_ratios` pages (default 3) instead of
  contributing noise
- **Audit trail** — returns a `report` dict logging every imputation median
  and dropped constant column

Output is all-numeric with zero NaNs. Scaling is deliberately **not** applied —
standardize inside your cross-validation pipeline to avoid leakage.

## Modeling caveats

1. **Labels vs. crawl success.** Before training, cross-tab your fraud labels
   against `crawl_failed` and the `*_missing` indicators. If known-fraudulent
   sites are disproportionately offline (e.g., shut down by regulators), the
   model will learn "site is down," not "site is fraudulent."
2. **Size confounding.** Volume features conflate *small* with *fraudulent*.
   Lean on the normalized rates, legitimacy signals, and cross-site
   template-reuse features; check that small legitimate institutions aren't
   systematically flagged.
3. **Cross-site features depend on list composition.** They are computed over
   whatever URL list you crawl, so they shift if the list changes — recompute
   them (rerun `add_cross_site_features`) whenever sites are added.
4. **Point-in-time snapshot.** Sites change; record the `crawl_timestamp`
   column alongside any labels you assign.

## Files

| File | Purpose |
|---|---|
| `university_site_features.py` | Polite crawler + raw feature extraction |
| `site_feature_engineering.py` | Raw features → model-ready matrix |
| `requirements.txt` | Dependencies |
| `data_dictionary.csv` | Description of every scraped and engineered variable |
| `site_features_checkpoint.json` | Auto-generated resume state (safe to delete to force a fresh crawl) |
| `site_features_checkpoint_unsuccessful.json` | Failed URLs from last run (prioritized on retry) |
| `site_features.csv` | Raw per-site features |
| `model_matrix.csv` | Final numeric matrix |

## Running the Code

### Command Line (Terminal)

**Basic crawl (multi-page mode, slow, feature-rich):**
```bash
python university_site_features.py universities.csv
```

**Home-page-only mode (fast, 1 request per site, minimal bot detection):**
```bash
python university_site_features.py universities.csv --home-only
```

**With options:**
```bash
# Crawl only first 10 URLs (for testing)
python university_site_features.py universities.csv --limit 10

# Skip SSL certificate verification (use with caution)
python university_site_features.py universities.csv --insecure

# Combine options
python university_site_features.py universities.csv --home-only --insecure --limit 50

# Custom column names
python university_site_features.py universities.csv \
  --url-column "website_url" \
  --id-column "institution_id"

# Override max pages per site (default 25)
python university_site_features.py universities.csv --max-pages 10
```

**Running feature engineering after crawl:**
```bash
python site_feature_engineering.py site_features.csv --output model_matrix.csv
```

### Jupyter Notebook

```python
from university_site_features import build_feature_dataframe, CONFIG
from site_feature_engineering import build_model_matrix, summarize

# Crawl with default settings
df = build_feature_dataframe(
    "universities.csv",
    url_column="school.school_url",
    id_column="UNITID",
    limit=10  # test with 10 URLs first
)

# Crawl with home-only mode
CONFIG["scrape_home_only"] = True
df = build_feature_dataframe("universities.csv", limit=10)

# Engineer features
X, report = build_model_matrix(df)
summarize(report)

# Final model matrix (drop metadata columns)
X_model = X.drop(columns=["input_url", "registered_domain", "tld", "crawl_timestamp"])
X_model.to_csv("model_ready.csv", index=False)
```

### Configuration Options

Edit `CONFIG` dict at the top of `university_site_features.py` or mutate it in a notebook:

| Setting | Default | Purpose |
|---------|---------|---------|
| `max_pages_per_site` | 25 | Max interior pages to crawl per site (multi-page mode only) |
| `request_delay_sec` | 1.5 | Minimum seconds between HTTP requests to a site |
| `request_timeout_sec` | 15 | Network timeout per request (seconds) |
| `max_css_files_per_site` | 5 | CSS files fetched for content hashing (template reuse detection) |
| `broken_link_sample_size` | 10 | Internal links spot-checked for 404s (multi-page mode) |
| `verify_ssl` | `True` | Verify SSL certificates; set `False` to bypass cert errors (insecure) |
| `mid_run_pause_sec` | 5 | Sleep between site crawls (only multi-page mode; home-only uses random 1-5s) |
| `scrape_home_only` | `False` | If `True`, fetch only home page (ignore robots.txt, skip interior links) |
| `collect_sitemap` | `True` | Discover and parse sitemaps for URL inventory features |
| `max_sitemap_files` | 10 | Max sitemap index files to recurse through per site |
| `max_sitemap_urls` | 50,000 | Max URLs to collect from sitemaps per site |
| `save_url_inventory_dir` | `None` | If set (e.g., `"url_inventories/"`), save each site's URL list as gzipped text |
| `checkpoint_path` | `"site_features_checkpoint.json"` | Where to auto-save progress (resumable on interrupt) |
| `output_csv` | `"site_features.csv"` | Output file for raw crawled features |
| `user_agent` | Descriptive bot string | Identifies crawler to servers; **edit before running** |

**Example: Custom configuration in notebook:**
```python
from university_site_features import CONFIG, build_feature_dataframe

# Fast crawl, minimal bot detection
CONFIG["scrape_home_only"] = True
CONFIG["request_delay_sec"] = 0.5  # speed up within-site requests
CONFIG["max_pages_per_site"] = 5   # if multi-page mode is used

# Save URL inventory (for later analysis)
CONFIG["save_url_inventory_dir"] = "url_inventories/"

df = build_feature_dataframe("universities.csv", limit=100)
```

## Workflow: Full Example

```bash
# Step 1: Crawl 100 universities, home-page-only (fast)
python university_site_features.py universities.csv --home-only --limit 100

# Step 2: If any fail, retry unsuccessful ones (auto-loads checkpoint_unsuccessful.json)
python university_site_features.py universities.csv --home-only --insecure --limit 100

# Step 3: Engineer features
python site_feature_engineering.py site_features.csv

# Step 4: Load model-ready matrix
python -c "import pandas as pd; X = pd.read_csv('model_matrix.csv'); print(X.shape, X.head())"
```

## Troubleshooting

**"Could not verify the SSL certificate"**
- Add `--insecure` flag to bypass SSL verification
- Or set `CONFIG["verify_ssl"] = False` in notebook

**Crawl is too slow**
- Use `--home-only` mode (1 request per site vs. 10–25+)
- Lower `request_delay_sec` in CONFIG (default 1.5s)
- Reduce `max_pages_per_site` (default 25)

**Getting kicked from sites / bot detection**
- Use `--home-only` mode (drastically fewer requests)
- Increase delays: `CONFIG["mid_run_pause_sec"] = 10` (multi-page mode)
- Spread crawls over time: interrupt and resume later

**Resuming after interruption**
- Re-run the same command; checkpoint file auto-saves after each site
- Unsuccessful sites are logged to `site_features_checkpoint_unsuccessful.json` and retried first on next run

**Memory issues with large URL lists**
- Process in batches: `--limit 500` per run
- Delete checkpoint after each batch if you don't need resume capability
- Use terminal/`nohup` instead of Jupyter

## Keep the Mac awake while a long crawl runs

This project does not need a Python package named `python-caffinate`.
On macOS, the built-in `caffeinate` command is the standard way to prevent
sleep while the crawl is running:

```bash
caffeinate -dims python university_site_features.py university_urls.csv --limit 100
```

`-dims` keeps the machine awake by preventing idle sleep, display sleep, and
system sleep while the scraper is active. You can also run:

```bash
caffeinate -dims python university_site_features.py university_urls.csv --home-only
```

If you want to leave a long run unattended, wrap the crawl command with
`caffeinate -dims` and leave the terminal open until it completes.

## Fresh start / wipe prior run state

If you want a truly clean restart, delete the checkpoint and output files that
store progress from prior runs before rerunning the scraper:

```bash
rm -f site_features_checkpoint.json site_features_checkpoint_unsuccessful.json site_features.csv
```

If you also saved URL inventories, clear those as well:

```bash
rm -rf url_inventories
```

This is the reset list to keep in mind:
- `site_features_checkpoint.json` — saved per-URL crawl state for resume
- `site_features_checkpoint_unsuccessful.json` — prior failed URLs that were retried
- `site_features.csv` — raw output from the previous crawl
- `url_inventories/` — optional saved page inventories for each domain

After deleting these files, rerun the scraper and it will start from a clean slate.
