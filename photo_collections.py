"""Folders and albums (iCloud Photos style), stored in inbox.db.

- A folder contains folders and albums; an album contains photos (by reference -
  a photo can be in many albums, and removing it from an album never deletes it).
- Both have a note whose #hashtags are indexed in collection_tags.
- Deleting a folder deletes everything inside it (albums, sub-folders), not the photos.
"""

import sqlite3
from datetime import datetime, timezone

from photo_db import WHEN_SQL, _like, extract_hashtags, tag_term

MAX_NAME = 200
LATEST = f"ORDER BY {WHEN_SQL.format(p='p.')} DESC"
SIBLING_ORDER = "ORDER BY c.position IS NULL, c.position, c.name COLLATE NOCASE"
COVER_SQL = f"""COALESCE(c.cover_sha,
    (SELECT ap.sha256 FROM album_photos ap JOIN photos p USING (sha256)
      WHERE ap.album_id = c.id {LATEST} LIMIT 1))"""


class CollectionError(ValueError):
    """User-facing validation error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise CollectionError("Name can't be empty.")
    if len(name) > MAX_NAME:
        raise CollectionError(f"Name is longer than {MAX_NAME} characters.")
    return name


def get(conn: sqlite3.Connection, cid: int) -> sqlite3.Row | None:
    return conn.execute(
        f"""SELECT c.*, {COVER_SQL} AS cover,
                   (SELECT COUNT(*) FROM album_photos ap WHERE ap.album_id = c.id) AS photo_count
            FROM collections c WHERE c.id = ?""",
        (cid,),
    ).fetchone()


def _check_parent(conn, parent_id: int | None, moving_id: int | None = None) -> None:
    if parent_id is None:
        return
    parent = conn.execute("SELECT kind FROM collections WHERE id = ?", (parent_id,)).fetchone()
    if not parent:
        raise CollectionError("That folder no longer exists.")
    if parent["kind"] != "folder":
        raise CollectionError("Only folders can contain other items.")
    if moving_id is not None and parent_id in descendant_ids(conn, moving_id, include_self=True):
        raise CollectionError("A folder can't be moved inside itself.")


def _set_tags(conn, cid: int, note: str) -> list[str]:
    tags = extract_hashtags(note)
    conn.execute("DELETE FROM collection_tags WHERE collection_id = ?", (cid,))
    conn.executemany("INSERT INTO collection_tags (collection_id, tag) VALUES (?, ?)", [(cid, t) for t in tags])
    return tags


def create(conn, kind: str, name: str, parent_id: int | None = None, note: str = "") -> int:
    if kind not in ("folder", "album"):
        raise CollectionError("Unknown type.")
    name = _clean_name(name)
    _check_parent(conn, parent_id)
    now = _now()
    cid = conn.execute(
        "INSERT INTO collections (kind, name, parent_id, note, created_at, updated_at, position) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, name, parent_id, note or "", now, now, _next_position(conn, parent_id)),
    ).lastrowid
    _set_tags(conn, cid, note)
    return cid


def _next_position(conn, parent_id: int | None) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(position) + 1, 0) FROM collections WHERE parent_id IS ?", (parent_id,)
    ).fetchone()[0]


def update(conn, cid: int, name: str, parent_id: int | None, note: str) -> None:
    name = _clean_name(name)
    _check_parent(conn, parent_id, moving_id=cid)
    old_parent = conn.execute("SELECT parent_id FROM collections WHERE id = ?", (cid,)).fetchone()[0]
    if old_parent != parent_id:  # moved via the form: append to the new folder
        conn.execute("UPDATE collections SET position = ? WHERE id = ?", (_next_position(conn, parent_id), cid))
    conn.execute(
        "UPDATE collections SET name = ?, parent_id = ?, note = ?, updated_at = ? WHERE id = ?",
        (name, parent_id, note or "", _now(), cid),
    )
    _set_tags(conn, cid, note)


def move(conn, cid: int, parent_id: int | None, order: list[int]) -> sqlite3.Row:
    """Drag & drop: put cid inside parent_id (None = top level), siblings ordered as `order`."""
    row = conn.execute("SELECT * FROM collections WHERE id = ?", (cid,)).fetchone()
    if not row:
        raise CollectionError("That item no longer exists.")
    _check_parent(conn, parent_id, moving_id=cid)
    conn.execute("UPDATE collections SET parent_id = ? WHERE id = ?", (parent_id, cid))
    siblings = [r[0] for r in conn.execute(
        f"SELECT c.id FROM collections c WHERE c.parent_id IS ? {SIBLING_ORDER}", (parent_id,))]
    wanted = [i for i in order if i in siblings]
    final = wanted + [i for i in siblings if i not in wanted]
    conn.executemany("UPDATE collections SET position = ? WHERE id = ?", [(n, i) for n, i in enumerate(final)])
    return row


def set_note(conn, cid: int, note: str) -> list[str]:
    conn.execute("UPDATE collections SET note = ?, updated_at = ? WHERE id = ?", (note or "", _now(), cid))
    return _set_tags(conn, cid, note)


def delete(conn, cid: int) -> None:
    conn.execute("DELETE FROM collections WHERE id = ?", (cid,))  # cascades to contents


def descendant_ids(conn, cid: int, include_self: bool = False) -> set[int]:
    rows = conn.execute(
        """WITH RECURSIVE sub(id) AS (
               SELECT ? UNION ALL SELECT c.id FROM collections c JOIN sub ON c.parent_id = sub.id)
           SELECT id FROM sub""",
        (cid,),
    )
    ids = {r[0] for r in rows}
    if not include_self:
        ids.discard(cid)
    return ids


def breadcrumbs(conn, cid: int | None) -> list[sqlite3.Row]:
    """Ancestors from the top level down to (and including) cid."""
    if cid is None:
        return []
    rows = conn.execute(
        """WITH RECURSIVE up(id, parent_id, kind, name, depth) AS (
               SELECT id, parent_id, kind, name, 0 FROM collections WHERE id = ?
               UNION ALL
               SELECT c.id, c.parent_id, c.kind, c.name, up.depth + 1
               FROM collections c JOIN up ON c.id = up.parent_id)
           SELECT * FROM up ORDER BY depth DESC""",
        (cid,),
    ).fetchall()
    return rows


def folder_mosaic(conn, folder_id: int, limit: int = 4) -> list[str]:
    rows = conn.execute(
        f"""WITH RECURSIVE sub(id) AS (
                SELECT ? UNION ALL SELECT c.id FROM collections c JOIN sub ON c.parent_id = sub.id)
            SELECT {COVER_SQL} AS cover FROM collections c
            WHERE c.id IN sub AND c.kind = 'album' ORDER BY c.updated_at DESC""",
        (folder_id,),
    )
    covers = []
    for r in rows:
        if r["cover"] and r["cover"] not in covers:
            covers.append(r["cover"])
            if len(covers) == limit:
                break
    return covers


def tree(conn) -> list[dict]:
    """Whole hierarchy as nested dicts (siblings in manual order): [{..., 'children': [...]}]."""
    rows = conn.execute(
        f"""SELECT c.id, c.kind, c.name, c.parent_id, c.note, c.updated_at, {COVER_SQL} AS cover,
                  (SELECT COUNT(*) FROM album_photos ap WHERE ap.album_id = c.id) AS photo_count
           FROM collections c {SIBLING_ORDER}"""
    ).fetchall()
    tags = tags_for(conn, [r["id"] for r in rows])
    nodes = {r["id"]: {**dict(r), "tags": tags[r["id"]], "children": []} for r in rows}
    roots = []
    for node in nodes.values():
        parent = nodes.get(node["parent_id"])
        (parent["children"] if parent else roots).append(node)
    return roots


def ancestor_ids(conn, cid: int) -> list[int]:
    return [r["id"] for r in breadcrumbs(conn, cid)]


def folder_options(conn, exclude: int | None = None) -> list[tuple[int, str, int]]:
    """(id, name, depth) for every folder, depth-first; excludes `exclude` and its subtree."""
    skip = descendant_ids(conn, exclude, include_self=True) if exclude else set()
    out = []

    def walk(nodes, depth):
        for n in nodes:
            if n["kind"] == "folder" and n["id"] not in skip:
                out.append((n["id"], n["name"], depth))
                walk(n["children"], depth + 1)

    walk(tree(conn), 0)
    return out


# ---- album membership -------------------------------------------------------

def _require_album(conn, album_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM collections WHERE id = ?", (album_id,)).fetchone()
    if not row:
        raise CollectionError("That album no longer exists.")
    if row["kind"] != "album":
        raise CollectionError("Photos can only be added to albums, not folders.")
    return row


def add_photos(conn, album_id: int, shas: list[str]) -> int:
    _require_album(conn, album_id)
    now = _now()
    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO album_photos (album_id, sha256, added_at)
           SELECT ?, sha256, ? FROM photos WHERE sha256 = ?""",
        [(album_id, now, s) for s in dict.fromkeys(shas)],
    )
    added = conn.total_changes - before
    if added:
        conn.execute("UPDATE collections SET updated_at = ? WHERE id = ?", (now, album_id))
    return added


