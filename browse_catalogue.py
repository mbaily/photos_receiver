"""Read-only discovery of originals held in a library away from this machine."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import photo_catalogue
import photo_library
from web_templates import templates, proxy_of, qs

router = APIRouter()
PAGE_SIZE = 50


@router.get('/libraries/{library_id}/catalogue', response_class=HTMLResponse)
def catalogue(request: Request, library_id: str, q: str = '', type: str = '', page: int = 1):
    try:
        lib = photo_library.find(library_id)
    except photo_library.LibraryError as exc:
        raise HTTPException(404, str(exc))
    proxy = proxy_of(request)
    if lib.online:
        query = qs(q=q, type=type)
        return RedirectResponse(f'{proxy}/lib/{lib.id}/' + ('?' + query if query else ''), status_code=303)
    page = max(page, 1)
    rows, total = photo_catalogue.inventory(lib.id, q, type, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return templates.TemplateResponse(request, 'catalogue.html', {
        'lib': lib, 'rows': rows, 'total': total, 'q': q, 'type': type,
        'number': page, 'more': page * PAGE_SIZE < total,
        'catalogue_url': f'{proxy}/libraries/{lib.id}/catalogue',
        'cached_items': photo_catalogue.item_counts().get(lib.id, 0),
        'sync_pending': lib.id in photo_catalogue.pending_syncs(),
    })
