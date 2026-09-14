from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent

# DB config (overridable via environment variables)
DB_HOST = os.getenv("DB_HOST", "database")
DB_NAME = os.getenv("DB_NAME", "buecherdb")
DB_USER = os.getenv("DB_USER", "admin")
DB_PASS = os.getenv("DB_PASS", "Speechy$2026")

# UI / query limits
ALLOWED_LIMITS = {"10", "20", "50", "200", "500", "ALL"}
DEFAULT_LIMIT = "20"

# Optional: common options for templates
LIMIT_OPTIONS = ["10", "20", "50", "200", "500", "ALL"]
