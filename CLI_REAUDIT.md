# Command-line ingestion re-audit — 14 November 2026

The stronger claim "bug-free" is not supported by a finite test run. This focused pass
reproduced additional cases missed by the earlier audit, fixed them and re-tested both
commands against the read-only loopback USB card on the laptop and the real HTTPS receiver.

## Fixes

- Persisted path/size/mtime hashes could silently hide a changed source, especially if
  the previous hash was already held. Both CLI paths now hash original bytes afresh every
  run. Repeat scans require card reads; they no longer assume file attributes prove content.
- An invalid Sony XML date could abort a valid video's import. Date parsing now falls back
  to other metadata/file time while retaining the sidecar for inspection.
- A database row alone could hide a missing/wrong-sized original. Wanted/classification and
  storage now recover these originals while retaining the photo's notes, tags, albums,
  covers, arrival time and existing source metadata. Other libraries holding the same hash
  do not block repair of an already-recorded photo. Deliberate deletion hashes still block
  re-import. Wrong-sized existing files are preserved; recovery uses a non-colliding name.
- Standalone uploads include an expected SHA-256. The receiver checks it against streamed
  bytes before storage, rejecting a changed source with HTTP 409 and cleaning its temporary
  file. The header is optional for older clients/iPhone shortcut compatibility.
- Duplicate messages follow the receiver's actual decision, including server-wide allow
  policy. Local override and deletion-record messages describe policy without falsely
  claiming that dry-run or failed files were ingested. Dry runs show missing-sidecar and
  missing-original repairs, and summaries separate repaired originals from new logical items.

## Live checks

The fake card now contains four generated batches plus the UTC-only clip: 25 media entries,
21 unique originals, eight sidecars and a manifest covering all 33 source files. One new
sidecar has a deliberately invalid date. Earlier source files were unchanged.

Server path, run on the laptop:

    ./send_camera.sh --library "Synthetic server audit" --dry-run
    ./send_camera.sh --library "Synthetic server audit"
    ./send_camera.sh --library "Synthetic server audit"

The dry run predicted five new originals and one repair. Actual import sent all six with
zero failures; repeat sent zero. Recovery of a safely quarantined server original preserved
its notes/source metadata/album references. A one-file alias reading from the same mounted
card verified default duplicate refusal, --allow-duplicates upload and repeat-zero behaviour.
The synthetic server library ends with 10 exact originals and its previous purged hash.
An HTTPS request with an intentionally incorrect expected hash was rejected before storage.

Laptop-library path:

    ./ingest_camera.sh --library "Synthetic camera test" --dry-run
    ./ingest_camera.sh --library "Synthetic camera test"
    ./ingest_camera.sh --library "Synthetic camera test" --allow-duplicates
    ./ingest_camera.sh --library "Synthetic camera test"

The dry run predicted one repair and refused the five originals already on the server.
Normal ingest repaired the safely quarantined laptop original while preserving notes,
applied tags, album membership and explicit cover. The override imported five new originals;
repeat imported zero. The laptop synthetic library ends with 21 exact originals.

All source/target byte hashes and SQLite quick checks passed. For all five new shared
originals, both destinations agree on hash, original name, size, media type, capture time,
camera/lens, dimensions, duration and sidecar XML. The invalid-date video imported successfully
to both destinations with the same fallback capture time. Live changes were limited to
synthetic data; quarantined originals remain available outside their libraries as backups.

## Automated coverage and boundaries

115 tests pass on both machines, including the new same-size/mtime, malformed-date,
repair/preservation, receiver-policy, dry-run and expected-hash regressions. Earlier live
FAT32/exFAT, read-only, unmount, interrupted-copy/SIGKILL, atomic sync and background retry
checks remain recorded in VALIDATION.md.

This is verified coverage of the tested workflows, not proof of all possible inputs and
failures. Actual USB transport is accepted as confirmed by the user; the live source here
is a loopback card. The generated media exercise JPEG and H.264/AAC MP4 with Sony-style XML;
real ARW decoding and physical PTP transport were not newly exercised. Duplicate presence
checks detect missing files and size mismatches; ingestion is not a full background hash
scrub of all previously stored originals. Originals remain immutable within normal suite
operations. Storage damage outside those operations needs a separate integrity audit.
