"""Runtime configuration from environment variables."""
import os
import secrets
from pathlib import Path

VERSION = "0.1.0"
API_VERSION = "v1"

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
STORAGE_DIR = DATA_DIR / "storage"      # imported IPAs + icons
INBOX_DIR = DATA_DIR / "inbox"          # WebDAV drop zone
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "ipa-source.db"

# Public URL the devices use. Can also be set in the web UI (settings.base_url).
BASE_URL_ENV = os.environ.get("BASE_URL", "").rstrip("/")

# Optional bootstrap admin (only used when no user exists yet)
ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# Optional: GitHub token from env (overrides nothing; used if none set in UI)
GITHUB_TOKEN_ENV = os.environ.get("GITHUB_TOKEN", "").strip()

# Set to "1" behind HTTPS so session cookies get the Secure flag
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "2048")) * 1024 * 1024


def ensure_dirs():
    for d in (DATA_DIR, STORAGE_DIR, INBOX_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)


def secret_key() -> str:
    """Persistent secret for signing session cookies."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    ensure_dirs()
    f = DATA_DIR / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(48))
        f.chmod(0o600)
    return f.read_text().strip()
