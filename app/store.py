"""Importing IPAs into storage, app/build bookkeeping, retention."""
import json
import queue
import re
import shutil
import threading
import uuid
from pathlib import Path

from . import config, db, ipa, pngutil
from .ipa import IpaInfo, sha256_file


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._") or "app"


def app_dir(bundle_id: str) -> Path:
    return config.STORAGE_DIR / safe_name(bundle_id)


def icon_path(bundle_id: str) -> Path:
    return app_dir(bundle_id) / "icon.png"


def build_file(build) -> Path:
    return config.STORAGE_DIR / build["path"]


def find_build(source_id, origin, origin_ref):
    if source_id is None:
        return db.one(
            "SELECT * FROM builds WHERE source_id IS NULL AND origin = ? AND origin_ref = ?",
            origin, origin_ref,
        )
    return db.one(
        "SELECT * FROM builds WHERE source_id = ? AND origin = ? AND origin_ref = ?",
        source_id, origin, origin_ref,
    )


def get_or_create_app(info: IpaInfo):
    a = db.one("SELECT * FROM apps WHERE bundle_id = ?", info.bundle_id)
    if a:
        return a
    ts = db.now()
    db.execute(
        "INSERT INTO apps(bundle_id, name, developer, created_at, updated_at) VALUES(?,?,?,?,?)",
        info.bundle_id, info.name, db.get_setting("developer"), ts, ts,
    )
    db.log_event("info", f"Neue App angelegt: {info.name} ({info.bundle_id})")
    return db.one("SELECT * FROM apps WHERE bundle_id = ?", info.bundle_id)


