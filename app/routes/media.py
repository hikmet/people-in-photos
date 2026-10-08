"""Photo files, for recipients and the photographer alike, behind short-lived signed URLs."""

import re

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import storage
from app.db import get_db
from app.i18n import fail
from app.models import Card, CardPhoto, Photo
from app.security import unsign
from app.services import event
from app.web import require_photographer

router = APIRouter()


@router.get("/media/{token}")
def media_file(token: str, request: Request, db: Session = Depends(get_db)):
    value = unsign(token, "media", 900)
    if not value.get("card"):
        # The photographer's own view. Checked before this request's transaction begins: the
        # sign-in check reads in a session of its own, which would wait on this one's lock.
        require_photographer(request)
    photo = db.get(Photo, value["photo"])
    variant = value["variant"]
    if not photo or photo.status != "ready":
        raise fail(404, "err_not_found")
    if variant not in storage.VARIANTS:
        raise fail(404, "err_not_found")
    download = bool(request.query_params.get("download"))
    if value.get("card"):
        card = db.get(Card, value["card"])
        if not card or card.status != "delivered":
            raise fail(404, "err_not_found")
        link = db.scalar(
            select(CardPhoto).where(
                CardPhoto.card_id == card.id, CardPhoto.photo_id == photo.id, CardPhoto.delivered.is_(True)
            )
        )
        # Recipients keep the full-resolution original; the smaller versions are only for viewing.
        if not link or (download and variant != "original"):
            raise fail(404, "err_not_found")
        if download:
            event(db, "photo_downloaded", card.id, "subject", photo_id=photo.id)
            db.commit()
        filename = "people-photograph-" + photo.id[:8] + ".jpg"
    else:
        filename = download_name(photo, variant)
    return storage.response(storage.photo_key(photo.id, variant), "image/jpeg", filename if download else None)


def download_name(photo, variant):
    """The photographer's own download: IMG_4908.jpeg for the original, IMG_4908-display.jpeg otherwise.

    Recipients always get the anonymous name, whichever size they are allowed."""
    name = re.sub(r'[\\/"\r\n]', "_", photo.filename) or "photo.jpg"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, "jpg"
    return f"{stem}.{ext}" if variant == "original" else f"{stem}-{variant}.{ext}"
