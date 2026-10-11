"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""

import os
import re
import time
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.collection import ReturnDocument


load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
MAX_EPOCH_MS = 4102444800000
MIN_EPOCH_MS = 100000000000

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# ---------------------------------------------------------------------------
# MongoDB indexes
# ---------------------------------------------------------------------------

@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_employee_department_code",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="idx_employee_joined_department",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="idx_attendance_employee_date_status",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", ASCENDING)],
        name="idx_attendance_status_date",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_attendance_employee_punchin",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_late_code",
    )


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "Timestamp must be an integer in epoch milliseconds")
    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise HTTPException(422, "Timestamp must be a valid epoch-millisecond value")
    return datetime.fromtimestamp(value // 1000, UTC).replace(microsecond=0)


def datetime_to_epoch_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def serialize(value):
    """Convert MongoDB values into JSON-safe API values."""
    if isinstance(value, datetime):
        return datetime_to_epoch_ms(value)
    if isinstance(value, dict):
        return {
            key: serialize(item)
            for key, item in value.items()
            if key != "_id"
        }
    if isinstance(value, list):
        return [serialize(item) for item in value]
    return value


def parse_date(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(422, f"{field_name} must use YYYY-MM-DD format")
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field_name} must use YYYY-MM-DD format")
    return parsed


def validate_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-\d{2}", value or ""):
        raise HTTPException(422, "month must use YYYY-MM format")
    try:
        year, month = map(int, value.split("-"))
        first = date(year, month, 1)
    except ValueError:
        raise HTTPException(422, "month must be a valid YYYY-MM value")
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def month_date_strings(month: str) -> tuple[str, str]:
    first, last = validate_month(month)
    return first.isoformat(), last.isoformat()


def round_half_up(value, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def require_employee(emp_code: str) -> dict:
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")
    return employee


def shift_bounds(attendance_day: date, shift_start: str, shift_end: str):
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    start = datetime(
        attendance_day.year,
        attendance_day.month,
        attendance_day.day,
        start_hour,
        start_minute,
        tzinfo=IST,
    )
    end = datetime(
        attendance_day.year,
        attendance_day.month,
        attendance_day.day,
        end_hour,
        end_minute,
        tzinfo=IST,
    )
    if shift_end <= shift_start:
        end += timedelta(days=1)
    return start, end


def attendance_date_for_punch(punched_at: datetime, employee: dict) -> date:
    local_time = punched_at.astimezone(IST)
    local_day = local_time.date()

    if employee["shift_end"] <= employee["shift_start"]:
        end_hour, end_minute = map(int, employee["shift_end"].split(":"))
        shift_end_minutes = end_hour * 60 + end_minute
        punch_minutes = local_time.hour * 60 + local_time.minute
        if punch_minutes < shift_end_minutes:
            return local_day - timedelta(days=1)

    return local_day


def compute_late_minutes(
    punch_in: Optional[datetime],
    shift_start: str,
    attendance_day: date,
) -> int:
    if punch_in is None:
        return 0

    shift_start_dt, _ = shift_bounds(
        attendance_day, shift_start, "00:00"
    )
    # shift_bounds above may roll the end for an overnight shift; only its
    # start value is used here.
    delta_seconds = int((punch_in - shift_start_dt.astimezone(UTC)).total_seconds())

    if delta_seconds <= 600:
        return 0
    return max(0, delta_seconds // 60)


def compute_derived(
    employee: dict,
    attendance_day: date,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
) -> dict:
    late_minutes = compute_late_minutes(
        punch_in, employee["shift_start"], attendance_day
    )

    work_hours = None
    overtime_minutes = 0
    half_day = False

    if punch_in is not None and punch_out is not None:
        seconds = (punch_out - punch_in).total_seconds()
        work_hours = round_half_up(seconds / 3600, 2)
        half_day = work_hours < 4.50

        _, shift_end_dt = shift_bounds(
            attendance_day, employee["shift_start"], employee["shift_end"]
        )
        overtime_seconds = (
            punch_out - shift_end_dt.astimezone(UTC)
        ).total_seconds()
        if overtime_seconds >= 1800:
            overtime_minutes = int(overtime_seconds // 60)

    return {
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }


def working_days_between(start: date, end: date) -> int:
    if start > end:
        return 0
    total = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)
    return total


def date_is_weekday(date_string: str) -> bool:
    return parse_date(date_string).weekday() < 5


def validate_status(status: str):
    if status not in ALL_STATUSES:
        raise HTTPException(
            422,
            f"status must be one of: {', '.join(ALL_STATUSES)}",
        )


def attendance_filter(
    emp_code=None,
    date_from=None,
    date_to=None,
    status=None,
):
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return query


def month_attendance_query(emp_code: str, first: str, last: str):
    return {
        "emp_code": emp_code,
        "date": {"$gte": first, "$lte": last},
    }


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("email must be a valid email address")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("shift times must use HH:MM format")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must use YYYY-MM-DD format")
        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")
        return value

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must be different")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value is not None:
            validate_status(value)
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value


# ---------------------------------------------------------------------------
# Health and employee endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")

    return serialize(doc)


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
    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance punch-in / punch-out / list
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    employee = require_employee(body.emp_code)
    punched_at = (
        epoch_ms_to_datetime(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )
    attendance_day = attendance_date_for_punch(punched_at, employee)
    date_string = attendance_day.isoformat()

    derived = compute_derived(
        employee, attendance_day, punched_at, None
    )
    doc = {
        "emp_code": body.emp_code,
        "date": date_string,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derived,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Attendance already exists for this date")
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")

    return serialize(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    employee = require_employee(body.emp_code)
    punched_at = (
        epoch_ms_to_datetime(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punched_at, "$ne": None},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, "No matching punch-in record found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Attendance record is already punched out")

    punch_in_value = record["punch_in"]
    if punched_at <= punch_in_value:
        raise HTTPException(422, "punch-out must be after punch-in")
    if punched_at > punch_in_value + timedelta(hours=24):
        raise HTTPException(422, "punch-out cannot be more than 24 hours after punch-in")

    derived = compute_derived(
        employee,
        parse_date(record["date"]),
        punch_in_value,
        punched_at,
    )

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_at,
                **derived,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(409, "Attendance record was updated concurrently")

    return serialize(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")
    if status:
        validate_status(status)

    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)

    docs = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance regularization
# ---------------------------------------------------------------------------

@app.patch("/attendance/{emp_code}/{attendance_date}")
def regularize_attendance(
    emp_code: str,
    attendance_date: str,
    body: RegularizeRequest,
):
    day = parse_date(attendance_date, "date")
    employee = require_employee(emp_code)

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": day.isoformat()}
    )
    if not record:
        raise HTTPException(404, "Attendance record not found")

    fields_set = body.model_fields_set
    new_status = body.status if "status" in fields_set else record["status"]
    new_punch_in = record.get("punch_in")
    new_punch_out = record.get("punch_out")

    if "punch_in" in fields_set:
        if body.punch_in is None:
            raise HTTPException(422, "punch_in must be an epoch-millisecond integer")
        new_punch_in = epoch_ms_to_datetime(body.punch_in)

    if "punch_out" in fields_set:
        if body.punch_out is None:
            raise HTTPException(422, "punch_out must be an epoch-millisecond integer")
        new_punch_out = epoch_ms_to_datetime(body.punch_out)

    if new_status in ("ABSENT", "LEAVE"):
        if (
            "punch_in" in fields_set and body.punch_in is not None
        ) or (
            "punch_out" in fields_set and body.punch_out is not None
        ):
            raise HTTPException(
                422,
                "ABSENT and LEAVE records cannot have punch timestamps",
            )
        new_punch_in = None
        new_punch_out = None
    elif new_punch_in is None:
        raise HTTPException(422, "Presence statuses require a punch_in timestamp")

    if new_punch_in is not None:
        calculated_day = attendance_date_for_punch(new_punch_in, employee)
        if calculated_day != day:
            raise HTTPException(
                422,
                "punch_in must belong to the attendance date under the employee's shift rules",
            )

    if new_punch_out is not None:
        if new_punch_in is None or new_punch_out <= new_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if new_punch_out > new_punch_in + timedelta(hours=24):
            raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    changes = {}
    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")

    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}

    if not changes:
        raise HTTPException(422, "Regularization request makes no changes")

    derived = compute_derived(
        employee, day, new_punch_in, new_punch_out
    )
    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    history = record.get("history", [])

    # Match the original values as well as history so two concurrent
    # regularizations cannot silently overwrite each other's history.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "status": old_status,
            "punch_in": old_punch_in,
            "punch_out": old_punch_out,
            "history": history,
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                **derived,
                "history": history + [history_entry],
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(409, "Attendance record changed concurrently")

    return serialize(updated)


# ---------------------------------------------------------------------------
# Employee monthly analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
):
    employee = require_employee(emp_code)
    first, last = validate_month(month)
    first_string, last_string = first.isoformat(), last.isoformat()

    joined_on = parse_date(employee["joined_on"], "joined_on")
    working_start = max(first, joined_on)
    working_days = working_days_between(working_start, last)

    records = list(
        db.attendance_logs.find(
            month_attendance_query(emp_code, first_string, last_string),
            {
                "_id": 0,
                "date": 1,
                "status": 1,
                "half_day": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
            },
        )
    )

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    for record in records:
        status_value = record.get("status")
        record_day = parse_date(record["date"])

        if status_value in PRESENT_STATUSES and record_day.weekday() < 5:
            present_days += 0.5 if record.get("half_day", False) else 1.0

        if status_value == "LEAVE":
            leave_days += 1

        late_minutes = record.get("late_minutes") or 0
        overtime_minutes = record.get("overtime_minutes") or 0
        if late_minutes > 0:
            late_count += 1
        total_late_minutes += late_minutes
        total_overtime_minutes += overtime_minutes

    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# ---------------------------------------------------------------------------
# Department summary analytics
# ---------------------------------------------------------------------------

def department_summary_pipeline(first_string, last_string, department=None):
    employee_match = {"joined_on": {"$lte": last_string}}
    if department is not None:
        employee_match["department"] = department

    present_statuses = list(PRESENT_STATUSES)

    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", first_string]},
                                    {"$lte": ["$date", last_string]},
                                ]
                            }
                        }
                    },
                    {
                        "$set": {
                            "_day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$log.status", present_statuses]},
                                            {"$in": ["$$log._day_of_week", [2, 3, 4, 5, 6]]},
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$ifNull": ["$$log.half_day", False]},
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "_work_hours_values": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$log.status", present_statuses]},
                                        {"$ne": ["$$log.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "log",
                        "in": "$$log.work_hours",
                    }
                },
                "_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "_work_hours_sum": {"$sum": "$_work_hours_values"},
                "_work_hours_count": {"$size": "$_work_hours_values"},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$_present_days"},
                "late_count": {"$sum": "$_late_count"},
                "total_late_minutes": {"$sum": "$_late_minutes"},
                "leave_count": {"$sum": "$_leave_count"},
                "on_duty_count": {"$sum": "$_on_duty_count"},
                "work_hours_sum": {"$sum": "$_work_hours_sum"},
                "work_hours_count": {"$sum": "$_work_hours_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$round": ["$present_days", 2]},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    department: Optional[str] = None,
):
    first, last = validate_month(month)
    first_string, last_string = first.isoformat(), last.isoformat()

    pipeline = department_summary_pipeline(
        first_string, last_string, department
    )
    results = list(db.employees.aggregate(pipeline))

    for item in results:
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
        item["present_days"] = round_half_up(item["present_days"], 2)

    return {"month": month, "items": results}


