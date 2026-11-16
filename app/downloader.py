"""
Link downloader: fetch an IPA from a URL sent by the web UI, the API or an iOS shortcut.

Understands
  - direct .ipa links (with resume on dropped connections)
  - itms-services:// install links (reads the manifest)
  - GitHub / Gitea / Forgejo / Codeberg repo, release and tag pages (newest .ipa asset)
  - Dropbox share links
  - any web page that links to .ipa files or itms-services (one link -> taken, several -> user picks)
"""
import html
import json
import plistlib
import queue
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import config, db, github, providers, store

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
MAX_HTML = 5 * 1024 * 1024
_q: "queue.Queue[int]" = queue.Queue()
_cancel: set = set()
_started = False
_lock = threading.Lock()


class NeedsChoice(Exception):
    def __init__(self, candidates):
        self.candidates = candidates


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- public --
def enqueue(url: str, notes: str = "", via: str = "web") -> dict:
    url = (url or "").strip()
    m = re.search(r"(https?://|itms-services://)\S+", url)   # shortcuts sometimes send "Title https://…"
    if not m:
        raise ValueError("Keine gültige URL")
    url = m.group(0).rstrip(").,;\"'>")
    did = db.execute("INSERT INTO downloads(url, notes, via, created_at) VALUES(?,?,?,?)",
                     url, notes or "", via, db.now())
    _start()
    _q.put(did)
    return get(did)


def choose(did: int, url: str):
    d = get(did)
    if not d or d["status"] != "auswahl":
        raise ValueError("Download wartet nicht auf eine Auswahl")
    if url not in [c["url"] for c in d["candidates"]]:
        raise ValueError("Unbekannte Auswahl")
    db.execute("UPDATE downloads SET current_url = ?, status = 'wartet', candidates = '[]' WHERE id = ?", url, did)
    _start()
    _q.put(did)
    return get(did)


def cancel(did: int):
    d = get(did)
    if d and d["status"] in ("wartet", "läuft"):
        _cancel.add(did)
        db.execute("UPDATE downloads SET status = 'abgebrochen', finished_at = ? WHERE id = ?", db.now(), did)
    return True


def get(did: int):
    r = db.one("SELECT * FROM downloads WHERE id = ?", did)
    if r:
        r["candidates"] = json.loads(r["candidates"] or "[]")
        r["repo_hint"] = json.loads(r["repo_hint"]) if r["repo_hint"] else None
    return r


def recent(limit=50):
    return [get(r["id"]) for r in db.all_("SELECT id FROM downloads ORDER BY id DESC LIMIT ?", limit)]


def resume_pending():
    for r in db.all_("SELECT id FROM downloads WHERE status IN ('wartet', 'läuft')"):
        db.execute("UPDATE downloads SET status = 'wartet' WHERE id = ?", r["id"])
        _start()
        _q.put(r["id"])


# ---------------------------------------------------------------- worker --
def _start():
    global _started
    with _lock:
        if not _started:
            threading.Thread(target=_worker, daemon=True, name="downloader").start()
            _started = True


def _worker():
    while True:
        did = _q.get()
        try:
            _run(did)
        except Exception as e:
            db.execute("UPDATE downloads SET status='fehler', error=?, finished_at=? WHERE id=?",
                       str(e)[:500], db.now(), did)
        finally:
            _q.task_done()


def _set(did, **kw):
    if kw:
        db.execute(f"UPDATE downloads SET {', '.join(f'{k} = ?' for k in kw)} WHERE id = ?", *kw.values(), did)


