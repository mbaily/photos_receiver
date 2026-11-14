"""Folder / album browser routes (iCloud Photos style) for browse_photos.py.

htmx conventions used here:
- Mutations respond with HX-Trigger events instead of hand-patching the page:
  collectionsChanged (folder/album listings & headers re-fetch themselves),
  albumsChanged (the viewer's album list), gridChanged (the photo grid),
  toast ({message, level}), closeFormDialog / closePicker.
- Forms render into dialogs (#form-dialog-body, #picker-body) and re-render
  themselves with an error message on validation failure.
"""

import json
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import photo_collections as coll
import photo_db as db
from web_templates import local_path, root_of, templates

router = APIRouter()


def parent_id(value: str) -> int | None:
    return int(value) if value and value.isdigit() else None


def events(response: Response, **triggers) -> Response:
    response.headers["HX-Trigger"] = json.dumps(triggers)
    return response


def toast(message: str, level: str = "success") -> dict:
    return {"message": message, "level": level}


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def load(conn, cid: int, kind: str | None = None):
    c = coll.get(conn, cid)
    if not c or (kind and c["kind"] != kind):
        raise HTTPException(404, f"{kind or 'item'} not found")
    return c


def header_context(conn, c) -> dict:
    return {
        "c": c,
        "crumbs": coll.breadcrumbs(conn, c["id"]) if c else [],
        "ctags": coll.tags_for(conn, [c["id"]])[c["id"]] if c else [],
    }


# ---- pages ------------------------------------------------------------------

@router.get("/albums", response_class=HTMLResponse)
def albums_tree_page(request: Request, folder: str = ""):
    """The albums tree. ?folder=ID opens (and highlights) that folder."""
    focus = parent_id(folder)
    with db.get_db() as conn:
        tree = coll.tree(conn)
        open_ids = coll.ancestor_ids(conn, focus) if focus else []
    return templates.TemplateResponse(request, "albums.html", {
        "tree": tree, "open_ids": open_ids, "focus": focus, "page": "albums", "q": "",
    })


@router.get("/folders/{fid}")
def folder_redirect(request: Request, fid: int):
    return RedirectResponse(f"{root_of(request)}/albums?folder={fid}", status_code=303)


@router.get("/albums/{aid}", response_class=HTMLResponse)
def album_page(request: Request, aid: int, q: str = "", type: str = ""):
    with db.get_db() as conn:
        c = load(conn, aid, "album")
        ctx = header_context(conn, c)
    return templates.TemplateResponse(request, "index.html", {
        **ctx, "q": q, "type": type, "album": aid, "page": "album",
    })


# ---- partials that re-fetch themselves on collectionsChanged ----------------

@router.get("/tree", response_class=HTMLResponse)
def tree_partial(request: Request):
    with db.get_db() as conn:
        tree = coll.tree(conn)
    return templates.TemplateResponse(request, "_tree.html", {"tree": tree, "open_ids": [], "focus": None})


@router.post("/tree/move", response_class=HTMLResponse)
def tree_move(request: Request, id: int = Form(...), parent: str = Form(""), order: str = Form("")):
    """Drag & drop from the tree. Always answers with the fresh tree (reverting a bad drop)."""
    ids = [int(x) for x in order.split(",") if x.strip().isdigit()]
    with db.get_db() as conn:
        try:
            row = coll.move(conn, id, parent_id(parent), ids)
            target = coll.get(conn, parent_id(parent)) if parent_id(parent) else None
            note = toast(f"Moved “{row['name']}” to {('“' + target['name'] + '”') if target else 'the top level'}")
        except coll.CollectionError as e:
            conn.rollback()
            note = toast(str(e), "error")
        tree = coll.tree(conn)
    resp = templates.TemplateResponse(request, "_tree.html", {"tree": tree, "open_ids": [], "focus": None})
    return events(resp, toast=note)


@router.get("/collections/{cid}/header", response_class=HTMLResponse)
def collection_header(request: Request, cid: int):
    with db.get_db() as conn:
        ctx = header_context(conn, load(conn, cid))
    return templates.TemplateResponse(request, "_collection_header.html", ctx)


# ---- create / edit / delete -------------------------------------------------

def form_response(request, conn, *, kind, cid=None, name="", parent=None, note="", error=None):
    return templates.TemplateResponse(request, "_collection_form.html", {
        "kind": kind, "cid": cid, "name": name, "parent": parent, "note": note, "error": error,
        "folders": coll.folder_options(conn, exclude=cid if kind == "folder" else None),
    })


@router.get("/collections/new", response_class=HTMLResponse)
def new_form(request: Request, kind: str = "album", parent: str = ""):
    if kind not in ("album", "folder"):
        raise HTTPException(400, "bad kind")
    with db.get_db() as conn:
        return form_response(request, conn, kind=kind, parent=parent_id(parent))


@router.get("/collections/{cid}/edit", response_class=HTMLResponse)
def edit_form(request: Request, cid: int):
    with db.get_db() as conn:
        c = load(conn, cid)
        return form_response(request, conn, kind=c["kind"], cid=cid, name=c["name"],
                             parent=c["parent_id"], note=c["note"])


@router.post("/collections", response_class=HTMLResponse)
def create(request: Request, kind: str = Form(...), name: str = Form(""), parent: str = Form(""),
           note: str = Form("")):
    with db.get_db() as conn:
        try:
            coll.create(conn, kind, name, parent_id(parent), note)
        except coll.CollectionError as e:
            return form_response(request, conn, kind=kind, name=name, parent=parent_id(parent),
                                 note=note, error=str(e))
    return events(HTMLResponse(""), collectionsChanged=True, closeFormDialog=True,
                  toast=toast(f"Created {kind} “{name.strip()}”"))


