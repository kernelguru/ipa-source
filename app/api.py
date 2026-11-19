"""REST API /api/v1 – used by the web admin and (later) the iOS app."""
import functools
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import urllib.request
import uuid

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import auth, certs, config, db, downloader, feed, github, pairing, providers, signing, store, sync, uploads
from . import ipa as ipa_mod
from .db import jloads


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def err(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def endpoint(need="full"):
    """
    need: None (public) | "any" (any authenticated) | "read" | "upload" | "full"
    Session-authenticated mutating requests must carry X-Requested-With (CSRF guard).
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(request: Request):
            try:
                p = None
                if need is not None:
                    p = await run_in_threadpool(auth.authenticate, request)
                    if not p:
                        return err(401, "unauthorized", "Anmeldung erforderlich")
                    if need not in ("any",) and not p.can(need):
                        return err(403, "forbidden", f"Token-Scope '{p.scope}' reicht nicht aus")
                    if (p.via == "session" and request.method not in ("GET", "HEAD")
                            and request.headers.get("x-requested-with") != "ipa-source"):
                        return err(403, "csrf", "X-Requested-With fehlt")
                request.state.principal = p
                return await fn(request)
            except ApiError as e:
                return err(e.status, e.code, e.message)
            except github.GitHubError as e:
                return err(502, "github", str(e))
            except ValueError as e:
                return err(400, "invalid", str(e))
            except providers.ProviderError as e:
                return err(502, "provider", str(e))
        return wrapper
    return deco


async def body(request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise ApiError(400, "invalid_json", "Ungültiges JSON")
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "JSON-Objekt erwartet")
    return data


def get_or_404(sql, *args):
    r = db.one(sql, *args)
    if not r:
        raise ApiError(404, "not_found", "Nicht gefunden")
    return r


def b(v):
    return 1 if v in (True, 1, "1", "true", "on") else 0


# ======================================================================= info
@endpoint(None)
async def info(request):
    return JSONResponse({
        "name": "ipa-source",
        "version": config.VERSION,
        "api": config.API_VERSION,
        "setup_required": auth.user_count() == 0,
        "features": ["github", "github_device_flow", "upload", "chunked_upload", "icon_designer", "icon_inject", "server_signing", "ota_install", "usb_install", "link_downloader", "gitea_sources", "webdav", "webhook", "legacy_feed"],
    })


@endpoint(None)
async def setup(request):
    if auth.user_count() > 0:
        raise ApiError(409, "already_setup", "Es existiert bereits ein Benutzer")
    data = await body(request)
    uid = auth.create_user(data.get("username", "admin"), data.get("password", ""))
    if not db.get_setting("feed_key"):
        db.set_settings({"feed_key": secrets.token_urlsafe(16)})
    if not db.get_setting("webhook_secret"):
        db.set_settings({"webhook_secret": secrets.token_urlsafe(24)})
    return _login_response(db.one("SELECT * FROM users WHERE id = ?", uid))


# ======================================================================= auth
def _user_json(u):
    return {"id": u["id"], "username": u["username"], "created_at": u["created_at"]}


def _login_response(u):
    resp = JSONResponse({"user": _user_json(u)})
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(u), max_age=auth.SESSION_MAX_AGE,
                    httponly=True, samesite="strict", secure=config.COOKIE_SECURE, path="/")
    return resp


@endpoint(None)
async def login(request):
    data = await body(request)
    u = await run_in_threadpool(auth.check_login, data.get("username", ""), data.get("password", ""))
    if not u:
        time.sleep(0.5)
        raise ApiError(401, "bad_credentials", "Benutzername oder Passwort falsch")
    return _login_response(u)


@endpoint(None)
async def token_login(request):
    """For native clients: exchange username/password for an API token."""
    data = await body(request)
    u = await run_in_threadpool(auth.check_login, data.get("username", ""), data.get("password", ""))
    if not u:
        time.sleep(0.5)
        raise ApiError(401, "bad_credentials", "Benutzername oder Passwort falsch")
    tid, tok = auth.create_token(u["id"], data.get("name") or "iOS-App", data.get("scope", "full"))
    return JSONResponse({"token": tok, "token_id": tid, "user": _user_json(u)}, status_code=201)


@endpoint("any")
async def logout(request):
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@endpoint("any")
async def me(request):
    p = request.state.principal
    return JSONResponse({"user": _user_json(p.user), "scope": p.scope, "via": p.via})


@endpoint("full")
async def change_password(request):
    data = await body(request)
    u = request.state.principal.user
    if not auth.verify_password(data.get("current", ""), u["password_hash"]):
        raise ApiError(400, "bad_credentials", "Aktuelles Passwort falsch")
    if len(data.get("new", "")) < 8:
        raise ApiError(400, "invalid", "Neues Passwort: mindestens 8 Zeichen")
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", auth.hash_password(data["new"]), u["id"])
    u = db.one("SELECT * FROM users WHERE id = ?", u["id"])
    return _login_response(u)


# ===================================================================== tokens
@endpoint("full")
async def tokens(request):
    if request.method == "GET":
        rows = db.all_("SELECT id, name, scope, prefix, created_at, last_used_at FROM api_tokens "
                       "WHERE user_id = ? ORDER BY id", request.state.principal.user["id"])
        return JSONResponse({"tokens": rows})
    data = await body(request)
    tid, tok = auth.create_token(request.state.principal.user["id"], data.get("name") or "Token",
                                 data.get("scope", "full"))
    return JSONResponse({"id": tid, "token": tok}, status_code=201)


@endpoint("full")
async def token_delete(request):
    db.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?",
               int(request.path_params["id"]), request.state.principal.user["id"])
    return JSONResponse({"ok": True})


# =================================================================== settings
EDITABLE = {"base_url", "feed_name", "feed_identifier", "feed_subtitle", "feed_description", "feed_icon_url",
            "feed_header_url", "feed_website", "feed_tint", "feed_private", "developer",
            "github_client_id", "sync_interval", "webhook_secret"}


def _settings_json(request):
    s = db.get_settings()
    out = {k: s[k] for k in EDITABLE}
    out["github_connected"] = bool(github.token())
    out["github_user"] = s["github_user"]
    out["github_token_from_env"] = bool(config.GITHUB_TOKEN_ENV and not s["github_token"])
    out["base_url_effective"] = feed.base_url(request)
    return out


@endpoint("full")
async def settings(request):
    if request.method == "PATCH":
        data = await body(request)
        upd = {k: v for k, v in data.items() if k in EDITABLE}
        if "sync_interval" in upd:
            upd["sync_interval"] = max(60, int(upd["sync_interval"]))
        if "feed_private" in upd:
            upd["feed_private"] = b(upd["feed_private"])
        if "base_url" in upd:
            upd["base_url"] = str(upd["base_url"]).rstrip("/")
        db.set_settings(upd)
    return JSONResponse(_settings_json(request))


@endpoint("full")
async def feed_urls(request):
    pub = feed.public_prefix(request)
    return JSONResponse({
        "apps_json": f"{pub}/apps.json",
        "legacy_json": f"{pub}/apps-legacy.json",
        "private": db.get_setting("feed_private") == "1",
        "webhook_url": f"{feed.base_url(request)}/api/v1/webhooks/github",
        "webdav_url": f"{feed.base_url(request)}/dav/",
        "upload_url": f"{feed.base_url(request)}/api/v1/uploads",
    })


@endpoint("full")
async def feed_rotate(request):
    db.set_settings({"feed_key": secrets.token_urlsafe(16)})
    return await feed_urls.__wrapped__(request)


# ======================================================================= apps
def _app_json(a, request, with_builds=False):
    pub = feed.public_prefix(request)
    builds = db.all_("SELECT * FROM builds WHERE app_id = ? ORDER BY date DESC", a["id"])
    out = {
        **{k: a[k] for k in ("id", "bundle_id", "name", "subtitle", "description", "developer", "tint",
                             "category", "icon_url", "created_at", "updated_at", "sort_order")},
        "visible": bool(a["visible"]),
        "screenshots": jloads(a["screenshots"], []),
        "entitlements": jloads(a["entitlements"], []),
        "privacy": jloads(a["privacy"], {}),
        "icon": a["icon_url"] or (f"{pub}/icons/{a['id']}.png?v={a['updated_at'][:19].replace(':', '')}"
                                  if a["has_icon"] else None),
        "icon_status": ("custom" if a["icon_custom"] else "external" if a["icon_url"] else
                        "blank" if a["has_icon"] and a["icon_blank"] else "ipa" if a["has_icon"] else "missing"),
        "icon_custom": bool(a["icon_custom"]),
        "sign_options": jloads(a["sign_options"], {}),
        "auto_inject": bool(a["auto_inject"]),
        "build_count": len(builds),
        "latest": _build_json(builds[0], request) if builds else None,
    }
    if with_builds:
        out["builds"] = [_build_json(x, request) for x in builds]
    return out


def _build_json(x, request):
    src = db.one("SELECT repo FROM sources WHERE id = ?", x["source_id"]) if x["source_id"] else None
    return {
        **{k: x[k] for k in ("id", "app_id", "source_id", "origin", "origin_ref", "version", "build_version",
                             "min_os", "size", "sha256", "date", "notes", "filename", "created_at")},
        "visible": bool(x["visible"]),
        "pinned": bool(x["pinned"]),
        "icon_injected": bool(x["icon_injected"]),
        "icon_state": x["icon_state"],
        "repo": src["repo"] if src else None,
        "permissions": jloads(x["permissions"], {}),
        "download_url": f"{feed.public_prefix(request)}/dl/{x['id']}/{x['filename']}",
    }


@endpoint("read")
async def apps(request):
    rows = db.all_("SELECT * FROM apps ORDER BY sort_order, name COLLATE NOCASE")
    return JSONResponse({"apps": [_app_json(a, request) for a in rows]})


@endpoint("read")
async def app_detail(request):
    aid = int(request.path_params["id"])
    if request.method == "GET":
        return JSONResponse(_app_json(get_or_404("SELECT * FROM apps WHERE id = ?", aid), request, True))
    if not request.state.principal.can("full"):
        raise ApiError(403, "forbidden", "Scope 'full' nötig")
    get_or_404("SELECT id FROM apps WHERE id = ?", aid)
    if request.method == "DELETE":
        await run_in_threadpool(store.delete_app, aid)
        return JSONResponse({"ok": True})
    data = await body(request)
    fields = {}
    for k in ("name", "subtitle", "description", "developer", "tint", "category", "icon_url"):
        if k in data:
            fields[k] = str(data[k] or "")
    if "category" in fields and fields["category"] not in feed.CATEGORIES:
        raise ApiError(400, "invalid", f"category muss einer von {', '.join(sorted(feed.CATEGORIES))} sein")
    for k in ("visible", "auto_inject"):
        if k in data:
            fields[k] = b(data[k])
    if "sort_order" in data:
        fields["sort_order"] = int(data["sort_order"])
    for k, default in (("screenshots", []), ("entitlements", []), ("privacy", {})):
        if k in data:
            if not isinstance(data[k], type(default)):
                raise ApiError(400, "invalid", f"{k} hat falschen Typ")
            fields[k] = json.dumps(data[k])
    if fields:
        fields["updated_at"] = db.now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE apps SET {sets} WHERE id = ?", *fields.values(), aid)
    return JSONResponse(_app_json(db.one("SELECT * FROM apps WHERE id = ?", aid), request, True))


@endpoint("full")
async def app_icon(request):
    aid = int(request.path_params["id"])
    a = get_or_404("SELECT * FROM apps WHERE id = ?", aid)
    data = await request.body()
    if not data.startswith(b"\x89PNG"):
        raise ApiError(400, "invalid", "PNG erwartet")
    store.app_dir(a["bundle_id"]).mkdir(parents=True, exist_ok=True)
    store.icon_path(a["bundle_id"]).write_bytes(data)
    db.execute("UPDATE apps SET has_icon = 1, icon_blank = 0, icon_url = '', updated_at = ? WHERE id = ?",
               db.now(), aid)
    return JSONResponse(_app_json(db.one("SELECT * FROM apps WHERE id = ?", aid), request))


@endpoint("full")
async def app_custom_icon(request):
    """
    PUT multipart: icon_1024, icon_180, icon_167, icon_152, icon_120, icon_76 (PNG),
                   inject=none|all, auto_inject=0|1
    DELETE: drop the custom icon (feed falls back to the IPA icon; builds stay as they are)
    """
    aid = int(request.path_params["id"])
    a = get_or_404("SELECT * FROM apps WHERE id = ?", aid)
    if request.method == "DELETE":
        await run_in_threadpool(store.remove_custom_icon, a)
        return JSONResponse(_app_json(db.one("SELECT * FROM apps WHERE id = ?", aid), request, True))
    form = await request.form(max_files=10, max_part_size=1024 * 1024)
    pngs = {}
    for px in ipa_mod.ICON_SIZES:
        f = form.get(f"icon_{px}")
        if f is None or not hasattr(f, "read"):
            raise ApiError(400, "invalid", f"Feld icon_{px} fehlt")
        pngs[px] = await f.read()
    inject = str(form.get("inject") or "none")
    auto = str(form.get("auto_inject") or "0") in ("1", "true", "on")
    await form.close()
    await run_in_threadpool(store.save_custom_icon, a, pngs)
    db.execute("UPDATE apps SET auto_inject = ? WHERE id = ?", 1 if auto else 0, aid)
    if inject == "all":
        ids = [x["id"] for x in db.all_("SELECT id FROM builds WHERE app_id = ?", aid)]
        store.queue_inject(ids)
    return JSONResponse(_app_json(db.one("SELECT * FROM apps WHERE id = ?", aid), request, True))


@endpoint("full")
async def app_inject_icon(request):
    """POST {build_ids?: [..]} – default: all builds of the app."""
    aid = int(request.path_params["id"])
    a = get_or_404("SELECT * FROM apps WHERE id = ?", aid)
    if not a["icon_custom"]:
        raise ApiError(400, "no_icon", "Zuerst ein eigenes Icon im Designer speichern")
    data = {}
    if await request.body():
        data = await body(request)
    rows = db.all_("SELECT id FROM builds WHERE app_id = ?", aid)
    ids = [r["id"] for r in rows]
    if data.get("build_ids"):
        ids = [i for i in ids if i in set(map(int, data["build_ids"]))]
    store.queue_inject(ids)
    return JSONResponse({"queued": ids}, status_code=202)


@endpoint("full")
async def fetch_image(request):
    """Server-side image fetch for the icon designer (avoids browser CORS limits)."""
    url = request.query_params.get("url", "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ApiError(400, "invalid", "Nur http(s)-URLs")

    def get():
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ipa-source",
                                                   "Accept": "image/*"})
        with urllib.request.urlopen(req, timeout=20) as r:
            ctype = r.headers.get_content_type()
            data = r.read(15 * 1024 * 1024 + 1)
        return ctype, data

    try:
        ctype, data = await run_in_threadpool(get)
    except Exception as e:
        raise ApiError(502, "fetch_failed", f"Bild konnte nicht geladen werden: {e}")
    if len(data) > 15 * 1024 * 1024:
        raise ApiError(413, "too_large", "Bild größer als 15 MB")
    if not ctype.startswith("image/"):
        raise ApiError(415, "not_image", f"Keine Bilddatei ({ctype})")
    return Response(data, media_type=ctype, headers={"Cache-Control": "no-store"})


# ===================================================================== builds
@endpoint("full")
async def build_detail(request):
    bid = int(request.path_params["id"])
    get_or_404("SELECT id FROM builds WHERE id = ?", bid)
    if request.method == "DELETE":
        await run_in_threadpool(store.delete_build, bid)
        return JSONResponse({"ok": True})
    data = await body(request)
    fields = {}
    for k in ("visible", "pinned"):
        if k in data:
            fields[k] = b(data[k])
    if "notes" in data:
        fields["notes"] = str(data["notes"] or "")
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE builds SET {sets} WHERE id = ?", *fields.values(), bid)
    return JSONResponse(_build_json(db.one("SELECT * FROM builds WHERE id = ?", bid), request))


# ==================================================================== sources
SOURCE_FIELDS = {"repo": str, "label": str, "use_releases": b, "use_artifacts": b, "include_prereleases": b,
                 "asset_pattern": str, "artifact_name": str, "artifact_branch": str,
                 "keep_releases": int, "keep_artifacts": int, "enabled": b}


def _source_json(s):
    n = db.one("SELECT COUNT(*) AS n FROM builds WHERE source_id = ?", s["id"])["n"]
    out = dict(s)
    out["has_token"] = bool(out.pop("token_enc", ""))
    out["web_url"] = (f"{s['host']}/{s['repo']}" if s["kind"] == "gitea" else f"https://github.com/{s['repo']}")
    out["provider"] = ("Codeberg" if "codeberg.org" in s["host"] else "Gitea/Forgejo") if s["kind"] == "gitea" else "GitHub"
    for k in ("use_releases", "use_artifacts", "include_prereleases", "enabled"):
        out[k] = bool(out[k])
    out["build_count"] = n
    out["syncing"] = sync.status()["running"] == s["repo"]
    return out


def _clean_source(data, partial=False):
    out = {}
    for k, conv in SOURCE_FIELDS.items():
        if k in data:
            out[k] = conv(data[k]) if conv is not b else b(data[k])
    if "repo" in out:
        try:
            kind, host, repo = providers.parse_repo(out["repo"], str(data.get("kind") or ""), str(data.get("host") or ""))
        except ValueError as e:
            raise ApiError(400, "invalid", str(e))
        out.update(repo=repo, kind=kind, host=host)
        if kind == "gitea":
            out["use_artifacts"] = 0      # Gitea/Forgejo actions artifacts are not supported
    elif not partial:
        raise ApiError(400, "invalid", "repo fehlt")
    if "token" in data:
        out["token_enc"] = certs.encrypt(str(data["token"])) if data["token"] else ""
    for k in ("asset_pattern",):
        if k in out and not out[k]:
            out[k] = "*.ipa"
    return out


@endpoint("full")
async def sources(request):
    if request.method == "GET":
        return JSONResponse({"sources": [_source_json(s) for s in db.all_("SELECT * FROM sources ORDER BY repo")]})
    data = _clean_source(await body(request))
    if db.one("SELECT id FROM sources WHERE kind = ? AND host = ? AND lower(repo) = lower(?)",
              data["kind"], data["host"], data["repo"]):
        raise ApiError(409, "exists", "Quelle existiert bereits")
    cols = ["created_at", *data.keys()]
    try:
        sid = db.execute(f"INSERT INTO sources({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                         db.now(), *data.values())
    except Exception as e:
        if "UNIQUE" in str(e):
            raise ApiError(409, "exists", "Ein Repo mit diesem Namen existiert bereits als Quelle")
        raise
    sync.enqueue("source", sid)
    return JSONResponse(_source_json(db.one("SELECT * FROM sources WHERE id = ?", sid)), status_code=201)


@endpoint("full")
async def source_detail(request):
    sid = int(request.path_params["id"])
    s = get_or_404("SELECT * FROM sources WHERE id = ?", sid)
    if request.method == "GET":
        return JSONResponse(_source_json(s))
    if request.method == "DELETE":
        if request.query_params.get("delete_builds") in ("1", "true"):
            for x in db.all_("SELECT id FROM builds WHERE source_id = ?", sid):
                await run_in_threadpool(store.delete_build, x["id"])
        db.execute("DELETE FROM sources WHERE id = ?", sid)
        return JSONResponse({"ok": True})
    raw = await body(request)
    raw.pop("repo", None)        # repo/provider are fixed after creation
    data = _clean_source(raw, partial=True)
    if data:
        sets = ", ".join(f"{k} = ?" for k in data)
        db.execute(f"UPDATE sources SET {sets} WHERE id = ?", *data.values(), sid)
    return JSONResponse(_source_json(db.one("SELECT * FROM sources WHERE id = ?", sid)))


@endpoint("full")
async def source_sync(request):
    sid = int(request.path_params["id"])
    get_or_404("SELECT id FROM sources WHERE id = ?", sid)
    sync.enqueue("source", sid)
    return JSONResponse({"queued": True}, status_code=202)


@endpoint("full")
async def sync_all(request):
    sync.enqueue("all")
    return JSONResponse({"queued": True}, status_code=202)


@endpoint("read")
async def status(request):
    return JSONResponse({
        "worker": sync.status(),
        "counts": {
            "apps": db.one("SELECT COUNT(*) n FROM apps")["n"],
            "builds": db.one("SELECT COUNT(*) n FROM builds")["n"],
            "sources": db.one("SELECT COUNT(*) n FROM sources")["n"],
            "bytes": db.one("SELECT COALESCE(SUM(size),0) n FROM builds")["n"],
        },
    })


@endpoint("full")
async def events(request):
    limit = min(500, int(request.query_params.get("limit", 100)))
    return JSONResponse({"events": db.all_("SELECT * FROM events ORDER BY id DESC LIMIT ?", limit)})


# ===================================================================== github
_device = {}


@endpoint("full")
async def gh_status(request):
    tok = github.token()
    user = None
    if tok:
        try:
            user = (await run_in_threadpool(github.whoami))["login"]
        except github.GitHubError as e:
            return JSONResponse({"connected": False, "error": str(e)})
    return JSONResponse({"connected": bool(tok), "user": user,
                         "from_env": bool(config.GITHUB_TOKEN_ENV and not db.get_setting("github_token")),
                         "device_flow_available": bool(db.get_setting("github_client_id"))})


@endpoint("full")
async def gh_token(request):
    if request.method == "DELETE":
        db.set_settings({"github_token": "", "github_user": ""})
        return JSONResponse({"ok": True})
    data = await body(request)
    tok = (data.get("token") or "").strip()
    user = await run_in_threadpool(github.whoami, tok)
    db.set_settings({"github_token": tok, "github_user": user["login"]})
    return JSONResponse({"connected": True, "user": user["login"]})


@endpoint("full")
async def gh_device_start(request):
    cid = db.get_setting("github_client_id")
    if not cid:
        raise ApiError(400, "no_client_id", "Keine OAuth-App Client-ID in den Einstellungen")
    res = await run_in_threadpool(github.device_start, cid)
    handle = uuid.uuid4().hex
    _device[handle] = {"device_code": res["device_code"], "expires": time.time() + res.get("expires_in", 900)}
    return JSONResponse({"handle": handle, "user_code": res["user_code"],
                         "verification_uri": res["verification_uri"], "interval": res.get("interval", 5)})


@endpoint("full")
async def gh_device_poll(request):
    data = await body(request)
    d = _device.get(data.get("handle", ""))
    if not d or d["expires"] < time.time():
        raise ApiError(410, "expired", "Anmeldung abgelaufen, bitte neu starten")
    res = await run_in_threadpool(github.device_poll, db.get_setting("github_client_id"), d["device_code"])
    if "access_token" in res:
        user = await run_in_threadpool(github.whoami, res["access_token"])
        db.set_settings({"github_token": res["access_token"], "github_user": user["login"]})
        _device.pop(data["handle"], None)
        return JSONResponse({"status": "connected", "user": user["login"]})
    return JSONResponse({"status": res.get("error", "pending"), "interval": res.get("interval")})


@endpoint("full")
async def gh_repos(request):
    repos = await run_in_threadpool(github.list_repos)
    configured = {s["repo"].lower(): s["id"] for s in db.all_("SELECT id, repo FROM sources")}
    for r in repos:
        r["source_id"] = configured.get(r["full_name"].lower())
    return JSONResponse({"repos": repos})


@endpoint("full")
async def gh_scan(request):
    repo = f"{request.path_params['owner']}/{request.path_params['repo']}"
    return JSONResponse(await run_in_threadpool(github.scan_repo, repo))


# ==================================================================== uploads
async def _receive_to_tmp(stream_iter):
    tmp = config.TMP_DIR / f"up-{uuid.uuid4().hex}.ipa"
    size = 0
    with open(tmp, "wb") as f:
        async for chunk in stream_iter:
            size += len(chunk)
            if config.MAX_UPLOAD_BYTES and size > config.MAX_UPLOAD_BYTES:
                f.close()
                tmp.unlink(missing_ok=True)
                raise ApiError(413, "too_large", "Datei zu groß")
            f.write(chunk)
    return tmp


@endpoint("upload")
async def upload(request):
    """
    POST multipart/form-data: file=<ipa>, notes=<text>, visible=0|1
    PUT  raw body (application/octet-stream), ?filename=&notes=
    """
    notes, filename, visible = "", None, True
    if request.method == "PUT":
        filename = request.query_params.get("filename")
        notes = request.query_params.get("notes", "")
        visible = request.query_params.get("visible", "1") not in ("0", "false")
        tmp = await _receive_to_tmp(request.stream())
    else:
        form = await request.form(max_files=1, max_part_size=1024 * 1024)
        up = form.get("file")
        if up is None or not hasattr(up, "read"):
            raise ApiError(400, "invalid", "Feld 'file' fehlt")
        filename = up.filename
        notes = str(form.get("notes") or "")
        visible = str(form.get("visible", "1")) not in ("0", "false")

        async def chunks():
            while True:
                c = await up.read(1024 * 1024)
                if not c:
                    break
                yield c
        tmp = await _receive_to_tmp(chunks())
        await form.close()
    try:
        build = await run_in_threadpool(store.import_ipa, tmp, origin="upload", filename=filename, notes=notes)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise ApiError(400, "invalid_ipa", f"IPA konnte nicht gelesen werden: {e}")
    if not visible:
        db.execute("UPDATE builds SET visible = 0 WHERE id = ?", build["id"])
    a = db.one("SELECT * FROM apps WHERE id = ?", build["app_id"])
    return JSONResponse({"build": _build_json(db.one("SELECT * FROM builds WHERE id = ?", build["id"]), request),
                         "app": _app_json(a, request)}, status_code=201)


# ======================================================= chunked uploads
def _session_json(meta, request):
    out = uploads.public(meta)
    if meta.get("build_id"):
        bld = db.one("SELECT * FROM builds WHERE id = ?", meta["build_id"])
        app_row = db.one("SELECT * FROM apps WHERE id = ?", meta["app_id"]) if bld else None
        if bld and app_row:
            out["build"] = _build_json(bld, request)
            out["app"] = _app_json(app_row, request)
    return out


def _own_session(request):
    meta = uploads.get(request.path_params["sid"])
    if not meta:
        raise ApiError(404, "not_found", "Upload-Sitzung nicht gefunden oder abgelaufen")
    p = request.state.principal
    if meta.get("user_id") not in (None, p.user["id"]):
        raise ApiError(404, "not_found", "Upload-Sitzung nicht gefunden")
    return meta


@endpoint("upload")
async def upload_session_create(request):
    data = await body(request)
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid", "size muss eine Zahl sein")
    try:
        meta = await run_in_threadpool(uploads.create, str(data.get("filename") or ""), size,
                                       str(data.get("notes") or ""), data.get("visible", True) not in (False, 0, "0"),
                                       request.state.principal.user["id"])
    except ValueError as e:
        code = 413 if "groß" in str(e) else 507 if "Speicherplatz" in str(e) else 400
        raise ApiError(code, "too_large" if code != 400 else "invalid", str(e))
    return JSONResponse(_session_json(meta, request), status_code=201)


@endpoint("upload")
async def upload_session(request):
    meta = _own_session(request)
    if request.method == "GET":
        return JSONResponse(_session_json(meta, request))
    if request.method == "DELETE":
        await run_in_threadpool(uploads.abort, meta["id"])
        return JSONResponse({"ok": True})
    # PUT chunk
    try:
        offset = int(request.query_params.get("offset", "-1"))
    except ValueError:
        offset = -1
    try:
        meta = await uploads.append(meta["id"], offset, request.stream())
    except uploads.OffsetMismatch as e:
        return JSONResponse({"error": {"code": "offset_mismatch", "message": "Falscher Offset",
                                       "received": e.received}, "received": e.received}, status_code=409)
    return JSONResponse({"received": meta["received"], "size": meta["size"]})


@endpoint("upload")
async def upload_session_complete(request):
    meta = _own_session(request)
    meta = await run_in_threadpool(uploads.complete, meta["id"])
    return JSONResponse(_session_json(meta, request), status_code=202)


# ==================================================================== signing
@endpoint("full")
async def certs_list(request):
    if request.method == "GET":
        return JSONResponse({"certs": [certs.public(c) for c in db.all_("SELECT * FROM certs ORDER BY id")],
                             "capabilities": signing.capabilities()})
    form = await request.form(max_files=2, max_part_size=1024 * 1024)
    p12, prof = form.get("p12"), form.get("profile")
    if not hasattr(p12, "read") or not hasattr(prof, "read"):
        raise ApiError(400, "invalid", ".p12 und .mobileprovision sind nötig")
    p12_data, prof_data = await p12.read(), await prof.read()
    name, password = str(form.get("name") or ""), str(form.get("password") or "")
    await form.close()
    c = await run_in_threadpool(certs.add, name, p12_data, password, prof_data)
    return JSONResponse(certs.public(c), status_code=201)


@endpoint("full")
async def cert_detail(request):
    cid = int(request.path_params["id"])
    c = get_or_404("SELECT * FROM certs WHERE id = ?", cid)
    if request.method == "DELETE":
        await run_in_threadpool(certs.remove, cid)
        return JSONResponse({"ok": True})
    data = await body(request)
    if data.get("is_default"):
        certs.set_default(cid)
    if data.get("name"):
        db.execute("UPDATE certs SET name = ? WHERE id = ?", str(data["name"]), cid)
    if "adapt_bundle_id" in data:
        db.execute("UPDATE certs SET adapt_bundle_id = ? WHERE id = ?", b(data["adapt_bundle_id"]), cid)
    return JSONResponse(certs.public(certs.get(cid)))


@endpoint("full")
async def build_sign(request):
    """POST {cert_id?, force?, bundle_id?, options?: {filepicker: bool}, remember?} -> signed job.
    options omitted -> the app's remembered options are used.
    The app keeps its bundle ID (like Feather) unless bundle_id is given or the
    certificate is set to adapt it to the profile's App ID."""
    bid = int(request.path_params["id"])
    get_or_404("SELECT id FROM builds WHERE id = ?", bid)
    data = await body(request) if await request.body() else {}
    cid = data.get("cert_id") or (certs.default() or {}).get("id")
    if not cid:
        raise ApiError(400, "no_cert", "Kein Signier-Zertifikat hinterlegt")
    opts = data.get("options")
    if opts is not None and not isinstance(opts, dict):
        raise ApiError(400, "invalid", "options muss ein Objekt sein, z. B. {\"filepicker\": true}")
    s = await run_in_threadpool(signing.request_sign, bid, int(cid), bool(data.get("force")),
                                str(data.get("bundle_id") or ""), opts, data.get("remember", True) is not False)
    return JSONResponse(signing.public(s, feed.base_url(request)), status_code=202)


@endpoint("full")
async def signed_list(request):
    app_id = request.query_params.get("app_id")
    sql = ("SELECT s.* FROM signed s JOIN builds b ON b.id = s.build_id "
           + ("WHERE b.app_id = ? " if app_id else "") + "ORDER BY s.id DESC")
    rows = db.all_(sql, int(app_id)) if app_id else db.all_(sql)
    return JSONResponse({"signed": [signing.public(r, feed.base_url(request)) for r in rows]})


@endpoint("full")
async def signed_detail(request):
    sid = int(request.path_params["id"])
    s = get_or_404("SELECT * FROM signed WHERE id = ?", sid)
    if request.method == "DELETE":
        await run_in_threadpool(signing.delete_signed, sid)
        return JSONResponse({"ok": True})
    out = signing.public(s, feed.base_url(request))
    if request.query_params.get("log"):
        out["log"] = s["log"]
    return JSONResponse(out)


@endpoint("full")
async def signed_install_usb(request):
    sid = int(request.path_params["id"])
    data = await body(request)
    if not data.get("udid"):
        raise ApiError(400, "invalid", "udid fehlt")
    jid = await run_in_threadpool(signing.start_usb_install, sid, str(data["udid"]))
    return JSONResponse(signing.job(jid), status_code=202)


@endpoint("full")
async def install_job(request):
    j = signing.job(request.path_params["jid"])
    if not j:
        raise ApiError(404, "not_found", "Auftrag unbekannt (Server neu gestartet?)")
    return JSONResponse(j)


@endpoint("full")
async def devices(request):
    return JSONResponse(await run_in_threadpool(signing.devices))


@endpoint("full")
async def device_pair(request):
    ok, msg = await run_in_threadpool(signing.pair, request.path_params["udid"])
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 409)


