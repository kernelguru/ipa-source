"""
Pairing files for StikDebug, SideStore, Feather & co.

Uses the bundled Rust helper `ipas-pair` (idevice crate) for everything that
talks to the iPhone. Two kinds of files:

  lockdown  classic pairing record (usbmuxd) + UDID; first pairing needs USB.
            Over Wi-Fi it can be verified and written into apps ("renew").
  rp        RPPairing (iOS 17.4+), created over USB; on iOS 27+ also over
            Wi-Fi with the PIN shown on the iPhone.

Files are stored encrypted (Fernet, keyed from secret.key) because Wi-Fi
renewal needs them later. They can be deleted any time.
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid

from cryptography.fernet import Fernet, InvalidToken

from . import config, db
from .db import jloads

_jobs: dict = {}
_lock = threading.Lock()

# Known apps and where they expect the file (relative to the app's Documents)
LOCKDOWN_APPS = [
    ("StikDebug", "pairingFile.plist"),
    ("StikStore", "pairingFile.plist"),
    ("Feather", "pairingFile.plist"),
    ("Ksign", "pairingFile.plist"),
    ("Antrag", "pairingFile.plist"),
    ("EnsWilde", "pairingFile.plist"),
    ("Protokolle", "pairingFile.plist"),
    ("SparseBox", "pairingFile.plist"),
    ("SideStore", "ALTPairingFile.mobiledevicepairing"),
    ("LiveContainer", "SideStore/Documents/ALTPairingFile.mobiledevicepairing"),
]
RP_APPS = [
    ("StikDebug", "pairingFile.plist"),
    ("StosDebug", "pairingFile.plist"),
    ("Feather", "pairingFile.plist"),
    ("Ksign", "pairingFile.plist"),
    ("Antrag", "pairingFile.plist"),
    ("Protokolle", "pairingFile.plist"),
    ("Reynard", "pairingFile.plist"),
    ("SideStore", "pairingFile.plist"),
    ("LiveContainer", "pairingFile.plist"),
    ("Auto Capture", "rpPairingFile.plist"),
]
KNOWN_BUNDLES = {"com.stik.sj": "StikDebug"}
PATH_RE = re.compile(r"^[A-Za-z0-9 ._/-]+$")


# ------------------------------------------------------------------ helpers
def helper():
    p = shutil.which(config.IPAS_PAIR_BIN)
    if p:
        return p
    return config.IPAS_PAIR_BIN if os.access(config.IPAS_PAIR_BIN, os.X_OK) else None


def _fernet():
    key = hashlib.sha256(("ipa-source/pairing/" + config.secret_key()).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _enc(data: bytes) -> str:
    return _fernet().encrypt(data).decode()


def _dec(s: str) -> bytes:
    try:
        return _fernet().decrypt(s.encode())
    except InvalidToken:
        raise ValueError("Pairing-Datei kann nicht entschlüsselt werden (secret.key geändert?) – neu erzeugen")


class _TmpFile:
    """Short-lived 0600 file in TMP_DIR, removed on exit."""

    def __init__(self, data: bytes = None, suffix=".plist"):
        config.TMP_DIR.mkdir(parents=True, exist_ok=True)
        fd, self.path = tempfile.mkstemp(prefix="pair-", suffix=suffix, dir=config.TMP_DIR)
        with os.fdopen(fd, "wb") as f:
            if data:
                f.write(data)

    def read(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def run(args, timeout=60, on_event=None):
    """Run the helper; returns the final result dict. Raises RuntimeError on failure."""
    exe = helper()
    if not exe:
        raise RuntimeError("ipas-pair ist in diesem Image nicht enthalten")
    p = subprocess.Popen([exe, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         env={**os.environ, "RUST_LOG": "off"})
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    result = None
    try:
        for line in p.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "event" in msg:
                if on_event:
                    on_event(msg)
            else:
                result = msg
        p.wait()
    finally:
        timer.cancel()
    err = (p.stderr.read() or "").strip()
    if result is None:
        if p.returncode in (-9, 137):
            raise RuntimeError("Zeitüberschreitung")
        raise RuntimeError(err.splitlines()[-1] if err else f"ipas-pair beendet mit Code {p.returncode}")
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "Fehler")
    return result


# --------------------------------------------------------------------- jobs
def _job(kind, **extra):
    jid = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[jid] = {"id": jid, "kind": kind, "status": "läuft", "log": [], "need_pin": False,
                      "pairing_id": None, "error": "", **extra}
    return jid


def _event(jid, msg):
    j = _jobs[jid]
    j["log"].append(msg.get("message", ""))
    j["log"] = j["log"][-30:]
    if msg.get("event") == "pin":
        j["need_pin"] = True


def job(jid):
    j = _jobs.get(jid)
    return {k: v for k, v in j.items() if not k.startswith("_")} if j else None


def submit_pin(jid, pin: str):
    j = _jobs.get(jid)
    if not j or not j.get("_pin_file"):
        raise ValueError("Auftrag unbekannt")
    pin = re.sub(r"\D", "", pin or "")
    if len(pin) < 4:
        raise ValueError("PIN fehlt")
    with open(j["_pin_file"], "w") as f:
        f.write(pin)
    j["need_pin"] = False


def _spawn(jid, fn, *args):
    def runner():
        j = _jobs[jid]
        try:
            j["pairing_id"] = fn(jid, *args)
            j["status"] = "fertig"
        except Exception as e:
            j["status"] = "fehler"
            j["error"] = str(e)
    threading.Thread(target=runner, daemon=True, name=f"pair-{jid}").start()
    return jid


# ----------------------------------------------------------------- storage
def _save(udid, kind, data: bytes, device: dict = None, **fields) -> int:
    device = device or {}
    now = db.now()
    vals = {
        "name": device.get("DeviceName", ""), "ios": device.get("ProductVersion", ""),
        "model": device.get("ProductType", ""), "wifi_mac": (device.get("WiFiAddress") or "").lower(),
    }
    vals = {k: v for k, v in vals.items() if v}
    vals.update(fields)
    with db.tx() as c:
        row = c.execute("SELECT id FROM pairings WHERE udid=? AND kind=?", (udid, kind)).fetchone()
        if row:
            sets = ", ".join(f"{k}=?" for k in vals)
            c.execute(f"UPDATE pairings SET data_enc=?, updated_at=?, verified_at=?, last_error=''"
                      f"{', ' + sets if sets else ''} WHERE id=?",
                      (_enc(data), now, now, *vals.values(), row["id"]))
            return row["id"]
        cols = ["udid", "kind", "data_enc", "created_at", "updated_at", "verified_at", *vals]
        cur = c.execute(f"INSERT INTO pairings ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        (udid, kind, _enc(data), now, now, now, *vals.values()))
        return cur.lastrowid


def _row(pid):
    r = db.one("SELECT * FROM pairings WHERE id=?", pid)
    if not r:
        raise LookupError("Pairing unbekannt")
    return r


def _update(pid, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE pairings SET {sets} WHERE id=?", *fields.values(), pid)


def public(r):
    return {
        "id": r["id"], "udid": r["udid"], "kind": r["kind"], "name": r["name"], "ios": r["ios"],
        "model": r["model"], "wifi_mac": r["wifi_mac"], "ip": r["ip"], "rp_port": r["rp_port"],
        "wifi_debugging": bool(r["wifi_debugging"]), "targets": jloads(r["targets"], []),
        "created_at": r["created_at"], "updated_at": r["updated_at"], "verified_at": r["verified_at"],
        "last_error": r["last_error"], "filename": filename(r),
    }


def list_():
    return [public(r) for r in db.all_("SELECT * FROM pairings ORDER BY name, udid, kind")]


def filename(r):
    return f"{r['udid']}.plist" if r["kind"] == "lockdown" else f"{r['udid']}-rppairing.plist"


def file_bytes(pid):
    r = _row(pid)
    return filename(r), _dec(r["data_enc"])


def delete(pid):
    db.execute("DELETE FROM pairings WHERE id=?", pid)


def status():
    return {"available": bool(helper()), "host_name": config.PAIRING_HOST_NAME,
            "apps": {"lockdown": [a for a, _ in LOCKDOWN_APPS], "rp": [a for a, _ in RP_APPS]},
            "pairings": list_()}


# --------------------------------------------------------------- USB create
def _do_usb(jid, udid, kind, wifi):
    ev = lambda m: _event(jid, m)
    with _TmpFile() as out:
        if kind == "lockdown":
            args = ["lockdown", "--udid", udid, "--out", out.path] + ([] if wifi else ["--no-wifi"])
            res = run(args, timeout=60, on_event=ev)
            wd = res.get("wifi_debugging") is True
            if wifi and not wd and res.get("wifi_debugging"):
                _jobs[jid]["log"].append(f"WLAN-Debugging nicht aktiviert: {res['wifi_debugging']}")
            return _save(udid, "lockdown", out.read(), res.get("device"), wifi_debugging=int(wd))
        res = run(["rppair", "--udid", udid, "--host", config.PAIRING_HOST_NAME, "--out", out.path],
                  timeout=120, on_event=ev)
        dev = {}
        lock = db.one("SELECT name, ios, model, wifi_mac FROM pairings WHERE udid=? AND kind='lockdown'", udid)
        if lock:
            dev = {"DeviceName": lock["name"], "ProductVersion": lock["ios"], "ProductType": lock["model"],
                   "WiFiAddress": lock["wifi_mac"]}
        return _save(udid, "rp", out.read(), dev, host_name=config.PAIRING_HOST_NAME)


def start_usb(udid: str, kind: str, wifi: bool = True) -> str:
    if kind not in ("lockdown", "rp"):
        raise ValueError("kind muss lockdown oder rp sein")
    if not helper():
        raise RuntimeError("ipas-pair ist in diesem Image nicht enthalten")
    return _spawn(_job("usb-" + kind, udid=udid), _do_usb, udid, kind, wifi)


# ------------------------------------------------------------------ Wi-Fi
def discover(timeout=4):
    res = run(["discover", "--timeout", str(int(timeout))], timeout=timeout + 10)
    return res.get("services", [])


def _pick_ip(svc):
    return (svc.get("ipv4") or [None])[0] or next(iter(svc.get("addresses") or []), None)


def _locate(r, services=None):
    """Find the iPhone's current IP via mDNS (_apple-mobdev2 instance = '<wifi-mac>@...')."""
    services = services if services is not None else discover()
    mac = (r["wifi_mac"] or "").lower()
    ip = None
    for s in services:
        if s.get("type") == "apple-mobdev2" and mac and s.get("fullname", "").lower().startswith(mac + "@"):
            ip = _pick_ip(s)
            break
    port = 0
    for s in services:
        if s.get("type") == "remotepairing" and ip and ip in (s.get("ipv4") or []) + (s.get("addresses") or []):
            port = s.get("port") or 0
    return ip, port


