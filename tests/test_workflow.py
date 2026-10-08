import hashlib
import io
import logging
import re
import subprocess

import pytest
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from starlette.testclient import TestClient

from app import storage
from app.config import settings
from app.db import SessionLocal
from app.i18n import CATALOGS
from app.main import app
from app.matching import read_codes, scan_card_codes
from app.models import (
    Batch,
    Card,
    CardPhoto,
    Consent,
    Event,
    Job,
    Photo,
    PhotoSet,
    Profile,
    Release,
)
from app.security import code, normalize, sign
from app.services import latest, links, new_release, photo_permission
from app.worker import run_once
from tests.conftest import decide, fields, jpeg, mpo, post, qr_jpeg, screen_qr_jpeg, setup_gallery


def test_login_and_return_visits_open_photographer_workspace(photographer):
    for route in ("/", "/login"):
        response = photographer.get(route)
        assert response.url.path == "/en/dashboard"
        assert '<aside class="sidebar">' in response.text
        assert "New photo set" in response.text
        assert "Your card code" not in response.text

    assert post(photographer, "/logout").url.path == "/en/login"
    response = photographer.get("/")
    assert response.url.path == "/en/"
    assert "Your card code" in response.text
    assert 'class="text-link" href="/en/login"' in response.text
    assert photographer.get("/dashboard").url.path == "/en/login"


def test_changing_the_password_signs_every_browser_out(photographer, monkeypatch):
    assert photographer.get("/dashboard").url.path == "/en/dashboard"
    monkeypatch.setattr(settings(), "photographer_password", "a-new-password-entirely")
    assert photographer.get("/dashboard").url.path == "/en/login"


@pytest.mark.parametrize("session", ["invalid", sign("not-the-password-fingerprint", "session")])
def test_unusable_session_can_sign_in_again(client, session):
    client.cookies.set("session", session)
    assert "Your card code" in client.get("/").text
    assert 'name="password"' in client.get("/login").text
    assert client.get("/dashboard").url.path == "/en/login"


def test_complete_bilingual_loop_and_immutable_record(photographer, tmp_path):
    client = photographer
    _, card_id, card_code, photo_id = setup_gallery(client)
    gallery = client.get("/c/" + card_code)
    assert gallery.status_code == 200
    assert "noindex" in gallery.headers["x-robots-tag"]
    assert "no-store" in gallery.headers["cache-control"]
    download = re.search(r'href="([^"]+\?download=1)"', gallery.text).group(1)
    client.cookies.delete("session")
    response = client.get(download)
    assert response.status_code == 200  # unconditional and no photographer auth
    assert response.headers["content-disposition"].startswith('attachment; filename="people-photograph-')
    assert decide(client, card_code, lang="tr").status_code == 200
    with SessionLocal() as db:
        record = latest(db, card_id)
        assert record.locale == "tr" and record.signer_name == "Çağrı Şahin"
        assert record.snapshot["photos"][0]["allowed"]
        record_id = record.id
    pdf = client.get(f"/c/{card_code}/records/{record_id}.pdf")
    assert pdf.status_code == 200
    (tmp_path / "decision-tr.pdf").write_bytes(pdf.content)  # keeps the bytes inspectable on failure
    pages = PdfReader(io.BytesIO(pdf.content)).pages
    pdf_text = "\n".join(page.extract_text() for page in pages)
    assert "Çağrı Şahin" in pdf_text and "ticari kullanım" in " ".join(pdf_text.split())
    assert sum(len(page.images) for page in pages) == 1  # the decided photograph's thumbnail
    # Labelled in one language: the recipient's copy in the language they read the page in.
    assert "İzin verildi" in pdf_text and "Karar kaydı" in pdf_text
    assert "Consent granted" not in pdf_text and "Record ID" not in pdf_text
    english = PdfReader(io.BytesIO(client.get(f"/en/c/{card_code}/records/{record_id}.pdf").content)).pages
    english_text = "\n".join(page.extract_text() for page in english)
    assert "Consent granted" in english_text and "Record ID" in english_text and "Karar kaydı" not in english_text
    assert decide(client, card_code, "withdrawal", lang="en").status_code == 200
    with SessionLocal() as db:
        current = latest(db, card_id)
        assert current.kind == "withdrawal" and current.supersedes_id == record_id
        assert current.snapshot["photos"][0]["allowed"] is False
        assert photo_permission(db, db.get(Photo, photo_id)) == "private"
    assert client.get(download).status_code == 200  # withdrawal doesn't remove the gift


def test_activity_log_lines_name_the_card_decision_and_photos(photographer):
    client = photographer
    set_id, card_id, card_code, photo_id = setup_gallery(client)
    client.get("/c/" + card_code)
    assert decide(client, card_code, attribution="credit", credit_name="Ç. Şahin").status_code == 200
    with SessionLocal() as db:
        record_id, set_name = latest(db, card_id).id, db.get(PhotoSet, set_id).name
    page = client.get("/dashboard/inbox").text
    assert "Çağrı Şahin allowed the only photo · credit as “Ç. Şahin”" in page
    assert f'href="/en/dashboard/consents/{record_id}"' in page
    assert "Card opened" in page and "1 photo assigned" in page and "added to the card" in page
    assert format_code(card_code) in page and set_name in page
    assert "View card" not in page and "photographer ·" not in page
    assert client.get("/tr/dashboard/inbox").text.count("Çağrı Şahin tek fotoğrafa izin verdi") == 1
    card_page = client.get(f"/en/dashboard/cards/{card_id}").text
    assert "Çağrı Şahin allowed the only photo" in card_page and "Card opened" in card_page
    assert decide(client, card_code, "withdrawal").status_code == 200
    assert 'class="log-row denial"' in client.get("/dashboard/inbox").text


def test_decline_requires_no_name_or_photo_selection(photographer):
    _, card_id, card_code, _ = setup_gallery(photographer)
    response = photographer.get("/c/" + card_code)
    data = fields(response)
    data["action"] = "decline"
    connection_headers = {
        "user-agent": "Record Browser/1.0",
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Record Browser";v="1"',
        "sec-ch-ua-platform": '"Linux"',
        "sec-ch-ua-mobile": "?0",
    }
    assert (
        photographer.post(
            "/c/" + card_code + "/consent", data=data, headers=connection_headers
        ).status_code
        == 200
    )
    with SessionLocal() as db:
        record = latest(db, card_id)
        assert record.kind == "decline" and record.signer_name == ""
        record_id = record.id
        recorded_ip = record.ip
        assert record.snapshot["connection"] == {
            "accept_language": "en-US,en;q=0.9",
            "browser_brands": '"Record Browser";v="1"',
            "platform": '"Linux"',
            "mobile": "?0",
        }
    history = photographer.get(f"/dashboard/cards/{card_id}")
    assert "Consent denied" in history.text
    assert f'href="/en/dashboard/consents/{record_id}"' in history.text
    details = photographer.get(f"/dashboard/consents/{record_id}")
    assert details.status_code == 200
    for value in (
        "Consent denied",
        "Record Browser/1.0",
        "en-US,en;q=0.9",
        "Browser brands",
        "Mobile client hint",
        recorded_ip,
        "Not supplied (a name is not requested for denial or withdrawal)",
    ):
        assert value in details.text
    pdf = photographer.get(f"/dashboard/consents/{record_id}/pdf")
    pdf_text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "Consent denied" in pdf_text and "İzin verilmedi" not in pdf_text
    # The photographer's copy follows the language they use the dashboard in.
    turkish = photographer.get(f"/tr/dashboard/consents/{record_id}/pdf")
    turkish_text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(turkish.content)).pages)
    assert "İzin verilmedi" in turkish_text and "Karar kaydı" in turkish_text and "Consent denied" not in turkish_text
    assert "Record Browser/1.0" in pdf_text
    assert "en-US,en;q=0.9" in pdf_text
    assert ">Decline<" not in history.text