# ================================================================= pairing
async def _pcall(fn, *args, **kw):
    try:
        return await run_in_threadpool(fn, *args, **kw)
    except LookupError as e:
        raise ApiError(404, "not_found", str(e))
    except RuntimeError as e:
        raise ApiError(502, "device", str(e))


@endpoint("full")
async def pairing_status(request):
    return JSONResponse(await run_in_threadpool(pairing.status))


@endpoint("full")
async def pairing_usb(request):
    b = await body(request)
    jid = await _pcall(pairing.start_usb, str(b.get("udid") or ""), b.get("kind") or "lockdown",
                       bool(b.get("wifi", True)))
    return JSONResponse({"job": jid}, status_code=202)


@endpoint("full")
async def pairing_job(request):
    j = pairing.job(request.path_params["jid"])
    if not j:
        raise ApiError(404, "not_found", "Auftrag unbekannt (Server neu gestartet?)")
    return JSONResponse(j)


@endpoint("full")
async def pairing_job_pin(request):
    b = await body(request)
    pairing.submit_pin(request.path_params["jid"], str(b.get("pin") or ""))
    return JSONResponse({"ok": True})


@endpoint("full")
async def pairing_discover(request):
    t = min(max(int(request.query_params.get("timeout", 4)), 1), 15)
    return JSONResponse({"services": await _pcall(pairing.discover, t)})


