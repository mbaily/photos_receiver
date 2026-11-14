"""Per-library recovery and permanent deletion in the browser."""
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import photo_library
from photo_db import get_db
from photo_trash import entries, restore_one, purge_one, TrashError
from web_templates import root_of, templates, qs

router = APIRouter()
PAGE_SIZE = 50


@router.get('/trash', response_class=HTMLResponse)
def trash_page(request: Request, q: str = '', show: str = '', page: int = 1,
               message: str = '', error: str = ''):
    lib = photo_library.current()
    with get_db() as conn:
        rows = entries(lib, conn, include_unavailable=show == 'all')
    terms = q.lower().split()
    rows = [r for r in rows if all(term in r['original_filename'].lower() for term in terms)]
    total = len(rows)
    page = min(max(page, 1), max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))
    return templates.TemplateResponse(request, 'trash.html', {
        'page': 'trash', 'q': q, 'show': show, 'message': message, 'error': error,
        'rows': rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE], 'total': total,
        'number': page, 'more': page * PAGE_SIZE < total,
    })


def change(request, digests, action, show):
    lib = photo_library.current()
    selected = list(dict.fromkeys(digests))
    failed, warnings = [], []
    changed = 0
    if not selected or len(selected) > 100:
        failed.append('Select between 1 and 100 items.')
    else:
        with get_db() as conn:
            for digest in selected:
                if not re.fullmatch(r'[0-9a-f]{64}', digest):
                    failed.append('Invalid item identifier.')
                    continue
                try:
                    if action == 'restore':
                        result = restore_one(lib, conn, digest)
                        changed += 1
                        if result['elsewhere']:
                            warnings.append(f"Also exists in {result['elsewhere']}.")
                        if result['metadata_warning']:
                            warnings.append(result['metadata_warning'])
                    else:
                        removed, _ = purge_one(lib, conn, digest)
                        changed += removed
                except (OSError, TrashError, photo_library.LibraryError) as exc:
                    failed.append(str(exc))
    verb = 'Restored' if action == 'restore' else 'Permanently deleted'
    message = f'{verb} {changed} item(s).'
    if action == 'purge' and changed:
        message += ' Their hashes remain remembered to prevent re-ingestion.'
    return RedirectResponse(root_of(request) + '/trash?' + qs(
        show='all' if show == 'all' else '', message=message,
        error=' '.join(dict.fromkeys(failed + warnings))), status_code=303)


@router.post('/trash/restore')
def restore(request: Request, sha: list[str] = Form(default=[]), show: str = Form('')):
    return change(request, sha, 'restore', show)


@router.post('/trash/purge')
def purge(request: Request, sha: list[str] = Form(default=[]), show: str = Form('')):
    return change(request, sha, 'purge', show)