def test_stale_and_duplicate_forms_cannot_overwrite(photographer):
    _, card_id, card_code, photo_id = setup_gallery(photographer)
    data = fields(photographer.get("/c/" + card_code))
    data.update(action="grant", signer_name="Test Person", adult="on")
    assert photographer.post("/c/" + card_code + "/consent", data=data).status_code == 200
    assert photographer.post("/c/" + card_code + "/consent", data=data).status_code == 409
    with SessionLocal() as db:
        assert len(list(db.scalars(select(Consent).where(Consent.card_id == card_id)))) == 1


def test_changed_release_requires_reload_and_preserves_old_record(photographer):
    _, card_id, card_code, photo_id = setup_gallery(photographer)
    assert decide(photographer, card_code).status_code == 200
    stale = fields(photographer.get("/c/" + card_code))
    with SessionLocal.begin() as db:
        record = latest(db, card_id)
        old_text = record.snapshot["release_text"]
        new_release(db, "Updated social text")
    stale.update(action="decline")
    assert photographer.post("/c/" + card_code + "/consent", data=stale).status_code == 409
    with SessionLocal() as db:
        assert latest(db, card_id).snapshot["release_text"] == old_text


def test_draft_photos_and_originals_cannot_be_reached_by_subject(photographer):
    _, card_id, card_code, photo_id = setup_gallery(photographer, False)
    response = photographer.get("/c/" + card_code)
    assert "/media/" not in response.text
    with SessionLocal() as db:
        p = db.get(Photo, photo_id)
        token = sign({"photo": p.id, "card": card_id, "variant": "display"}, "media")
        original = sign({"photo": p.id, "card": card_id, "variant": "original"}, "media")
    assert photographer.get("/media/" + token).status_code == 404
    assert photographer.get("/media/" + original).status_code == 404
    assert post(photographer, f"/dashboard/cards/{card_id}/deliver", photo_ids=photo_id).status_code == 200
    assert photographer.get("/media/" + original).status_code == 200
    assert photographer.get("/media/" + token).status_code == 200
    assert post(photographer, f"/dashboard/cards/{card_id}/close", action="void").status_code == 200
    assert photographer.get("/media/" + token).status_code == 404


def test_photographer_deletes_a_cards_photos(photographer):
    _, card_id, card_code, photo_id = setup_gallery(photographer)
    assert decide(photographer, card_code).status_code == 200
    gallery = photographer.get("/c/" + card_code)
    download = re.search(r'href="([^"]+\?download=1)"', gallery.text).group(1)

    assert post(photographer, f"/dashboard/cards/{card_id}/delete-photos").status_code == 200
    assert photographer.get(download).status_code == 404
    gallery = photographer.get("/c/" + card_code).text
    assert "These photos have been deleted." in gallery and "/records/" in gallery
    for _ in range(10):
        if not run_once():
            break
    with SessionLocal() as db:
        photo = db.get(Photo, photo_id)
        assert photo.status == "deleted" and photo.exif == {}
        assert not any(storage.exists(storage.photo_key(photo.id, v)) for v in storage.VARIANTS)
        assert db.get(Card, card_id).status == "deleted"
        assert latest(db, card_id) is not None
        assert db.scalar(select(Event).where(Event.type == "photos_deleted")).payload == {"count": 1}
    assert "The photos on this card were deleted" in photographer.get(f"/dashboard/cards/{card_id}").text
    assert "1 photo deleted" in photographer.get("/dashboard/inbox").text
    assert post(photographer, f"/dashboard/cards/{card_id}/delete-photos").status_code == 409


def test_database_blocks_rewriting_evidence(photographer):
    _, _, card_code, photo_id = setup_gallery(photographer)
    decide(photographer, card_code)
    for table in ("events", "releases", "consents"):
        with SessionLocal() as db:
            with pytest.raises(DBAPIError):
                db.execute(text(f"UPDATE {table} SET created_at = CURRENT_TIMESTAMP"))
            db.rollback()
            with pytest.raises(DBAPIError):
                db.execute(text(f"DELETE FROM {table}"))


