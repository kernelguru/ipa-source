"""Link downloader against local fake web/Gitea servers: python tests/test_downloads.py"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from make_ipa import make  # noqa: E402

PORT, WEB, GITEA = 18767, 18768, 18769
BASE = f"http://127.0.0.1:{PORT}"
TMP = Path(tempfile.mkdtemp())
TOKEN = None
FLAKY = {"done": False}


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


class Web(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_file(self, data, ctype="application/octet-stream"):
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data) - start))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        body = data[start:]
        if self.path.startswith("/flaky") and not FLAKY["done"] and not rng:
            FLAKY["done"] = True
            self.wfile.write(body[: len(body) // 2])   # drop the connection halfway
            self.wfile.flush()
            self.connection.shutdown(2)
            return
        self.wfile.write(body)

    def html(self, s):
        b = s.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p in ("/a.ipa", "/flaky/b.ipa", "/files/c.ipa", "/files/d.ipa", "/e.ipa"):
            name = p.rsplit("/", 1)[-1][0]
            return self.send_file((TMP / f"{name}.ipa").read_bytes())
        if p == "/page-one":
            return self.html('<html><body><a href="/files/c.ipa">Download</a></body></html>')
        if p == "/page-two":
            return self.html('<a href="files/c.ipa">C</a> <a href="https://127.0.0.1:%d/files/d.ipa">D</a>' % WEB)
        if p == "/page-none":
            return self.html("<p>nothing here</p>")
        if p == "/manifest.plist":
            import plistlib
            b = plistlib.dumps({"items": [{"assets": [{"kind": "software-package", "url": f"http://127.0.0.1:{WEB}/e.ipa"}],
                                           "metadata": {"title": "Echo", "bundle-identifier": "de.dl.e"}}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.end_headers()
            self.wfile.write(b)
            return
        if p == "/notanipa.zip":
            return self.send_file(b"PK\x05\x06" + b"\0" * 18)
        self.send_response(404)
        self.end_headers()


class Gitea(Web):
    def json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/v1/version":
            return self.json({"version": "11.0.0+forgejo"})
        if p == "/api/v1/repos/team/app/releases":
            return self.json([
                {"tag_name": "v2.0", "draft": False, "prerelease": False, "published_at": "2026-11-10T00:00:00Z",
                 "body": "Neue Version", "assets": [{"name": "App.ipa", "size": 1,
                                                     "browser_download_url": f"http://127.0.0.1:{GITEA}/team/app/releases/download/v2.0/App.ipa"}]},
                {"tag_name": "v1.0", "draft": False, "prerelease": False, "published_at": "2026-11-01T00:00:00Z",
                 "body": "", "assets": [{"name": "App.ipa", "size": 1,
                                         "browser_download_url": f"http://127.0.0.1:{GITEA}/team/app/releases/download/v1.0/App.ipa"}]}])
        if p.startswith("/team/app/releases/download/"):
            ver = p.split("/")[-2]
            return self.send_file((TMP / f"g-{ver}.ipa").read_bytes())
        self.send_response(404)
        self.end_headers()


def call(method, path, body=None):
    h = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def wait(did, until=("fertig", "fehler", "auswahl")):
    for _ in range(120):
        st, d = call("GET", f"/api/v1/downloads/{did}")
        if d["status"] in until:
            return d
        time.sleep(0.25)
    return d


def main():
    global TOKEN
    for n, bid in (("a", "de.dl.a"), ("b", "de.dl.b"), ("c", "de.dl.c"), ("d", "de.dl.d"), ("e", "de.dl.e")):
        make(TMP / f"{n}.ipa", bid, "1.0", "1", n.upper())
    # make "b" big enough that dropping halfway is meaningful
    import zipfile
    with zipfile.ZipFile(TMP / "b.ipa", "a") as z:
        z.writestr("Payload/T.app/blob.bin", os.urandom(3 * 1024 * 1024))
    make(TMP / "g-v2.0.ipa", "org.codeberg.app", "2.0", "20", "GiteaApp")
    make(TMP / "g-v1.0.ipa", "org.codeberg.app", "1.0", "10", "GiteaApp")
    for port, h in ((WEB, Web), (GITEA, Gitea)):
        srv = ThreadingHTTPServer(("127.0.0.1", port), h)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    env = {**os.environ, "DATA_DIR": str(TMP / "data"), "BASE_URL": BASE, "NO_PROXY": "127.0.0.1,localhost",
           "no_proxy": "127.0.0.1,localhost", "ADMIN_USER": "admin", "ADMIN_PASSWORD": "password123", "GITHUB_TOKEN": ""}
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        env.pop(k, None)
    app = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], cwd=ROOT, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(BASE + "/healthz")
                break
            except Exception:
                time.sleep(0.2)
        st, d = call("POST", "/api/v1/auth/token", {"username": "admin", "password": "password123", "scope": "full"})
        TOKEN = d["token"]
        w = f"http://127.0.0.1:{WEB}"

        st, d = call("POST", "/api/v1/downloads", {"url": f"{w}/a.ipa", "notes": "direkt"})
        check(st == 202 and "Download gestartet" in d["message"], "download queued with message for shortcut")
        d = wait(d["id"])
        check(d["status"] == "fertig" and d["app"]["bundle_id"] == "de.dl.a", "direct .ipa link imported")

        st, d = call("POST", f"/api/v1/downloads?url={urllib.parse.quote('Schau mal: ' + w + '/flaky/b.ipa')}")
        d = wait(d["id"])
        check(d["status"] == "fertig" and FLAKY["done"], "URL extracted from shared text; dropped connection resumed")

        st, d = call("POST", "/api/v1/downloads", {"url": f"{w}/page-one"})
        d = wait(d["id"])
        check(d["status"] == "fertig" and d["app"]["bundle_id"] == "de.dl.c", "web page with one IPA link followed")

        st, d = call("POST", "/api/v1/downloads", {"url": f"{w}/page-two"})
        d = wait(d["id"])
        check(d["status"] == "auswahl" and len(d["candidates"]) == 2, "web page with two links asks for a choice")
        st, d = call("POST", f"/api/v1/downloads/{d['id']}/choose", {"url": f"{w}/files/c.ipa"})
        d = wait(d["id"])
        check(d["status"] == "fertig", "chosen candidate downloaded")

        st, d = call("POST", "/api/v1/downloads", {"url": f"{w}/page-none"})
        d = wait(d["id"])
        check(d["status"] == "fehler" and "kein IPA-Link" in d["error"], "page without IPA -> clear error")

        itms = "itms-services://?action=download-manifest&url=" + urllib.parse.quote(f"{w}/manifest.plist", safe="")
        st, d = call("POST", "/api/v1/downloads", {"url": itms})
        d = wait(d["id"])
        check(d["status"] == "fertig" and d["app"]["bundle_id"] == "de.dl.e", "itms-services link resolved")

        st, d = call("POST", "/api/v1/downloads", {"url": f"{w}/notanipa.zip"})
        d = wait(d["id"])
        check(d["status"] == "fehler" and "keine IPA" in d["error"], "non-IPA file rejected")

        g = f"http://127.0.0.1:{GITEA}"
        st, d = call("POST", "/api/v1/downloads", {"url": f"{g}/team/app/releases"})
        d = wait(d["id"])
        check(d["status"] == "fertig" and d["build"]["version"] == "2.0" and d["repo_hint"]["kind"] == "gitea",
              "Gitea/Forgejo release page -> newest IPA + repo hint")

        st, src = call("POST", "/api/v1/sources", {"repo": f"{g}/team/app", "keep_releases": 1})
        check(st == 201 and src["kind"] == "gitea" and src["host"] == g and src["repo"] == "team/app", "Gitea source from URL")
        for _ in range(60):
            st, s2 = call("GET", f"/api/v1/sources/{src['id']}")
            if s2["last_sync_at"]:
                break
            time.sleep(0.25)
        check(s2["build_count"] == 1 and not s2["last_error"], f"Gitea source synced, keep 1 ({s2['last_error']})")
        st, d = call("GET", "/api/v1/downloads")
        check(any(x.get("repo_watched") for x in d["downloads"]), "download marks repo as watched")

        st, dup = call("POST", "/api/v1/sources", {"repo": f"{g}/team/app"})
        check(st == 409, "duplicate source rejected")
        print("\nDownload-Tests bestanden.")
    finally:
        app.terminate()


if __name__ == "__main__":
    main()