def _run(did: int):
    d = get(did)
    if not d or d["status"] not in ("wartet",) or did in _cancel:
        return
    _set(did, status="läuft", error="", received=0, total=0)
    url = d["current_url"] or d["url"]
    tmp = config.TMP_DIR / f"dl-{did}.ipa"
    try:
        url, filename, notes_extra, hint = _resolve(url, did)
        if hint:
            _set(did, repo_hint=json.dumps(hint))
        _set(did, current_url=url)
        _fetch(url, tmp, did)
        if not _is_ipa(tmp):
            raise ValueError("Die Datei ist keine IPA (kein Payload/*.app im Archiv)")
        notes = "\n".join(x for x in (d["notes"], notes_extra, f"Geladen von {urllib.parse.urlparse(url).netloc}") if x)
        b = store.import_ipa(tmp, origin="download", origin_ref=f"dl-{did}", filename=filename, notes=notes)
        _set(did, status="fertig", build_id=b["id"], finished_at=db.now())
    except NeedsChoice as e:
        _set(did, status="auswahl", candidates=json.dumps(e.candidates[:30]), finished_at=db.now())
    except Cancelled:
        pass
    except Exception as e:
        if did not in _cancel:
            _set(did, status="fehler", error=str(e)[:500], finished_at=db.now())
            db.log_event("error", f"Download {d['url']}: {e}")
    finally:
        tmp.unlink(missing_ok=True)
        tmp.with_name(tmp.name + ".part").unlink(missing_ok=True)
        _cancel.discard(did)


# ------------------------------------------------------------- resolving --
def _resolve(url: str, did: int):
    """-> (direct_url, filename, extra_notes, repo_hint)"""
    if url.startswith("itms-services:"):
        return _from_itms(url) + ("", None)

    u = urllib.parse.urlparse(url)
    host = u.netloc.lower()
    parts = [p for p in u.path.split("/") if p]
    if u.path.lower().endswith(".ipa"):
        return url, None, "", None

    if host in ("www.dropbox.com", "dropbox.com"):
        q = dict(urllib.parse.parse_qsl(u.query))
        q["dl"] = "1"
        return u._replace(query=urllib.parse.urlencode(q)).geturl(), None, "", None

    # GitHub repo / release pages (not direct asset downloads)
    if host in ("github.com", "www.github.com") and len(parts) >= 2 and "download" not in parts:
        repo = f"{parts[0]}/{parts[1]}"
        tag = parts[4] if len(parts) >= 5 and parts[2] == "releases" and parts[3] == "tag" else None
        rel = github.get(f"/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}") if tag else _gh_latest(repo)
        asset = _pick_asset(rel.get("assets", []), "browser_download_url")
        return asset["url"], asset["name"], f"Release {rel.get('tag_name', '')}", \
            {"kind": "github", "host": "", "repo": repo}

    # Gitea / Forgejo / Codeberg repo or release pages
    if len(parts) >= 2 and "download" not in parts and _looks_gitea(u):
        repo = f"{parts[0]}/{parts[1]}"
        base = f"{u.scheme}://{u.netloc}"
        tag = parts[4] if len(parts) >= 5 and parts[2] == "releases" and parts[3] == "tag" else None
        rels = providers.gitea_releases(base, repo)
        rel = next((r for r in rels if r["tag_name"] == tag), None) if tag else \
            next((r for r in rels if not r.get("draft") and not r.get("prerelease")), rels[0] if rels else None)
        if not rel:
            raise ValueError("Keine Releases gefunden")
        asset = _pick_asset(rel.get("assets", []), "browser_download_url")
        return asset["url"], asset["name"], f"Release {rel.get('tag_name', '')}", \
            {"kind": "gitea", "host": base, "repo": repo}

    return url, None, "", None


def _gh_latest(repo):
    for rel in github.get(f"/repos/{repo}/releases?per_page=15"):
        if not rel.get("draft") and any(a["name"].lower().endswith(".ipa") for a in rel.get("assets", [])):
            return rel
    raise ValueError(f"Kein Release mit IPA in {repo} gefunden")


def _pick_asset(assets, url_key):
    ipas = [a for a in assets if a["name"].lower().endswith(".ipa")]
    if not ipas:
        raise ValueError("Das Release enthält keine .ipa-Datei")
    if len(ipas) > 1:
        raise NeedsChoice([{"url": a[url_key], "label": a["name"], "size": a.get("size")} for a in ipas])
    return {"url": ipas[0][url_key], "name": ipas[0]["name"]}


_gitea_cache: dict = {}


