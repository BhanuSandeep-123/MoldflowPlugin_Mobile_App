import sqlite3

from fcm_service import send_fcm_notification


DB_PATH = r"C:\MF\MoldflowSynergyPlugin\mobile_backend\moldflow_jobs.db"


conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

try:
    row = conn.execute(
        """
        SELECT device_id, user_id, push_token
        FROM devices
        WHERE platform = 'android'
          AND push_token IS NOT NULL
          AND TRIM(push_token) <> ''
        ORDER BY updated_at DESC
        LIMIT 1
        """
    ).fetchone()
finally:
    conn.close()


if row is None:
    raise RuntimeError(
        "No Android FCM device token exists in the devices table."
    )


device_id = row["device_id"]
user_id = row["user_id"]
token = row["push_token"]

print(f"Testing device: {device_id}")
print(f"Testing user: {user_id}")
print(f"Token length: {len(token)}")
print(f"Token prefix: {token[:20]}...")


send_fcm_notification(
    device_token=token,
    title="Moldflow Test",
    body="FCM notification test from the Moldflow backend.",
    data={
        "test": "true",
    },
)

print("FCM test completed.")