# ---------------------------------------------------------------------------
# Late leaderboard
# ---------------------------------------------------------------------------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1, le=100),
    department: Optional[str] = None,
):
    first, last = validate_month(month)
    first_string, last_string = first.isoformat(), last.isoformat()

    pipeline = [
        {
            "$match": {
                "date": {"$gte": first_string, "$lte": last_string},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "rank": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Department daily trend
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")

    if end < start:
        raise HTTPException(422, "to cannot be before from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "Department not found")

    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    end_exclusive = datetime(
        end.year, end.month, end.day, tzinfo=UTC
    ) + timedelta(days=1)

    pipeline = [
        {"$documents": [{"date": start_dt}]},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },
        {
            "$set": {
                "_date_string": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date"}
                },
                "_day_of_week": {"$dayOfWeek": "$date"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_string": "$_date_string"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_string"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "_headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$_headcount_result.count", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day_string": "$_date_string"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day_string"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "let": {"code": "$emp_code"},
                            "pipeline": [
                                {
                                    "$match": {
                                        "$expr": {
                                            "$and": [
                                                {"$eq": ["$emp_code", "$$code"]},
                                                {"$eq": ["$department", department]},
                                            ]
                                        }
                                    }
                                },
                                {"$project": {"_id": 0, "emp_code": 1}},
                            ],
                            "as": "_matching_employee",
                        }
                    },
                    {
                        "$match": {
                            "$expr": {
                                "$gt": [{"$size": "$_matching_employee"}, 0]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                        }
                    },
                ],
                "as": "_logs",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$log.status", list(PRESENT_STATUSES)]},
                                    {
                                        "$cond": [
                                            "$$log.half_day",
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {"$gt": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "is_working_day": {
                    "$in": ["$_day_of_week", [2, 3, 4, 5, 6]]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_date_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$round": ["$present_count", 2]},
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        {"$round": ["$moving_avg_7d", 4]},
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]

    try:
        rows = list(db.aggregate(pipeline))
    except PyMongoError as exc:
        raise HTTPException(
            500,
            "Unable to build department trend; check MongoDB version supports $documents, $densify, and $setWindowFields",
        ) from exc

    return {
        "department": department,
        "from": from_date,
        "to": to_date,
        "items": rows,
    }


# ---------------------------------------------------------------------------
# Explain plans for contract endpoints
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    month: Optional[str] = None,
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    attendance_collection = db.attendance_logs
    employees_collection = db.employees

    if endpoint == "attendance_list":
        if date_from:
            parse_date(date_from, "date_from")
        if date_to:
            parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be after date_to")
        if status:
            validate_status(status)

        query = attendance_filter(emp_code, date_from, date_to, status)
        explanation = attendance_collection.find(query).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).explain("executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explanation,
        }

    if endpoint == "employee_monthly":
        if not month or not emp_code:
            raise HTTPException(422, "month and emp_code are required")
        first, last = month_date_strings(month)
        require_employee(emp_code)
        query = month_attendance_query(emp_code, first, last)
        explanation = attendance_collection.find(query).explain("executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explanation,
        }

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        first, last = month_date_strings(month)
        query = {"date": {"$gte": first, "$lte": last}}
        explanation = attendance_collection.find(query).explain("executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explanation,
        }

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        first, last = month_date_strings(month)
        query = {
            "date": {"$gte": first, "$lte": last},
            "late_minutes": {"$gt": 0},
        }
        explanation = attendance_collection.find(query).explain("executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explanation,
        }

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required")
        start = parse_date(from_date, "from")
        end = parse_date(to_date, "to")
        if end < start:
            raise HTTPException(422, "to cannot be before from")
        if (end - start).days + 1 > 92:
            raise HTTPException(422, "Date range cannot exceed 92 days")
        query = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
        explanation = attendance_collection.find(query).explain("executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explanation,
        }

    raise HTTPException(
        422,
        "endpoint must be one of: attendance_list, employee_monthly, department_summary, late_leaderboard, department_trend",
    )