"""
site_feature_engineering.py
============================
First-pass feature engineering: turn the raw output of
`university_site_features.py` (site_features.csv or its DataFrame) into a
numeric, model-ready matrix.

Usage in Jupyter:

    from site_feature_engineering import build_model_matrix
    X, report = build_model_matrix("site_features.csv")

Or from the command line:

    python site_feature_engineering.py site_features.csv

What it does
------------
1. Parses list-valued columns that pandas stringified on the CSV round trip.
2. Derives interpretable features (per-page rates, staleness, tracker counts,
   TLD flags, log-scaled volumes).
3. Converts booleans/objects to numeric.
4. Handles missing data transparently: median imputation PLUS a `_missing`
   indicator column for every imputed feature (so the model can learn from
   missingness itself — e.g., failed WHOIS lookups are informative).
5. Flags failed crawls rather than dropping them.
6. Returns (X, report): the matrix and a dict describing every transformation,
   so nothing happens silently.

The output intentionally does NOT standardize/scale — do that inside your CV
pipeline (e.g., sklearn Pipeline) to avoid leakage across folds.
"""

from __future__ import annotations

import argparse
import ast
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Column groups (matching university_site_features.py output)
# ---------------------------------------------------------------------------
LIST_COLUMNS = [
    "structure_hashes", "css_content_hashes", "css_frameworks",
    "site_builders_detected", "meta_generators", "server_headers",
    "ga_ua_ids", "ga4_ids", "gtm_ids", "fb_pixel_ids",
]

ID_COLUMNS = ["input_url", "registered_domain"]

# Raw numeric features carried through as-is (imputed if missing)
PASSTHROUGH_NUMERIC = [
    "pages_crawled", "avg_words_per_page", "avg_images_per_page",
    "img_alt_coverage", "text_to_html_ratio", "dom_structure_diversity",
    "boilerplate_ratio", "avg_inline_styles_per_page", "num_stylesheets_landing",
    "pct_pages_meta_description", "pct_pages_og_tags", "pct_pages_canonical",
    "mailto_on_domain_ratio", "broken_link_rate_sampled",
    "num_subdomains_seen", "robots_blocked_count",
    "ssl_days_to_expiry", "landing_status_code",
    "max_structure_similarity_other_site",
    "n_sites_sharing_css_file", "n_sites_sharing_tracker_id",
]

# Heavy-tailed volume counts -> log1p
LOG_COLUMNS = [
    "total_a_tags", "total_internal_links", "total_external_links",
    "total_words", "total_images", "total_html_bytes",
    "pdf_link_count", "trusted_outbound_links", "mailto_count",
    "free_email_hits", "phone_number_hits",
]

