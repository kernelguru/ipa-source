"""Signing certificates: .p12 + password + .mobileprovision, validated and stored on the server."""
import base64
import hashlib
import json
import plistlib
import re
import shutil
from datetime import datetime, timezone

from cryptography import x509
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

from . import config, db
from .db import jloads


# ------------------------------------------------------------ encryption --
def _fernet():
    key = hashlib.sha256(("ipa-source/cert-password/" + config.secret_key()).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(s: str) -> str:
    return _fernet().encrypt(s.encode()).decode()


def decrypt(s: str) -> str:
    try:
        return _fernet().decrypt(s.encode()).decode()
    except InvalidToken:
        raise ValueError("Zertifikatspasswort kann nicht entschlüsselt werden (secret.key geändert?)")


# --------------------------------------------------------------- parsing --
def _iso(dt) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_profile(data: bytes) -> dict:
    """A .mobileprovision is a CMS envelope around an XML plist – extract the plist."""
    m = re.search(rb"<\?xml.*?</plist>", data, re.S)
    if not m:
        raise ValueError("Keine gültige .mobileprovision-Datei")
    pl = plistlib.loads(m.group(0))
    ent = pl.get("Entitlements", {})
    if pl.get("ProvisionsAllDevices"):
        ptype = "enterprise"
    elif pl.get("ProvisionedDevices"):
        ptype = "development" if ent.get("get-task-allow") else "adhoc"
    else:
        ptype = "appstore"
    return {
        "name": pl.get("Name", ""),
        "uuid": pl.get("UUID", ""),
        "team_id": (pl.get("TeamIdentifier") or [""])[0],
        "team_name": pl.get("TeamName", ""),
        "expires": _iso(pl.get("ExpirationDate")),
        "devices": pl.get("ProvisionedDevices", []),
        "all_devices": bool(pl.get("ProvisionsAllDevices")),
        "app_id": ent.get("application-identifier", ""),
        "entitlements": ent,
        "certificates": pl.get("DeveloperCertificates", []),
        "type": ptype,
    }


def parse_p12(data: bytes, password: str) -> dict:
    try:
        key, cert, _extra = pkcs12.load_key_and_certificates(data, password.encode() if password else None)
    except ValueError:
        raise ValueError("Passwort falsch oder keine gültige .p12-Datei")
    if not key or not cert:
        raise ValueError("Die .p12 enthält keinen privaten Schlüssel oder kein Zertifikat")
    cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    ou = cert.subject.get_attributes_for_oid(x509.NameOID.ORGANIZATIONAL_UNIT_NAME)
    cn = cn[0].value if cn else ""
    return {
        "cn": cn,
        "team_id": ou[0].value if ou else "",
        "expires": _iso(cert.not_valid_after_utc),
        "der": cert.public_bytes(serialization.Encoding.DER),
        "type": "development" if re.search(r"Development|Developer:", cn) else "distribution",
    }


def app_id_matches(app_id: str, bundle_id: str) -> bool:
    """app_id like 'TEAMID.*', 'TEAMID.com.x.*' or 'TEAMID.com.x.y'."""
    pattern = app_id.split(".", 1)[1] if "." in app_id else app_id
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return bundle_id.startswith(pattern[:-1])
    return pattern == bundle_id


# --------------------------------------------------------------- storage --
def cert_dir(cid: int):
    return config.CERTS_DIR / str(cid)


def add(name: str, p12: bytes, password: str, profile: bytes) -> dict:
    c = parse_p12(p12, password)
    p = parse_profile(profile)
    warnings = []
    if c["der"] not in p["certificates"]:
        warnings.append("Das Zertifikat ist nicht im Provisioning-Profil enthalten – Profil und .p12 passen nicht zusammen")
    now = datetime.now(timezone.utc)
    if c["expires"] and c["expires"] < _iso(now):
        warnings.append("Das Zertifikat ist abgelaufen")
    if p["expires"] and p["expires"] < _iso(now):
        warnings.append("Das Provisioning-Profil ist abgelaufen")
    if warnings and any("passen nicht" in w or "abgelaufen" in w for w in warnings):
        raise ValueError("; ".join(warnings))
    is_default = 0 if db.one("SELECT id FROM certs LIMIT 1") else 1
    cid = db.execute(
        """INSERT INTO certs(name, team_id, team_name, cert_cn, cert_expires, cert_type, profile_name, profile_uuid,
                             profile_expires, profile_type, app_id, devices, entitlements, password_enc, is_default,
                             created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        name or p["name"] or c["cn"], p["team_id"] or c["team_id"], p["team_name"], c["cn"], c["expires"], c["type"],
        p["name"], p["uuid"], p["expires"], p["type"], p["app_id"], json.dumps(p["devices"]),
        json.dumps(p["entitlements"], default=str), encrypt(password or ""), is_default, db.now(),
    )
    d = cert_dir(cid)
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    (d / "cert.p12").write_bytes(p12)
    (d / "profile.mobileprovision").write_bytes(profile)
    for f in d.iterdir():
        f.chmod(0o600)
    db.log_event("info", f"Signier-Zertifikat hinzugefügt: {c['cn']} / Profil {p['name']}")
    return get(cid)


def get(cid: int):
    return db.one("SELECT * FROM certs WHERE id = ?", cid)


def default():
    return db.one("SELECT * FROM certs ORDER BY is_default DESC, id LIMIT 1")


def set_default(cid: int):
    with db.tx() as c:
        c.execute("UPDATE certs SET is_default = 0")
        c.execute("UPDATE certs SET is_default = 1 WHERE id = ?", (cid,))


def remove(cid: int):
    for s in db.all_("SELECT path FROM signed WHERE cert_id = ?", cid):
        if s["path"]:
            (config.SIGNED_DIR / s["path"]).unlink(missing_ok=True)
    shutil.rmtree(cert_dir(cid), ignore_errors=True)
    db.execute("DELETE FROM certs WHERE id = ?", cid)
    if not db.one("SELECT id FROM certs WHERE is_default = 1"):
        first = db.one("SELECT id FROM certs ORDER BY id LIMIT 1")
        if first:
            set_default(first["id"])


def public(c) -> dict:
    now = db.now()
    return {
        **{k: c[k] for k in ("id", "name", "team_id", "team_name", "cert_cn", "cert_expires", "cert_type",
                             "profile_name", "profile_uuid", "profile_expires", "profile_type", "app_id",
                             "created_at")},
        "is_default": bool(c["is_default"]),
        "devices": jloads(c["devices"], []),
        "expired": bool((c["cert_expires"] and c["cert_expires"] < now) or
                        (c["profile_expires"] and c["profile_expires"] < now)),
        "wildcard": c["app_id"].endswith("*"),
    }
