Validation audit — 14 November 2026

Acceptance basis

README and tasks2.txt define the intended feature set. The user confirms that the real
Sony USB connection works; physical camera handling is accepted on that basis. Ongoing
camera tests use the loopback card on the laptop, without requiring human camera actions.
Laptop ingestion keeps originals on the laptop and sends a catalogue to the server.
Whole-library network transfer/merge and remote thumbnails remain outside that scope.

Fixes made during the audit

- Repeated hashes on one card no longer suppress the first copy as well.
- Local ingest recovers a Sony sidecar that arrives after its video, without recopying
  the original. Dry runs leave missing sidecars unchanged.
- Receiver sidecar uploads refresh catalogue metadata immediately.
- Restore refreshes the catalogue after re-extracting metadata; refresh_metadata.py
  also updates the catalogue after each successful metadata update.
- Filename collision handling preserves both original and fallback filenames.
- Duplicate checks and original-file commits are serialized across libraries on the
  same machine. Ingest rechecks local duplicates at commit time. Metadata extraction
  runs outside those locks so it does not serialize other uploads.
- SQLite contexts close connections after commit/rollback in library and catalogue DBs.
- Restore recovers explicit album covers unless a replacement was selected meanwhile.
- Delete confirmations describe the current library's trash and restoration accurately.
- requirements-test.txt declares the TestClient dependency needed on a fresh laptop.

Automated verification

115 tests pass on both machines (server Python 3.14, laptop Python 3.13). Coverage includes
card ingest/re-ingest, mocked gphoto2, offline/online catalogue sync, duplicate refusal,
concurrent uploads within/across libraries, delete/restore, metadata refresh, hashtags,
notes, albums/covers, library isolation, proxy paths and late-sidecar recovery.
Each newly found code defect was reproduced before its regression test passed.
Tests use temporary libraries and registries, separate from the real photo libraries.

Run:

    ./.venv/bin/pip install -r requirements-test.txt
    ./.venv/bin/python -m unittest discover -s tests -v

Live end-to-end verification using synthetic media

- The initial two-batch loopback-card test used: 8 JPEG entries
  (6 unique), 4 H.264/AAC videos and 4 Sony-style XML sidecars. Its manifest records
  all 16 source files. The card remains mounted read-only.
- Automatic discovery/dry run, offline ingest, subsequent authenticated HTTPS sync,
  simulated unplug/reconnect and repeated zero-new-item ingest pass.
- One video was deliberately ingested without its sidecar; the next run recovered the
  late XML and corrected its capture metadata, with no new original copied.
- All card originals and stored copies pass SHA-256 verification. Initial real-library
  integrity checks also passed: 101 iPhone-library originals and 15 laptop-camera originals.
- Playwright verified grid, viewer, keyboard navigation, tags and albums on both machines.
  Generated video playback works; HTTP byte-range requests return 206 on both machines.
- Playwright exercised note autosave, bulk tags, hashtag search, nested folder/album
  creation, album membership, confirmation dialogs and deletion. No JS/HTTP errors.
- Re-ingest respects a deleted hash. Restore retains notes, applied tags, album membership,
  explicit cover, metadata and original bytes. Catalogue fields agree afterward.
- A second loopback disk holds "Synthetic portable library". It was unmounted on the
  laptop, moved to the mini PC, edited/deleted through the server browser, then moved
  back. Recognition, notes, tags, album membership, trash and server edits all survived.
  Restoration on the laptop recovered the photo and its album membership.
- A photo deleted from one library can be ingested into another, with a warning, as
  specified by tasks2 D3. Restoring it can intentionally produce an overlap; sync warns.
  The two synthetic libraries retain one such deliberate overlap.
- A 300 MiB synthetic camera file was refused before copying into the 256 MiB library
  disk. No original or partial file was added; existing contents stayed unchanged.
- /wanted refuses synthetic originals already held by a synced laptop library.

Deployment and fixtures

Server services are running with the fixes. Laptop runtime: ~/photos_receiver.
The older ~/other_git/photos_receiver checkout is not the runtime tested here.
Changed laptop files were backed up under ~/photos_receiver-audit-backup-20261114.
Source is maintained on laptop-ingest; the laptop runtime checkout is reconciled to the
tested committed revision. Certificates and library data remain outside that checkout.
See tools/FAKE_CAMERA.md for fixture paths, remount commands and generator instructions.

Limits of this audit

