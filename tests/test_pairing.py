"""
Pairing files (StikDebug & co.) with a fake ipas-pair helper.
Run: python tests/test_pairing.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 18768
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = None
UDID = "00008110-001A2B3C4D5E801E"


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def call(method, path, body=None):
    h = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            p = r.read()
            return r.status, (json.loads(p) if r.headers.get_content_type().endswith("json") else p), r.headers
    except urllib.error.HTTPError as e:
        p = e.read()
        try:
            return e.code, json.loads(p), e.headers
        except Exception:
            return e.code, p, e.headers


def wait_job(jid, pin=None):
    for _ in range(100):
        _, j, _ = call("GET", f"/api/v1/pairing/jobs/{jid}")
        if pin and j.get("need_pin"):
            s, _, _ = call("POST", f"/api/v1/pairing/jobs/{jid}/pin", {"pin": pin})
            check(s == 200, "PIN übermittelt")
            pin = None
        if j["status"] != "läuft":
            return j
        time.sleep(0.2)
    raise SystemExit("job timeout")


def main():
    global TOKEN
    tmp = Path(tempfile.mkdtemp())
    fake = ROOT / "tests" / "fake_idevice"
    log = tmp / "calls.jsonl"
    env = {**os.environ, "DATA_DIR": str(tmp / "data"), "BASE_URL": "https://apps.example.test", "NO_PROXY": "127.0.0.1",
           "IDEVICE_BIN_DIR": str(fake), "IPAS_PAIR_BIN": str(fake / "ipas-pair"), "FAKE_PAIR_LOG": str(log),
           "ADMIN_USER": "admin", "ADMIN_PASSWORD": "password123", "GITHUB_TOKEN": ""}
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], cwd=ROOT, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(BASE + "/healthz")
                break
            except Exception:
                time.sleep(0.2)
        _, d, _ = call("POST", "/api/v1/auth/token", {"username": "admin", "password": "password123", "name": "t"})
        TOKEN = d["token"]

        print("status")
        s, st, _ = call("GET", "/api/v1/pairing")
        check(s == 200 and st["available"] and st["pairings"] == [], "Helper verfügbar, noch keine Dateien")

        print("USB lockdown")
        s, d, _ = call("POST", "/api/v1/pairing/usb", {"udid": UDID, "kind": "lockdown", "wifi": True})
        check(s == 202, "Auftrag gestartet")
        j = wait_job(d["job"])
        check(j["status"] == "fertig" and j["pairing_id"], f"fertig ({j.get('error')})")
        pid = j["pairing_id"]
        s, p, _ = call("GET", f"/api/v1/pairing/{pid}")
        check(p["name"] == "Marcs iPhone" and p["wifi_mac"] == "aa:bb:cc:dd:ee:ff" and p["wifi_debugging"], "Geräteinfo gespeichert")
        s, data, h = call("GET", f"/api/v1/pairing/{pid}/file")
        check(s == 200 and data == f"<plist>lockdown {UDID}</plist>".encode(), "Datei herunterladbar")
        check(f'{UDID}.plist' in h.get("Content-Disposition", ""), "Dateiname UDID.plist")
        raw = b"".join(f.read_bytes() for f in (tmp / "data").glob("ipa-source.db*"))
        check(b"lockdown " + UDID.encode() not in raw, "verschlüsselt gespeichert")
        leftovers = list((tmp / "data" / "tmp").glob("pair-*"))
        check(not leftovers, "keine Temp-Dateien übrig")

        print("apps + push")
        s, d, _ = call("GET", f"/api/v1/pairing/{pid}/apps?via=usb")
        check(s == 200 and len(d["apps"]) == 1 and d["apps"][0]["known"] == "StikDebug" and d["apps"][0]["path"] == "pairingFile.plist", "StikDebug erkannt")
        s, d, _ = call("GET", f"/api/v1/pairing/{pid}/apps?via=usb&all=1")
        check(len(d["apps"]) == 2, "alle Apps")
        s, d, _ = call("POST", f"/api/v1/pairing/{pid}/push", {"bundle_id": "com.stik.sj", "path": "pairingFile.plist", "via": "usb", "name": "StikDebug"})
        check(s == 200 and d["path"] == "/Documents/pairingFile.plist", "geschrieben")
        s, d, _ = call("POST", f"/api/v1/pairing/{pid}/push", {"bundle_id": "com.stik.sj", "path": "../etc/x", "via": "usb"})
        check(s == 400, "Pfad mit .. abgelehnt")

        print("WLAN prüfen/erneuern (IP per mDNS)")
        s, d, _ = call("POST", f"/api/v1/pairing/{pid}/verify", {})
        check(s == 200 and d["ip"] == "192.168.1.50", f"per mDNS gefunden ({d})")
        s, d, _ = call("POST", f"/api/v1/pairing/{pid}/renew", {})
        check(s == 200 and d["targets"] and all(t["ok"] for t in d["targets"]), "erneut in StikDebug geschrieben")
        calls = [json.loads(x) for x in log.read_text().splitlines()]
        last_push = [c for c in calls if c["cmd"] == "push"][-1]
        check(last_push.get("ip") == "192.168.1.50" and "udid" not in last_push, "Erneuerung lief über WLAN")
        s, d, _ = call("POST", f"/api/v1/pairing/{pid}/verify", {"ip": "10.9.9.9"})
        check(s == 200, "falsche IP → Fallback auf bekannte")

        print("RPPairing USB + WLAN-Prüfung")
        s, d, _ = call("POST", "/api/v1/pairing/usb", {"udid": UDID, "kind": "rp"})
        j = wait_job(d["job"])
        check(j["status"] == "fertig", "RPPairing erzeugt")
        rid = j["pairing_id"]
        _, p, _ = call("GET", f"/api/v1/pairing/{rid}")
        check(p["kind"] == "rp" and p["name"] == "Marcs iPhone" and p["filename"].endswith("-rppairing.plist"), "RP-Datensatz")
        s, d, _ = call("POST", f"/api/v1/pairing/{rid}/verify", {})
        check(s == 200 and d["rp_port"] == 49152, "RP per WLAN geprüft")

        print("WLAN-Kopplung mit PIN")
        s, d, _ = call("POST", "/api/v1/pairing/wifi", {"ip": "192.168.1.77", "port": 49152})
        j = wait_job(d["job"], pin="123456")
        check(j["status"] == "fertig", f"gekoppelt ({j.get('error')})")
        s, d, _ = call("POST", "/api/v1/pairing/wifi", {"ip": "192.168.1.77", "port": 49152})
        j = wait_job(d["job"], pin="000111")
        check(j["status"] == "fehler" and "PIN" in j["error"], "falsche PIN → Fehler")

        print("Löschen")
        s, _, _ = call("DELETE", f"/api/v1/pairing/{rid}")
        _, st, _ = call("GET", "/api/v1/pairing")
        check(s == 200 and len(st["pairings"]) == 2, "gelöscht")
        s, _, _ = call("GET", f"/api/v1/pairing/{rid}/file")
        check(s == 404, "weg")
        TOKEN = None
        s, _, _ = call("GET", f"/api/v1/pairing/{pid}/file")
        check(s == 401, "Download nur angemeldet")
        print("ALL OK")
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