def remove_photos(conn, album_id: int, shas: list[str]) -> int:
    before = conn.total_changes
    conn.executemany(
        "DELETE FROM album_photos WHERE album_id = ? AND sha256 = ?", [(album_id, s) for s in shas]
    )
    removed = conn.total_changes - before
    conn.execute(
        "UPDATE collections SET cover_sha = NULL WHERE id = ? AND cover_sha NOT IN "
        "(SELECT sha256 FROM album_photos WHERE album_id = ?)",
        (album_id, album_id),
    )
    return removed


def set_cover(conn, album_id: int, sha: str) -> None:
    _require_album(conn, album_id)
    if not conn.execute(
        "SELECT 1 FROM album_photos WHERE album_id = ? AND sha256 = ?", (album_id, sha)
    ).fetchone():
        raise CollectionError("The cover must be a photo in this album.")
    conn.execute("UPDATE collections SET cover_sha = ? WHERE id = ?", (sha, album_id))


def albums_for_photo(conn, sha: str) -> list[dict]:
    rows = conn.execute(
        """SELECT c.id, c.name FROM album_photos ap JOIN collections c ON c.id = ap.album_id
           WHERE ap.sha256 = ? ORDER BY c.name COLLATE NOCASE""",
        (sha,),
    ).fetchall()
    out = []
    for r in rows:
        crumbs = breadcrumbs(conn, r["id"])
        out.append({"id": r["id"], "name": r["name"], "folders": [(c["id"], c["name"]) for c in crumbs[:-1]]})
    return out


