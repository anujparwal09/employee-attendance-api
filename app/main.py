import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Environment variables supplied by the operating system take precedence
# over values in the local .env file.
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# tz_aware=True makes MongoDB return timezone-aware UTC datetimes, so they
# can be compared safely with the aware datetimes created in this file.
client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=5000,
    tz_aware=True,
    tzinfo=UTC,
)
db = client[MONGO_DB]

PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
PRESENT_STATUS_LIST = sorted(PRESENT_STATUSES)
ALL_STATUSES = PRESENT_STATUSES | {"ABSENT", "LEAVE"}

MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000


def create_indexes():
    # Enforce unique employee codes.
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )

    # Support department headcount lookups and join-date filtering.
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employee_department_joined_on",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="ix_employee_joined_on",
    )

    # Enforce one attendance record per employee per date.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )

    # Support attendance filtering and date sorting.
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_employee_date",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# =========================
# HELPERS
# =========================

def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "Date must use YYYY-MM-DD format")


def parse_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
        if parsed.tzinfo is not None or len(value) != 5:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(422, "Time must use HH:MM format")


def truncate_seconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def epoch_ms_to_utc(value: int) -> datetime:
    try:
        # Avoid accepting booleans as timestamps.
        if isinstance(value, bool):
            raise ValueError
        return truncate_seconds(datetime.fromtimestamp(value / 1000, tz=UTC))
    except (OverflowError, OSError, ValueError, TypeError):
        raise HTTPException(422, "Invalid epoch-millisecond timestamp")


def now_utc() -> datetime:
    return truncate_seconds(datetime.now(UTC))


def as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = as_ist(punch_in)
    start = parse_time(shift_start)
    end = parse_time(shift_end)

    # An overnight shift's early-morning punch belongs to the prior
    # shift-start date.
    if end <= start and local.time().replace(tzinfo=None) < end:
        local_date = local.date() - timedelta(days=1)
    else:
        local_date = local.date()

    return local_date.isoformat()


def shift_start_datetime(date_str: str, shift_start: str) -> datetime:
    day = parse_date(date_str)
    clock = parse_time(shift_start)
    return datetime.combine(day, clock, tzinfo=IST)


def shift_end_datetime(
    date_str: str, shift_start: str, shift_end: str
) -> datetime:
    day = parse_date(date_str)
    start = parse_time(shift_start)
    end = parse_time(shift_end)

    if end <= start:
        day += timedelta(days=1)

    return datetime.combine(day, end, tzinfo=IST)


def compute_late_minutes(
    punch_in: datetime, shift_start: str, date_str: str
) -> int:
    local_punch = as_ist(truncate_seconds(punch_in))
    scheduled_start = shift_start_datetime(date_str, shift_start)
    delay_seconds = int((local_punch - scheduled_start).total_seconds())

    # Strictly more than 10 minutes is late; count from shift start.
    if delay_seconds <= 10 * 60:
        return 0
    return delay_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = seconds / Decimal("3600")
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> int:
    local_out = as_ist(truncate_seconds(punch_out))
    scheduled_end = shift_end_datetime(date_str, shift_start, shift_end)
    extra_seconds = int((local_out - scheduled_end).total_seconds())

    # Overtime starts at 30 full minutes after scheduled shift end.
    if extra_seconds < 30 * 60:
        return 0
    return extra_seconds // 60


def clean_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    return doc


def clean_attendance(doc: dict) -> dict:
    # Convert a MongoDB attendance document to the API response format.
    return phase3_attendance_response(doc)


def _check_time_value(value: str) -> str:
    # Validators must raise ValueError (not HTTPException) for Pydantic.
    try:
        parse_time(value)
    except HTTPException:
        raise ValueError("Time must use HH:MM format")
    return value


def _check_date_value(value: str) -> str:
    try:
        parse_date(value)
    except HTTPException:
        raise ValueError("Date must use YYYY-MM-DD format")
    return value


