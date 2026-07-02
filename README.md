# University Website Structural Features for Fraud Detection

A two-script pipeline that crawls a list of university websites and turns their
HTML structure and metadata into a model-ready feature matrix — **no NLP**.
The premise: diploma mills and fraudulent institutions leave structural
fingerprints (thin sites, cloned templates, shared trackers, missing
legitimacy signals) that can be measured without reading a word of content.

```
university_urls.xlsx
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
df = build_feature_dataframe("university_urls.xlsx", url_column="url", limit=5)

# Step 2: engineer features — accepts the DataFrame directly or the saved CSV
X, report = build_model_matrix(df)
summarize(report)

X_model = X.drop(columns=["input_url", "registered_domain"])
```

**Command line:**

```bash
python university_site_features.py university_urls.xlsx --url-column url --limit 5
python site_feature_engineering.py site_features.csv --output model_matrix.csv
```

The Excel file needs one column of URLs (default column name `url`; scheme
optional — `www.example.edu` is fine).

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

### Configuration

Edit the `CONFIG` dict at the top of the script (or mutate it in the notebook
before calling): `max_pages_per_site`, `request_delay_sec`,
`request_timeout_sec`, `broken_link_sample_size`, `checkpoint_path`,
`output_csv`, `user_agent`.

**Runtime budget:** ~45–60 s per site at default settings. For hundreds of
URLs, run it in a terminal/`nohup` session rather than a notebook cell, or
lower `max_pages_per_site`.

## Script 2: `site_feature_engineering.py`

Transforms the raw output into a numeric matrix:

- **Derived features** — TLD dummy flags (`is_edu_tld`, etc.), domain age in
  years, copyright staleness, tracker counts, per-page rates (PDFs/page,
  trusted links/page), external-link share, trusted-share-of-external
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
| `site_features_checkpoint.json` | Auto-generated resume state (safe to delete to force a fresh crawl) |
| `site_features.csv` | Raw per-site features |
| `model_matrix.csv` | Final numeric matrix |
