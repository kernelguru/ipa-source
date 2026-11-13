"""Users, password hashing, session cookies and API tokens."""
import base64
import hashlib
import hmac
import secrets

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from . import config, db

SESSION_COOKIE = "ipas_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
TOKEN_PREFIX = "ipas_"
SCOPES = ("full", "read", "upload")

_signer = None


def signer():
    global _signer
    if _signer is None:
        _signer = TimestampSigner(config.secret_key(), salt="session")
    return _signer


# --------------------------------------------------------------- passwords --
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


# ------------------------------------------------------------------- users --
def user_count() -> int:
    return db.one("SELECT COUNT(*) AS n FROM users")["n"]


def create_user(username: str, password: str) -> int:
    if len(password) < 8:
        raise ValueError("Passwort muss mindestens 8 Zeichen haben")
    return db.execute(
        "INSERT INTO users(username, password_hash, created_at) VALUES(?,?,?)",
        username.strip(), hash_password(password), db.now(),
    )


def bootstrap_admin():
    if config.ADMIN_USER and config.ADMIN_PASSWORD and user_count() == 0:
        create_user(config.ADMIN_USER, config.ADMIN_PASSWORD)
        db.log_event("info", f"Admin-Benutzer '{config.ADMIN_USER}' aus ENV angelegt")


def check_login(username: str, password: str):
    u = db.one("SELECT * FROM users WHERE username = ?", username.strip())
    if u and verify_password(password, u["password_hash"]):
        return u
    return None


# ---------------------------------------------------------------- sessions --
def _pw_tag(u) -> str:
    # Changes when the password changes -> invalidates existing sessions
    return hashlib.sha256(u["password_hash"].encode()).hexdigest()[:12]


def make_session(u) -> str:
    return signer().sign(f"{u['id']}:{_pw_tag(u)}".encode()).decode()


def user_from_session(value: str):
    try:
        raw = signer().unsign(value, max_age=SESSION_MAX_AGE).decode()
        uid, tag = raw.split(":")
    except (BadSignature, SignatureExpired, ValueError):
        return None
    u = db.one("SELECT * FROM users WHERE id = ?", int(uid))
    if u and hmac.compare_digest(tag, _pw_tag(u)):
        return u
    return None


# -------------------------------------------------------------- api tokens --
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(user_id: int, name: str, scope: str = "full"):
    if scope not in SCOPES:
        raise ValueError(f"scope muss einer von {', '.join(SCOPES)} sein")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    tid = db.execute(
        "INSERT INTO api_tokens(user_id, name, scope, prefix, token_hash, created_at) VALUES(?,?,?,?,?,?)",
        user_id, name, scope, token[:12], _hash_token(token), db.now(),
    )
    return tid, token


def user_from_token(token: str):
    t = db.one("SELECT * FROM api_tokens WHERE token_hash = ?", _hash_token(token))
    if not t:
        return None, None
    db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", db.now(), t["id"])
    u = db.one("SELECT * FROM users WHERE id = ?", t["user_id"])
    return u, t["scope"]


# ---------------------------------------------------------- request auth --
class Principal:
    def __init__(self, user, scope, via):
        self.user = user
        self.scope = scope   # full | read | upload
        self.via = via       # session | token | basic

    def can(self, need: str) -> bool:
        if self.scope == "full":
            return True
        if need == "read":
            return self.scope in ("read",)
        if need == "upload":
            return self.scope == "upload"
        return False


def authenticate(request) -> "Principal | None":
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        u, scope = user_from_token(h[7:].strip())
        return Principal(u, scope, "token") if u else None
    if h.lower().startswith("basic "):
        try:
            username, _, pw = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            return None
        if pw.startswith(TOKEN_PREFIX):
            u, scope = user_from_token(pw)
            return Principal(u, scope, "basic") if u and u["username"] == username else None
        u = check_login(username, pw)
        return Principal(u, "full", "basic") if u else None
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        u = user_from_session(cookie)
        return Principal(u, "full", "session") if u else None
    return None