def _check_epoch_ms(value):
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Timestamp must be epoch milliseconds")
        if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
            raise ValueError("Timestamp is outside the allowed epoch-ms range")
    return value


# =========================
# MODELS
# =========================

class EmployeeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    emp_code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    email: str = Field(min_length=1)
    department: str = Field(min_length=1)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        return _check_time_value(value)

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        return _check_date_value(value)


class PunchInIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value):
        return _check_epoch_ms(value)


class PunchOutIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value):
        return _check_epoch_ms(value)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamps(cls, value):
        return _check_epoch_ms(value)


# =========================
# PHASE 3 HELPERS
# =========================

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def to_epoch_ms(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    # Exact integer arithmetic (no float rounding).
    return (value - _EPOCH) // timedelta(milliseconds=1)


def serialize_history(history):
    result = []
    for entry in history or []:
        changes = {}
        for field_name, change in entry.get("changes", {}).items():
            old_value = change.get("from")
            new_value = change.get("to")
            if field_name in ("punch_in", "punch_out"):
                old_value = to_epoch_ms(old_value)
                new_value = to_epoch_ms(new_value)
            changes[field_name] = {"from": old_value, "to": new_value}

        result.append({
            "at": to_epoch_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })
    return result


def phase3_attendance_response(doc):
    if not doc:
        return doc
    result = {k: v for k, v in doc.items() if k != "_id"}
    result["punch_in"] = to_epoch_ms(result.get("punch_in"))
    result["punch_out"] = to_epoch_ms(result.get("punch_out"))
    result["history"] = serialize_history(result.get("history", []))
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    return result


def month_bounds(month):
    if len(month) != 7 or month[4] != "-":
        raise HTTPException(422, "month must use YYYY-MM format")
    try:
        start = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(422, "month must use YYYY-MM format")

    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start, end


def working_days_between(start, end_exclusive):
    count = 0
    current = start
    while current < end_exclusive:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def round_number(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def parse_request_timestamp(value):
    if value is None:
        return now_utc()
    return epoch_ms_to_utc(value)


def to_jsonable(value):
    # Explain output can contain BSON types; convert to plain JSON.
    return json.loads(json_util.dumps(value))


# =========================
# SHARED QUERY / PIPELINE BUILDERS
# The live endpoints and /admin/explain both use these, so the explained
# query is always the same as the one the endpoint really runs.
# =========================

ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]

# $dayOfWeek: 1 = Sunday, 2 = Monday ... 7 = Saturday.
# The stored date is a plain YYYY-MM-DD string, parsed as UTC midnight, which
# gives the correct weekday for that calendar date.
WEEKDAY_EXPR = {
    "$dayOfWeek": {
        "$dateFromString": {
            "dateString": "$date",
            "format": "%Y-%m-%d",
        }
    }
}
IS_PRESENT_STATUS = {"$in": ["$status", PRESENT_STATUS_LIST]}
IS_WEEKDAY = {"$and": [
    {"$gte": [WEEKDAY_EXPR, 2]},
    {"$lte": [WEEKDAY_EXPR, 6]},
]}
DAY_CREDIT = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}

# Present days that fall on Monday-Friday only (half days count 0.5).
WEEKDAY_PRESENT_DAYS = {"$sum": {
    "$cond": [{"$and": [IS_PRESENT_STATUS, IS_WEEKDAY]}, DAY_CREDIT, 0]
}}


def half_up_expr(value_expr, places=4):
    # MongoDB's $round rounds ties to even; this rounds halves upward.
    # Only used for non-negative values (rates and averages).
    factor = 10 ** places
    return {"$divide": [
        {"$floor": {"$add": [{"$multiply": [value_expr, factor]}, 0.5]}},
        factor,
    ]}


