"""
Server-side signing with zsign and installing:
  - OTA: iOS installs straight from this server via an itms-services manifest (HTTPS required)
  - USB: ideviceinstaller (libimobiledevice) to an iPhone attached to the server
"""
import os
import plistlib
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid

from . import certs, config, db, store
from .db import jloads

ANSI = re.compile(r"\x1b\[[0-9;]*m")
_q: "queue.Queue[int]" = queue.Queue()
_started = False
_lock = threading.Lock()
_jobs: dict = {}          # USB install jobs (in memory)


def tool(name: str):
    if config.IDEVICE_BIN_DIR:
        p = os.path.join(config.IDEVICE_BIN_DIR, name)
        return p if os.access(p, os.X_OK) else None
    return shutil.which(name)


def zsign_path():
    return shutil.which(config.ZSIGN_BIN) or (config.ZSIGN_BIN if os.access(config.ZSIGN_BIN, os.X_OK) else None)


def capabilities():
    return {
        "zsign": bool(zsign_path()),
        "ideviceinstaller": bool(shutil.which(config.IDEVICEINSTALLER_BIN) or tool("ideviceinstaller")),
        # local socket (host usbmuxd mounted / embedded) or remote usbmuxd, e.g. Apple Mobile
        # Device Service on Windows: USBMUXD_SOCKET_ADDRESS=192.168.1.10:27015
        "usbmuxd": os.path.exists("/var/run/usbmuxd") or bool(os.environ.get("USBMUXD_SOCKET_ADDRESS")),
        "usbmuxd_remote": os.environ.get("USBMUXD_SOCKET_ADDRESS", ""),
    }


# ================================================================= signing
def _start_worker():
    global _started
    with _lock:
        if not _started:
            threading.Thread(target=_worker, daemon=True, name="signer").start()
            _started = True


def request_sign(build_id: int, cert_id: int, force=False) -> dict:
    b = db.one("SELECT * FROM builds WHERE id = ?", build_id)
    c = certs.get(cert_id)
    if not b or not c:
        raise ValueError("Build oder Zertifikat nicht gefunden")
    if not zsign_path():
        raise ValueError("zsign ist auf dem Server nicht installiert")
    existing = db.one("SELECT * FROM signed WHERE build_id = ? AND cert_id = ? ORDER BY id DESC LIMIT 1",
                      build_id, cert_id)
    if existing and not force:
        if existing["status"] in ("wartet", "läuft"):
            return existing
        if (existing["status"] == "fertig" and existing["build_sha256"] == b["sha256"]
                and (config.SIGNED_DIR / existing["path"]).exists()):
            return existing
    sid = db.execute(
        "INSERT INTO signed(build_id, cert_id, build_sha256, token, created_at) VALUES(?,?,?,?,?)",
        build_id, cert_id, b["sha256"], secrets.token_urlsafe(24), db.now(),
    )
    _start_worker()
    _q.put(sid)
    return db.one("SELECT * FROM signed WHERE id = ?", sid)


def _worker():
    while True:
        sid = _q.get()
        try:
            _sign(sid)
        except Exception as e:
            import traceback
            msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            db.execute("UPDATE signed SET status='fehler', error=?, log=?, finished_at=? WHERE id=?",
                       msg[:500], traceback.format_exc()[-8000:], db.now(), sid)
            db.log_event("error", f"Signieren #{sid}: {msg}")
        finally:
            _q.task_done()


def _target_bundle_id(app_id: str, bundle_id: str):
    """Bundle ID to use with this profile (None = keep)."""
    if certs.app_id_matches(app_id, bundle_id):
        return None
    pattern = app_id.split(".", 1)[1] if "." in app_id else app_id
    if pattern.endswith("*"):
        return pattern[:-1] + bundle_id
    return pattern


