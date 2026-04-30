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


FLASK_CONFIG = {
    "SECRET_KEY": SECRET_KEY,
    "SQLALCHEMY_DATABASE_URI": SQLALCHEMY_DATABASE_URI,
    "SQLALCHEMY_TRACK_MODIFICATIONS": False,
    "MAX_CONTENT_LENGTH": MAX_UPLOAD_BYTES,
}
