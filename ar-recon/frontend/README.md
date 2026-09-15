# AR Reconciliation — Frontend (React + MUI, KPMG theme)

React 18 + Vite + Material UI dashboard for the AR Reconciliation backend.
KPMG palette: Blue `#00338D`, Medium `#005EB8`, Light `#0091DA`.

## Setup

```bash
cd frontend
npm install
cp .env.example .env         # set VITE_API_BASE_URL if backend isn't on :8000
npm run dev                  # http://localhost:5173
```

The backend must be running (default `http://localhost:8000`) and must allow
this origin via CORS (`CORS_ORIGINS_RAW` in the backend `.env` — 5173 is
allowed by default).

## Pages

- **Files** — upload .xlsx with a file-type tag; lists uploaded files.
- **Run Reconciliation** — pick uploaded files, month, tolerance; triggers a run.
- **Dashboard** — 11 summary cards + exception-summary chart/table.
- **Results** — filterable/searchable/sortable final reconciliation grid; CSV/Excel export.
- **Exceptions** — exception grid with severity, filter by code; CSV/Excel export.

## Build

```bash
npm run build       # outputs to dist/
npm run preview
```
