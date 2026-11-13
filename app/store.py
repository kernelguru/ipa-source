"""Importing IPAs into storage, app/build bookkeeping, retention."""
import json
import re
import shutil
import uuid
from pathlib import Path

from . import config, db
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

    # Icon: take it from the newest build
    newest = db.one("SELECT MAX(date) AS d FROM builds WHERE app_id = ?", app["id"])["d"]
    if info.icon and (not newest or date >= newest or not icon_path(info.bundle_id).exists()):
        icon_path(info.bundle_id).write_bytes(info.icon)
        db.execute("UPDATE apps SET has_icon = 1, updated_at = ? WHERE id = ?", db.now(), app["id"])

    bid = db.execute(
        """INSERT INTO builds(app_id, source_id, origin, origin_ref, version, build_version, min_os,
                              size, sha256, date, notes, filename, path, permissions, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        app["id"], source_id, origin, origin_ref, info.version, info.build_version, info.min_os,
        size, sha, date, notes or "", filename, str(rel), json.dumps(info.permissions), db.now(),
    )
    db.log_event("info", f"Import {origin}: {info.name} {info.version} ({info.build_version})")
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
