#!/usr/bin/env python3
"""Send photos/videos straight from a USB-connected camera (e.g. Sony ZV-E10) to the photo server.

Runs on the laptop the camera is plugged into. Mounted cards are read directly.
PTP cameras (Canon, etc.) use gphoto2 and temporary storage, one media file at a time.
Uses the same protocol as the iPhone shortcut: POST hashes to /wanted, then PUT only
the files the server doesn't already have. Standard library only - copy this one file
to any machine with Python 3.10+.

Camera: Menu > Setup > USB > USB Connection Mode = "Mass Storage", then plug in and
mount (opening it in the file manager is enough). For Canon/PTP, install gphoto2,
turn the camera on, and close Dolphin camera windows before running. With no mounted
card the script auto-detects a gphoto2 USB camera; --gphoto forces that mode.

    python3 send_camera.py                    # auto-detect the card
    python3 send_camera.py /media/me/CARD     # or name the mount point (E:\\ on Windows)
    python3 send_camera.py --gphoto           # USB PTP camera (gphoto2 required)
    python3 send_camera.py --dry-run          # ask the server what's new, upload nothing

The server uses a private CA ("Git23069 Root Authority"). Either install it in the
system trust store, or pass --cacert nucbox3.crt (or set PHOTO_SERVER_CACERT), or pass
--insecure to skip certificate verification.
"""

import argparse
import getpass
import hashlib
import json
import os
import re
import ssl
import shutil
import subprocess
import tempfile
from contextlib import ExitStack
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path, PurePosixPath

DEFAULT_SERVER = os.environ.get("PHOTO_SERVER", "https://photos.example.com:9443/iphone_photos")

# Sony layout: stills in DCIM/1xxMSDCF, XAVC S video in PRIVATE/M4ROOT/CLIP.
MEDIA_EXTS = {".jpg", ".jpeg", ".arw", ".hif", ".heif", ".mp4", ".mov", ".mts"}
CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".arw": "image/x-sony-arw",
    ".hif": "image/heif", ".heif": "image/heif",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".mts": "video/mp2t",
}
CHUNK = 4 * 1024 * 1024
WANTED_BATCH = 200
CACHE_FILE = Path.home() / ".cache" / "send_camera_hashes.json"
TOKEN_FILE = Path.home() / ".config" / "photo_token"


MEDIA_DIRS = ("DCIM", "PRIVATE/M4ROOT/CLIP")


def is_camera_root(vol: Path) -> bool:
    """A card that has only ever recorded video has no DCIM folder, just PRIVATE/M4ROOT/CLIP."""
    return any((vol / sub).is_dir() for sub in MEDIA_DIRS)


