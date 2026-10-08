"""Cards and batches: printing, virtual cards, attaching and delivering photos."""

import io
import re

import segno
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app import storage
from app.config import settings
from app.db import get_db
from app.i18n import fail
from app.models import (
    Batch,
    Card,
    CardPhoto,
    Consent,
    Event,
    PhotoSet,
    now,
)
from app.pdfs import batch_pdf
from app.security import code
from app.services import (
    CARD_TEXT_MAX,
    asks_consent,
    delete_card_photos,
    event,
    found,
    links,
    marker_frames,
    photo_decision,
)
from app.storage import content_disposition
from app.timeline import describe
from app.web import form_data, go, render, require_photographer, required

router = APIRouter()


def unused_card_conditions():
    return (
        Card.photo_set_id.is_(None),
        Card.first_opened_at.is_(None),
        Card.delivered_at.is_(None),
        Card.status.in_(["waiting", "void"]),
        ~(select(CardPhoto.id).where(CardPhoto.card_id == Card.id).exists()),
        ~(select(Consent.id).where(Consent.card_id == Card.id).exists()),
        ~(select(Event.id).where(Event.card_id == Card.id).exists()),
    )


def opened_card_conditions():
    """Opened by the recipient before any photo was grouped or attached: people waiting on the photographer."""
    return (
        Card.photo_set_id.is_(None),
        Card.first_opened_at.is_not(None),
        Card.status == "waiting",
        ~(select(CardPhoto.id).where(CardPhoto.card_id == Card.id).exists()),
    )


def tab_conditions():
    unused = and_(*unused_card_conditions())
    opened = and_(*opened_card_conditions())
    return {
        "active": and_(~unused, ~opened),
        "opened": opened,
        "unused": unused,
    }


