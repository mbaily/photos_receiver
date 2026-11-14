"""What libraries that are not plugged in contain: one row per photo (hash, names, size,
capture time, camera, lens) and the hashes deleted from them. No files, notes or albums.
Kept in one small database next to the registry, catalogue.db.
Incoming manifests and row batches are staged separately. The previous published snapshot
remains visible until a complete checksum verifies; rows and deletion hashes then swap in
one transaction. The registry's successful report time advances only after publication.

Two sources fill it:
- This machine's own libraries are recorded while they are plugged in (when found, hourly,
  and on every upload, delete and restore), so their contents are still known - for the
  duplicate check - once the disk is unplugged.
- A laptop that ingests with ingest_camera.py keeps its library on its own disk, which this
  machine never mounts, and sends the same rows over the network (receiver.py /remote/...).

It is a cache: delete it and it is rebuilt (own libraries within the hour while plugged in,
laptop libraries at their next sync). A library that is plugged in is always answered from
its own inbox.db; its catalogue rows are only used while it is away.
(The tables are called remote_* because laptop libraries were the first use.)
"""

import logging

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from sqlite_connection import ClosingConnection
from pathlib import Path

import photo_library

# Columns of the photos table that make up a catalogue row, besides sha256.
FIELDS = ("saved_filename", "original_filename", "size_bytes", "media_type",
          "taken_at", "camera", "lens", "uploaded_at")


def photo_db_private(path) -> None:
    import photo_db
    photo_db.private(path)


def db_file() -> Path:
    return photo_library.REGISTRY.parent / "catalogue.db"


