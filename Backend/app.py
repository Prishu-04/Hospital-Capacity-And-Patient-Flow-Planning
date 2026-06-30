"""
app.py — Flask backend for the Hospital Capacity & Patient-Flow dashboard.

Run with:  python app.py
Then open: http://localhost:5000

What this does:
  - Serves Frontend/index.html (the dashboard) at "/"
  - GET  /api/raw/<year>      -> raw CSV rows as JSON   (year = 2025 or 2026)
  - GET  /api/clean/<year>    -> runs the REAL cleaning pipeline (pipeline_lib.py)
                                 on the raw CSV and returns the cleaned rows as JSON
  - GET  /api/summary         -> KPI numbers computed live from both years
  - GET  /api/charts          -> every chart's data, computed live from the
                                 cleaned datasets (no hardcoded numbers)

Everything returned by these endpoints is computed from the two CSV files in
Backend/ML/ at request time (cleaned results are cached in memory after the
first request per year so repeated chart/summary calls don't re-run pandas
from scratch).
"""

import os
import sys
import json

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
import pandas as pd
import numpy as np

ML_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ML")
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Frontend")
sys.path.insert(0, ML_DIR)

import pipeline_lib as pl  # noqa: E402

app = Flask(__name__, static_folder=None)
CORS(app)

RAW_PATHS = {
    "2025": os.path.join(ML_DIR, "dataset1_baseline_2025.csv"),
    "2026": os.path.join(ML_DIR, "dataset2_comparison_2026.csv"),
}
YEAR_INT = {"2025": 2025, "2026": 2026}

_cache = {"raw": {}, "clean": {}, "log": {}}


def get_raw(year):
    if year not in _cache["raw"]:
        _cache["raw"][year] = pl.load_raw(RAW_PATHS[year])
    return _cache["raw"][year]


def get_clean(year):
    if year not in _cache["clean"]:
        raw = get_raw(year)
        cleaned, log = pl.clean_hospital_data(raw, YEAR_INT[year])
        _cache["clean"][year] = cleaned
        _cache["log"][year] = log
    return _cache["clean"][year], _cache["log"][year]


def df_to_records(df):
    # NaN -> None so it serialises as JSON null, not the string "NaN"
    return json.loads(df.replace({np.nan: None}).to_json(orient="records"))


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:filename>")
def frontend_assets(filename):
    return send_from_directory(FRONTEND_DIR, filename)


# ---------------------------------------------------------------------------
# Raw / cleaned data
# ---------------------------------------------------------------------------
@app.route("/api/raw/<year>")
def api_raw(year):
    if year not in RAW_PATHS:
        return jsonify({"error": "year must be 2025 or 2026"}), 400
    df = get_raw(year)
    df = df.copy()
    df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_").str.replace("-", "_")
    stats = pl.raw_quality_stats(df, YEAR_INT[year])
    return jsonify({
        "year": year,
        "columns": list(df.columns),
        "rows": df_to_records(df),
        "stats": stats,
    })


@app.route("/api/clean/<year>")
def api_clean(year):
    if year not in RAW_PATHS:
        return jsonify({"error": "year must be 2025 or 2026"}), 400
    cleaned, log = get_clean(year)
    return jsonify({
        "year": year,
        "columns": list(cleaned.columns),
        "rows": df_to_records(cleaned),
        "log": log,
    })


# ---------------------------------------------------------------------------
# KPI summary
# ---------------------------------------------------------------------------
@app.route("/api/summary")
def api_summary():
    raw25, raw26 = get_raw("2025"), get_raw("2026")
    clean25, log25 = get_clean("2025")
    clean26, log26 = get_clean("2026")

    raw25_c = raw25.copy()
    raw25_c.columns = raw25_c.columns.str.strip().str.lower().str.replace(" ", "_").str.replace("-", "_")
    raw26_c = raw26.copy()
    raw26_c.columns = raw26_c.columns.str.strip().str.lower().str.replace(" ", "_").str.replace("-", "_")

    raw_nulls_total = int(raw25_c.isnull().sum().sum() + raw26_c.isnull().sum().sum())
    dup_total = log25["duplicates_removed"] + log26["duplicates_removed"]
    residual_nulls_total = log25["residual_nulls"] + log26["residual_nulls"]

    combined = pd.concat([clean25.assign(year=2025), clean26.assign(year=2026)], ignore_index=True)
    hospitals = sorted(combined["hospital_name"].unique().tolist())
    departments = sorted(combined["department"].unique().tolist())

    avg_occ_2025 = float(clean25["occupancy_rate"].mean())
    avg_occ_2026 = float(clean26["occupancy_rate"].mean())
    avg_wait_2025 = float(clean25["avg_wait_time_min"].mean())
    avg_wait_2026 = float(clean26["avg_wait_time_min"].mean())

    total_cells_raw = (raw25_c.shape[0] * raw25_c.shape[1]) + (raw26_c.shape[0] * raw26_c.shape[1])
    completeness_after = round(100 * (1 - residual_nulls_total / total_cells_raw), 1) if total_cells_raw else None

    return jsonify({
        "total_records": int(len(clean25) + len(clean26)),
        "records_per_year": {"2025": int(len(clean25)), "2026": int(len(clean26))},
        "raw_rows_per_year": {"2025": int(len(raw25_c)), "2026": int(len(raw26_c))},
        "raw_null_cells_total": raw_nulls_total,
        "raw_null_cells_per_year": {"2025": int(raw25_c.isnull().sum().sum()),
                                     "2026": int(raw26_c.isnull().sum().sum())},
        "duplicates_removed_total": dup_total,
        "duplicates_removed_per_year": {"2025": log25["duplicates_removed"],
                                         "2026": log26["duplicates_removed"]},
        "capacity_violations_capped": {"2025": log25["capacity_violations_capped"],
                                        "2026": log26["capacity_violations_capped"]},
        "residual_nulls_total": residual_nulls_total,
        "completeness_after_cleaning_pct": completeness_after,
        "avg_occupancy_2025": round(avg_occ_2025 * 100, 1),
        "avg_occupancy_2026": round(avg_occ_2026 * 100, 1),
        "avg_wait_2025": round(avg_wait_2025, 1),
        "avg_wait_2026": round(avg_wait_2026, 1),
        "wait_change_pct": round((avg_wait_2026 - avg_wait_2025) / avg_wait_2025 * 100, 1),
        "hospitals": hospitals,
        "n_hospitals": len(hospitals),
        "departments": departments,
        "n_departments": len(departments),
        "columns": int(raw25_c.shape[1]),
    })


