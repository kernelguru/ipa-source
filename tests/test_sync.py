"""Sync logic against a mocked GitHub API: python tests/test_sync.py"""
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp())
os.environ["DATA_DIR"] = str(tmp / "data")
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
from make_ipa import make  # noqa: E402
from app import db, feed, github, sync  # noqa: E402

db.init()
files = {}
for v, bnum in [("1.0", "1"), ("1.1", "5"), ("1.1", "6"), ("1.2", "7"), ("1.2", "8")]:
    p = tmp / f"{v}-{bnum}.ipa"; make(p, "de.x.app", v, bnum, "X"); files[f"ipa-{v}-{bnum}"] = p
    z = tmp / f"{v}-{bnum}.zip"
    with zipfile.ZipFile(z, "w") as zz: zz.write(p, "X.ipa")
    files[f"art-{v}-{bnum}"] = z

rel = lambda tag, date, key: {"tag_name": tag, "published_at": date, "body": f"Notes {tag}",
                              "assets": [{"name": "X.ipa", "url": key}]}
art = lambda i, date, key, br="main": {"id": i, "name": "X-ipa", "expired": False, "created_at": date,
                                       "archive_download_url": key, "workflow_run": {"head_branch": br, "head_sha": "abc1234"}}
RELEASES = [rel("v1.1", "2026-11-05T00:00:00Z", "ipa-1.1-6"), rel("v1.0", "2026-11-01T00:00:00Z", "ipa-1.0-1")]
ARTS = [art(9, "2026-11-10T00:00:00Z", "art-1.2-8"), art(8, "2026-11-08T00:00:00Z", "art-1.2-7", "dev"),
        art(7, "2026-11-04T23:00:00Z", "art-1.1-6"), art(6, "2026-11-03T00:00:00Z", "art-1.1-5")]
github.releases = lambda repo: RELEASES
github.artifacts = lambda repo: [a for a in ARTS]
github.download = lambda url, dest, accept=None: (dest.parent.mkdir(parents=True, exist_ok=True), shutil.copy(files[url], dest))

sid = db.execute("INSERT INTO sources(kind, repo, use_artifacts, artifact_branch, keep_releases, keep_artifacts, created_at) "
                 "VALUES('github','me/x',1,'main',5,2,?)", db.now())
sync.sync_source(sid)
src = db.one("SELECT * FROM sources WHERE id=?", sid)
print("status:", src["last_status"], "| error:", src["last_error"] or "-")
builds = db.all_("SELECT origin, origin_ref, version, build_version FROM builds ORDER BY date DESC")
for b in builds: print("  ", b)
ok = [(b["origin"], b["version"], b["build_version"]) for b in builds] == [
    ("artifact", "1.2", "8"), ("release", "1.1", "6"), ("artifact", "1.1", "6"), ("release", "1.0", "1")]
versions = [(v["version"], v["buildVersion"]) for v in feed.build_source()["apps"][0]["versions"]]
print("feed:", versions)
ok &= versions == [("1.2", "8"), ("1.1", "6"), ("1.0", "1")]

# artifact 9 expires on GitHub, a new one arrives -> retention keeps 2 newest artifacts locally
ARTS[0]["expired"] = True
ARTS.insert(0, art(10, "2026-11-12T00:00:00Z", "art-1.2-8"))
files["art-1.2-8b"] = files["art-1.2-8"]
sync.sync_source(sid)
arts = [b["origin_ref"] for b in db.all_("SELECT origin_ref FROM builds WHERE origin='artifact' ORDER BY date DESC")]
print("artifacts after expiry+new:", arts)
ok &= arts == ["10", "9"]
print("\nOK" if ok else "\nFAIL"); sys.exit(0 if ok else 1)
