"""
Resumable chunked uploads for large IPAs (multi-GB).

Flow:
  POST   /api/v1/uploads/sessions                 {filename, size, notes?, visible?} -> {id, chunk_size, received}
  PUT    /api/v1/uploads/sessions/{id}?offset=N   raw chunk body                     -> {received}
  GET    /api/v1/uploads/sessions/{id}            state (use `received` to resume)
  POST   /api/v1/uploads/sessions/{id}/complete   -> 202, import runs in background; poll GET until status=done|error
  DELETE /api/v1/uploads/sessions/{id}            abort

Every chunk is a small request, so reverse proxies (body limits, timeouts) are not a problem,
and an interrupted upload continues at `received`.
"""
import json
import shutil
import threading
import time
import uuid

from . import config, db, store

CHUNK_SIZE = int(__import__("os").environ.get("UPLOAD_CHUNK_MB", "32")) * 1024 * 1024
SESSION_TTL = 24 * 3600
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _dir(sid: str):
    return config.TMP_DIR / "uploads" / sid


def _lock(sid):
    with _locks_guard:
        return _locks.setdefault(sid, threading.Lock())


def _load(sid: str):
    if not sid or not sid.isalnum():
        return None
    f = _dir(sid) / "meta.json"
    if not f.exists():
        return None
    meta = json.loads(f.read_text())
    part = _dir(sid) / "data.part"
    meta["received"] = part.stat().st_size if part.exists() else 0
    return meta


def _save(meta):
    f = _dir(meta["id"]) / "meta.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: v for k, v in meta.items() if k != "received"}))
    tmp.replace(f)


def public(meta):
    return {k: meta.get(k) for k in ("id", "filename", "size", "received", "status", "error",
                                    "build_id", "app_id", "chunk_size", "created_at", "updated_at")}


def check_space(size: int):
    free = shutil.disk_usage(config.DATA_DIR).free
    if size * 1.05 + 100 * 1024 * 1024 > free:
        raise ValueError(f"Nicht genug Speicherplatz: {free // 2**20} MB frei, {size // 2**20} MB benötigt")


def create(filename: str, size: int, notes: str = "", visible: bool = True, user_id=None):
    if size <= 0:
        raise ValueError("size fehlt oder ist 0")
    if config.MAX_UPLOAD_BYTES and size > config.MAX_UPLOAD_BYTES:
        raise ValueError(f"Datei zu groß: Limit ist {config.MAX_UPLOAD_BYTES // 2**20} MB (MAX_UPLOAD_MB)")
    check_space(size)
    sid = uuid.uuid4().hex
    _dir(sid).mkdir(parents=True)
    (_dir(sid) / "data.part").touch()
    meta = {"id": sid, "filename": filename or "upload.ipa", "size": int(size), "notes": notes or "",
            "visible": bool(visible), "user_id": user_id, "status": "uploading", "error": "",
            "build_id": None, "app_id": None, "chunk_size": CHUNK_SIZE,
            "created_at": db.now(), "updated_at": db.now()}
    _save(meta)
    meta["received"] = 0
    return meta


def get(sid: str):
    return _load(sid)


class OffsetMismatch(Exception):
    def __init__(self, received):
        self.received = received


async def append(sid: str, offset: int, stream):
    """Append a chunk at `offset`. The chunk is written to a side file first, so a broken
    request never leaves a half-written chunk in the main file."""
    meta = _load(sid)
    if not meta:
        return None
    if meta["status"] != "uploading":
        raise ValueError(f"Upload ist bereits im Status '{meta['status']}'")
    lock = _lock(sid)
    if not lock.acquire(blocking=False):
        raise OffsetMismatch(meta["received"])
    try:
        if offset != meta["received"]:
            raise OffsetMismatch(meta["received"])
        chunk_file = _dir(sid) / "chunk.tmp"
        n = 0
        with open(chunk_file, "wb") as f:
            async for piece in stream:
                n += len(piece)
                if offset + n > meta["size"]:
                    raise ValueError("Mehr Daten als angekündigt (size)")
                f.write(piece)
        with open(_dir(sid) / "data.part", "ab") as out, open(chunk_file, "rb") as inp:
            shutil.copyfileobj(inp, out, length=8 * 1024 * 1024)
        chunk_file.unlink(missing_ok=True)
        meta["updated_at"] = db.now()
        _save(meta)
        meta["received"] = offset + n
        return meta
    finally:
        (_dir(sid) / "chunk.tmp").unlink(missing_ok=True)
        lock.release()


def complete(sid: str):
    meta = _load(sid)
    if not meta:
        return None
    if meta["status"] in ("processing", "done"):
        return meta
    if meta["received"] != meta["size"]:
        raise ValueError(f"Upload unvollständig: {meta['received']} von {meta['size']} Bytes")
    meta["status"] = "processing"
    meta["updated_at"] = db.now()
    _save(meta)
    threading.Thread(target=_finalize, args=(sid,), daemon=True, name=f"upload-{sid[:6]}").start()
    return meta


def _finalize(sid: str):
    meta = _load(sid)
    try:
        build = store.import_ipa(_dir(sid) / "data.part", origin="upload", filename=meta["filename"],
                                 notes=meta["notes"], move=True)
        if not meta["visible"]:
            db.execute("UPDATE builds SET visible = 0 WHERE id = ?", build["id"])
        meta.update(status="done", build_id=build["id"], app_id=build["app_id"])
    except Exception as e:
        meta.update(status="error", error=f"IPA konnte nicht gelesen werden: {e}")
        db.log_event("error", f"Upload {meta['filename']}: {e}")
        (_dir(sid) / "data.part").unlink(missing_ok=True)
    meta["updated_at"] = db.now()
    _save(meta)


def abort(sid: str):
    if not _load(sid):
        return False
    shutil.rmtree(_dir(sid), ignore_errors=True)
    return True


def cleanup():
    """Remove sessions without activity for SESSION_TTL (finished ones after 1 h)."""
    root = config.TMP_DIR / "uploads"
    if not root.exists():
        return
    now = time.time()
    for d in root.iterdir():
        try:
            meta_f = d / "meta.json"
            mtime = max((p.stat().st_mtime for p in d.iterdir()), default=0)
            status = json.loads(meta_f.read_text()).get("status") if meta_f.exists() else None
            ttl = 3600 if status in ("done", "error") else SESSION_TTL
            if now - mtime > ttl:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            continue


def resume_pending():
    """After a restart: finish imports that were interrupted while processing."""
    root = config.TMP_DIR / "uploads"
    if not root.exists():
        return
    for d in root.iterdir():
        meta = _load(d.name)
        if meta and meta["status"] == "processing" and (d / "data.part").exists():
            threading.Thread(target=_finalize, args=(d.name,), daemon=True).start()
