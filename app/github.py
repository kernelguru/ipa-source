"""Minimal GitHub REST client (stdlib only)."""
import fnmatch
import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, db

API = "https://api.github.com"
UA = "ipa-source"


class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


def token() -> str:
    return db.get_setting("github_token") or config.GITHUB_TOKEN_ENV


def _headers(accept="application/vnd.github+json", tok=None):
    h = {"Accept": accept, "User-Agent": UA, "X-GitHub-Api-Version": "2022-11-28"}
    t = tok if tok is not None else token()
    if t:
        h["Authorization"] = f"Bearer {t}"
    return h


def request(path_or_url, method="GET", body=None, tok=None, accept="application/vnd.github+json"):
    url = path_or_url if path_or_url.startswith("http") else API + path_or_url
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers(accept, tok))
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None, r.headers
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("message", e.reason)
        except Exception:
            msg = e.reason
        raise GitHubError(e.code, msg) from None


def get(path, tok=None):
    return request(path, tok=tok)[0]


def paginate(path, key=None, limit=1000):
    out = []
    url = API + path + ("&" if "?" in path else "?") + "per_page=100"
    while url and len(out) < limit:
        data, headers = request(url)
        items = data[key] if key else data
        out.extend(items)
        url = None
        for part in (headers.get("Link") or "").split(","):
            if 'rel="next"' in part:
                url = part[part.index("<") + 1:part.index(">")]
    return out[:limit]


# ---------------------------------------------------------------- download --
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def download(api_url: str, dest: Path, accept="application/octet-stream"):
    """Follow GitHub's redirect to blob storage WITHOUT sending the token there."""
    req = urllib.request.Request(api_url, headers=_headers(accept))
    try:
        resp = _opener.open(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise GitHubError(e.code, e.reason) from None
        resp = urllib.request.urlopen(
            urllib.request.Request(e.headers["Location"], headers={"User-Agent": UA}), timeout=900
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with resp as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, length=1024 * 1024)
    tmp.replace(dest)


# ---------------------------------------------------------------- queries --
def whoami(tok=None):
    return get("/user", tok=tok)


def list_repos():
    repos = paginate("/user/repos?sort=pushed&affiliation=owner,collaborator,organization_member")
    return [
        {
            "full_name": r["full_name"],
            "private": r["private"],
            "description": r.get("description") or "",
            "pushed_at": r.get("pushed_at"),
            "language": r.get("language"),
            "archived": r.get("archived", False),
        }
        for r in repos
    ]


def releases(repo):
    return get(f"/repos/{repo}/releases?per_page=30")


def artifacts(repo):
    return get(f"/repos/{repo}/actions/artifacts?per_page=100").get("artifacts", [])


def scan_repo(repo, asset_pattern="*.ipa"):
    """Look for IPA releases and likely IPA artifacts. Used for repo discovery."""
    result = {"repo": repo, "releases_with_ipa": 0, "latest_release": None,
              "artifact_names": [], "suggested": False, "error": None}
    try:
        for rel in releases(repo):
            if rel.get("draft"):
                continue
            if any(fnmatch.fnmatch(a["name"], asset_pattern) for a in rel.get("assets", [])):
                result["releases_with_ipa"] += 1
                if not result["latest_release"]:
                    result["latest_release"] = rel["tag_name"]
    except GitHubError as e:
        result["error"] = str(e)
    try:
        names = {}
        for a in artifacts(repo):
            if not a.get("expired"):
                names[a["name"]] = names.get(a["name"], 0) + 1
        result["artifact_names"] = [{"name": n, "count": c} for n, c in names.items()]
    except GitHubError as e:
        if not result["error"]:
            result["error"] = str(e)
    ipa_like = [n for n in result["artifact_names"] if "ipa" in n["name"].lower() or "ios" in n["name"].lower()]
    result["suggested"] = bool(result["releases_with_ipa"] or ipa_like)
    return result


# ------------------------------------------------------------ device flow --
def _form_post(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise GitHubError(e.code, e.reason) from None


def device_start(client_id: str, scope="repo"):
    """Start OAuth device flow. Returns user_code, verification_uri, device_code, interval."""
    res = _form_post("https://github.com/login/device/code", {"client_id": client_id, "scope": scope})
    if "error" in res:
        raise GitHubError(400, res.get("error_description") or res["error"])
    return res


def device_poll(client_id: str, device_code: str):
    """Returns {'access_token': ...} when done, or {'error': 'authorization_pending' | ...}."""
    return _form_post(
        "https://github.com/login/oauth/access_token",
        {
            "client_id": client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
