"""
Server-side signing: certificate upload, zsign, OTA manifest, USB install (fake libimobiledevice).
Needs `zsign` (set ZSIGN_BIN) and the `openssl` CLI to create a throw-away test certificate.
Run: ZSIGN_BIN=/path/to/zsign python tests/test_signing.py
"""
import datetime
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from make_ipa import make  # noqa: E402

ZSIGN = os.environ.get("ZSIGN_BIN") or shutil.which("zsign")
PORT = 18766
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = None


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def call(method, path, body=None, raw=None, headers=None):
    h = {"Authorization": f"Bearer {TOKEN}", **(headers or {})} if TOKEN else dict(headers or {})
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            p = r.read()
            return r.status, (json.loads(p) if r.headers.get_content_type().endswith("json") else p)
    except urllib.error.HTTPError as e:
        p = e.read()
        try:
            return e.code, json.loads(p)
        except Exception:
            return e.code, p


def multipart(fields):
    bnd = uuid.uuid4().hex
    out = b""
    for name, value, filename in fields:
        out += f"--{bnd}\r\nContent-Disposition: form-data; name=\"{name}\"".encode()
        if filename:
            out += f"; filename=\"{filename}\"\r\nContent-Type: application/octet-stream".encode()
        out += b"\r\n\r\n" + (value if isinstance(value, bytes) else value.encode()) + b"\r\n"
    return out + f"--{bnd}--\r\n".encode(), {"Content-Type": f"multipart/form-data; boundary={bnd}"}


def make_cert(d: Path, udid: str):
    """CA + leaf certificate + p12 + signed mobileprovision (zsign needs an issuer chain)."""
    run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True)
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
        "-days", "30", "-subj", "/CN=Test CA")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "key.pem", "-out", "leaf.csr",
        "-subj", "/UID=TEAM123456/CN=Apple Development: Test (ABC)/OU=TEAM123456/O=Test")
    (d / "ext.cnf").write_text("keyUsage=critical,digitalSignature\nextendedKeyUsage=codeSigning\n")
    run("openssl", "x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
        "-out", "cert.pem", "-days", "30", "-extfile", "ext.cnf")
    run("openssl", "pkcs12", "-export", "-inkey", "key.pem", "-in", "cert.pem", "-certfile", "ca.pem",
        "-out", "test.p12", "-passout", "pass:pw123")
    der = subprocess.run(["openssl", "x509", "-in", "cert.pem", "-outform", "DER"], cwd=d, check=True,
                         capture_output=True).stdout
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    (d / "profile.plist").write_bytes(plistlib.dumps({
        "Name": "Test Profile", "UUID": str(uuid.uuid4()), "TeamIdentifier": ["TEAM123456"], "TeamName": "Test",
        "ExpirationDate": now + datetime.timedelta(days=20), "CreationDate": now, "DeveloperCertificates": [der],
        "ProvisionedDevices": [udid], "Entitlements": {"application-identifier": "TEAM123456.*", "get-task-allow": True}}))
    run("openssl", "cms", "-sign", "-in", "profile.plist", "-signer", "cert.pem", "-inkey", "key.pem",
        "-outform", "DER", "-nodetach", "-binary", "-out", "test.mobileprovision")