def build_attendance_query(emp_code, date_from, date_to, status):
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code

    start = parse_date(date_from) if date_from is not None else None
    end = parse_date(date_to) if date_to is not None else None
    if start is not None and end is not None and start > end:
        raise HTTPException(422, "date_from must not be after date_to")

    if start is not None or end is not None:
        query["date"] = {}
        if start is not None:
            query["date"]["$gte"] = start.isoformat()
        if end is not None:
            query["date"]["$lte"] = end.isoformat()

    if status is not None:
        query["status"] = status
    return query


def require_employee(emp_code: str) -> dict:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "Employee not found")
    return employee


def parse_trend_range(from_date: str, to_date: str, department: str):
    start = parse_date(from_date)
    end = parse_date(to_date)
    if end < start:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}) is None:
        raise HTTPException(404, "Department not found")
    return start, end


def build_employee_monthly_pipeline(emp_code: str, start: date, end: date):
    return [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": start.isoformat(), "$lt": end.isoformat()},
        }},
        {"$group": {
            "_id": None,
            "present_days": WEEKDAY_PRESENT_DAYS,
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


def build_department_summary_pipeline(start: date, end: date, department=None):
    # Headcount: employees who joined before the month ended.
    employee_match = {"joined_on": {"$lt": end.isoformat()}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lt": end.isoformat()}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    work_hours_ok = {"$and": [
        IS_PRESENT_STATUS,
        {"$ne": ["$work_hours", None]},
    ]}

    pipeline.extend([
        # Attendance statistics per department.
        {"$group": {
            "_id": "$employee.department",
            "present_days": WEEKDAY_PRESENT_DAYS,
            "work_hours_sum": {"$sum": {
                "$cond": [work_hours_ok, "$work_hours", 0]
            }},
            "work_hours_count": {"$sum": {
                "$cond": [work_hours_ok, 1, 0]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
        # Headcount per department, computed by MongoDB. This also adds
        # departments that have employees but no attendance logs.
        {"$unionWith": {
            "coll": "employees",
            "pipeline": [
                {"$match": employee_match},
                {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
            ],
        }},
        # Merge the attendance rows and headcount rows per department.
        {"$group": {
            "_id": "$_id",
            "headcount": {"$sum": "$headcount"},
            "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
    ])
    return pipeline


def build_late_leaderboard_pipeline(
    start: date, end: date, limit: int, department=None
):
    pipeline = [
        {"$match": {
            "date": {"$gte": start.isoformat(), "$lt": end.isoformat()},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"rank": 1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    return pipeline


def build_department_trend_pipeline(department: str, start: date, end: date):
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    end_exclusive = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)

    # Day filling and the moving average are performed by MongoDB.
    return [
        {"$match": {
            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {
            "_id": {"$dateFromString": {"dateString": "$date"}},
            "present_count": {"$sum": {
                "$cond": [IS_PRESENT_STATUS, DAY_CREDIT, 0]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "day": "$_id",
            "present_count": 1,
            "late_count": 1,
        }},
        {"$densify": {
            "field": "day",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [start_dt, end_exclusive],
            },
        }},
        {"$set": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            # $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Mon-Fri = 2..6.
            "is_working_day": {
                "$and": [
                    {"$gte": [{"$dayOfWeek": "$day"}, 2]},
                    {"$lte": [{"$dayOfWeek": "$day"}, 6]},
                ]
            },
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day_string": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", department]},
                    {"$lte": ["$joined_on", "$$day_string"]},
                ]}}},
                {"$count": "count"},
            ],
            "as": "headcount_data",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_data.count", 0]}, 0]},
        }},
        {"$set": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    half_up_expr({"$divide": ["$present_count", "$headcount"]}),
                    None,
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                }
            },
        }},
        {"$set": {
            "moving_avg_7d": {
                "$cond": [
                    {"$isNumber": "$moving_avg_7d"},
                    half_up_expr("$moving_avg_7d"),
                    None,
                ]
            }
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]


# =========================
# PHASE 1 / 2 ENDPOINTS
# =========================

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB is unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    # Stored as a BSON datetime; the API response uses epoch milliseconds.
    response = clean_employee(doc.copy())
    response["created_at"] = to_epoch_ms(response["created_at"])
    return response


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    ts = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else now_utc()
    record_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts, emp["shift_start"], record_date
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this attendance date")
    return clean_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    items = [
        phase3_attendance_response(item)
        for item in db.attendance_logs.find(query, {"_id": 0})
        .sort(ATTENDANCE_SORT)
        .skip(skip)
        .limit(page_size)
    ]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# =========================
# PHASE 3 — ENDPOINTS
# =========================

# 1. POST /attendance/punch-out
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "Employee not found")
    punched_at = parse_request_timestamp(body.punched_at)

    # Find the most recent open attendance record whose punch-in is
    # not later than the requested punch-out time.
    # ("punch_out": None also matches records where the field is missing.)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punched_at},
            "punch_out": None,
            "status": {"$in": list(PRESENT_STATUSES)},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        # Distinguish no eligible record from a record already closed.
        previous = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": punched_at},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if previous is not None and previous.get("punch_out") is not None:
            raise HTTPException(409, "Attendance record is already punched out")
        raise HTTPException(404, "No open punch-in found")
    if punched_at <= record["punch_in"]:
        raise HTTPException(422, "punched_at must be after punch-in")
    if punched_at - record["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "Punch-out cannot be more than 24 hours after punch-in")
    work_hours = compute_work_hours(record["punch_in"], punched_at)
    overtime = compute_overtime(
        punched_at,
        employee["shift_start"],
        employee["shift_end"],
        record["date"],
    )
    half_day = work_hours < 4.50

    # Conditional update means simultaneous punch-outs cannot both win.
    # The filter also re-checks punch_in and status so a record that was
    # regularized after it was read is not overwritten.
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_in": record["punch_in"],
            "punch_out": None,
            "status": {"$in": list(PRESENT_STATUSES)},
        },
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Attendance record is already punched out")
    return phase3_attendance_response(updated)


# 2. PATCH /attendance/{emp_code}/{date_str}
@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeIn):
    record_date = parse_date(date_str).isoformat()
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "Employee not found")
    old = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": record_date}
    )
    if old is None:
        raise HTTPException(404, "Attendance record not found")
    original = {
        "status": old.get("status"),
        "punch_in": old.get("punch_in"),
        "punch_out": old.get("punch_out"),
        "work_hours": old.get("work_hours"),
        "late_minutes": old.get("late_minutes", 0),
        "overtime_minutes": old.get("overtime_minutes", 0),
        "half_day": old.get("half_day", False),
    }
    final_status = body.status if body.status is not None else original["status"]

    # An omitted field means "leave unchanged".
    # An explicitly supplied null means "clear this value".
    final_in = original["punch_in"]
    if "punch_in" in body.model_fields_set:
        final_in = (
            epoch_ms_to_utc(body.punch_in)
            if body.punch_in is not None
            else None
        )

    final_out = original["punch_out"]
    if "punch_out" in body.model_fields_set:
        final_out = (
            epoch_ms_to_utc(body.punch_out)
            if body.punch_out is not None
            else None
        )

    # ABSENT and LEAVE must not have punch times.
    if final_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        final_in = None
        final_out = None
    elif final_in is None:
        raise HTTPException(422, "Presence statuses require a punch-in")
    if final_in is not None:
        calculated_date = attendance_date(
            final_in, employee["shift_start"], employee["shift_end"]
        )
        if calculated_date != record_date:
            raise HTTPException(422, "punch_in must belong to the record's attendance date")
    if final_out is not None:
        if final_in is None or final_out <= final_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if final_out - final_in > timedelta(hours=24):
            raise HTTPException(422, "Punch-out cannot be more than 24 hours after punch-in")
    if final_in is not None:
        late = compute_late_minutes(
            final_in, employee["shift_start"], record_date
        )
    else:
        late = 0
    if final_in is not None and final_out is not None:
        hours = compute_work_hours(final_in, final_out)
        overtime = compute_overtime(
            final_out,
            employee["shift_start"],
            employee["shift_end"],
            record_date,
        )
        half_day = hours < 4.50
    else:
        hours = None
        overtime = 0
        half_day = False
    final_values = {
        "status": final_status,
        "punch_in": final_in,
        "punch_out": final_out,
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }
    changes = {}
    for field_name, new_value in final_values.items():
        old_value = original[field_name]
        if old_value != new_value:
            changes[field_name] = {
                "from": old_value,
                "to": new_value,
            }
    if not changes:
        raise HTTPException(422, "The request does not change this record")
    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Compare-and-set guards against silently overwriting concurrent edits.
    query = {
        "emp_code": emp_code,
        "date": record_date,
        "status": old.get("status"),
        "punch_in": old.get("punch_in"),
        "punch_out": old.get("punch_out"),
    }
    if "history" in old:
        query["history"] = old["history"]
    else:
        query["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        query,
        {
            "$set": final_values,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Attendance record changed concurrently; retry")
    return phase3_attendance_response(updated)


# 3. GET /analytics/employees/{emp_code}/monthly
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = month_bounds(month)
    employee = require_employee(emp_code)

    joined_on = parse_date(employee["joined_on"])
    effective_start = max(start, joined_on)
    working_days = (
        working_days_between(effective_start, end)
        if effective_start < end else 0
    )

    pipeline = build_employee_monthly_pipeline(emp_code, start, end)
    result = list(db.attendance_logs.aggregate(pipeline))
    stats = result[0] if result else {}

    present_days = round_number(stats.get("present_days", 0))
    attendance_pct = (
        round_number(present_days / working_days * 100)
        if working_days else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


# 4. GET /analytics/departments/summary
@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = month_bounds(month)
    pipeline = build_department_summary_pipeline(start, end, department)

    items = []
    for row in db.attendance_logs.aggregate(pipeline):
        count = row.get("work_hours_count", 0)
        average = (
            round_number(row.get("work_hours_sum", 0) / count)
            if count else None
        )
        items.append({
            "department": row["_id"],
            "headcount": row.get("headcount", 0),
            "present_days": round_number(row.get("present_days", 0)),
            "avg_work_hours": average,
            "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0),
            "leave_count": row.get("leave_count", 0),
            "on_duty_count": row.get("on_duty_count", 0),
        })
    return {"month": month, "items": items}


# 5. GET /analytics/leaderboard/late
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    pipeline = build_late_leaderboard_pipeline(start, end, limit, department)
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


# 6. GET /analytics/departments/{department}/trend
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start, end = parse_trend_range(from_date, to_date, department)
    pipeline = build_department_trend_pipeline(department, start, end)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


# 7. GET /admin/explain/{endpoint}
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    def run_explain(command: dict):
        result = db.command({"explain": command, "verbosity": "executionStats"})
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": to_jsonable(result),
        }

    def explain_pipeline(pipeline: list):
        return run_explain({
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        })

    if endpoint == "attendance_list":
        query = build_attendance_query(emp_code, date_from, date_to, status)
        return run_explain({
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        })

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        trend_start, trend_end = parse_trend_range(from_date, to_date, department)
        return explain_pipeline(
            build_department_trend_pipeline(department, trend_start, trend_end)
        )

    # The remaining endpoints all need a month.
    if month is None:
        raise HTTPException(422, "month is required for this endpoint")
    start, end = month_bounds(month)

    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required")
        require_employee(emp_code)
        return explain_pipeline(build_employee_monthly_pipeline(emp_code, start, end))

    if endpoint == "department_summary":
        return explain_pipeline(build_department_summary_pipeline(start, end, department))

    # late_leaderboard
    return explain_pipeline(build_late_leaderboard_pipeline(start, end, limit, department))