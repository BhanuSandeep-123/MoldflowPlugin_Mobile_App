import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jwt


FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TOKEN_URL = "https://oauth2.googleapis.com/token"

_cached_access_token: str | None = None
_cached_access_token_expiry: float = 0.0


def _load_service_account() -> dict:
    """
    Load Firebase service-account credentials.

    Supported modes:

    1. Local development:
       GOOGLE_APPLICATION_CREDENTIALS -> path to JSON file

    2. Cloud:
       FIREBASE_SERVICE_ACCOUNT_JSON -> complete JSON string

    3. Cloud secret settings:
       FIREBASE_PROJECT_ID
       FIREBASE_CLIENT_EMAIL
       FIREBASE_PRIVATE_KEY

    The existing local file-based behavior remains supported.
    """

    # ------------------------------------------------------------------
    # Cloud: complete JSON stored in an environment/secret variable.
    # ------------------------------------------------------------------
    json_text = os.environ.get(
        "FIREBASE_SERVICE_ACCOUNT_JSON"
    )

    if json_text:
        try:
            credentials = json.loads(json_text)
        except json.JSONDecodeError as ex:
            raise RuntimeError(
                "FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON."
            ) from ex

        _validate_credentials(credentials)
        return credentials

    # ------------------------------------------------------------------
    # Cloud: individual secret settings.
    # ------------------------------------------------------------------
    project_id = os.environ.get(
        "FIREBASE_PROJECT_ID"
    )
    client_email = os.environ.get(
        "FIREBASE_CLIENT_EMAIL"
    )
    private_key = os.environ.get(
        "FIREBASE_PRIVATE_KEY"
    )

    if project_id and client_email and private_key:
        credentials = {
            "project_id": project_id,
            "client_email": client_email,
            # Azure/app settings commonly store newlines escaped.
            "private_key": private_key.replace(
                "\\n",
                "\n",
            ),
        }

        _validate_credentials(credentials)
        return credentials

    # ------------------------------------------------------------------
    # Local development: existing JSON file behavior.
    # ------------------------------------------------------------------
    credentials_path = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )

    if not credentials_path:
        raise RuntimeError(
            "Firebase credentials are not configured. "
            "Set GOOGLE_APPLICATION_CREDENTIALS for local development "
            "or configure cloud Firebase secret environment variables."
        )

    path = Path(credentials_path)

    if not path.exists():
        raise FileNotFoundError(
            f"Firebase service-account file was not found: {path}"
        )

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            credentials = json.load(file)
    except json.JSONDecodeError as ex:
        raise RuntimeError(
            f"Firebase service-account file is not valid JSON: {path}"
        ) from ex

    _validate_credentials(credentials)
    return credentials


def _validate_credentials(credentials: dict) -> None:
    required = [
        "project_id",
        "client_email",
        "private_key",
    ]

    for field in required:
        if not credentials.get(field):
            raise RuntimeError(
                f"Firebase credentials are missing '{field}'."
            )


def _get_access_token() -> str:
    global _cached_access_token
    global _cached_access_token_expiry

    now = time.time()

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
            response_body = (
                response.read().decode("utf-8")
            )

    except Exception as ex:
        raise RuntimeError(
            f"Failed to obtain Google OAuth access token: {ex}"
        ) from ex

    try:
        token_response = json.loads(
            response_body
        )
    except json.JSONDecodeError as ex:
        raise RuntimeError(
            "Google OAuth response was not valid JSON."
        ) from ex

    access_token = token_response.get(
        "access_token"
    )

    if not access_token:
        raise RuntimeError(
            "Google OAuth response did not contain access_token."
        )

    expires_in = int(
        token_response.get(
            "expires_in",
            3600,
        )
    )

    _cached_access_token = access_token
    _cached_access_token_expiry = (
        now + expires_in
    )

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
        }
    }

    if data:
        message["message"]["data"] = {
            str(key): str(value)
            for key, value in data.items()
        }

    payload = json.dumps(message).encode(
        "utf-8"
    )

    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization":
                f"Bearer {access_token}",
            "Content-Type":
                "application/json; UTF-8",
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

        raise RuntimeError(
            f"FCM notification failed ({ex.code}): "
            f"{error_body}"
        ) from ex

    except Exception as ex:
        raise RuntimeError(
            f"FCM notification failed: {ex}"
        ) from ex
