"""
ZIP64-safe IPA extract/pack for signing, independent of zsign's own archiver.

pack() deletes every file right after it has been written into the new IPA, so the
peak disk usage is about the unpacked size instead of twice that.
"""
import os
import shutil
import stat
import time
import zipfile
from pathlib import Path


MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
               b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}


def uncompressed_size(ipa: Path):
    """(total uncompressed bytes, largest single file)"""
    with zipfile.ZipFile(ipa) as z:
        sizes = [i.file_size for i in z.infolist()]
    return sum(sizes), max(sizes or [0])


def _safe(dest: Path, name: str) -> Path:
    p = (dest / name).resolve()
    if not str(p).startswith(str(dest.resolve()) + os.sep) and p != dest.resolve():
        raise ValueError(f"Unsicherer Pfad in der IPA: {name}")
    return p


def extract(ipa: Path, dest: Path, log=print):
    t0 = time.time()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ipa) as z:
        for zi in z.infolist():
            target = _safe(dest, zi.filename)
            mode = (zi.external_attr >> 16) & 0xFFFF
            if zi.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                link = z.read(zi).decode("utf-8", "replace")
                if target.exists() or target.is_symlink():
                    target.unlink()
                os.symlink(link, target)
                continue
            with z.open(zi) as src, open(target, "wb") as out:
                head = src.read(4)
                out.write(head)
                shutil.copyfileobj(src, out, length=8 * 1024 * 1024)
            perm = (mode & 0o777) | 0o644
            if head in MACHO_MAGIC:
                perm |= 0o755            # executables/dylibs must stay executable on iOS
            os.chmod(target, perm)
    log(f">>> Entpackt in {time.time() - t0:.0f} s")


def pack(root: Path, out: Path, level: int = 0, delete: bool = True, log=print):
    """Zip everything below root into out (ZIP64 automatic)."""
    t0 = time.time()
    tmp = out.with_name(out.name + ".part")
    comp = zipfile.ZIP_STORED if level <= 0 else zipfile.ZIP_DEFLATED
    kw = {} if level <= 0 else {"compresslevel": level}
    with zipfile.ZipFile(tmp, "w", compression=comp, allowZip64=True, **kw) as z:
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            dirnames.sort()
            rel_dir = os.path.relpath(dirpath, root)
            if rel_dir != ".":
                zi = zipfile.ZipInfo(rel_dir.replace(os.sep, "/") + "/")
                zi.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
                z.writestr(zi, b"")
            for name in sorted(filenames) + sorted(d for d in dirnames if os.path.islink(os.path.join(dirpath, d))):
                full = os.path.join(dirpath, name)
                arc = os.path.relpath(full, root).replace(os.sep, "/")
                if os.path.islink(full):
                    zi = zipfile.ZipInfo(arc)
                    zi.external_attr = (stat.S_IFLNK | 0o777) << 16
                    z.writestr(zi, os.readlink(full))
                else:
                    z.write(full, arc)
                if delete:
                    os.unlink(full)
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
    tmp.replace(out)
    log(f">>> Verpackt in {time.time() - t0:.0f} s ({out.stat().st_size / 2**30:.2f} GB)")
