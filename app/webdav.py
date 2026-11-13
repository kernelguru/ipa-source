"""
Minimal WebDAV (class 1 + fake locks, enough for Finder, Windows, rclone, Cyberduck, iOS apps).

Layout under /dav/:
  /dav/                      root: one folder per app + files currently in the inbox
  /dav/<App>/                builds of that app (read-only, deletable)
  PUT /dav/<file>.ipa        drop an IPA -> imported automatically
"""
import email.utils
import re
import urllib.parse
import uuid
from datetime import datetime
from xml.sax.saxutils import escape

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from . import auth, config, db, store, sync

PREFIX = "/dav"


def _http_date(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now()
    return email.utils.format_datetime(dt, usegmt=True) if dt.tzinfo else email.utils.format_datetime(dt)


def _folder_name(a, taken):
    n = re.sub(r"[/\\:]+", "_", a["name"]).strip() or a["bundle_id"]
    if n in taken:
        n = f"{n} ({a['bundle_id']})"
    taken.add(n)
    return n


def _build_name(b):
    return f"{b['version']} ({b['build_version']}) #{b['id']}.ipa"


def _tree():
    taken, folders = set(), {}
    for a in db.all_("SELECT * FROM apps ORDER BY name COLLATE NOCASE"):
        folders[_folder_name(a, taken)] = a
    return folders


def _inbox_files():
    return [f for f in sorted(config.INBOX_DIR.iterdir()) if f.is_file() and not f.name.startswith(".ipas-")
            and not f.name.endswith(".done")]


def _resolve(parts):
    """-> ('root'|'app'|'build'|'inbox'|None, obj)"""
    if not parts:
        return "root", None
    folders = _tree()
    if len(parts) == 1:
        if parts[0] in folders:
            return "app", folders[parts[0]]
        f = config.INBOX_DIR / parts[0]
        if f.is_file():
            return "inbox", f
        return None, parts[0]
    if len(parts) == 2 and parts[0] in folders:
        a = folders[parts[0]]
        m = re.search(r"#(\d+)\.ipa$", parts[1])
        if m:
            b = db.one("SELECT * FROM builds WHERE id = ? AND app_id = ?", int(m.group(1)), a["id"])
            if b:
                return "build", b
    return None, None


def _response(href, *, collection, size=0, mtime=None, ctype="application/octet-stream"):
    mtime = mtime or db.now()
    props = [f"<D:displayname>{escape(urllib.parse.unquote(href.rstrip('/').rsplit('/', 1)[-1]) or 'ipa-source')}</D:displayname>",
             f"<D:getlastmodified>{_http_date(mtime)}</D:getlastmodified>",
             f"<D:creationdate>{escape(mtime)}</D:creationdate>"]
    if collection:
        props.append("<D:resourcetype><D:collection/></D:resourcetype>")
    else:
        props += ["<D:resourcetype/>", f"<D:getcontentlength>{size}</D:getcontentlength>",
                  f"<D:getcontenttype>{ctype}</D:getcontenttype>",
                  f"<D:getetag>\"{size}-{escape(mtime)}\"</D:getetag>"]
    props.append("<D:supportedlock><D:lockentry><D:lockscope><D:exclusive/></D:lockscope>"
                 "<D:locktype><D:write/></D:locktype></D:lockentry></D:supportedlock>")
    return (f"<D:response><D:href>{escape(href)}</D:href><D:propstat><D:prop>{''.join(props)}</D:prop>"
            f"<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


def _q(name):
    return urllib.parse.quote(name)


def _propfind(parts, depth):
    kind, obj = _resolve(parts)
    out = []
    if kind == "root":
        out.append(_response(PREFIX + "/", collection=True))
        if depth != "0":
            for name, a in _tree().items():
                out.append(_response(f"{PREFIX}/{_q(name)}/", collection=True, mtime=a["updated_at"]))
            for f in _inbox_files():
                st = f.stat()
                out.append(_response(f"{PREFIX}/{_q(f.name)}", collection=False, size=st.st_size,
                                     mtime=datetime.fromtimestamp(st.st_mtime).isoformat()))
    elif kind == "app":
        out.append(_response(f"{PREFIX}/{_q(parts[0])}/", collection=True, mtime=obj["updated_at"]))
        if depth != "0":
            for b in db.all_("SELECT * FROM builds WHERE app_id = ? ORDER BY date DESC", obj["id"]):
                out.append(_response(f"{PREFIX}/{_q(parts[0])}/{_q(_build_name(b))}", collection=False,
                                     size=b["size"], mtime=b["date"]))
    elif kind == "build":
        out.append(_response(f"{PREFIX}/{_q(parts[0])}/{_q(parts[1])}", collection=False,
                             size=obj["size"], mtime=obj["date"]))
    elif kind == "inbox":
        st = obj.stat()
        out.append(_response(f"{PREFIX}/{_q(obj.name)}", collection=False, size=st.st_size,
                             mtime=datetime.fromtimestamp(st.st_mtime).isoformat()))
    else:
        return Response(status_code=404)
    xml = '<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">' + "".join(out) + "</D:multistatus>"
    return Response(xml, status_code=207, media_type="application/xml; charset=utf-8")


def _unauthorized():
    return Response("Unauthorized", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="ipa-source WebDAV"'})


async def handler(request: Request):
    p = await run_in_threadpool(auth.authenticate, request)
    if not p:
        return _unauthorized()
    m = request.method
    write = m in ("PUT", "DELETE", "MOVE", "COPY", "MKCOL", "PROPPATCH", "LOCK", "UNLOCK")
    if write and not (p.can("full") or (p.can("upload") and m in ("PUT", "LOCK", "UNLOCK", "PROPPATCH"))):
        return Response(status_code=403)
    if not write and not (p.can("read") or p.can("upload")):
        return Response(status_code=403)

    path = request.scope["path"][len(PREFIX):]  # already percent-decoded; url.path would cut at "#"
    parts = [x for x in path.split("/") if x]
    if any(x in (".", "..") for x in parts):
        return Response(status_code=400)

    if m == "OPTIONS":
        return Response(headers={"DAV": "1, 2", "MS-Author-Via": "DAV",
                                 "Allow": "OPTIONS, PROPFIND, GET, HEAD, PUT, DELETE, MOVE, LOCK, UNLOCK, PROPPATCH"})

    if m == "PROPFIND":
        return await run_in_threadpool(_propfind, parts, request.headers.get("depth", "1"))

    if m in ("GET", "HEAD"):
        kind, obj = await run_in_threadpool(_resolve, parts)
        if kind == "build":
            return FileResponse(store.build_file(obj), filename=_build_name(obj),
                                media_type="application/octet-stream")
        if kind == "inbox":
            return FileResponse(obj, media_type="application/octet-stream")
        if kind in ("root", "app"):
            return Response("WebDAV-Ordner – bitte mit einem WebDAV-Client öffnen.\n", media_type="text/plain")
        return Response(status_code=404)

    if m == "PUT":
        if len(parts) != 1:
            return Response("IPAs bitte direkt in den Hauptordner legen", status_code=403)
        name = re.sub(r"[/\\]+", "_", parts[0])
        dest = config.INBOX_DIR / name
        tmp = config.INBOX_DIR / f".ipas-{uuid.uuid4().hex}.part"
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        tmp.replace(dest)
        (config.INBOX_DIR / f".{name}.done").unlink(missing_ok=True)
        sync.enqueue("inbox")
        return Response(status_code=201)

    if m == "DELETE":
        kind, obj = await run_in_threadpool(_resolve, parts)
        if kind == "build":
            await run_in_threadpool(store.delete_build, obj["id"])
            return Response(status_code=204)
        if kind == "inbox":
            obj.unlink(missing_ok=True)
            return Response(status_code=204)
        if kind == "app":
            return Response("Apps bitte über die Weboberfläche löschen", status_code=403)
        return Response(status_code=404)

    if m == "MOVE":
        kind, obj = await run_in_threadpool(_resolve, parts)
        dest_hdr = urllib.parse.urlparse(request.headers.get("destination", "")).path
        dest_parts = [x for x in urllib.parse.unquote(dest_hdr[len(PREFIX):]).split("/") if x]
        if kind == "inbox" and len(dest_parts) == 1:
            obj.rename(config.INBOX_DIR / re.sub(r"[/\\]+", "_", dest_parts[0]))
            sync.enqueue("inbox")
            return Response(status_code=201)
        return Response(status_code=403)

    if m == "MKCOL":
        return Response(status_code=405)

    if m == "LOCK":
        token = f"opaquelocktoken:{uuid.uuid4()}"
        xml = ('<?xml version="1.0" encoding="utf-8"?><D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
               '<D:locktype><D:write/></D:locktype><D:lockscope><D:exclusive/></D:lockscope>'
               f'<D:depth>0</D:depth><D:timeout>Second-3600</D:timeout><D:locktoken><D:href>{token}</D:href>'
               '</D:locktoken></D:activelock></D:lockdiscovery></D:prop>')
        return Response(xml, status_code=200, media_type="application/xml; charset=utf-8",
                        headers={"Lock-Token": f"<{token}>"})

    if m == "UNLOCK":
        return Response(status_code=204)

    if m == "PROPPATCH":
        href = urllib.parse.quote(request.scope["path"])
        xml = ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:"><D:response>'
               f'<D:href>{escape(href)}</D:href><D:propstat><D:prop/><D:status>HTTP/1.1 200 OK</D:status>'
               '</D:propstat></D:response></D:multistatus>')
        return Response(xml, status_code=207, media_type="application/xml; charset=utf-8")

    return Response(status_code=405)