def _lockdown_row(udid):
    r = db.one("SELECT * FROM pairings WHERE udid=? AND kind='lockdown'", udid)
    if not r:
        raise LookupError("Für WLAN wird die Lockdown-Pairing-Datei dieses Geräts benötigt (einmal per USB erzeugen)")
    return r


def _verify_ip(lock, ip):
    with _TmpFile(_dec(lock["data_enc"])) as pf:
        return run(["verify", "--ip", ip, "--pairing", pf.path], timeout=20)


def verify_wifi(pid, ip: str = ""):
    """Check that the device accepts the pairing over Wi-Fi; refreshes IP and device info."""
    r = _row(pid)
    lock = r if r["kind"] == "lockdown" else _lockdown_row(r["udid"])
    candidates = [x for x in (ip, r["ip"], lock["ip"]) if x]
    errors = []
    services = None
    for attempt in range(2):
        for cand in dict.fromkeys(candidates):
            try:
                res = _verify_ip(lock, cand)
                dev = res.get("device") or {}
                upd = {"ip": cand, "verified_at": db.now(), "last_error": ""}
                for k, f in (("DeviceName", "name"), ("ProductVersion", "ios"), ("ProductType", "model")):
                    if dev.get(k):
                        upd[f] = dev[k]
                _update(lock["id"], **upd)
                port = 0
                if r["kind"] == "rp":
                    port = _rp_check(r, cand, services)
                    _update(pid, ip=cand, rp_port=port, verified_at=db.now(), last_error="")
                return {"ok": True, "ip": cand, "rp_port": port, "device": dev}
            except Exception as e:
                errors.append(f"{cand}: {e}")
        if attempt == 0:
            try:
                services = discover()
                found, _ = _locate(lock, services)
            except Exception as e:
                errors.append(f"mDNS: {e}")
                found = None
            if not found or found in candidates:
                break
            candidates = [found]
    msg = "; ".join(errors) or ("iPhone im WLAN nicht gefunden – IP angeben oder Container mit "
                                "network_mode: host betreiben (mDNS)")
    _update(pid, last_error=msg[:500])
    raise RuntimeError(msg)