def _sign(sid: int):
    s = db.one("SELECT * FROM signed WHERE id = ?", sid)
    if not s:
        return
    b = db.one("SELECT * FROM builds WHERE id = ?", s["build_id"])
    a = db.one("SELECT * FROM apps WHERE id = ?", b["app_id"])
    c = certs.get(s["cert_id"])
    src = store.build_file(b)
    need = int(b["size"] * 2.2) + 200 * 2**20
    free = shutil.disk_usage(config.DATA_DIR).free
    if need > free:
        raise ValueError(f"Zu wenig Speicher zum Signieren: {need // 2**20} MB nötig, {free // 2**20} MB frei")

    db.execute("UPDATE signed SET status='läuft', error='', log='' WHERE id=?", sid)
    cdir = certs.cert_dir(c["id"])
    rel = f"{store.safe_name(a['bundle_id'])}/{sid}-{uuid.uuid4().hex[:8]}.ipa"
    out = config.SIGNED_DIR / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.TMP_DIR / f"zsign-{sid}"
    tmp.mkdir(parents=True, exist_ok=True)
    new_bid = _target_bundle_id(c["app_id"], a["bundle_id"])
    cmd = [zsign_path(), "-k", str(cdir / "cert.p12"), "-p", certs.decrypt(c["password_enc"]),
           "-m", str(cdir / "profile.mobileprovision"), "-z", str(config.SIGN_ZIP_LEVEL),
           "-t", str(tmp), "-o", str(out)]
    if new_bid:
        cmd += ["-b", new_bid]
    cmd.append(str(src))
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=4 * 3600)
        raw = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
        log = ANSI.sub("", raw)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok = r.returncode == 0 and out.exists() and out.stat().st_size > 0
    if not ok:
        out.unlink(missing_ok=True)
        msg = _zsign_error(raw, r.returncode, out)
        log = f"$ zsign … (Exit-Code {r.returncode})\n" + log
        db.execute("UPDATE signed SET status='fehler', error=?, log=?, finished_at=? WHERE id=?",
                   msg[:500], log[-8000:], db.now(), sid)
        db.log_event("error", f"Signieren {a['name']} {b['version']}: {msg}")
        return
    db.execute("UPDATE signed SET status='fertig', path=?, size=?, bundle_id=?, log=?, finished_at=? WHERE id=?",
               rel, out.stat().st_size, new_bid or a["bundle_id"], log[-8000:], db.now(), sid)
    db.log_event("info", f"Signiert: {a['name']} {b['version']} ({b['build_version']}) mit „{c['name']}“ "
                         f"in {time.time() - t0:.0f} s")
    # keep only the newest signed copy per app + certificate
    for old in db.all_("""SELECT s.id, s.path FROM signed s JOIN builds b ON b.id = s.build_id
                          WHERE b.app_id = ? AND s.cert_id = ? AND s.id != ? AND s.status IN ('fertig','fehler')""",
                       a["id"], c["id"], sid):
        if old["path"]:
            (config.SIGNED_DIR / old["path"]).unlink(missing_ok=True)
        db.execute("DELETE FROM signed WHERE id = ?", old["id"])


def _zsign_error(raw: str, rc: int, out) -> str:
    """Readable reason from zsign's output: red lines are errors, yellow ones only warnings."""
    red = [ANSI.sub("", ln).strip().lstrip(">").strip() for ln in raw.splitlines() if "\x1b[31m" in ln]
    red = [x for x in red if x]
    if rc < 0:
        import signal
        try:
            name = signal.Signals(-rc).name
        except ValueError:
            name = f"Signal {-rc}"
        hint = " – vermutlich zu wenig Arbeitsspeicher (OOM-Killer)" if -rc == 9 else ""
        return f"zsign wurde vom System beendet ({name}){hint}"
    if red:
        return f"{red[0]} (Exit-Code {rc})"
    if rc == 0:
        return "zsign meldet Erfolg, aber es entstand keine signierte Datei – Speicherplatz/Schreibrechte in /data prüfen"
    tail = [ANSI.sub("", ln).strip() for ln in raw.splitlines() if ln.strip()][-1:] or [""]
    return f"zsign beendet mit Exit-Code {rc}: {tail[0]}".rstrip(": ")


def delete_signed(sid: int):
    s = db.one("SELECT * FROM signed WHERE id = ?", sid)
    if s and s["path"]:
        (config.SIGNED_DIR / s["path"]).unlink(missing_ok=True)
    db.execute("DELETE FROM signed WHERE id = ?", sid)


def cleanup_orphans():
    """Signed files whose row is gone (build deleted, etc.)."""
    known = {r["path"] for r in db.all_("SELECT path FROM signed WHERE path != ''")}
    for f in config.SIGNED_DIR.rglob("*.ipa"):
        if str(f.relative_to(config.SIGNED_DIR)) not in known:
            f.unlink(missing_ok=True)


def public(s, base: str) -> dict:
    b = db.one("SELECT * FROM builds WHERE id = ?", s["build_id"]) or {}
    a = db.one("SELECT * FROM apps WHERE id = ?", b.get("app_id")) if b else None
    c = certs.get(s["cert_id"])
    out = {k: s[k] for k in ("id", "build_id", "cert_id", "bundle_id", "size", "status", "error",
                             "created_at", "finished_at")}
    out.update({
        "app_id": a["id"] if a else None,
        "app_name": a["name"] if a else "",
        "version": b.get("version"),
        "build_version": b.get("build_version"),
        "cert_name": c["name"] if c else "",
        "outdated": bool(b) and s["build_sha256"] != b.get("sha256"),
    })
    if s["status"] == "fertig":
        page = f"{base}/install/{s['token']}"
        out.update({
            "install_page": page,
            "manifest_url": f"{page}/manifest.plist",
            "itms_url": "itms-services://?action=download-manifest&url=" + urllib.parse.quote(f"{page}/manifest.plist", safe=""),
            "download_url": f"{page}/app.ipa",
            "ota_ready": base.startswith("https://"),
        })
    return out


