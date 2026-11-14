"""Trash operations shared by the browser and command-line recovery tool."""
import os
from pathlib import Path

import photo_catalogue
from photo_db import restore_photo
from photo_meta import index_photo
from photo_store import elsewhere, library_lock


class TrashError(ValueError):
    pass


def entries(lib, conn, include_unavailable=False):
    result = []
    for row in conn.execute('SELECT * FROM deleted_photos ORDER BY deleted_at DESC, sha256'):
        path = lib.trash_dir / Path(row['saved_filename']).name
        try:
            available, size = path.is_file(), path.stat().st_size
        except OSError:
            available, size = False, 0
        if available or include_unavailable:
            result.append({**dict(row), 'available': available, 'size_bytes': size})
    return result


def restore_one(lib, conn, digest):
    with library_lock(lib):
        row = conn.execute('SELECT * FROM deleted_photos WHERE sha256 = ?', (digest,)).fetchone()
        if not row:
            raise TrashError('Item is no longer in the trash.')
        name = Path(row['saved_filename']).name
        src, dest = lib.trash_dir / name, lib.path / name
        if not src.is_file():
            raise TrashError('The original is no longer in the trash; it cannot be restored.')
        if dest.exists() or dest.is_symlink():
            raise TrashError(f'{name} already exists; it was not overwritten.')
        os.replace(src, dest)
        try:
            result = restore_photo(conn, digest, dest.stat().st_size)
            conn.commit()
        except Exception:
            conn.rollback()
            os.replace(dest, src)
            raise
    warning = ''
    try:
        index_photo(conn, digest)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        warning = f'Metadata could not be re-read: {exc}'
    photo_catalogue.refresh(lib.id, conn, [digest])
    return {**result, 'name': row['original_filename'], 'metadata_warning': warning,
            'elsewhere': elsewhere([digest], lib.id).get(digest)}


def purge_one(lib, conn, digest):
    """Remove only the original. Keep its deletion record to prevent re-ingestion."""
    with library_lock(lib):
        row = conn.execute('SELECT saved_filename FROM deleted_photos WHERE sha256 = ?', (digest,)).fetchone()
        if not row:
            raise TrashError('Item is no longer in the trash.')
        path = lib.trash_dir / Path(row['saved_filename']).name
        try:
            size = path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            return 0, 0
        return 1, size
