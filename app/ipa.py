"""IPA inspection: Info.plist, permissions, entitlements, icon (incl. CgBI fix)."""
import hashlib
import plistlib
import re
import struct
import zipfile
import zlib
from pathlib import Path


def log(msg):
    print(msg, flush=True)


APP_RE = re.compile(r"^Payload/[^/]+\.app/")


def read_ipa(path: Path):
    """Return (info_plist_dict, icon_png_bytes_or_None)."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        plist_name = next(
            (n for n in names if re.match(r"^Payload/[^/]+\.app/Info\.plist$", n)), None
        )
        if not plist_name:
            raise ValueError("No Payload/*.app/Info.plist in IPA")
        info = plistlib.loads(z.read(plist_name))
        app_prefix = plist_name.rsplit("/", 1)[0] + "/"

        # Collect icon file name prefixes from the plist
        prefixes = set()
        for key in ("CFBundleIcons", "CFBundleIcons~ipad"):
            files = (
                info.get(key, {}).get("CFBundlePrimaryIcon", {}).get("CFBundleIconFiles", [])
            )
            prefixes.update(files)
        prefixes.update(info.get("CFBundleIconFiles", []))
        if not prefixes:
            prefixes = {"AppIcon", "Icon"}

        candidates = []
        for n in names:
            if not n.startswith(app_prefix) or not n.lower().endswith(".png"):
                continue
            base = n[len(app_prefix):]
            if "/" in base:
                continue
            if any(base.startswith(p) for p in prefixes):
                candidates.append((z.getinfo(n).file_size, n))
        icon = None
        if candidates:
            candidates.sort(reverse=True)
            raw = z.read(candidates[0][1])
            try:
                icon = normalize_png(raw)
            except Exception as e:  # icon is optional
                log(f"  icon conversion failed: {e}")
        info["_permissions"] = read_permissions(z, names, app_prefix)
        return info, icon


IGNORED_ENTITLEMENTS = {
    "application-identifier",
    "com.apple.developer.team-identifier",
    "keychain-access-groups",
    "get-task-allow",
}


def read_permissions(z, names, app_prefix):
    """
    AltStore 'appPermissions': UsageDescription keys from all Info.plists
    (app + extensions) and entitlements embedded in the Mach-O signatures
    (present if the build was ad-hoc/ldid-signed; empty for fully unsigned builds).
    """
    privacy, entitlements = {}, set()
    bundle_re = re.compile(re.escape(app_prefix) + r"(?:(?:PlugIns|Extensions|AppClips)/[^/]+\.app[a-z]*/)?Info\.plist$")
    for n in names:
        if not bundle_re.match(n):
            continue
        try:
            pl = plistlib.loads(z.read(n))
        except Exception:
            continue
        for k, v in pl.items():
            if k.endswith("UsageDescription") and isinstance(v, str):
                privacy.setdefault(k, v)
        exe = pl.get("CFBundleExecutable")
        if exe:
            binary = n.rsplit("/", 1)[0] + "/" + exe
            if binary in names:
                entitlements |= extract_entitlements(z.read(binary))
    return {
        "entitlements": sorted(entitlements - IGNORED_ENTITLEMENTS),
        "privacy": privacy,
    }


def extract_entitlements(binary: bytes):
    """Find embedded entitlements blobs (magic 0xfade7171) in a Mach-O."""
    found = set()
    start = 0
    while True:
        i = binary.find(b"\xfa\xde\x71\x71", start)
        if i < 0:
            break
        start = i + 4
        length = struct.unpack(">I", binary[i + 4:i + 8])[0]
        blob = binary[i + 8:i + length]
        if not blob.lstrip().startswith(b"<?xml"):
            continue
        try:
            found |= set(plistlib.loads(blob).keys())
        except Exception:
            pass
    return found


def normalize_png(data: bytes) -> bytes:
    """Convert Apple's CgBI-'optimized' PNG into a standard PNG (no-op otherwise)."""
    sig = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(sig):
        raise ValueError("not a PNG")
    pos, chunks = 8, []
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunks.append((ctype, data[pos + 8:pos + 8 + length]))
        pos += 12 + length
    if not any(c == b"CgBI" for c, _ in chunks):
        return data

    ihdr = next(d for c, d in chunks if c == b"IHDR")
    width, height, depth, ctype_ = struct.unpack(">IIBB", ihdr[:10])
    if depth != 8 or ctype_ != 6:
        raise ValueError("unsupported CgBI format")
    idat = b"".join(d for c, d in chunks if c == b"IDAT")
    raw = zlib.decompress(idat, -15)  # raw deflate, no zlib header

    stride = width * 4
    out = bytearray()
    prev = bytearray(stride)
    i = 0
    for _ in range(height):
        ftype = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        # Undo PNG filtering
        for x in range(stride):
            a = line[x - 4] if x >= 4 else 0
            b = prev[x]
            c = prev[x - 4] if x >= 4 else 0
            if ftype == 1:
                line[x] = (line[x] + a) & 0xFF
            elif ftype == 2:
                line[x] = (line[x] + b) & 0xFF
            elif ftype == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 0xFF
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 0xFF
        prev = line
        # BGRA (premultiplied) -> RGBA
        fixed = bytearray(stride)
        for x in range(0, stride, 4):
            bl, g, r, al = line[x], line[x + 1], line[x + 2], line[x + 3]
            if 0 < al < 255:
                r = min(255, r * 255 // al)
                g = min(255, g * 255 // al)
                bl = min(255, bl * 255 // al)
            fixed[x:x + 4] = bytes((r, g, bl, al))
        out += b"\x00" + fixed

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    new_ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return sig + chunk(b"IHDR", new_ihdr) + chunk(b"IDAT", zlib.compress(bytes(out), 9)) + chunk(b"IEND", b"")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class IpaInfo:
    def __init__(self, path: Path):
        plist, icon = read_ipa(path)
        self.plist = plist
        self.icon = icon
        self.bundle_id = plist.get("CFBundleIdentifier") or ""
        self.name = plist.get("CFBundleDisplayName") or plist.get("CFBundleName") or self.bundle_id
        self.version = str(plist.get("CFBundleShortVersionString") or "0")
        self.build_version = str(plist.get("CFBundleVersion") or self.version)
        self.min_os = str(plist.get("MinimumOSVersion") or "")
        self.permissions = plist.get("_permissions", {"entitlements": [], "privacy": {}})
        if not self.bundle_id:
            raise ValueError("IPA hat keine CFBundleIdentifier")
