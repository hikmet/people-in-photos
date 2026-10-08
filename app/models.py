import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """An aware UTC datetime. SQLite keeps timestamps as text with no zone, so values are stored
    as naive UTC (which also keeps them comparable as text) and come back marked UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value is not None else None

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Row:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)


class Profile(Row, Base):
    """The photographer this installation belongs to: one row, created at startup (app/main.py)."""

    __tablename__ = "profile"
    public_name: Mapped[str] = mapped_column(String(100), default="Photographer")
    # Where people can find the photographer's work: {"website": url, "instagram": handle, ...},
    # one entry per filled field of app/links.py, shown as links beside the permission question.
    links: Mapped[dict] = mapped_column(JSON, default=dict)
    bio: Mapped[str] = mapped_column(Text, default="A moment on the street. A photograph for you.")
    # Printed on the right of every QR card, next to the QR code. The sentence is the default that
    # pre-fills a new batch (each batch keeps its own copy, see `Batch.card_text`); the logo is
    # stored at `storage.LOGO_KEY` as PNG and this timestamp says one is there.
    card_text: Mapped[str] = mapped_column(Text, default="")
    logo_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class PhotoSet(Row, Base):
    __tablename__ = "photo_sets"
    name: Mapped[str] = mapped_column(String(150))
    location: Mapped[str] = mapped_column(String(150), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    # Whether recipients of this set's cards are asked for permission to post their photos. Off,
    # the card page is a plain gift; whether consent is needed is the photographer's call, as it
    # depends on the country and the kind of photo.
    request_consent: Mapped[bool] = mapped_column(Boolean, default=False)


class Batch(Row, Base):
    __tablename__ = "card_batches"
    label: Mapped[str] = mapped_column(String(150))
    quantity: Mapped[int] = mapped_column(Integer)
    base_url: Mapped[str] = mapped_column(Text)
    card_text: Mapped[str] = mapped_column(Text, default="")
    downloaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Card(Row, Base):
    __tablename__ = "cards"
    code: Mapped[str] = mapped_column(String(16), unique=True)
    photo_set_id: Mapped[str | None] = mapped_column(ForeignKey("photo_sets.id"))
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("card_batches.id"))
    sequence: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default="virtual")
    status: Mapped[str] = mapped_column(String(20), default="waiting")
    notes: Mapped[str] = mapped_column(Text, default="")
    handed_out_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    first_opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revision: Mapped[int] = mapped_column(Integer, default=0)


class Photo(Row, Base):
    __tablename__ = "photos"
    photo_set_id: Mapped[str] = mapped_column(ForeignKey("photo_sets.id"))
    # The camera sequence a photo was last uploaded in, and the grouping mode chosen for it; QR
    # groups are recomputed within one upload when a marker is assigned by hand.
    upload_id: Mapped[str] = mapped_column(String(36), index=True)
    grouping_mode: Mapped[str] = mapped_column(String(20), default="qr_after")
    # Set on QR marker frames (status "marker"): the card this frame was photographed for.
    marker_card_id: Mapped[str | None] = mapped_column(ForeignKey("cards.id"))
    filename: Mapped[str] = mapped_column(String(200))
    # The uploaded bytes, stored untouched as the original everyone downloads.
    original_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    display_sha256: Mapped[str] = mapped_column(String(64), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[str] = mapped_column(String(40), default="")
    exif: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str] = mapped_column(Text, default="")


class CardPhoto(Row, Base):
    """A photo on a card: proposed (private) until delivered. A photo is on one card at most; people
    photographed together share one card."""

    __tablename__ = "card_photos"
    card_id: Mapped[str] = mapped_column(ForeignKey("cards.id"), index=True)
    photo_id: Mapped[str] = mapped_column(ForeignKey("photos.id"), unique=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)


class Release(Row, Base):
    __tablename__ = "releases"
    version: Mapped[int] = mapped_column(Integer, unique=True)
    # One text, in whatever languages the photographer writes side by side.
    text: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))


class Consent(Row, Base):
    __tablename__ = "consents"
    __table_args__ = (UniqueConstraint("supersedes_id"),)
    card_id: Mapped[str] = mapped_column(ForeignKey("cards.id"), index=True)
    release_id: Mapped[str] = mapped_column(ForeignKey("releases.id"))
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("consents.id"))
    kind: Mapped[str] = mapped_column(String(20))
    locale: Mapped[str] = mapped_column(String(2))
    signer_name: Mapped[str] = mapped_column(String(150), default="")
    signer_email: Mapped[str] = mapped_column(String(254), default="")
    attribution: Mapped[str] = mapped_column(String(20), default="anonymous")
    credit_name: Mapped[str] = mapped_column(String(150), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    snapshot: Mapped[dict] = mapped_column(JSON)


class Event(Row, Base):
    __tablename__ = "events"
    card_id: Mapped[str | None] = mapped_column(ForeignKey("cards.id"), index=True)
    type: Mapped[str] = mapped_column(String(60))
    actor: Mapped[str] = mapped_column(String(20), default="photographer")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class Job(Row, Base):
    __tablename__ = "jobs"
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str] = mapped_column(Text, default="")
