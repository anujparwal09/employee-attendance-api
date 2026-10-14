
from fastapi.testclient import TestClient

from app.main import app, db


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_list_employees_endpoint():
    with TestClient(app) as client:
        response = client.get("/employees")

    assert response.status_code == 200
    assert isinstance(response.json(), (list, dict))


def test_list_attendance_endpoint():
    with TestClient(app) as client:
        response = client.get("/attendance")

    assert response.status_code == 200
    assert isinstance(response.json(), (list, dict))


def test_department_summary_endpoint():
    with TestClient(app) as client:
        response = client.get(
            "/analytics/departments/summary",
            params={"month": "2026-10"},
        )

    assert response.status_code == 200
    assert isinstance(response.json(), (list, dict))


def test_late_leaderboard_endpoint():
    with TestClient(app) as client:
        response = client.get(
            "/analytics/leaderboard/late",
            params={"month": "2026-10"},
        )

    assert response.status_code == 200
    assert isinstance(response.json(), (list, dict))


def test_create_employee_and_reject_duplicate():
    emp_code = "EMP987654"

    employee = {
        "emp_code": emp_code,
        "name": "Automated Test Employee",
        "email": "automated.test.employee@example.com",
        "department": "Testing",
        "joined_on": "2026-10-01",
        "shift_start": "09:30",
        "shift_end": "18:00",
    }

    db.employees.delete_one({"emp_code": emp_code})

    try:
        with TestClient(app) as client:
            first_response = client.post("/employees", json=employee)
            assert first_response.status_code == 201, first_response.text

            duplicate_response = client.post("/employees", json=employee)
            assert duplicate_response.status_code == 409, (
                duplicate_response.text
            )
    finally:
        db.employees.delete_one({"emp_code": emp_code})


def test_punch_in_and_reject_duplicate():
    emp_code = "EMP987655"
    attendance_date = "2026-10-12"

    employee = {
        "emp_code": emp_code,
        "name": "Punch In Test Employee",
        "email": "punch.in.test@example.com",
        "department": "Testing",
        "joined_on": attendance_date,
        "shift_start": "09:30",
        "shift_end": "18:00",
    }

    # 2026-10-12 09:45 IST, represented as Unix epoch milliseconds.
    punch_in_timestamp = 1791778500000

    db.attendance_logs.delete_many({"emp_code": emp_code})
    db.employees.delete_one({"emp_code": emp_code})

    try:
        with TestClient(app) as client:
            create_response = client.post("/employees", json=employee)
            assert create_response.status_code == 201, create_response.text

            punch_body = {
                "emp_code": emp_code,
                "punched_at": punch_in_timestamp,
            }

            first_response = client.post(
                "/attendance/punch-in",
                json=punch_body,
            )
            assert first_response.status_code in (200, 201), (
                first_response.text
            )

            duplicate_response = client.post(
                "/attendance/punch-in",
                json=punch_body,
            )
            assert duplicate_response.status_code == 409, (
                duplicate_response.text
            )
    finally:
        db.attendance_logs.delete_many({"emp_code": emp_code})
        db.employees.delete_one({"emp_code": emp_code})


def test_punch_out_closes_attendance_record():
    emp_code = "EMP987656"
    attendance_date = "2026-10-12"

    employee = {
        "emp_code": emp_code,
        "name": "Punch Out Test Employee",
        "email": "punch.out.test@example.com",
        "department": "Testing",
        "joined_on": attendance_date,
        "shift_start": "09:30",
        "shift_end": "18:00",
    }

    # 09:45 IST and 18:30 IST on 12 Oct 2026.
    punch_in_timestamp = 1791778500000
    punch_out_timestamp = 1791810000000

    db.attendance_logs.delete_many({"emp_code": emp_code})
    db.employees.delete_one({"emp_code": emp_code})

    try:
        with TestClient(app) as client:
            create_response = client.post(
                "/employees",
                json=employee,
            )
            assert create_response.status_code == 201, create_response.text

            punch_in_response = client.post(
                "/attendance/punch-in",
                json={
                    "emp_code": emp_code,
                    "punched_at": punch_in_timestamp,
                },
            )
            assert punch_in_response.status_code in (200, 201), (
                punch_in_response.text
            )

            punch_out_response = client.post(
                "/attendance/punch-out",
                json={
                    "emp_code": emp_code,
                    "punched_at": punch_out_timestamp,
                },
            )
            assert punch_out_response.status_code in (200, 201), (
                punch_out_response.text
            )

            # Verify that the attendance record is now closed.
            record = db.attendance_logs.find_one(
                {
                    "emp_code": emp_code,
                    "date": attendance_date,
                }
            )
            assert record is not None
            assert record["punch_in"] is not None
            assert record["punch_out"] is not None

            # A second punch-out must not overwrite the first one.
            duplicate_response = client.post(
                "/attendance/punch-out",
                json={
                    "emp_code": emp_code,
                    "punched_at": punch_out_timestamp + 60000,
                },
            )
            assert duplicate_response.status_code >= 400, (
                duplicate_response.text
            )

            record_after_duplicate = db.attendance_logs.find_one(
                {
                    "emp_code": emp_code,
                    "date": attendance_date,
                }
            )
            assert record_after_duplicate["punch_out"] == record["punch_out"]
    finally:
        db.attendance_logs.delete_many({"emp_code": emp_code})
        db.employees.delete_one({"emp_code": emp_code})
