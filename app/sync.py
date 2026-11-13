"""GitHub source synchronisation and the background worker."""
import fnmatch
import queue
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path

from . import config, db, github, store

_queue: "queue.Queue[tuple]" = queue.Queue()
_state = {"running": None, "queued": 0, "last_full_sync": None}
_wakeup = threading.Event()


def status():
    return {**_state, "queued": _queue.qsize()}


def enqueue(kind: str, arg=None):
    _queue.put((kind, arg))


# ---------------------------------------------------------------- remote --
def _remote_candidates(src):
    out = []
    if src["use_releases"]:
        for rel in github.releases(src["repo"]):
            if rel.get("draft") or (rel.get("prerelease") and not src["include_prereleases"]):
                continue
            asset = next((a for a in rel.get("assets", [])
                          if fnmatch.fnmatch(a["name"], src["asset_pattern"])), None)
            if asset:
                out.append({
                    "origin": "release",
                    "ref": rel["tag_name"],
                    "date": rel.get("published_at") or rel.get("created_at"),
                    "notes": (rel.get("body") or rel.get("name") or "").strip(),
                    "filename": asset["name"],
                    "label": f"Release {rel['tag_name']}",
                    "fetch": lambda dest, a=asset: github.download(a["url"], dest),
                })
    if src["use_artifacts"]:
        for art in github.artifacts(src["repo"]):
            if art.get("expired") or not fnmatch.fnmatch(art["name"], src["artifact_name"] or "*"):
                continue
            run = art.get("workflow_run") or {}
            if src["artifact_branch"] and run.get("head_branch") != src["artifact_branch"]:
                continue
            sha = (run.get("head_sha") or "")[:7]
            notes = f"CI-Build {art['name']}"
            if run.get("head_branch"):
                notes += f" ({run['head_branch']}@{sha})"
            out.append({
                "origin": "artifact",
                "ref": str(art["id"]),
                "date": art.get("created_at"),
                "notes": notes,
                "filename": None,
                "label": f"Artefakt {art['name']} #{art['id']}",
                "fetch": lambda dest, a=art: _fetch_artifact(a, dest, src["asset_pattern"]),
            })
    out.sort(key=lambda c: c["date"] or "", reverse=True)
    return out


def _fetch_artifact(art, dest: Path, inner_pattern):
    zpath = dest.with_name(dest.name + ".zip")
    github.download(art["archive_download_url"], zpath, accept="application/vnd.github+json")
    try:
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            ipa = next((n for n in names if fnmatch.fnmatch(n.rsplit("/", 1)[-1], inner_pattern)), None)
            if ipa:
                with z.open(ipa) as s, open(dest, "wb") as f:
                    shutil.copyfileobj(s, f, length=1024 * 1024)
                return
            if any(n.startswith("Payload/") for n in names):
                zpath.replace(dest)
                return
        raise ValueError(f"keine Datei '{inner_pattern}' im Artefakt {art['name']}")
    finally:
        zpath.unlink(missing_ok=True)


def sync_source(source_id: int):
    src = db.one("SELECT * FROM sources WHERE id = ?", source_id)
    if not src or not src["enabled"]:
        return
    _state["running"] = src["repo"]
    imported, errors = 0, []
    try:
        candidates = _remote_candidates(src)
        limits = {"release": src["keep_releases"], "artifact": src["keep_artifacts"]}
        counts = {"release": 0, "artifact": 0}
        for c in candidates:
            if counts[c["origin"]] >= limits[c["origin"]]:
                continue
            counts[c["origin"]] += 1
            if store.find_build(src["id"], c["origin"], c["ref"]):
                continue
            tmp = config.TMP_DIR / f"{uuid.uuid4().hex}.ipa"
            try:
                db.log_event("info", f"{src['repo']}: lade {c['label']}")
                c["fetch"](tmp)
                store.import_ipa(tmp, origin=c["origin"], origin_ref=c["ref"], source_id=src["id"],
                                 date=c["date"], notes=c["notes"], filename=c["filename"])
                imported += 1
            except Exception as e:
                errors.append(f"{c['label']}: {e}")
                db.log_event("error", f"{src['repo']}: {c['label']}: {e}")
            finally:
                tmp.unlink(missing_ok=True)
        removed = store.apply_retention(src)
        status_msg = f"{imported} neu, {removed} entfernt"
        db.execute(
            "UPDATE sources SET last_sync_at=?, last_status=?, last_error=? WHERE id=?",
            db.now(), status_msg, "; ".join(errors)[:2000], src["id"],
        )
    except Exception as e:
        db.execute("UPDATE sources SET last_sync_at=?, last_status='Fehler', last_error=? WHERE id=?",
                   db.now(), str(e)[:2000], src["id"])
        db.log_event("error", f"{src['repo']}: {e}")
    finally:
        _state["running"] = None


def sync_all():
    for s in db.all_("SELECT id FROM sources WHERE enabled = 1 ORDER BY id"):
        sync_source(s["id"])
    _state["last_full_sync"] = db.now()


def sync_repo(repo: str):
    for s in db.all_("SELECT id FROM sources WHERE enabled = 1 AND lower(repo) = lower(?)", repo):
        enqueue("source", s["id"])


# ---------------------------------------------------------------- inbox --
def process_inbox(max_age_done=600):
    """Import IPAs dropped via WebDAV; remove processed files after a grace period."""
    for f in config.INBOX_DIR.glob("*"):
        if not f.is_file():
            continue
        if f.name.endswith(".done"):
            if not f.with_name(f.name[1:-5]).exists():
                f.unlink(missing_ok=True)   # orphaned marker
            continue
        if f.name.startswith(".") or not f.name.lower().endswith(".ipa"):
            # Finder/Windows side files (.DS_Store, ._x) or non-IPAs: drop after grace period
            if time.time() - f.stat().st_mtime > max_age_done:
                f.unlink(missing_ok=True)
            continue
        marker = f.with_name("." + f.name + ".done")
        if marker.exists():
            if time.time() - marker.stat().st_mtime > max_age_done:
                f.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
            continue
        if time.time() - f.stat().st_mtime < 5 or f.stat().st_size == 0:
            continue  # still being written
        try:
            store.import_ipa(f, origin="webdav", filename=f.name, move=False)
        except Exception as e:
            db.log_event("error", f"WebDAV-Import {f.name}: {e}")
        marker.touch()


# --------------------------------------------------------------- workers --
def _worker():
    while True:
        kind, arg = _queue.get()
        try:
            if kind == "all":
                sync_all()
            elif kind == "source":
                sync_source(arg)
            elif kind == "inbox":
                process_inbox()
        except Exception as e:
            db.log_event("error", f"Worker: {e}")
        finally:
            _queue.task_done()


def _scheduler():
    next_sync = time.time() + 5
    while True:
        try:
            interval = max(60, int(db.get_setting("sync_interval") or 900))
        except ValueError:
            interval = 900
        if time.time() >= next_sync:
            enqueue("all")
            next_sync = time.time() + interval
        enqueue("inbox")
        _wakeup.wait(10)
        _wakeup.clear()


def start():
    for d in config.TMP_DIR.glob("*"):
        if d.is_file():
            d.unlink(missing_ok=True)
    threading.Thread(target=_worker, daemon=True, name="worker").start()
    threading.Thread(target=_scheduler, daemon=True, name="scheduler").start()
