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

from . import api, auth, config, db, feed, store, sync, webdav

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


async def index(request: Request):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def health(request: Request):
    return JSONResponse({"ok": True, "version": config.VERSION})


async def openapi(request: Request):
    return FileResponse(Path(__file__).parent.parent / "docs" / "openapi.yaml", media_type="application/yaml")


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
        Route("/dav", lambda r: RedirectResponse("/dav/", status_code=301), methods=["GET", "HEAD"]),
        Route("/dav/{path:path}", webdav.handler, methods=DAV_METHODS),
        Route("/dav", webdav.handler, methods=[m for m in DAV_METHODS if m not in ("GET", "HEAD")]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ],
    lifespan=lifespan,
)