def find_camera_roots() -> list[Path]:
    """Look for mounted volumes containing a camera's media folders (Linux, macOS, Windows)."""
    user = getpass.getuser()
    bases = [Path("/media") / user, Path("/run/media") / user, Path("/media"), Path("/mnt"),
             Path("/Volumes")]
    vols = []
    for base in bases:
        try:
            vols += [p for p in base.iterdir() if p.is_dir()]
        except OSError:
            pass
    if os.name == "nt":
        vols += [Path(f"{d}:\\") for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
    roots = []
    for vol in vols:
        try:
            if is_camera_root(vol) and vol not in roots:
                roots.append(vol)
        except OSError:
            pass
    return roots


def iter_media(root: Path):
    for sub in MEDIA_DIRS:
        base = root / sub
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in MEDIA_EXTS:
                    yield path


class GPhotoCamera:
    """Read a PTP camera through gphoto2; spool at most one media file at a time."""

    def __init__(self, root: Path, port: str | None = None):
        self.root = root
        if not shutil.which('gphoto2'):
            raise RuntimeError('Install gphoto2 on this computer (Debian/Ubuntu: sudo apt install gphoto2).')
        if port is None:
            output = self.run('--auto-detect')
            ports = re.findall(r'\s(usb:\S+)\s*$', output, re.MULTILINE)
            if not ports:
                raise RuntimeError('No USB camera detected. Turn it on and check the USB data cable.')
            if len(ports) != 1:
                raise RuntimeError('Several USB cameras detected; select one with --camera-port usb:BUS,DEVICE.')
            port = ports[0]
        self.port = port
        output = self.run('--quiet', '--parsable', '--recurse', '--list-files')
        self.entries = {}
        for line in output.splitlines():
            match = re.fullmatch(r"FILENAME='(.*)'(.*)", line)
            if not match:
                if line.strip():
                    raise RuntimeError('Unexpected gphoto2 file listing; requires --quiet --parsable support.')
                continue
            remote = PurePosixPath(match[1])
            if '..' in remote.parts or not remote.is_absolute():
                raise RuntimeError('Unsafe path in camera file listing')
            attrs = dict(re.findall(r'(FILESIZE|FILEMTIME)=(-?\d+)', match[2]))
            local = root.joinpath(*remote.parts[1:])
            self.entries[local] = (remote, attrs)
        self.files = [p for p in self.entries if p.suffix.lower() in MEDIA_EXTS]

    def run(self, *args, stdout=None):
        command = ['gphoto2']
        if getattr(self, 'port', None):
            command += ['--port', self.port]
        command += list(args)
        try:
            result = subprocess.run(command, stdout=stdout if stdout is not None else subprocess.PIPE,
                                    stderr=subprocess.PIPE, env={**os.environ, 'LC_ALL': 'C'},
                                    timeout=600)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError('Camera operation timed out; check the cable and camera power.') from e
        if result.returncode:
            detail = result.stderr.decode('utf-8', 'replace').strip()
            raise RuntimeError('gphoto2 failed. Close Dolphin/Gwenview camera windows and retry.\n' + detail)
        return result.stdout.decode('utf-8', 'replace') if stdout is None else ''

    def size(self, path):
        return int(self.entries[path][1].get('FILESIZE', 0))

    def materialize(self, path):
        if path.is_file():
            return
        remote, attrs = self.entries[path]
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('wb') as target:
                self.run('--quiet', '--folder', str(remote.parent), '--no-recurse',
                         '--get-file', remote.name, '--stdout', stdout=target)
            if 'FILESIZE' in attrs and path.stat().st_size != int(attrs['FILESIZE']):
                raise RuntimeError(f'Incomplete camera download: {remote}')
            if 'FILEMTIME' in attrs:
                stamp = int(attrs['FILEMTIME'])
                os.utime(path, (stamp, stamp))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        # Preserve Sony sidecars when a PTP-capable Sony is used too.
        if path.suffix.lower() in {'.mp4', '.mov', '.mts'}:
            for candidate in self.entries:
                if (candidate.parent == path.parent and candidate.stem == path.stem + 'M01'
                        and candidate.suffix.lower() == '.xml'):
                    self.materialize(candidate)


def find_sidecar(path: Path) -> Path | None:
    """Sony writes clip metadata next to the video: C0001.MP4 -> C0001M01.XML."""
    for candidate in path.parent.glob(f"{path.stem}M01.*"):
        if candidate.suffix.lower() == ".xml":
            return candidate
    return None


def exif_datetime(path: Path) -> datetime | None:
    """DateTimeOriginal from a JPEG or a TIFF-based raw (ARW), read with the standard library."""
    try:
        with open(path, "rb") as f:
            base = 0
            if f.read(2) == b"\xff\xd8":                    # JPEG: find the Exif APP1 segment
                while True:
                    marker = f.read(4)
                    if len(marker) < 4 or marker[0] != 0xFF or marker[1] in (0xDA, 0xD9):
                        return None
                    size = int.from_bytes(marker[2:4], "big")
                    if marker[1] == 0xE1 and f.read(6) == b"Exif\0\0":
                        base = f.tell()
                        break
                    f.seek(size - (8 if marker[1] == 0xE1 else 2), 1)
            f.seek(base)
            header = f.read(8)
            order = {b"II": "little", b"MM": "big"}.get(header[:2])
            if not order:
                return None

            def num(raw: bytes) -> int:
                return int.from_bytes(raw, order)

            def ifd(offset: int) -> dict[int, bytes]:
                f.seek(base + offset)
                count = num(f.read(2))
                data = f.read(12 * count)
                return {num(data[i:i + 2]): data[i + 8:i + 12] for i in range(0, len(data), 12)}

            ifd0 = ifd(num(header[4:8]))
            exif = ifd(num(ifd0[0x8769])) if 0x8769 in ifd0 else {}
            for tags, tag in ((exif, 0x9003), (exif, 0x9004), (ifd0, 0x0132)):   # original, digitized, modified
                if tag in tags:
                    f.seek(base + num(tags[tag]))
                    return datetime.strptime(f.read(19).decode("ascii"), "%Y:%m:%d %H:%M:%S")
    except (OSError, ValueError, KeyError):
        pass
    return None


def capture_time(path: Path) -> datetime:
    """When the shot was taken, as the camera's own clock recorded it.

    The card's file timestamps are not used unless nothing better exists: the laptop can
    misread them (a card written in local time read as UTC shifts every file by hours).
    """
    if sidecar := find_sidecar(path):
        try:
            if m := re.search(r'<CreationDate\s+value="(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)', sidecar.read_text("utf-8", "replace")):
                return datetime.fromisoformat(m[1])
        except (OSError, ValueError):
            pass
    return exif_datetime(path) or datetime.fromtimestamp(path.stat().st_mtime)


def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache))
    except OSError:
        pass


