"""The photographer's public profile, card branding and versioned release text."""

import io

from fastapi import APIRouter, Depends, Request
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.orm import Session

from app import links, storage
from app.db import get_db
from app.i18n import fail
from app.models import now
from app.services import CARD_TEXT_MAX, current_release, new_release, profile
from app.web import form_data, go, render, require_photographer, required

router = APIRouter()

LOGO_MAX_BYTES = 5 * 1024 * 1024
LOGO_MAX_SIDE = 1200


def logo_png(data):
    """A PNG or JPEG upload as the PNG the card printer draws, downscaled so a hundred cards do not
    carry a hundred copies of a poster-sized file. SVG is not accepted: ReportLab cannot draw it
    without another library, and it would need sanitising as a script-capable format."""
    if len(data) > LOGO_MAX_BYTES:
        raise fail(400, "err_logo_large")
    try:
        with Image.open(io.BytesIO(data)) as raw:
            if raw.format not in {"PNG", "JPEG"}:
                raise fail(400, "err_logo_format")
            image = ImageOps.exif_transpose(raw).convert("RGBA")
    except UnidentifiedImageError:
        raise fail(400, "err_logo_format") from None
    image.thumbnail((LOGO_MAX_SIDE, LOGO_MAX_SIDE), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, "PNG", optimize=True)
    return output.getvalue()


@router.get("/dashboard/profile")
def profile_page(request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    release = current_release(db)
    return render(
        request,
        "profile.html",
        photographer,
        release=release,
        card_text_max=CARD_TEXT_MAX,
        link_fields=[("website", "Website")] + [(n.key, n.label) for n in links.NETWORKS],
    )


@router.get("/dashboard/profile/logo")
def logo(photographer=Depends(require_photographer)):
    if not photographer.logo_updated_at:
        raise fail(404, "err_not_found")
    return storage.response(storage.LOGO_KEY, "image/png")


@router.post("/dashboard/profile", dependencies=[Depends(require_photographer)])
async def save_profile(request: Request, db: Session = Depends(get_db)):
    form = await form_data(request)
    # Read again in this request's own session, which saves it.
    photographer = profile(db)
    photographer.public_name = required(form, "public_name", 100)
    photographer.links = links.clean(form)
    photographer.bio = str(form.get("bio", ""))[:500]
    photographer.card_text = str(form.get("card_text", "")).strip()[:CARD_TEXT_MAX]
    upload = form.get("logo")
    data = await upload.read() if hasattr(upload, "read") else b""
    if data:
        storage.put(storage.LOGO_KEY, logo_png(data))
        photographer.logo_updated_at = now()
    elif form.get("remove_logo") and photographer.logo_updated_at:
        storage.delete(storage.LOGO_KEY)
        photographer.logo_updated_at = None
    text = required(form, "release_text", 24000)
    if current_release(db).text != text:
        new_release(db, text)
    db.commit()
    return go("/dashboard/profile")
