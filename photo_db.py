"""Shared SQLite access for the photo library (receiver, browser, metadata refresh)."""

import json
import os
import re
import sqlite3
import stat
from sqlite_connection import ClosingConnection
from datetime import datetime, timezone
from pathlib import Path

import photo_library

_initialised: set[str] = set()   # database files whose schema has been checked in this process

# Columns added after the original iPhone-only schema; created on startup if missing.
EXTRA_COLUMNS = {
    "media_type": "TEXT",              # image | raw | video
    "taken_at": "TEXT",                # local capture time, 'YYYY-MM-DD HH:MM:SS'
    "camera": "TEXT",
    "lens": "TEXT",
    "width": "INTEGER",
    "height": "INTEGER",
    "duration_s": "REAL",
    "exif": "TEXT",                    # JSON: every tag the extractor found (+ SonyXML:* sidecar)
    "extractor": "TEXT",               # 'exiftool' or 'basic' (Pillow fallback)
    "sidecar_xml": "TEXT",             # raw Sony C0001M01.XML for videos
    "note1": "TEXT NOT NULL DEFAULT ''",
    "note2": "TEXT NOT NULL DEFAULT ''",
    "notes_updated_at": "TEXT",
    "hashtags": "TEXT NOT NULL DEFAULT ''",  # manually applied tags, separate from notes
}

# When a photo was taken, for ordering and date search. taken_at is 'YYYY-MM-DD HH:MM:SS';
# a photo without one falls back to when it arrived, put into the same text form (it is
# stored as an ISO time with a 'T' and an offset, which would otherwise sort out of place).
# Deterministic on purpose, so the photos_when index can serve it. {p} is a table prefix.
WHEN_SQL = "COALESCE({p}taken_at, substr(replace({p}uploaded_at, 'T', ' '), 1, 19))"
WHEN = WHEN_SQL.format(p="")

RAW_EXTS = {".arw"}
VIDEO_EXTS = {".mp4", ".mov", ".mts"}
HASHTAG_RE = re.compile(r"(?<![\w#&/])#(\w[\w-]*)")


def private(path) -> None:
    """Databases hold notes and file names: readable by their owner only, like the photos.
    (SQLite gives the -wal/-shm companions the same permissions as the database.)"""
    try:
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass


