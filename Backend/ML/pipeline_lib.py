"""
pipeline_lib.py — the same cleaning/feature-engineering logic used in
Datacleaning.py / Datacleaning.ipynb, refactored into pure functions with no
side effects (no printing, no file-saving, no plotting) so the Flask backend
(app.py) can import and call it directly on every request.

This is intentionally the SAME logic as Datacleaning.py — it is not a second,
divergent implementation. If you change the cleaning rules, change them in
both places (or better: have Datacleaning.py import from here too).
"""

import pandas as pd
import numpy as np
from dateutil import parser as dateutil_parser

TEXT_COLS = ["record_id", "hospital_id", "hospital_name", "region", "department",
             "source_system", "notes", "period"]
NUMERIC_COLS = ["total_beds", "operational_beds", "occupied_beds_morning", "admissions",
                 "discharges", "transfers_in", "transfers_out", "emergency_arrivals",
                 "elective_surgeries", "cancellations", "staff_nurses", "staff_doctors",
                 "avg_wait_time_min", "median_wait_time_min", "bed_turnover_time_hr",
                 "patients_left_without_seen", "isolation_beds_used"]
CLIP_COLS = ["avg_wait_time_min", "median_wait_time_min", "bed_turnover_time_hr"]
DEPARTMENT_MAP = {
    "Er": "Emergency", "Emergency Room": "Emergency", "Emergency Dept": "Emergency",
    "Icu": "ICU", "Intensive Care Unit": "ICU", "Intensive Care": "ICU",
    "Opd": "OPD", "Out Patient": "OPD", "Outpatient": "OPD",
    "Paeds": "Pediatrics", "Medical Ward": "General Medicine",
    "Surgical Ward": "Surgery", "Obstetrics": "Maternity",
}


def _dateutil_safe(x, dayfirst):
    try:
        return dateutil_parser.parse(x, dayfirst=dayfirst)
    except Exception:
        return pd.NaT


def parse_mixed_dates(series, year, period_start_month=1, period_end_month=4):
    s = series.astype(str).str.strip()
    parsed = pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")

    mask = parsed.isna()
    if mask.any():
        parsed.loc[mask] = pd.to_datetime(s[mask], format="%Y/%m/%d", errors="coerce")

    mask = parsed.isna()
    if mask.any():
        parsed.loc[mask] = pd.to_datetime(s[mask].apply(lambda x: _dateutil_safe(x, True)))

    lo, hi = pd.Timestamp(year, period_start_month, 1), pd.Timestamp(year, period_end_month, 30)
    out_of_range = (parsed < lo) | (parsed > hi) | parsed.isna()
    if out_of_range.any():
        retry = pd.to_datetime(s[out_of_range].apply(lambda x: _dateutil_safe(x, False)))
        fixed_ok = (retry >= lo) & (retry <= hi)
        parsed.loc[out_of_range[out_of_range].index[fixed_ok.values]] = retry[fixed_ok]

    still_bad = parsed.isna() | (parsed < lo) | (parsed > hi)
    return parsed, still_bad


def load_raw(path):
    return pd.read_csv(path)


def raw_quality_stats(df_raw, year):
    """Stats about the RAW file, for the dashboard's 'before cleaning' banner."""
    df = df_raw.copy()
    df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_").str.replace("-", "_")
    stats = {
        "rows": int(len(df)),
        "columns": int(df.shape[1]),
        "null_cells": int(df.isnull().sum().sum()),
        "null_by_column": df.isnull().sum().to_dict(),
        "duplicate_rows": int(df.duplicated().sum()),
    }
    if {"occupied_beds_morning", "operational_beds"}.issubset(df.columns):
        stats["capacity_violations"] = int((df["occupied_beds_morning"] > df["operational_beds"]).sum())
    if "date" in df.columns:
        _, bad_dates = parse_mixed_dates(df["date"], year)
        stats["unparseable_dates_before_fix"] = int(bad_dates.sum())
    return stats


def clean_hospital_data(df_raw, year):
    """Same cleaning + feature-engineering pipeline as Datacleaning.py."""
    df = df_raw.copy()
    df.columns = (df.columns.str.strip().str.lower()
                  .str.replace(" ", "_").str.replace("-", "_"))

    n_before = len(df)
    df = df.drop_duplicates()
    n_duplicates_removed = n_before - len(df)

    for col in TEXT_COLS:
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip()

    if "department" in df.columns:
        df["department"] = df["department"].str.lower().str.title()
        df["department"] = df["department"].replace(DEPARTMENT_MAP)
        if df["department"].isnull().sum() > 0:
            df["department"] = df["department"].fillna(df["department"].mode()[0])

    parsed_dates, unresolved = parse_mixed_dates(df["date"], year)
    df["date"] = parsed_dates
    n_unresolved_dates = int(unresolved.sum())

    for col in NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in NUMERIC_COLS:
        if col in df.columns:
            df.loc[df[col] < 0, col] = np.nan

    for col in NUMERIC_COLS:
        if col in df.columns:
            dept_median = df.groupby("department")[col].transform("median")
            df[col] = df[col].fillna(dept_median)
            df[col] = df[col].fillna(df[col].median())

    df["capacity_flag"] = df["occupied_beds_morning"] > df["operational_beds"]
    n_capacity_flagged = int(df["capacity_flag"].sum())
    df.loc[df["capacity_flag"], "occupied_beds_morning"] = df.loc[df["capacity_flag"], "operational_beds"]

    for col in CLIP_COLS:
        if col in df.columns:
            def _clip_group(s):
                Q1, Q3 = s.quantile(0.25), s.quantile(0.75)
                IQR = Q3 - Q1
                return s.clip(Q1 - 1.5 * IQR, Q3 + 1.5 * IQR)
            df[col] = df.groupby("department")[col].transform(_clip_group)

    for col in ["region", "source_system"]:
        if col in df.columns and df[col].isnull().sum() > 0:
            df[col] = df[col].fillna(df[col].mode()[0])
    if "notes" in df.columns:
        df["notes"] = df["notes"].fillna("No issue flagged")

    df["occupancy_rate"] = (df["occupied_beds_morning"] / df["operational_beds"]).round(4)
    df["capacity_status"] = pd.cut(
        df["occupancy_rate"], bins=[-np.inf, 0.75, 0.90, np.inf],
        labels=["Normal", "High", "Critical"]
    ).astype(str)
    df["patient_turnover"] = df["admissions"] + df["discharges"]
    df["net_patient_flow"] = df["admissions"] - df["discharges"]
    df["patients_per_staff"] = (
        df["occupied_beds_morning"] / (df["staff_nurses"] + df["staff_doctors"]).replace(0, np.nan)
    ).round(2)

    df["date"] = df["date"].dt.strftime("%Y-%m-%d")

    cleaning_log = {
        "duplicates_removed": int(n_duplicates_removed),
        "unresolved_dates": n_unresolved_dates,
        "capacity_violations_capped": n_capacity_flagged,
        "rows_after_cleaning": int(len(df)),
        "residual_nulls": int(df.isnull().sum().sum()),
    }
    return df, cleaning_log