def import_ipa(src: Path, *, origin: str, origin_ref: str = None, source_id=None,
               date: str = None, notes: str = "", filename: str = None, move=True):
    """
    Parse and store an IPA. Returns the build row (existing one if already imported).
    `src` is consumed when move=True.
    """
    origin_ref = origin_ref or uuid.uuid4().hex
    existing = find_build(source_id, origin, origin_ref)
    if existing:
        if move:
            Path(src).unlink(missing_ok=True)
        return existing

    info = IpaInfo(src)
    app = get_or_create_app(info)
    date = date or db.now()

    filename = safe_name(filename or f"{info.name}-{info.version}-{info.build_version}.ipa")
    if not filename.lower().endswith(".ipa"):
        filename += ".ipa"
    rel = Path(safe_name(info.bundle_id)) / uuid.uuid4().hex[:12] / filename
    dest = config.STORAGE_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(str(src), dest)
    else:
        shutil.copyfile(src, dest)

    size = dest.stat().st_size
    sha = sha256_file(dest)

    # Icon: take it from the newest build (unless the user designed one)
    newest = db.one("SELECT MAX(date) AS d FROM builds WHERE app_id = ?", app["id"])["d"]
    if not app["icon_custom"] and (not newest or date >= newest or not icon_path(info.bundle_id).exists()):
        if info.icon:
            icon_path(info.bundle_id).parent.mkdir(parents=True, exist_ok=True)
            icon_path(info.bundle_id).write_bytes(info.icon)
        db.execute("UPDATE apps SET has_icon = ?, icon_blank = ?, updated_at = ? WHERE id = ?",
                   1 if info.icon else 0, 1 if info.icon_blank else 0, db.now(), app["id"])

    bid = db.execute(
        """INSERT INTO builds(app_id, source_id, origin, origin_ref, version, build_version, min_os,
                              size, sha256, date, notes, filename, path, permissions, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        app["id"], source_id, origin, origin_ref, info.version, info.build_version, info.min_os,
        size, sha, date, notes or "", filename, str(rel), json.dumps(info.permissions), db.now(),
    )
    db.log_event("info", f"Import {origin}: {info.name} {info.version} ({info.build_version})")
    if app["auto_inject"] and app["icon_custom"]:
        queue_inject([bid])
    return db.one("SELECT * FROM builds WHERE id = ?", bid)


def delete_build(build_id: int):
    b = db.one("SELECT * FROM builds WHERE id = ?", build_id)
    if not b:
        return False
    f = build_file(b)
    shutil.rmtree(f.parent, ignore_errors=True)
    db.execute("DELETE FROM builds WHERE id = ?", build_id)
    return True


def delete_app(app_id: int):
    a = db.one("SELECT * FROM apps WHERE id = ?", app_id)
    if not a:
        return False
    shutil.rmtree(app_dir(a["bundle_id"]), ignore_errors=True)
    db.execute("DELETE FROM apps WHERE id = ?", app_id)
    return True


def apply_retention(source):
    """Keep the newest N releases / M artifacts of a source (pinned builds are kept)."""
    removed = 0
    for origin, keep in (("release", source["keep_releases"]), ("artifact", source["keep_artifacts"])):
        rows = db.all_(
            "SELECT id FROM builds WHERE source_id = ? AND origin = ? AND pinned = 0 ORDER BY date DESC",
            source["id"], origin,
        )
        for r in rows[max(0, int(keep)):]:
            delete_build(r["id"])
            removed += 1
    return removed


# --------------------------------------------------------------- icons --
def custom_icon_dir(bundle_id: str) -> Path:
    return app_dir(bundle_id) / "custom-icon"


def save_custom_icon(app, pngs: dict):
    """pngs: pixel size -> PNG bytes (all of ipa.ICON_SIZES)."""
    for px in ipa.ICON_SIZES:
        data = pngs.get(px)
        if not data or pngutil.size(data) != (px, px):
            raise ValueError(f"Icon {px}×{px} px fehlt oder hat falsche Größe")
    d = custom_icon_dir(app["bundle_id"])
    d.mkdir(parents=True, exist_ok=True)
    for px in ipa.ICON_SIZES:
        (d / f"{px}.png").write_bytes(pngs[px])
    icon_path(app["bundle_id"]).write_bytes(pngs[1024])
    db.execute("UPDATE apps SET icon_custom = 1, has_icon = 1, icon_blank = 0, icon_url = '', updated_at = ? "
               "WHERE id = ?", db.now(), app["id"])


def load_custom_icon(app) -> dict:
    d = custom_icon_dir(app["bundle_id"])
    return {px: (d / f"{px}.png").read_bytes() for px in ipa.ICON_SIZES if (d / f"{px}.png").exists()}


def remove_custom_icon(app):
    shutil.rmtree(custom_icon_dir(app["bundle_id"]), ignore_errors=True)
    icon_path(app["bundle_id"]).unlink(missing_ok=True)
    has, blank = 0, 0
    latest = db.one("SELECT * FROM builds WHERE app_id = ? ORDER BY date DESC LIMIT 1", app["id"])
    if latest:
        try:
            info = IpaInfo(build_file(latest))
            if info.icon:
                icon_path(app["bundle_id"]).write_bytes(info.icon)
                has, blank = 1, int(info.icon_blank)
        except Exception:
            pass
    db.execute("UPDATE apps SET icon_custom = 0, auto_inject = 0, has_icon = ?, icon_blank = ?, updated_at = ? "
               "WHERE id = ?", has, blank, db.now(), app["id"])


def recheck_icons():
    """Fill icon_blank for apps imported before blank detection existed."""
    for a in db.all_("SELECT * FROM apps WHERE icon_custom = 0 AND has_icon = 1"):
        p = icon_path(a["bundle_id"])
        if p.exists():
            blank = int(pngutil.is_blank(p.read_bytes()))
            if blank != a["icon_blank"]:
                db.execute("UPDATE apps SET icon_blank = ? WHERE id = ?", blank, a["id"])


_inject_q: "queue.Queue[int]" = queue.Queue()
_inject_started = False
_inject_lock = threading.Lock()


def queue_inject(build_ids):
    global _inject_started
    with _inject_lock:
        if not _inject_started:
            threading.Thread(target=_inject_worker, daemon=True, name="icon-inject").start()
            _inject_started = True
    for bid in build_ids:
        db.execute("UPDATE builds SET icon_state = 'wartet' WHERE id = ?", bid)
        _inject_q.put(bid)


def _inject_worker():
    while True:
        bid = _inject_q.get()
        try:
            inject_build(bid)
        except Exception as e:
            db.log_event("error", f"Icon-Einbau Build {bid}: {e}")
        finally:
            _inject_q.task_done()


def inject_build(build_id: int):
    b = db.one("SELECT * FROM builds WHERE id = ?", build_id)
    if not b:
        return
    a = db.one("SELECT * FROM apps WHERE id = ?", b["app_id"])
    pngs = load_custom_icon(a)
    if len(pngs) < len(ipa.ICON_SIZES):
        db.execute("UPDATE builds SET icon_state = 'kein eigenes Icon' WHERE id = ?", build_id)
        return
    f = build_file(b)
    db.execute("UPDATE builds SET icon_state = 'läuft' WHERE id = ?", build_id)
    try:
        ipa.inject_icons(f, pngs)
        size, sha = f.stat().st_size, sha256_file(f)
        db.execute("UPDATE builds SET icon_injected = 1, icon_state = '', size = ?, sha256 = ? WHERE id = ?",
                   size, sha, build_id)
        db.log_event("info", f"Icon eingebaut: {a['name']} {b['version']} ({b['build_version']})")
    except Exception as e:
        db.execute("UPDATE builds SET icon_state = ? WHERE id = ?", f"Fehler: {e}"[:300], build_id)
        raise
