# Starter Code Review

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes`, lines 33–37 | Incorrect time handling for overnight shifts and no explicit whole-second truncation before calculations. | Test punch-ins around the 10-minute grace boundary and overnight shifts. | Use IST-aware timestamps, truncate to whole seconds, and calculate lateness using the correct shift start. |
| 2 | `compute_work_hours`, lines 40–41 | Python `round()` does not guarantee the required half-up rounding. | Test durations at two-decimal rounding boundaries. | Use decimal half-up rounding. |
| 3 | `compute_overtime`, lines 44–47 | Always places shift end on the attendance date, which is wrong for overnight shifts. | Test a shift from 22:00 to 06:00. | Calculate shift end on the correct calendar day. |
| 4 | `compute_overtime`, lines 44–47 | Counts overtime without enforcing the minimum of 30 whole minutes. | Test punch-outs 29 and 30 minutes after shift end. | Return overtime only when elapsed whole minutes are at least 30. |
| 5 | `EmployeeIn` and `PunchInIn`, lines 53–67 | Missing validation for employee-code format, dates, shift times, timestamps, and allowed statuses. | Submit malformed employee details, invalid timestamps, or unsupported statuses. | Add request validation according to `openapi.yaml`. |
| 6 | MongoDB configuration, lines 20–23 | Required database indexes are not created at startup. | Inspect collection indexes after starting the application. | Create all required indexes idempotently. |
| 7 | MongoDB configuration, lines 22–23 | MongoDB datetime handling is not explicitly configured as timezone-aware. | Inspect retrieved datetimes and test UTC/IST conversions. | Configure timezone-aware datetime handling and explicit conversions. |
| 8 | `health`, lines 72–74 | Returns success without checking whether MongoDB is reachable. | Stop MongoDB and request `GET /health`. | Ping MongoDB and return HTTP 503 if the ping fails. |
| 9 | `create_employee`, lines 77–85 | Duplicate checking before insertion is not atomic and allows concurrent duplicate requests. | Send simultaneous requests with the same employee code. | Enforce a unique database index and handle duplicate-key errors with HTTP 409. |
| 10 | `create_employee`, lines 81–85 | Uses naive `datetime.now()` for `created_at`. | Inspect the stored timestamp and compare it with UTC. | Store a timezone-consistent UTC datetime and return epoch milliseconds. |
| 11 | `create_employee`, lines 77–85 | Employee fields and shift-time constraints are not fully validated. | Submit an invalid employee code or identical shift start and end times. | Add validation and return HTTP 422 for invalid requests. |
| 12 | `list_employees`, lines 89–96 | Uses `page * page_size` instead of `(page - 1) * page_size` for the offset. | Request page 1 with page size 2. | Calculate the correct pagination offset. |
| 13 | `list_employees`, lines 90–96 | Counts every employee instead of only employees matching the department filter. | Request employees from a department with fewer employees than the whole collection. | Calculate the total using the same filter as the query. |
| 14 | `list_employees`, lines 88–96 | Does not sort by `emp_code` ascending or validate pagination bounds. | Request multiple pages or use page 0 or page size greater than 100. | Add the required sorting and pagination validation. |
| 15 | `punch_in`, lines 100–105 | Does not return HTTP 404 for an unknown employee before accessing employee details. | Punch in using an unregistered employee code. | Check employee existence and return HTTP 404. |
| 16 | `punch_in`, lines 101–103 | Uses system-local timestamps and dates rather than the required epoch-millisecond and IST attendance-date rules. | Test a timestamp near midnight UTC or an overnight shift. | Validate timestamps, truncate to whole seconds, convert to IST, and apply R1. |
| 17 | `punch_in`, lines 104–118 | Uses check-then-insert without database-enforced uniqueness, allowing concurrent duplicate attendance records. | Send simultaneous punch-in requests for the same employee and date. | Add a unique compound index on `emp_code` and `date` and handle conflicts with HTTP 409. |
| 18 | `punch_in`, lines 106–117 | Does not validate allowed statuses or reject `ABSENT` and `LEAVE` punch-ins. | Submit an unsupported status or `ABSENT`. | Accept only `PRESENT`, `WFH`, and `ON_DUTY` for punch-in. |
| 19 | `punch_in`, lines 118–121 | Adds an unwanted `id` field to the response. | Inspect the response after a successful punch-in. | Omit both MongoDB `_id` and the generated `id` field. |
| 20 | `list_attendance`, lines 133–150 | Loads all matching records into Python and performs sorting and total calculation in memory. | Test with a large attendance collection. | Apply filters, sorting, counting, skip, and limit in MongoDB. |
| 21 | `list_attendance`, lines 144–150 | Sorts only by date descending and omits the `emp_code` ascending tie-breaker. | Request records for multiple employees with the same date. | Sort by date descending and employee code ascending. |
| 22 | `list_attendance`, lines 124–150 | Does not validate pagination parameters or reject an invalid date range. | Request page 0, page size above 100, or `date_from` later than `date_to`. | Validate query parameters and return HTTP 422 for invalid input. |
| 23 | `list_attendance`, lines 144–150 | Returns raw BSON datetime values and replaces `_id` with an unwanted `id` field. | Inspect response field names and timestamp formats. | Serialize instants as integer epoch milliseconds and omit internal IDs. |
| 24 | Missing `/attendance/punch-out` endpoint | Punch-out functionality is not implemented. | Request the documented punch-out path. | Implement atomic punch-out updates, timestamp validation, derived calculations, and required error responses. |
| 25 | Missing `PATCH /attendance/{emp_code}/{date}` endpoint | Attendance regularization, change detection, and audit history are not implemented. | Send a PATCH request and inspect record history. | Implement validated corrections, recompute derived values, and append one history entry per successful change. |
| 26 | Missing `/analytics/employees/{emp_code}/monthly` endpoint | Employee monthly attendance analytics are not implemented. | Request monthly analytics for an employee. | Implement the required MongoDB aggregation and rounding rules. |
| 27 | Missing `/analytics/departments/summary` endpoint | Department-level headcount and attendance summaries are not implemented. | Request a monthly department summary. | Aggregate employees and attendance records while including eligible employees without attendance logs. |
| 28 | Missing `/analytics/leaderboard/late` endpoint | Late-minute ranking and competition-rank handling are not implemented. | Test employees with equal total late minutes. | Implement ranking with ties sharing ranks and apply the limit after ranking. |
| 29 | Missing `/analytics/departments/{department}/trend` endpoint | Daily department trends, missing calendar dates, and seven-day moving averages are not implemented. | Request a date range containing weekends or days without attendance records. | Use a MongoDB aggregation pipeline to generate daily rows and moving averages. |
| 30 | Missing `/admin/explain/{endpoint}` endpoint | Required query-plan inspection is not implemented. | Request explain output for each supported endpoint. | Return MongoDB execution statistics for the same filters, sorting, pagination, or aggregation used by the real endpoint. |

## Checked items that are not defects

- The application uses FastAPI and defines the expected application title and API version.
- The default database name, `attendance_db`, matches the sample seed script.
- `load_dotenv()` runs before the environment variables are read, allowing actual environment variables to take precedence over values loaded from `.env`.
- Employee creation already removes MongoDB `_id` from its response.
- Attendance listing already builds filters for employee code, date range, and status, although its sorting, pagination, total calculation, and serialization require correction.
- The starter has separate helper functions for lateness, work hours, and overtime; their calculations need correction rather than removing the helpers entirely.