@router.post("/collections/{cid}", response_class=HTMLResponse)
def update(request: Request, cid: int, name: str = Form(""), parent: str = Form(""), note: str = Form("")):
    with db.get_db() as conn:
        c = load(conn, cid)
        try:
            coll.update(conn, cid, name, parent_id(parent), note)
        except coll.CollectionError as e:
            return form_response(request, conn, kind=c["kind"], cid=cid, name=name,
                                 parent=parent_id(parent), note=note, error=str(e))
    return events(HTMLResponse(""), collectionsChanged=True, closeFormDialog=True,
                  toast=toast("Saved"))


@router.post("/collections/{cid}/note", response_class=HTMLResponse)
def save_note(request: Request, cid: int, note: str = Form("")):
    with db.get_db() as conn:
        load(conn, cid)
        tags = coll.set_note(conn, cid, note)
    return templates.TemplateResponse(request, "_collection_note_saved.html", {
        "ctags": tags, "saved_at": datetime.now().strftime("%H:%M:%S"),
    })


@router.delete("/collections/{cid}")
def delete(request: Request, cid: int):
    with db.get_db() as conn:
        c = load(conn, cid)
        coll.delete(conn, cid)
    if local_path(request, request.headers.get("HX-Current-URL", "")) == f"/albums/{cid}":
        return Response(headers={"HX-Redirect": f"{root_of(request)}/albums"})  # the page we're on is gone
    return events(Response(), collectionsChanged=True, toast=toast(f"Deleted {c['kind']} “{c['name']}”"))


# ---- album membership -------------------------------------------------------

@router.get("/picker", response_class=HTMLResponse)
def picker(request: Request, sha: list[str] = Query(default=[])):
    with db.get_db() as conn:
        albums, exact = coll.album_options(conn)
    return templates.TemplateResponse(request, "_picker.html", {
        "shas": sha, "albums": albums, "exact": exact, "q": "",
    })


@router.get("/picker/albums", response_class=HTMLResponse)
def picker_results(request: Request, q: str = ""):
    """Incremental search results for the picker (hx-get on every keystroke, debounced)."""
    with db.get_db() as conn:
        albums, exact = coll.album_options(conn, q)
    return templates.TemplateResponse(request, "_picker_results.html", {"albums": albums, "exact": exact, "q": q.strip()})


@router.post("/albums/{aid}/add", response_class=HTMLResponse)
def add_to_album(aid: int, sha: list[str] = Form(default=[])):
    with db.get_db() as conn:
        try:
            album = load(conn, aid, "album")
            added = coll.add_photos(conn, aid, sha)
        except coll.CollectionError as e:
            conn.rollback()
            return events(HTMLResponse(""), toast=toast(str(e), "error"))
    skipped = len(set(sha)) - added
    msg = f"Added {plural(added, 'photo')} to “{album['name']}”"
    if skipped:
        msg += f" ({skipped} already there)"
    return events(HTMLResponse(""), toast=toast(msg), albumsChanged=True, collectionsChanged=True,
                  closePicker=True)


@router.post("/picker/new-album", response_class=HTMLResponse)
def new_album_with_photos(request: Request, name: str = Form(""), parent: str = Form(""),
                          sha: list[str] = Form(default=[])):
    with db.get_db() as conn:
        try:
            aid = coll.create(conn, "album", name, parent_id(parent))
            added = coll.add_photos(conn, aid, sha)
        except coll.CollectionError as e:
            conn.rollback()
            return events(HTMLResponse(""), toast=toast(str(e), "error"))
    return events(HTMLResponse(""), toast=toast(f"Created “{name.strip()}” with {plural(added, 'photo')}"),
                  albumsChanged=True, collectionsChanged=True, closePicker=True)


@router.post("/albums/{aid}/remove", response_class=HTMLResponse)
def remove_from_album(aid: int, sha: list[str] = Form(default=[])):
    with db.get_db() as conn:
        load(conn, aid, "album")
        removed = coll.remove_photos(conn, aid, sha)
    return events(HTMLResponse(""), toast=toast(f"Removed {plural(removed, 'photo')} from the album"),
                  gridChanged=True, collectionsChanged=True, albumsChanged=True)


@router.post("/albums/{aid}/cover", response_class=HTMLResponse)
def set_cover(aid: int, sha: list[str] = Form(default=[])):
    with db.get_db() as conn:
        try:
            if len(sha) != 1:
                raise coll.CollectionError("Select exactly one photo for the cover.")
            coll.set_cover(conn, aid, sha[0])
        except coll.CollectionError as e:
            conn.rollback()
            return events(HTMLResponse(""), toast=toast(str(e), "error"))
    return events(HTMLResponse(""), toast=toast("Album cover updated"), collectionsChanged=True)


@router.get("/photo/{sha}/albums", response_class=HTMLResponse)
def photo_albums(request: Request, sha: str):
    with db.get_db() as conn:
        albums = coll.albums_for_photo(conn, sha)
    return templates.TemplateResponse(request, "_photo_albums.html", {"sha": sha, "albums": albums})


@router.delete("/albums/{aid}/photos/{sha}", response_class=HTMLResponse)
def remove_one(request: Request, aid: int, sha: str):
    with db.get_db() as conn:
        coll.remove_photos(conn, aid, [sha])
        albums = coll.albums_for_photo(conn, sha)
    resp = templates.TemplateResponse(request, "_photo_albums.html", {"sha": sha, "albums": albums})
    current = local_path(request, request.headers.get("HX-Current-URL", ""))
    if current == f"/albums/{aid}":  # viewing that album: its grid is now stale
        return events(resp, gridChanged=True, collectionsChanged=True)
    return events(resp, collectionsChanged=True)
