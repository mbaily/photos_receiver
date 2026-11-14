"""Extract camera metadata from stored files into the photos table.

Uses exiftool when installed (everything, including Sony MakerNotes and the XML that
Sony embeds in its MP4s); otherwise falls back to Pillow EXIF ("basic"). Sony video
sidecars (C0001M01.XML) are merged in as SonyXML:* keys.
"""

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

import photo_library
from photo_db import media_type_for, update_columns

EXIFTOOL = os.environ.get("EXIFTOOL") or shutil.which("exiftool")
DROP_KEYS = {
    "SourceFile", "ExifTool:ExifToolVersion", "System:Directory", "System:FileName",
    "System:FilePermissions", "System:FileAccessDate", "System:FileInodeChangeDate",
    "System:FileModifyDate",
}


def jsonable(value):
    if isinstance(value, bytes):
        if len(value) <= 64:
            text = value.rstrip(b"\x00").decode("utf-8", "replace")
            return text if text.isprintable() else value.hex()
        return f"(binary, {len(value)} bytes)"
    if isinstance(value, (tuple, list)):
        return [jsonable(v) for v in value]
    if isinstance(value, (int, str, bool)) or value is None:
        return value.strip("\x00 ") if isinstance(value, str) else value
    try:
        f = float(value)  # IFDRational etc.
        return int(f) if f.is_integer() else round(f, 6)
    except (TypeError, ValueError, ZeroDivisionError):
        return str(value)


def run_exiftool(path: Path) -> dict | None:
    if not EXIFTOOL:
        return None
    try:
        proc = subprocess.run(
            [EXIFTOOL, "-json", "-G1", "-unknown", "-api", "LargeFileSupport=1", str(path)],
            capture_output=True, timeout=180,
        )
        data = json.loads(proc.stdout)[0]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return {k: v for k, v in data.items() if k not in DROP_KEYS}


def pillow_metadata(path: Path) -> dict:
    out: dict = {}
    try:
        from PIL import ExifTags, Image
        import pillow_heif
        pillow_heif.register_heif_opener()
        with Image.open(path) as img:
            out["File:ImageWidth"], out["File:ImageHeight"] = img.size
            exif = img.getexif()
            groups = [
                ("IFD0", exif, ExifTags.TAGS),
                ("ExifIFD", exif.get_ifd(0x8769), ExifTags.TAGS),
                ("GPS", exif.get_ifd(0x8825), ExifTags.GPSTAGS),
                ("InteropIFD", exif.get_ifd(0xA005), ExifTags.TAGS),
            ]
            for group, ifd, names in groups:
                for tag, value in ifd.items():
                    if group == "IFD0" and tag in (0x8769, 0x8825, 0xA005):
                        continue  # sub-IFD pointers
                    if (isinstance(value, tuple) and len(value) == 2
                            and all(isinstance(v, int) for v in value) and value[1]):
                        value = value[0] / value[1]  # rational stored as (num, den)
                    out[f"{group}:{names.get(tag, f'0x{tag:04x}')}"] = jsonable(value)
    except Exception:
        pass
    if path.suffix.lower() == ".arw":
        try:
            import rawpy
            with rawpy.imread(str(path)) as raw:
                s = raw.sizes
                out.setdefault("File:ImageWidth", s.width)
                out.setdefault("File:ImageHeight", s.height)
                out["RAW:RawWidth"], out["RAW:RawHeight"] = s.raw_width, s.raw_height
                out["RAW:Flip"] = s.flip
                out["RAW:CameraWhiteBalance"] = [round(x, 4) for x in raw.camera_whitebalance]
                out["RAW:BlackLevel"] = list(raw.black_level_per_channel)
                out["RAW:WhiteLevel"] = raw.white_level
        except Exception:
            pass
    return out


def parse_sidecar(xml_text: str) -> dict:
    """Flatten Sony NonRealTimeMeta XML into {'Element:attr': value, 'Item:<name>': value}."""
    out: dict = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "Item" and "name" in el.attrib:
            out[f"Item:{el.attrib['name']}"] = el.attrib.get("value")
            continue
        for attr, value in el.attrib.items():
            key = f"{tag}:{attr}"
            n = 2
            while key in out:
                key, n = f"{tag}:{attr}#{n}", n + 1
            out[key] = value
        if el.text and el.text.strip():
            out[tag] = el.text.strip()
    return out


def lookup(meta: dict, *names: str):
    """First value whose tag name (ignoring the 'Group:' prefix) matches, in priority order."""
    for name in names:
        for key, value in meta.items():
            if key.split(":", 1)[-1] == name and value not in (None, "", "Unknown"):
                return value
    return None


def local_capture_timezone() -> str:
    """An IANA name, preserving daylight-saving rules for the date of the capture."""
    candidates = [os.environ.get('TZ', '').lstrip(':')]
    try:
        candidates.append(str(Path('/etc/localtime').resolve()).split('/zoneinfo/', 1)[1])
    except (OSError, IndexError):
        pass
    try:
        candidates.append(Path('/etc/timezone').read_text().strip())
    except OSError:
        pass
    for name in candidates:
        try:
            ZoneInfo(name)
            return name
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return 'UTC'


def capture_zone(name):
    if match := re.fullmatch(r'([+-])(\d{2}):(\d{2})', name):
        minutes = int(match[2]) * 60 + int(match[3])
        if minutes <= 14 * 60 and int(match[3]) < 60:
            return timezone(timedelta(minutes=minutes * (-1 if match[1] == '-' else 1)))
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return timezone.utc