@router.get("/dashboard/cards")
def cards_page(request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    tab = request.query_params.get("tab", "active")
    tabs = tab_conditions()
    if tab not in tabs:
        raise fail(404, "err_not_found")
    counts = {name: db.scalar(select(func.count()).select_from(Card).where(where)) or 0 for name, where in tabs.items()}
    batches = list(db.scalars(select(Batch).order_by(Batch.created_at.desc())))
    cards, batch_unused, unused_one_offs = [], {}, 0
    if tab == "unused":
        batch_unused = {
            batch_id: int(count)
            for batch_id, count in db.execute(
                select(Card.batch_id, func.count())
                .where(tabs["unused"], Card.batch_id.is_not(None))
                .group_by(Card.batch_id)
            )
        }
        unused_one_offs = counts["unused"] - sum(batch_unused.values())
    else:
        cards = list(db.scalars(select(Card).where(tabs[tab]).order_by(Card.created_at.desc(), Card.sequence)))
    return render(
        request,
        "cards.html",
        photographer,
        tab=tab,
        counts=counts,
        cards=cards,
        batches=batches,
        batch_unused=batch_unused,
        card_text_max=CARD_TEXT_MAX,
        unused_one_offs=unused_one_offs,
        local=settings().is_local_url,
        purged=request.query_params.get("purged"),
    )


@router.post("/dashboard/cards")
async def create_card(request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    form = await form_data(request)
    set_id = str(form.get("set_id", ""))
    if set_id:
        found(db, PhotoSet, set_id)
    card = Card(
        code=code(),
        photo_set_id=set_id or None,
        notes=str(form.get("notes", ""))[:1000],
        handed_out_at=now(),
    )
    db.add(card)
    db.commit()
    return go("/dashboard/cards/" + card.id)


@router.post("/dashboard/batches")
async def create_batch(request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    form = await form_data(request)
    try:
        count = int(str(form.get("quantity", "10")))
    except ValueError:
        raise fail(400, "err_quantity") from None
    if not 1 <= count <= 100:
        raise fail(400, "err_quantity")
    batch = Batch(
        label=required(form, "label"),
        quantity=count,
        base_url=settings().public_base_url,
        card_text=str(form.get("card_text", "")).strip()[:CARD_TEXT_MAX],
    )
    db.add(batch)
    db.flush()
    for n in range(1, count + 1):
        db.add(Card(code=code(), batch_id=batch.id, sequence=n, kind="printed"))
    db.commit()
    return go("/dashboard/cards?tab=unused")


@router.post("/dashboard/cards/purge-unused")
async def purge_unused_cards(
    request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)
):
    await form_data(request)
    cards = list(
        db.scalars(select(Card).where(*unused_card_conditions()))
    )
    batch_ids = {card.batch_id for card in cards if card.batch_id}
    for card in cards:
        db.delete(card)
    db.flush()
    for batch_id in batch_ids:
        if not db.scalar(select(Card.id).where(Card.batch_id == batch_id).limit(1)):
            batch = db.get(Batch, batch_id)
            if batch:
                db.delete(batch)
    db.commit()
    return go(f"/dashboard/cards?tab=unused&purged={len(cards)}")


@router.get("/dashboard/batches/{identity}/pdf")
def batch_download(identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    preview = request.query_params.get("preview") == "1"
    batch = found(db, Batch, identity)
    if not preview:
        if settings().is_local_url:
            raise fail(409, "err_print")
        # The final sheet is handed out once. Reprinting it is the one realistic way to give the
        # same code to two people, so the batch remembers its download and refuses a second one.
        if batch.downloaded_at:
            raise fail(409, "err_batch_downloaded")
        batch.downloaded_at = now()
        db.commit()
    cards = list(db.scalars(select(Card).where(Card.batch_id == identity).order_by(Card.sequence)))
    logo = storage.get(storage.LOGO_KEY) if photographer.logo_updated_at else None
    return Response(
        batch_pdf(batch, cards, photographer, logo, preview),
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(pdf_filename(batch))},
    )


def pdf_filename(batch):
    """photocards-YYYYMMDD-<label>.pdf: the day the batch was generated, spaces in the label as dashes."""
    label = re.sub(r"[\s/\\]+", "-", batch.label.strip()).strip("-") or "batch"
    return f"photocards-{batch.created_at:%Y%m%d}-{label}.pdf"


@router.get("/dashboard/cards/{identity}")
def card_detail(identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    card = found(db, Card, identity)
    attached = links(db, card)
    sets = list(db.scalars(select(PhotoSet)))
    records = list(
        db.scalars(select(Consent).where(Consent.card_id == card.id).order_by(Consent.created_at.desc()))
    )
    events = list(db.scalars(select(Event).where(Event.card_id == card.id).order_by(Event.created_at.desc())))
    current = records[0] if records else None
    asks = asks_consent(db, card)
    return render(
        request,
        "card.html",
        photographer,
        card=card,
        attached=attached,
        deletable=card.status != "deleted" and any(link.delivered for link, _ in attached),
        preview={p.id: [(card, link, current, photo_decision(current, p, asks))] for link, p in attached},
        sets=sets,
        all_ready=bool(attached) and all(p.status == "ready" for _, p in attached),
        has_draft=any(not link.delivered for link, _ in attached),
        records=records,
        days=describe(db, events, request.state.lang),
    )


@router.get("/dashboard/cards/{identity}/qr")
def qr(identity: str, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    card = found(db, Card, identity)
    out = io.BytesIO()
    segno.make(settings().public_base_url + "/c/" + card.code, micro=False).save(out, kind="svg", scale=8)
    return Response(out.getvalue(), media_type="image/svg+xml")


@router.post("/dashboard/cards/{identity}/edit")
async def card_edit(identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    form = await form_data(request)
    card = found(db, Card, identity)
    card.notes = str(form.get("notes", ""))[:1000]
    set_id = str(form.get("set_id", ""))
    if set_id and set_id != card.photo_set_id:
        found(db, PhotoSet, set_id)
        if links(db, card) or marker_frames(db, card):
            raise fail(409, "err_card_move")
        card.photo_set_id = set_id
    db.commit()
    return go("/dashboard/cards/" + identity)


@router.post("/dashboard/cards/{identity}/deliver")
async def deliver(identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    form = await form_data(request)
    card = found(db, Card, identity)
    if card.status not in ("waiting", "delivered"):
        raise fail(409, "err_card_closed")
    attached = links(db, card)
    proposed = [(link, photo) for link, photo in attached if not link.delivered]
    if not proposed:
        return go("/dashboard/cards/" + identity)
    selected = set(form.getlist("photo_ids"))
    ready = {photo.id for _, photo in proposed if photo.status == "ready"}
    if not selected:
        raise fail(400, "err_select_deliver")
    if not selected.issubset(ready):
        raise fail(400, "err_ready_group")
    excluded = 0
    for link, photo in proposed:
        if photo.id in selected:
            link.delivered = True
        else:
            db.delete(link)
            excluded += 1
    card.status = "delivered"
    card.delivered_at = card.delivered_at or now()
    card.revision += 1
    event(db, "photo_group_confirmed", card.id, delivered=len(selected), excluded=excluded)
    db.commit()
    return go("/dashboard/cards/" + identity)


@router.post("/dashboard/cards/{identity}/close")
async def close_card(
    identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)
):
    form = await form_data(request)
    card = found(db, Card, identity)
    action = str(form.get("action"))
    if action not in ("void", "unavailable") or card.status == "deleted":
        raise fail(400, "err_action")
    if action == "unavailable" and card.delivered_at:
        raise fail(409, "err_delivered_close")
    if card.status == "void" or (action == "unavailable" and card.status != "waiting"):
        raise fail(409, "err_card_closed")
    if action == "unavailable":
        for link, _ in links(db, card):
            if not link.delivered:
                db.delete(link)
    card.status = action
    card.revision += 1
    event(db, "card_" + action, card.id)
    db.commit()
    if form.get("return_to") == "set" and card.photo_set_id:
        return go("/dashboard/sets/" + card.photo_set_id)
    return go("/dashboard/cards/" + identity)


@router.post("/dashboard/cards/{identity}/delete-photos")
async def delete_photos(
    identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)
):
    await form_data(request)
    delete_card_photos(db, found(db, Card, identity))
    db.commit()
    return go("/dashboard/cards/" + identity)
