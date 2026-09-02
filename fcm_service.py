class UnregisteredDeviceError(RuntimeError):
    """Raised when FCM responds that the device token is invalid or no longer registered."""
    pass


import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

import jwt


FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TOKEN_URL = "https://oauth2.googleapis.com/token"

_cached_access_token: str | None = None
_cached_access_token_expiry: float = 0


def _load_service_account() -> dict:
    credentials_path = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )

    if not credentials_path:
        raise RuntimeError(
            "GOOGLE_APPLICATION_CREDENTIALS is not set."
        )

    path = Path(credentials_path)

    if not path.exists():
        raise FileNotFoundError(
            f"Firebase service-account file was not found: {path}"
        )

    with path.open("r", encoding="utf-8") as file:
        credentials = json.load(file)

    required = [
        "project_id",
        "client_email",
        "private_key",
    ]

    for field in required:
        if not credentials.get(field):
            raise RuntimeError(
                f"Firebase service-account JSON is missing '{field}'."
            )

    return credentials


def _get_access_token() -> str:
    global _cached_access_token
    global _cached_access_token_expiry

    now = time.time()

    # Reuse token while it is still valid.
    if (
        _cached_access_token
        and now < _cached_access_token_expiry - 60
    ):
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
            "grant_type":
                "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type":
                "application/x-www-form-urlencoded"
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            response_body = response.read().decode("utf-8")

    except Exception as ex:
        raise RuntimeError(
            f"Failed to obtain Google OAuth access token: {ex}"
        ) from ex

    token_response = json.loads(response_body)

    access_token = token_response.get("access_token")

    if not access_token:
        raise RuntimeError(
            "Google OAuth response did not contain access_token."
        )

    expires_in = int(
        token_response.get("expires_in", 3600)
    )

    _cached_access_token = access_token
    _cached_access_token_expiry = now + expires_in

    return access_token


def send_fcm_notification(
    device_token: str,
    title: str,
    body: str,
    data: dict[str, str] | None = None,
) -> None:
    credentials = _load_service_account()

    access_token = _get_access_token()

    project_id = credentials["project_id"]

    url = (
        "https://fcm.googleapis.com/v1/projects/"
        f"{urllib.parse.quote(project_id, safe='')}"
        "/messages:send"
    )

    message: dict = {
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
        message["message"]["data"] = {
            str(key): str(value)
            for key, value in data.items()
        }

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
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            response_body = (
                response.read().decode("utf-8")
            )

        print(
            "[FCM] Notification sent successfully: "
            f"{response_body}"
        )

    except urllib.error.HTTPError as ex:
        error_body = ex.read().decode(
            "utf-8",
            errors="replace",
        )

        print(
            f"[FCM] HTTP error {ex.code}: "
            f"{error_body}"
        )

        if ex.code in (400, 404, 410) and (
            "UNREGISTERED" in error_body
            or "NOT_FOUND" in error_body
            or "INVALID_ARGUMENT" in error_body
        ):
            raise UnregisteredDeviceError(
                f"FCM token unregistered ({ex.code}): {error_body}"
            ) from ex

        raise RuntimeError(
            f"FCM notification failed ({ex.code}): "
            f"{error_body}"
        ) from ex

    except Exception as ex:
        raise RuntimeError(
            f"FCM notification failed: {ex}"
        ) from ex