# Employee Attendance & Analytics API

A REST API built with **FastAPI** and **MongoDB** for employee management, attendance tracking, attendance regularization, and analytics.

## Features

- Employee creation and listing
- Employee punch-in and punch-out
- Attendance record listing and regularization
- Monthly employee attendance summaries
- Department-wise attendance summaries
- Late-comer leaderboard
- Daily department attendance trends
- MongoDB indexes created automatically at application startup
- API health check and query execution-plan inspection

## Prerequisites

Make sure you have the following installed:

- **Python 3.11 or newer**
- **MongoDB 6.0 or newer** (MongoDB 7.0 is recommended)
- **Git** (for version control and submission)

MongoDB must be running and accessible to the application.

## Setup and Installation

### 1. Clone the repository

After publishing the repository, use its actual URL:

```bash
git clone <YOUR_PUBLIC_REPOSITORY_URL>
cd <YOUR_REPOSITORY_FOLDER>
```

Replace the placeholders with your Git repository URL and folder name.

If you already have the project on your computer, open a terminal in the project root instead.

### 2. Create a virtual environment

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure MongoDB

Set the MongoDB connection URI and database name using environment variables.

**Windows (PowerShell):**

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "hrone_attendance_dev"
```

**macOS / Linux:**

```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="hrone_attendance_dev"
```

These values are examples for a local MongoDB instance. Update them to match your environment.

The application can also load configuration from a local `.env` file. Environment variables take precedence over values in `.env`. Never commit `.env` files, database credentials, or other secrets to the repository.

### 5. Start the API

From the repository root, run:

```bash
uvicorn app.main:app --port 8000
```

The API will be available at:

- **Base URL:** http://127.0.0.1:8000
- **Health check:** http://127.0.0.1:8000/health
- **Interactive API documentation:** http://127.0.0.1:8000/docs
- **Alternative API documentation:** http://127.0.0.1:8000/redoc

MongoDB must be running before you start the API.

## Running Tests

Run the automated test suite from the repository root:

```bash
python -m pytest -q
```

Tests require the project's dependencies and the test environment to be configured correctly.

## Database Initialization

The application creates its required MongoDB indexes automatically at startup. No separate index-creation command is required.

Configure `MONGO_URI` and `MONGO_DB` before starting the API to ensure it connects to the intended database.

## Project Structure

```text
.
├── app/
│   └── main.py
├── tests/
├── requirements.txt
├── REVIEW.md
├── DECISIONS.md
├── README.md
├── .gitignore
├── openapi.yaml
└── DATA_MODEL.md
```

The API implementation is contained in `app/main.py`, as required by the assignment. `REVIEW.md` documents the starter-code review, while `DECISIONS.md` records the design decisions.

## Configuration and Security

- Configure the database using `MONGO_URI` and `MONGO_DB`.
- Keep credentials and secrets out of version control.
- Do not commit `.env` files, virtual environments, or generated data dumps.
- Do not include the confidential `PROBLEM_STATEMENT.docx` in the public repository.

## Notes

This project was developed for the Employee Attendance System assignment. Its correctness and performance should be evaluated against the assignment's business rules and larger test datasets, not only the provided sample records.
