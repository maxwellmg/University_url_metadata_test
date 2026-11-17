# Running `build_stats_sheet_2.py` in Jupyter

`build_stats_sheet_2.py` turns the crawler output into the fraud-screening stats sheet. It does four things:

- classifies every institution's website as reachable, blocking bots, or broken
- checks each site against up to 18 red flags
- scores and tiers each institution (High / Medium / Low / Insufficient data)
- finds websites that share trackers, templates or contact details across owners

It writes an Excel workbook, about 20 CSV tables and 6 PNG charts. **It does not create a PDF.** To build your own report in any layout, use `export_report_data()` ([section 9](#9-exporting-the-report-data-to-format-yourself)). It returns every number, chart series and table as plain data.

You only need to give it the path to `model_matrix.csv`. It works out the rest from that file.

## Contents

1. [What you need](#1-what-you-need)
2. [Quick start](#2-quick-start)
3. [Adding institution data and known-bad labels](#3-adding-institution-data-and-known-bad-labels)
4. [Other settings](#4-other-settings)
5. [What you get back](#5-what-you-get-back)
6. [How the model matrix is read](#6-how-the-model-matrix-is-read)
7. [Editing the script and re-running](#7-editing-the-script-and-re-running)
8. [Troubleshooting](#8-troubleshooting)
9. [Exporting the report data to format yourself](#9-exporting-the-report-data-to-format-yourself)

---

## 1. What you need

**Files.** Keep these together in `analysis_findings/`:

| File | Needed for |
|---|---|
| `build_stats_sheet_2.py` (version 1.2.0) | Everything |
| `export_report_data.py` | Section 9 only |

Your notebook can live anywhere. Section 2 shows how to point it at this folder.

**Python 3.9 or newer, with these packages:**

| Package | Required? | What it's for |
|---|---|---|
| `pandas` (1.5+), `numpy` | Required | Everything |
| `openpyxl` | Recommended | The `stats_sheet.xlsx` workbook. Without it you still get the CSVs. |
| `matplotlib` | Recommended | The PNG charts |
| `tldextract` | Recommended | Correct domains for multi-part endings such as `.ac.uk` and `.k12.ca.us` |
| `scikit-learn` | Optional | Only when you pass `iforest=True` |

Install them from a notebook cell:

```python
%pip install pandas numpy openpyxl matplotlib tldextract
```

> **Import `build_stats_sheet_2`, not `build_stats_sheet`.** The same folder holds `build_stats_sheet.py`, the older 1.0 version. `from build_stats_sheet import build_stats` runs that old version without any warning.

---

## 2. Quick start

One cell runs everything:

```python
import sys
sys.path.insert(0, "/path/to/University_url_metadata_test/analysis_findings")  # the folder holding build_stats_sheet_2.py

from build_stats_sheet_2 import build_stats

MODEL_MATRIX = "/path/to/model_matrix.csv"

res = build_stats(MODEL_MATRIX)
res["summary"]
```

When it runs, the script:

1. Recognizes the file as a model matrix (it has `source_*` columns and a `crawl_failed` column).
2. Rebuilds the crawler's own columns from the `source_*` columns and ignores the engineered, imputed ones ([section 6](#6-how-the-model-matrix-is-read) explains why).
3. Works out which crawl produced the data (home-only, multi-page, or v2) from the columns present, and turns on the stats and flags that data supports.
4. Writes everything to a `stats_output/` folder **next to `model_matrix.csv`**.
5. Returns `res`, a dictionary of every table as a pandas DataFrame ([section 5](#5-what-you-get-back)).

If you leave `sys.path.insert` out, the notebook must sit in `analysis_findings/` itself.

You can pass `site_features.csv` instead of the model matrix and get the same results.

---

## 3. Adding institution data and known-bad labels

The crawl file alone covers availability, domains, trackers and homepage content. Two optional files unlock the rest:

```python
res = build_stats(
    MODEL_MATRIX,
    institutions="/path/to/scorecard_extract.csv",  # College Scorecard or IPEDS extract, keyed by UNITID
    known_bad="/path/to/known_bad.csv",             # UNITID column, plus an optional 'label' column
)
```

**`institutions`** adds:
- institution names, and the "University/College name without .edu" flag
- ownership and predominant degree, so thin-homepage and anomaly checks compare each school with its peers
- OPEID6, so branch campuses that share a tracker or template are not counted as "unrelated owners"
- enrollment, for the homepage-size-vs-enrollment check and chart
- HCM2 and operating-status labels, used to validate the score

Column names are found automatically. Both API-style names (`school.name`, `school.ownership`, ...) and CSV-style names (`INSTNM`, `CONTROL`, `PREDDEG`, `UGDS`, `OPEID6`, `CURROPER`, `HCM2`, `STABBR`, `INSTURL`) work. If your file uses other names, map them:

```python
res = build_stats(MODEL_MATRIX, institutions="scorecard.csv",
                  column_map={"name": "INSTITUTION_NAME", "enrollment": "UGDS_2024"})
```

The fields you can map are `id`, `name`, `ownership`, `pred_degree`, `enrollment`, `ope6`, `operating`, `hcm2`, `state` and `url`.

**`known_bad`** is a CSV of UNITIDs already known to be problematic. Each distinct `label` value is tested separately in the Validation tab: how much more often each flag fires for those schools than for the rest, and the score's AUC.

---

## 4. Other settings

Every argument besides the input file is optional:

| Argument | Default | What it does |
|---|---|---|
| `out_dir` | `stats_output/` next to the input file | Where files are written. Use a new folder per run to keep runs apart. |
| `charts` | `True` | `False` skips the PNG charts (faster). |
| `excel` | `True` | `False` skips `stats_sheet.xlsx`. The CSVs are always written. |
| `iforest` | `False` | `True` also fits an Isolation Forest anomaly score (needs scikit-learn). |
| `id_col` | `"UNITID"` | ID column in the crawl file |
| `url_col` | `"school.school_url"` | URL column in the crawl file |
| `column_map` | `None` | Institutions-file column overrides (section 3) |

**Thresholds.** Pass any of these as keyword arguments:

| Setting | Default | Meaning |
|---|---|---|
| `high_threshold` | 3 | Red flags needed for the High tier |
| `medium_threshold` | 2 | Red flags needed for the Medium tier |
| `young_domain_years` | 3.0 | A domain younger than this raises "Young domain" |
| `stale_copyright_years` | 3 | Copyright year this many years old raises "Stale copyright" |
| `domain_expiry_days` | 60 | Domain expiring within this many days raises a flag |
| `thin_percentile` | 5.0 | Bottom X% of words-per-page within the peer group raises "Thin homepage" |
| `min_peer_n` | 30 | Smallest peer group used before falling back to a broader one |
| `vendor_threshold` | 25 | A tracker, template, etc. shared by more domains than this is treated as a vendor or platform and not counted |
| `near_dup_bits` | 3 | SimHash distance counted as near-duplicate text |
| `template_min_words` | 50 | Pages smaller than this are not template-matched |
| `anomaly_threshold` | 2.0 | Mean robust-z deficit counted as "anomalous vs. peers" |
| `min_completeness` | 0.7 | Share of applicable flags that must be checked before a school can be tiered "Low" |
| `min_capture_pct` | 0.75 | Fetched homepages with fewer core fields captured go to the re-crawl list |
| `js_shell_min_kb` | 20.0 | Page size above which very few words or links suggests a JavaScript-built page |
| `enrollment_residual_z` | -2.0 | Homepage-size-vs-enrollment residual below this marks an outlier |

```python
res = build_stats(MODEL_MATRIX, institutions="scorecard.csv", high_threshold=4, young_domain_years=5, charts=False)
```

A misspelled setting raises an error that lists the valid names. It is not silently ignored.

---

## 5. What you get back

### Files (in `stats_output/`)

| File | Contents |
|---|---|
| `stats_sheet.xlsx` | Every table below as a tab, plus a README tab |
| `summary.csv` | Headline statistics, ranked by relevance. Section 0 reports data capture: read it first. |
| `institution_flags.csv` | One row per UNITID: availability, each flag (1 raised, 0 checked, blank = could not be checked), score, tier, completeness |
| `deep_dive_candidates.csv` | Reachable sites worth a deeper crawl. Has UNITID + URL, so it can go straight to the crawler. |
| `verify_status_candidates.csv` | Broken URLs to check by other means, with a suggested check for each |
| `recrawl_candidates.csv` | Homepages fetched but not fully captured, or that look built with JavaScript |
| `availability.csv` | Counts by failure cause |
| `validation.csv` | Flag lift and score AUC against known-problem labels (needs section 3 inputs) |
| `clusters_<key>.csv`, `cross_site_edges.csv` | Sites linked by a shared tracker, template, phone, ... and the domain pairs behind them |
| `excluded_shared_keys.csv` | Keys shared too widely to count (vendors, platforms) |
| `shared_urls.csv` | Several UNITIDs on one website |
| `distributions.csv`, `crosstab_*.csv`, `top_values.csv` | Supporting tables |
| `coverage.csv` | How complete each input field is, with known caveats |
| `run_info.csv` | Input file and format, schema detected, settings used, **warnings** |
| `flag_definitions.csv` | Every red flag's rule, and whether it ran |
| `charts/*.png` | Availability, red-flag score distribution, flag prevalence, TLD mix, domain age, words vs. enrollment |

### The `res` dictionary

| Key | Contents |
|---|---|
| `summary`, `institutions`, `deep_dive_candidates`, `verify_status`, `recrawl_candidates`, `availability`, `validation`, `edges`, `excluded_keys`, `shared_urls`, `distributions`, `top_values`, `coverage`, `run_info`, `flag_definitions` | The tables above, as DataFrames |
| `clusters`, `crosstabs` | Dicts of DataFrames, one per key type / crosstab |
| `data` | The full working table: every input column plus every derived metric and flag, one row per institution |
| `charts` | Paths of the PNG charts |
| `warnings` | Run warnings (also in `run_info`) |
| `schema`, `input_format`, `options`, `out_dir`, `input` | What was detected, which settings were used, where files went |

### Useful cells

```python
# Read the warnings before quoting any number
for w in res["warnings"]:
    print("-", w)
```

```python
# Show the charts inline
from IPython.display import Image, display
for path in res["charts"]:
    display(Image(path))
```

```python
# High-tier institutions and why
res["institutions"].query("triage_tier == 'High'")[["UNITID", "school.school_url", "red_flag_score", "flag_reasons"]]
```

```python
# One Summary section, e.g. 1 = Availability, 2 = Red-flag triage
res["summary"].query("rank == 2")
```

---

## 6. How the model matrix is read

`model_matrix.csv` (from `site_feature_engineering.py`) holds two kinds of column:

- **`source_*` columns:** the crawler's values, unchanged apart from the prefix
- **engineered columns:** some of these reuse crawler names (`pages_crawled`, `ssl_ok`, `landing_status_code`, ...) but hold **median-imputed** values, with `*_missing` indicators alongside

The stats need the real values: a missing value must stay "unknown", never become a typical value. The script therefore uses only the ID columns and the `source_*` columns, and ignores everything else.

Before this was added, passing `model_matrix.csv` produced wrong results without any error. In a 640-school test it reported 0 broken URLs instead of 87, ran 5 of the 11 flags, and found 1 High-tier school instead of 19.

The model matrix also changes list columns (tracker IDs, builders, structure hashes) in two ways. The script undoes both:

| What the model matrix does | Why it matters | What the script does |
|---|---|---|
| Blank list cells on failed crawls become `[]` | `[]` means "checked, none found"; those sites were never read | Sets them back to blank on rows with a `crawl_error` |
| List columns the crawl never wrote are added as `[]` on every row (in home-only mode: `structure_hashes`, `css_content_hashes`, `server_headers`, `sitemap_expected_sections`) | An empty `structure_hashes` column hides the crawler's real `structure_hash`, so template sharing is never found | Drops those columns when they are empty on every row |

**Result:** on test data, every table matches the run on `site_features.csv` exactly. The `run_info` warnings say what was rebuilt.

**One thing it cannot recover:** on a homepage that was fetched, a list cell the crawler left blank also reads as `[]`. Those fields count as captured on fetched homepages. This only matters if the crawler missed a list field on a page it did read, which is rare. Use `site_features.csv` if you need that distinction.

An older model matrix with no `source_*` columns holds only engineered values. The script warns about it, and you should use `site_features.csv` instead.

You can also load the rebuilt table yourself:

```python
from build_stats_sheet_2 import load_model_matrix
sf = load_model_matrix(MODEL_MATRIX)   # the crawler's columns, as in site_features.csv
```

---

## 7. Editing the script and re-running

Python caches imports, so edits to `build_stats_sheet_2.py` don't take effect until you reload it:

```python
import importlib, build_stats_sheet_2
importlib.reload(build_stats_sheet_2)
from build_stats_sheet_2 import build_stats
```

Or put this at the top of the notebook, so edits are picked up automatically:

```python
%load_ext autoreload
%autoreload 2
```

Each run overwrites the files in its output folder. Pass a different `out_dir` to keep runs side by side.

The script also runs from a terminal:

```bash
python build_stats_sheet_2.py /path/to/model_matrix.csv --institutions scorecard.csv --known-bad known_bad.csv
python build_stats_sheet_2.py --help    # every option
```

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'build_stats_sheet_2'` | The path in `sys.path.insert` must be the folder that contains the file, not the file itself. |
| `KeyError: ID column 'UNITID' not found` | Pass `id_col="..."` with your ID column's name. |
| Warning: "Fewer than half the UNITIDs matched the institutions file" | Both files need the same IDs in the same format. Check for leading zeros or a different ID column, and use `column_map={"id": "..."}`. |
| No `stats_sheet.xlsx` | Install `openpyxl`. The CSVs are written either way. |
| No charts | Install `matplotlib`. |
| `TypeError: Unknown option(s) [...]` | A setting name is misspelled. The error lists the valid names. |
| Your own plots stop appearing, or change style, after a run | You're running an older copy. Version 1.2.0 leaves the notebook's plot settings alone. Check `res["run_info"]` for the version. |
| Many sites in "Insufficient data" | Expected when the crawl captured few fields. Section 0 of the Summary and `coverage.csv` show which fields are missing. |

---

## 9. Exporting the report data to format yourself

`export_report_data()` collects everything behind the stats sheet and its charts into plain Python data. It also writes the same data to JSON and CSV. Use it to lay out your own report (PDF, Word, slides, Power BI) without re-deriving any number.

### Set it up

1. Save `export_report_data.py` in the same folder as `build_stats_sheet_2.py`.
2. After `build_stats`, run:

```python
from export_report_data import export_report_data

report = export_report_data(res)
```

That's all. `report` holds the data, and the files are written to `stats_output/report_data/`.

### What's in `report`

| Key | Type | Contents |
|---|---|---|
| `meta` | dict | Input file and format, schema, crawl dates, institutions / known-bad files, run time, thresholds used, flags evaluated |
| `headline` | dict | The key numbers: institutions, homepages fetched, broken URLs, availability groups, tiers (overall and among reachable sites), mean score, most common flag, candidate counts, clusters, domain age, validation AUCs. Each count comes as `{"count", "of", "percent"}`. |
| `summary` | DataFrame | The Summary tab |
| `sections` | list | The Summary rows grouped by section, in report order: `[{"rank", "section", "rows": DataFrame}, ...]` |
| `flags` | DataFrame | One row per red flag: label, definition, what it needs, who it applies to, whether it ran, `raised`, `evaluated`, `percent_raised` |
| `charts` | dict | One entry per chart: `title`, `subtitle`, `kind`, `x_label`, `y_label`, `legend` (labels and colors), `png` (path of the drawn chart) and `data` (DataFrame of the plotted values, in display order, with a `color` column) |
| `charts_skipped` | dict | Charts that could not be drawn, and why (e.g. no enrollment data) |
| `tables` | dict | Every table: `institutions`, `deep_dive_candidates`, `verify_status`, `recrawl_candidates`, `availability`, `validation`, `clusters_<key>`, `edges`, `excluded_keys`, `shared_urls`, `distributions`, `crosstab_<name>`, `top_values`, `coverage`, `flag_definitions`, `run_info`, `summary` |
| `table_notes` | dict | One line per table saying what it holds |
| `warnings` | list | Run warnings. Carry them into your report where they apply. |
| `files` | dict | Paths of everything written |

The chart titles are the same sentences drawn on the PNGs, for example "87 of 640 institution URLs (13.6%) are broken".

### Files written (`stats_output/report_data/`)

| File | Contents |
|---|---|
| `report_data.json` | `meta`, `headline`, `sections`, `flags`, `charts` (with data), `charts_skipped`, `table_notes`, `warnings`, and an index of the table files |
| `headline.csv` | The headline numbers as `key,value` rows (e.g. `broken_urls.percent,13.6`), handy for mail merge or Power BI cards |
| `flags.csv` | The flag table |
| `charts/<name>.csv` | One file per chart: `availability`, `red_flag_scores`, `flag_prevalence`, `tld_mix`, `domain_age`, `words_vs_enrollment` |
| `tables/<name>.csv` | One file per table |

### Examples

Put a number in a sentence:

```python
h = report["headline"]
b = h["broken_urls"]
print(f"{b['count']:,} of {h['institutions']:,} institution URLs ({b['percent']}%) do not work.")
print(f"{h['tiers']['High']['count']} institutions raise {report['meta']['options']['high_threshold']}+ red flags.")
```

Redraw a chart in your own style:

```python
import matplotlib.pyplot as plt

ch = report["charts"]["flag_prevalence"]
d = ch["data"].iloc[::-1]                       # data is top-to-bottom; barh draws bottom-up
fig, ax = plt.subplots(figsize=(8, 0.4 * len(d) + 1))
ax.barh(d["label"], d["percent"])
ax.set_title(ch["title"])
ax.set_xlabel(ch["x_label"])
plt.show()
```

Lay out one Summary section as a table:

```python
for sec in report["sections"]:
    if sec["rank"] == 1:                        # 1 = Availability
        print(sec["section"])
        display(sec["rows"])
```

Power BI or Excel: point it at `report_data/headline.csv`, `report_data/charts/*.csv` and `report_data/tables/*.csv`.

### Options

```python
export_report_data(res)                                  # data + files in stats_output/report_data/
export_report_data(res, out_dir="/path/to/report_v1")    # write the files somewhere else
export_report_data(res, write=False)                     # data only, no files
export_report_data(res, tables_in_json=True)             # also put every table inside report_data.json
```

By default, `report_data.json` lists the large tables (one row per institution) by file name rather than including them, which keeps it small. `tables_in_json=True` makes the JSON one self-contained file.

### Alternative: put the function inside `build_stats_sheet_2.py`

To keep a single file instead of two:

1. Open `export_report_data.py` and copy everything from the line
   `# Copied from build_stats_sheet_2.py so the chart colors match the PNGs.`
   to the end of the file.
2. In `build_stats_sheet_2.py`, paste it directly above the line `def _parse_map(items):`, which comes right after `build_stats` ends. No new imports are needed: the script already imports everything it uses.
3. Import both functions from the script:

   ```python
   from build_stats_sheet_2 import build_stats, export_report_data
   report = export_report_data(build_stats(MODEL_MATRIX))
   ```

4. Optional: to also export on every command-line run, add this line in `main()` right after the `try: ... except` block that calls `build_stats`, just before `s = res["summary"]`:

   ```python
       export_report_data(res)
   ```

### Keeping it in step with the charts

The chart series are computed the same way as `_draw_charts()` in `build_stats_sheet_2.py`, and the colors are copied from its `PALETTE`. If you change a chart or a color there, make the same change in `_rd_chart_data()` and `_RD_PALETTE` / `_RD_GROUP_COLORS` in `export_report_data.py`.

The function also accepts a result from older versions of the script. It reads the thresholds from `run_info`. Older versions don't report their output folder, so pass `out_dir=` to say where the files should go.
