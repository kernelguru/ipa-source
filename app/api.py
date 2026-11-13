"""REST API /api/v1 – used by the web admin and (later) the iOS app."""
import functools
import hashlib
import hmac
import json
import secrets
import time
import uuid

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import auth, config, db, feed, github, store, sync
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
        "features": ["github", "github_device_flow", "upload", "webdav", "webhook", "legacy_feed"],
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
        "icon": a["icon_url"] or (f"{pub}/icons/{a['id']}.png" if a["has_icon"] else None),
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
    for k in ("visible",):
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
    db.execute("UPDATE apps SET has_icon = 1, icon_url = '', updated_at = ? WHERE id = ?", db.now(), aid)
    return JSONResponse(_app_json(db.one("SELECT * FROM apps WHERE id = ?", aid), request))


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
SOURCE_FIELDS = {"repo": str, "use_releases": b, "use_artifacts": b, "include_prereleases": b,
                 "asset_pattern": str, "artifact_name": str, "artifact_branch": str,
                 "keep_releases": int, "keep_artifacts": int, "enabled": b}


def _source_json(s):
    n = db.one("SELECT COUNT(*) AS n FROM builds WHERE source_id = ?", s["id"])["n"]
    out = dict(s)
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
        out["repo"] = out["repo"].strip().removeprefix("https://github.com/").strip("/")
        if out["repo"].count("/") != 1:
            raise ApiError(400, "invalid", "repo muss 'owner/name' sein")
    elif not partial:
        raise ApiError(400, "invalid", "repo fehlt")
    for k in ("asset_pattern",):
        if k in out and not out[k]:
            out[k] = "*.ipa"
    return out


@endpoint("full")
async def sources(request):
    if request.method == "GET":
        return JSONResponse({"sources": [_source_json(s) for s in db.all_("SELECT * FROM sources ORDER BY repo")]})
    data = _clean_source(await body(request))
    if db.one("SELECT id FROM sources WHERE kind='github' AND lower(repo)=lower(?)", data["repo"]):
        raise ApiError(409, "exists", "Quelle existiert bereits")
    cols = ["kind", "created_at", *data.keys()]
    sid = db.execute(f"INSERT INTO sources({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                     "github", db.now(), *data.values())
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
    data = _clean_source(await body(request), partial=True)
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
            if size > config.MAX_UPLOAD_BYTES:
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
        Route(p + "/webhooks/github", webhook, methods=["POST"]),
    ]
