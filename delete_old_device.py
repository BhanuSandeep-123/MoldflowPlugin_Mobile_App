import sqlite3

db = r"C:\MF\MoldflowSynergyPlugin\mobile_backend\moldflow_jobs.db"

conn = sqlite3.connect(db)

cursor = conn.execute(
    "DELETE FROM devices WHERE device_id = ?",
    ("DEV-ANDROID-001",),
)

conn.commit()

print("Deleted:", cursor.rowcount)

conn.close()