def get_db() -> sqlite3.Connection:
    """Connection to the current library's database (see photo_library.current)."""
    lib = photo_library.current()
    photo_library.require_attached(lib)
    path = lib.db_path
    if not os.access(lib.path, os.W_OK):
        if not path.is_file():
            raise photo_library.LibraryError('The library is read-only and has no readable database.')
        wal = path.with_name(path.name + '-wal')
        # A clean, unmounted checkpoint has no WAL data. Immutable mode then permits
        # viewing a read-only disk without trying to create SQLite's -shm file.
        uri = path.resolve().as_uri() + '?mode=ro'
        if not wal.exists() or wal.stat().st_size == 0:
            uri += '&immutable=1'
        conn = sqlite3.connect(uri, uri=True, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        return conn
    if path.is_file() and photo_library.hold(lib.id):
        # A suspected older copy: readable, never written (photo_library.require_writable
        # gives the reason at every entry point; this makes sure nothing slips past them).
        conn = sqlite3.connect(path, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA query_only=ON;")
        return conn
    if str(path) not in _initialised:
        _initialised.add(str(path))
        try:
            init_db()
        except Exception:
            _initialised.discard(str(path))
            raise
    conn = sqlite3.connect(path, factory=ClosingConnection)
    private(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photos (
                sha256 TEXT PRIMARY KEY,
                saved_filename TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                uploaded_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        have = {r["name"] for r in conn.execute("PRAGMA table_info(photos)")}
        for col, decl in EXTRA_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE photos ADD COLUMN {col} {decl}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_tags (
                sha256 TEXT NOT NULL REFERENCES photos(sha256) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                PRIMARY KEY (sha256, tag)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS photo_tags_tag ON photo_tags(tag)")
        # Photos deleted in the browser. Remembering the hash stops the phone/camera sync
        # from uploading them again; the file itself sits in the library's .trash folder.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deleted_photos (
                sha256 TEXT PRIMARY KEY,
                saved_filename TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                deleted_at TEXT NOT NULL
            )
            """
        )
        conn.execute("DROP INDEX IF EXISTS photos_taken")     # ordered by the two mixed formats
        conn.execute(f"CREATE INDEX IF NOT EXISTS photos_when ON photos({WHEN})")
        # Everything needed to put a deleted photo back as it was (restore_deleted.py).
        if "details" not in {r["name"] for r in conn.execute("PRAGMA table_info(deleted_photos)")}:
            conn.execute("ALTER TABLE deleted_photos ADD COLUMN details TEXT")
        # iCloud-style organisation: folders hold folders/albums; albums reference photos.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS collections (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('folder', 'album')),
                name TEXT NOT NULL,
                parent_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
                note TEXT NOT NULL DEFAULT '',
                cover_sha TEXT REFERENCES photos(sha256) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS collections_parent ON collections(parent_id);
            CREATE TABLE IF NOT EXISTS album_photos (
                album_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                sha256 TEXT NOT NULL REFERENCES photos(sha256) ON DELETE CASCADE,
                added_at TEXT NOT NULL,
                PRIMARY KEY (album_id, sha256)
            );
            CREATE INDEX IF NOT EXISTS album_photos_sha ON album_photos(sha256);
            CREATE TABLE IF NOT EXISTS collection_tags (
                collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                PRIMARY KEY (collection_id, tag)
            );
            CREATE INDEX IF NOT EXISTS collection_tags_tag ON collection_tags(tag);
            """
        )
        # Manual ordering within a parent (drag & drop in the albums tree).
        if "position" not in {r["name"] for r in conn.execute("PRAGMA table_info(collections)")}:
            conn.execute("ALTER TABLE collections ADD COLUMN position INTEGER")
        unordered = conn.execute(
            "SELECT id, parent_id FROM collections WHERE position IS NULL "
            "ORDER BY parent_id, kind = 'album', name COLLATE NOCASE"
        ).fetchall()
        for r in unordered:
            conn.execute(
                "UPDATE collections SET position = (SELECT COALESCE(MAX(position) + 1, 0) FROM collections "
                "WHERE parent_id IS ?) WHERE id = ?",
                (r["parent_id"], r["id"]),
            )
        # A change counter that travels with the library. Every change to its contents or
        # organisation raises it, so a machine that meets an older copy of the library (a
        # copied folder, a restored backup) can tell: see photo_catalogue.record_state.
        conn.execute("CREATE TABLE IF NOT EXISTS library_meta (key TEXT PRIMARY KEY, value)")
        conn.execute("INSERT OR IGNORE INTO library_meta (key, value) VALUES ('revision', 0)")
        for table in REVISION_TABLES:
            for event in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(
                    f"CREATE TRIGGER IF NOT EXISTS revision_{table}_{event.lower()} AFTER {event} ON {table} "
                    "BEGIN UPDATE library_meta SET value = value + 1 WHERE key = 'revision'; END")


REVISION_TABLES = ("photos", "deleted_photos", "photo_tags", "collections", "album_photos", "collection_tags")


def revision(conn: sqlite3.Connection) -> int | None:
    """The library's change counter; None for a database that has none yet (a read-only
    disk last written by an older version)."""
    try:
        row = conn.execute("SELECT value FROM library_meta WHERE key = 'revision'").fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] is not None else None


def get_meta(conn: sqlite3.Connection, key: str):
    try:
        row = conn.execute("SELECT value FROM library_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT INTO library_meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def media_type_for(filename: str) -> str:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in RAW_EXTS:
        return "raw"
    if ext in VIDEO_EXTS:
        return "video"
    return "image"


def original_present(row, folder=None) -> bool:
    """A row alone is not evidence that the original still exists on disk."""
    folder = folder if folder is not None else photo_library.current().path
    path = folder / Path(row['saved_filename']).name
    try:
        info = path.stat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size == row['size_bytes']


def existing_file(conn: sqlite3.Connection, digest: str) -> str | None:
    row = conn.execute(
        "SELECT saved_filename,size_bytes FROM photos WHERE sha256 = ?", (digest,)
    ).fetchone()
    return row['saved_filename'] if row and original_present(row) else None


def insert_photo(
    conn: sqlite3.Connection,
    digest: str,
    saved_filename: str,
    original_filename: str,
    size_bytes: int,
    meta: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO photos (
            sha256, saved_filename, original_filename, size_bytes, uploaded_at, metadata, media_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            digest,
            saved_filename,
            original_filename,
            size_bytes,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(meta),
            media_type_for(saved_filename),
        ),
    )


def delete_photos(conn: sqlite3.Connection, digests: list[str]) -> list[str]:
    """Forget photos (notes, tags and album membership go too) but remember their hashes.

    Returns the stored filenames; the caller moves those files to the library's .trash folder.
    """
    filenames = []
    for digest in digests:
        row = conn.execute("SELECT * FROM photos WHERE sha256 = ?", (digest,)).fetchone()
        if not row:
            continue
        details = {
            # exif is left out: it is read from the file again on restore.
            "row": {k: row[k] for k in row.keys() if k != "exif"},
            "albums": [r[0] for r in conn.execute("SELECT album_id FROM album_photos WHERE sha256 = ?", (digest,))],
            "covers": [r[0] for r in conn.execute("SELECT id FROM collections WHERE cover_sha = ?", (digest,))],
        }
        # An album's number can be given to a new album after the old one is deleted. Its
        # creation time tells them apart, so a restore never lands in an unrelated album.
        wanted = sorted(set(details["albums"]) | set(details["covers"]))
        details["album_created"] = {
            str(r["id"]): r["created_at"] for r in conn.execute(
                f"SELECT id, created_at FROM collections WHERE id IN ({','.join('?' * len(wanted))})", wanted)}
        conn.execute(
            "INSERT OR REPLACE INTO deleted_photos (sha256, saved_filename, original_filename, deleted_at, details) "
            "VALUES (?, ?, ?, ?, ?)",
            (digest, row["saved_filename"], row["original_filename"], datetime.now(timezone.utc).isoformat(),
             json.dumps(details)),
        )
        conn.execute("DELETE FROM photos WHERE sha256 = ?", (digest,))
        filenames.append(row["saved_filename"])
    return filenames


RESTORED_COLUMNS = ("uploaded_at", "metadata", "sidecar_xml", "note1", "note2", "notes_updated_at", "hashtags",
                    "taken_at", "camera", "lens", "width", "height", "duration_s", "extractor")


def restore_photo(conn: sqlite3.Connection, digest: str, size_bytes: int) -> dict:
    """Put a deleted photo's row back (its file must already be back in the library).

    Notes, hashtags, the sidecar and album places return when the photo was deleted by a
    version that recorded them; albums that were deleted meanwhile are skipped.
    Returns {"full": bool, "albums": n}.
    """
    gone = conn.execute("SELECT * FROM deleted_photos WHERE sha256 = ?", (digest,)).fetchone()
    details = json.loads(gone["details"]) if gone["details"] else {}
    old = details.get("row") or {}
    insert_photo(conn, digest, Path(gone["saved_filename"]).name, gone["original_filename"], size_bytes,
                 {"restored": True})
    keep = {k: old[k] for k in RESTORED_COLUMNS if old.get(k) is not None}
    if keep:
        conn.execute(f"UPDATE photos SET {', '.join(f'{k} = ?' for k in keep)} WHERE sha256 = ?",
                     [*keep.values(), digest])
        tags = extract_hashtags(old.get("note1") or "", old.get("note2") or "", old.get("hashtags") or "")
        conn.executemany("INSERT OR IGNORE INTO photo_tags (sha256, tag) VALUES (?, ?)", [(digest, t) for t in tags])
    albums = 0
    created = details.get("album_created")      # absent in records made by older versions

    def same_album(album_id) -> bool:
        if created is None:
            return True
        row = conn.execute("SELECT created_at FROM collections WHERE id = ?", (album_id,)).fetchone()
        return bool(row) and row["created_at"] == created.get(str(album_id))

    for album_id in details.get("albums", []):
        if not same_album(album_id):
            continue
        albums += conn.execute(
            "INSERT OR IGNORE INTO album_photos (album_id, sha256, added_at) "
            "SELECT id, ?, ? FROM collections WHERE id = ? AND kind = 'album'",
            (digest, datetime.now(timezone.utc).isoformat(), album_id)).rowcount
    # Restore chosen covers only if nobody chose a different cover while it was away.
    for album_id in details.get("covers", []):
        if not same_album(album_id):
            continue
        conn.execute(
            "UPDATE collections SET cover_sha = ? WHERE id = ? AND cover_sha IS NULL "
            "AND EXISTS (SELECT 1 FROM album_photos WHERE album_id = ? AND sha256 = ?)",
            (digest, album_id, album_id, digest))
    conn.execute("DELETE FROM deleted_photos WHERE sha256 = ?", (digest,))
    return {"full": bool(old), "albums": albums}


def deleted_hashes(conn: sqlite3.Connection, digests: list[str]) -> set[str]:
    out: set[str] = set()
    for i in range(0, len(digests), 500):
        chunk = digests[i:i + 500]
        out |= {r[0] for r in conn.execute(
            f"SELECT sha256 FROM deleted_photos WHERE sha256 IN ({','.join('?' * len(chunk))})", chunk)}
    return out


def update_columns(conn: sqlite3.Connection, digest: str, cols: dict) -> None:
    cols = {k: v for k, v in cols.items() if k in EXTRA_COLUMNS}
    if cols:
        assignments = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE photos SET {assignments} WHERE sha256 = ?", [*cols.values(), digest])


# ---- notes & hashtags -------------------------------------------------------

def extract_hashtags(*texts: str) -> list[str]:
    tags = set()
    for text in texts:
        for m in HASHTAG_RE.finditer(text or ""):
            tag = m.group(1).rstrip("-_").lower()
            if tag:
                tags.add(tag)
    return sorted(tags)


def set_notes(conn: sqlite3.Connection, digest: str, note1: str, note2: str) -> list[str]:
    conn.execute(
        "UPDATE photos SET note1 = ?, note2 = ?, notes_updated_at = ? WHERE sha256 = ?",
        (note1, note2, datetime.now(timezone.utc).isoformat(), digest),
    )
    row = conn.execute("SELECT hashtags FROM photos WHERE sha256 = ?", (digest,)).fetchone()
    tags = extract_hashtags(note1, note2, row['hashtags'] if row else '')
    conn.execute("DELETE FROM photo_tags WHERE sha256 = ?", (digest,))
    conn.executemany("INSERT INTO photo_tags (sha256, tag) VALUES (?, ?)", [(digest, t) for t in tags])
    return tags


def edit_hashtags(conn: sqlite3.Connection, digests: list[str], tags: list[str], mode: str) -> int:
    """Apply tags separately from notes; remove matching hashtag tokens from notes too."""
    if mode not in ('add', 'remove'):
        raise ValueError('Choose add or remove')
    targets = set(tags)
    changed = 0
    for digest in dict.fromkeys(digests):
        row = conn.execute('SELECT hashtags, note1, note2 FROM photos WHERE sha256 = ?', (digest,)).fetchone()
        if row is None:
            continue
        manual = set(extract_hashtags(row['hashtags']))
        note1, note2 = row['note1'], row['note2']
        if mode == 'add':
            manual |= targets
        else:
            manual -= targets
            def strip_tag(match):
                return '' if match[1].rstrip('-_').lower() in targets else match[0]
            note1, note2 = (HASHTAG_RE.sub(strip_tag, note or '') for note in (note1, note2))
        hashtags = ' '.join('#' + tag for tag in sorted(manual))
        if (hashtags, note1, note2) == (row['hashtags'], row['note1'], row['note2']):
            continue
        conn.execute('UPDATE photos SET hashtags = ? WHERE sha256 = ?', (hashtags, digest))
        # Rebuild the shared index so grids, search, tag counts and later note saves agree.
        if (note1, note2) != (row['note1'], row['note2']):
            set_notes(conn, digest, note1, note2)
        else:
            indexed = extract_hashtags(hashtags, note1, note2)
            conn.execute('DELETE FROM photo_tags WHERE sha256 = ?', (digest,))
            conn.executemany('INSERT INTO photo_tags (sha256, tag) VALUES (?, ?)',
                             [(digest, tag) for tag in indexed])
        changed += 1
    return changed


def tags_for(conn: sqlite3.Connection, digests: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {d: [] for d in digests}
    for i in range(0, len(digests), 500):
        chunk = digests[i:i + 500]
        rows = conn.execute(
            f"SELECT sha256, tag FROM photo_tags WHERE sha256 IN ({','.join('?' * len(chunk))}) ORDER BY tag",
            chunk,
        )
        for r in rows:
            out[r["sha256"]].append(r["tag"])
    return out


def all_tags(conn: sqlite3.Connection, sort: str = "count") -> list[sqlite3.Row]:
    """Hashtags used on photos and on albums/folders, with counts and a cover photo."""
    order = "tag" if sort == "name" else "n DESC, tag"
    WHEN_P = WHEN_SQL.format(p="p.")
    return conn.execute(
        f"""
        WITH counts AS (
            SELECT tag, COUNT(*) AS photos, 0 AS collections FROM photo_tags GROUP BY tag
            UNION ALL
            SELECT tag, 0, COUNT(*) FROM collection_tags GROUP BY tag
        ), t AS (
            SELECT tag, SUM(photos) AS photos, SUM(collections) AS collections,
                   SUM(photos) + SUM(collections) AS n
            FROM counts GROUP BY tag
        )
        SELECT t.*, COALESCE(
            (SELECT p.sha256 FROM photo_tags pt JOIN photos p USING (sha256)
              WHERE pt.tag = t.tag ORDER BY {WHEN_P} DESC LIMIT 1),
            (SELECT COALESCE(c.cover_sha,
                    (SELECT ap.sha256 FROM album_photos ap JOIN photos p USING (sha256)
                      WHERE ap.album_id = c.id ORDER BY {WHEN_P} DESC LIMIT 1))
               FROM collection_tags ct JOIN collections c ON c.id = ct.collection_id
              WHERE ct.tag = t.tag AND c.kind = 'album' LIMIT 1)
        ) AS cover
        FROM t ORDER BY {order}
        """
    ).fetchall()


# ---- search -----------------------------------------------------------------

# hashtags: tags applied from the grid are found by a plain word too, as tags typed into
# a note always were.
SEARCH_FIELDS = ("note1", "note2", "hashtags", "original_filename", "camera", "lens", WHEN)


def tag_term(token: str) -> str:
    """A '#tag' search word in the form tags are stored in (see extract_hashtags)."""
    return token.lstrip("#").rstrip("-_").lower()
ORDER_BY = f"ORDER BY {WHEN} DESC, sha256"


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def build_search(q: str, media: str, album: int | None = None, camera: str = "") -> tuple[str, list]:
    """'#beach sunset' -> tagged #beach AND 'sunset' in notes/filename/camera/lens/date."""
    where, params = [], []
    if album is not None:
        where.append("sha256 IN (SELECT sha256 FROM album_photos WHERE album_id = ?)")
        params.append(album)
    if camera == "__unknown__":
        where.append("(camera IS NULL OR TRIM(camera) = '')")
    elif camera:
        where.append("camera = ?")
        params.append(camera)
    for token in (q or "").split():
        if token.startswith("#") and len(token) > 1:
            where.append("sha256 IN (SELECT sha256 FROM photo_tags WHERE tag = ?)")
            params.append(tag_term(token))
        else:
            where.append("(" + " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in SEARCH_FIELDS) + ")")
            params += [_like(token)] * len(SEARCH_FIELDS)
    if media in ("image", "raw", "video"):
        where.append("media_type = ?")
        params.append(media)
    return (" WHERE " + " AND ".join(where)) if where else "", params


def search(conn, q: str = "", media: str = "", limit: int | None = None, offset: int = 0,
           album: int | None = None, camera: str = ""):
    clause, params = build_search(q, media, album, camera)
    sql = f"SELECT * FROM photos{clause} {ORDER_BY}"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    return conn.execute(sql, params).fetchall()


def search_ids(conn, q: str = "", media: str = "", album: int | None = None, camera: str = "") -> list[str]:
    clause, params = build_search(q, media, album, camera)
    return [r[0] for r in conn.execute(f"SELECT sha256 FROM photos{clause} {ORDER_BY}", params)]


def search_count(conn, q: str = "", media: str = "", album: int | None = None, camera: str = "") -> int:
    clause, params = build_search(q, media, album, camera)
    return conn.execute(f"SELECT COUNT(*) FROM photos{clause}", params).fetchone()[0]


def all_cameras(conn) -> list[dict]:
    """Distinct smartphone/camera sources in the library, ordered by item count."""
    rows = conn.execute(f"""
        WITH ranked AS (
            SELECT sha256, camera, media_type, {WHEN} AS captured_at,
                   COALESCE(NULLIF(TRIM(camera), ''), '__unknown__') AS source_key,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(NULLIF(TRIM(camera), ''), '__unknown__')
                       ORDER BY {WHEN} DESC, sha256
                   ) AS rn
            FROM photos
        )
        SELECT source_key AS key,
               CASE WHEN source_key = '__unknown__' THEN 'Unknown device' ELSE source_key END AS name,
               COUNT(*) AS count,
               SUM(CASE WHEN media_type != 'video' THEN 1 ELSE 0 END) AS photos,
               SUM(CASE WHEN media_type = 'video' THEN 1 ELSE 0 END) AS videos,
               MAX(captured_at) AS latest,
               MAX(CASE WHEN rn = 1 THEN sha256 END) AS cover
        FROM ranked
        GROUP BY source_key
        ORDER BY (source_key = '__unknown__') ASC, count DESC, name COLLATE NOCASE ASC
    """).fetchall()
    return [dict(r) for r in rows]