def main():
    global TOKEN
    if not ZSIGN or not shutil.which("openssl"):
        print("SKIP: zsign or openssl not available")
        return
    macho = Path(os.environ.get("TEST_MACHO", "/tmp/zsign/test/dylib/bin/demo1.dylib"))
    if not macho.exists():
        print("SKIP: no arm64 Mach-O for the test app (set TEST_MACHO)")
        return
    tmp = Path(tempfile.mkdtemp())
    udid = "00008110-001A2B3C4D5E801E"
    make_cert(tmp, udid)
    make(tmp / "base.ipa", "de.test.signing", "2.1", "21", "SignTest")
    with zipfile.ZipFile(tmp / "base.ipa") as z, zipfile.ZipFile(tmp / "app.ipa", "w", zipfile.ZIP_DEFLATED) as o:
        for i in z.infolist():
            o.writestr(i, macho.read_bytes() if i.filename == "Payload/T.app/T" else z.read(i))

    fake = ROOT / "tests" / "fake_idevice"
    env = {**os.environ, "DATA_DIR": str(tmp / "data"), "BASE_URL": "https://apps.example.test", "NO_PROXY": "127.0.0.1",
           "ZSIGN_BIN": ZSIGN, "IDEVICE_BIN_DIR": str(fake), "IDEVICEINSTALLER_BIN": str(fake / "ideviceinstaller"),
           "ADMIN_USER": "admin", "ADMIN_PASSWORD": "password123", "GITHUB_TOKEN": ""}
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], cwd=ROOT, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(BASE + "/healthz")
                break
            except Exception:
                time.sleep(0.2)
        st, d = call("POST", "/api/v1/auth/token", {"username": "admin", "password": "password123"})
        TOKEN = d["token"]

        mp, h = multipart([("name", "Test", None), ("password", "wrong", None),
                           ("p12", (tmp / "test.p12").read_bytes(), "t.p12"),
                           ("profile", (tmp / "test.mobileprovision").read_bytes(), "t.mobileprovision")])
        st, d = call("POST", "/api/v1/certs", raw=mp, headers=h)
        check(st == 400, "wrong p12 password rejected")
        mp, h = multipart([("name", "Test", None), ("password", "pw123", None),
                           ("p12", (tmp / "test.p12").read_bytes(), "t.p12"),
                           ("profile", (tmp / "test.mobileprovision").read_bytes(), "t.mobileprovision")])
        st, cert = call("POST", "/api/v1/certs", raw=mp, headers=h)
        check(st == 201 and cert["profile_type"] == "development" and cert["devices"] == [udid], "certificate stored")
        check(oct((tmp / "data" / "certs" / str(cert["id"]) / "cert.p12").stat().st_mode)[-3:] == "600", "p12 file mode 600")

        st, d = call("PUT", "/api/v1/uploads?filename=app.ipa", raw=(tmp / "app.ipa").read_bytes())
        build_id = d["build"]["id"]
        st, s = call("POST", f"/api/v1/builds/{build_id}/sign", {})
        check(st == 202, "sign job queued")
        for _ in range(60):
            st, s = call("GET", f"/api/v1/signed/{s['id']}")
            if s["status"] in ("fertig", "fehler"):
                break
            time.sleep(0.25)
        check(s["status"] == "fertig", f"signed ({s.get('error')})")
        check(s["itms_url"].startswith("itms-services://?action=download-manifest&url=https%3A%2F%2Fapps.example.test"),
              "itms-services link")
        token = s["install_page"].rsplit("/", 1)[1]
        st, man = call("GET", f"/install/{token}/manifest.plist")
        man = plistlib.loads(man)["items"][0]
        check(man["metadata"]["bundle-identifier"] == "de.test.signing"
              and man["assets"][0]["url"].endswith(f"/install/{token}/app.ipa"), "OTA manifest")
        st, ipa = call("GET", f"/install/{token}/app.ipa")
        (tmp / "signed.ipa").write_bytes(ipa)
        with zipfile.ZipFile(tmp / "signed.ipa") as z:
            names = z.namelist()
            exe_mode = z.getinfo("Payload/T.app/T").external_attr >> 16
        check(bool(exe_mode & 0o111), "app binary is executable in the signed IPA")
        check("Payload/T.app/_CodeSignature/CodeResources" in names and "Payload/T.app/embedded.mobileprovision" in names,
              "downloaded IPA is code-signed with embedded profile")
        st, _ = call("GET", "/install/doesnotexist/manifest.plist")
        check(st == 404, "unknown install token -> 404")

        st, again = call("POST", f"/api/v1/builds/{build_id}/sign", {})
        check(again["id"] == s["id"], "unchanged build reuses signed copy")

        make(tmp / "broken.ipa", "de.test.broken", "1.0", "1", "Broken")   # fake 4-byte "binary"
        st, d = call("PUT", "/api/v1/uploads?filename=broken.ipa", raw=(tmp / "broken.ipa").read_bytes())
        st, bad = call("POST", f"/api/v1/builds/{d['build']['id']}/sign", {})
        for _ in range(60):
            st, bad = call("GET", f"/api/v1/signed/{bad['id']}?log=1")
            if bad["status"] in ("fertig", "fehler"):
                break
            time.sleep(0.25)
        check(bad["status"] == "fehler" and "Exit-Code" in bad["error"] and len(bad["error"]) > 15 and bad["log"],
              f"failed signing gives a readable reason + log ({bad['error']})")

        st, devs = call("GET", "/api/v1/devices")
        check(devs["available"] and devs["devices"][0]["paired"], "device list")
        st, j = call("POST", f"/api/v1/signed/{s['id']}/install-usb", {"udid": udid})
        for _ in range(40):
            st, j = call("GET", f"/api/v1/jobs/{j['id']}")
            if j["status"] != "läuft":
                break
            time.sleep(0.25)
        check(j["status"] == "fertig" and j["progress"] == 100, "USB install job")
        st, d = call("POST", f"/api/v1/signed/{s['id']}/install-usb", {"udid": "not-in-profile"})
        check(st == 400, "device not in profile rejected")

        st, _ = call("DELETE", f"/api/v1/certs/{cert['id']}")
        st, lst = call("GET", "/api/v1/signed")
        check(lst["signed"] == [] and not list((tmp / "data" / "signed").rglob("*.ipa")), "deleting cert removes signed IPAs")
        print("\nSignier-Tests bestanden.")
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
