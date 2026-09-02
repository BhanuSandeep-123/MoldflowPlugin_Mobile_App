# Moldflow Mobile Backend — Phase 1

This is the first proof-of-concept backend for the existing Moldflow Synergy Plugin.

## What it does

```text
Moldflow Synergy
    ↓
compute_jobs.py
    ↓
cad_diagnostics.py
    ↓
mobile_reporter.py
    ↓ HTTPS/HTTP POST
FastAPI /reportJobStatus
    ↓
in-memory job store
    ↓
GET /jobs
```

It intentionally does **not** use a database, Firebase, Autodesk APS, or push notifications yet.

## 1. Create a virtual environment

From this `mobile_backend` directory in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation, run the server with the venv Python directly:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

## 2. Start the backend

```powershell
$env:MOLDFLOW_API_KEY="dev-moldflow-key-change-me"
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

You should see:

```text
Uvicorn running on http://127.0.0.1:8000
```

## 3. Test health

Open:

```text
http://127.0.0.1:8000/health
```

Expected:

```json
{"status":"ok","service":"Moldflow Mobile Job Backend"}
```

## 4. Connect the existing plugin

Create `mobile_report_config.json` beside `mobile_reporter.py` by copying the existing example and use:

```json
{
  "enabled": true,
  "backend_url": "http://127.0.0.1:8000",
  "api_key": "dev-moldflow-key-change-me",
  "user_id": "DEV-USER-001",
  "machine_id": "DEV-PC-001"
}
```

Do this only for the local proof of concept. Do not use this development key in production.

## 5. Run a Moldflow analysis

Watch the backend terminal. You should see lines such as:

```text
[JOB] 12345 | INPROGRESS | 25% | Automotive_Bumper_001 | user=DEV-USER-001 machine=DEV-PC-001
[JOB] 12345 | INPROGRESS | 30% | Automotive_Bumper_001 | user=DEV-USER-001 machine=DEV-PC-001
[JOB] 12345 | COMPLETED  | 100% | Automotive_Bumper_001 | user=DEV-USER-001 machine=DEV-PC-001
```

## 6. Read current jobs

PowerShell:

```powershell
$headers = @{ "X-Api-Key" = "dev-moldflow-key-change-me" }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/jobs" -Headers $headers
```

## Phase 1 success criteria

- Backend starts locally.
- `/health` returns OK.
- Existing plugin remains unchanged except for the local mobile config.
- A real Moldflow job produces a POST to `/reportJobStatus`.
- Backend prints the job ID, name, status, and progress.
- `GET /jobs` returns the latest job.

After this works, Phase 2 will replace the in-memory store with a real database and introduce proper authentication. Firebase/FCM, cloud hosting, Autodesk APS, and the mobile app come later.