No failures remain in the scenarios above. This is verified coverage, not a proof that
all possible bugs are absent. The fake card exercises mounted storage, not USB/PTP
transport; the user accepts the real Sony connection as working. Mocked gphoto2 tests
cover that client's contract. Real RAW decoding, FAT/exFAT destination behaviour and
actual iPhone share-sheet execution were not exercised in this audit. The iPhone
receiver API contract passes automated tests. README's documented feature limits,
including catalogue freshness, still apply. The QuickTime timezone gap was closed in the
conceptual follow-up below.

Conceptual parity follow-up

The browser now has Trash recovery/permanent deletion backed by the same service as the
CLI, and read-only searchable catalogues for remote/unplugged libraries. Cached offline
counts agree across browser, CLI and receiver API. Notes update their grid snippet on
save, including Note 2 fallback. UTC-only video capture timezone is retained in per-photo
metadata across machine timezone changes; legacy indexed/restored rows retain their
original capture offset when the existing date and UTC EXIF are available.

Live public HTTPS and LAN checks used the URLs in test_urls.txt. Authentication, library
pages, remote redirects, shortcut page, catalogue search/filtering, note autosave, Trash
restore, irreversible purge and retained deletion history all passed through Caddy.
The temporary audit login was removed; revocation was verified (401), and the owner-only
credential file was deleted. Existing logins and upload-token configuration were retained.

A third fake-camera batch was sent directly to a new server-only synthetic library.
Online laptop ingest refused its five originals; offline ingest copied them and later sync
warned about the overlap. Public browser recovery retained notes; purging one synthetic
server original did not cause re-upload. The generated card then gained a UTC-only MP4.
All 16 synthetic-library capture times stayed identical after a real exiftool refresh
under TZ=America/Los_Angeles. Mobile catalogue/Trash layouts were checked at 390px width.
The real 101-item iPhone library was read-only tested for infinite scroll and video filters.

Fixture state now: camera card has 19 media entries (16 unique) plus six XML sidecars;
Synthetic camera test on laptop has 16 originals; Synthetic portable library has one;
Synthetic server audit (df9d15e7) has four originals and one deliberately purged deletion
record. See CONCEPTUAL_PARITY.md for current links and remaining intentional boundaries.

Reliability follow-up (103 automated tests)

Catalogue sync now stages incoming manifests and rows until checksum/count validation is
complete, preserving the old rows, tombstones, name/count and last-success time across
interruption. Publication is serialized across workers. Old clients retain their request
sequence; new clients confirm the verified commit. Local snapshot reads and cache replacement
are transactional too. Tests cover incomplete same-count updates, deletions, stale sessions,
wrong rows, legacy request sequences and deletion-only transfers.

Background sync was explicitly configured on the laptop. Its user service is requested by
catalogue-affecting mutations and its enabled timer retries once per minute. Durable local
state retains pending changes, failure details and the last success. The browser exposes
those states and a nonblocking Sync now action; new mutations cannot be cleared by completion
of an older snapshot. No token is stored in the sync configuration or progress files.

Live tests: a deliberately incomplete HTTPS transfer retained the original 16 catalogue rows
and original registry report. The public catalogue displayed pending status over that old
verified snapshot. A forced loopback connection failure showed Failed while retaining the
last success; the installed timer recovered after the correct server URL was restored.
Browser deletion and restoration automatically changed the server's verified inventory from
16 to 15 and back, preserving the UTC clip's capture time.

Real filesystem tests used isolated registries and disposable loopback images. FAT32 and
exFAT each copied all 16 unique synthetic originals with matching hashes; read-only remounts
refused ingestion clearly. FAT32 rejected both dry-run and actual plans containing a 4 GiB
file before any original copied. A real unmount rejected the cached library object without
creating files under the mount point. An import killed with SIGKILL during a 2 GiB copy left
no incomplete database row. After aging its orphan past the documented cleanup threshold,
retry swept it and recovered exactly one byte-verified original. Temporary large source and
copy-test image files were removed. Read-only clean libraries can still be viewed.

Fault tests also cover cumulative PTP forecasts, stop-before-download when storage fills,
cleanup on failed stores, partial-import/resume reporting, and cache invalidation when a
same-size/mtime source changes between hashing and copying. Physical camera transport remains
accepted as requested; these tests use synthetic files and devices.

Command-line re-audit: see CLI_REAUDIT.md. Additional regressions were reproduced and
fixed for same-size/mtime source changes, malformed sidecar dates and missing originals
hidden behind database rows. Both commands now read source contents afresh per run;
server uploads carry an expected hash, and both storage paths preserve organisation when
recovering missing/wrong-sized originals. Policy/dry-run output was corrected too.
115 tests pass on both machines; fresh/repeat imports, duplicate refusal/override,
malformed metadata, byte equality and missing-original repair were live tested with the
fake mounted USB card. No statement of universal bug freedom is implied.