@endpoint("full")
async def pairing_wifi(request):
    b = await body(request)
    jid = await _pcall(pairing.start_wifipair, str(b.get("ip") or ""), int(b.get("port") or 0),
                       str(b.get("udid") or ""))
    return JSONResponse({"job": jid}, status_code=202)


@endpoint("full")
async def pairing_detail(request):
    pid = request.path_params["id"]
    if request.method == "DELETE":
        await run_in_threadpool(pairing.delete, pid)
        return JSONResponse({"ok": True})
    r = await _pcall(pairing._row, pid)
    return JSONResponse(pairing.public(r))


@endpoint("full")
async def pairing_file(request):
    name, data = await _pcall(pairing.file_bytes, request.path_params["id"])
    db.log_event("info", f"Pairing-Datei {name} heruntergeladen")
    return Response(data, media_type="application/x-plist", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@endpoint("full")
async def pairing_apps(request):
    q = request.query_params
    return JSONResponse({"apps": await _pcall(pairing.apps, request.path_params["id"], q.get("via", "usb"),
                                              q.get("ip", ""), q.get("all") == "1")})


@endpoint("full")
async def pairing_push(request):
    b = await body(request)
    res = await _pcall(pairing.push, request.path_params["id"], str(b.get("bundle_id") or ""),
                       str(b.get("path") or ""), b.get("via") or "usb", str(b.get("ip") or ""),
                       str(b.get("name") or ""))
    return JSONResponse(res)


@endpoint("full")
async def pairing_verify(request):
    b = await body(request)
    return JSONResponse(await _pcall(pairing.verify_wifi, request.path_params["id"], str(b.get("ip") or "")))


@endpoint("full")
async def pairing_renew(request):
    b = await body(request)
    return JSONResponse(await _pcall(pairing.renew_wifi, request.path_params["id"], str(b.get("ip") or "")))


# ================================================================= downloads
def _download_json(d, request):
    out = dict(d)
    if d.get("build_id"):
        bld = db.one("SELECT * FROM builds WHERE id = ?", d["build_id"])
        if bld:
            out["app"] = _app_json(db.one("SELECT * FROM apps WHERE id = ?", bld["app_id"]), request)
            out["build"] = {"id": bld["id"], "version": bld["version"], "build_version": bld["build_version"]}
    if d.get("repo_hint"):
        h = d["repo_hint"]
        out["repo_watched"] = bool(db.one("SELECT id FROM sources WHERE kind = ? AND host = ? AND lower(repo) = lower(?)",
                                          h["kind"], h["host"], h["repo"]))
    return out


@endpoint("upload")
async def downloads(request):
    """
    POST {url, notes?} or ?url=… (iOS shortcut) -> 202 {id, message, …}
    GET  -> recent downloads (scope read)
    """
    if request.method == "GET":
        if not request.state.principal.can("read"):
            raise ApiError(403, "forbidden", "Scope 'read' nötig")
        return JSONResponse({"downloads": [_download_json(d, request) for d in downloader.recent()]})
    url, notes = request.query_params.get("url", ""), request.query_params.get("notes", "")
    raw = await request.body()
    if raw:
        ctype = request.headers.get("content-type", "")
        if "json" in ctype:
            data = json.loads(raw)
            if isinstance(data, dict):
                url = str(data.get("url") or url)
                notes = str(data.get("notes") or notes)
        elif "form" in ctype:
            form = dict(urllib.parse.parse_qsl(raw.decode()))
            url, notes = form.get("url", url), form.get("notes", notes)
        else:
            url = url or raw.decode("utf-8", "replace")
    via = "shortcut" if "Shortcuts" in request.headers.get("user-agent", "") or request.query_params.get("via") == "shortcut" else \
        ("web" if request.state.principal.via == "session" else "api")
    d = await run_in_threadpool(downloader.enqueue, url, notes, via)
    out = _download_json(d, request)
    out["message"] = f"Download gestartet: {urllib.parse.urlparse(d['url']).netloc or d['url'][:40]}"
    return JSONResponse(out, status_code=202)


@endpoint("full")
async def download_detail(request):
    did = int(request.path_params["id"])
    d = downloader.get(did)
    if not d:
        raise ApiError(404, "not_found", "Download nicht gefunden")
    if request.method == "DELETE":
        downloader.cancel(did)
        if d["status"] not in ("wartet", "läuft"):
            db.execute("DELETE FROM downloads WHERE id = ?", did)
        return JSONResponse({"ok": True})
    return JSONResponse(_download_json(d, request))


@endpoint("full")
async def download_choose(request):
    data = await body(request)
    d = await run_in_threadpool(downloader.choose, int(request.path_params["id"]), str(data.get("url") or ""))
    return JSONResponse(_download_json(d, request), status_code=202)


@endpoint("full")
async def download_retry(request):
    did = int(request.path_params["id"])
    d = downloader.get(did)
    if not d:
        raise ApiError(404, "not_found", "Download nicht gefunden")
    db.execute("DELETE FROM downloads WHERE id = ?", did)
    n = await run_in_threadpool(downloader.enqueue, d["url"], d["notes"], d["via"])
    return JSONResponse(_download_json(n, request), status_code=202)


# ==================================================================== webhook
@endpoint(None)
async def webhook(request):
    raw = await request.body()
    secret = db.get_setting("webhook_secret")
    sig = request.headers.get("x-hub-signature-256", "")
    if not secret:
        return err(503, "no_secret", "Webhook-Secret nicht konfiguriert")
    calc = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, sig):
        return err(401, "bad_signature", "Signatur ungültig")
    event = request.headers.get("x-github-event", "")
    try:
        payload = json.loads(raw)
    except Exception:
        return err(400, "invalid_json", "Ungültiges JSON")
    repo = (payload.get("repository") or {}).get("full_name", "")
    if event == "ping":
        return JSONResponse({"ok": True, "pong": True})
    trigger = (
        (event == "release" and payload.get("action") in ("published", "released", "edited", "prereleased"))
        or (event == "workflow_run" and payload.get("action") == "completed")
    )
    if trigger and repo:
        sync.sync_repo(repo)
        db.log_event("info", f"Webhook {event} von {repo}: Sync angestoßen")
    return JSONResponse({"ok": True, "triggered": bool(trigger)})


