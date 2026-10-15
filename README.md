# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent). MongoDB 6.0+ is required (`$densify`, `$setWindowFields`).

## Status
All endpoints in `openapi.yaml` are implemented, including `/admin/explain/{endpoint}`. Part A findings are in `REVIEW.md`, answers in `DECISIONS.md`.

## Things worth knowing
- Analytics read the **stored** derived values; rates, averages and percentages are rounded half-up using integer arithmetic inside the pipelines.
- `moving_avg_7d` averages the already rounded (4 decimals) `attendance_rate` values of the window.
- `PATCH` treats a request as "no change" when status / punch_in / punch_out are all unchanged. Concurrent PATCHes use an optimistic check on the history length and retry, then 409.
- Records dated before `joined_on` are still counted in the monthly numbers (only `working_days` starts at `joined_on`).
