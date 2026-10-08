import hashlib
import math
import re

from sqlalchemy import select

from app import links as profile_links
from app.i18n import fail
from app.matching import group_entries, natural_filename
from app.models import Card, CardPhoto, Consent, Event, Job, Photo, PhotoSet, Profile, Release, now

# The camera details the worker reads with exiftool, grouped by the tabs of the photographer's
# preview. The original file keeps them all (the recipient downloads it as it was uploaded); the
# display and thumbnail versions carry none. Cleared when a card's photos are deleted.
EXIF_TABS = {
    "camera": ["Make", "Model", "SerialNumber", "LensModel", "LensSerialNumber", "Software"],
    "exposure": [
        "ExposureTime",
        "FNumber",
        "ISO",
        "FocalLength",
        "FocalLengthIn35mmFormat",
        "ExposureCompensation",
        "ExposureProgram",
        "MeteringMode",
        "Flash",
        "WhiteBalance",
    ],
    "image": ["DateTimeOriginal", "OffsetTimeOriginal", "ImageSize", "Megapixels", "Orientation", "ColorSpace"],
    "location": ["GPSPosition", "GPSAltitude", "GPSImgDirection", "GPSDateTime"],
}


def exif_tabs(exif):
    """The preview's tabs that have at least one value, as (tab, [(tag, value), ...])."""
    tabs = [(tab, [(tag, exif[tag]) for tag in tags if exif.get(tag)]) for tab, tags in EXIF_TABS.items()]
    return [(tab, rows) for tab, rows in tabs if rows]


MAP_ZOOM = 15


def exif_map(exif):
    """The preview's small map of where the photo was taken: OpenStreetMap's 3×3 tiles around the
    spot, the spot's pixel position within them, and a link to the full map. None without GPS."""
    match = re.fullmatch(r"([\d.]+) ([NS]), ([\d.]+) ([EW])", exif.get("GPSPosition", ""))
    if not match:
        return None
    lat = float(match[1]) * (-1 if match[2] == "S" else 1)
    lon = float(match[3]) * (-1 if match[4] == "W" else 1)
    n = 2**MAP_ZOOM
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    left, top = math.floor(x) - 1, math.floor(y) - 1
    if not 0 <= top <= n - 3:  # within a tile of the poles, where the map ends
        return None
    return {
        "tiles": [
            f"https://tile.openstreetmap.org/{MAP_ZOOM}/{(left + col) % n}/{top + row}.png"
            for row in range(3)
            for col in range(3)
        ],
        "x": round((x - left) * 256),
        "y": round((y - top) * 256),
        "link": f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=17/{lat:.6f}/{lon:.6f}",
    }


# One text, in whatever languages the photographer wants side by side; the recipient reads it all.
DEFAULT_RELEASE_TEXT = (
    "I allow the photographer named in this record, and anyone they authorise, to use, publish, reproduce, adapt, sell and license the photograph versions listed in this record, in any medium and for any purpose, including commercial use, advertising, stock licensing and print sales, worldwide and without time limit, with no payment to me. Photos added to my card later are not covered until I decide on them. Downloading is free and does not require permission. My credit preference is recorded below. I confirm that I am the person pictured and I am 18 or older. I can change or withdraw permission using my card link. Withdrawal stops new use under this permission from that point on; it does not affect uses or licences granted before it, cannot recall printed copies and cannot guarantee removal of copies held by others. This record preserves the exact wording and image hashes I reviewed."
    + "\n\n"
    + "Bu kayıtta adı belirtilen fotoğrafçının ve onun yetkilendirdiği herkesin, bu kayıtta listelenen fotoğraf sürümlerini her ortamda ve her amaçla, ticari kullanım, reklam, stok lisanslama ve baskı satışı dahil, dünya çapında ve süre sınırı olmaksızın, bana herhangi bir ödeme yapılmaksızın kullanmasına, yayımlamasına, çoğaltmasına, uyarlamasına, satmasına ve lisanslamasına izin veriyorum. Kartıma sonradan eklenen fotoğraflar, onlar hakkında karar verene kadar bu izne dahil değildir. İndirme ücretsizdir ve izin vermeyi gerektirmez. Adımın belirtilmesiyle ilgili tercihim aşağıda kayıtlıdır. Fotoğraftaki kişi olduğumu ve 18 yaşında veya daha büyük olduğumu onaylıyorum. Kart bağlantımdan iznimi değiştirebilir veya geri çekebilirim. İznin geri çekilmesi, o andan itibaren bu izne dayanan yeni kullanımı durdurur; daha önce yapılmış kullanımları veya verilmiş lisansları etkilemez, basılmış kopyalar geri toplanamaz ve başkalarının elindeki kopyaların silinmesi garanti edilemez. Bu kayıt, incelediğim metni ve fotoğraf özetlerini saklar."
)