def album_options(conn, q: str = "", limit: int = 50) -> tuple[list[dict], bool]:
    """Albums for the incremental picker, each with its folder path.

    Empty query: most recently changed first. Otherwise every word must appear in
    the album's path ("home org" finds Home › Organisation 2026); name matches rank first.
    Returns (albums, exact_name_exists).
    """
    rows = conn.execute(
        f"""SELECT c.id, c.kind, c.name, c.parent_id, c.updated_at, {COVER_SQL} AS cover,
                   (SELECT COUNT(*) FROM album_photos ap WHERE ap.album_id = c.id) AS photo_count
            FROM collections c"""
    ).fetchall()
    by_id = {r["id"]: r for r in rows}

    def folders_of(r):
        chain, pid, seen = [], r["parent_id"], set()
        while pid in by_id and pid not in seen:
            seen.add(pid)
            chain.append(by_id[pid]["name"])
            pid = by_id[pid]["parent_id"]
        return chain[::-1]

    albums = [{**dict(r), "folders": folders_of(r)} for r in rows if r["kind"] == "album"]
    terms = q.lower().split()
    exact = any(a["name"].lower() == q.strip().lower() for a in albums) if q.strip() else False
    if not terms:
        albums.sort(key=lambda a: a["updated_at"], reverse=True)
        return albums[:limit], exact

    def rank(a):
        name = a["name"].lower()
        return (0 if name.startswith(terms[0]) else 1 if terms[0] in name else 2, name)

    hits = [a for a in albums
            if all(t in " ".join([*a["folders"], a["name"]]).lower() for t in terms)]
    return sorted(hits, key=rank)[:limit], exact


# ---- tags & search ----------------------------------------------------------

def tags_for(conn, ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {i: [] for i in ids}
    if ids:
        rows = conn.execute(
            f"SELECT collection_id, tag FROM collection_tags WHERE collection_id IN ({','.join('?' * len(ids))}) ORDER BY tag",
            ids,
        )
        for r in rows:
            out[r["collection_id"]].append(r["tag"])
    return out


def search(conn, q: str, limit: int = 24) -> list[dict]:
    """Albums/folders matching every term: '#tag' terms by tag, words by name or note."""
    where, params = [], []
    for token in (q or "").split():
        if token.startswith("#") and len(token) > 1:
            where.append("c.id IN (SELECT collection_id FROM collection_tags WHERE tag = ?)")
            params.append(tag_term(token))
        else:
            where.append("(c.name LIKE ? ESCAPE '\\' OR c.note LIKE ? ESCAPE '\\')")
            params += [_like(token)] * 2
    if not where:
        return []
    rows = conn.execute(
        f"""SELECT c.*, {COVER_SQL} AS cover,
                   (SELECT COUNT(*) FROM album_photos ap WHERE ap.album_id = c.id) AS photo_count,
                   (SELECT COUNT(*) FROM collections k WHERE k.parent_id = c.id) AS child_count
            FROM collections c WHERE {' AND '.join(where)}
            ORDER BY c.kind = 'album', c.name COLLATE NOCASE LIMIT ?""",
        [*params, limit],
    ).fetchall()
    tags = tags_for(conn, [r["id"] for r in rows])
    return [{**dict(r), "tags": tags[r["id"]],
             "mosaic": folder_mosaic(conn, r["id"]) if r["kind"] == "folder" else []} for r in rows]
