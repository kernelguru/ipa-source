"""
End-to-end test: starts a server on a temp data dir and exercises the API.
Run: python tests/test_api.py   (needs only the runtime requirements)
"""
import base64
import hashlib
import hmac
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
sys.path.insert(0, str(ROOT / "tests"))
from make_ipa import make  # noqa: E402

PORT = 18765
BASE = f"http://127.0.0.1:{PORT}"
COOKIE = {}


def call(method, path, body=None, headers=None, raw=None, auth=None):
    h = {"X-Requested-With": "ipa-source", **(headers or {})}
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    if auth:
        h["Authorization"] = auth
    elif COOKIE:
        h["Cookie"] = "; ".join(f"{k}={v}" for k, v in COOKIE.items())
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            for c in r.headers.get_all("set-cookie") or []:
                k, v = c.split(";")[0].split("=", 1)
                COOKIE[k] = v
            payload = r.read()
            return r.status, (json.loads(payload) if payload and r.headers.get_content_type().endswith("json") else payload)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except Exception:
            return e.code, payload


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def main():
    tmp = Path(tempfile.mkdtemp())
    env = {**os.environ, "DATA_DIR": str(tmp / "data"), "BASE_URL": BASE, "NO_PROXY": "127.0.0.1",
           "GITHUB_TOKEN": ""}
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
                           cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(BASE + "/healthz")
                break
            except Exception:
                time.sleep(0.2)

        st, d = call("GET", "/api/v1/info")
        check(d["setup_required"], "setup required on fresh server")
        st, _ = call("POST", "/api/v1/setup", {"username": "admin", "password": "password123"})
        check(st == 200 and "ipas_session" in COOKIE, "setup creates user + session")
        st, _ = call("POST", "/api/v1/setup", {"username": "x", "password": "password123"})
        check(st == 409, "second setup rejected")

        st, d = call("POST", "/api/v1/tokens", {"name": "ci", "scope": "upload"})
        up_tok = d["token"]
        check(st == 201 and up_tok.startswith("ipas_"), "upload token created")

        make(tmp / "a.ipa", "de.test.a", "1.0", "1", "Alpha")
        make(tmp / "a2.ipa", "de.test.a", "1.1", "2", "Alpha")
        make(tmp / "b.ipa", "de.test.b", "3.0", "9", "Beta")

        st, d = call("PUT", "/api/v1/uploads?filename=a.ipa&notes=first", raw=(tmp / "a.ipa").read_bytes(),
                     auth=f"Bearer {up_tok}")
        check(st == 201 and d["build"]["version"] == "1.0", "raw PUT upload")
        st, d = call("PUT", "/api/v1/uploads?filename=a.ipa", raw=b"not a zip", auth=f"Bearer {up_tok}")
        check(st == 400, "invalid IPA rejected")
        st, _ = call("GET", "/api/v1/apps", auth=f"Bearer {up_tok}")
        check(st == 403, "upload scope cannot read")

        boundary = "XyZ"
        mp = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a2.ipa\"\r\n"
              f"Content-Type: application/octet-stream\r\n\r\n").encode() + (tmp / "a2.ipa").read_bytes() + \
             f"\r\n--{boundary}--\r\n".encode()
        st, d = call("POST", "/api/v1/uploads", raw=mp, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        check(st == 201 and d["app"]["build_count"] == 2, "multipart upload via session")

        basic = "Basic " + base64.b64encode(b"admin:password123").decode()
        st, _ = call("PUT", "/dav/Beta.ipa", raw=(tmp / "b.ipa").read_bytes(), auth=basic)
        check(st == 201, "WebDAV PUT")
        for _ in range(40):
            st, d = call("GET", "/api/v1/apps")
            if len(d["apps"]) == 2:
                break
            time.sleep(0.5)
        check(len(d["apps"]) == 2, "WebDAV import picked up")

        st, feed = call("GET", "/apps.json", headers={})
        alpha = next(a for a in feed["apps"] if a["bundleIdentifier"] == "de.test.a")
        check([v["version"] for v in alpha["versions"]] == ["1.1", "1.0"], "feed lists versions newest first")
        check(alpha["appPermissions"]["privacy"].get("NSLocalNetworkUsageDescription") == "LAN", "privacy from IPA")
        st, leg = call("GET", "/apps-legacy.json")
        lv = leg["apps"][0]["versions"][0]
        check(len(lv["date"]) == 10 and isinstance(leg["apps"][0]["appPermissions"]["privacy"], list), "legacy format")

        st, blob = call("GET", alpha["versions"][0]["downloadURL"].replace(BASE, ""))
        check(st == 200 and hashlib.sha256(blob).hexdigest() == alpha["versions"][0]["sha256"], "download + sha256")

        st, _ = call("PATCH", "/api/v1/settings", {"feed_private": True})
        st, urls = call("GET", "/api/v1/feed")
        st1, _ = call("GET", "/apps.json")
        st2, _ = call("GET", urls["apps_json"].replace(BASE, ""))
        check(st1 == 404 and st2 == 200, "private feed requires key")
        call("PATCH", "/api/v1/settings", {"feed_private": False})

        st, s = call("GET", "/api/v1/settings")
        payload = json.dumps({"action": "published", "repository": {"full_name": "x/y"}}).encode()
        sig = "sha256=" + hmac.new(s["webhook_secret"].encode(), payload, hashlib.sha256).hexdigest()
        st, d = call("POST", "/api/v1/webhooks/github", raw=payload,
                     headers={"X-GitHub-Event": "release", "X-Hub-Signature-256": sig, "Content-Type": "application/json"})
        check(st == 200 and d["triggered"], "webhook signature accepted")
        st, _ = call("POST", "/api/v1/webhooks/github", raw=payload,
                     headers={"X-GitHub-Event": "release", "X-Hub-Signature-256": "sha256=00"})
        check(st == 401, "bad webhook signature rejected")

        st, d = call("POST", "/api/v1/auth/token", {"username": "admin", "password": "password123", "name": "iPhone"})
        check(st == 201, "native token login")
        st, me = call("GET", "/api/v1/me", auth=f"Bearer {d['token']}")
        check(me["scope"] == "full", "bearer auth works")

        build_id = alpha["versions"][0]["downloadURL"].split("/dl/")[1].split("/")[0]
        st, _ = call("PATCH", f"/api/v1/builds/{build_id}", {"visible": False})
        st, feed = call("GET", "/apps.json")
        alpha = next(a for a in feed["apps"] if a["bundleIdentifier"] == "de.test.a")
        check(len(alpha["versions"]) == 1, "hidden build excluded from feed")
        print("\nAlle Tests bestanden.")
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
