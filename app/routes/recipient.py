"""What the person photographed sees: the card page, downloads, the decision."""

import hashlib

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import links as profile_links
from app.db import get_db
from app.i18n import fail
from app.models import Consent, now
from app.security import ip, limiter, sign, unsign
from app.services import (
    asks_consent,
    covers,
    current_release,
    decision_snapshot,
    event,
    latest,
    links,
    profile,
)
from app.web import card_by_code, form_data, go, pdf_response, render, required

router = APIRouter()


def active_card(card):
    if card.status != "delivered":
        raise fail(409, "err_photos_unavailable")


@router.get("/c/{value}")
def gallery(value: str, request: Request, db: Session = Depends(get_db)):
    limiter.check(request, "cards", 180)
    card = card_by_code(db, value)
    photographer = profile(db)
    if not card.first_opened_at:
        card.first_opened_at = now()
        event(db, "link_opened", card.id, "subject")
    db.commit()
    attached = links(db, card, True) if card.status == "delivered" else []
    record = latest(db, card.id)
    asks = asks_consent(db, card)
    # The standing grant, if any, prefills the form; a decision covers every photo on the card.
    current = record if record and record.kind in ("grant", "change") else None
    # The question is open on arrival until answered, and again once a grant no longer covers every
    # photo (one was added or reprocessed since). A decline or withdrawal stands until changed.
    prompt = asks and (not record or bool(current and not all(covers(current, p) for _, p in attached)))
    release = current_release(db)
    token = sign(
        {
            "card": card.id,
            "revision": card.revision,
            "release": release.id,
            "previous": record.id if record else None,
            "profile": profile_hash(photographer),
        },
        "consent-form",
    )
    return render(
        request,
        "gallery.html",
        photographer,
        card=card,
        attached=attached,
        record=record,
        current=current,
        asks=asks,
        prompt=prompt,
        release=release,
        photographer_links=profile_links.shown(photographer.links),
        form_token=token,
        message=request.query_params.get("message", ""),
    )


def profile_hash(photographer):
    """What the consent form is bound to of the profile: a change of name or accounts rejects it."""
    text = photographer.public_name + "\n" + profile_links.as_text(photographer.links)
    return hashlib.sha256(text.encode()).hexdigest()


@router.post("/c/{value}/consent")
async def consent_submit(value: str, request: Request, db: Session = Depends(get_db)):
    limiter.check(request, "decisions", 30, 300)
    form = await form_data(request)
    card = card_by_code(db, value)
    active_card(card)
    previous = latest(db, card.id)
    # Only a card whose set asks for permission takes a first decision; one already recorded can
    # still be changed or withdrawn after the photographer stops asking.
    if not previous and not asks_consent(db, card):
        raise fail(409, "err_not_asked")
    context = unsign(str(form.get("form_token", "")), "consent-form")
    release = current_release(db)
    photographer = profile(db)
    if context != {
        "card": card.id,
        "revision": card.revision,
        "release": release.id,
        "previous": previous.id if previous else None,
        "profile": profile_hash(photographer),
    }:
        raise fail(409, "err_form_changed")
    action = str(form.get("action", ""))
    if action not in ("grant", "decline", "withdrawal"):
        raise fail(400, "err_decision")
    attached = links(db, card, True)
    if action == "grant" and (not attached or form.get("adult") != "on"):
        raise fail(400, "err_grant")
    if action == "withdrawal" and not previous:
        raise fail(409, "err_nothing_to_withdraw")
    attribution = str(form.get("attribution", "anonymous")) if action == "grant" else "anonymous"
    if attribution not in ("anonymous", "credit", "no_preference"):
        raise fail(400, "err_credit")
    lang = str(form.get("locale", ""))
    if lang not in ("en", "tr"):
        raise fail(400, "err_language")
    # Language is bound into the rendered release content by a separate signed token.
    language_token = unsign(str(form.get("language_token", "")), "release-language")
    if language_token != {"form": str(form.get("form_token")), "locale": lang}:
        raise fail(400, "err_language_form")
    record = Consent(
        card_id=card.id,
        release_id=release.id,
        supersedes_id=previous.id if previous else None,
        kind="change" if previous and action == "grant" else action,
        locale=lang,
        signer_name=required(form, "signer_name") if action == "grant" else "",
        signer_email=signer_email(form) if action == "grant" else "",
        attribution=attribution,
        credit_name=required(form, "credit_name") if attribution == "credit" else "",
        note=str(form.get("note", ""))[:2000],
        ip=ip(request),
        user_agent=request.headers.get("user-agent", "")[:300],
        snapshot=decision_snapshot(
            photographer,
            release,
            [p for _, p in attached],
            allowed=action == "grant",
            connection={
                "accept_language": request.headers.get("accept-language", "")[:300],
                "browser_brands": request.headers.get("sec-ch-ua", "")[:300],
                "platform": request.headers.get("sec-ch-ua-platform", "")[:100],
                "mobile": request.headers.get("sec-ch-ua-mobile", "")[:20],
            },
        ),
    )
    db.add(record)
    db.flush()
    event(db, "consent_" + record.kind, card.id, "subject", record_id=record.id)
    db.commit()
    return go("/c/" + card.code + "?message=saved")


def signer_email(form):
    value = str(form.get("signer_email", "")).strip()
    if not value:
        return ""
    try:
        return validate_email(value[:254], check_deliverability=False).normalized
    except EmailNotValidError:
        raise fail(400, "err_email") from None


@router.get("/c/{value}/records/{identity}.pdf")
def subject_pdf(value: str, identity: str, request: Request, db: Session = Depends(get_db)):
    card = card_by_code(db, value)
    record = db.scalar(select(Consent).where(Consent.id == identity, Consent.card_id == card.id))
    if not record:
        raise fail(404, "err_not_found")
    return pdf_response(record, request.state.lang)

