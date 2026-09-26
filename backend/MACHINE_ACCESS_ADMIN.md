# Machine-access provisioning endpoint

Lets an admin assign a mobile user to a workstation (so they can see its jobs and get its push
notifications) **without database shell access**. Job ownership is never changed.

## Before deploying

Set this in **Render → your service → Environment** (it is not in the repo and must never be committed):

| Variable | Value |
|---|---|
| `MACHINE_ACCESS_ADMIN_SECRET` | a long random string, at least 24 characters (e.g. 48+ random characters) |

If it is missing or shorter than 24 characters the endpoints are disabled and every call returns `401`.
Rotate it (or remove it) once provisioning is done.

## Endpoints

Both require the header `X-Admin-Secret: <the secret>` and are hidden from `/docs` and `/openapi.json`.
Mobile JWTs, enrollment tokens and workstation API keys are **not** accepted.

```
POST /admin/machine-access/grant    {"user_id": "...", "machine_id": "..."}  ->  {"granted": true, "user_id": "...", "machine_id": "..."}
POST /admin/machine-access/revoke   {"user_id": "...", "machine_id": "..."}  ->  {"revoked": true, "user_id": "...", "machine_id": "..."}
```

`404` if the user or machine does not exist (grant) or no assignment exists (revoke). Grant is idempotent;
revoke keeps the row with `enabled = 0`.

## Example (PowerShell — the secret is typed, not stored in history)

```powershell
$s = Read-Host "Admin secret" -AsSecureString
$secret = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
Invoke-RestMethod -Method Post -Uri "https://<your-service>.onrender.com/admin/machine-access/grant" `
  -Headers @{ "X-Admin-Secret" = $secret } -ContentType "application/json" `
  -Body (@{ user_id = "<mobile user id>"; machine_id = "<machine id>" } | ConvertTo-Json)
```
