"""AltStore source generation (spec 2.0 + legacy variant for strict parsers)."""
import re

from . import config, db
from .db import jloads

CATEGORIES = {"developer", "entertainment", "games", "lifestyle", "other", "photo-video", "social", "utilities"}
ORIGIN_RANK = {"release": 3, "upload": 2, "webdav": 2, "artifact": 1}
IGNORED_ENT = {"application-identifier", "com.apple.developer.team-identifier",
               "keychain-access-groups", "get-task-allow"}


def base_url(request=None) -> str:
    b = db.get_setting("base_url") or config.BASE_URL_ENV
    if not b and request is not None:
        b = str(request.base_url)
    return b.rstrip("/")


def public_prefix(request=None) -> str:
    """Base for public URLs; includes the feed key when the feed is private."""
    b = base_url(request)
    if db.get_setting("feed_private") == "1" and db.get_setting("feed_key"):
        return f"{b}/s/{db.get_setting('feed_key')}"
    return b


def hexcolor(c, default="#5C7AEA"):
    c = str(c or "").strip().lstrip("#")
    return "#" + c.upper() if re.fullmatch(r"[0-9A-Fa-f]{6}", c) else default


def visible_versions(app_id):
    """Visible builds, newest first, deduplicated on version+build."""
    rows = db.all_("SELECT * FROM builds WHERE app_id = ? AND visible = 1 ORDER BY date DESC", app_id)
    best = {}
    for b in rows:
        k = (b["version"], b["build_version"])
        if k not in best or ORIGIN_RANK.get(b["origin"], 0) > ORIGIN_RANK.get(best[k]["origin"], 0):
            best[k] = b
    return sorted(best.values(), key=lambda b: b["date"], reverse=True)


def build_source(request=None, legacy=False):
    s = db.get_settings()
    pub = public_prefix(request)
    tint = hexcolor(s["feed_tint"])
    apps = []
    for a in db.all_("SELECT * FROM apps WHERE visible = 1 ORDER BY sort_order, name COLLATE NOCASE"):
        builds = visible_versions(a["id"])
        if not builds:
            continue
        apps.append(_app_entry(a, builds, s, pub, tint, legacy))

    feed_url = f"{pub}/{'apps-legacy.json' if legacy else 'apps.json'}"
    icon = s["feed_icon_url"] or (apps[0]["iconURL"] if apps else f"{pub}/default-icon.png")
    website = s["feed_website"] or base_url(request)
    count = len(apps)
    src = {
        "name": s["feed_name"],
        "identifier": s["feed_identifier"],
        "sourceURL": feed_url,
        # Strict parsers (KravaSign) need every key present and non-empty
        "subtitle": s["feed_subtitle"] or f"{count} App{'s' if count != 1 else ''}",
        "description": s["feed_description"] or s["feed_subtitle"] or s["feed_name"],
        "iconURL": icon,
        "headerURL": s["feed_header_url"] or icon,
        "website": website,
        "patreonURL": website,
        "tintColor": tint,
        "nsfw": False,
        "featuredApps": [x["bundleIdentifier"] for x in apps][:5],
        "apps": apps,
        "news": _news(apps, website, legacy),
    }
    if legacy:
        for k in ("nsfw", "featuredApps", "headerURL"):
            src.pop(k)
    return src


def _news(apps, website, legacy, limit=6):
    """One news item per recent version (newest first) – also keeps strict parsers happy."""
    items = []
    for a in apps:
        for v in a["versions"][:2]:
            items.append({
                "title": f"{a['name']} {v['version']}",
                "identifier": f"{a['bundleIdentifier']}-{v['version']}-{v['buildVersion']}",
                "caption": f"Version {v['version']} ({v['buildVersion']}) ist verfügbar.",
                "date": v["date"],
                "url": website,
                "appID": a["bundleIdentifier"],
                "imageURL": a["iconURL"],
                "tintColor": a["tintColor"],
                "notify": False,
            })
    items.sort(key=lambda n: n["date"], reverse=True)
    return items[:limit]


def _app_entry(a, builds, s, pub, tint, legacy):
    latest_perm = jloads(builds[0]["permissions"], {"entitlements": [], "privacy": {}})
    entitlements = sorted((set(latest_perm.get("entitlements", [])) | set(jloads(a["entitlements"], [])))
                          - IGNORED_ENT)
    privacy = {**latest_perm.get("privacy", {}), **jloads(a["privacy"], {})}
    icon = a["icon_url"] or (f"{pub}/icons/{a['id']}.png?v={a['updated_at'][:19].replace(':', '')}"
                             if a["has_icon"] else f"{pub}/default-icon.png")
    category = a["category"] if a["category"] in CATEGORIES else "other"
    screenshots = jloads(a["screenshots"], [])

    versions = []
    for b in builds:
        v = {
            "version": b["version"],
            "buildVersion": b["build_version"],
            "date": b["date"][:10] if legacy else b["date"],
            "localizedDescription": b["notes"] or f"Version {b['version']} ({b['build_version']})",
            "downloadURL": f"{pub}/dl/{b['id']}/{b['filename']}",
            "size": b["size"],
            "sha256": b["sha256"],
        }
        if legacy:
            v["buildNumber"] = b["build_version"]
        if b["min_os"]:
            v["minOSVersion"] = b["min_os"]
        versions.append(v)

    entry = {
        "name": a["name"],
        "bundleIdentifier": a["bundle_id"],
        "developerName": a["developer"] or s["developer"] or "Unknown",
        "subtitle": a["subtitle"] or a["description"][:80] or a["name"],
        "localizedDescription": a["description"] or a["subtitle"] or a["name"],
        "iconURL": icon,
        "tintColor": hexcolor(a["tint"], tint),
        "category": category,
    }
    if legacy:
        entry.update({
            "screenshotURLs": [x if isinstance(x, str) else x.get("imageURL", "") for x in screenshots],
            "size": versions[0]["size"],
            "versions": versions,
            "permissions": [],
            "appPermissions": {"privacy": [{"name": k, "usageDescription": v} for k, v in privacy.items()]},
        })
    else:
        entry.update({
            "screenshots": screenshots,
            "versions": versions,
            "appPermissions": {"entitlements": entitlements, "privacy": privacy},
            # AltStore 1.x fields for older clients
            "version": versions[0]["version"],
            "versionDate": versions[0]["date"],
            "versionDescription": versions[0]["localizedDescription"],
            "downloadURL": versions[0]["downloadURL"],
            "size": versions[0]["size"],
        })
    return entry
