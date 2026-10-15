"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded; real environment variables win)

Everything lives in this one file:
  1. configuration + indexes
  2. time / rule helpers (R1 - R5, R8)
  3. request models and serialisation
  4. employees, attendance (punch-in / punch-out / list / regularize)
  5. analytics aggregation pipelines + /admin/explain
"""
import calendar
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()  # does not override variables that are already set in the environment
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration + indexes
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo returns timezone-aware (UTC) datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
ABSENT_LIKE = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60
OVERTIME_MIN_MINUTES = 30
HALF_DAY_BELOW = Decimal("4.50")

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent. Called at startup (and again from /health if MongoDB was not up yet)."""
    global _indexes_ready
    if _indexes_ready:
        return
    emp, logs = db.employees, db.attendance_logs
    # employees
    emp.create_index([("emp_code", 1)], unique=True, name="emp_code_unique")
    emp.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")
    emp.create_index([("department", 1), ("joined_on", 1)], name="dept_joined_on")
    emp.create_index([("joined_on", 1)], name="joined_on")
    # attendance_logs
    logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="emp_date_unique")
    logs.create_index([("date", -1), ("emp_code", 1)], name="date_desc_emp")
    logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp")
    logs.create_index([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception:  # MongoDB may still be starting; /health retries
        log.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Helpers: time, validation, business rules
# --------------------------------------------------------------------------- #
def unprocessable(name: str, msg: str, where: str = "query") -> HTTPException:
    return HTTPException(422, detail=[{"loc": [where, name], "msg": msg, "type": "value_error"}])


def aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def trunc_sec(dt: datetime) -> datetime:
    """R1: instants are truncated to whole seconds before any calculation."""
    return aware(dt).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    return trunc_sec(EPOCH + timedelta(milliseconds=ms))


def dt_to_ms(dt: datetime) -> int:
    return (aware(dt) - EPOCH) // timedelta(milliseconds=1)


def now_dt() -> datetime:
    return trunc_sec(datetime.now(UTC))


def parse_date(value: str, name: str, where: str = "query") -> Date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise unprocessable(name, "date must be YYYY-MM-DD", where)
    try:
        return Date(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        raise unprocessable(name, "invalid calendar date", where)


MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def month_range(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    if not re.fullmatch(MONTH_PATTERN, month or "") or int(month[:4]) < 1:
        raise unprocessable("month", "month must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def round_half_up(value: Decimal | float | int, places: int) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hm(shift_end) <= hm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST calendar date of the punch-in; an overnight-shift punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    ist = aware(punch_in).astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and (ist.hour, ist.minute) < hm(shift_end):
        d -= timedelta(days=1)
    return d


def shift_start_dt(d: Date, shift_start: str) -> datetime:
    h, m = hm(shift_start)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_end_dt(d: Date, shift_start: str, shift_end: str) -> datetime:
    h, m = hm(shift_end)
    end = datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


def compute_late_minutes(punch_in: datetime, d: Date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    secs = int((trunc_sec(punch_in) - shift_start_dt(d, shift_start)).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, d: Date, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift end, counted only when >= 30."""
    secs = int((trunc_sec(punch_out) - shift_end_dt(d, shift_start, shift_end)).total_seconds())
    minutes = secs // 60 if secs > 0 else 0
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded half-up to 2 decimals (Decimal, not float round())."""
    secs = int((trunc_sec(punch_out) - trunc_sec(punch_in)).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_half_day(work_hours: float) -> bool:
    """R5: based on the already rounded work_hours."""
    return Decimal(str(work_hours)) < HALF_DAY_BELOW


def working_days(month: str, joined_on: str) -> int:
    """R7: Mon-Fri in the month, counted from joined_on for a mid-month joiner."""
    first, last = month_range(month)
    start = max(first, joined_on)
    if start > last:
        return 0
    s, e = Date.fromisoformat(start), Date.fromisoformat(last)
    return sum(1 for i in range((e - s).days + 1) if (s + timedelta(days=i)).weekday() < 5)


# --------------------------------------------------------------------------- #
# 3. Models + serialisation
# --------------------------------------------------------------------------- #
# Strict int: floats, strings and booleans are rejected. Range rejects seconds-looking values.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
SHIFT_RE = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=SHIFT_RE)
    shift_end: str = Field(default="18:30", pattern=SHIFT_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        Date(int(v[:4]), int(v[5:7]), int(v[8:10]))  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def ser_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]) if doc.get("created_at") else None,
    }


def _ms_or_none(v: Any) -> Any:
    return dt_to_ms(v) if isinstance(v, datetime) else v


def ser_history(entry: dict) -> dict:
    changes = {}
    for field, ch in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):
            changes[field] = {"from": _ms_or_none(ch.get("from")), "to": _ms_or_none(ch.get("to"))}
        else:
            changes[field] = {"from": ch.get("from"), "to": ch.get("to")}
    return {"at": dt_to_ms(entry["at"]), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def ser_record(doc: dict) -> dict:
    """Never exposes _id. Missing legacy fields get their documented defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms_or_none(doc.get("punch_in")),
        "punch_out": _ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [ser_history(h) for h in (doc.get("history") or [])],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4a. System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()
    except Exception:
        raise HTTPException(503, "mongodb unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = ms_to_dt(int(time.time() * 1000))
    try:  # the unique index on emp_code is the guarantee, not a find_one beforehand
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return ser_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    cur = db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [ser_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 4b. Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:  # unique (emp_code, date) index: exactly one concurrent request wins
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return ser_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    out = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out}}, sort=[("punch_in", -1)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = trunc_sec(rec["punch_in"])
    if out <= pin:
        raise unprocessable("punched_at", "punched_at must be after punch_in", "body")
    if out - pin > timedelta(hours=24):
        raise unprocessable("punched_at", "punch_out is more than 24 hours after punch_in", "body")
    d = Date.fromisoformat(rec["date"])
    work_hours = compute_work_hours(pin, out)
    updated = db.attendance_logs.find_one_and_update(
        # the punch_out: None guard makes a second simultaneous punch-out match nothing
        {"emp_code": body.emp_code, "date": rec["date"], "punch_out": None},
        {
            "$set": {
                "punch_in": pin,
                "punch_out": out,
                "work_hours": work_hours,
                "overtime_minutes": compute_overtime(out, d, emp["shift_start"], emp["shift_end"]),
                "half_day": is_half_day(work_hours),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return ser_record(updated)


def build_attendance_query(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
    status: Optional[str], page: int, page_size: int,
) -> dict:
    """Shared by GET /attendance and its explain, so the explained query is the real one."""
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    lo = parse_date(date_from, "date_from") if date_from else None
    hi = parse_date(date_to, "date_to") if date_to else None
    if lo and hi and lo > hi:
        raise unprocessable("date_from", "date_from must not be after date_to")
    if lo or hi:
        q["date"] = {}
        if lo:
            q["date"]["$gte"] = lo.isoformat()
        if hi:
            q["date"]["$lte"] = hi.isoformat()
    if status:
        q["status"] = status
    if emp_code:
        hint = "emp_date_unique"
    elif status:
        hint = "status_date_emp"
    else:
        hint = "date_desc_emp"
    return {
        "filter": q,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
        "hint": hint,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    coll = db.attendance_logs
    total = coll.count_documents(spec["filter"], hint=spec["hint"])
    cur = (
        coll.find(spec["filter"])
        .hint(spec["hint"])
        .sort(list(spec["sort"].items()))
        .skip(spec["skip"])
        .limit(spec["limit"])
    )
    return {"items": [ser_record(d) for d in cur], "total": total, "page": page, "page_size": page_size}


TRACKED = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    d = parse_date(date, "date", "path")
    emp = get_employee_or_404(emp_code)
    logs = db.attendance_logs

    for _ in range(5):  # optimistic concurrency: retry if somebody else wrote in between
        cur = logs.find_one({"emp_code": emp_code, "date": date})
        if not cur:
            raise HTTPException(404, "no attendance record for that date")
        old_in = trunc_sec(cur["punch_in"]) if cur.get("punch_in") else None
        old_out = trunc_sec(cur["punch_out"]) if cur.get("punch_out") else None
        history_len = len(cur.get("history") or [])
        new_status = body.status or cur["status"]

        if new_status in ABSENT_LIKE:
            if body.punch_in is not None or body.punch_out is not None:
                raise unprocessable("punch_in", "ABSENT / LEAVE cannot carry punch times", "body")
            new_in = new_out = None
        else:
            new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
            new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
            if new_in is None:
                raise unprocessable("punch_in", "a presence status requires a punch_in", "body")
            if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != d:
                raise unprocessable("punch_in", "punch_in must stay on the record's attendance date", "body")
            if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
                raise unprocessable("punch_out", "punch_out must be after punch_in and within 24 hours", "body")

        if new_status in ABSENT_LIKE:
            derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
        else:
            late = compute_late_minutes(new_in, d, emp["shift_start"])
            if new_out is None:
                derived = {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
            else:
                wh = compute_work_hours(new_in, new_out)
                derived = {
                    "work_hours": wh,
                    "late_minutes": late,
                    "overtime_minutes": compute_overtime(new_out, d, emp["shift_start"], emp["shift_end"]),
                    "half_day": is_half_day(wh),
                }
        new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
        old_vals = {
            "status": cur["status"], "punch_in": old_in, "punch_out": old_out,
            "work_hours": cur.get("work_hours"),
            "late_minutes": cur.get("late_minutes") or 0,
            "overtime_minutes": cur.get("overtime_minutes") or 0,
            "half_day": bool(cur.get("half_day", False)),
        }
        # "changes nothing" is judged on what the client controls; stored derived values may be stale seed data
        if all(new_vals[k] == old_vals[k] for k in ("status", "punch_in", "punch_out")):
            raise unprocessable("body", "the request does not change anything", "body")

        entry = {
            "at": now_dt(),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": {k: {"from": old_vals[k], "to": new_vals[k]} for k in TRACKED if new_vals[k] != old_vals[k]},
        }
        updated = logs.find_one_and_update(
            {
                "emp_code": emp_code,
                "date": date,
                "status": cur["status"],
                "punch_in": cur.get("punch_in"),
                "punch_out": cur.get("punch_out"),
                "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
            },
            {"$set": new_vals, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated:
            return ser_record(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# 5. Analytics: pipelines are built by functions shared with /admin/explain
# --------------------------------------------------------------------------- #
def _presence() -> dict:
    return {"$in": ["$status", PRESENCE]}


def _weekday() -> dict:
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}


def _present_x2(weekday_only: bool) -> dict:
    """Present days times 2 (so everything stays an exact integer): full day 2, half day 1."""
    half = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
    base = {"$cond": [_presence(), {"$cond": [half, 1, 2]}, 0]}
    return {"$cond": [_weekday(), base, 0]} if weekday_only else base


def _late_flag() -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


def build_employee_monthly(emp_code: str, month: str) -> dict:
    first, last = month_range(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_x2": {"$sum": _present_x2(True)},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": _late_flag()},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]
    return {"collection": "attendance_logs", "pipeline": pipeline, "hint": "emp_date_unique"}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee_or_404(emp_code)
    spec = build_employee_monthly(emp_code, month)
    rows = list(db.attendance_logs.aggregate(spec["pipeline"], hint=spec["hint"]))
    r = rows[0] if rows else {}
    p2 = int(r.get("present_x2", 0))
    wd = working_days(month, emp["joined_on"])
    pct = None
    if wd > 0:  # attendance_pct = (p2/2)/wd*100, half-up to 2 decimals using integers only
        pct = ((p2 * 10000 + wd) // (2 * wd)) / 100
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": p2 / 2,
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


def build_department_summary(month: str, department: Optional[str]) -> dict:
    first, last = month_range(month)
    emp_match: dict = {"joined_on": {"$lte": last}}  # R9: joined on/before the end of the month
    if department:
        emp_match["department"] = department
    has_wh = {"$and": [_presence(), {"$isNumber": "$work_hours"}]}
    pipeline = [
        {"$match": emp_match},
        # Starting from employees (not from the logs) is what keeps people with zero logs in the headcount.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"c": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$emp_code", "$$c"]}, "date": {"$gte": first, "$lte": last}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_x2": {"$sum": _present_x2(True)},
                            "wh_sum100": {
                                "$sum": {
                                    "$cond": [has_wh, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]
                                }
                            },
                            "wh_n": {"$sum": {"$cond": [has_wh, 1, 0]}},
                            "late_count": {"$sum": _late_flag()},
                            "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                        }
                    },
                ],
                "as": "l",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{
                    k: {"$sum": {"$ifNull": [{"$arrayElemAt": [f"$l.{k}", 0]}, 0]}}
                    for k in ("present_x2", "wh_sum100", "wh_n", "late_count", "late_minutes", "leave_count", "on_duty_count")
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$divide": ["$present_x2", 2]},
                # mean over records, half-up to 2 decimals, using integer hundredths (no float ties)
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$wh_n", 0]},
                        {
                            "$divide": [
                                {"$floor": {"$divide": [{"$add": [{"$multiply": ["$wh_sum100", 2]}, "$wh_n"]}, {"$multiply": ["$wh_n", 2]}]}},
                                100,
                            ]
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": "$late_minutes",
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]
    hint = "dept_joined_on" if department else "joined_on"
    return {"collection": "employees", "pipeline": pipeline, "hint": hint}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    spec = build_department_summary(month, department)
    items = list(db.employees.aggregate(spec["pipeline"], hint=spec["hint"]))
    return {"month": month, "items": items}


def build_late_leaderboard(month: str, limit: int, department: Optional[str]) -> dict:
    first, last = month_range(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs whose employee does not exist disappear here
    ]
    if department:
        pipeline.append({"$match": {"e.department": department}})  # filter BEFORE ranking
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4); ties share a rank
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the rank, not to the row count
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return {"collection": "attendance_logs", "pipeline": pipeline, "hint": "date_desc_emp"}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    spec = build_late_leaderboard(month, limit, department)
    items = list(db.attendance_logs.aggregate(spec["pipeline"], hint=spec["hint"]))
    return {"month": month, "items": items}


def check_trend_range(frm: str, to: str) -> tuple[Date, Date]:
    f, t = parse_date(frm, "from"), parse_date(to, "to")
    if t < f:
        raise unprocessable("to", "to must not be before from")
    if (t - f).days + 1 > 92:
        raise unprocessable("to", "range must not exceed 92 days")
    return f, t


def build_department_trend(department: str, frm: str, to: str) -> dict:
    f, t = check_trend_range(frm, to)
    codes = db.employees.distinct("emp_code", {"department": department})
    f_dt = datetime(f.year, f.month, f.day, tzinfo=UTC)
    t_dt = datetime(t.year, t.month, t.day, tzinfo=UTC)
    win = {"documents": [-6, 0]}
    pipeline = [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": frm, "$lte": to}}},
        {"$group": {"_id": "$date", "p2": {"$sum": _present_x2(False)}, "late": {"$sum": _late_flag()}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
        # gap filling in the database: one document per calendar day (upper bound is exclusive)
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [f_dt, t_dt + timedelta(days=1)]}}},
        {"$match": {"d": {"$lte": t_dt}}},
        {
            "$addFields": {
                "ds": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "p2": {"$ifNull": ["$p2", 0]},
                "late": {"$ifNull": ["$late", 0]},
                "is_wd": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
            }
        },
        {  # R9 headcount for that day
            "$lookup": {
                "from": "employees",
                "let": {"day": "$ds"},
                "pipeline": [
                    {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {"$addFields": {"hc": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {
            # rate in 1e-4 units, half-up, integers only: floor((10000*p2 + hc) / (2*hc))
            "$addFields": {
                "rate_int": {
                    "$cond": [
                        {"$and": ["$is_wd", {"$gt": ["$hc", 0]}]},
                        {"$floor": {"$divide": [{"$add": [{"$multiply": ["$p2", 10000]}, "$hc"]}, {"$multiply": ["$hc", 2]}]}},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "rsum": {"$sum": "$rate_int", "window": win},
                    "rcnt": {"$sum": {"$cond": [{"$ne": ["$rate_int", None]}, 1, 0]}, "window": win},
                },
            }
        },
        {"$sort": {"d": 1}},
        {
            "$project": {
                "_id": 0,
                "date": "$ds",
                "is_working_day": "$is_wd",
                "headcount": "$hc",
                "present_count": {"$divide": ["$p2", 2]},
                "late_count": "$late",
                "attendance_rate": {"$cond": [{"$ne": ["$rate_int", None]}, {"$divide": ["$rate_int", 10000]}, None]},
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$rcnt", 0]},
                        {
                            "$divide": [
                                {"$floor": {"$divide": [{"$add": [{"$multiply": ["$rsum", 2]}, "$rcnt"]}, {"$multiply": ["$rcnt", 2]}]}},
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
    ]
    return {"collection": "attendance_logs", "pipeline": pipeline, "hint": "emp_date_unique", "codes": codes}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    check_trend_range(from_, to)  # 422 before 404
    spec = build_department_trend(department, from_, to)
    if not spec["codes"]:
        raise HTTPException(404, "unknown department")
    items = list(db.attendance_logs.aggregate(spec["pipeline"], hint=spec["hint"]))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 5b. /admin/explain
# --------------------------------------------------------------------------- #
def _required(value: Any, name: str) -> Any:
    if value is None or value == "":
        raise unprocessable(name, "field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        s = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        inner = {"find": coll, "filter": s["filter"], "sort": s["sort"], "skip": s["skip"], "limit": s["limit"], "hint": s["hint"]}
    else:
        if endpoint == "employee_monthly":
            s = build_employee_monthly(_required(emp_code, "emp_code"), _required(month, "month"))
        elif endpoint == "department_summary":
            s = build_department_summary(_required(month, "month"), department)
        elif endpoint == "late_leaderboard":
            s = build_late_leaderboard(_required(month, "month"), limit, department)
        else:
            s = build_department_trend(_required(department, "department"), _required(from_, "from"), _required(to, "to"))
        coll = s["collection"]
        inner = {"aggregate": coll, "pipeline": s["pipeline"], "cursor": {}, "hint": s["hint"]}
    result = db.command({"explain": inner, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(result))}
