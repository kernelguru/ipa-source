"""ipa-source – self-hosted AltStore/Feather source with web admin, API and WebDAV."""
import struct
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import api, auth, config, db, feed, signing, store, sync, webdav

STATIC = Path(__file__).parent / "static"


def _default_icon(tint="#5C7AEA") -> bytes:
    c = feed.hexcolor(tint).lstrip("#")
    rgb = bytes(int(c[i:i + 2], 16) for i in (0, 2, 4))
    w = h = 512
    raw = (b"\x00" + rgb * w) * h

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


# ------------------------------------------------------------ public feed --
def _key_ok(request) -> bool:
    """Public resources: if the feed is private, the /s/<key>/ prefix is required."""
    key = request.path_params.get("key")
    if db.get_setting("feed_private") == "1":
        return bool(key) and key == db.get_setting("feed_key")
    return key is None or key == db.get_setting("feed_key")


def _json(data):
    import json
    return Response(json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})


async def apps_json(request: Request):
    if not _key_ok(request):
        return Response(status_code=404)
    return _json(await run_in_threadpool(feed.build_source, request, False))


async def apps_legacy_json(request: Request):
    if not _key_ok(request):
        return Response(status_code=404)
    return _json(await run_in_threadpool(feed.build_source, request, True))


async def download(request: Request):
    if not _key_ok(request):
        return Response(status_code=404)
    b = db.one("SELECT * FROM builds WHERE id = ?", int(request.path_params["id"]))
    if not b:
        return Response(status_code=404)
    f = store.build_file(b)
    if not f.exists():
        return Response(status_code=410)
    return FileResponse(f, filename=b["filename"], media_type="application/octet-stream")


