"""
export_report_data.py
=====================
Pull every number, chart series and table behind the stats sheet out of a
build_stats() result, as plain data you can format however you like.

    from build_stats_sheet_2 import build_stats
    from export_report_data import export_report_data

    res = build_stats("model_matrix.csv")
    report = export_report_data(res)      # also writes <stats_output>/report_data/

What comes back (all plain dicts, lists and DataFrames):

    report["meta"]        run details: input file, schema, crawl dates, thresholds used
    report["headline"]    the key numbers: broken URLs, tiers, clusters, validation AUC, ...
    report["summary"]     the Summary tab as a DataFrame (count / denominator / percent / value / note)
    report["sections"]    the same Summary rows grouped by section, in report order
    report["flags"]       one row per red flag: label, definition, raised, evaluated, percent
    report["charts"]      the data behind every chart, with its title, subtitle, axis labels and colors
    report["tables"]      every table in the workbook (institutions, candidates, clusters, ...)
    report["table_notes"] one line per table saying what it holds
    report["warnings"]    run warnings; read them before quoting numbers
    report["files"]       where each file was written (empty when write=False)

Files written to <out_dir> (default: report_data/ inside the build_stats output folder):

    report_data.json   everything above except the large tables, in one file
    headline.csv       the headline numbers as key,value rows
    flags.csv          the flag table
    charts/<name>.csv  one file per chart series
    tables/<name>.csv  one file per table

Depends only on pandas and numpy, and only on the dict build_stats() returns,
so it works from its own file or pasted into build_stats_sheet_2.py.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# Copied from build_stats_sheet_2.py so the chart colors match the PNGs.
# If you change the colors there, change them here too.
_RD_PALETTE = {
    "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
    "good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b", "muted": "#898781", "axis": "#c3c2b7",
    "seq_light": "#86b6ef", "seq_dark": "#184f95",
}
_RD_GROUP_COLORS = {"reachable": "#0ca30c", "blocked": "#fab219", "broken_http": "#d03b3b",
                    "unreachable_server": "#d03b3b", "dns_dead": "#d03b3b", "tls_failure": "#d03b3b",
                    "bad_url": "#d03b3b", "unknown": "#898781"}
_RD_TLD_ORDER = [".edu", ".org", ".com / .net", ".gov / state .us", "other", "unknown"]
_RD_DEFAULT_OPTIONS = {"high_threshold": 3, "medium_threshold": 2, "young_domain_years": 3.0,
                       "enrollment_residual_z": -2.0, "template_min_words": 50, "vendor_threshold": 25}
_RD_TIERS = ["High", "Medium", "Low", "Insufficient data"]

_RD_TABLE_NOTES = {
    "summary": "Headline statistics in ranked order (1 = most relevant). Section 0 reports data capture; read it first. percent = count / denominator, and denominators count only rows where the value is known.",
    "flag_definitions": "Every red flag, its rule, and whether it was active in this run.",
    "availability": "Every institution URL by failure cause. counts_as_broken marks the categories in the broken total.",
    "institutions": "One row per UNITID: availability, flags (1 raised, 0 checked, blank = could not be checked), score, completeness, tier, capture status, anomaly scores, cluster IDs.",
    "deep_dive_candidates": "Reachable sites to crawl in depth (UNITID + URL), with the reason each was picked.",
    "verify_status": "Broken URLs to verify by other means (Scorecard status, Wayback Machine, WHOIS), with a suggested check.",
    "recrawl_candidates": "Homepages that were fetched but not fully captured, or that look built with JavaScript.",
    "validation": "Flag rates among known-problem schools vs. the rest (lift), and the score's AUC.",
    "clusters_": "Domains linked by a shared key (tracker ID, template, ...). cross_owner = different OPEID6 or owner unknown.",
    "edges": "Domain pairs and the key they share; load into a network-graph tool.",
    "excluded_keys": "Keys shared by too many domains to count (web vendors, platforms).",
    "shared_urls": "Registered domains used by several UNITIDs.",
    "distributions": "Numeric summaries (p5 to p95) overall and by ownership.",
    "crosstab_": "Category x ownership tables (ownership columns need an institutions file).",
    "top_values": "Most common builders, frameworks, certificate issuers, registrants.",
    "coverage": "How complete each input field is, with known caveats.",
    "run_info": "Schema detected, options and warnings.",
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _rd_pct(n, d):
    """Same rounding as the stats sheet: one decimal, None when there is no denominator."""
    return round(100.0 * n / d, 1) if d else None


def _rd_count(n, d):
    return {"count": int(n), "of": int(d), "percent": _rd_pct(n, d)}


def _rd_json(v):
    """Make any value JSON-safe: NaN/NA -> null, numpy -> Python, DataFrame -> list of row dicts."""
    if isinstance(v, pd.DataFrame):
        return [_rd_json(r) for r in v.to_dict(orient="records")]
    if isinstance(v, pd.Series):
        return [_rd_json(x) for x in v.tolist()]
    if isinstance(v, dict):
        return {str(k): _rd_json(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_rd_json(x) for x in v]
    if isinstance(v, set):
        return sorted(_rd_json(x) for x in v)
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, (pd.Timestamp, datetime)):
        return None if pd.isna(v) else v.isoformat()
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def _rd_flat_cells(df: pd.DataFrame) -> pd.DataFrame:
    """Lists in cells -> 'a; b; c' so the CSV stays one value per cell."""
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object:
            out[c] = out[c].map(lambda v: "; ".join(map(str, v)) if isinstance(v, (list, tuple, set)) else v)
    return out


def _rd_flatten(d, prefix=""):
    """Nested headline dict -> [(key.path, value)] rows."""
    rows = []
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            rows += _rd_flatten(v, key + ".")
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            for i, x in enumerate(v):
                nk = next((n for n in ("group", "key_type", "label") if n in x), None)
                name = x[nk] if nk else str(i)
                rows += _rd_flatten({kk: vv for kk, vv in x.items() if kk != nk}, f"{key}.{name}.")
        else:
            rows.append((key, _rd_json(v)))
    return rows


def _rd_options(res) -> dict:
    """Thresholds used in the run: from res['options'] (build_stats_sheet_2.py 1.2+) or the Run_info tab."""
    if isinstance(res.get("options"), dict):
        return {**_RD_DEFAULT_OPTIONS, **res["options"]}
    ri = res.get("run_info")
    if isinstance(ri, pd.DataFrame) and "item" in ri.columns:
        hit = ri.loc[ri["item"] == "Options", "value"]
        if len(hit):
            try:
                return {**_RD_DEFAULT_OPTIONS, **json.loads(hit.iloc[0])}
            except (TypeError, ValueError):
                pass
    return dict(_RD_DEFAULT_OPTIONS)


def _rd_run_info(res, item):
    ri = res.get("run_info")
    if isinstance(ri, pd.DataFrame) and "item" in ri.columns:
        hit = ri.loc[ri["item"] == item, "value"]
        if len(hit):
            return hit.iloc[0]
    return None


# --------------------------------------------------------------------------
# Chart series (mirror make_charts in build_stats_sheet_2.py)
# --------------------------------------------------------------------------
def _rd_chart_data(res, opts) -> tuple[dict, dict]:
    df = res["data"]
    N = len(df)
    fdefs = res["flag_definitions"]
    fcols = [c for c, a in zip(fdefs["flag_column"], fdefs["active_in_this_run"]) if a and c in df.columns]
    pngs = {Path(p).stem: str(p) for p in (res.get("charts") or [])}
    charts, skipped = {}, {}

    def add(name, title, data, subtitle="", x_label="", y_label="", kind="", legend=None):
        charts[name] = {"title": title, "subtitle": subtitle, "kind": kind, "x_label": x_label, "y_label": y_label,
                        "legend": legend or [], "png": pngs.get(name), "data": data.reset_index(drop=True)}

    # 1 availability: horizontal bars, one per failure category, in display order (top to bottom)
    av = res["availability"].copy()
    broken = int(df["is_broken"].sum())
    av_data = pd.DataFrame({
        "category": av["availability_category"], "category_label": av["category_label"].fillna(av["availability_category"]),
        "group": av["availability_group"], "group_label": av["group_label"],
        "count": av["count"].astype(int), "percent": (100 * av["count"] / N).round(1),
        "counts_as_broken": av["counts_as_broken"].astype(bool),
        "color": av["availability_group"].map(_RD_GROUP_COLORS).fillna(_RD_PALETTE["muted"]),
    })
    legend_all = [("Reachable", _RD_PALETTE["good"]), ("Alive, blocks automated access", _RD_PALETTE["warning"]),
                  ("Broken (counted in the headline)", _RD_PALETTE["critical"]), ("Unclassified error", _RD_PALETTE["muted"])]
    used = set(av_data["color"])
    add("availability", f"{broken:,} of {N:,} institution URLs ({100 * broken / N:.1f}%) are broken", av_data,
        "403/429/503 answers count as alive: those sites block automated visitors rather than being down.",
        x_label="Institutions", kind="horizontal bar",
        legend=[{"label": l, "color": c} for l, c in legend_all if c in used])

    # 2 red-flag score distribution: stacked bars (enough flags checked / insufficient data)
    reach = df["availability_group"].isin(["reachable", "blocked"])
    sub = df.loc[reach, ["red_flag_score", "triage_tier"]]
    if not sub.empty:
        insuff = sub["triage_tier"] == "Insufficient data"
        xs = list(range(0, int(sub["red_flag_score"].max()) + 1))
        checked = sub.loc[~insuff, "red_flag_score"].value_counts().reindex(xs, fill_value=0)
        unchecked = sub.loc[insuff, "red_flag_score"].value_counts().reindex(xs, fill_value=0)
        hi_t = opts["high_threshold"]
        data = pd.DataFrame({"red_flags_raised": xs, "sites_enough_checked": checked.values.astype(int),
                             "sites_insufficient_data": unchecked.values.astype(int)})
        data["sites_total"] = data["sites_enough_checked"] + data["sites_insufficient_data"]
        data["high_tier"] = data["red_flags_raised"] >= hi_t
        data["color"] = np.where(data["high_tier"], _RD_PALETTE["seq_dark"], _RD_PALETTE["seq_light"])
        data["color_insufficient_data"] = _RD_PALETTE["axis"]
        hi = int(data.loc[data["high_tier"], "sites_enough_checked"].sum())
        add("red_flag_scores", f"{hi:,} reachable sites raise {hi_t}+ red flags (High tier)", data,
            "Dark bars = High tier. Gray = sites tiered 'Insufficient data'. Reachable and bot-blocking sites only.",
            x_label="Red flags raised", y_label="Sites", kind="stacked bar",
            legend=[{"label": "Enough flags checked", "color": _RD_PALETTE["seq_light"]},
                    {"label": "Too few flags checked to rule out High", "color": _RD_PALETTE["axis"]}])
    else:
        skipped["red_flag_scores"] = "No reachable or bot-blocking sites."

    # 3 flag prevalence: one bar per active flag, most common first
    rows = []
    for c in fcols:
        ev = df[c].notna()
        if ev.sum():
            raw = 100 * float(df.loc[ev, c].mean())
            rows.append({"flag_column": c, "label": fdefs.set_index("flag_column").at[c, "label"],
                         "percent": round(raw, 1), "raised": int((df[c] == 1).sum()), "evaluated": int(ev.sum()), "_raw": raw})
    if rows:
        rows.sort(key=lambda r: r["_raw"])   # same order as the chart (ties keep flag order)
        data = pd.DataFrame(rows[::-1]).drop(columns="_raw")
        data["color"] = _RD_PALETTE["series"][0]
        top = rows[-1]
        add("flag_prevalence", f"Most common flag: {top['label']} ({top['percent']:.1f}%)", data,
            x_label="Percent of institutions where the flag could be evaluated", kind="horizontal bar")
    else:
        skipped["flag_prevalence"] = "No flag could be evaluated."

    # 4 TLD mix: by ownership when an institutions file was joined, else overall
    colmap = dict(zip(_RD_TLD_ORDER, _RD_PALETTE["series"][:5] + [_RD_PALETTE["muted"]]))
    if "inst_ownership_label" in df.columns:
        own = df["inst_ownership_label"].fillna("Unknown")
        counts = pd.crosstab(own, df["tld_category"])
        counts = counts[[c for c in _RD_TLD_ORDER if c in counts.columns]]
        share = counts.div(counts.sum(axis=1), axis=0) * 100
        data = (counts.stack().rename("count").to_frame().join(share.stack().rename("percent").round(1))
                .reset_index().rename(columns={"inst_ownership_label": "ownership", "row_0": "ownership"}))
        data.columns = ["ownership", "tld_category", "count", "percent"]
        data["color"] = data["tld_category"].map(colmap)
        edu = 100 * (df["tld_category"] == ".edu").mean()
        add("tld_mix", f"{edu:.0f}% of institutions are on .edu; the rest vary by ownership", data,
            x_label="Percent of institutions", kind="100% stacked horizontal bar, one bar per ownership type",
            legend=[{"label": c, "color": colmap[c]} for c in counts.columns])
    else:
        vc = df["tld_category"].value_counts().reindex([c for c in _RD_TLD_ORDER if c in set(df["tld_category"])])
        data = pd.DataFrame({"tld_category": vc.index, "count": vc.values.astype(int),
                             "percent": (100 * vc.values / N).round(1)})
        data["color"] = _RD_PALETTE["series"][0]
        add("tld_mix", f"{100 * vc.get('.edu', 0) / N:.0f}% of institution sites are on .edu", data, kind="horizontal bar")

    # 5 domain age histogram
    a = df["domain_age_years"].dropna() if "domain_age_years" in df.columns else pd.Series(dtype=float)
    if len(a) >= 10:
        labels = ["< 1", "1-3", "3-5", "5-10", "10-20", "20+"]
        cut = pd.cut(a, bins=[0, 1, 3, 5, 10, 20, 200], labels=labels, right=False).value_counts().reindex(labels)
        data = pd.DataFrame({"age_years": labels, "institutions": cut.values.astype(int),
                             "bin_start_years": [0, 1, 3, 5, 10, 20], "bin_end_years": [1, 3, 5, 10, 20, None]})
        data["color"] = [_RD_PALETTE["series"][1] if l in ("< 1", "1-3") else _RD_PALETTE["series"][0] for l in labels]
        young = int((a < opts["young_domain_years"]).sum())
        add("domain_age", f"{young:,} of {len(a):,} domains are under {opts['young_domain_years']:g} years old", data,
            f"Orange = younger than 3 years (young-domain flag uses {opts['young_domain_years']:g}). Institutions with a WHOIS creation date only.",
            x_label="Domain age (years)", y_label="Institutions", kind="bar")
    else:
        skipped["domain_age"] = "Fewer than 10 institutions with a WHOIS creation date."

    # 6 homepage words vs enrollment (scatter, log-log)
    if "words_vs_enrollment_resid_z" in df.columns:
        m = df["words_vs_enrollment_resid_z"].notna()
        idc = res["institutions"].columns[0]   # the ID column leads the institutions table
        cols = [c for c in [idc, "inst_name", "registered_domain", "inst_enrollment", "words_per_page",
                            "words_vs_enrollment_resid_z", "low_size_for_enrollment"] if c in df.columns]
        data = df.loc[m, cols].copy()
        data["outlier"] = data["low_size_for_enrollment"] == 1
        data["color"] = np.where(data["outlier"], _RD_PALETTE["series"][1], _RD_PALETTE["series"][0])
        add("words_vs_enrollment", f"{int(data['outlier'].sum()):,} homepages are far thinner than enrollment predicts", data,
            f"Outlier = robust residual z < {opts['enrollment_residual_z']} from a log-log fit.",
            x_label="Undergraduate enrollment (log scale)", y_label="Words on homepage (log scale)", kind="scatter, log-log",
            legend=[{"label": "Within expected range", "color": _RD_PALETTE["series"][0]},
                    {"label": "Far smaller than enrollment predicts", "color": _RD_PALETTE["series"][1]}])
    else:
        skipped["words_vs_enrollment"] = "Needs an institutions file with enrollment (UGDS / latest.student.size)."
    return charts, skipped


# --------------------------------------------------------------------------
# Headline numbers
# --------------------------------------------------------------------------
def _rd_headline(res, opts, flags: pd.DataFrame, charts: dict) -> dict:
    df = res["data"]
    N = len(df)
    reach = df["availability_group"] == "reachable"
    fetched = df["availability_category"] == "ok"
    h = {
        "institutions": N,
        "unique_urls": int(df["input_url"].nunique()),
        "unique_registered_domains": int(df["registered_domain"].nunique()),
        "homepages_fetched": _rd_count(fetched.sum(), N),
        "broken_urls": _rd_count(df["is_broken"].sum(), N),
    }
    av = res["availability"]
    h["availability_groups"] = [
        {"group": g, "label": sub["group_label"].iloc[0], **_rd_count(sub["count"].sum(), N)}
        for g, sub in av.groupby("availability_group", sort=False)]
    h["tiers"] = {t: _rd_count((df["triage_tier"] == t).sum(), N) for t in _RD_TIERS}
    h["tiers_among_reachable"] = {t: _rd_count(((df["triage_tier"] == t) & reach).sum(), reach.sum())
                                  for t in ["High", "Medium", "Insufficient data"]}
    h["mean_red_flag_score_reachable"] = round(float(df.loc[reach, "red_flag_score"].mean()), 2) if reach.any() else None
    h["could_reach_high_if_unchecked_flags_raised"] = _rd_count((df["could_reach_high"] == 1).sum(), N)
    h["mean_data_completeness_pct"] = round(float(df["data_completeness_pct"].mean()), 1) if df["data_completeness_pct"].notna().any() else None
    h["flags_evaluated"] = int(flags["active_in_this_run"].sum())
    if "flag_prevalence" in charts:
        top = charts["flag_prevalence"]["data"].iloc[0]
        h["most_common_flag"] = {"label": top["label"], **_rd_count(top["raised"], top["evaluated"])}
    h["deep_dive_candidates"] = int(len(res["deep_dive_candidates"]))
    h["verify_status_candidates"] = int(len(res["verify_status"]))
    h["recrawl_candidates"] = int(len(res["recrawl_candidates"]))
    h["clusters"] = [{"key_type": k, "clusters": int(len(c)), "cross_owner_clusters": int(c["cross_owner"].sum()),
                      "domains_in_clusters": int(c["n_domains"].sum()), "largest_cluster_domains": int(c["n_domains"].max())}
                     for k, c in (res.get("clusters") or {}).items() if c is not None and not c.empty]
    su = res.get("shared_urls")
    h["domains_used_by_several_unitids"] = int(len(su)) if isinstance(su, pd.DataFrame) else 0
    if "domain_age_years" in df.columns and df["domain_age_years"].notna().any():
        a = df["domain_age_years"]
        h["domain_age"] = {"with_creation_date": _rd_count(a.notna().sum(), N), "median_years": round(float(a.median()), 1),
                           f"younger_than_{opts['young_domain_years']:g}_years": _rd_count((a < opts["young_domain_years"]).sum(), a.notna().sum())}
    val = res.get("validation")
    if isinstance(val, pd.DataFrame) and not val.empty:
        h["validation"] = []
        for label, sub in val.groupby("label", sort=False):
            auc = sub.loc[sub["measure"] == "Red-flag score AUC", "value"]
            hi = sub[sub["measure"] == "Share in High tier"]
            h["validation"].append({
                "label": label, "n_labelled": int(sub["n_labelled"].iloc[0]),
                "auc": _rd_json(auc.iloc[0]) if len(auc) else None,
                "high_tier_pct_labelled": _rd_json(hi["rate_labelled_pct"].iloc[0]) if len(hi) else None,
                "high_tier_pct_rest": _rd_json(hi["rate_rest_pct"].iloc[0]) if len(hi) else None})
    return h


# --------------------------------------------------------------------------
# Main function
# --------------------------------------------------------------------------
def export_report_data(res: dict, out_dir=None, write: bool = True, tables_in_json: bool = False) -> dict:
    """Collect every number, chart series and table from a build_stats() result.

    res            : the dict returned by build_stats()
    out_dir        : folder for the files; default <build_stats output folder>/report_data
    write          : False = return the data only, write nothing
    tables_in_json : True = also put every table (e.g. one row per institution) inside report_data.json
    """
    required = ["data", "summary", "availability", "flag_definitions", "deep_dive_candidates", "verify_status", "recrawl_candidates"]
    missing = [k for k in required if k not in res]
    if missing:
        raise KeyError(f"Not a build_stats() result: missing {missing}.")
    df = res["data"]
    opts = _rd_options(res)

    # flags: definition + how often each was raised
    fl = res["flag_definitions"].copy()
    raised, evaluated = [], []
    for c, active in zip(fl["flag_column"], fl["active_in_this_run"]):
        if active and c in df.columns:
            raised.append(int((df[c] == 1).sum()))
            evaluated.append(int(df[c].notna().sum()))
        else:
            raised.append(None)
            evaluated.append(None)
    fl["raised"] = pd.array(raised, dtype="Int64")
    fl["evaluated"] = pd.array(evaluated, dtype="Int64")
    fl["percent_raised"] = [_rd_pct(r, e) if r is not None else None for r, e in zip(raised, evaluated)]

    summary = res["summary"].copy()
    sections = [{"rank": int(rank), "section": sec, "rows": summary.loc[sub.index, ["metric", "count", "denominator", "percent", "value", "note"]]}
                for (rank, sec), sub in summary.groupby(["rank", "section"], sort=False)]

    charts, skipped = _rd_chart_data(res, opts)

    tables = {}
    for name in ["summary", "institutions", "deep_dive_candidates", "verify_status", "recrawl_candidates", "availability",
                 "validation", "edges", "excluded_keys", "shared_urls", "distributions", "top_values", "coverage",
                 "flag_definitions", "run_info"]:
        t = res.get(name)
        if isinstance(t, pd.DataFrame) and not t.empty:
            tables[name] = t
    for k, t in (res.get("clusters") or {}).items():
        if isinstance(t, pd.DataFrame) and not t.empty:
            tables[f"clusters_{k}"] = t
    for k, t in (res.get("crosstabs") or {}).items():
        if isinstance(t, pd.DataFrame) and not t.empty:
            tables[f"crosstab_{k}"] = t
    notes = {n: _RD_TABLE_NOTES.get(n) or next((v for p, v in _RD_TABLE_NOTES.items() if p.endswith("_") and n.startswith(p)), "")
             for n in tables}

    meta = {
        "exported_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "script": _rd_run_info(res, "Script"),
        "run_at_utc": _rd_run_info(res, "Run at (UTC)"),
        "input": res.get("input") or _rd_run_info(res, "Input"),
        "input_format": res.get("input_format") or _rd_run_info(res, "Input format") or "site_features",
        "schema": res.get("schema"),
        "crawl_dates": _rd_run_info(res, "Crawl dates"),
        "institutions_file": _rd_run_info(res, "Institutions file"),
        "known_bad_file": _rd_run_info(res, "Known-bad file"),
        "stats_output_folder": res.get("out_dir"),
        "institutions": len(df),
        "flags_evaluated": [l for l, a in zip(fl["label"], fl["active_in_this_run"]) if a],
        "options": opts,
    }

    report = {"meta": meta, "headline": _rd_headline(res, opts, fl, charts), "summary": summary, "sections": sections,
              "flags": fl, "charts": charts, "charts_skipped": skipped, "tables": tables, "table_notes": notes,
              "warnings": list(res.get("warnings") or []), "files": {}}
    if not write:
        return report

    base = Path(out_dir) if out_dir is not None else Path(res.get("out_dir") or "stats_output") / "report_data"
    (base / "charts").mkdir(parents=True, exist_ok=True)
    (base / "tables").mkdir(parents=True, exist_ok=True)
    files = {"tables": {}, "charts": {}}
    for name, t in tables.items():
        p = base / "tables" / f"{name}.csv"
        _rd_flat_cells(t).to_csv(p, index=False)
        files["tables"][name] = str(p)
    for name, ch in charts.items():
        p = base / "charts" / f"{name}.csv"
        ch["data"].to_csv(p, index=False)
        files["charts"][name] = str(p)
    p = base / "headline.csv"
    pd.DataFrame(_rd_flatten(report["headline"]), columns=["key", "value"]).to_csv(p, index=False)
    files["headline"] = str(p)
    p = base / "flags.csv"
    fl.to_csv(p, index=False)
    files["flags"] = str(p)

    doc = {k: report[k] for k in ["meta", "headline", "sections", "flags", "charts", "charts_skipped", "table_notes", "warnings"]}
    doc["tables"] = ({n: t for n, t in tables.items()} if tables_in_json else
                     {n: {"file": files["tables"][n], "rows": len(t), "columns": list(map(str, t.columns))} for n, t in tables.items()})
    p = base / "report_data.json"
    files["json"] = str(p)
    doc["files"] = files
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(_rd_json(doc), fh, indent=2, ensure_ascii=False, allow_nan=False)
    report["files"] = files
    return report