def invalidate_hash(path: Path, cache: dict) -> None:
    """A failed verification must not leave a stale same-size/mtime cache entry."""
    prefix = str(path.resolve()) + '|'
    for key in list(cache):
        if key.startswith(prefix):
            cache.pop(key, None)
    save_cache(cache)


def sha256_of(path: Path, cache: dict) -> str:
    """Hash a file, reusing a cached result when size and mtime are unchanged."""
    st = path.stat()
    key = f"{path.resolve()}|{st.st_dev}|{st.st_ino}|{st.st_size}|{st.st_mtime_ns}|{st.st_ctime_ns}"
    if key in cache:
        return cache[key]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    cache[key] = h.hexdigest()
    return cache[key]


class Server:
    def __init__(self, base: str, cacert: str | None, insecure: bool = False, token: str = ""):
        self.base = base.rstrip("/")
        # Sent with every request: the token, then the chosen library and duplicate override.
        self.auth = {"Authorization": f"Bearer {token}"} if token else {}
        self.allow_duplicates = False
        self.repairs: set[str] = set()
        self.ctx = None
        if insecure:   # encrypted, but the server's identity is not checked
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        elif cacert:
            self.ctx = ssl.create_default_context(cafile=cacert)
            # Python 3.13+ rejects CAs lacking a keyUsage extension (the home CA does);
            # chain and hostname are still verified.
            self.ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)

    def libraries(self) -> list[dict] | None:
        """The server's libraries, or None if it is an older single-library server."""
        req = urllib.request.Request(f"{self.base}/libraries", headers=self.auth)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=60) as resp:
                return json.load(resp)["libraries"]
        except urllib.error.HTTPError as e:
            if e.code in (404, 405):
                return None
            raise

    def use_library(self, library_id: str) -> None:
        self.auth["X-Photo-Library"] = library_id

    def wanted(self, hashes: list[str], have_sidecar: list[str]) -> tuple[set[str], set[str], dict[str, str]]:
        """-> (files the library lacks, files it has but whose sidecar it lacks,
        {hash: other library's name} for files another library already has)."""
        body = json.dumps({"hashes": hashes, "have_sidecar": have_sidecar, "detail": True,
                           "allow_elsewhere": self.allow_duplicates}).encode()
        req = urllib.request.Request(f"{self.base}/wanted", data=body, method="POST",
                                     headers={"Content-Type": "application/json", **self.auth})
        with urllib.request.urlopen(req, context=self.ctx, timeout=60) as resp:
            data = json.load(resp)
        self.repairs.update(data.get('repair_wanted', []))
        return set(data["wanted"]), set(data.get("sidecar_wanted", [])), data.get("elsewhere", {})

    def send_sidecar(self, sidecar: Path, digest: str) -> None:
        req = urllib.request.Request(
            f"{self.base}/sidecar/{digest}", data=sidecar.read_bytes(), method="PUT",
            headers={"Content-Type": "application/xml", **self.auth},
        )
        with urllib.request.urlopen(req, context=self.ctx, timeout=60) as resp:
            resp.read()

    def upload(self, path: Path, digest: str, root: Path) -> dict:
        # Same naming as the iPhone shortcut: <sha256>_<capture time>.<ext>.
        stamp = capture_time(path).strftime("%Y-%m-%d_%H-%M-%S")
        ext = path.suffix.lower()
        name = urllib.parse.quote(f"{digest}_{stamp}{ext}")
        size = path.stat().st_size
        with open(path, "rb") as f:
            req = urllib.request.Request(
                f"{self.base}/{name}", data=f, method="PUT",
                headers={
                    "Content-Type": CONTENT_TYPES.get(ext, "application/octet-stream"),
                    "Content-Length": str(size),
                    "X-Photo-Source": "usb_camera",
                    "X-Photo-SHA256": digest,
                    "X-Original-Filename": path.name,
                    "X-Source-Path": path.relative_to(root).as_posix(),
                    **({"X-Allow-Duplicates": "1"} if self.allow_duplicates else {}),
                    **self.auth,
                },
            )
            # Big videos can take a while over the internet.
            with urllib.request.urlopen(req, context=self.ctx, timeout=600) as resp:
                result = json.load(resp)
        if result.get("sha256") != digest:
            raise RuntimeError(f"server hash mismatch: {result}")
        if sidecar := find_sidecar(path):   # if this fails, the next run sends it on its own
            self.send_sidecar(sidecar, digest)
            result["sidecar"] = sidecar.name
        return result


