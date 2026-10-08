"""The activity log: one sentence per event, naming the card, set, photo, and decision behind it."""

from itertools import groupby

from sqlalchemy import select

from app.i18n import CATALOGS
from app.models import Card, Consent, Photo, PhotoSet, now

APPROVALS = ("consent_grant", "consent_change")
DENIALS = ("consent_decline", "consent_withdrawal")


def describe(db, events, lang):
    """Rows for the log templates, grouped by day (newest first): each row carries the sentence,
    where clicking it leads, the tint, and the card and set the event belongs to."""
    text = CATALOGS[lang]
    cards = lookup(db, Card, {e.card_id for e in events})
    sets = lookup(
        db,
        PhotoSet,
        {c.photo_set_id for c in cards.values()} | {e.payload.get("photo_set_id") for e in events},
    )
    consents = lookup(db, Consent, {e.payload.get("record_id") for e in events})
    photos = lookup(
        db, Photo, {e.payload.get("photo_id") for e in events if e.type == "photo_downloaded"}
    )
    today = now().date()
    rows = []
    for e in events:
        card = cards.get(e.card_id)
        record = consents.get(e.payload.get("record_id"))
        photo_set = sets.get(card.photo_set_id if card else e.payload.get("photo_set_id"))
        rows.append(
            {
                "event": e,
                "line": sentence(text, e, record, photos.get(e.payload.get("photo_id")), photo_set),
                "href": target(e, record),
                "tone": "approval" if e.type in APPROVALS else "denial" if e.type in DENIALS else "",
                "card": card,
                "set": photo_set,
                "day": day_label(text, e.created_at.date(), today),
                "time": e.created_at.strftime("%H:%M"),
            }
        )
    return [(label, list(group)) for label, group in groupby(rows, key=lambda r: r["day"])]


def lookup(db, model, ids):
    ids.discard(None)
    if not ids:
        return {}
    rows = db.scalars(select(model).where(model.id.in_(ids)))
    return {row.id: row for row in rows}


def day_label(text, day, today):
    if day == today:
        return text["log_today"]
    if (today - day).days == 1:
        return text["log_yesterday"]
    return day.strftime("%d %b %Y")


def target(e, record):
    if record:
        return f"/dashboard/consents/{record.id}"
    if e.card_id:
        return f"/dashboard/cards/{e.card_id}"
    if e.payload.get("photo_set_id"):
        return f"/dashboard/sets/{e.payload['photo_set_id']}"
    return None


def sentence(text, e, record, photo, photo_set):
    kind, p = e.type, e.payload
    key = "ev_" + kind
    values = {}
    extras = []
    if kind.startswith("consent_"):
        values = {"name": record.signer_name or text["log_subject"], "total": len(record.snapshot["photos"])}
        if kind in APPROVALS:
            extras.append(text["log_attr_" + record.attribution].format(credit=record.credit_name))
    elif kind == "photo_downloaded":
        values = {"filename": photo.filename if photo else text["log_a_photo"]}
    elif kind in ("marker_assigned", "photo_attached_by_hand"):
        values = {"filename": p.get("filename", "")}
    elif kind == "photo_group_confirmed":
        values = {"delivered": p.get("delivered", 0)}
        if p.get("excluded"):
            extras.append(text["log_left_out"].format(excluded=p["excluded"]))
    elif kind == "photos_deleted":
        values = {"count": p["count"]}
    elif kind in ("photos_grouped_from_qr", "unassigned_photos_deleted"):
        values = {"count": p.get("count", 0), "set": photo_set.name if photo_set else ""}
    if values.get("count", values.get("delivered", values.get("total"))) == 1 and key + "_one" in text:
        key += "_one"
    template = text.get(key) or kind.replace("_", " ").capitalize()
    return " · ".join([template.format(**values), *extras])