def by_token(token: str):
    return db.one("SELECT * FROM signed WHERE token = ? AND status = 'fertig'", token)


def manifest(s, base: str) -> bytes:
    b = db.one("SELECT * FROM builds WHERE id = ?", s["build_id"])
    a = db.one("SELECT * FROM apps WHERE id = ?", b["app_id"])
    page = f"{base}/install/{s['token']}"
    return plistlib.dumps({"items": [{
        "assets": [
            {"kind": "software-package", "url": f"{page}/app.ipa"},
            {"kind": "display-image", "url": f"{page}/icon.png"},
            {"kind": "full-size-image", "url": f"{page}/icon.png"},
        ],
        "metadata": {
            "bundle-identifier": s["bundle_id"] or a["bundle_id"],
            "bundle-version": b["version"],
            "kind": "software",
            "title": a["name"],
        },
    }]})


# ===================================================================== USB
def _run(args, timeout=20):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def devices():
    caps = capabilities()
    out = {"available": bool(tool("idevice_id")), **caps, "devices": []}
    if not out["available"]:
        return out
    seen = set()
    for flag, conn in (("-l", "usb"), ("-n", "network")):
        try:
            rc, so, _ = _run([tool("idevice_id"), flag])
        except Exception:
            continue
        for udid in so.split():
            if udid in seen:
                continue
            seen.add(udid)
            d = {"udid": udid, "connection": conn, "paired": False, "name": "", "ios": "", "model": ""}
            try:
                prc, _, _ = _run([tool("idevicepair"), "-u", udid, "validate"])
                d["paired"] = prc == 0
                if d["paired"]:
                    net = ["-n"] if conn == "network" else []
                    for key, field in (("DeviceName", "name"), ("ProductVersion", "ios"), ("ProductType", "model")):
                        rc, val, _ = _run([tool("ideviceinfo"), "-u", udid, *net, "-k", key])
                        if rc == 0:
                            d[field] = val
            except Exception as e:
                d["error"] = str(e)
            out["devices"].append(d)
    return out


def pair(udid: str):
    rc, so, se = _run([tool("idevicepair"), "-u", udid, "pair"], timeout=60)
    msg = (so or se).strip()
    if rc != 0:
        if "Please accept the trust dialog" in msg or "passcode" in msg.lower():
            return False, "Bitte auf dem iPhone „Vertrauen“ antippen (Gerät entsperrt) und erneut koppeln"
        return False, msg or "Koppeln fehlgeschlagen"
    return True, msg or "Gekoppelt"


def start_usb_install(signed_id: int, udid: str) -> str:
    s = db.one("SELECT * FROM signed WHERE id = ? AND status = 'fertig'", signed_id)
    if not s:
        raise ValueError("Signierte IPA nicht gefunden – bitte zuerst signieren")
    inst = shutil.which(config.IDEVICEINSTALLER_BIN) or tool("ideviceinstaller")
    if not inst:
        raise ValueError("ideviceinstaller ist auf dem Server nicht installiert")
    c = certs.get(s["cert_id"])
    devs = jloads(c["devices"], []) if c else []
    if c and c["profile_type"] in ("development", "adhoc") and devs and udid not in devs:
        raise ValueError("Dieses iPhone ist nicht im Provisioning-Profil enthalten")
    jid = uuid.uuid4().hex[:12]
    _jobs[jid] = {"id": jid, "signed_id": signed_id, "udid": udid, "status": "läuft", "progress": 0,
                  "message": "Übertrage IPA aufs iPhone …", "log": "", "started_at": db.now()}
    threading.Thread(target=_usb_install, args=(jid, inst, config.SIGNED_DIR / s["path"], udid), daemon=True).start()
    return jid


def _usb_install(jid, inst, path, udid):
    job = _jobs[jid]
    try:
        p = subprocess.Popen([inst, "-u", udid, "-i", str(path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
        for line in p.stdout:
            line = ANSI.sub("", line).strip()
            if not line:
                continue
            job["log"] = (job["log"] + line + "\n")[-6000:]
            m = re.search(r"\((\d+)%\)", line)
            if m:
                job["progress"] = int(m.group(1))
            if line.startswith("Install:") or line.startswith("Installing"):
                job["message"] = line
            elif "Uploading" in line or "Copying" in line:
                job["message"] = "Übertrage IPA aufs iPhone …"
            if "ERROR" in line.upper():
                job["message"] = line
        rc = p.wait()
        ok = rc == 0 and ("Complete" in job["log"] or "ERROR" not in job["log"].upper())
        job.update(status="fertig" if ok else "fehler", progress=100 if ok else job["progress"],
                   message="Installiert" if ok else (job["message"] or f"Fehler (Code {rc})"))
        db.log_event("info" if ok else "error", f"USB-Installation auf {udid}: {job['message']}")
    except Exception as e:
        job.update(status="fehler", message=str(e))
    job["finished_at"] = db.now()


def job(jid: str):
    return _jobs.get(jid)
