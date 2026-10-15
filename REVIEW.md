# REVIEW.md

Defects found in the starter `app/main.py`, and how each was fixed.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Works on the UTC/naive datetime, not IST: `punch_in.replace(hour=...)` puts the shift start on the UTC clock, so a 09:28 IST punch-in is compared against 09:30 **UTC**. | Punch-in 09:28 IST gives ~5 h late. | Convert to IST, build the shift start on the attendance date in IST. |
| 2 | `compute_late_minutes` | `minutes > 10` after `int()` is not "strictly more than 10:00". 09:40:01 gives int(10.01)=10, not > 10, so it returns 0 (should be 10). Also ignores sub-second truncation and overnight shifts. | Shift 09:30, punch-in 09:40:01 returns 0. | Compare **seconds** `> 600`, then floor to minutes since shift start (R2). Truncate to whole seconds first (R1). |
| 3 | `compute_work_hours` | Float `round()` (banker's rounding on binary floats), not half-up. Does not truncate to seconds. | 27 s of work, or any exact .xx5 case, rounds wrongly. | Integer seconds, `Decimal` quantize with `ROUND_HALF_UP` (R4). |
| 4 | `compute_overtime` | No 30-minute minimum (R3). Shift end is always put on the punch date, so overnight shifts (22:00-06:00) are wrong. `replace(tzinfo=IST)` on a date parsed from a string, then subtracts a naive `punch_out` (naive minus aware raises TypeError, a 500). | Shift 09:30-18:30, out 18:45 returns 15 instead of 0. Overnight shift returns huge negative/zero. | Aware datetimes everywhere; shift end falls on the next day for overnight shifts; threshold 30; floor minutes. |
| 5 | `half_day` / R5 | Starter never computes `half_day` at punch-out (no punch-out endpoint at all). | Half-day never set. | Computed from the **rounded** work_hours `< 4.50`. |
| 6 | Module level: `MongoClient(...)` | No `tz_aware=True`, so every datetime read from MongoDB is naive; mixing naive and aware values breaks. | TypeError when subtracting. | `tz_aware=True`, plus `aware()` helper. |
| 7 | `/health` | Always returns ok, even if MongoDB is down. | Stop MongoDB, still 200. | Pings MongoDB, 503 on failure. |
| 8 | `create_employee` | Check-then-insert race and no unique index: two parallel creates both get 201. | Parallel duplicate POSTs. | Unique index on `emp_code`; catch `DuplicateKeyError` -> 409. |
| 9 | `create_employee` | `datetime.now()` is naive local time, and the response returns a datetime (ISO string) instead of epoch ms. | `created_at` is a string in the response. | UTC time truncated to ms, returned as epoch ms. |
| 10 | `EmployeeIn` | No validation: any `emp_code`, bad email, `shift_start == shift_end`, invalid times such as `25:99`, invalid `joined_on`. | POST with `"shift_start":"25:99"` returns 201. | Pattern/length constraints and a validator, all 422. |
| 11 | `list_employees` | `skip = page * page_size` (off by one, page 1 skips the first page). `total` counts the whole collection, ignoring the department filter. No sort, so pages are unstable. `page=0`/`page_size=1000000` unvalidated (negative skip is a 500). | Page 1 misses the first 20 employees. | `(page-1)*page_size`, `count_documents(q)`, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 12 | `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises TypeError -> 500 instead of 404. | POST with unknown emp_code. | 404. |
| 13 | `punch_in` | `datetime.fromtimestamp(ms/1000)` is local server time (naive), and the date comes from it, not IST. No R1 (overnight shifts). Sub-second not truncated. `if body.punched_at` treats a value of 0 as missing. | Server in UTC: a 02:00 IST punch-in lands on the previous date. | `ms_to_dt` (UTC, aware, seconds), `attendance_date()` per R1. |
| 14 | `punch_in` | `punched_at` accepts seconds, floats, bools; `status` is any string (`ABSENT` accepted, even `"banana"`). | `punched_at: 1783312500` (seconds) accepted. | Strict int with range check; `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 15 | `punch_in` | Check-then-insert race, no unique index. | Two parallel requests both get 201. | Unique `(emp_code, date)` index; `DuplicateKeyError` -> 409. |
| 16 | `punch_in` response | Returns `id` (the ObjectId) and datetimes. `_id` must never leave the API; instants must be epoch ms. | Response contains `id`. | `ser_record()` for every response. |
| 17 | `list_attendance` | Loads the whole matching collection into memory and sorts in Python; no index; will not survive 100k records. | Slow, memory heavy, COLLSCAN. | Server-side sort/skip/limit on indexed queries, with hints. |
| 18 | `list_attendance` | Sorts by `date` only (no `emp_code` tie-break); `page=0` gives a negative slice; dates not validated; `date_from > date_to` not rejected; returns `id` and raw datetimes. | `page=0`. | Sort `date desc, emp_code asc`; validation -> 422; `ser_record`. |
| 19 | `list_attendance` | `status` filter accepts any string. | `status=banana` returns 200 with nothing. | `Literal` Status -> 422. |

**Looked at and decided it is fine:** `IST = timezone(timedelta(hours=5, minutes=30))` is correct (IST has no DST). `load_dotenv()` does not override real environment variables, which is what the contract wants. Status codes 201/409 for create and duplicate are right. `doc.pop("_id")` after `insert_one` is correct (PyMongo adds `_id` to the dict), though building the response through a serialiser is cleaner.
