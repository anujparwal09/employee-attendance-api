# Design Decisions

1. **Indexes.** I created a unique index on `employees.emp_code` to enforce employee identity, plus indexes on `(department, joined_on)` and `joined_on` to support department and eligibility queries. On `attendance_logs`, the unique `(emp_code, date)` index prevents duplicate daily records; `(emp_code, date DESC)` supports employee history, while `(date DESC, emp_code ASC)` supports attendance listing. I did not add a separate attendance index on `emp_code`, because the unique compound index already has `emp_code` as its prefix.

2. **Punch-in race.** Both requests may attempt to insert the same employee/date record. MongoDB’s unique compound index allows only one insert to succeed. The other request receives a duplicate-key error, which the API converts to HTTP 409. This avoids relying on a race-prone check-then-insert.

3. **Ties.** The leaderboard uses ranking rather than array position. It filters by rank after ranking, so employees tied at the cutoff rank are all returned. Rows are ordered by total late minutes and then `emp_code`.

4. **Headcount.** The department summary combines attendance aggregates with employees, so eligible employees with no attendance records still count. Employees who join after the period ends are excluded.

5. **100x scale.** I would benchmark realistic data, inspect execution plans, and tune indexes and aggregation pipelines using measured bottlenecks before introducing caching or pre-aggregated summaries.