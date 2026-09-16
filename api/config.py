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

SRU_MAX_RECORDS = 100

TABLE_COLUMNS = [
    # DNB order/layout as default
    {"key": "id",          "label": "ID",      "visible": True,  "order": 10, "type": "string", "class": ""},
    {"key": "title_block", "label": "Titel",   "visible": True,  "order": 20, "type": "string", "class": ""},
    {"key": "authors",     "label": "Autoren", "visible": True,  "order": 30, "type": "string", "class": ""},
    {"key": "year",        "label": "Jahr",    "visible": True,  "order": 40, "type": "number", "class": ""},
    {"key": "pages",       "label": "Seiten",  "visible": True,  "order": 50, "type": "number", "class": ""},
    {"key": "publisher",   "label": "Verlag",  "visible": True,  "order": 60, "type": "string", "class": ""},
    {"key": "isbn",        "label": "ISBN",    "visible": True,  "order": 70, "type": "string", "class": ""},
    # optional extra fields (off by default)
    {"key": "matching",    "label": "Matching-Key", "visible": False, "order": 80, "type": "string", "class": "key"},
    {"key": "dnb_id",      "label": "DNB-ID",       "visible": False, "order": 90, "type": "string", "class": ""},
]

# Amazon
CLIENT_ID = "***"
CLIENT_SECRET = "***"
PARTNER_TAG = "***"