# ---------------------------------------------------------------------------
# Chart data — every number here is computed live from the cleaned dataframes
# ---------------------------------------------------------------------------
@app.route("/api/charts")
def api_charts():
    clean25, _ = get_clean("2025")
    clean26, _ = get_clean("2026")
    raw25, _ = get_raw("2025"), None
    raw25c = raw25.copy()
    raw25c.columns = raw25c.columns.str.strip().str.lower().str.replace(" ", "_").str.replace("-", "_")

    combined = pd.concat([clean25.assign(year=2025), clean26.assign(year=2026)], ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"])

    # 1. Occupancy rate by department, 2025 vs 2026
    occ_dept = combined.groupby(["department", "year"])["occupancy_rate"].mean().unstack("year") * 100
    occ_dept = occ_dept.sort_values(2025, ascending=False)

    # 2. Occupancy rate by hospital
    occ_hosp = combined.groupby(["hospital_name", "year"])["occupancy_rate"].mean().unstack("year") * 100

    # 3. Wait time by hospital
    wait_hosp = combined.groupby(["hospital_name", "year"])["avg_wait_time_min"].mean().unstack("year")

    # 4. Monthly admissions by hospital (2025 only, to match the original chart's scope)
    c25 = clean25.copy()
    c25["date"] = pd.to_datetime(c25["date"])
    c25["month"] = c25["date"].dt.strftime("%b")
    month_order = ["Jan", "Feb", "Mar", "Apr"]
    monthly_adm = (c25.groupby(["hospital_name", "month"])["admissions"].sum()
                   .unstack("month").reindex(columns=month_order))

    # 5. Admissions by department, 2025 vs 2026
    adm_dept = combined.groupby(["department", "year"])["admissions"].sum().unstack("year")
    adm_dept = adm_dept.sort_values(2025, ascending=False)

    # 6. Admissions by region (both years combined)
    adm_region = combined.groupby("region")["admissions"].sum().sort_values(ascending=False)

    # 7. Nurse-to-bed ratio by department (2025)
    nurse_bed = (clean25.groupby("department")
                 .apply(lambda g: (g["staff_nurses"] / g["operational_beds"]).mean())
                 .sort_values(ascending=False))

    # 8. Elective surgery cancellations by hospital (2025)
    cancellations = clean25.groupby("hospital_name")["cancellations"].sum().sort_values(ascending=False)

    # 9. Missing values by column, raw 2025
    missing_raw = raw25c.isnull().sum()
    missing_raw = missing_raw[missing_raw > 0].sort_values(ascending=False)

    def series_to_pairs(s):
        return {"labels": [str(x) for x in s.index.tolist()], "values": [round(float(v), 2) for v in s.values.tolist()]}

    def frame_to_grouped(df_):
        return {
            "labels": [str(x) for x in df_.index.tolist()],
            "2025": [None if pd.isna(v) else round(float(v), 2) for v in df_.get(2025, pd.Series(dtype=float)).reindex(df_.index).values.tolist()],
            "2026": [None if pd.isna(v) else round(float(v), 2) for v in df_.get(2026, pd.Series(dtype=float)).reindex(df_.index).values.tolist()],
        }

    return jsonify({
        "occupancy_by_department": frame_to_grouped(occ_dept),
        "occupancy_by_hospital": frame_to_grouped(occ_hosp),
        "wait_by_hospital": frame_to_grouped(wait_hosp),
        "monthly_admissions_by_hospital": {
            "labels": month_order,
            "series": [{"label": h, "data": [None if pd.isna(v) else round(float(v), 1) for v in row.values]}
                       for h, row in monthly_adm.iterrows()],
        },
        "admissions_by_department": frame_to_grouped(adm_dept),
        "admissions_by_region": series_to_pairs(adm_region),
        "nurse_bed_ratio": series_to_pairs(nurse_bed),
        "cancellations_by_hospital": series_to_pairs(cancellations),
        "missing_by_column": series_to_pairs(missing_raw),
    })










if __name__ == " __main__\:
