"""Git hosting providers: GitHub (github.py) and Gitea/Forgejo/Codeberg (this module)."""
import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = "ipa-source"
KNOWN_GITEA = {"codeberg.org", "gitea.com", "git.disroot.org", "next.forgejo.org"}


class ProviderError(Exception):
    pass


def parse_repo(text: str, kind_hint: str = "", host_hint: str = ""):
    """
    Accepts 'owner/repo', 'github.com/owner/repo', 'https://codeberg.org/owner/repo/releases' …
    Returns (kind, host, 'owner/repo'). host is '' for GitHub.
    """
    t = (text or "").strip()
    if not re.match(r"^[a-z]+://", t) and re.match(r"^[\w.-]+\.[a-z]{2,}/", t):
        t = "https://" + t
    if re.match(r"^https?://", t):
        u = urllib.parse.urlparse(t)
        parts = [p for p in u.path.split("/") if p]
        if len(parts) < 2:
            raise ValueError("URL enthält kein owner/repo")
        repo = f"{parts[0]}/{parts[1].removesuffix('.git')}"
        if u.netloc.lower() in ("github.com", "www.github.com"):
            return "github", "", repo
        return "gitea", f"{u.scheme}://{u.netloc}", repo
    t = t.strip("/")
    if t.count("/") != 1:
        raise ValueError("Repository muss 'owner/name' oder eine URL sein")
    if kind_hint == "gitea":
        if not host_hint:
            raise ValueError("Für Gitea/Forgejo fehlt die Server-Adresse")
        return "gitea", host_hint.rstrip("/"), t
    return "github", "", t


# ------------------------------------------------------------- Gitea API --
def _headers(token=""):
    h = {"Accept": "application/json", "User-Agent": UA}
    if token:
        h["Authorization"] = f"token {token}"
    return h


def gitea_get(host: str, path: str, token=""):
    url = f"{host.rstrip('/')}/api/v1{path}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=_headers(token)), timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("message", e.reason)
        except Exception:
            msg = e.reason
        raise ProviderError(f"{host}: HTTP {e.code} {msg}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"{host}: {e.reason}") from None


def gitea_releases(host: str, repo: str, token=""):
    return gitea_get(host, f"/repos/{repo}/releases?limit=30", token)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def download(url: str, dest: Path, token="", auth_host=""):
    """Download following redirects; the token is only sent to auth_host."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(8):
        same = auth_host and urllib.parse.urlparse(url).netloc == urllib.parse.urlparse(auth_host).netloc
        req = urllib.request.Request(url, headers=_headers(token if same else ""))
        try:
            resp = opener.open(req, timeout=900)
            break
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise ProviderError(f"Download HTTP {e.code}") from None
    else:
        raise ProviderError("Zu viele Weiterleitungen")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with resp as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, length=1024 * 1024)
    tmp.replace(dest)
