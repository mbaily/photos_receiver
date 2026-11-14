Synthetic camera card on the laptop (192.0.2.20)

Created 14 November 2026. This is a real loopback block device and mounted filesystem,
not a USB-bus peripheral. It exercises the same mounted-card discovery and copy path
as a Sony in Mass Storage mode. It does not test USB transport, Sony firmware, PTP,
real ARW decoding, FAT/exFAT behaviour or cable failures.

- Image: ~/.local/share/photos_receiver/fake-camera.img (256 MiB sparse ext4)
- Mount: /mnt/photos-receiver-fake-camera (read-only, nodev,nosuid,noexec)
- Label: SONY_FAKE
- Test library: ~/photos/synthetic-camera-test, name "Synthetic camera test", id 5e3807bb
- Generator: ~/photos_receiver/tools/make_fake_camera.py

The card now holds three batches: DCIM/100MSDCF has twelve JPEG entries (nine unique),
and PRIVATE/M4ROOT/CLIP has six 3-second H.264/AAC MP4s with Sony-style XML sidecars,
plus C0301.MP4 with a UTC-only QuickTime capture date. There are 19 media entries,
16 unique originals and six XML sidecars; the laptop target has all 16 unique originals.
JPEGs contain EXIF capture dates, simulated Sony make/model and a synthetic test label.
Video comments identify the synthetic fixture; moving test patterns and tones are generated.
SYNTHETIC-MANIFEST.json records original file sizes and hashes.

On the laptop, from ~/photos_receiver:

    ./ingest_camera.sh --library "Synthetic camera test" --dry-run
    ./ingest_camera.sh --library "Synthetic camera test"

Simulate unplugging (no data is removed):

    sudo umount /mnt/photos-receiver-fake-camera

Simulate reconnecting:

    sudo mount -o loop,ro,nodev,nosuid,noexec \
      "$HOME/.local/share/photos_receiver/fake-camera.img" /mnt/photos-receiver-fake-camera

Generate a different batch in an empty folder using --seed 4 (no mounting needed if
an explicit source folder is supplied to ingest_camera.sh):

    .venv/bin/python tools/make_fake_camera.py /tmp/new-camera-card-4 --seed 4
    ./ingest_camera.sh /tmp/new-camera-card-4 --library "Synthetic camera test"

The generator refuses nonempty destinations. Change the seed for different originals.
Unmount the fixture before testing automatic discovery of a physical camera; otherwise
both sources appear and ingest correctly asks for an explicit source path.

Verified end-to-end:
- Automatic card discovery and dry run: 5 unique new items, 1 repeated entry.
- Offline ingest (--no-sync): 5 saved; later authenticated catalogue sync: 5 reported.
- Unmount: fixture disappears from camera discovery. Remount: 0 new ingests.
- All source hashes unchanged; every stored original matches its SHA-256.
- Capture times, Sony-style video metadata, dimensions and durations are correct.
- Server /wanted refuses all 5 hashes because the laptop library holds them.
- Laptop browser displays five items and plays the generated video.

Leave the test library and card in place for further testing. No physical camera files
or existing photo libraries were modified.

Further tests (see VALIDATION.md): late-sidecar recovery, browser note/tag/album edits,
delete/re-ingest/restore, album-cover restoration, full-disk preflight, and a portable
library moved between laptop and mini PC all pass. The portable image is
~/.local/share/photos_receiver/portable-library.img on the laptop, mounted at
/mnt/photos-receiver-portable-test; its library is /photos inside that disk, id b9c086c1.
Both loopback disks remain mounted on the laptop. The mini PC mount is empty/offline.
The portable library deliberately overlaps one photo with Synthetic camera test,
so duplicate warnings for that photo are expected.

The conceptual follow-up added a server synthetic library, id df9d15e7, at
~/photos/synthetic-server-audit on the mini PC. It holds four third-batch originals and
one deliberately purged deletion record. Duplicate warnings between the server synthetic
library, laptop synthetic library and portable synthetic library are expected test state.
The original read-only card manifest now covers 25 source files. The UTC-only clip was
used to verify stable dates after refreshing under a different machine timezone.

CLI re-audit: a fourth batch (DSC00301..00304.JPG and C0401/C0402.MP4 + sidecars)
was added. C0402M01.XML deliberately has an invalid capture date. The source manifest
now covers 33 files; the standard camera scan sees 25 media entries / 21 unique originals.
Synthetic camera test on the laptop holds 21 verified originals. Synthetic server audit
holds 10 verified originals plus its previous deliberately purged record. The extra server
original exercises --allow-duplicates with a one-file alias card under
~/.local/share/photos_receiver/reaudit-override-card (its source is the mounted fake card).
Quarantined synthetic originals used for repair tests are preserved under
~/.local/share/photos_receiver/reaudit-originals on each host. See CLI_REAUDIT.md.
