"""Moldflow Mobile Job Backend
Canonical FastAPI application entrypoint.
Routes to app_postgres_ready which supports both PostgreSQL and SQLite,
along with Network License Ingestion and Read APIs.
"""
import sys
from pathlib import Path

_backend_dir = str(Path(__file__).resolve().parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app_postgres_ready import *
from app_postgres_ready import app
