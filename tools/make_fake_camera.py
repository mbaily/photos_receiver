#!/usr/bin/env python3
"""Generate labelled synthetic camera originals in an empty Sony-style card folder.

Run with the repository's .venv/bin/python. Requires ffmpeg and exiftool.
This creates ordinary files; mount a disk image separately to test card discovery.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw


def run(*args):
    subprocess.run([str(a) for a in args], check=True, stdout=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('destination', type=Path)
    ap.add_argument('--seed', type=int, default=1, help='change to generate distinct originals')
    args = ap.parse_args()
    root = args.destination.resolve()
    for command in ('ffmpeg', 'exiftool'):
        if not shutil.which(command):
            ap.error(f'{command} is required')
    root.mkdir(parents=True, exist_ok=True)
    if any(p.name != 'lost+found' for p in root.iterdir()):
        ap.error('destination must be empty (apart from lost+found); originals are never overwritten')
    stills = root / 'DCIM/100MSDCF'
    clips = root / 'PRIVATE/M4ROOT/CLIP'
    stills.mkdir(parents=True)
    clips.mkdir(parents=True)
    for number, color in enumerate(('#2563eb', '#059669', '#d97706'), 1):
        path = stills / f'DSC{number:05}.JPG'
        image = Image.new('RGB', (1600, 1066), color)
        draw = ImageDraw.Draw(image)
        draw.text((100, 120), f'SYNTHETIC CAMERA TEST / seed {args.seed} / photo {number}',
                  fill='white', font_size=42)
        draw.rectangle((100, 250, 1500, 850), outline='white', width=12)
        image.save(path, quality=92)
        run('exiftool', '-overwrite_original', '-Make=SONY', '-Model=ZV-E10',
            '-LensModel=Synthetic test lens', '-ImageDescription=Generated test image, not a camera exposure',
            f'-DateTimeOriginal=2026:11:14 10:00:0{number}', path)
    for number in (1, 2):
        path = clips / f'C{number:04}.MP4'
        run('ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
            f'testsrc2=size=1280x720:rate=25, hue=h={args.seed * 30 + number * 60}',
            '-f', 'lavfi', '-i', f'sine=frequency={400 + args.seed * 20 + number * 100}:sample_rate=48000',
            '-t', '3', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-movflags', '+faststart',
            '-metadata', f'comment=SYNTHETIC CAMERA TEST seed {args.seed}, clip {number}', path)
        (clips / f'C{number:04}M01.XML').write_text(
            '<?xml version="1.0"?>\n<NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.00">'
            f'<CreationDate value="2026-11-14T10:01:0{number}+11:00"/>'
            '<Device manufacturer="Sony" modelName="ZV-E10"/>'
            '<VideoFormat><VideoFrame formatFps="25p"/>'
            '<VideoLayout pixel="1280" numOfVerticalLine="720"/></VideoFormat>'
            '<Duration value="75"/></NonRealTimeMeta>\n')
    # Regression case: two card entries with exactly the same bytes.
    shutil.copyfile(stills / 'DSC00001.JPG', stills / 'DSC00004.JPG')
    manifest = {}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            with path.open('rb') as f:
                digest = hashlib.file_digest(f, 'sha256').hexdigest()
            manifest[path.relative_to(root).as_posix()] = {'sha256': digest, 'bytes': path.stat().st_size}
    (root / 'SYNTHETIC-MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Generated 4 JPEG entries (3 unique), 2 MP4 videos and 2 Sony-style XML sidecars in {root}')


if __name__ == '__main__':
    main()