BOOL_COLUMNS = [
    "https_redirect", "has_favicon", "has_edu_schema_markup",
    "has_last_modified_header", "has_street_address",
    "uses_css_framework", "uses_site_builder",
    "ssl_ok", "ssl_org_validated", "whois_privacy_protected",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_listlike(v):
    """CSV round trips lists as strings like "['a', 'b']" — parse them back."""
    if isinstance(v, (list, set)):
        return list(v)
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("[") and s.endswith("]"):
            for parser in (ast.literal_eval, json.loads):
                try:
                    out = parser(s)
                    return list(out) if isinstance(out, (list, set, tuple)) else []
                except (ValueError, SyntaxError, json.JSONDecodeError):
                    continue
        return []
    return []


def _to_numeric_bool(series: pd.Series) -> pd.Series:
    """Map True/False/'True'/'False' -> 1/0, everything else -> NaN."""
    mapping = {True: 1.0, False: 0.0, "True": 1.0, "False": 0.0,
               "true": 1.0, "false": 0.0, 1: 1.0, 0: 0.0}
    return series.map(lambda v: mapping.get(v, np.nan))


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def build_model_matrix(
    source: str | pd.DataFrame,
    output_csv: str | None = "model_matrix.csv",
    min_pages_for_ratios: int = 3,
    drop_constant_columns: bool = True,
) -> tuple[pd.DataFrame, dict]:
    """
    Parameters
    ----------
    source : path to site_features.csv OR the DataFrame from
             build_feature_dataframe().
    output_csv : where to save the matrix (None to skip saving).
    min_pages_for_ratios : within-site ratio features (boilerplate, diversity)
             are unreliable on tiny crawls; below this page count they are set
             to NaN and picked up by the missing-indicator machinery instead.
    drop_constant_columns : drop features with zero variance in this sample.

    Returns
    -------
    (X, report) : model-ready DataFrame (ID columns + numeric features) and a
                  dict documenting imputation medians, dropped columns, etc.
    """
    df = pd.read_csv(source) if isinstance(source, str) else source.copy()
    report = {"n_rows_in": len(df)}

    # -- 0. parse list columns back from CSV strings --------------------------
    for col in LIST_COLUMNS:
        if col in df.columns:
            df[col] = df[col].apply(_parse_listlike)
        else:
            df[col] = [[] for _ in range(len(df))]

    X = pd.DataFrame(index=df.index)
    for col in ID_COLUMNS:
        if col in df.columns:
            X[col] = df[col]

    # -- 1. crawl-quality flags (keep failed rows, flagged) --------------------
    X["crawl_failed"] = df.get("crawl_error", pd.Series([None] * len(df))).notna().astype(int)
    X["pages_hit_cap"] = 0
    if "pages_crawled" in df.columns:
        cap = df["pages_crawled"].max()
        X["pages_hit_cap"] = (df["pages_crawled"] >= cap).astype(int) if cap and cap > 1 else 0
    report["n_failed_crawls"] = int(X["crawl_failed"].sum())

    # -- 2. TLD features -------------------------------------------------------
    tld = df.get("tld", pd.Series([""] * len(df))).fillna("").astype(str).str.lower()
    X["is_edu_tld"] = (tld == "edu").astype(int)
    X["is_gov_or_state_tld"] = tld.str.endswith(("gov", "us")).astype(int)
    X["is_org_tld"] = (tld == "org").astype(int)
    X["is_com_net_tld"] = tld.isin(["com", "net"]).astype(int)
    X["is_foreign_or_other_tld"] = (
        (~tld.isin(["edu", "org", "com", "net", ""])) & (~tld.str.endswith(("gov", "us")))
    ).astype(int)

    # -- 3. domain age & staleness ---------------------------------------------
    now_year = datetime.now(timezone.utc).year
    if "domain_age_days" in df.columns:
        X["domain_age_years"] = pd.to_numeric(df["domain_age_days"], errors="coerce") / 365.25
    if "max_copyright_year" in df.columns:
        yr = pd.to_numeric(df["max_copyright_year"], errors="coerce")
        X["copyright_staleness_years"] = (now_year - yr).clip(lower=0)
    if {"max_copyright_year", "min_copyright_year"}.issubset(df.columns):
        X["copyright_year_span"] = (
            pd.to_numeric(df["max_copyright_year"], errors="coerce")
            - pd.to_numeric(df["min_copyright_year"], errors="coerce")
        )

    # -- 4. list-column counts ---------------------------------------------------
    X["n_css_frameworks"] = df["css_frameworks"].str.len()
    X["n_site_builders"] = df["site_builders_detected"].str.len()
    X["n_meta_generators"] = df["meta_generators"].str.len()
    X["n_css_files_hashed"] = df["css_content_hashes"].str.len()
    X["n_tracker_ids"] = (
        df["ga_ua_ids"].str.len() + df["ga4_ids"].str.len()
        + df["gtm_ids"].str.len() + df["fb_pixel_ids"].str.len()
    )
    X["has_any_analytics"] = (X["n_tracker_ids"] > 0).astype(int)
    X["has_fb_pixel"] = (df["fb_pixel_ids"].str.len() > 0).astype(int)

    # -- 5. per-page rates (size-normalized versions of raw counts) -------------
    pages = pd.to_numeric(df.get("pages_crawled"), errors="coerce").replace(0, np.nan)
    for raw, name in [
        ("pdf_link_count", "pdf_links_per_page"),
        ("trusted_outbound_links", "trusted_links_per_page"),
        ("total_external_links", "external_links_per_page"),
        ("total_internal_links", "internal_links_per_page"),
        ("phone_number_hits", "phone_hits_per_page"),
    ]:
        if raw in df.columns:
            X[name] = pd.to_numeric(df[raw], errors="coerce") / pages

    if {"total_external_links", "total_internal_links"}.issubset(df.columns):
        ext = pd.to_numeric(df["total_external_links"], errors="coerce")
        internal = pd.to_numeric(df["total_internal_links"], errors="coerce")
        X["external_link_share"] = ext / (ext + internal).replace(0, np.nan)
    if {"trusted_outbound_links", "total_external_links"}.issubset(df.columns):
        ext = pd.to_numeric(df["total_external_links"], errors="coerce")
        X["trusted_share_of_external"] = (
            pd.to_numeric(df["trusted_outbound_links"], errors="coerce") / ext.replace(0, np.nan)
        )

    # -- 6. log-scaled volumes ----------------------------------------------------
    for col in LOG_COLUMNS:
        if col in df.columns:
            X[f"log1p_{col}"] = np.log1p(pd.to_numeric(df[col], errors="coerce"))

    # -- 7. passthrough numerics ---------------------------------------------------
    for col in PASSTHROUGH_NUMERIC:
        if col in df.columns:
            X[col] = pd.to_numeric(df[col], errors="coerce")

    # unreliable within-site ratios on tiny crawls -> NaN (handled in step 9)
    if "pages_crawled" in df.columns:
        tiny = pd.to_numeric(df["pages_crawled"], errors="coerce") < min_pages_for_ratios
        for col in ("boilerplate_ratio", "dom_structure_diversity"):
            if col in X.columns:
                X.loc[tiny, col] = np.nan
        report["n_tiny_crawls"] = int(tiny.sum())

    # -- 8. booleans -----------------------------------------------------------------
    for col in BOOL_COLUMNS:
        if col in df.columns:
            X[col] = _to_numeric_bool(df[col])

    # -- 9. missingness: indicator + median imputation ---------------------------------
    feature_cols = [c for c in X.columns if c not in ID_COLUMNS]
    imputed = {}
    indicators = {}
    for col in feature_cols:
        if X[col].isna().any():
            indicators[f"{col}_missing"] = X[col].isna().astype(int)
            med = X[col].median()
            med = 0.0 if pd.isna(med) else float(med)  # all-NaN column
            X[col] = X[col].fillna(med)
            imputed[col] = med
    if indicators:
        X = pd.concat([X, pd.DataFrame(indicators, index=X.index)], axis=1)
    report["imputation_medians"] = imputed

    # -- 10. drop constants --------------------------------------------------------------
    dropped = []
    if drop_constant_columns:
        for col in [c for c in X.columns if c not in ID_COLUMNS]:
            if X[col].nunique(dropna=False) <= 1:
                dropped.append(col)
        X = X.drop(columns=dropped)
    report["dropped_constant_columns"] = dropped
    report["n_features_out"] = len([c for c in X.columns if c not in ID_COLUMNS])

    if output_csv:
        X.to_csv(output_csv, index=False)
        report["output_csv"] = output_csv

    return X, report


def summarize(report: dict) -> None:
    """Pretty-print the transformation report."""
    print(f"rows in:              {report['n_rows_in']}")
    print(f"failed crawls kept:   {report['n_failed_crawls']} (flagged via crawl_failed)")
    if "n_tiny_crawls" in report:
        print(f"tiny crawls:          {report['n_tiny_crawls']} (ratio features nulled)")
    print(f"features out:         {report['n_features_out']}")
    print(f"imputed columns:      {len(report['imputation_medians'])} (each has a *_missing indicator)")
    if report["dropped_constant_columns"]:
        print(f"dropped (constant):   {report['dropped_constant_columns']}")
    if "output_csv" in report:
        print(f"saved:                {report['output_csv']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Feature-engineer site_features.csv into a model matrix.")
    ap.add_argument("input_csv", help="site_features.csv from university_site_features.py")
    ap.add_argument("--output", default="model_matrix.csv")
    args = ap.parse_args()
    X, rep = build_model_matrix(args.input_csv, output_csv=args.output)
    summarize(rep)
