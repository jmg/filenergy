import os
import secrets

from dotenv import load_dotenv

load_dotenv()

ENV = os.environ.get("FILENERGY_ENV", "LOCAL")

SECRET_KEY = os.environ.get("FILENERGY_SECRET_KEY") or secrets.token_hex(32)

LOGIN_VIEW = "user.login"

UPLOAD_DIR = os.environ.get("FILENERGY_UPLOAD_DIR", "files")
MAX_UPLOAD_BYTES = int(os.environ.get("FILENERGY_MAX_UPLOAD_BYTES", 50 * 1024 * 1024))

DB_URI_DEFAULT = "sqlite:///" + os.path.abspath(
    os.environ.get("FILENERGY_DB_PATH", "filenergy.db")
)
SQLALCHEMY_DATABASE_URI = os.environ.get("FILENERGY_DB_URI", DB_URI_DEFAULT)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")

VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY")
VOYAGE_EMBED_MODEL = os.environ.get("VOYAGE_EMBED_MODEL", "voyage-3-lite")

CHUNK_SIZE = int(os.environ.get("FILENERGY_CHUNK_SIZE", 1200))
CHUNK_OVERLAP = int(os.environ.get("FILENERGY_CHUNK_OVERLAP", 150))
RETRIEVAL_K = int(os.environ.get("FILENERGY_RETRIEVAL_K", 6))

# Rate limit: at most ASK_RATE_LIMIT /ask requests per ASK_RATE_WINDOW_SECONDS per user.
ASK_RATE_LIMIT = int(os.environ.get("FILENERGY_ASK_RATE_LIMIT", 30))
ASK_RATE_WINDOW_SECONDS = int(os.environ.get("FILENERGY_ASK_RATE_WINDOW", 60))

# When True, run indexing in-line on the request thread. Tests force this on.
SYNC_INDEXING = os.environ.get("FILENERGY_SYNC_INDEXING", "false").lower() == "true"

# Stripe
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLIC_KEY = os.environ.get("STRIPE_PUBLIC_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_PRO = os.environ.get("STRIPE_PRICE_PRO")
STRIPE_PRICE_TEAM = os.environ.get("STRIPE_PRICE_TEAM")
APP_BASE_URL = os.environ.get("FILENERGY_BASE_URL", "http://localhost:5000")

# Plan limits per workspace per month / forever.
PLAN_LIMITS = {
    "free": {
        "asks_per_month": 100,
        "files_max": 25,
        "members_max": 1,
        "storage_bytes_max": 100 * 1024 * 1024,    # 100 MB
        "label": "Free",
        "price_monthly": 0,
    },
    "pro": {
        "asks_per_month": 2000,
        "files_max": 1000,
        "members_max": 1,
        "storage_bytes_max": 5 * 1024 * 1024 * 1024,  # 5 GB
        "label": "Pro",
        "price_monthly": 19,
    },
    "team": {
        "asks_per_month": 20000,
        "files_max": 25000,
        "members_max": 25,
        "storage_bytes_max": 100 * 1024 * 1024 * 1024,  # 100 GB
        "label": "Team",
        "price_monthly": 99,
    },
}


FLASK_CONFIG = {
    "SECRET_KEY": SECRET_KEY,
    "SQLALCHEMY_DATABASE_URI": SQLALCHEMY_DATABASE_URI,
    "SQLALCHEMY_TRACK_MODIFICATIONS": False,
    "MAX_CONTENT_LENGTH": MAX_UPLOAD_BYTES,
}