def choose_library(libs: list[dict], wanted: str | None) -> dict | None:
    """Pick the library to upload to: --library, else a menu, else the server's upload library."""
    online = [l for l in libs if l["online"]]
    if wanted:
        matches = [l for l in libs if l["id"] == wanted] or [l for l in libs if l["name"].lower() == wanted.lower()]
        if len(matches) != 1:
            print(f"No library called '{wanted}'. The server has: " + ", ".join(l["name"] for l in libs), file=sys.stderr)
            return None
        if not matches[0]["online"]:
            where = f" (it is on {matches[0]['host']})" if matches[0].get("host") else ""
            print(f"Library '{matches[0]['name']}' is not plugged in to the server{where}.", file=sys.stderr)
            return None
        return matches[0]
    if not online:
        print("No photo library is plugged in to the server.", file=sys.stderr)
        return None
    default = next((l for l in online if l["inbox"]), online[0])
    if len(online) == 1 or not sys.stdin.isatty():
        return default
    print("Which library should these go to?")
    for n, lib in enumerate(online, 1):
        items = "?" if lib.get("items") is None else lib["items"]
        held = "  [read-only for now: suspected older copy]" if lib.get("read_only") else ""
        print(f"  {n}. {lib['name']}  ({items} items){'  [default]' if lib is default else ''}{held}")
    for lib in libs:
        if not lib["online"]:
            print(f"     {lib['name']}  ({'on ' + lib['host'] if lib.get('host') else 'not plugged in'})")
    while True:
        try:
            answer = input(f"Library [1-{len(online)}, Enter = {default['name']}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(online):
            return online[int(answer) - 1]
        named = [l for l in online if l["name"].lower() == answer.lower()]
        if len(named) == 1:
            return named[0]
        print("  Type one of the numbers above.")


def refusal(error: urllib.error.HTTPError) -> str:
    """What the server said when it answered with an error (it was reached; it said no)."""
    try:
        detail = json.load(error)["detail"]
    except Exception:
        detail = ""
    return str(detail) if detail else f"HTTP {error.code} {error.reason}"


def unreachable(server: str, error) -> str:
    """Why the server could not be used - with the fix, when it is the certificate."""
    reason = getattr(error, "reason", error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (f"{server} could not be verified: {reason.verify_message or reason}.\n"
                "It uses a private CA: copy the server's CA certificate (nucbox3.crt) next to this script, or\n"
                "pass --cacert FILE / set PHOTO_SERVER_CACERT. --insecure skips the check, but then the upload\n"
                "token could be sent to an impostor.")
    return f"Could not reach {server}: {error}"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def run(resources: ExitStack) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", type=Path, help="camera/card mount point (auto-detected if omitted)")
    ap.add_argument("--server", default=os.environ.get("PHOTO_SERVER", DEFAULT_SERVER),
                    help=f"receiver base URL (default {DEFAULT_SERVER}, or $PHOTO_SERVER)")
    ap.add_argument("--cacert", default=os.environ.get("PHOTO_SERVER_CACERT"),
                    help="CA certificate file to trust for the server (or $PHOTO_SERVER_CACERT)")
    ap.add_argument("--token", default=os.environ.get("PHOTO_TOKEN", ""),
                    help=f"upload token, if the server requires one (or $PHOTO_TOKEN, or {TOKEN_FILE})")
    ap.add_argument("--library", help="library to upload to, by name or id (default: ask, or the server's upload library)")
    ap.add_argument("--allow-duplicates", action="store_true",
                    help="also upload files that another library on the server already has")
    ap.add_argument("--insecure", action="store_true",
                    help="don't verify the server's TLS certificate (ignored if --cacert is given)")
    ap.add_argument("--dry-run", action="store_true", help="report what's new, upload nothing")
    ap.add_argument('--gphoto', action='store_true', help='use a USB PTP camera via gphoto2 instead of a mounted card')
    ap.add_argument('--camera-port', help='gphoto2 USB port, e.g. usb:001,041 (implies --gphoto)')
    args = ap.parse_args()
    camera = None
    if args.source and (args.gphoto or args.camera_port):
        ap.error('choose either a mounted source folder or --gphoto/--camera-port')
    if args.gphoto or args.camera_port:
        root = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix='send-camera-')))
        camera = GPhotoCamera(root, args.camera_port)

    if args.source:
        root = args.source
        if not is_camera_root(root):
            print(f"{root} has no DCIM or PRIVATE/M4ROOT/CLIP folder - is that the camera's mount point?", file=sys.stderr)
            return 1
    elif camera is None:
        roots = find_camera_roots()
        if len(roots) > 1:
            print("Several camera volumes found; pass one explicitly:", *roots, sep="\n  ", file=sys.stderr)
            return 1
        if roots:
            root = roots[0]
        else:
            root = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix='send-camera-')))
            camera = GPhotoCamera(root)

    token = args.token.strip()
    if not token:
        try:
            token = TOKEN_FILE.read_text().strip()
        except OSError:
            pass
    server = Server(args.server, args.cacert, insecure=args.insecure and not args.cacert, token=token)
    server.allow_duplicates = args.allow_duplicates
    library_name = None
    try:
        libs = server.libraries()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print(f"{args.server} rejected the upload token. Put the server's token in {TOKEN_FILE}\n"
                  "(or pass --token / set PHOTO_TOKEN).", file=sys.stderr)
        else:
            print(f"{args.server} refused: {refusal(e)}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(unreachable(args.server, e), file=sys.stderr)
        return 1
    if libs is not None:
        library = choose_library(libs, args.library)
        if library is None:
            return 1
        server.use_library(library["id"])
        library_name = library["name"]
        print(f"Library: {library_name}")
    files = camera.files if camera else list(iter_media(root))
    size_of = camera.size if camera else lambda p: p.stat().st_size
    source_label = f'USB camera ({camera.port})' if camera else str(root)
    print(f"{source_label}: {len(files)} media files, {human(sum(size_of(p) for p in files))}")
    if camera:
        print('Reading camera files for checksums; temporary media files are removed after each read.')

    # Persistent file attributes cannot prove content identity; rehash on every run.
    cache = {}
    hashed: list[tuple[Path, str]] = []
    for i, path in enumerate(files, 1):
        print(f"\r  hashing {i}/{len(files)}", end="", flush=True)
        if camera:
            camera.materialize(path)
        hashed.append((path, sha256_of(path, cache)))
        if camera:
            path.unlink()
    print()

    try:
        wanted: set[str] = set()
        sidecar_wanted: set[str] = set()
        elsewhere: dict[str, str] = {}
        for i in range(0, len(hashed), WANTED_BATCH):
            batch = hashed[i:i + WANTED_BATCH]
            new, lacking, other = server.wanted([d for _, d in batch], [d for p, d in batch if find_sidecar(p)])
            wanted |= new
            sidecar_wanted |= lacking
            elsewhere.update(other)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print(f"{args.server} rejected the upload token. Put the server's token in {TOKEN_FILE}\n"
                  "(or pass --token / set PHOTO_TOKEN).", file=sys.stderr)
        else:
            print(f"{args.server} refused: {refusal(e)}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(unreachable(args.server, e), file=sys.stderr)
        return 1

    todo, seen = [], set()
    for path, digest in hashed:
        if digest in wanted and digest not in seen:  # skip identical copies on the card itself
            seen.add(digest)
            todo.append((path, digest))
    # Clips already on the server whose sidecar XML never arrived.
    sidecars, seen = [], set()
    for path, digest in hashed:
        if digest in sidecar_wanted and digest not in seen:
            seen.add(digest)
            sidecars.append((find_sidecar(path), digest))
    total = sum(size_of(p) for p, _ in todo)
    repairs = getattr(server, 'repairs', set())
    repair_count = sum(d in repairs for _, d in todo)
    print(f"{len(todo) - repair_count} new, {repair_count} original(s) to repair ({human(total)}); "
          f"{len(files) - len(todo)} already held, deliberately deleted, or duplicated on card")
    if elsewhere:
        accepted = {h: name for h, name in elsewhere.items() if h in wanted}
        skipped = {h: name for h, name in elsewhere.items() if h not in wanted}
        for overlap, decision in ((accepted, 'allows upload'), (skipped, 'refused')):
            if not overlap:
                continue
            by_library: dict[str, int] = {}
            for name in overlap.values():
                by_library[name] = by_library.get(name, 0) + 1
            where = ", ".join(f"{n} in '{name}'" for name, n in sorted(by_library.items()))
            print(f"WARNING: {len(overlap)} offered file(s) also exist in another library ({where}); "
                  f"the receiver {decision} for '{library_name}'.")
            if skipped and overlap is skipped:
                print('         Use --allow-duplicates to override the duplicate policy.')
    if sidecars:
        print(f"{len(sidecars)} sidecar file(s) missing on the server")
    if args.dry_run:
        for path, digest in todo:
            print(f"  would send {path.relative_to(root)}" + (' (repair original)' if digest in repairs else ''))
        for sidecar, _ in sidecars:
            print(f"  would send sidecar {sidecar.relative_to(root)}")
        return 0

    sent = errors = 0
    start = time.monotonic()
    for sidecar, digest in sidecars:
        try:
            server.send_sidecar(sidecar, digest)
            print(f"  sidecar           {sidecar.relative_to(root)}")
        except (urllib.error.URLError, OSError) as e:
            errors += 1
            why = refusal(e) if isinstance(e, urllib.error.HTTPError) else e
            print(f"  ERROR sidecar {sidecar.relative_to(root)}: {why}", file=sys.stderr)
    for i, (path, digest) in enumerate(todo, 1):
        rel = path.relative_to(root)
        try:
            if camera:
                camera.materialize(path)
                # Detect camera/card changes between the hash pass and upload.
                if sha256_of(path, {}) != digest:
                    raise RuntimeError('Camera file changed during sync; retry the upload')
            result = server.upload(path, digest, root)
            sent += 1
            extra = f" (+{result['sidecar']})" if result.get("sidecar") else ""
            print(f"  [{i}/{len(todo)}] {result.get('status', '?'):17} {rel}{extra}")
        except (urllib.error.URLError, OSError, RuntimeError, ValueError) as e:
            if not camera and isinstance(e, RuntimeError) and 'hash mismatch' in str(e):
                invalidate_hash(path, cache)
            errors += 1
            why = refusal(e) if isinstance(e, urllib.error.HTTPError) else e
            print(f"  [{i}/{len(todo)}] ERROR {rel}: {why}", file=sys.stderr)
        finally:
            if camera:
                path.unlink(missing_ok=True)
    secs = time.monotonic() - start
    print(f"Done: {sent} sent, {errors} failed in {secs:.0f}s")
    return 1 if errors else 0


def main() -> int:
    try:
        with ExitStack() as resources:
            return run(resources)
    except (RuntimeError, OSError, ValueError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nCancelled; temporary camera files removed.', file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