# ===================================================================== routes
def routes():
    p = "/api/v1"
    return [
        Route(p + "/info", info, methods=["GET"]),
        Route(p + "/setup", setup, methods=["POST"]),
        Route(p + "/auth/login", login, methods=["POST"]),
        Route(p + "/auth/token", token_login, methods=["POST"]),
        Route(p + "/auth/logout", logout, methods=["POST"]),
        Route(p + "/me", me, methods=["GET"]),
        Route(p + "/me/password", change_password, methods=["PUT"]),
        Route(p + "/tokens", tokens, methods=["GET", "POST"]),
        Route(p + "/tokens/{id:int}", token_delete, methods=["DELETE"]),
        Route(p + "/settings", settings, methods=["GET", "PATCH"]),
        Route(p + "/feed", feed_urls, methods=["GET"]),
        Route(p + "/feed/rotate-key", feed_rotate, methods=["POST"]),
        Route(p + "/apps", apps, methods=["GET"]),
        Route(p + "/apps/{id:int}", app_detail, methods=["GET", "PATCH", "DELETE"]),
        Route(p + "/apps/{id:int}/icon", app_icon, methods=["PUT"]),
        Route(p + "/apps/{id:int}/custom-icon", app_custom_icon, methods=["PUT", "DELETE"]),
        Route(p + "/apps/{id:int}/inject-icon", app_inject_icon, methods=["POST"]),
        Route(p + "/tools/fetch-image", fetch_image, methods=["GET"]),
        Route(p + "/builds/{id:int}", build_detail, methods=["PATCH", "DELETE"]),
        Route(p + "/sources", sources, methods=["GET", "POST"]),
        Route(p + "/sources/{id:int}", source_detail, methods=["GET", "PATCH", "DELETE"]),
        Route(p + "/sources/{id:int}/sync", source_sync, methods=["POST"]),
        Route(p + "/sync", sync_all, methods=["POST"]),
        Route(p + "/status", status, methods=["GET"]),
        Route(p + "/events", events, methods=["GET"]),
        Route(p + "/github", gh_status, methods=["GET"]),
        Route(p + "/github/token", gh_token, methods=["PUT", "DELETE"]),
        Route(p + "/github/device/start", gh_device_start, methods=["POST"]),
        Route(p + "/github/device/poll", gh_device_poll, methods=["POST"]),
        Route(p + "/github/repos", gh_repos, methods=["GET"]),
        Route(p + "/github/repos/{owner}/{repo}/scan", gh_scan, methods=["GET"]),
        Route(p + "/uploads", upload, methods=["POST", "PUT"]),
        Route(p + "/uploads/sessions", upload_session_create, methods=["POST"]),
        Route(p + "/uploads/sessions/{sid}", upload_session, methods=["GET", "PUT", "DELETE"]),
        Route(p + "/uploads/sessions/{sid}/complete", upload_session_complete, methods=["POST"]),
        Route(p + "/webhooks/github", webhook, methods=["POST"]),
        Route(p + "/downloads", downloads, methods=["GET", "POST"]),
        Route(p + "/downloads/{id:int}", download_detail, methods=["GET", "DELETE"]),
        Route(p + "/downloads/{id:int}/choose", download_choose, methods=["POST"]),
        Route(p + "/downloads/{id:int}/retry", download_retry, methods=["POST"]),
        Route(p + "/certs", certs_list, methods=["GET", "POST"]),
        Route(p + "/certs/{id:int}", cert_detail, methods=["PATCH", "DELETE"]),
        Route(p + "/builds/{id:int}/sign", build_sign, methods=["POST"]),
        Route(p + "/signed", signed_list, methods=["GET"]),
        Route(p + "/signed/{id:int}", signed_detail, methods=["GET", "DELETE"]),
        Route(p + "/signed/{id:int}/install-usb", signed_install_usb, methods=["POST"]),
        Route(p + "/jobs/{jid}", install_job, methods=["GET"]),
        Route(p + "/devices", devices, methods=["GET"]),
        Route(p + "/devices/{udid}/pair", device_pair, methods=["POST"]),
        Route(p + "/pairing", pairing_status, methods=["GET"]),
        Route(p + "/pairing/usb", pairing_usb, methods=["POST"]),
        Route(p + "/pairing/wifi", pairing_wifi, methods=["POST"]),
        Route(p + "/pairing/discover", pairing_discover, methods=["GET"]),
        Route(p + "/pairing/jobs/{jid}", pairing_job, methods=["GET"]),
        Route(p + "/pairing/jobs/{jid}/pin", pairing_job_pin, methods=["POST"]),
        Route(p + "/pairing/{id:int}", pairing_detail, methods=["GET", "DELETE"]),
        Route(p + "/pairing/{id:int}/file", pairing_file, methods=["GET"]),
        Route(p + "/pairing/{id:int}/apps", pairing_apps, methods=["GET"]),
        Route(p + "/pairing/{id:int}/push", pairing_push, methods=["POST"]),
        Route(p + "/pairing/{id:int}/verify", pairing_verify, methods=["POST"]),
        Route(p + "/pairing/{id:int}/renew", pairing_renew, methods=["POST"]),
    ]