def _looks_gitea(u) -> bool:
    host = u.netloc.lower()
    if host in providers.KNOWN_GITEA:
        return True
    if host in _gitea_cache:
        return _gitea_cache[host]
    ok = False
    try:
        req = urllib.request.Request(f"{u.scheme}://{u.netloc}/api/v1/version", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            ok = "version" in json.loads(r.read(4096))
    except Exception:
        ok = False
    _gitea_cache[host] = ok
    return ok


def _from_itms(url: str):
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url.replace("itms-services://", "https://x/")).query))
    murl = q.get("url")
    if not murl:
        raise ValueError("itms-services-Link ohne Manifest-URL")
    req = urllib.request.Request(murl, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        pl = plistlib.loads(r.read(MAX_HTML))
    item = pl["items"][0]
    pkg = next(a["url"] for a in item["assets"] if a.get("kind") == "software-package")
    title = item.get("metadata", {}).get("title")
    return pkg, (f"{title}.ipa" if title else None)


LINK_RE = re.compile(r"""(?:href|src|data-url)\s*=\s*["']([^"']+)["']""", re.I)


def _links_from_html(page: bytes, base: str):
    text = page.decode("utf-8", "replace")
    found, seen = [], set()
    for raw in LINK_RE.findall(text) + re.findall(r"(itms-services://[^\s\"'<>]+)", text):
        href = html.unescape(raw).strip()
        if href.startswith("itms-services:"):
            target = href
        else:
            target = urllib.parse.urljoin(base, href)
            path = urllib.parse.urlparse(target).path.lower()
            if not path.endswith(".ipa"):
                continue
        if target in seen:
            continue
        seen.add(target)
        label = urllib.parse.unquote(urllib.parse.urlparse(target).path.rsplit("/", 1)[-1]) or target
        if target.startswith("itms-services:"):
            label = "Installationslink (itms-services)"
        found.append({"url": target, "label": label})
    return found


# ------------------------------------------------------------- fetching --
def _fetch(url: str, dest, did: int, depth=0):
    """Stream to dest with progress and resume. Web pages are scanned for IPA links."""
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    received, total, tries = 0, 0, 0
    while True:
        if did in _cancel:
            raise Cancelled()
        headers = {"User-Agent": UA, "Accept": "*/*"}
        if received:
            headers["Range"] = f"bytes={received}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                ctype = r.headers.get_content_type()
                if received == 0 and (ctype in ("text/html", "application/xhtml+xml")):
                    if depth > 1:
                        raise ValueError("Keine IPA gefunden (Weiterleitungsschleife)")
                    links = _links_from_html(r.read(MAX_HTML), r.geturl())
                    if not links:
                        raise ValueError("Auf der Seite wurde kein IPA-Link gefunden")
                    if len(links) > 1:
                        raise NeedsChoice(links)
                    nxt = links[0]["url"]
                    if nxt.startswith("itms-services:"):
                        nxt, _ = _from_itms(nxt)
                    _set(did, current_url=nxt)
                    return _fetch(nxt, dest, did, depth + 1)
                if received and r.status != 206:
                    received = 0                     # server ignores Range -> start over
                    part.unlink(missing_ok=True)
                if not total:
                    total = int(r.headers.get("Content-Length") or 0) + received
                    if total:
                        free = shutil.disk_usage(config.DATA_DIR).free
                        if total * 2.1 > free:
                            raise ValueError(f"Zu wenig Speicher: {total // 2**20} MB Download, {free // 2**20} MB frei")
                    _set(did, total=total)
                last = time.time()
                with open(part, "ab") as f:
                    while True:
                        chunk = r.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        received += len(chunk)
                        if time.time() - last > 1:
                            if did in _cancel:
                                raise Cancelled()
                            _set(did, received=received)
                            last = time.time()
            if total and received < total:
                raise ConnectionError("Verbindung vorzeitig beendet")
            _set(did, received=received, total=total or received)
            part.replace(dest)
            return
        except (NeedsChoice, Cancelled, ValueError):
            raise
        except urllib.error.HTTPError as e:
            if e.code == 416 and received:
                part.replace(dest)
                return
            if e.code < 500 or tries >= 4:
                raise ValueError(f"HTTP {e.code} beim Laden von {urllib.parse.urlparse(url).netloc}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            if tries >= 4:
                raise ValueError(f"Download abgebrochen: {e}") from None
        tries += 1
        time.sleep(min(30, 2 ** tries))
        received = part.stat().st_size if part.exists() else 0


def _is_ipa(path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return any(re.match(r"^Payload/[^/]+\.app/Info\.plist$", n) for n in z.namelist())
    except zipfile.BadZipFile:
        return False
