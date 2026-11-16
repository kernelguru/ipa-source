"""Tiny PNG helpers (stdlib only): size, decoding to RGBA for analysis, blank detection."""
import struct
import zlib

SIG = b"\x89PNG\r\n\x1a\n"


def _chunks(data: bytes):
    pos = 8
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        yield data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]
        pos += 12 + length


def size(data: bytes):
    """(width, height) or None if not a PNG."""
    if not data.startswith(SIG) or len(data) < 24:
        return None
    for ctype, body in _chunks(data):
        if ctype == b"IHDR":
            return struct.unpack(">II", body[:8])
    return None


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)


def decode_rgba(data: bytes, max_pixels=1024 * 1024):
    """
    Decode a non-interlaced 8-bit PNG (gray, gray+alpha, RGB, RGBA, palette) into
    (w, h, bytes RGBA). Returns None for unsupported variants.
    """
    if not data.startswith(SIG):
        return None
    ihdr, plte, trns, idat = None, b"", b"", []
    for ctype, body in _chunks(data):
        if ctype == b"IHDR":
            ihdr = body
        elif ctype == b"PLTE":
            plte = body
        elif ctype == b"tRNS":
            trns = body
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"CgBI":
            return None  # normalize first
    if not ihdr:
        return None
    w, h, depth, ctype_, _, _, interlace = struct.unpack(">IIBBBBB", ihdr[:13])
    if depth != 8 or interlace or w * h > max_pixels:
        return None
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype_)
    if not bpp:
        return None
    raw = zlib.decompress(b"".join(idat))
    stride = w * bpp
    out = bytearray()
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        if f:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                if f == 1:
                    line[x] = (line[x] + a) & 0xFF
                elif f == 2:
                    line[x] = (line[x] + b) & 0xFF
                elif f == 3:
                    line[x] = (line[x] + ((a + b) >> 1)) & 0xFF
                elif f == 4:
                    line[x] = (line[x] + _paeth(a, b, prev[x - bpp] if x >= bpp else 0)) & 0xFF
        prev = line
        if ctype_ == 6:
            out += line
        elif ctype_ == 2:
            for x in range(0, stride, 3):
                out += line[x:x + 3] + b"\xff"
        elif ctype_ == 0:
            for v in line:
                out += bytes((v, v, v, 255))
        elif ctype_ == 4:
            for x in range(0, stride, 2):
                out += bytes((line[x], line[x], line[x], line[x + 1]))
        elif ctype_ == 3:
            for v in line:
                r, g, b_ = plte[v * 3:v * 3 + 3] or b"\0\0\0"
                a = trns[v] if v < len(trns) else 255
                out += bytes((r, g, b_, a))
    return w, h, bytes(out)


def is_blank(data: bytes) -> bool:
    """True if the image is (almost) fully transparent or a single flat colour."""
    dec = decode_rgba(data)
    if not dec:
        return False
    w, h, px = dec
    step = max(1, (w * h) // 4096) * 4          # sample ~4096 pixels
    lo, hi = [255] * 3, [0] * 3
    opaque = 0
    for i in range(0, len(px), step):
        if px[i + 3] < 16:
            continue
        opaque += 1
        for c in range(3):
            v = px[i + c]
            lo[c] = min(lo[c], v)
            hi[c] = max(hi[c], v)
    samples = len(px) // step
    if opaque < samples * 0.05:
        return True                              # (nearly) transparent
    return max(hi[c] - lo[c] for c in range(3)) < 10   # flat colour
