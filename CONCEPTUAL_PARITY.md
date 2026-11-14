# Conceptual feature parity — 14 November 2026

The intended scope comes from README and tasks2.txt, with the user's confirmation that
physical Sony USB operation works. Camera testing uses the loopback card on the laptop.

The conceptual gaps addressed in this pass were recovery requiring a terminal, inaccessible
remote/offline inventories, inconsistent offline counts, stale grid note snippets and
capture dates changing when a library was re-indexed on a machine in another timezone.

| User workflow | Current behaviour |
| --- | --- |
| Camera → server | send_camera uploads wanted originals/sidecars over authenticated HTTPS. |
| Camera → laptop/USB library | ingest_camera uses the same storage/indexing code and works offline. |
| Repeat import | Existing/deleted hashes are respected; duplicate card entries copy once. |
| Find an original in a remote/unplugged library | **New:** View catalogue lists/searches cached names, dates, camera, lens and hashes, with media filters and freshness context. |
| Open a returned disk | Its live database wins; catalogue links redirect to the live grid, preserving filters. |
| Notes and organisation | Notes, applied tags, albums/folders and covers remain per library. **Fixed:** note previews update immediately after autosave. |
| Delete and recover in the browser | **New:** Trash restores selected originals with notes/tags/albums/covers. It shares the CLI recovery implementation. |
| Permanently remove trash | **New in browser:** explicit confirmation; original removed, hash retained; deleted history explains unavailable originals. |
| Remember offline libraries | Cached item counts agree in browser, CLI and receiver API; missing cache stays unknown. |
| Duplicate policy across machines | Online checks include local, unplugged and laptop catalogues; offline overlaps are allowed and warned about at next sync. |
| Move a library between machines | The marker/database/files/trash travel together; live round-trip with server edits passed. A remote sync is rejected if the server already has that library ID mounted; copies are not merged. |
| Capture dates across machines | **Fixed:** UTC-only clips retain their original timezone context; old indexed/restored rows can recover their original capture offset. Camera-local dates retain their wall time. |
| Browser login and upload authentication | Existing HTTPS/basic-auth browser routes and bearer-token receiver routes retained; public domain and LAN paths verified. |

## Useful live pages

Normal existing browser login applies to the server links.

- [All server libraries](https://photos.example.com:9443/photos/libraries)
- [Laptop synthetic catalogue on the server](https://photos.example.com:9443/photos/libraries/5e3807bb/catalogue)
- [Real laptop-camera catalogue on the server](https://photos.example.com:9443/photos/libraries/79f8c1a4/catalogue)
- [Synthetic server library](https://photos.example.com:9443/photos/lib/df9d15e7/)
- [Synthetic server Trash](https://photos.example.com:9443/photos/lib/df9d15e7/trash)
- [Deliberately purged synthetic item in history](https://photos.example.com:9443/photos/lib/df9d15e7/trash?show=all)
- [LAN catalogue](https://192.0.2.10:9443/photos/libraries/5e3807bb/catalogue)
- Laptop browser: http://127.0.0.1:8095/lib/5e3807bb/ and its `/trash` page, on the laptop itself.

## Verification and limits

115 automated tests pass on both machines. Public HTTPS Playwright checks covered catalogue
search/filtering, notes, deletion, restore and permanent deletion through the real Caddy
prefix/auth routes. Laptop recovery and mobile layouts passed. Real-library read checks
verified all 101 items via infinite scroll and the video filter. A real exiftool refresh
under a different TZ preserved all 16 synthetic capture times. See VALIDATION.md for more.

Writes were confined to synthetic libraries, fixture files, suite code and the temporary
HTTPS audit login. The login was removed and verified revoked; existing logins remain.
The tested changes are maintained on the laptop-ingest branch and deployed on the server
and ~/photos_receiver on the laptop. The laptop checkout is reconciled with that revision.

These remain deliberate boundaries, rather than promises made by the current suite:

- A remote catalogue is a last-reported inventory. It contains no originals/previews,
  notes, tags, albums or trash. Those are available on the machine holding the disk.
- Cross-machine catalogue changes become visible at the next ingest/manual/timer sync.
  The laptop timer is now installed explicitly on laptop-host, requests work on catalogue edits,
  and retries every minute. Other machines opt in using configure_laptop_sync.py.
- Network push of whole libraries, two-way merging and duplicate removal across offline
  machines remain outside tasks2's scope. Sync warns about overlaps and does not delete them.
- Physical USB/PTP transport is accepted or mocked for this audit. Actual iOS Shortcut
  execution and real ARW decoding were not newly exercised; their documented API/preview
  paths remain in place. No finite test suite proves that every possible bug is absent.

## Reliability follow-up

- Incoming catalogue batches stage separately. Interrupted transfers, stale sessions and
  incorrect rows leave the previous published snapshot and success time untouched. Complete
  manifests publish rows and tombstones atomically; the current client confirms publication.
- Laptop /libraries shows durable progress, failures and last success; browser Sync now is
  nonblocking. An edit during a transfer remains pending. A stopped worker is shown as
  awaiting retry rather than being left indefinitely "syncing".
- The laptop Git checkout was reconciled with the committed source without overwriting its
  working files or certificates. Subsequent deployment uses that branch as its source.
- Dry-run capacity forecasts, FAT32 limits, resumable partial imports, read-only storage and
  detached-library guards are covered by tests. Real FAT32/exFAT loopback copies, a real
  unmount and a SIGKILL during a 2 GiB copy all passed safe recovery/integrity checks.
- Live HTTPS tests observed an interrupted two-row update retaining its old published data;
  browser status reported a forced network failure and recovered on the installed timer.
  Browser deletion and restore automatically propagated verified catalogue snapshots.

Command-line re-audit: see CLI_REAUDIT.md. Additional regressions were reproduced and
fixed for same-size/mtime source changes, malformed sidecar dates and missing originals
hidden behind database rows. Both commands now read source contents afresh per run;
server uploads carry an expected hash, and both storage paths preserve organisation when
recovering missing/wrong-sized originals. Policy/dry-run output was corrected too.
115 tests pass on both machines; fresh/repeat imports, duplicate refusal/override,
malformed metadata, byte equality and missing-original repair were live tested with the
fake mounted USB card. No statement of universal bug freedom is implied.