def test_csrf_and_sign_in(photographer):
    assert photographer.post("/dashboard/sets", data={"name": "Bad"}).status_code == 403
    assert (
        photographer.post(
            "/dashboard/sets",
            data={"name": "Bad", "csrf": photographer.cookies["csrf"]},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    local_alias = TestClient(app, base_url="http://127.0.0.1:8010")
    with local_alias:
        local_alias.get("/login")
        response = local_alias.post(
            "/login",
            data={"csrf": local_alias.cookies["csrf"], "password": settings().photographer_password},
            headers={"Origin": "http://127.0.0.1:8010"},
        )
        assert response.url.path == "/en/dashboard"
    rewritten_proxy = TestClient(app, base_url="http://internal-app:8000")
    with rewritten_proxy:
        rewritten_proxy.get("/login")
        response = rewritten_proxy.post(
            "/login",
            data={"csrf": rewritten_proxy.cookies["csrf"], "password": settings().photographer_password},
            headers={
                "Origin": "http://localhost:8010",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        assert response.url.path == "/en/dashboard"
    photographer.get("/login")
    assert (
        photographer.post(
            "/login",
            data={"csrf": photographer.cookies["csrf"], "password": settings().photographer_password},
            headers={
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
            },
        ).status_code
        == 403
    )
    photographer.cookies.delete("session")
    assert photographer.get("/en/dashboard", follow_redirects=False).status_code == 303


def test_original_is_kept_untouched_and_display_is_stripped(photographer, tmp_path):
    _, _, _, photo_id = setup_gallery(photographer)
    with SessionLocal() as db:
        p = db.get(Photo, photo_id)
        data = storage.get(storage.photo_key(p.id, "original"))
        # The original is the uploaded file, byte for byte, metadata and all.
        assert data == jpeg(metadata=True)
        assert hashlib.sha256(data).hexdigest() == p.original_sha256
        assert b"PRIVATE LOCATION" in data and b"SECRET COMMENT" in data
        display_data = storage.get(storage.photo_key(p.id, "display"))
        display = tmp_path / "display.jpg"
        display.write_bytes(display_data)
        assert b"PRIVATE LOCATION" not in display_data and b"SECRET COMMENT" not in display_data
        assert b"Private owner" not in display_data
        with Image.open(io.BytesIO(display_data)) as image:
            assert image.size == (1000, 800)
            assert image.getexif().get(274) is None
            assert image.getexif().get(315) == "Photographer"
        metadata = subprocess.run(
            ["exiftool", "-GPS:all", "-XMP:all", "-IPTC:all", "-SerialNumber", str(display)],
            capture_output=True,
            check=True,
        )
        assert not metadata.stdout
        assert p.exif["Make"] == "Fujifilm" and p.exif["SerialNumber"] == "SERIAL-1234"
        assert p.exif["GPSPosition"] == "41.025000 N, 28.975000 E"
    preview = photographer.get(f"/dashboard/sets/{p.photo_set_id}").text
    assert "Shutter speed" in preview and "1/250" in preview and "41.025000 N, 28.975000 E" in preview
    assert "https://www.openstreetmap.org/?mlat=41.025000&amp;mlon=28.975000" in preview


def test_batch_preview_and_final_print_guard(photographer):
    result = post(photographer, "/dashboard/batches", label="Test batch", quantity="12")
    assert result.url.path == "/en/dashboard/cards" and result.url.params["tab"] == "unused"
    assert "Test batch" in result.text
    with SessionLocal() as db:
        batch = db.scalar(select(Batch))
        identity = batch.id
        cards = list(db.scalars(select(Card).order_by(Card.sequence)))
        assert len(cards) == 12 and len({c.code for c in cards}) == 12
    assert photographer.get(f"/dashboard/batches/{identity}/pdf").status_code == 409
    pdf = photographer.get(f"/dashboard/batches/{identity}/pdf?preview=1")
    reader = PdfReader(io.BytesIO(pdf.content))
    assert len(reader.pages) == 2
    assert "LOCAL PREVIEW" in reader.pages[0].extract_text()
    assert "Assign a range to a set" not in photographer.get("/dashboard/cards").text


def format_code(value):
    return "-".join(value[i : i + 4] for i in range(0, 16, 4))


def test_batch_final_pdf_downloads_once(photographer, monkeypatch):
    post(photographer, "/dashboard/batches", label="Street batch", quantity="3")
    with SessionLocal() as db:
        batch_id = db.scalar(select(Batch)).id
    monkeypatch.setattr(settings(), "public_base_url", "https://cards.example.com")
    assert "Download final PDF" in photographer.get("/dashboard/cards?tab=unused").text
    first = photographer.get(f"/dashboard/batches/{batch_id}/pdf")
    assert first.status_code == 200 and first.headers["content-type"] == "application/pdf"
    assert re.fullmatch(
        r'attachment; filename="photocards-\d{8}-Street-batch.pdf"',
        first.headers["content-disposition"],
    )
    assert "LOCAL PREVIEW" not in PdfReader(io.BytesIO(first.content)).pages[0].extract_text()
    assert photographer.get(f"/dashboard/batches/{batch_id}/pdf").status_code == 409
    assert photographer.get(f"/dashboard/batches/{batch_id}/pdf?preview=1").status_code == 200
    page = photographer.get("/dashboard/cards?tab=unused").text
    assert "Final PDF downloaded" in page and "Download final PDF" not in page
    with SessionLocal() as db:
        assert db.get(Batch, batch_id).downloaded_at is not None


def test_unused_qr_codes_are_permanently_purged_and_reusable(photographer):
    post(photographer, "/dashboard/batches", label="Unused batch", quantity="2")
    unused_result = post(photographer, "/dashboard/cards")
    unused_one_off_id = str(unused_result.url).rsplit("/", 1)[1]

    opened_result = post(photographer, "/dashboard/cards")
    opened_id = str(opened_result.url).rsplit("/", 1)[1]
    set_result = post(photographer, "/dashboard/sets", name="Detected card set")
    set_id = str(set_result.url).rsplit("/", 1)[1]
    detected_result = post(photographer, "/dashboard/cards", set_id=set_id)
    detected_id = str(detected_result.url).rsplit("/", 1)[1]

    with SessionLocal() as db:
        batch = db.scalar(select(Batch).where(Batch.label == "Unused batch"))
        batch_id = batch.id
        batch_card_ids = list(db.scalars(select(Card.id).where(Card.batch_id == batch_id)))
        unused_code = db.get(Card, unused_one_off_id).code
        opened_code = db.get(Card, opened_id).code

    assert photographer.get("/c/" + opened_code).status_code == 200
    page = photographer.get("/dashboard/cards")
    assert page.url.path == "/en/dashboard/cards"
    assert re.findall(r'<span class="tab-count">(\d+)</span>', page.text) == ["1", "1", "3"]
    assert format_code(opened_code) not in page.text
    opened = photographer.get("/dashboard/cards?tab=opened").text
    assert format_code(opened_code) in opened and format_code(unused_code) not in opened
    assert "These people are waiting on you." in opened
    unused = photographer.get("/dashboard/cards?tab=unused").text
    assert "Unused batch" in unused and "2 of 2 unused" in unused and "1 unused one-off cards." in unused
    assert format_code(unused_code) not in unused and "Card #" not in unused
    assert "3 generated codes have never been detected, opened, or used to assign photos." in unused
    assert "Purge unused QR codes" in unused
    assert "only the preview is available here" in unused
    assert photographer.get("/dashboard/cards?tab=bogus").status_code == 404

    response = post(photographer, "/dashboard/cards/purge-unused")
    assert response.status_code == 200
    assert response.url.query == b"tab=unused&purged=3"
    with SessionLocal() as db:
        assert db.get(Card, unused_one_off_id) is None
        assert all(db.get(Card, identity) is None for identity in batch_card_ids)
        assert db.get(Batch, batch_id) is None
        assert db.get(Card, opened_id) is not None
        assert db.get(Card, detected_id) is not None
        replacement = Card(code=unused_code)
        db.add(replacement)
        db.commit()
        assert db.get(Card, replacement.id).code == unused_code


def test_consent_email_is_optional_and_validated(photographer):
    _, card_id, card_code, photo_id = setup_gallery(photographer)
    assert decide(photographer, card_code, signer_email="not an address").status_code == 400
    assert decide(photographer, card_code, signer_email=" Person@Example.com ").status_code == 200
    with SessionLocal() as db:
        record = latest(db, card_id)
        assert record.signer_email == "Person@example.com"
    assert 'value="Person@example.com"' in photographer.get("/c/" + card_code).text
    assert "Person@example.com" in photographer.get(f"/dashboard/consents/{record.id}").text
    assert decide(photographer, card_code, action="decline").status_code == 200
    with SessionLocal() as db:
        assert latest(db, card_id).signer_email == ""


def test_i18n_code_normalization_and_no_opt_in_defaults(client):
    assert CATALOGS["en"].keys() == CATALOGS["tr"].keys()
    assert normalize("7k3m 9x2b-o4hd-zp8r") == "7K3M9X2B04HDZP8R"
    assert len(set(code() for _ in range(1000))) == 1000
    assert "Fotoğraflarınız" in client.get("/", headers={"Accept-Language": "de;q=1,tr;q=0.9,en;q=0.5"}).text


def test_new_photo_reopens_the_question_and_inherits_no_consent(photographer):
    set_id, card_id, card_code, photo_id = setup_gallery(photographer)
    # The question is open on arrival, with nothing prefilled.
    gallery = photographer.get("/c/" + card_code).text
    assert '/consent"' in gallery and 'class="decision-change"' not in gallery
    assert decide(photographer, card_code, attribution="credit", credit_name="Ç. Ş.").status_code == 200
    gallery = photographer.get("/c/" + card_code).text
    # Answered: the decision is summarised and the form folds away, prefilled for a change.
    assert 'class="decision-change"' in gallery and "Permission to publish given for all photos" in gallery
    assert 'value="Çağrı Şahin"' in gallery and 'value="credit" selected' in gallery and 'value="Ç. Ş."' in gallery
    result = photographer.post(
        f"/dashboard/sets/{set_id}/upload",
        data={"csrf": photographer.cookies["csrf"]},
        files={"photos": ("edit.jpg", jpeg("red"), "image/jpeg")},
    )
    assert result.status_code == 200
    for _ in range(5):
        if not run_once():
            break
    with SessionLocal() as db:
        new = db.scalar(select(Photo).where(Photo.id != photo_id))
        new_id = new.id
    assert post(photographer, f"/dashboard/sets/{set_id}/attach", photo_id=new_id, card_id=card_id).status_code == 200
    with SessionLocal() as db:
        assert not db.scalar(select(CardPhoto).where(CardPhoto.photo_id == new_id)).delivered
        assert not any(p["id"] == new_id for p in latest(db, card_id).snapshot["photos"])
    assert post(photographer, f"/dashboard/cards/{card_id}/deliver", photo_ids=new_id).status_code == 200
    with SessionLocal() as db:
        assert photo_permission(db, db.get(Photo, photo_id)) == "ready"
        assert photo_permission(db, db.get(Photo, new_id)) == "pending"
    # A photo the grant never covered opens the question again, for all the photos.
    gallery = photographer.get("/c/" + card_code).text
    assert "New photos were added since your decision" in gallery and 'class="decision-change"' not in gallery
    assert "Allow me to publish all photos" in gallery
    assert decide(photographer, card_code).status_code == 200
    with SessionLocal() as db:
        record = latest(db, card_id)
        assert record.kind == "change" and all(item["allowed"] for item in record.snapshot["photos"])
        assert {item["id"] for item in record.snapshot["photos"]} == {photo_id, new_id}
        assert photo_permission(db, db.get(Photo, new_id)) == "ready"
    assert decide(photographer, card_code, action="withdrawal").status_code == 200
    gallery = photographer.get("/c/" + card_code).text
    assert 'value="Çağrı Şahin"' not in gallery and "Permission withdrawn" in gallery


def test_set_without_consent_asks_nothing(photographer):
    set_id, card_id, card_code, photo_id = setup_gallery(photographer, ask=False)
    gallery = photographer.get("/c/" + card_code)
    assert "?download=1" in gallery.text
    assert "/consent" not in gallery.text and 'id="decide"' not in gallery.text
    assert post(photographer, "/c/" + card_code + "/consent", action="grant", adult="on").status_code == 409
    with SessionLocal() as db:
        assert photo_permission(db, db.get(Photo, photo_id)) == "not_asked"
        assert latest(db, card_id) is None
    set_page = photographer.get(f"/dashboard/sets/{set_id}").text
    assert "No permission asked" in set_page and "Permission not asked for this set" in set_page
    # Switching it on later opens the question on the next visit.
    with SessionLocal() as db:
        name = db.get(PhotoSet, set_id).name
    assert post(photographer, f"/dashboard/sets/{set_id}/edit", name=name, request_consent="on").status_code == 200
    assert 'id="decide"' in photographer.get("/c/" + card_code).text
    with SessionLocal() as db:
        assert photo_permission(db, db.get(Photo, photo_id)) == "pending"


def test_recipient_downloads_only_the_untouched_original(photographer):
    _, _, card_code, photo_id = setup_gallery(photographer)
    gallery = photographer.get("/c/" + card_code).text
    downloads = re.findall(r'href="(/media/[^"?]+)\?download=1"', gallery)
    assert len(downloads) == 1
    original = storage.get(storage.photo_key(photo_id, "original"))
    photographer.cookies.delete("session")
    assert photographer.get(downloads[0] + "?download=1").content == original
    # The display version is for viewing on the page, never a download.
    display = re.search(r'<img src="(/media/[^"]+)"', gallery).group(1)
    assert photographer.get(display).status_code == 200
    assert photographer.get(display + "?download=1").status_code == 404


def test_wrong_file_is_rejected(photographer, caplog):
    result = post(photographer, "/dashboard/sets", name="Invalid image test")
    set_id = str(result.url).rsplit("/", 1)[1]
    response = photographer.post(
        f"/dashboard/sets/{set_id}/upload",
        data={"csrf": photographer.cookies["csrf"]},
        files={"photos": ("bad.jpg", b"not a jpeg", "image/jpeg")},
    )
    assert response.status_code == 400
    assert "bad.jpg: This file is not a JPEG" in response.text
    # A photographer's error page is not the recipient's code form.
    assert 'action="/en/resolve"' not in response.text
    assert 'action="/en/resolve"' in photographer.get("/c/NOTACODE").text

    # A phone library hands over PNGs despite the JPEG filter: the batch is refused, naming the
    # file, and the good photo is not kept.
    png = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(png, "PNG")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app"):
        response = upload(photographer, set_id, "qr_after", [("IMG_0001.jpg", jpeg("red")), ("IMG_0002.PNG", png.getvalue())])
    assert response.status_code == 400
    assert "IMG_0002.PNG: This file is not a JPEG" in response.text
    assert any("IMG_0002.PNG: This file is not a JPEG" in record.message and "400" in record.message for record in caplog.records)
    with SessionLocal() as db:
        assert db.scalar(select(Photo).where(Photo.photo_set_id == set_id)) is None

    # The browser leaves non-JPEGs out itself and names them; the set page repeats the names.
    response = photographer.post(
        f"/dashboard/sets/{set_id}/upload",
        data={"csrf": photographer.cookies["csrf"], "skipped": "IMG_0002.PNG, IMG_0003.HEIC"},
        files={"photos": ("IMG_0001.jpg", jpeg("red"), "image/jpeg")},
    )
    assert response.status_code == 200
    assert "not JPEGs: IMG_0002.PNG, IMG_0003.HEIC" in response.text


def test_failed_processing_names_the_cause(photographer, caplog):
    result = post(photographer, "/dashboard/sets", name="Broken JPEG test")
    set_id = str(result.url).rsplit("/", 1)[1]
    # JPEG magic bytes and nothing else: the upload accepts it, the worker cannot decode it.
    response = upload(photographer, set_id, "qr_after", [("IMG_0001.jpg", b"\xff\xd8\xff" + b"\0" * 100)])
    assert response.status_code == 200
    with caplog.at_level(logging.WARNING):
        while run_once():
            pass
    with SessionLocal() as db:
        photo = db.scalar(select(Photo).where(Photo.photo_set_id == set_id))
        assert photo.status == "failed"
        assert photo.error.startswith("Unable to process this JPEG (") and "Retry it on the Activity logs page." in photo.error
        job = db.scalar(select(Job).where(Job.type == "process_photo"))
        # The job waits for its second attempt; the photo already shows why the first one failed.
        assert job.status == "queued" and job.attempts == 1 and job.error and job.error in photo.error
    failures = [record for record in caplog.records if "failed:" in record.message and record.exc_info]
    assert failures and "process_photo" in failures[0].message
    assert "UnidentifiedImageError" in photographer.get("/dashboard/inbox").text


def test_phone_multipicture_jpeg_uses_primary_frame(photographer):
    result = post(photographer, "/dashboard/sets", name="Phone JPEG test")
    set_id = str(result.url).rsplit("/", 1)[1]
    response = photographer.post(
        f"/dashboard/sets/{set_id}/upload",
        data={"csrf": photographer.cookies["csrf"]},
        files={"photos": ("phone.jpeg", mpo(), "image/jpeg")},
    )
    assert response.status_code == 200
    assert run_once()
    with SessionLocal() as db:
        photo = db.scalar(select(Photo).where(Photo.photo_set_id == set_id))
        assert photo.status == "ready", photo.error
        assert (photo.width, photo.height) == (800, 1000)
        # The original stays the phone's file; the version shown on the page is a plain JPEG.
        assert storage.get(storage.photo_key(photo.id, "original")) == mpo()
        with Image.open(io.BytesIO(storage.get(storage.photo_key(photo.id, "display")))) as image:
            assert image.format == "JPEG"
            assert getattr(image, "n_frames", 1) == 1


def test_qr_marker_is_found_when_full_resolution_scan_fails(tmp_path):
    data = screen_qr_jpeg("https://photos.example.com/c/GZ199F4EWXDXAX7M")
    original = tmp_path / "IMG_4928.jpg"
    original.write_bytes(data)
    assert read_codes(original) == set(), "fixture no longer reproduces the full-resolution failure"
    assert scan_card_codes(data) == {"GZ199F4EWXDXAX7M"}


def test_set_without_qr_groups_can_delete_its_unassigned_photos(photographer):
    result = post(photographer, "/dashboard/sets", name="Marker missed")
    set_id = str(result.url).rsplit("/", 1)[1]
    response = photographer.post(
        f"/dashboard/sets/{set_id}/upload",
        data={"csrf": photographer.cookies["csrf"], "grouping_mode": "qr_after"},
        files=[("photos", ("IMG_0001.jpg", jpeg("red"), "image/jpeg")), ("photos", ("IMG_0002.jpg", jpeg("green"), "image/jpeg"))],
    )
    assert response.status_code == 200, response.text
    assert response.url.query == b"markers=0&grouped=0&unassigned=2"

    set_page = photographer.get(f"/dashboard/sets/{set_id}")
    cleanup_button = re.search(r'<button class="danger"([^>]*)>Delete 2 unassigned photos</button>', set_page.text)
    assert cleanup_button and "disabled" not in cleanup_button.group(1)
    response = post(photographer, f"/dashboard/sets/{set_id}/delete-unassigned")
    assert response.status_code == 200
    assert response.url.query == b"cleaned=2"
    with SessionLocal() as db:
        assert db.scalar(select(Photo).where(Photo.photo_set_id == set_id)) is None

    # Cleaned-up photos leave no deletion record behind: the same files upload again, elsewhere too.
    result = post(photographer, "/dashboard/sets", name="Second attempt")
    other_set_id = str(result.url).rsplit("/", 1)[1]
    response = photographer.post(
        f"/dashboard/sets/{other_set_id}/upload",
        data={"csrf": photographer.cookies["csrf"], "grouping_mode": "qr_after"},
        files=[("photos", ("IMG_0001.jpg", jpeg("red"), "image/jpeg"))],
    )
    assert response.status_code == 200, response.text
    assert response.url.query == b"markers=0&grouped=0&unassigned=1"


@pytest.mark.parametrize("mode", ["qr_after", "qr_before"])
def test_qr_markers_automatically_group_photos(photographer, mode):
    result = post(photographer, "/dashboard/sets", name="Automatic QR grouping")
    set_id = str(result.url).rsplit("/", 1)[1]
    upload_page = photographer.get(f"/dashboard/sets/{set_id}")
    assert "data-auto-upload" in upload_page.text
    assert "Selection starts the upload automatically." in upload_page.text
    assert "Scan QR markers and upload" not in upload_page.text
    card_ids = []
    card_codes = []
    for _ in range(2):
        result = post(photographer, "/dashboard/cards")
        card_id = str(result.url).rsplit("/", 1)[1]
        # A virtual card's QR is scanned off this page, so it shows until the card has photos.
        assert f"/dashboard/cards/{card_id}/qr" in result.text
        assert photographer.get(f"/dashboard/cards/{card_id}/qr").headers["content-type"] == "image/svg+xml"
        card_ids.append(card_id)
        with SessionLocal() as db:
            card_codes.append(db.get(Card, card_id).code)

    first_marker = qr_jpeg(f"http://localhost:8010/c/{card_codes[0]}")
    second_marker = qr_jpeg(f"http://localhost:8010/c/{card_codes[1]}")
    if mode == "qr_after":
        ordered = [
            ("IMG_0001.jpg", jpeg("red"), "image/jpeg"),
            ("IMG_0002.jpg", first_marker, "image/jpeg"),
            ("IMG_0003.jpg", jpeg("green"), "image/jpeg"),
            ("IMG_0004.jpg", jpeg("blue"), "image/jpeg"),
            ("IMG_0005.jpg", second_marker, "image/jpeg"),
        ]
        expected = [["IMG_0001.jpg"], ["IMG_0003.jpg", "IMG_0004.jpg"]]
    else:
        ordered = [
            ("IMG_0001.jpg", first_marker, "image/jpeg"),
            ("IMG_0002.jpg", jpeg("red"), "image/jpeg"),
            ("IMG_0003.jpg", second_marker, "image/jpeg"),
            ("IMG_0004.jpg", jpeg("green"), "image/jpeg"),
            ("IMG_0005.jpg", jpeg("blue"), "image/jpeg"),
        ]
        expected = [["IMG_0002.jpg"], ["IMG_0004.jpg", "IMG_0005.jpg"]]
    response = photographer.post(
        f"/dashboard/sets/{set_id}/upload",
        data={"csrf": photographer.cookies["csrf"], "grouping_mode": mode},
        files=[("photos", item) for item in ordered],
    )
    assert response.status_code == 200, response.text
    assert response.url.query == b"markers=2&grouped=3&unassigned=0"
    for _ in range(3):
        assert run_once()

    with SessionLocal() as db:
        cards = [db.get(Card, identity) for identity in card_ids]
        assert [card.photo_set_id for card in cards] == [set_id, set_id]
        grouped_names = [[photo.filename for _, photo in links(db, card)] for card in cards]
        assert grouped_names == expected
        stored = list(db.scalars(select(Photo).where(Photo.photo_set_id == set_id)))
        assert sorted(photo.status for photo in stored) == ["marker", "marker", "ready", "ready", "ready"]
        # The marker frames stay, hidden, so the sequence can be regrouped later.
        assert {photo.marker_card_id for photo in stored if photo.status == "marker"} == set(card_ids)
        assert len({photo.upload_id for photo in stored}) == 1
        assert {photo.grouping_mode for photo in stored} == {mode}
        marker_ids = [photo.id for photo in stored if photo.status == "marker"]
        first_group_id = links(db, cards[0])[0][1].id
        second_group = links(db, cards[1])
        selected_id = second_group[0][1].id
        excluded_id = second_group[1][1].id

    review = photographer.get(f"/dashboard/cards/{card_ids[1]}")
    assert review.status_code == 200
    assert f"/dashboard/cards/{card_ids[1]}/qr" not in review.text
    assert review.text.count('name="photo_ids"') == 2
    assert f'value="{selected_id}" checked' not in review.text
    assert "Assign selected photos" in review.text
    set_review = photographer.get(f"/dashboard/sets/{set_id}")
    assert f'action="/en/dashboard/cards/{card_ids[1]}/deliver"' in set_review.text
    assert f'name="photo_ids" value="{selected_id}"' in set_review.text
    assert f'name="photo_ids" value="{excluded_id}"' in set_review.text

    response = post(
        photographer,
        f"/dashboard/cards/{card_ids[1]}/deliver",
        photo_ids=selected_id,
    )
    assert response.status_code == 200

    with SessionLocal() as db:
        card = db.get(Card, card_ids[1])
        confirmed = links(db, card)
        assert [(link.photo_id, link.delivered) for link, _ in confirmed] == [(selected_id, True)]
        assert db.get(Photo, excluded_id).status == "ready"
        assert db.scalar(
            select(CardPhoto).where(
                CardPhoto.card_id == card.id,
                CardPhoto.photo_id == excluded_id,
            )
        ) is None

    set_review = photographer.get(f"/dashboard/sets/{set_id}")
    assert set_review.status_code == 200
    assert f'action="/en/dashboard/cards/{card_ids[1]}/deliver"' in set_review.text
    assert f'name="photo_ids" value="{excluded_id}"' not in set_review.text
    assert "Do not assign photos" in set_review.text
    assert re.search(r'<button class="danger"[^>]*disabled[^>]*>Delete 1 unassigned photo', set_review.text)
    assert set_review.text.count('<strong class="tile-unassigned">Not assigned to a QR card</strong>') == 1
    assert post(photographer, f"/dashboard/sets/{set_id}/delete-unassigned").status_code == 409

    response = post(
        photographer,
        f"/dashboard/cards/{card_ids[0]}/close",
        action="unavailable",
        return_to="set",
    )
    assert response.status_code == 200
    assert response.url.path == f"/en/dashboard/sets/{set_id}"
    with SessionLocal() as db:
        first_card = db.get(Card, card_ids[0])
        assert first_card.status == "unavailable"
        assert links(db, first_card) == []
        removed_keys = [
            storage.photo_key(photo_id, "original") for photo_id in (first_group_id, excluded_id)
        ]
        assert all(storage.exists(key) for key in removed_keys)

    set_review = photographer.get(f"/dashboard/sets/{set_id}")
    cleanup_button = re.search(
        r'<button class="danger"([^>]*)>Delete 2 unassigned photos</button>', set_review.text
    )
    assert cleanup_button and "disabled" not in cleanup_button.group(1)
    response = post(photographer, f"/dashboard/sets/{set_id}/delete-unassigned")
    assert response.status_code == 200
    assert response.url.query == b"cleaned=2"
    with SessionLocal() as db:
        assert db.get(Photo, first_group_id) is None
        assert db.get(Photo, excluded_id) is None
        assert all(db.get(Photo, marker_id) is None for marker_id in marker_ids)
        assert db.get(Photo, selected_id).status == "ready"
    while run_once():
        pass
    assert all(not storage.exists(key) for key in removed_keys)

    gallery = photographer.get("/c/" + card_codes[1])
    assert gallery.status_code == 200
    assert gallery.text.count('<article class="tile">') == 1  # the delivered photo, not the excluded one


def new_cards(photographer, count):
    ids, codes = [], []
    for _ in range(count):
        result = post(photographer, "/dashboard/cards")
        ids.append(str(result.url).rsplit("/", 1)[1])
        with SessionLocal() as db:
            codes.append(db.get(Card, ids[-1]).code)
    return ids, codes


def upload(photographer, set_id, mode, files):
    return photographer.post(
        f"/dashboard/sets/{set_id}/upload",
        data={"csrf": photographer.cookies["csrf"], "grouping_mode": mode},
        files=[("photos", (name, data, "image/jpeg")) for name, data in files],
    )


def grouped_names(card_id):
    with SessionLocal() as db:
        return [photo.filename for _, photo in links(db, db.get(Card, card_id))]


def photo_by_name(set_id, filename):
    with SessionLocal() as db:
        return db.scalar(select(Photo).where(Photo.photo_set_id == set_id, Photo.filename == filename))


@pytest.mark.parametrize("mode", ["qr_after", "qr_before"])
def test_missed_qr_marker_can_be_assigned_by_hand(photographer, mode):
    result = post(photographer, "/dashboard/sets", name="Missed marker")
    set_id = str(result.url).rsplit("/", 1)[1]
    card_ids, card_codes = new_cards(photographer, 2)
    # IMG_0003 is a card frame zbar cannot read (a plain photo here), so the upload groups wrongly.
    if mode == "qr_after":
        files = [
            ("IMG_0001.jpg", jpeg("red")),
            ("IMG_0002.jpg", jpeg("green")),
            ("IMG_0003.jpg", jpeg("blue")),
            ("IMG_0004.jpg", jpeg("yellow")),
            ("IMG_0005.jpg", qr_jpeg(f"http://localhost:8010/c/{card_codes[1]}")),
        ]
        wrong = {card_ids[0]: [], card_ids[1]: ["IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg", "IMG_0004.jpg"]}
        right = {card_ids[0]: ["IMG_0001.jpg", "IMG_0002.jpg"], card_ids[1]: ["IMG_0004.jpg"]}
        missed_card = 0
    else:
        files = [
            ("IMG_0001.jpg", qr_jpeg(f"http://localhost:8010/c/{card_codes[0]}")),
            ("IMG_0002.jpg", jpeg("red")),
            ("IMG_0003.jpg", jpeg("blue")),
            ("IMG_0004.jpg", jpeg("yellow")),
        ]
        wrong = {card_ids[0]: ["IMG_0002.jpg", "IMG_0003.jpg", "IMG_0004.jpg"], card_ids[1]: []}
        right = {card_ids[0]: ["IMG_0002.jpg"], card_ids[1]: ["IMG_0004.jpg"]}
        missed_card = 1
    response = upload(photographer, set_id, mode, files)
    assert response.status_code == 200, response.text
    assert response.url.query == f"markers=1&grouped={len(files) - 1}&unassigned=0".encode()
    assert {card_id: grouped_names(card_id) for card_id in card_ids} == wrong
    while run_once():
        pass

    missed = photo_by_name(set_id, "IMG_0003.jpg")
    assert missed.status == "ready"
    set_page = photographer.get(f"/dashboard/sets/{set_id}")
    assert set_page.text.count(f'action="/en/dashboard/sets/{set_id}/marker"') == len(files) - 1
    assert f'name="photo_id" value="{missed.id}"' in set_page.text

    # A wrong code changes nothing.
    response = post(photographer, f"/dashboard/sets/{set_id}/marker", photo_id=missed.id, code="not a code")
    assert response.status_code == 400
    assert "No QR card has this code" in response.text
    response = post(photographer, f"/dashboard/sets/{set_id}/marker", photo_id=missed.id, code=code())
    assert response.status_code == 400
    assert {card_id: grouped_names(card_id) for card_id in card_ids} == wrong

    # The code is typed the way it is printed: dashes, and O for 0 is forgiven.
    typed = "-".join(card_codes[missed_card][i : i + 4] for i in range(0, 16, 4)).replace("0", "o").lower()
    response = post(photographer, f"/dashboard/sets/{set_id}/marker", photo_id=missed.id, code=typed)
    assert response.status_code == 200, response.text
    regrouped = sum(len(names) for names in right.values())
    assert response.url.query == f"marked=1&grouped={regrouped}&unassigned=0".encode()
    assert {card_id: grouped_names(card_id) for card_id in card_ids} == right
    with SessionLocal() as db:
        marker = db.get(Photo, missed.id)
        assert (marker.status, marker.marker_card_id) == ("marker", card_ids[missed_card])
        assert db.get(Card, card_ids[missed_card]).photo_set_id == set_id
        assert db.scalar(select(CardPhoto).where(CardPhoto.photo_id == missed.id)) is None
    assert f"QR marker assigned and grouped {regrouped} photos automatically." in " ".join(response.text.split())
    set_page = photographer.get(f"/dashboard/sets/{set_id}")
    assert "IMG_0003.jpg" not in set_page.text
    assert f'action="/en/dashboard/cards/{card_ids[missed_card]}/deliver"' in set_page.text

    # Delivered photos are someone's already: they cannot turn into a marker.
    delivered = photo_by_name(set_id, right[card_ids[1]][0])
    assert post(photographer, f"/dashboard/cards/{card_ids[1]}/deliver", photo_ids=delivered.id).status_code == 200
    response = post(photographer, f"/dashboard/sets/{set_id}/marker", photo_id=delivered.id, code=card_codes[0])
    assert response.status_code == 409
    set_page = photographer.get(f"/dashboard/sets/{set_id}")
    assert f'name="photo_id" value="{delivered.id}"' not in set_page.text
    assert {card_id: grouped_names(card_id) for card_id in card_ids} == right

    # Regrouping keeps what was delivered and only redraws the proposals.
    other = photo_by_name(set_id, right[card_ids[0]][0])
    response = post(photographer, f"/dashboard/sets/{set_id}/marker", photo_id=other.id, code=card_codes[0])
    assert response.status_code == 200, response.text
    assert grouped_names(card_ids[1]) == right[card_ids[1]]


def test_unassigned_photo_can_be_added_to_a_card_by_hand(photographer):
    result = post(photographer, "/dashboard/sets", name="Missed photo")
    set_id = str(result.url).rsplit("/", 1)[1]
    card_ids, card_codes = new_cards(photographer, 3)
    # Only the first card is photographed; IMG_0003 belongs to nobody after the scan.
    files = [
        ("IMG_0001.jpg", jpeg("red")),
        ("IMG_0002.jpg", qr_jpeg(f"http://localhost:8010/c/{card_codes[0]}")),
        ("IMG_0003.jpg", jpeg("blue")),
    ]
    response = upload(photographer, set_id, "qr_after", files)
    assert response.status_code == 200, response.text
    assert response.url.query == b"markers=1&grouped=1&unassigned=1"
    while run_once():
        pass
    missed = photo_by_name(set_id, "IMG_0003.jpg")
    grouped = photo_by_name(set_id, "IMG_0001.jpg")

    # Every photo nobody has been given offers the set's cards; the second card is not in the set yet.
    set_page = photographer.get(f"/dashboard/sets/{set_id}")
    assert set_page.text.count(f'action="/en/dashboard/sets/{set_id}/attach"') == 2
    assert set_page.text.count(f'<option value="{card_ids[0]}">') == 2
    assert card_ids[1] not in set_page.text
    response = post(photographer, f"/dashboard/sets/{set_id}/attach", photo_id=missed.id, card_id=card_ids[1])
    assert response.status_code == 404
    assert grouped_names(card_ids[1]) == []

    # Photograph the second card in a later upload, then put the missed photo on it by hand.
    response = upload(photographer, set_id, "qr_after", [("IMG_0010.jpg", qr_jpeg(f"http://localhost:8010/c/{card_codes[1]}"))])
    assert response.url.query == b"markers=1&grouped=0&unassigned=0"
    set_page = photographer.get(f"/dashboard/sets/{set_id}")
    assert set_page.text.count(f'<option value="{card_ids[1]}">') == 2
    response = post(photographer, f"/dashboard/sets/{set_id}/attach", photo_id=missed.id, card_id=card_ids[1])
    assert response.status_code == 200, response.text
    assert response.url.query == b"attached=1"
    assert "Added to the card" in response.text
    assert grouped_names(card_ids[1]) == ["IMG_0003.jpg"]
    assert f'action="/en/dashboard/cards/{card_ids[1]}/deliver"' in response.text
    assert "IMG_0003.jpg added to the card" in photographer.get("/dashboard/inbox").text
    with SessionLocal() as db:
        link = db.scalar(select(CardPhoto).where(CardPhoto.photo_id == missed.id))
        assert (link.card_id, link.delivered) == (card_ids[1], False)
        assert db.get(Card, card_ids[1]).revision == 1

    # A photo proposed to the wrong card moves; one that was delivered stays where it is.
    response = post(photographer, f"/dashboard/sets/{set_id}/attach", photo_id=grouped.id, card_id=card_ids[1])
    assert response.status_code == 200, response.text
    assert grouped_names(card_ids[0]) == []
    assert grouped_names(card_ids[1]) == ["IMG_0003.jpg", "IMG_0001.jpg"]
    assert post(photographer, f"/dashboard/cards/{card_ids[1]}/deliver", photo_ids=missed.id).status_code == 200
    response = post(photographer, f"/dashboard/sets/{set_id}/attach", photo_id=missed.id, card_id=card_ids[0])
    assert response.status_code == 409
    assert "cannot be moved" in response.text
    assert grouped_names(card_ids[1]) == ["IMG_0003.jpg"]
    set_page = photographer.get(f"/dashboard/sets/{set_id}")
    assert set_page.text.count(f'action="/en/dashboard/sets/{set_id}/attach"') == 1
    assert f'name="photo_id" value="{missed.id}"' not in set_page.text

    # A card that is closed cannot receive photos even when it sits in the set.
    assert post(photographer, f"/dashboard/cards/{card_ids[2]}/close", action="void").status_code == 200
    with SessionLocal() as db:
        db.get(Card, card_ids[2]).photo_set_id = set_id
        db.commit()
    response = post(photographer, f"/dashboard/sets/{set_id}/attach", photo_id=grouped.id, card_id=card_ids[2])
    assert response.status_code == 409
    assert grouped_names(card_ids[1]) == ["IMG_0003.jpg"]


def test_set_page_preview_shows_card_decision(photographer):
    set_id, card_id, card_code, photo_id = setup_gallery(photographer)
    page = photographer.get(f"/dashboard/sets/{set_id}").text
    assert "No decision yet" in page and "Not assigned to a QR card" not in page
    assert page.count("?download=1") == 2
    names = set()
    for url in re.findall(r'href="(/media/[^"?]+\?download=1)"', page):
        response = photographer.get(url)
        assert response.status_code == 200
        names.add(response.headers["content-disposition"])
    assert names == {'attachment; filename="portrait.jpg"', 'attachment; filename="portrait-display.jpg"'}
    assert decide(photographer, card_code, signer_email="cagri@example.com", note="Thanks!").status_code == 200
    for url in (f"/dashboard/sets/{set_id}", f"/dashboard/cards/{card_id}"):
        page = photographer.get(url).text
        assert "Consent granted for this photo" in page and 'data-preview-open' in page
        assert "Çağrı Şahin" in page and "cagri@example.com" in page and "Thanks!" in page
    assert decide(photographer, card_code, "withdrawal").status_code == 200
    page = photographer.get(f"/dashboard/sets/{set_id}").text
    assert "Consent withdrawn" in page and "Consent granted for this photo" not in page


def test_reference_pages_are_in_the_sidebar_and_behind_sign_in(client, monkeypatch):
    assert "mailto:" not in client.get("/en/").text
    monkeypatch.setattr(settings(), "contact_email", "studio@example.com")
    for path in ("/dashboard/guide", "/dashboard/faq"):
        assert client.get(path).url.path == "/en/login"
    client.get("/login")
    client.post("/login", data={"csrf": client.cookies["csrf"], "password": settings().photographer_password})
    for lang, guide_title, faq_title, step in (
        ("en", "How to use", "FAQ", "Review each group and assign"),
        ("tr", "Nasıl kullanılır", "Sık sorulan sorular", "Her grubu inceleyin ve atayın"),
    ):
        guide = client.get(f"/{lang}/dashboard/guide")
        faq = client.get(f"/{lang}/dashboard/faq")
        assert guide.status_code == 200 and faq.status_code == 200
        assert f"<h1>{guide_title}</h1>" in guide.text and step in guide.text
        assert f'href="/{lang}/dashboard/faq" class="active"' in faq.text
        assert f"<h1>{faq_title}</h1>" in faq.text
        assert "mailto:" not in faq.text  # the address is for recipients, not the photographer
        assert f"/static/guide/{lang}/group.png" in guide.text
    assert client.get("/en/dashboard/inbox").text.count('href="/en/dashboard/guide"') == 1
    contact = 'href="mailto:studio@example.com">studio@example.com</a>'
    assert contact not in client.get("/en/dashboard/inbox").text
    assert contact in client.get("/en/privacy").text  # public pages keep it


def profile_form(client, **changes):
    page = client.get("/dashboard/profile").text
    current = re.search(r'name="release_text"[^>]*>([^<]*)<', page).group(1)
    data = {
        "public_name": "Deniz Kaya",
        "link_instagram": "@people",
        "bio": "",
        "release_text": current,
        **changes,
    }
    return client.post("/dashboard/profile", data={"csrf": client.cookies["csrf"], **data})


def png_logo(size=(1600, 400)):
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    image.paste((220, 40, 40, 255), (0, 0, size[0] // 2, size[1]))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def test_profile_links_are_read_from_any_form_and_shown_as_links(photographer):
    _, card_id, card_code, _ = setup_gallery(photographer)
    assert 'class="accounts"' not in photographer.get("/c/" + card_code).text  # nothing filled in
    response = profile_form(
        photographer,
        link_website="portfolio.example.com/",
        link_instagram="https://www.instagram.com/street.person/?hl=en",
        link_x="@street_person",
        link_bluesky="streetperson",
        link_tiktok="",
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        assert db.scalar(select(Profile)).links == {
            "website": "https://portfolio.example.com/",
            "instagram": "street.person",
            "x": "street_person",
            "bluesky": "streetperson.bsky.social",
        }
    gallery = photographer.get("/c/" + card_code).text
    for href, label in (
        ("https://portfolio.example.com/", "portfolio.example.com"),
        ("https://www.instagram.com/street.person/", "@street.person"),
        ("https://x.com/street_person", "@street_person"),
        ("https://bsky.app/profile/streetperson.bsky.social", "@streetperson.bsky.social"),
    ):
        assert f'<a href="{href}" target="_blank" rel="noopener noreferrer"><span>' in gallery
        assert f"<b>{label}</b>" in gallery
    assert "TikTok" not in gallery
    assert decide(photographer, card_code).status_code == 200
    with SessionLocal() as db:
        assert "Instagram: @street.person (https://www.instagram.com/street.person/)" in latest(db, card_id).snapshot["accounts"]
    # A link from another site in a network's field, or a malformed address, is refused.
    assert profile_form(photographer, link_instagram="https://x.com/someone").status_code == 400
    assert profile_form(photographer, link_website="not a site").status_code == 400


def test_profile_logo_and_card_sentence(photographer):
    page = photographer.get("/dashboard/profile").text
    assert 'name="card_text"' in page and 'name="logo"' in page
    assert 'name="release_en"' not in page and 'name="release_text"' in page
    assert photographer.get("/dashboard/profile/logo").status_code == 404
    result = photographer.post(
        "/dashboard/profile",
        data={"csrf": photographer.cookies["csrf"], **profile_data(photographer), "card_text": "  See you in a few days.  "},
        files={"logo": ("logo.png", png_logo(), "image/png")},
    )
    assert result.status_code == 200, result.text
    with SessionLocal() as db:
        row = db.scalar(select(Profile))
        assert row.card_text == "See you in a few days." and row.logo_updated_at is not None
        with Image.open(io.BytesIO(storage.get(storage.LOGO_KEY))) as saved:
            assert saved.format == "PNG" and saved.size == (1200, 300)
    logo = photographer.get("/dashboard/profile/logo")
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
    page = photographer.get("/dashboard/profile").text
    assert "/dashboard/profile/logo?v=" in page and 'name="remove_logo"' in page
    # A JPEG is accepted and converted; anything else is refused before it is stored.
    assert photographer.post(
        "/dashboard/profile",
        data={"csrf": photographer.cookies["csrf"], **profile_data(photographer)},
        files={"logo": ("logo.jpg", jpeg("red"), "image/jpeg")},
    ).status_code == 200
    refused = photographer.post(
        "/dashboard/profile",
        data={"csrf": photographer.cookies["csrf"], **profile_data(photographer)},
        files={"logo": ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")},
    )
    assert refused.status_code == 400 and "PNG or JPEG" in refused.text
    assert profile_form(photographer, remove_logo="1").status_code == 200
    with SessionLocal() as db:
        row = db.scalar(select(Profile))
        assert row.logo_updated_at is None and not storage.exists(storage.LOGO_KEY)


def profile_data(client):
    page = client.get("/dashboard/profile").text
    return {
        "public_name": "Deniz Kaya",
        "link_instagram": "@people",
        "bio": "",
        "release_text": re.search(r'name="release_text"[^>]*>([^<]*)<', page).group(1),
    }


def test_release_is_one_text_in_any_language(photographer):
    _, card_id, card_code, photo_id = setup_gallery(photographer)
    assert profile_form(photographer, release_text="Sadece bu. Only this.").status_code == 200
    with SessionLocal() as db:
        release = db.scalar(select(Release).order_by(Release.version.desc()))
        assert release.text == "Sadece bu. Only this."
    assert "Sadece bu. Only this." in photographer.get("/c/" + card_code).text
    assert "Sadece bu. Only this." in photographer.get("/tr/c/" + card_code).text
    assert decide(photographer, card_code).status_code == 200
    with SessionLocal() as db:
        assert latest(db, card_id).snapshot["release_text"] == "Sadece bu. Only this."
    # Saving the same text again does not mint a new version.
    assert profile_form(photographer, release_text="Sadece bu. Only this.").status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(Release).order_by(Release.version.desc())).text == "Sadece bu. Only this."
        assert db.scalar(select(func.count()).select_from(Release)) == 2


def test_cards_print_logo_and_batch_sentence(photographer):
    assert profile_form(photographer, card_text="Default sentence from the profile.").status_code == 200
    assert 'Default sentence from the profile.</textarea>' in photographer.get("/dashboard/cards").text
    post(photographer, "/dashboard/batches", label="Named", quantity="2", card_text="This batch says something else, long enough to wrap across the lines beside the code.")
    with SessionLocal() as db:
        batch = db.scalar(select(Batch))
        assert batch.card_text.startswith("This batch says")
    text = PdfReader(io.BytesIO(photographer.get(f"/dashboard/batches/{batch.id}/pdf?preview=1").content)).pages[0]
    assert "Bir an" not in text.extract_text() and "Deniz Kaya" in text.extract_text()
    assert "This batch says something else" in text.extract_text().replace("\n", " ")
    assert not text.images
    photographer.post(
        "/dashboard/profile",
        data={"csrf": photographer.cookies["csrf"], **profile_data(photographer)},
        files={"logo": ("logo.png", png_logo(), "image/png")},
    )
    page = PdfReader(io.BytesIO(photographer.get(f"/dashboard/batches/{batch.id}/pdf?preview=1").content)).pages[0]
    assert len(page.images) >= 1 and "Deniz Kaya" not in page.extract_text()