def original_datetime(meta):
    """Capture timestamps that already describe the camera's wall clock."""
    return (norm_datetime(lookup(meta, 'SubSecDateTimeOriginal', 'DateTimeOriginal'))
            or norm_datetime(lookup(meta, 'CreationDateValue', 'CreationDate:value'))
            or norm_datetime(lookup(meta, 'CreationDate', 'ContentCreateDate')))


def norm_datetime(value, utc: bool = False, capture_timezone: str | None = None) -> str | None:
    if not value:
        return None
    m = re.match(r"(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", str(value))
    if not m or m.group(1) == "0000":
        return None
    try:
        dt = datetime(*map(int, m.groups()))
    except ValueError:
        return None
    if utc:
        zone = capture_zone(capture_timezone) if capture_timezone else None
        dt = dt.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_duration(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value)
    if m := re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", s):
        return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    if m := re.match(r"([\d.]+)\s*s", s):
        return float(m[1])
    return None


def summarize(meta: dict, path: Path, capture_timezone: str | None = None) -> dict:
    taken = original_datetime(meta) or norm_datetime(
        lookup(meta, "CreateDate", "MediaCreateDate"), utc=True, capture_timezone=capture_timezone)
    if not taken and (m := re.search(r"_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.", path.name)):
        taken = f"{m[1]} {m[2]}:{m[3]}:{m[4]}"

    make = lookup(meta, "Make", "Device:manufacturer", "AndroidManufacturer", "DeviceManufacturer")
    model = lookup(meta, "Model", "Device:modelName", "AndroidModel", "DeviceModelName", "CameraModelName")
    camera = None
    if model:
        camera = str(model) if not make or str(model).lower().startswith(str(make).lower()) else f"{make} {model}"
    lens = lookup(meta, "LensModel", "LensID", "LensType", "Lens:modelName", "LensSpec")

    width = height = None
    if size := lookup(meta, "ImageSize"):
        if m := re.fullmatch(r"(\d+)[x ](\d+)", str(size)):
            width, height = int(m[1]), int(m[2])
    width = width or lookup(meta, "ImageWidth", "ExifImageWidth", "VideoLayout:pixel")
    height = height or lookup(meta, "ImageHeight", "ExifImageHeight", "VideoLayout:numOfVerticalLine")

    # exiftool reports the stored size; a 90/270 degree rotation means it is shown the other way up.
    turned = any(re.search(r"\b(90|270)\b", str(v)) for v in (lookup(meta, "Orientation"), lookup(meta, "Rotation"))
                 if isinstance(v, str) or (isinstance(v, int) and v in (90, 270)))
    if turned and width and height:
        width, height = height, width

    duration = parse_duration(lookup(meta, "Duration", "MediaDuration", "TrackDuration"))
    if duration is None and (frames := lookup(meta, "Duration:value")):
        fps = re.match(r"([\d.]+)", str(lookup(meta, "VideoFrame:formatFps") or ""))
        if fps and float(fps[1]):
            duration = int(frames) / float(fps[1])

    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "taken_at": taken,
        "camera": camera,
        "lens": str(lens) if lens else None,
        "width": as_int(width),
        "height": as_int(height),
        "duration_s": round(duration, 2) if duration else None,
    }


def extract(path: Path, sidecar_xml: str | None = None, capture_timezone: str | None = None) -> dict:
    meta = run_exiftool(path)
    extractor = "exiftool"
    if meta is None:
        meta, extractor = pillow_metadata(path), "basic"
    if sidecar_xml:
        meta.update({f"SonyXML:{k}": v for k, v in parse_sidecar(sidecar_xml).items()})
    return {
        "media_type": media_type_for(path.name),
        "exif": json.dumps(meta, ensure_ascii=False),
        "extractor": extractor,
        **summarize(meta, path, capture_timezone),
    }


def index_photo(conn, digest: str) -> None:
    """(Re)extract metadata for one stored photo and save it."""
    row = conn.execute(
        "SELECT saved_filename, sidecar_xml, metadata, exif, taken_at FROM photos WHERE sha256 = ?", (digest,)
    ).fetchone()
    if not row:
        return
    path = photo_library.current().path / Path(row["saved_filename"]).name
    metadata = json.loads(row['metadata'] or '{}')
    hint = metadata.get('capture_timezone') or local_capture_timezone()
    extracted = extract(path, row['sidecar_xml'], hint)
    if not metadata.get('capture_timezone'):
        # Older rows already contain the original UTC->wall-clock conversion. For
        # a restored row, EXIF is read anew but its previous taken_at is retained.
        old_exif = json.loads(row['exif'] or extracted['exif'] or '{}')
        old_utc = norm_datetime(lookup(old_exif, 'CreateDate', 'MediaCreateDate'))
        if row['taken_at'] and old_utc and not original_datetime(old_exif):
            try:
                seconds = int((datetime.fromisoformat(row['taken_at']) - datetime.fromisoformat(old_utc)).total_seconds())
                if abs(seconds) <= 14 * 3600 and seconds % 60 == 0:
                    minutes = abs(seconds) // 60
                    hint = f"{'-' if seconds < 0 else '+'}{minutes // 60:02d}:{minutes % 60:02d}"
                    extracted.update(summarize(json.loads(extracted['exif']), path, hint))
            except ValueError:
                pass
        metadata['capture_timezone'] = hint
        conn.execute('UPDATE photos SET metadata = ? WHERE sha256 = ?', (json.dumps(metadata), digest))
    update_columns(conn, digest, extracted)