async def icon(request: Request):
    if not _key_ok(request):
        return Response(status_code=404)
    a = db.one("SELECT * FROM apps WHERE id = ?", int(request.path_params["id"]))
    if not a or not store.icon_path(a["bundle_id"]).exists():
        return Response(_default_icon(db.get_setting("feed_tint")), media_type="image/png")
    return FileResponse(store.icon_path(a["bundle_id"]), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=3600"})


async def default_icon(request: Request):
    return Response(_default_icon(db.get_setting("feed_tint")), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


def _asset_version() -> str:
    """Changes whenever a static file changes -> browsers fetch fresh JS/CSS after an update."""
    import hashlib
    h = hashlib.sha256(config.VERSION.encode())
    for f in sorted(STATIC.glob("*")):
        h.update(f.name.encode()); h.update(str(f.stat().st_mtime_ns).encode()); h.update(str(f.stat().st_size).encode())
    return h.hexdigest()[:10]


async def index(request: Request):
    v = _asset_version()
    html = (STATIC / "index.html").read_text()
    html = (html.replace("/static/app.js", f"/static/app.js?v={v}")
                .replace("/static/style.css", f"/static/style.css?v={v}")
                .replace("__VERSION__", config.VERSION))
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-store"})


async def health(request: Request):
    return JSONResponse({"ok": True, "version": config.VERSION})


async def openapi(request: Request):
    return FileResponse(Path(__file__).parent.parent / "docs" / "openapi.yaml", media_type="application/yaml")


# ------------------------------------------------------ OTA installation --
def _signed_or_404(request):
    s = signing.by_token(request.path_params["token"])
    if not s or not (config.SIGNED_DIR / s["path"]).exists():
        return None
    return s


def _fmt_size(n: int) -> str:
    return f"{n / 2**30:.2f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB" if n >= 2**20 else f"{max(1, n // 1024)} KB"


async def install_page(request: Request):
    s = _signed_or_404(request)
    if not s:
        return Response("Link ungültig oder abgelaufen", status_code=404)
    info = signing.public(s, feed.base_url(request))
    import html as _h
    e = _h.escape
    https = info["ota_ready"]
    body = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(info['app_name'])} installieren</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;max-width:420px;margin:12vh auto;padding:0 20px;text-align:center;color:#111}}
img{{width:110px;height:110px;border-radius:24px;box-shadow:0 4px 18px rgba(0,0,0,.2)}}
a.b{{display:block;margin:22px 0 10px;padding:15px;border-radius:14px;background:#5c7aea;color:#fff;text-decoration:none;font-weight:600;font-size:17px}}
.m{{color:#777;font-size:14px}} @media(prefers-color-scheme:dark){{body{{background:#000;color:#eee}}}}</style></head><body>
<img src="/install/{e(s['token'])}/icon.png" alt="">
<h2>{e(info['app_name'])}</h2><div class="m">Version {e(info['version'])} ({e(info['build_version'])}) · {_fmt_size(info['size'])}<br>signiert mit „{e(info['cert_name'])}“</div>
{f'<a class="b" href="{e(info["itms_url"])}">Installieren</a>' if https else '<p><b>OTA-Installation braucht HTTPS.</b> Bitte BASE_URL auf https:// setzen.</p>'}
<div class="m">Nach dem Tippen auf „Installieren“ fragt iOS nach – die App lädt dann im Hintergrund auf den Homescreen.
Große Apps brauchen WLAN und etwas Geduld.</div>
<p class="m"><a href="/install/{e(s['token'])}/app.ipa">IPA herunterladen</a></p></body></html>"""
    return Response(body, media_type="text/html", headers={"Cache-Control": "no-store"})


async def install_manifest(request: Request):
    s = _signed_or_404(request)
    if not s:
        return Response(status_code=404)
    return Response(signing.manifest(s, feed.base_url(request)), media_type="application/xml",
                    headers={"Cache-Control": "no-store"})


async def install_ipa(request: Request):
    s = _signed_or_404(request)
    if not s:
        return Response(status_code=404)
    return FileResponse(config.SIGNED_DIR / s["path"], media_type="application/octet-stream",
                        filename=f"{s['bundle_id'] or 'app'}.ipa")


async def install_icon(request: Request):
    s = _signed_or_404(request)
    b = db.one("SELECT app_id FROM builds WHERE id = ?", s["build_id"]) if s else None
    a = db.one("SELECT * FROM apps WHERE id = ?", b["app_id"]) if b else None
    if a and store.icon_path(a["bundle_id"]).exists():
        return FileResponse(store.icon_path(a["bundle_id"]), media_type="image/png")
    return Response(_default_icon(db.get_setting("feed_tint")), media_type="image/png")


def public_routes(prefix=""):
    return [
        Route(prefix + "/apps.json", apps_json),
        Route(prefix + "/apps-legacy.json", apps_legacy_json),
        Route(prefix + "/dl/{id:int}/{filename}", download, methods=["GET", "HEAD"]),
        Route(prefix + "/icons/{id:int}.png", icon),
    ]


@asynccontextmanager
async def lifespan(app):
    db.init()
    auth.bootstrap_admin()
    try:
        store.recheck_icons()
    except Exception as e:
        db.log_event("error", f"Icon-Prüfung: {e}")
    sync.start()
    db.log_event("info", f"ipa-source {config.VERSION} gestartet, Daten in {config.DATA_DIR}")
    yield


DAV_METHODS = ["OPTIONS", "PROPFIND", "PROPPATCH", "GET", "HEAD", "PUT", "DELETE", "MOVE", "COPY",
               "MKCOL", "LOCK", "UNLOCK"]

app = Starlette(
    routes=[
        Route("/", index),
        Route("/healthz", health),
        Route("/default-icon.png", default_icon),
        Route("/api/v1/openapi.yaml", openapi),
        *api.routes(),
        *public_routes(),
        *public_routes("/s/{key}"),
        Route("/install/{token}", install_page),
        Route("/install/{token}/manifest.plist", install_manifest),
        Route("/install/{token}/app.ipa", install_ipa, methods=["GET", "HEAD"]),
        Route("/install/{token}/icon.png", install_icon),
        Route("/dav", lambda r: RedirectResponse("/dav/", status_code=301), methods=["GET", "HEAD"]),
        Route("/dav/{path:path}", webdav.handler, methods=DAV_METHODS),
        Route("/dav", webdav.handler, methods=[m for m in DAV_METHODS if m not in ("GET", "HEAD")]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ],
    lifespan=lifespan,
)