def connect() -> sqlite3.Connection:
    path = db_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, factory=ClosingConnection)
    photo_db_private(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS remote_photos (
            library_id TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            saved_filename TEXT,
            original_filename TEXT,
            size_bytes INTEGER,
            media_type TEXT,
            taken_at TEXT,
            camera TEXT,
            lens TEXT,
            uploaded_at TEXT,
            stamp TEXT NOT NULL DEFAULT '',     -- '' until the row's details have arrived
            PRIMARY KEY (library_id, sha256)
        );
        CREATE INDEX IF NOT EXISTS remote_photos_sha ON remote_photos(sha256);
        CREATE TABLE IF NOT EXISTS remote_deleted (
            library_id TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            PRIMARY KEY (library_id, sha256)
        );
        CREATE INDEX IF NOT EXISTS remote_deleted_sha ON remote_deleted(sha256);
        CREATE TABLE IF NOT EXISTS sync_sessions (
            library_id TEXT PRIMARY KEY, sync_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            info TEXT NOT NULL, manifest TEXT, deleted TEXT, committed INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_rows (
            library_id TEXT NOT NULL, sha256 TEXT NOT NULL, stamp TEXT NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY (library_id, sha256)
        );
        CREATE TABLE IF NOT EXISTS library_state (
            library_id TEXT PRIMARY KEY, revision INTEGER, recorded_at TEXT NOT NULL
        );
        -- Originals a verify found missing or damaged and could not recover: recorded, but
        -- not held, so they must not count as "already in another library".
        CREATE TABLE IF NOT EXISTS library_absent (
            library_id TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY (library_id, sha256)
        );
        CREATE TABLE IF NOT EXISTS library_warnings (
            id INTEGER PRIMARY KEY, library_id TEXT NOT NULL, kind TEXT NOT NULL,
            detail TEXT NOT NULL, at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_moves (
            id TEXT PRIMARY KEY,
            source_lib_id TEXT NOT NULL,
            target_lib_id TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            original_filename TEXT,
            created_at TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS pending_moves_target ON pending_moves(target_lib_id);
        """
    )
    return conn


# ---- what was recorded before, against what a library holds now --------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def library_revision(conn: sqlite3.Connection):
    """The change counter in a library's own database (photo_db.revision), or None."""
    try:
        row = conn.execute("SELECT value FROM library_meta WHERE key = 'revision'").fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def warn(cat: sqlite3.Connection, library_id: str, kind: str, detail: str) -> str:
    if not cat.execute('SELECT 1 FROM library_warnings WHERE library_id=? AND kind=? AND detail=?',
                       (library_id, kind, detail)).fetchone():
        cat.execute('INSERT INTO library_warnings (library_id, kind, detail, at) VALUES (?,?,?,?)',
                    (library_id, kind, detail, _now()))
    return detail


def record_state(cat: sqlite3.Connection, library_id: str, revision, shas=None, deleted=(),
                 found: dict | None = None) -> list[str]:
    """Compare what is about to be recorded for a library with what was recorded before,
    then remember its change counter. Call inside the transaction that replaces the rows,
    before they are replaced. Returns the warnings raised (they are also stored).

    Two things cannot happen through the application and so mean the library is not the
    one seen last time - a copied folder, a restored backup or a damaged database:
    photos that vanished without being deleted, and a change counter that went backwards.
    """
    raised = []
    state = cat.execute('SELECT revision FROM library_state WHERE library_id=?', (library_id,)).fetchone()
    if shas is not None:
        gone = set(deleted)
        lost = [r for r in cat.execute(
            'SELECT sha256, original_filename FROM remote_photos WHERE library_id=? ORDER BY original_filename, sha256',
            (library_id,)) if r['sha256'] not in shas and r['sha256'] not in gone]
        if lost:
            names = ', '.join((r['original_filename'] or r['sha256'][:12]) for r in lost[:5])
            raised.append(warn(cat, library_id, 'missing',
                               f"{len(lost)} item{'s' if len(lost) != 1 else ''} recorded earlier "
                               f"{'are' if len(lost) != 1 else 'is'} no longer in this library and "
                               f"{'were' if len(lost) != 1 else 'was'} not deleted through the application "
                               f"({names}{', ...' if len(lost) > 5 else ''}). This may be an older copy of the library."))
    before = state['revision'] if state else None
    if found is not None and revision is not None and before is not None and revision < before:
        found['expected'] = before          # the counter the up-to-date copy has reached
    if revision is not None and before is not None and revision < before:
        raised.append(warn(cat, library_id, 'older-copy',
                           f"This copy's change counter ({revision}) is behind the one recorded earlier ({before}): "
                           "it is an older copy or a restored backup, and later changes are not in it."))
    cat.execute('INSERT INTO library_state (library_id, revision, recorded_at) VALUES (?,?,?) '
                'ON CONFLICT(library_id) DO UPDATE SET recorded_at=excluded.recorded_at, '
                'revision=COALESCE(excluded.revision, revision)', (library_id, revision, _now()))
    return raised


def set_absent(library_id: str, digests) -> None:
    """Replace the list of originals a library records but does not actually hold."""
    with connect() as cat:
        cat.execute('DELETE FROM library_absent WHERE library_id=?', (library_id,))
        cat.executemany('INSERT OR IGNORE INTO library_absent VALUES (?,?)', [(library_id, d) for d in digests])


def recorded_revision(library_id: str):
    if not db_file().is_file():
        return None
    with connect() as conn:
        row = conn.execute('SELECT revision FROM library_state WHERE library_id=?', (library_id,)).fetchone()
    return row['revision'] if row else None


def touch(library_id: str) -> None:
    """Note that a library exists and is known (an empty one has no rows to show it)."""
    try:
        with connect() as cat:
            cat.execute('INSERT OR IGNORE INTO library_state (library_id, revision, recorded_at) VALUES (?,NULL,?)',
                        (library_id, _now()))
    except Exception:
        logging.getLogger("receiver").exception("could not note library %s in the catalogue", library_id)


def warnings(library_id: str | None = None) -> list[dict]:
    if not db_file().is_file():
        return []
    with connect() as conn:
        rows = conn.execute('SELECT library_id, kind, detail, at FROM library_warnings '
                            + ('WHERE library_id=? ' if library_id else '') + 'ORDER BY id',
                            (library_id,) if library_id else ()).fetchall()
    return [dict(r) for r in rows]


def acknowledge(library_id: str, kind: str | None = None) -> int:
    if not db_file().is_file():
        return 0
    with connect() as conn:
        return conn.execute('DELETE FROM library_warnings WHERE library_id=?' + (' AND kind=?' if kind else ''),
                            (library_id, kind) if kind else (library_id,)).rowcount


def set_warning(library_id: str, kind: str, detail: str | None) -> None:
    """Replace a library's warning of one kind (None clears it)."""
    with connect() as conn:
        conn.execute('DELETE FROM library_warnings WHERE library_id=? AND kind=?', (library_id, kind))
        if detail:
            warn(conn, library_id, kind, detail)


# ---- computed identically on both machines ----------------------------------

def row_stamp(row) -> str:
    """Short checksum of a row's catalogue fields, so a changed row is sent again."""
    text = json.dumps([row[f] for f in FIELDS], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def fingerprint(stamps: dict[str, str], deleted) -> str:
    """One checksum for a whole library: equal on both sides means nothing to send."""
    h = hashlib.sha256()
    for sha in sorted(stamps):
        h.update(f"{sha}:{stamps[sha]}\n".encode())
    for sha in sorted(set(deleted)):
        h.update(f"D:{sha}\n".encode())
    return h.hexdigest()


def local_rows(conn: sqlite3.Connection) -> tuple[dict[str, dict], list[str]]:
    """({sha: catalogue row incl. stamp}, [deleted hashes]) of a library's own inbox.db."""
    if not conn.in_transaction:
        conn.execute('BEGIN')
    rows = {}
    for r in conn.execute(f"SELECT sha256, {', '.join(FIELDS)} FROM photos"):
        row = dict(r)
        row["stamp"] = row_stamp(row)
        rows[row["sha256"]] = row
    deleted = [r[0] for r in conn.execute("SELECT sha256 FROM deleted_photos")]
    return rows, deleted


# ---- the receiving side -----------------------------------------------------

def stored_fingerprint(library_id: str) -> str:
    if not db_file().is_file():
        return fingerprint({}, [])
    with connect() as conn:
        conn.execute('BEGIN')
        stamps = {r["sha256"]: r["stamp"] for r in conn.execute(
            "SELECT sha256, stamp FROM remote_photos WHERE library_id = ?", (library_id,))}
        deleted = [r[0] for r in conn.execute(
            "SELECT sha256 FROM remote_deleted WHERE library_id = ?", (library_id,))]
    return fingerprint(stamps, deleted)



# ---- this machine's own libraries -------------------------------------------

def snapshot(lib) -> None:
    """Record everything a plugged-in library holds. Never raises: the catalogue is a
    convenience and must not get in the way of finding libraries."""
    try:
        import photo_db
        token = photo_library.use(lib)
        try:
            with photo_db.get_db() as conn:
                rows, deleted = local_rows(conn)
                revision = library_revision(conn)
        finally:
            photo_library._current.reset(token)
        stamps = {sha: row["stamp"] for sha, row in rows.items()}
        if stored_fingerprint(lib.id) == fingerprint(stamps, deleted):
            found: dict = {}
            with connect() as cat:
                cat.execute('BEGIN IMMEDIATE')
                raised = record_state(cat, lib.id, revision, found=found)
            _hold_if_suspect(lib, raised, found)
            return
        found = {}
        with connect() as cat:
            cat.execute('BEGIN IMMEDIATE')
            raised = record_state(cat, lib.id, revision, set(rows), deleted, found)
            cat.execute('DELETE FROM remote_photos WHERE library_id=?', (lib.id,))
            cat.execute('DELETE FROM remote_deleted WHERE library_id=?', (lib.id,))
            columns = ('library_id', 'sha256', *FIELDS, 'stamp')
            cat.executemany(f"INSERT INTO remote_photos ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                            [(lib.id, sha, *(r[f] for f in FIELDS), r['stamp']) for sha, r in rows.items()])
            cat.executemany('INSERT INTO remote_deleted VALUES (?,?)', [(lib.id, sha) for sha in deleted])
        _hold_if_suspect(lib, raised, found)
    except Exception:
        logging.getLogger("receiver").exception("could not record library %s in the catalogue", lib.name)


def _hold_if_suspect(lib, raised: list[str], found: dict) -> None:
    """A library plugged in here that is not the one recorded last time is made read-only
    until that is acknowledged (photo_library.hold)."""
    if not raised:
        return
    for text in raised:
        logging.getLogger("receiver").warning("library '%s': %s", lib.name, text)
    reason = ("it is an older copy or a restored backup of the library." if 'expected' in found else
              "photos recorded earlier are no longer in it.")
    photo_library.set_hold(lib.id, reason, found.get('expected'))


def refresh(library_id: str, conn: sqlite3.Connection, digests: list[str]) -> None:
    """Bring the catalogue in line for these photos after a change in the library whose
    database `conn` is: stored, deleted or restored. Never raises."""
    try:
        with connect() as cat:
            for digest in dict.fromkeys(digests):
                row = conn.execute(f"SELECT sha256, {', '.join(FIELDS)} FROM photos WHERE sha256 = ?",
                                   (digest,)).fetchone()
                gone = conn.execute("SELECT 1 FROM deleted_photos WHERE sha256 = ?", (digest,)).fetchone()
                cat.execute("DELETE FROM remote_photos WHERE library_id = ? AND sha256 = ?", (library_id, digest))
                cat.execute("DELETE FROM remote_deleted WHERE library_id = ? AND sha256 = ?", (library_id, digest))
                # Stored again, repaired, or deleted: whichever, it is no longer "recorded but absent".
                cat.execute("DELETE FROM library_absent WHERE library_id = ? AND sha256 = ?", (library_id, digest))
                if row:
                    row = dict(row)
                    columns = ("library_id", "sha256", *FIELDS, "stamp")
                    cat.execute(f"INSERT INTO remote_photos ({', '.join(columns)}) "
                                f"VALUES ({', '.join('?' * len(columns))})",
                                (library_id, digest, *(row[f] for f in FIELDS), row_stamp(row)))
                elif gone:
                    cat.execute("INSERT INTO remote_deleted (library_id, sha256) VALUES (?, ?)", (library_id, digest))
            # Changes made through the application are never "an older copy".
            cat.execute('INSERT INTO library_state (library_id, revision, recorded_at) VALUES (?,?,?) '
                        'ON CONFLICT(library_id) DO UPDATE SET recorded_at=excluded.recorded_at, '
                        'revision=MAX(COALESCE(revision, 0), COALESCE(excluded.revision, 0))',
                        (library_id, library_revision(conn), _now()))
        import photo_sync
        photo_sync.changed(library_id)
    except Exception:
        logging.getLogger("receiver").exception("could not update the catalogue")


def forget(library_id: str) -> None:
    if not db_file().is_file():
        return
    with connect() as conn:
        conn.execute("DELETE FROM remote_photos WHERE library_id = ?", (library_id,))
        conn.execute("DELETE FROM remote_deleted WHERE library_id = ?", (library_id,))
        conn.execute('DELETE FROM sync_sessions WHERE library_id=?', (library_id,))
        conn.execute('DELETE FROM sync_rows WHERE library_id=?', (library_id,))
        conn.execute('DELETE FROM library_state WHERE library_id=?', (library_id,))
        conn.execute('DELETE FROM library_warnings WHERE library_id=?', (library_id,))
        conn.execute('DELETE FROM library_absent WHERE library_id=?', (library_id,))


def _lookup(table: str, hashes: list[str], skip_ids: set[str]) -> dict[str, str]:
    """{hash: library id} from the catalogue, ignoring the given libraries."""
    found: dict[str, str] = {}
    if not hashes or not db_file().is_file():
        return found
    with connect() as conn:
        conn.execute('BEGIN')
        for i in range(0, len(hashes), 500):
            chunk = hashes[i:i + 500]
            absent = ("" if table != "remote_photos" else
                      " AND NOT EXISTS (SELECT 1 FROM library_absent a WHERE a.library_id = t.library_id "
                      "AND a.sha256 = t.sha256)")
            for r in conn.execute(
                    f"SELECT sha256, library_id FROM {table} t WHERE sha256 IN ({','.join('?' * len(chunk))}){absent}",
                    chunk):
                if r["library_id"] not in skip_ids:
                    found.setdefault(r["sha256"], r["library_id"])
    return found


def holders(hashes: list[str], skip_ids: set[str]) -> dict[str, str]:
    """{hash: library id} for photos a library that is not plugged in is known to hold."""
    return _lookup("remote_photos", hashes, skip_ids)


def deleted_in(hashes: list[str], skip_ids: set[str]) -> dict[str, str]:
    return _lookup("remote_deleted", hashes, skip_ids)


def item_counts() -> dict[str, int]:
    if not db_file().is_file():
        return {}
    with connect() as conn:
        # A library recorded as empty counts 0; one never recorded is absent.
        counts = {r[0]: 0 for r in conn.execute('SELECT library_id FROM library_state')}
        counts.update({r[0]: r[1] for r in conn.execute(
            'SELECT library_id, COUNT(*) FROM remote_photos GROUP BY library_id')})
        return counts


def inventory(library_id: str, q: str = '', media: str = '', limit: int = 50, offset: int = 0):
    """Search cached descriptive fields. Notes, tags and files are never in this cache."""
    if not db_file().is_file():
        return [], 0
    from photo_db import _like
    fields = ('original_filename', 'saved_filename', 'camera', 'lens', 'taken_at', 'sha256')
    where, params = ['library_id = ?'], [library_id]
    for term in q.split():
        where.append('(' + ' OR '.join(f"{f} LIKE ? ESCAPE '\\'" for f in fields) + ')')
        params.extend([_like(term)] * len(fields))
    if media in ('image', 'raw', 'video'):
        where.append('media_type = ?')
        params.append(media)
    clause = ' WHERE ' + ' AND '.join(where)
    with connect() as conn:
        conn.execute('BEGIN')
        count = conn.execute('SELECT COUNT(*) FROM remote_photos' + clause, params).fetchone()[0]
        rows = conn.execute('SELECT * FROM remote_photos' + clause +
                            ' ORDER BY COALESCE(taken_at, uploaded_at) DESC, sha256 LIMIT ? OFFSET ?',
                            [*params, limit, max(offset, 0)]).fetchall()
    return rows, count


STALLED_AFTER = 900       # seconds without completion before a transfer is shown as stalled


class SyncConflict(ValueError):
    """A transfer was superseded, incomplete or inconsistent with its checksum."""


def begin_sync(library_id, info, expected):
    sync_id = uuid.uuid4().hex
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        stamps = {r['sha256']: r['stamp'] for r in conn.execute(
            'SELECT sha256,stamp FROM remote_photos WHERE library_id=?', (library_id,))}
        deleted = [r[0] for r in conn.execute('SELECT sha256 FROM remote_deleted WHERE library_id=?', (library_id,))]
        in_sync = fingerprint(stamps, deleted) == expected
        if in_sync:
            if len(stamps) != info['remote']['items'] or len(deleted) != info['remote']['deleted']:
                raise SyncConflict('Catalogue counts disagree with its checksum.')
            info['remote']['synced_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            info['warnings'] = record_state(conn, library_id, info['remote'].get('revision'))
        conn.execute('DELETE FROM sync_rows WHERE library_id=?', (library_id,))
        conn.execute('INSERT OR REPLACE INTO sync_sessions VALUES (?,?,?,?,NULL,NULL,?,?)',
                     (library_id, sync_id, expected, json.dumps(info), int(in_sync),
                      datetime.now(timezone.utc).isoformat(timespec='seconds')))
    return sync_id, info if in_sync else None


def _session(conn, library_id, sync_id=None):
    row = conn.execute('SELECT * FROM sync_sessions WHERE library_id=?', (library_id,)).fetchone()
    if row is None or (sync_id is not None and row['sync_id'] != sync_id):
        raise SyncConflict('Sync session was superseded or expired; retry the sync.')
    return row


def pending_syncs():
    if not db_file().is_file():
        return {}
    with connect() as conn:
        rows = conn.execute('SELECT library_id,started_at FROM sync_sessions WHERE committed=0').fetchall()
    out = {}
    for r in rows:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(r['started_at'])).total_seconds()
        except (TypeError, ValueError):
            age = STALLED_AFTER
        # A transfer the other machine abandoned is not "in progress" for ever.
        out[r['library_id']] = {'state': 'stalled' if age >= STALLED_AFTER else 'syncing',
                                'started_at': r['started_at']}
    return out


def _publish_if_complete(conn, library_id):
    session = _session(conn, library_id)
    if session['committed']:
        return json.loads(session['info'])
    if session['manifest'] is None:
        return None
    stamps = {r['sha256']: r['stamp'] for r in conn.execute(
        'SELECT sha256,stamp FROM sync_rows WHERE library_id=?', (library_id,))}
    deleted = json.loads(session['deleted'])
    if fingerprint(stamps, deleted) != session['fingerprint']:
        return None
    info = json.loads(session['info'])
    info['remote']['synced_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    # Publication of rows, tombstones and the completed-session record is one transaction.
    info['warnings'] = record_state(conn, library_id, info['remote'].get('revision'), set(stamps), deleted)
    conn.execute('DELETE FROM remote_photos WHERE library_id=?', (library_id,))
    conn.execute('DELETE FROM remote_deleted WHERE library_id=?', (library_id,))
    columns = ('library_id', 'sha256', *FIELDS, 'stamp')
    rows = [json.loads(r[0]) for r in conn.execute('SELECT data FROM sync_rows WHERE library_id=?', (library_id,))]
    conn.executemany(f"INSERT INTO remote_photos ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                     [(library_id, r['sha256'], *(r.get(f) for f in FIELDS), row_stamp(r)) for r in rows])
    conn.executemany('INSERT INTO remote_deleted VALUES (?,?)', [(library_id, sha) for sha in deleted])
    conn.execute('UPDATE sync_sessions SET committed=1,info=? WHERE library_id=?', (json.dumps(info), library_id))
    conn.execute('DELETE FROM sync_rows WHERE library_id=?', (library_id,))
    return info


def stage_hashes(library_id, stamps, deleted, sync_id=None):
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        session = _session(conn, library_id, sync_id)
        if fingerprint(stamps, deleted) != session['fingerprint']:
            raise SyncConflict('Manifest does not match the advertised checksum.')
        info = json.loads(session['info'])
        if len(stamps) != info['remote']['items'] or len(set(deleted)) != info['remote']['deleted']:
            raise SyncConflict('Manifest counts do not match the advertised counts.')
        if session['committed']:
            return [], [], info
        have = {r['sha256']: dict(r) for r in conn.execute(
            'SELECT * FROM remote_photos WHERE library_id=?', (library_id,))}
        conn.execute('DELETE FROM sync_rows WHERE library_id=?', (library_id,))
        conn.executemany('INSERT INTO sync_rows VALUES (?,?,?,?)',
                         [(library_id, sha, stamp, json.dumps(have[sha])) for sha, stamp in stamps.items()
                          if sha in have and have[sha]['stamp'] == stamp])
        conn.execute('UPDATE sync_sessions SET manifest=?,deleted=? WHERE library_id=?',
                     (json.dumps(stamps), json.dumps(sorted(set(deleted))), library_id))
        need = [sha for sha, stamp in stamps.items() if sha not in have or have[sha]['stamp'] != stamp]
        new = [sha for sha in need if sha not in have]
        published = _publish_if_complete(conn, library_id)
    return need, new, published


def stage_rows(library_id, rows, sync_id=None):
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        session = _session(conn, library_id, sync_id)
        if session['manifest'] is None:
            raise SyncConflict('Send the manifest before rows.')
        expected = json.loads(session['manifest'])
        for row in rows:
            if expected.get(row['sha256']) != row_stamp(row):
                raise SyncConflict('A row does not match the manifest; retry with a fresh snapshot.')
        if session['committed']:
            return json.loads(session['info'])
        conn.executemany('INSERT OR REPLACE INTO sync_rows VALUES (?,?,?,?)',
                         [(library_id, r['sha256'], row_stamp(r), json.dumps(r)) for r in rows])
        published = _publish_if_complete(conn, library_id)
    return published


def confirm_sync(library_id, sync_id, expected):
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        session = _session(conn, library_id, sync_id)
        if expected != session['fingerprint']:
            raise SyncConflict('Wrong sync checksum.')
        published = _publish_if_complete(conn, library_id)
        if published is None:
            raise SyncConflict('Sync is incomplete; previous catalogue is still published.')
    return published


def queue_moves(source_lib_id: str, target_lib_id: str, items: list[tuple[str, str]]) -> int:
    """Queue `(sha256, original_filename)` pairs to be pulled by a remote machine."""
    now = _now()
    queued = 0
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for sha, filename in items:
            exists = conn.execute(
                'SELECT 1 FROM pending_moves WHERE source_lib_id=? AND target_lib_id=? AND sha256=?',
                (source_lib_id, target_lib_id, sha),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                'INSERT INTO pending_moves (id, source_lib_id, target_lib_id, sha256, original_filename, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (uuid.uuid4().hex, source_lib_id, target_lib_id, sha, filename, now),
            )
            queued += 1
    return queued


def pending_moves(target_lib_id: str | None = None, source_lib_id: str | None = None) -> list[dict]:
    if not db_file().is_file():
        return []
    where, params = [], []
    if target_lib_id:
        where.append('target_lib_id = ?')
        params.append(target_lib_id)
    if source_lib_id:
        where.append('source_lib_id = ?')
        params.append(source_lib_id)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    with connect() as conn:
        rows = conn.execute('SELECT * FROM pending_moves' + clause + ' ORDER BY created_at, id', params).fetchall()
    return [dict(r) for r in rows]


def get_move(move_id: str) -> dict | None:
    if not db_file().is_file():
        return None
    with connect() as conn:
        row = conn.execute('SELECT * FROM pending_moves WHERE id = ?', (move_id,)).fetchone()
    return dict(row) if row else None


def complete_move(move_id: str) -> None:
    if not db_file().is_file():
        return
    with connect() as conn:
        conn.execute('DELETE FROM pending_moves WHERE id = ?', (move_id,))


def fail_move(move_id: str, error: str) -> None:
    if not db_file().is_file():
        return
    with connect() as conn:
        conn.execute('UPDATE pending_moves SET error = ? WHERE id = ?', (error[:500], move_id))
