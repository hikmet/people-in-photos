import hashlib

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.i18n import CATALOGS, LANGUAGE_PREFIX, current_lang, decision_label, fail, locale, translate
from app.icons import icon
from app.models import Card
from app.pdfs import consent_pdf
from app.security import check_csrf, csrf, format_code, normalize, sign, unsign
from app.services import exif_map, exif_tabs, profile
from app.storage import content_disposition

# The largest JPEG an upload accepts, and so the largest part of any form.
UPLOAD_MAX_BYTES = 25 * 1024 * 1024

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["language_token"] = lambda token, lang: sign(
    {"form": token, "locale": lang}, "release-language"
)


def go(url):
    if url.startswith("/") and not LANGUAGE_PREFIX.match(url):
        url = f"/{current_lang.get()}{url}"
    return RedirectResponse(url, status_code=303)


def render(request, name, photographer=None, status=200, **context):
    lang = locale(request)
    query = f"?{request.url.query}" if request.url.query else ""
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "photographer": photographer,
            "lang": lang,
            "base": f"/{lang}",
            "switch": lambda code: f"/{code}{request.url.path}{query}",
            "t": lambda key, default=None: (
                translate(lang, key) if default is None else CATALOGS[lang].get(key, default)
            ),
            "decision_label": decision_label,
            "csrf": csrf(request),
            "path": request.url.path,
            "config": settings(),
            "format_code": format_code,
            "icon": icon,
            "exif_tabs": exif_tabs,
            "exif_map": exif_map,
            "media": lambda photo, variant="thumb", card=None: (
                "/media/"
                + sign(
                    {
                        "photo": photo.id,
                        "variant": variant,
                        "card": card.id if card else None,
                    },
                    "media",
                )
            ),
            **context,
        },
        status_code=status,
    )


async def form_data(request):
    form = await request.form(max_files=500, max_fields=200, max_part_size=UPLOAD_MAX_BYTES)
    check_csrf(request, form)
    return form


def required(form, key, maximum=150):
    value = str(form.get(key, "")).strip()
    if not value or len(value) > maximum:
        raise fail(400, "err_required", field=key.replace("_", " "), max=maximum)
    return value


def session_value():
    """What the signed session cookie holds: a fingerprint of the password, so changing
    PHOTOGRAPHER_PASSWORD signs out every browser that signed in with the old one."""
    return hashlib.sha256(("session\n" + settings().photographer_password).encode()).hexdigest()[:32]


def signed_in(request):
    token = request.cookies.get("session")
    if not token:
        return False
    try:
        return unsign(token, "session", 43200) == session_value()
    except HTTPException:
        return False


def current_photographer(request: Request):
    """The photographer's profile when the browser is signed in, otherwise None.

    Read in a session of its own: a transaction holds SQLite's write lock (app/db.py), and the
    route's own session should not start one before the route does any work."""
    if not signed_in(request):
        return None
    with SessionLocal() as db:
        return profile(db)


def require_photographer(request: Request):
    """The dependency of every dashboard route: the signed-in photographer's profile, or a 401."""
    photographer = current_photographer(request)
    if not photographer:
        raise HTTPException(401)
    return photographer


def card_by_code(db, value):
    card = db.scalar(select(Card).where(Card.code == normalize(value)))
    if not card or card.status == "void":
        raise fail(404, "err_not_found")
    return card


def pdf_response(record, lang):
    # Rendered on demand: the consent row and its snapshot are the record, the PDF is a view of it.
    return Response(
        consent_pdf(record, lang),
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(f"decision-{record.id[:8]}.pdf")},
    )
