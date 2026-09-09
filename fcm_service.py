"""backend/fcm_service.py
-----------------------
Authoritative Firebase Cloud Messaging (FCM) HTTP v1 service.

Provides send_fcm_notification() and UnregisteredDeviceError for push
notifications across all backend environments (local development and cloud).

Credential Resolution Order:
  1. FIREBASE_SERVICE_ACCOUNT_JSON
     Complete JSON string stored directly in environment/secret variables
     (standard for Render and containerized cloud deployments).

  2. Individual environment variables:
     FIREBASE_PROJECT_ID, FIREBASE_CLIENT_EMAIL, FIREBASE_PRIVATE_KEY
     (useful for secret managers that split fields).

  3. GOOGLE_APPLICATION_CREDENTIALS
     Filesystem path to service-account JSON file
     (standard for local development).

If credentials are not configured, a descriptive RuntimeError is raised
without exposing any sensitive values.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import jwt
except ImportError:
    jwt = None  # Lazy validation in _get_access_token


class UnregisteredDeviceError(RuntimeError):
    """Raised when FCM responds that the device token is invalid or no longer registered."""
    pass


FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TOKEN_URL = "https://oauth2.googleapis.com/token"

_cached_access_token: Optional[str] = None
_cached_access_token_expiry: float = 0.0


def _validate_credentials(credentials: Dict[str, Any]) -> None:
    """Ensure required service account fields are present."""
    required = ["project_id", "client_email", "private_key"]
    for field in required:
        if not credentials.get(field):
            raise RuntimeError(f"Firebase credentials are missing '{field}'.")


def _load_service_account() -> Dict[str, Any]:
    """Load Firebase service-account credentials in deterministic order:
    1. FIREBASE_SERVICE_ACCOUNT_JSON (raw JSON string in env)
    2. FIREBASE_PROJECT_ID, FIREBASE_CLIENT_EMAIL, FIREBASE_PRIVATE_KEY
    3. GOOGLE_APPLICATION_CREDENTIALS (file path)
    """
    # 1. Cloud: complete JSON in environment variable
    json_text = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if json_text and json_text.strip():
        try:
            credentials = json.loads(json_text)
        except json.JSONDecodeError as ex:
            raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON.") from ex
        _validate_credentials(credentials)
        return credentials

    # 2. Cloud: individual environment variables
    project_id = os.environ.get("FIREBASE_PROJECT_ID")
    client_email = os.environ.get("FIREBASE_CLIENT_EMAIL")
    private_key = os.environ.get("FIREBASE_PRIVATE_KEY")
    if project_id and client_email and private_key:
        credentials = {
            "project_id": project_id.strip(),
            "client_email": client_email.strip(),
            # Handle literal escaped newlines from environment strings
            "private_key": private_key.replace("\\n", "\n").strip(),
        }
        _validate_credentials(credentials)
        return credentials

    # 3. Local: file path from GOOGLE_APPLICATION_CREDENTIALS
    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if credentials_path and credentials_path.strip():
        path = Path(credentials_path.strip())
        if not path.exists():
            raise FileNotFoundError(f"Firebase service-account file was not found: {path}")
        try:
            with path.open("r", encoding="utf-8") as file:
                credentials = json.load(file)
        except json.JSONDecodeError as ex:
            raise RuntimeError(f"Firebase service-account file is not valid JSON: {path}") from ex
        _validate_credentials(credentials)
        return credentials

    raise RuntimeError(
        "Firebase credentials are not configured. "
        "Set GOOGLE_APPLICATION_CREDENTIALS for local development, "
        "or FIREBASE_SERVICE_ACCOUNT_JSON / individual FIREBASE_* variables for cloud."
    )


def _get_access_token() -> str:
    """Obtain a Google OAuth2 access token using RS256 JWT assertion."""
    global _cached_access_token, _cached_access_token_expiry

    if jwt is None:
        raise RuntimeError("PyJWT is required for Google OAuth token generation. Run: pip install pyjwt cryptography")

    now = time.time()
    if _cached_access_token and now < _cached_access_token_expiry - 60:
        return _cached_access_token

    credentials = _load_service_account()
    issued_at = int(now)
    expires_at = issued_at + 3600

    assertion = jwt.encode(
        {
            "iss": credentials["client_email"],
            "scope": FCM_SCOPE,
            "aud": TOKEN_URL,
            "iat": issued_at,
            "exp": expires_at,
        },
        credentials["private_key"],
        algorithm="RS256",
    )

    body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")
    except Exception as ex:
        raise RuntimeError(f"Failed to obtain Google OAuth access token: {ex}") from ex

    try:
        token_response = json.loads(response_body)
    except json.JSONDecodeError as ex:
        raise RuntimeError("Google OAuth response was not valid JSON.") from ex

    access_token = token_response.get("access_token")
    if not access_token:
        raise RuntimeError("Google OAuth response did not contain access_token.")

    expires_in = int(token_response.get("expires_in", 3600))
    _cached_access_token = access_token
    _cached_access_token_expiry = now + expires_in
    return access_token


def send_fcm_notification(
    device_token: str,
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
) -> None:
    """Send an FCM HTTP v1 push notification to a target Android device.

    Raises UnregisteredDeviceError if the device token has expired or is invalid.
    Raises RuntimeError on transport or authorization failures.
    """
    credentials = _load_service_account()
    access_token = _get_access_token()
    project_id = credentials["project_id"]

    url = f"https://fcm.googleapis.com/v1/projects/{urllib.parse.quote(project_id, safe='')}/messages:send"

    message: Dict[str, Any] = {
        "message": {
            "token": device_token,
            "notification": {
                "title": title,
                "body": body,
            },
            "android": {
                "priority": "HIGH",
                "notification": {
                    "channel_id": "moldflow_jobs",
                    "sound": "default",
                    "default_sound": True,
                    "default_vibrate_timings": True,
                    "notification_priority": "PRIORITY_MAX",
                    "visibility": "PUBLIC",
                },
            },
        }
    }

    if data:
        message["message"]["data"] = {str(k): str(v) for k, v in data.items()}

    payload = json.dumps(message).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; UTF-8",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")
        print(f"[FCM] Notification sent successfully: {response_body}")
    except urllib.error.HTTPError as ex:
        error_body = ex.read().decode("utf-8", errors="replace")
        print(f"[FCM] HTTP error {ex.code}: {error_body}")
        if ex.code in (400, 404, 410) and (
            "UNREGISTERED" in error_body
            or "NOT_FOUND" in error_body
            or "INVALID_ARGUMENT" in error_body
        ):
            raise UnregisteredDeviceError(f"FCM token unregistered ({ex.code}): {error_body}") from ex
        raise RuntimeError(f"FCM notification failed ({ex.code}): {error_body}") from ex
    except Exception as ex:
        raise RuntimeError(f"FCM notification failed: {ex}") from ex