def _rp_check(r, ip, services=None):
    services = services if services is not None else discover()
    port = 0
    for s in services:
        if s.get("type") == "remotepairing" and ip in (s.get("ipv4") or []) + (s.get("addresses") or []):
            port = s.get("port") or 0
    if not port:
        port = r["rp_port"]
    if not port:
        raise RuntimeError("RemotePairing-Dienst (_remotepairing._tcp) nicht gefunden")
    with _TmpFile(_dec(r["data_enc"])) as rp:
        run(["rpverify", "--ip", ip, "--port", str(port), "--rp", rp.path,
             "--host", r["host_name"] or config.PAIRING_HOST_NAME], timeout=20)
    return port


# --------------------------------------------------------- apps and push
def _via_args(r, via, ip, stack):
    """Connection args for apps/push: USB (udid) or Wi-Fi (ip + lockdown file)."""
    if via == "usb":
        return ["--udid", r["udid"]]
    lock = r if r["kind"] == "lockdown" else _lockdown_row(r["udid"])
    ip = ip or lock["ip"] or r["ip"]
    if not ip:
        ip = verify_wifi(lock["id"])["ip"]
    tmp = _TmpFile(_dec(lock["data_enc"]))
    stack.append(tmp)
    return ["--ip", ip, "--pairing", tmp.path]


