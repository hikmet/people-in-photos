"""Photo sets: the overview, uploads with QR grouping, and set cleanup."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import storage
from app.db import get_db
from app.i18n import fail
from app.matching import natural_filename, scan_card_codes
from app.models import Card, CardPhoto, Photo, PhotoSet, uid
from app.security import normalize
from app.services import (
    attach_photo,
    enqueue,
    event,
    found,
    latest,
    links,
    photo_decision,
    photo_permission,
    regroup_upload,
    set_marker,
)
from app.web import UPLOAD_MAX_BYTES, form_data, go, render, require_photographer, required

router = APIRouter()


@router.get("/dashboard")
def dashboard(request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    sets = list(db.scalars(select(PhotoSet).order_by(PhotoSet.created_at.desc())))
    photos = list(
        db.scalars(
            select(Photo)
            .where(Photo.status.not_in(["deleted", "marker"]))
            .order_by(Photo.created_at.desc())
        )
    )
    cards = list(db.scalars(select(Card).order_by(Card.created_at.desc())))
    return render(
        request,
        "dashboard.html",
        photographer,
        sets=sets,
        photos=photos,
        cards=cards,
        ready=sum(photo_permission(db, p) == "ready" for p in photos),
        set_photos=lambda s: [p for p in photos if p.photo_set_id == s.id],
        card_count=lambda s: sum(c.photo_set_id == s.id for c in cards),
    )


@router.get("/dashboard/sets")
def sets_page(request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    return dashboard(request, photographer, db)


@router.post("/dashboard/sets")
async def create_set(request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    form = await form_data(request)
    row = PhotoSet(
        name=required(form, "name"),
        location=str(form.get("location", ""))[:150],
        request_consent=form.get("request_consent") == "on",
    )
    db.add(row)
    db.commit()
    return go("/dashboard/sets/" + row.id)


@router.get("/dashboard/sets/{identity}")
def set_page(identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    row = found(db, PhotoSet, identity)
    photos = list(
        db.scalars(
            select(Photo)
            .where(Photo.photo_set_id == row.id, Photo.status.not_in(["deleted", "marker"]))
            .order_by(Photo.created_at)
        )
    )
    cards = list(db.scalars(select(Card).where(Card.photo_set_id == row.id).order_by(Card.created_at)))
    card_photos = {card.id: links(db, card) for card in cards}
    records = {card.id: latest(db, card.id) for card in cards}
    preview = {p.id: [] for p in photos}
    for card in cards:
        for link, photo in card_photos[card.id]:
            if photo.id in preview:
                record = records[card.id]
                preview[photo.id].append((card, link, record, photo_decision(record, photo, row.request_consent)))
    cleanup_ready = all(
        card.status == "unavailable"
        or (card_photos[card.id] and all(link.delivered for link, _ in card_photos[card.id]))
        for card in cards
    )
    unassigned_count = db.scalar(
        select(func.count())
        .select_from(Photo)
        .where(
            Photo.photo_set_id == row.id,
            Photo.status.not_in(["deleted", "marker"]),
            Photo.id.not_in(select(CardPhoto.photo_id)),
        )
    ) or 0
    return render(
        request,
        "set.html",
        photographer,
        photo_set=row,
        photos=photos,
        cards=cards,
        permissions={p.id: photo_permission(db, p) for p in photos},
        card_photos=card_photos,
        preview=preview,
        # A photo nobody has been given yet can be put on a card by hand, or may turn out to be a QR
        # frame the scanner missed.
        markable={p.id: not any(link.delivered for _, link, _, _ in preview[p.id]) for p in photos},
        open_cards=[card for card in cards if card.status in ("waiting", "delivered")],
        cleanup_ready=cleanup_ready,
        unassigned_count=unassigned_count,
        cleaned=request.query_params.get("cleaned"),
        marked=request.query_params.get("marked"),
        attached=request.query_params.get("attached"),
        upload_markers=request.query_params.get("markers"),
        upload_grouped=request.query_params.get("grouped"),
        upload_unassigned=request.query_params.get("unassigned"),
        upload_skipped=request.query_params.get("skipped", "")[:300],
    )


@router.post("/dashboard/sets/{identity}/delete-unassigned")
async def delete_unassigned(
    identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)
):
    await form_data(request)
    row = found(db, PhotoSet, identity)
    cards = list(db.scalars(select(Card).where(Card.photo_set_id == row.id)))
    if any(
        card.status != "unavailable"
        and (not (linked := links(db, card)) or any(not link.delivered for link, _ in linked))
        for card in cards
    ):
        raise fail(409, "err_cleanup")
    photos = list(
        db.scalars(
            select(Photo)
            .where(
                Photo.photo_set_id == row.id,
                Photo.status != "deleted",
                Photo.id.not_in(select(CardPhoto.photo_id)),
            )
        )
    )
    # Nothing here was ever proposed to a card, so no consent record or event names these rows:
    # they go entirely, and the same files can be uploaded again later. QR marker frames go with
    # them. Photos deleted from a card keep their row as the record (see delete_card_photos).
    for photo in photos:
        enqueue(db, "purge_photo", photo_id=photo.id)
        db.delete(photo)
    cleaned = sum(photo.status != "marker" for photo in photos)
    event(db, "unassigned_photos_deleted", photo_set_id=row.id, count=cleaned)
    db.commit()
    return go(f"/dashboard/sets/{identity}?cleaned={cleaned}")


@router.post("/dashboard/sets/{identity}/edit")
async def edit_set(identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    form = await form_data(request)
    row = found(db, PhotoSet, identity)
    row.name, row.location = required(form, "name"), str(form.get("location", ""))[:150]
    row.description = str(form.get("description", ""))[:2000]
    row.archived = form.get("archived") == "on"
    row.request_consent = form.get("request_consent") == "on"
    db.commit()
    return go("/dashboard/sets/" + identity)


@router.post("/dashboard/sets/{identity}/upload")
async def upload(identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    form = await form_data(request)
    mode = str(form.get("grouping_mode", "qr_after"))
    if mode not in {"qr_after", "qr_before"}:
        raise fail(400, "err_grouping_mode")
    files = sorted(
        (file for file in form.getlist("photos") if hasattr(file, "read")),
        key=lambda file: natural_filename(Path(getattr(file, "filename", "") or "photo.jpg").name),
    )
    if not files:
        raise fail(400, "err_jpeg")
    # Files the browser left out because they were not JPEGs, named in the notice after upload.
    skipped = str(form.get("skipped", ""))[:300]
    upload_id = uid()
    received, kept = [], set()
    try:
        # Reading, hashing, QR scanning and storing every file comes first and outside any
        # transaction, so the database's single write lock (app/db.py) is held only for the rows.
        for file in files:
            name = Path(file.filename or "photo.jpg").name[:200]
            try:
                received.append(await receive(file, name))
            except HTTPException as exc:
                # A batch fails on one file; the message names it so the right file gets fixed.
                exc.detail = f"{name}: {exc.detail}"
                raise
        row = found(db, PhotoSet, identity)
        if row.archived:
            raise fail(409, "err_archived")
        marker_cards = {}
        for item in received:
            try:
                if record(db, row, item, upload_id, mode, marker_cards):
                    kept.add(item.photo_id)
            except HTTPException as exc:
                exc.detail = f"{item.name}: {exc.detail}"
                raise
        db.flush()
        grouped, unassigned = regroup_upload(db, row, upload_id, mode)
        db.commit()
    except Exception:
        db.rollback()
        kept.clear()
        raise
    finally:
        for item in received:
            if item.photo_id not in kept:
                storage.delete(storage.photo_key(item.photo_id, "original"))
    query = f"markers={len(marker_cards)}&grouped={grouped}&unassigned={unassigned}"
    if skipped:
        query += "&skipped=" + quote(skipped)
    return go(f"/dashboard/sets/{identity}?{query}")


@dataclass
class Received:
    """An uploaded file, checked, scanned and stored under the id it gets if it becomes a new row."""

    name: str
    digest: str
    size: int
    codes: set
    photo_id: str


async def receive(file, name):
    data = await file.read(UPLOAD_MAX_BYTES + 1)
    if len(data) > UPLOAD_MAX_BYTES:
        raise fail(400, "err_too_large")
    if not data.startswith(b"\xff\xd8\xff"):
        raise fail(400, "err_not_jpeg")
    codes = await run_in_threadpool(scan_card_codes, data)
    if len(codes) > 1:
        raise fail(400, "err_one_marker")
    item = Received(name, hashlib.sha256(data).hexdigest(), len(data), codes, uid())
    # Stored once, untouched: this is the original everyone downloads (worker.process_photo).
    await run_in_threadpool(storage.put, storage.photo_key(item.photo_id, "original"), data)
    return item


def record(db, row, item, upload_id, mode, marker_cards):
    """One received file as a QR marker frame for a card, or a photo queued for processing.
    True when it became a new row, so its stored original is kept."""
    photo = db.scalar(select(Photo).where(Photo.original_sha256 == item.digest))
    if photo and (photo.photo_set_id != row.id or photo.status == "deleted"):
        raise fail(409, "err_marker_elsewhere" if item.codes else "err_duplicate")
    created = not photo
    if item.codes:
        card = marker_card(db, row, next(iter(item.codes)))
        marker_cards[card.id] = card
        if photo:
            # Uploaded before as an ordinary photo (the scan failed then, or it was assigned by
            # hand): it now stands for this card.
            set_marker(db, photo, card)
        else:
            # The marker frame is kept, hidden and unprocessed, so the sequence it marks can be
            # regrouped later.
            photo = Photo(status="marker", marker_card_id=card.id)
    elif not photo:
        photo = Photo()
        enqueue(db, "process_photo", photo_id=item.photo_id)
    if created:
        photo.id, photo.photo_set_id, photo.filename = item.photo_id, row.id, item.name
        photo.original_sha256, photo.size = item.digest, item.size
        db.add(photo)
    # A photo uploaded again joins this upload's sequence; a marker row stays a marker even when
    # the scan fails this time.
    photo.upload_id, photo.grouping_mode = upload_id, mode
    return created


def marker_card(db, photo_set, card_code, unknown="err_marker_unknown"):
    """The card a QR marker stands for, if it can still receive photos in this set."""
    card = db.scalar(select(Card).where(Card.code == card_code))
    if not card:
        raise fail(400, unknown)
    if card.status not in {"waiting", "delivered"}:
        raise fail(409, "err_marker_closed")
    if card.photo_set_id and card.photo_set_id != photo_set.id:
        raise fail(409, "err_marker_other_set")
    return card


@router.post("/dashboard/sets/{identity}/marker")
async def assign_marker(identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    """A QR frame the scanner missed: the photographer names its card, and its upload is regrouped."""
    form = await form_data(request)
    row = found(db, PhotoSet, identity)
    if row.archived:
        raise fail(409, "err_archived")
    photo = found(db, Photo, str(form.get("photo_id", "")))
    if photo.photo_set_id != row.id or photo.status == "deleted":
        raise fail(404, "err_not_found")
    try:
        card_code = normalize(str(form.get("code", "")))
    except HTTPException:
        raise fail(400, "err_marker_code") from None
    card = marker_card(db, row, card_code, unknown="err_marker_code")
    set_marker(db, photo, card)
    event(db, "marker_assigned", card.id, photo_id=photo.id, filename=photo.filename)
    grouped, unassigned = regroup_upload(db, row, photo.upload_id, photo.grouping_mode)
    db.commit()
    return go(f"/dashboard/sets/{identity}?marked=1&grouped={grouped}&unassigned={unassigned}")


@router.post("/dashboard/sets/{identity}/attach")
async def attach(identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    """A photo the scan left out: the photographer picks one of the set's cards for it."""
    form = await form_data(request)
    row = found(db, PhotoSet, identity)
    if row.archived:
        raise fail(409, "err_archived")
    photo = found(db, Photo, str(form.get("photo_id", "")))
    if photo.photo_set_id != row.id or photo.status in ("deleted", "marker"):
        raise fail(404, "err_not_found")
    card = found(db, Card, str(form.get("card_id", "")))
    if card.photo_set_id != row.id:
        raise fail(404, "err_not_found")
    attach_photo(db, photo, card)
    db.commit()
    return go(f"/dashboard/sets/{identity}?attached=1")
