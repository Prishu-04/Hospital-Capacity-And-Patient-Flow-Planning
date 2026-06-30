"""
Hospital Capacity & Patient-Flow Data Profiler
Corrected, complete pipeline.

Fixes vs the original Datacleaning.py/.ipynb:
  1. Mixed-format dates (ISO / D-M-Y / Y/M/D / ambiguous slash dates) are now
     parsed correctly using a multi-stage parser validated against each
     record's stated reporting period, instead of silently producing wrong
     dates or NaT via plain pd.to_datetime().
  2. The free-text "notes" data-quality-flag column is no longer mode-imputed
     (that was overwriting ~95% of genuinely clean rows with an arbitrary
     flag, "capacity reconciliation needed"). It is now left blank / labelled
     "No issue flagged".
  3. Outlier clipping is no longer applied globally across all departments.
     Department-mixed IQR clipping was collapsing Emergency department
     emergency_arrivals (true mean ~30/day) down to ~7.5, erasing 100% of the
     real signal for the one department where it matters most. Clipping is
     now done per-department, and only for genuinely continuous duration
     metrics (wait/turnover times) - not structural count metrics.
  4. Missing numeric values are now imputed with the department-level median
     instead of a single global median (departments differ hugely in scale).
  5. Impossible values (occupied beds > operational beds) are now detected,
     capped, and flagged in a new `capacity_flag` column instead of being
     silently ignored.
  6. Adds the feature engineering that was promised on the project site but
     never implemented: occupancy_rate, capacity_status, patient_turnover,
     net_patient_flow, patients_per_staff.
  7. Adds a real data dictionary, a before/after data-quality report, segment
     (department/region) breakdowns, and a saved (not just printed) insight
     memo.
  8. Fixes the 2 of 5 charts that silently never rendered (the code checked
     for columns named "bed_occupancy_rate" / "average_wait_time_minutes"
     which never existed in the dataset, so those `if` blocks were always
     False - only 3 of 5 charts were ever produced).
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from dateutil import parser as dateutil_parser
import json
import os

pd.set_option("display.max_columns", None)
plt.rcParams["figure.facecolor"] = "white"

RAW_BASELINE_PATH = "dataset1_baseline_2025.csv"
RAW_COMPARISON_PATH = "dataset2_comparison_2026.csv"
CHART_DIR = "charts"
os.makedirs(CHART_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# 1. LOAD RAW DATA
# ---------------------------------------------------------------------------
baseline_raw = pd.read_csv(RAW_BASELINE_PATH)
comparison_raw = pd.read_csv(RAW_COMPARISON_PATH)

print("Baseline shape:", baseline_raw.shape)
print("Comparison shape:", comparison_raw.shape)

# ---------------------------------------------------------------------------
# 2. DATA DICTIONARY  (deliverable that was missing entirely)
# ---------------------------------------------------------------------------
DATA_DICTIONARY = [
    ("record_id", "string", "Unique row identifier", "-", "unique"),
    ("date", "date", "Calendar date of the daily department record", "date", "within reporting period"),
    ("period", "string", "Reporting batch label", "-", "categorical"),
    ("hospital_id", "string", "Hospital code", "-", "HOSP_A..D"),
    ("hospital_name", "string", "Hospital full name", "-", "categorical"),
    ("region", "string", "Geographic region of the hospital", "-", "North/South/East/West"),
    ("department", "string", "Hospital department", "-", "categorical (standardised)"),
    ("total_beds", "integer", "Total bed capacity of the department", "beds", ">= operational_beds"),
    ("operational_beds", "integer", "Beds currently staffed/usable", "beds", "<= total_beds"),
    ("occupied_beds_morning", "float", "Beds occupied at the morning census", "beds", "<= operational_beds"),
    ("admissions", "integer", "Patients admitted that day", "patients", ">= 0"),
    ("discharges", "float", "Patients discharged that day", "patients", ">= 0"),
    ("transfers_in", "integer", "Patients transferred into the department", "patients", ">= 0"),
    ("transfers_out", "integer", "Patients transferred out of the department", "patients", ">= 0"),
    ("emergency_arrivals", "integer", "Emergency arrivals that day (structurally concentrated in Emergency dept.)", "patients", ">= 0"),
    ("elective_surgeries", "integer", "Elective surgeries performed", "procedures", ">= 0"),
    ("cancellations", "integer", "Cancelled procedures", "procedures", ">= 0"),
    ("staff_nurses", "float", "Nurses on duty", "staff", ">= 0"),
    ("staff_doctors", "integer", "Doctors on duty", "staff", ">= 0"),
    ("avg_wait_time_min", "float", "Average patient wait time", "minutes", ">= 0"),
    ("median_wait_time_min", "float", "Median patient wait time", "minutes", ">= 0"),
    ("bed_turnover_time_hr", "float", "Average time to turn over a bed between patients", "hours", ">= 0"),
    ("patients_left_without_seen", "integer", "Patients who left before being seen", "patients", ">= 0"),
    ("isolation_beds_used", "integer", "Isolation beds in use", "beds", ">= 0"),
    ("source_system", "string", "Originating IT system - proxy for data lineage", "-", "HIS_A/HIS_B/BedBoard/Manual_CSV"),
    ("notes", "string", "Free-text data-quality flag entered by an analyst (mostly blank)", "-", "free text"),
    ("occupancy_rate", "float (derived)", "occupied_beds_morning / operational_beds", "ratio", "0-1"),
    ("capacity_status", "string (derived)", "Normal / High / Critical band based on occupancy_rate", "-", "categorical"),
    ("patient_turnover", "integer (derived)", "admissions + discharges", "patients", ">= 0"),
    ("net_patient_flow", "integer (derived)", "admissions - discharges", "patients", "can be negative"),
    ("patients_per_staff", "float (derived)", "occupied_beds_morning / (staff_nurses + staff_doctors)", "ratio", ">= 0"),
    ("capacity_flag", "boolean (derived)", "True if occupied beds originally exceeded operational beds (data-entry error, capped)", "-", "True/False"),
]
data_dictionary = pd.DataFrame(
    DATA_DICTIONARY, columns=["column", "type", "description", "unit", "valid_range"]
)

# ---------------------------------------------------------------------------
# 3. ROBUST MIXED-FORMAT DATE PARSER
# ---------------------------------------------------------------------------
def _dateutil_safe(x, dayfirst):
    try:
        return dateutil_parser.parse(x, dayfirst=dayfirst)
    except Exception:
        return pd.NaT


def parse_mixed_dates(series, year, period_start_month=1, period_end_month=4):
    """Parses a column containing a mix of ISO, slash and dash date formats,
    validating the result against the dataset's known reporting period
    (Jan-Apr) and retrying the opposite day/month convention for any value
    that parses outside that window. Returns (parsed_series, unresolved_mask).
    """
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


# ---------------------------------------------------------------------------
# 4. DATA-QUALITY REPORT  (BEFORE cleaning) - "Quality Assessment" step
# ---------------------------------------------------------------------------
def quality_report(df, name, year):
    report = {}
    report["name"] = name
    report["rows"] = len(df)
    report["duplicate_rows"] = int(df.duplicated().sum())
    cols = df.columns.str.strip().str.lower().str.replace(" ", "_").str.replace("-", "_")
    df2 = df.copy()
    df2.columns = cols
    report["missing_pct"] = (df2.isnull().mean() * 100).round(2).to_dict()
    if "occupied_beds_morning" in df2.columns and "operational_beds" in df2.columns:
        report["capacity_violations"] = int((df2["occupied_beds_morning"] > df2["operational_beds"]).sum())
    if "date" in df2.columns:
        _, bad_dates = parse_mixed_dates(df2["date"], year)
        report["unparseable_dates_before_fix"] = int(bad_dates.sum())
    neg_cols = {}
    for c in ["admissions", "discharges", "transfers_in", "transfers_out", "staff_nurses", "staff_doctors"]:
        if c in df2.columns:
            n = int((df2[c] < 0).sum())
            if n:
                neg_cols[c] = n
    report["negative_value_counts"] = neg_cols
    return report


quality_before = {
    "baseline_2025": quality_report(baseline_raw, "baseline_2025", 2025),
    "comparison_2026": quality_report(comparison_raw, "comparison_2026", 2026),
}
print("\n=== DATA QUALITY REPORT (BEFORE CLEANING) ===")
print(json.dumps(quality_before, indent=2, default=str))

# ---------------------------------------------------------------------------
# 5. CLEANING FUNCTION (fixed)
# ---------------------------------------------------------------------------
TEXT_COLS = ["record_id", "hospital_id", "hospital_name", "region", "department",
             "source_system", "notes", "period"]
NUMERIC_COLS = ["total_beds", "operational_beds", "occupied_beds_morning", "admissions",
                 "discharges", "transfers_in", "transfers_out", "emergency_arrivals",
                 "elective_surgeries", "cancellations", "staff_nurses", "staff_doctors",
                 "avg_wait_time_min", "median_wait_time_min", "bed_turnover_time_hr",
                 "patients_left_without_seen", "isolation_beds_used"]
# Only continuous duration/rate metrics get outlier clipping - count metrics
# (admissions, emergency_arrivals, etc.) are left untouched since their
# department-to-department variation is real clinical signal, not noise.
CLIP_COLS = ["avg_wait_time_min", "median_wait_time_min", "bed_turnover_time_hr"]
DEPARTMENT_MAP = {
    "Er": "Emergency", "Emergency Room": "Emergency", "Emergency Dept": "Emergency",
    "Icu": "ICU", "Intensive Care Unit": "ICU", "Intensive Care": "ICU",
    "Opd": "OPD", "Out Patient": "OPD", "Outpatient": "OPD",
    "Paeds": "Pediatrics", "Medical Ward": "General Medicine",
    "Surgical Ward": "Surgery", "Obstetrics": "Maternity",
}


def clean_hospital_data(df_raw, year):
    df = df_raw.copy()
    df.columns = (df.columns.str.strip().str.lower()
                  .str.replace(" ", "_").str.replace("-", "_"))

    n_before = len(df)
    df = df.drop_duplicates()
    n_duplicates_removed = n_before - len(df)

    # --- text columns: trim whitespace only, leave NaN as real NaN ---
    for col in TEXT_COLS:
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip()

    # --- standardise department labels ---
    if "department" in df.columns:
        df["department"] = df["department"].str.lower().str.title()
        df["department"] = df["department"].replace(DEPARTMENT_MAP)
        # Fill department NaN *before* it is used as a groupby key below -
        # pandas' groupby() silently drops NaN-key rows from every group,
        # which would otherwise leave those rows un-imputed and un-clipped
        # further down the pipeline (a second, subtler version of the same
        # "silent data loss" bug class as the date-parsing issue above).
        if df["department"].isnull().sum() > 0:
            df["department"] = df["department"].fillna(df["department"].mode()[0])

    # --- robust date parsing ---
    parsed_dates, unresolved = parse_mixed_dates(df["date"], year)
    df["date"] = parsed_dates
    n_unresolved_dates = int(unresolved.sum())

    # --- numeric columns: coerce to numeric ---
    for col in NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # --- flag impossible negative values, then null them out ---
    for col in NUMERIC_COLS:
        if col in df.columns:
            df.loc[df[col] < 0, col] = np.nan

    # --- impute missing numerics with the DEPARTMENT-level median ---
    for col in NUMERIC_COLS:
        if col in df.columns:
            dept_median = df.groupby("department")[col].transform("median")
            df[col] = df[col].fillna(dept_median)
            df[col] = df[col].fillna(df[col].median())  # global fallback

    # --- capacity logic check: occupied beds cannot exceed operational beds ---
    df["capacity_flag"] = df["occupied_beds_morning"] > df["operational_beds"]
    n_capacity_flagged = int(df["capacity_flag"].sum())
    df.loc[df["capacity_flag"], "occupied_beds_morning"] = df.loc[df["capacity_flag"], "operational_beds"]

    # --- department-wise IQR clipping, continuous duration metrics only ---
    for col in CLIP_COLS:
        if col in df.columns:
            def _clip_group(s):
                Q1, Q3 = s.quantile(0.25), s.quantile(0.75)
                IQR = Q3 - Q1
                return s.clip(Q1 - 1.5 * IQR, Q3 + 1.5 * IQR)
            df[col] = df.groupby("department")[col].transform(_clip_group)

    # --- categorical missing-value fill: TRUE categoricals only ---
    # "notes" is a free-text flag column, NOT imputed with mode (that would
    # fabricate a data-quality flag on ~95% of clean rows).
    for col in ["region", "source_system"]:
        if col in df.columns and df[col].isnull().sum() > 0:
            df[col] = df[col].fillna(df[col].mode()[0])
    if "notes" in df.columns:
        df["notes"] = df["notes"].fillna("No issue flagged")

    # --- feature engineering (previously promised, never implemented) ---
    df["occupancy_rate"] = (df["occupied_beds_morning"] / df["operational_beds"]).round(4)
    df["capacity_status"] = pd.cut(
        df["occupancy_rate"], bins=[-np.inf, 0.75, 0.90, np.inf],
        labels=["Normal", "High", "Critical"]
    )
    df["patient_turnover"] = df["admissions"] + df["discharges"]
    df["net_patient_flow"] = df["admissions"] - df["discharges"]
    df["patients_per_staff"] = (
        df["occupied_beds_morning"] / (df["staff_nurses"] + df["staff_doctors"]).replace(0, np.nan)
    ).round(2)

    cleaning_log = {
        "duplicates_removed": int(n_duplicates_removed),
        "unresolved_dates": n_unresolved_dates,
        "capacity_violations_capped": n_capacity_flagged,
    }
    return df, cleaning_log


baseline_clean, log_baseline = clean_hospital_data(baseline_raw, 2025)
comparison_clean, log_comparison = clean_hospital_data(comparison_raw, 2026)

print("\nCleaning log - baseline:", log_baseline)
print("Cleaning log - comparison:", log_comparison)
print("\nCleaned baseline shape:", baseline_clean.shape)
print("Cleaned comparison shape:", comparison_clean.shape)

# ---------------------------------------------------------------------------
# 6. SAVE CLEANED EXPORTS
# ---------------------------------------------------------------------------
baseline_clean.to_csv("cleaned_baseline_2025.csv", index=False)
comparison_clean.to_csv("cleaned_comparison_2026.csv", index=False)
data_dictionary.to_csv("data_dictionary.csv", index=False)

baseline_clean["year"] = 2025
comparison_clean["year"] = 2026
combined = pd.concat([baseline_clean, comparison_clean], ignore_index=True)
combined.to_csv("combined_cleaned_hospital_data.csv", index=False)
print("\nSaved cleaned_baseline_2025.csv, cleaned_comparison_2026.csv, "
      "combined_cleaned_hospital_data.csv, data_dictionary.csv")

# ---------------------------------------------------------------------------
# 7. DATA-QUALITY REPORT (AFTER cleaning) - completeness / duplicate evidence
# ---------------------------------------------------------------------------
quality_after = {
    "baseline_2025": {"rows": len(baseline_clean),
                       "missing_pct": (baseline_clean.isnull().mean() * 100).round(2).to_dict(),
                       "duplicate_rows": int(baseline_clean.duplicated().sum())},
    "comparison_2026": {"rows": len(comparison_clean),
                         "missing_pct": (comparison_clean.isnull().mean() * 100).round(2).to_dict(),
                         "duplicate_rows": int(comparison_clean.duplicated().sum())},
}
print("\n=== DATA QUALITY REPORT (AFTER CLEANING) ===")
print(json.dumps(quality_after, indent=2, default=str))

# ---------------------------------------------------------------------------
# 8. SEGMENT ANALYSIS (department / region) - required by the brief, was missing
# ---------------------------------------------------------------------------
segment_department = combined.groupby("department").agg(
    avg_occupancy_rate=("occupancy_rate", "mean"),
    avg_wait_time_min=("avg_wait_time_min", "mean"),
    avg_patients_left_without_seen=("patients_left_without_seen", "mean"),
    records=("record_id", "count"),
).round(3).sort_values("avg_occupancy_rate", ascending=False)

segment_region = combined.groupby("region").agg(
    avg_occupancy_rate=("occupancy_rate", "mean"),
    avg_wait_time_min=("avg_wait_time_min", "mean"),
    records=("record_id", "count"),
).round(3).sort_values("avg_wait_time_min", ascending=False)

print("\n=== SEGMENT: BY DEPARTMENT ===")
print(segment_department)
print("\n=== SEGMENT: BY REGION (fairness / unequal-service check) ===")
print(segment_region)

# ---------------------------------------------------------------------------
# 9. VISUALISATIONS - 5 decision-oriented charts, saved to /charts
# ---------------------------------------------------------------------------
# Chart 1: Missing values in the raw data (missingness)
plt.figure(figsize=(12, 5))
baseline_raw.isnull().sum().plot(kind="bar", color="#2563EB")
plt.title("Chart 1 - Missing Values in Raw Baseline Dataset")
plt.xlabel("Column"); plt.ylabel("Missing Count")
plt.xticks(rotation=75, fontsize=8)
plt.tight_layout()
plt.savefig(f"{CHART_DIR}/chart1_missing_values.png", dpi=150)
plt.show()
plt.close()

# Chart 2: Monthly occupancy-rate trend, 2025 vs 2026 (trends)
combined["month"] = combined["date"].dt.to_period("M").dt.to_timestamp()
monthly_occ = combined.groupby(["year", "month"])["occupancy_rate"].mean().reset_index()
plt.figure(figsize=(10, 5))
for yr, grp in monthly_occ.groupby("year"):
    plt.plot(grp["month"].dt.strftime("%b"), grp["occupancy_rate"] * 100, marker="o", label=str(yr))
plt.axhline(90, color="#DC2626", linestyle="--", linewidth=1, label="Critical threshold")
plt.title("Chart 2 - Average Bed Occupancy Rate by Month: 2025 vs 2026")
plt.xlabel("Month"); plt.ylabel("Occupancy Rate (%)")
plt.legend()
plt.tight_layout()
plt.savefig(f"{CHART_DIR}/chart2_occupancy_trend.png", dpi=150)
plt.show()
plt.close()

# Chart 3: Average wait time by department (segments)
dept_wait = segment_department["avg_wait_time_min"].sort_values(ascending=False)
plt.figure(figsize=(10, 5))
dept_wait.plot(kind="bar", color="#F59E0B")
plt.title("Chart 3 - Average Wait Time by Department (minutes)")
plt.xlabel("Department"); plt.ylabel("Avg Wait Time (min)")
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(f"{CHART_DIR}/chart3_wait_time_by_department.png", dpi=150)
plt.show()
plt.close()

# Chart 4: Admissions vs discharges by month (trend / flow balance)
monthly_flow = combined.groupby(["year", "month"])[["admissions", "discharges"]].sum().reset_index()
months = sorted(monthly_flow["month"].unique())
x = np.arange(len(months))
width = 0.2
fig, ax = plt.subplots(figsize=(10, 5))
colors = {2025: "#2563EB", 2026: "#DC2626"}
for i, yr in enumerate(sorted(monthly_flow["year"].unique())):
    grp = monthly_flow[monthly_flow["year"] == yr].set_index("month").reindex(months)
    offset = (i - 0.5) * (2 * width)
    ax.bar(x + offset, grp["admissions"], width=width, color=colors[yr], alpha=0.95, label=f"{yr} Admissions")
    ax.bar(x + offset + width, grp["discharges"], width=width, color=colors[yr], alpha=0.45, label=f"{yr} Discharges")
ax.set_xticks(x)
ax.set_xticklabels([pd.Timestamp(m).strftime("%b") for m in months])
plt.title("Chart 4 - Admissions vs Discharges by Month")
plt.xlabel("Month"); plt.ylabel("Patients")
plt.legend(fontsize=8)
plt.tight_layout()
plt.savefig(f"{CHART_DIR}/chart4_admissions_vs_discharges.png", dpi=150)
plt.show()
plt.close()

# Chart 5: Year-over-year comparison of key operational metrics (insight memo support)
key_metrics = ["avg_wait_time_min", "patients_left_without_seen", "occupancy_rate", "bed_turnover_time_hr"]
yearly_avg = combined.groupby("year")[key_metrics].mean()
yearly_avg_norm = yearly_avg / yearly_avg.loc[2025]  # index to 2025 = 1.0 so different units are comparable
plt.figure(figsize=(10, 5))
yearly_avg_norm.T.plot(kind="bar")
plt.title("Chart 5 - 2026 vs 2025, Indexed to 2025 = 1.0")
plt.xlabel("Metric"); plt.ylabel("Ratio vs 2025 baseline")
plt.xticks(rotation=20)
plt.tight_layout()
plt.savefig(f"{CHART_DIR}/chart5_yoy_key_metrics.png", dpi=150)
plt.show()
plt.close()

print("\nSaved 5 charts to ./charts/")

# ---------------------------------------------------------------------------
# 10. INSIGHT MEMO - saved to file, not just printed
# ---------------------------------------------------------------------------
avg_2025 = combined[combined["year"] == 2025]
avg_2026 = combined[combined["year"] == 2026]

memo_lines = []
memo_lines.append("# Hospital Capacity & Patient-Flow - Insight Memo\n")
memo_lines.append(f"Comparing Jan-Apr 2025 (baseline) vs Jan-Apr 2026 (comparison), "
                   f"{len(baseline_clean)} and {len(comparison_clean)} cleaned department-day records respectively.\n")

memo_lines.append("## Headline numbers\n")
for col, label, unit in [
    ("occupancy_rate", "Average bed occupancy rate", "ratio"),
    ("avg_wait_time_min", "Average wait time", "min"),
    ("patients_left_without_seen", "Patients who left without being seen (avg/record)", "patients"),
    ("bed_turnover_time_hr", "Average bed turnover time", "hr"),
]:
    a, b = avg_2025[col].mean(), avg_2026[col].mean()
    change = b - a
    pct = (change / a * 100) if a else float("nan")
    memo_lines.append(f"- **{label}**: {a:.2f} {unit} (2025) -> {b:.2f} {unit} (2026), "
                       f"change {change:+.2f} ({pct:+.1f}%)")

memo_lines.append("\n## Segment notes\n")
top_dept = segment_department["avg_occupancy_rate"].idxmax()
memo_lines.append(f"- **{top_dept}** runs the highest average occupancy rate of any department "
                   f"({segment_department.loc[top_dept, 'avg_occupancy_rate']*100:.1f}%) - "
                   f"a candidate for proactive bed-planning review.")
top_region = segment_region["avg_wait_time_min"].idxmax()
memo_lines.append(f"- **{top_region}** region has the longest average wait time "
                   f"({segment_region.loc[top_region, 'avg_wait_time_min']:.1f} min) of any region - "
                   f"worth a fairness/unequal-service follow-up (see Responsible Use notes).")

memo_lines.append("\n## Data-quality notes\n")
memo_lines.append(f"- {log_baseline['duplicates_removed']} duplicate rows removed from baseline, "
                   f"{log_comparison['duplicates_removed']} from comparison.")
memo_lines.append(f"- {log_baseline['capacity_violations_capped']} (baseline) and "
                   f"{log_comparison['capacity_violations_capped']} (comparison) records had occupied beds "
                   f"recorded above operational capacity; these were capped and flagged in `capacity_flag` "
                   f"rather than silently changed.")
memo_lines.append("- All dates were re-parsed against each record's stated reporting period to resolve "
                   "ambiguous day/month ordering coming from multiple source systems.")
memo_lines.append("\n## Caveat\n")
memo_lines.append("This is a descriptive comparison only. No predictive model was trained and no causal claims "
                   "are made; differences may reflect seasonality, source-system changes, or genuine operational "
                   "shifts and should be reviewed by hospital operations staff before acting on them.\n")

with open("insight_memo.md", "w") as f:
    f.write("\n".join(memo_lines))
print("\nSaved insight_memo.md")
print("\n".join(memo_lines))