def _known_path(kind, app):
    table = LOCKDOWN_APPS if kind == "lockdown" else RP_APPS
    names = [app.get("name", ""), app.get("bundle_name", ""), KNOWN_BUNDLES.get(app.get("bundle_id"), "")]
    for label, path in table:
        for n in names:
            if n and (n.lower() == label.lower() or n.lower().startswith(label.lower())):
                return label, path
    return None, None


def apps(pid, via="usb", ip="", show_all=False):
    r = _row(pid)
    stack = []
    try:
        res = run(["apps", *_via_args(r, via, ip, stack)], timeout=40)
    finally:
        for t in stack:
            t.__exit__()
    out = []
    for a in res.get("apps", []):
        label, path = _known_path(r["kind"], a)
        if label or show_all:
            out.append({**a, "known": label, "path": path or ""})
    out.sort(key=lambda a: (not a["known"], (a["name"] or a["bundle_name"]).lower()))
    return out


def push(pid, bundle_id: str, path: str, via="usb", ip="", name=""):
    r = _row(pid)
    path = (path or "").strip().lstrip("/")
    if not path or ".." in path or not PATH_RE.match(path):
        raise ValueError("Ungültiger Zielpfad")
    if not re.match(r"^[A-Za-z0-9.-]+$", bundle_id or ""):
        raise ValueError("Ungültige Bundle-ID")
    stack = []
    try:
        with _TmpFile(_dec(r["data_enc"])) as f:
            res = run(["push", *_via_args(r, via, ip, stack), "--bundle", bundle_id, "--path", path,
                       "--file", f.path], timeout=40)
    finally:
        for t in stack:
            t.__exit__()
    targets = [t for t in jloads(r["targets"], []) if t.get("bundle_id") != bundle_id]
    targets.append({"bundle_id": bundle_id, "name": name, "path": path})
    _update(pid, targets=json.dumps(targets[-10:]))
    return res


def renew_wifi(pid, ip=""):
    """Wi-Fi renewal: verify the pairing and write it again into every app it went to before."""
    v = verify_wifi(pid, ip)
    r = _row(pid)
    results = []
    for t in jloads(r["targets"], []):
        try:
            push(pid, t["bundle_id"], t["path"], via="wifi", ip=v["ip"], name=t.get("name", ""))
            results.append({**t, "ok": True})
        except Exception as e:
            results.append({**t, "ok": False, "error": str(e)})
    return {"ok": True, "ip": v["ip"], "device": v.get("device"), "targets": results}


# ------------------------------------------------- Wi-Fi RPPairing (iOS 27+)
def _do_wifipair(jid, ip, port, udid_hint):
    j = _jobs[jid]
    with _TmpFile() as out, _TmpFile(suffix=".pin") as pin:
        os.unlink(pin.path)               # helper waits until the file appears
        j["_pin_file"] = pin.path
        run(["wifipair", "--ip", ip, "--port", str(port), "--host", config.PAIRING_HOST_NAME,
             "--out", out.path, "--pin-file", pin.path, "--timeout", "180"],
            timeout=240, on_event=lambda m: _event(jid, m))
        udid = udid_hint or f"wifi-{ip}"
        dev = {}
        lock = db.one("SELECT * FROM pairings WHERE udid=? AND kind='lockdown'", udid) if udid_hint else None
        if lock:
            dev = {"DeviceName": lock["name"], "ProductVersion": lock["ios"], "ProductType": lock["model"],
                   "WiFiAddress": lock["wifi_mac"]}
        return _save(udid, "rp", out.read(), dev, ip=ip, rp_port=int(port), host_name=config.PAIRING_HOST_NAME)


def start_wifipair(ip: str, port: int, udid: str = "") -> str:
    if not ip or not port:
        raise ValueError("IP und Port des RemotePairing-Dienstes angeben (oder per Suche wählen)")
    if not helper():
        raise RuntimeError("ipas-pair ist in diesem Image nicht enthalten")
    return _spawn(_job("wifi-rp", ip=ip), _do_wifipair, ip, int(port), udid)
