"""Create a tiny fake IPA for tests: python tests/make_ipa.py out.ipa bundle version build"""
import plistlib, struct, sys, zipfile, zlib

def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

def cgbi_png():
    w = h = 4
    rows = b"".join(b"\x00" + bytes((0, 0, 255, 255)) * w for _ in range(h))
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    idat = co.compress(rows) + co.flush()
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"CgBI", b"\x50\x00\x20\x06")
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))

def make(path, bundle="de.test.app", version="1.0", build="1", name="TestApp"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Payload/T.app/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": bundle, "CFBundleDisplayName": name, "CFBundleExecutable": "T",
            "CFBundleShortVersionString": version, "CFBundleVersion": build, "MinimumOSVersion": "17.0",
            "NSLocalNetworkUsageDescription": "LAN",
            "CFBundleIcons": {"CFBundlePrimaryIcon": {"CFBundleIconFiles": ["AppIcon60x60"]}}}))
        z.writestr("Payload/T.app/T", b"\xcf\xfa\xed\xfe")
        z.writestr("Payload/T.app/AppIcon60x60@2x.png", cgbi_png())

if __name__ == "__main__":
    make(*sys.argv[1:])
