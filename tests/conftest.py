import io
import re
import uuid
from pathlib import Path

import pytest
import segno
from PIL import Image, ImageChops, ImageDraw, ImageFilter
from starlette.testclient import TestClient

from app.config import settings
from app.db import create_schema, engine
from app.main import app
from app.security import limiter


@pytest.fixture(autouse=True)
def reset_database():
    """A brand-new database file for every test, created from app/schema.sql."""
    path = Path(settings().database_path)
    assert path.is_relative_to("/tmp"), "Refusing to delete a database outside /tmp"
    assert settings().storage_backend == "local", "Refusing to run against a remote storage bucket"
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    create_schema()
    limiter.buckets.clear()


@pytest.fixture
def client():
    with TestClient(app, base_url="http://localhost:8010") as client:
        yield client


@pytest.fixture
def photographer(client):
    client.get("/login")
    result = client.post(
        "/login", data={"csrf": client.cookies["csrf"], "password": settings().photographer_password}
    )
    assert result.status_code == 200, result.text
    return client


def fields(response):
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', response.text))


def post(client, route, **data):
    return client.post(route, data={"csrf": client.cookies.get("csrf", ""), **data})


def jpeg(color="navy", metadata=False):
    image = Image.new("RGB", (800, 1000), color)
    exif = Image.Exif()
    if metadata:
        exif[315] = "Private owner"
        exif[274] = 6
        exif[270] = "PRIVATE LOCATION"
        exif[271], exif[272] = "Fujifilm", "X100V"
        exif.get_ifd(0x8769).update({33434: 1 / 250, 33437: 2.8, 34855: 400, 42033: "SERIAL-1234"})
        exif.get_ifd(0x8825).update({1: "N", 2: (41.0, 1.0, 30.0), 3: "E", 4: (28.0, 58.0, 30.0)})
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif, comment=b"SECRET COMMENT")
    return out.getvalue()


def mpo():
    primary = Image.new("RGB", (800, 1000), "navy")
    auxiliary = Image.new("RGB", (160, 120), "red")
    out = io.BytesIO()
    primary.save(out, "MPO", save_all=True, append_images=[auxiliary])
    return out.getvalue()


def qr_jpeg(value):
    png = io.BytesIO()
    segno.make(value, micro=False).save(png, kind="png", scale=12, border=4)
    png.seek(0)
    with Image.open(png) as qr:
        image = Image.new("RGB", (1000, 1000), "white")
        code_image = qr.convert("RGB")
        offset = ((image.width - code_image.width) // 2, (image.height - code_image.height) // 2)
        image.paste(code_image, offset)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=95)
    return out.getvalue()


def screen_qr_jpeg(value):
    """A 12-megapixel photograph of a QR code shown on a screen.

    The LCD pixel grid (a dark line every third pixel, softened by the camera's optics) breaks the
    QR modules apart at full resolution, which is what zbar sees when a photographer photographs
    a card on a phone or monitor instead of a print. The code reads fine once the image is shrunk.
    """
    png = io.BytesIO()
    segno.make(value, micro=False).save(png, kind="png", scale=1, border=0)
    png.seek(0)
    with Image.open(png) as qr:
        code_image = qr.convert("L").resize((qr.width * 14, qr.height * 14), Image.NEAREST)
    canvas = Image.new("L", (4032, 2268), 235)
    canvas.paste(code_image, ((canvas.width - code_image.width) // 2, (canvas.height - code_image.height) // 2))
    grid = Image.new("L", canvas.size, 255)
    draw = ImageDraw.Draw(grid)
    for x in range(0, canvas.width, 3):
        draw.line([(x, 0), (x, canvas.height)], fill=60)
    for y in range(0, canvas.height, 3):
        draw.line([(0, y), (canvas.width, y)], fill=60)
    image = ImageChops.multiply(canvas, grid).filter(ImageFilter.GaussianBlur(0.7))
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=85)
    return out.getvalue()


def setup_gallery(client, deliver=True, ask=True):
    from app.db import SessionLocal
    from app.models import Card, Photo
    from app.worker import run_once

    extra = {"request_consent": "on"} if ask else {}
    result = post(client, "/dashboard/sets", name="Test walk " + str(uuid.uuid4())[:6], location="Istanbul", **extra)
    assert result.status_code == 200, result.text
    set_id = str(result.url).rsplit("/", 1)[1]
    result = client.post(
        "/dashboard/sets/" + set_id + "/upload",
        data={"csrf": client.cookies["csrf"]},
        files={"photos": ("portrait.jpg", jpeg(metadata=True), "image/jpeg")},
    )
    assert result.status_code == 200, result.text
    assert run_once()
    with SessionLocal() as db:
        from sqlalchemy import select

        photo = db.scalar(select(Photo).where(Photo.photo_set_id == set_id))
        assert photo.status == "ready", photo.error
        photo_id = photo.id
    result = post(client, "/dashboard/cards", set_id=set_id, notes="Red umbrella")
    assert result.status_code == 200, result.text
    card_id = str(result.url).rsplit("/", 1)[1]
    with SessionLocal() as db:
        card_code = db.get(Card, card_id).code
    result = post(client, f"/dashboard/sets/{set_id}/attach", photo_id=photo_id, card_id=card_id)
    assert result.status_code == 200, result.text
    if deliver:
        assert post(client, f"/dashboard/cards/{card_id}/deliver", photo_ids=photo_id).status_code == 200
    return set_id, card_id, card_code, photo_id


def decide(client, code, action="grant", lang="en", **kwargs):
    response = client.get(f"/{lang}/c/{code}")
    data = fields(response)
    data.update(action=action, signer_name="Çağrı Şahin", adult="on", **kwargs)
    return client.post("/c/" + code + "/consent", data=data)
