"""Moldflow Mobile Job Backend
Canonical FastAPI application entrypoint.
Routes to app_postgres_ready which supports both PostgreSQL and SQLite,
along with Network License Ingestion and Read APIs.
"""
from app_postgres_ready import *
from app_postgres_ready import app