# The sentence printed beside the QR code on every card (app/pdfs.py fits four lines of it).
CARD_TEXT_MAX = 120


def found(db, model, identity):
    row = db.scalar(select(model).where(model.id == identity))
    if not row:
        raise fail(404, "err_not_found")
    return row


def profile(db):
    """The installation's one photographer, created at startup (app/main.py)."""
    row = db.scalar(select(Profile))
    if not row:
        raise fail(503, "err_starting")
    return row


def event(db, kind, card_id=None, actor="photographer", **payload):
    db.add(Event(card_id=card_id, type=kind, actor=actor, payload=payload))


def enqueue(db, kind, **payload):
    row = Job(type=kind, payload=payload)
    db.add(row)
    return row


def latest_release(db):
    return db.scalar(select(Release).order_by(Release.version.desc()))


def current_release(db):
    """The release text in force, created at startup with the profile (app/main.py)."""
    row = latest_release(db)
    if not row:
        raise fail(503, "err_starting")
    return row


def new_release(db, text):
    previous = latest_release(db)
    row = Release(
        version=previous.version + 1 if previous else 1,
        text=text,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    db.add(row)
    db.flush()
    return row


def decision_snapshot(photographer, release, photos, allowed, connection):
    """What the recipient was shown when deciding, kept on the consent row as the record: who asked,
    the exact release text, and every photo on the card with the hashes of both its versions."""
    return {
        "photographer": photographer.public_name,
        "accounts": profile_links.as_text(photographer.links),
        "release_version": release.version,
        "release_sha256": release.sha256,
        "release_text": release.text,
        "connection": connection,
        "photos": [
            {
                "id": p.id,
                "filename": p.filename,
                "display_sha256": p.display_sha256,
                "original_sha256": p.original_sha256,
                "allowed": allowed,
            }
            for p in photos
        ],
    }


def latest(db, card_id):
    return db.scalar(
        select(Consent)
        .where(Consent.card_id == card_id)
        .order_by(Consent.created_at.desc(), Consent.id.desc())
    )


def links(db, card, delivered=False):
    stmt = (
        select(CardPhoto, Photo)
        .join(Photo, CardPhoto.photo_id == Photo.id)
        .where(
            CardPhoto.card_id == card.id,
            Photo.status.not_in(["deleted", "marker"]),
        )
        .order_by(CardPhoto.created_at, CardPhoto.id)
    )
    if delivered:
        stmt = stmt.where(CardPhoto.delivered.is_(True), Photo.status == "ready")
    return list(db.execute(stmt).all())


def set_marker(db, photo, card):
    """Make a photo the QR marker frame of a card.

    A marker is hidden from every gallery and never proposed, so it can only be a photo nobody has
    been given: a delivered link is refused, undelivered proposals are dropped.
    """
    links_to_photo = list(db.scalars(select(CardPhoto).where(CardPhoto.photo_id == photo.id)))
    if any(link.delivered for link in links_to_photo):
        raise fail(409, "err_marker_delivered")
    for link in links_to_photo:
        db.delete(link)
    photo.status, photo.error, photo.marker_card_id = "marker", "", card.id


def attach_photo(db, photo, card):
    """Put a photo in a card's proposed group by hand, private until it is delivered.

    The link is the same undelivered proposal a QR scan makes. A photo is on one card at most
    (people photographed together share a card): a proposal to another card moves here, so a photo
    on the wrong card simply moves, and a photo someone has been given stays theirs.
    """
    if card.status not in ("waiting", "delivered"):
        raise fail(409, "err_card_closed")
    if photo.photo_set_id != card.photo_set_id or photo.status in ("deleted", "marker"):
        raise fail(400, "err_ready_set")
    current = db.scalar(select(CardPhoto).where(CardPhoto.photo_id == photo.id))
    if current and current.delivered:
        raise fail(409, "err_attach_delivered")
    if current and current.card_id == card.id:
        return
    if current:
        db.delete(current)
        db.flush()  # the photo's one link goes before the new one arrives
    db.add(CardPhoto(card_id=card.id, photo_id=photo.id))
    card.handed_out_at = card.handed_out_at or now()
    card.revision += 1
    event(db, "photo_attached_by_hand", card.id, photo_id=photo.id, filename=photo.filename)


def marker_frames(db, card):
    return list(db.scalars(select(Photo).where(Photo.marker_card_id == card.id, Photo.status == "marker")))


def regroup_upload(db, photo_set, upload_id, mode):
    """Recompute the QR groups of one upload from the marker frames in it.

    The upload's photos are read in filename order and split around its markers. For every card a
    marker names, the undelivered links to this upload's photos are replaced by the computed group.
    Delivered links, links from other uploads and cards without a marker here are left alone, and a
    photo already on another card stays there.
    Returns how many photos ended up in a group and how many outside every group.
    """
    rows = sorted(
        db.scalars(
            select(Photo)
            .where(
                Photo.photo_set_id == photo_set.id,
                Photo.upload_id == upload_id,
                Photo.status != "deleted",
            )
        ),
        key=lambda photo: natural_filename(photo.filename),
    )
    card_ids = {photo.marker_card_id for photo in rows if photo.marker_card_id}
    cards = {card.id: card for card in db.scalars(select(Card).where(Card.id.in_(card_ids)))} if card_ids else {}
    entries = []
    for photo in rows:
        if photo.status != "marker":
            entries.append(("photo", photo))
        elif photo.marker_card_id in cards:
            entries.append(("marker", cards[photo.marker_card_id]))
    groups, unassigned = group_entries(entries, mode)
    groups = {
        card_id: group
        for card_id, group in groups.items()
        if cards[card_id].status in ("waiting", "delivered")
        and cards[card_id].photo_set_id in (None, photo_set.id)
    }
    # A photo has one link at most (unique in card_photos), so this runs in two passes: first the
    # regrouped cards drop the proposals their group no longer holds, then the groups are filled.
    by_photo = {
        link.photo_id: link
        for link in db.scalars(select(CardPhoto).where(CardPhoto.photo_id.in_([photo.id for photo in rows])))
    }
    changed = set()
    for link in list(by_photo.values()):
        group = groups.get(link.card_id)
        if group is not None and not link.delivered and link.photo_id not in {photo.id for photo in group}:
            db.delete(link)
            del by_photo[link.photo_id]
            changed.add(link.card_id)
    db.flush()
    grouped = 0
    for card_id, group in groups.items():
        card = cards[card_id]
        card.photo_set_id = photo_set.id
        card.handed_out_at = card.handed_out_at or now()
        count = 0
        for photo in group:
            link = by_photo.get(photo.id)
            if link is None:
                db.add(CardPhoto(card_id=card.id, photo_id=photo.id))
                changed.add(card_id)
            elif link.card_id != card_id:
                continue  # given to someone else, or put on another card by hand: it stays there
            count += 1
        grouped += count
        if card_id in changed:
            card.revision += 1
            event(db, "photos_grouped_from_qr", card.id, count=count, mode=mode)
    return grouped, len(unassigned)


def asks_consent(db, card):
    """Whether the card's recipient is asked for permission: its photo set asks for it."""
    photo_set = db.get(PhotoSet, card.photo_set_id) if card.photo_set_id else None
    return bool(photo_set and photo_set.request_consent)


def covers(record, photo):
    """Whether a decision record is a standing grant for this exact version of the photo. A grant
    covers every photo the card showed when it was given; one added or reprocessed since is not."""
    return (
        record is not None
        and record.kind in ("grant", "change")
        and any(
            item["id"] == photo.id and item["allowed"] and item["display_sha256"] == photo.display_sha256
            for item in record.snapshot["photos"]
        )
    )


def photo_permission(db, photo):
    """Where a photo stands for publishing, from the decision on the one card it was given through."""
    if photo.status != "ready":
        return "review"
    row = db.execute(
        select(CardPhoto, Card).join(Card, Card.id == CardPhoto.card_id).where(CardPhoto.photo_id == photo.id)
    ).first()
    if not row or row.Card.status != "delivered" or not row.CardPhoto.delivered:
        return "review"
    record = latest(db, row.Card.id)
    if record and record.kind in ("decline", "withdrawal"):
        return "private"
    if covers(record, photo):
        return "ready"
    return "pending" if asks_consent(db, row.Card) else "not_asked"


def photo_decision(record, photo, asks):
    """How a card's latest decision applies to one of its photos: 'allowed', 'pending' (the card
    asks and no grant covers this photo yet), 'not_asked', 'decline' or 'withdrawal'."""
    if record and record.kind in ("decline", "withdrawal"):
        return record.kind
    if covers(record, photo):
        return "allowed"
    return "pending" if asks else "not_asked"


def delete_card_photos(db, card):
    """Erase the photos a card's recipient was given, usually because they asked for it.

    The rows stay as the record, so the same files are refused at upload and decision records keep
    naming them; the files and camera details go (worker `purge_photo`). Undelivered proposals were
    never the recipient's: they return to the set unassigned.
    """
    attached = links(db, card)
    delivered = [photo for link, photo in attached if link.delivered]
    if card.status == "deleted" or not delivered:
        raise fail(409, "err_nothing_to_delete")
    for link, _ in attached:
        if not link.delivered:
            db.delete(link)
    for photo in delivered:
        photo.status = "deleted"
        enqueue(db, "purge_photo", photo_id=photo.id)
    card.status = "deleted"
    card.revision += 1
    event(db, "photos_deleted", card.id, count=len(delivered))
