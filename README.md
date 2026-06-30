# Hospital Capacity & Patient-Flow — Full-Stack Dashboard

**Sector:** Health & Life Sciences → Hospitals & Clinical Operations

A real full-stack version of the project: a Flask backend that runs your actual
pandas cleaning pipeline on request, and a dashboard frontend that shows the
raw dataset, lets you click **CLEAN DATA** to trigger the real pipeline on the
backend, and renders 9 charts built from numbers computed live from the
cleaned data — nothing on the page is hardcoded.

## Folder structure

```
Hospital-Capacity/
│
├── Frontend/
│   └── index.html              ← the dashboard (fetches everything from the backend)
│
└── Backend/
    ├── app.py                   ← Flask server: serves the dashboard + the API below
    ├── requirements.txt
    └── ML/
        ├── pipeline_lib.py       ← the cleaning/feature-engineering logic, as importable functions
        ├── Datacleaning.py       ← same pipeline as a standalone script (for the notebook/CLI deliverable)
        ├── Datacleaning.ipynb    ← the documented, executed notebook (data-science deliverable)
        ├── dataset1_baseline_2025.csv
        ├── dataset2_comparison_2026.csv
        ├── charts/               ← the 5 static chart PNGs from the notebook
        └── README.md
```

## How to run it

1. Clone or download this repo, then move into the project root:

   ```bash
   cd Hospital-Capacity-And-Patient-Flow-Planning
   ```

2. (Recommended) Create and activate a virtual environment:

   ```bash
   python -m venv venv
   source venv/bin/activate      # Windows: venv\Scripts\activate
   ```

3. Install the backend dependencies:

   ```bash
   pip install -r Backend/requirements.txt
   ```

4. Run the Flask server:

   ```bash
   python Backend/app.py
   ```

5. Open **http://localhost:5000** in your browser. That's it — one process
   serves both the dashboard and the API (no separate frontend server needed).

### Running with gunicorn / deploying

The included `Procfile` is set up for platforms like Heroku/Render:

```bash
web: gunicorn Backend.app:app
```

To run it the same way locally:

```bash
pip install gunicorn
gunicorn Backend.app:app
```

## What's actually happening when you click around

- **Overview tab** — the KPI numbers (total records, raw null cells, average
  occupancy, etc.) are fetched from `GET /api/summary`, which computes them
  live from both CSV files.
- **Dataset Viewer tab** — loads the *real* raw CSV rows via `GET /api/raw/<year>`.
  Clicking **CLEAN DATA** calls `GET /api/clean/<year>`, which runs
  `pipeline_lib.clean_hospital_data()` — the exact same cleaning function used
  in `Datacleaning.py`/`.ipynb` — on the raw data and returns the cleaned rows.
  This is a real computation each time, not a canned response (results are
  cached in memory after the first run per year so repeat views are instant).
- **Analytics tab** — all 9 charts are built from `GET /api/charts`, which
  aggregates the cleaned, combined dataset (occupancy by department/hospital,
  wait times, admissions by department/region, nurse-to-bed ratios,
  cancellations, and the raw missing-value counts).

## API reference

| Endpoint | What it returns |
|---|---|
| `GET /api/raw/<year>` | Raw CSV rows for `year` (2025 or 2026) + null/duplicate stats |
| `GET /api/clean/<year>` | Cleaned + feature-engineered rows for `year`, plus a cleaning log (duplicates removed, capacity violations capped, etc.) |
| `GET /api/summary` | Dashboard KPI numbers, computed from both years |
| `GET /api/charts` | All 9 charts' data, computed from the cleaned & combined dataset |

## If something doesn't load

Open your browser's developer console (F12) — failed fetches are logged
there. The most common cause is the backend not running: make sure
`python app.py` is still active in a terminal and that nothing else is using
port 5000. If you see "Backend not reachable" on the page itself, that
confirms the frontend is working but can't reach the Flask server.

## Note on the data-science deliverable

`Backend/ML/Datacleaning.ipynb` and `Datacleaning.py` are kept as standalone
artifacts (matching the original project brief — a documented, reproducible
notebook). `pipeline_lib.py` mirrors their cleaning logic in importable
function form so the web app can reuse it without re-running the whole
notebook on every click. If you change the cleaning rules, update both the
notebook/script (for the write-up) and `pipeline_lib.py` (for the live app).
