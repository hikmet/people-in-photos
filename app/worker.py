import hashlib
import io
import json
import logging
import subprocess
import sys
import tempfile
import time
import warnings
from datetime import timedelta
from pathlib import Path

from PIL import Image, ImageCms, ImageOps
from sqlalchemy import select

from app import storage
from app.db import SessionLocal, create_schema
from app.models import Job, Photo, now
from app.services import EXIF_TABS, profile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
Image.MAX_IMAGE_PIXELS = 36_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)


def process_photo(photo_id):
    """Read one uploaded JPEG's camera details and render the versions it is viewed in.

    The upload itself, stored untouched at upload with all its metadata, is the original that the
    photographer and the recipient download; it is only read here. The display and thumbnail
    versions, only ever viewed on a page, are re-encoded without camera metadata.

    Three steps, so the database's write lock (app/db.py) is never held while exiftool and Pillow
    run: read what the work needs, do the work on files in a scratch directory, then store the
    results only if the photo is still waiting for them (it may have been deleted meanwhile)."""
    with SessionLocal.begin() as db:
        photo = db.get(Photo, photo_id)
        if not photo or photo.status in ("deleted", "ready", "marker"):
            return
        name = profile(db).public_name
    with tempfile.TemporaryDirectory() as scratch:
        source = Path(scratch) / "original.jpg"
        source.write_bytes(storage.get(storage.photo_key(photo_id, "original")))
        with Image.open(source) as raw:
            # Phone cameras may store an auxiliary image in an otherwise normal JPEG.
            # Pillow reports these JPEG Multi-Picture files as MPO; use the primary frame.
            if raw.format not in {"JPEG", "MPO"}:
                raise ValueError("Only exported JPEG photographs are supported")
            raw.load()
            captured_at = str(raw.getexif().get_ifd(34665).get(36867, ""))[:40]
            img = ImageOps.exif_transpose(raw)
            if raw.info.get("icc_profile"):
                try:
                    img = ImageCms.profileToProfile(
                        img,
                        ImageCms.ImageCmsProfile(io.BytesIO(raw.info["icc_profile"])),
                        ImageCms.createProfile("sRGB"),
                        outputMode="RGB",
                    )
                except Exception as exc:
                    raise ValueError("Invalid colour profile; re-export as sRGB JPEG") from exc
            if img is None:
                raise ValueError("Colour conversion produced no image")
            img = img.convert("RGB")
            # Pillow would carry the upload's comment, XMP and other metadata into the new files.
            img.info = {}
            width, height = img.size
            exif = Image.Exif()
            exif[315] = name
            exif[33432] = "Copyright " + name
            srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            variants = {}
            for variant, size in (("display", 2048), ("thumb", 480)):
                copy = img.copy()
                copy.thumbnail((size, size), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                copy.save(output, "JPEG", quality=85, optimize=True, exif=exif, icc_profile=srgb)
                variants[variant] = output.getvalue()
        details = read_exif(source)
    with SessionLocal.begin() as db:
        photo = db.get(Photo, photo_id)
        if not photo or photo.status in ("deleted", "ready", "marker"):
            return
        for variant, data in variants.items():
            storage.put(storage.photo_key(photo.id, variant), data)
        photo.captured_at, photo.width, photo.height, photo.exif = captured_at, width, height, details
        photo.display_sha256 = hashlib.sha256(variants["display"]).hexdigest()
        photo.status, photo.error = "ready", ""


def read_exif(path):
    """The EXIF_TABS details of one JPEG as exiftool prints them, coordinates in decimal degrees.
    A photo whose details cannot be read is still processed, without them."""
    tags = [tag for tags in EXIF_TABS.values() for tag in tags]
    result = subprocess.run(
        ["exiftool", "-json", "-coordFormat", "%.6f", *("-" + tag for tag in tags), str(path)],
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        return {}
    found = json.loads(result.stdout)[0]
    return {tag: str(found[tag])[:120] for tag in tags if str(found.get(tag, "")).strip()}


def perform(kind, payload):
    """Run one claimed job. Slow work happens outside a transaction; each step that touches rows
    opens its own short one."""
    if kind == "process_photo":
        process_photo(payload["photo_id"])
    elif kind == "purge_photo":
        # The row is gone when unassigned photos were cleaned up; it is kept as the record when a
        # card's photos were deleted (services.delete_card_photos). Every file goes either way.
        with SessionLocal.begin() as db:
            photo = db.get(Photo, payload["photo_id"])
            if photo is None:
                storage.delete_photo_files(payload["photo_id"])
            elif photo.status == "deleted":
                storage.delete_photo_files(photo.id)
                photo.exif = {}
    else:
        raise ValueError("Unknown job type")


def run_once():
    with SessionLocal.begin() as db:
        stale = now() - timedelta(minutes=15)
        for job in db.scalars(select(Job).where(Job.status == "running", Job.locked_at < stale)):
            job.status = "queued" if job.attempts < 3 else "failed"
        job = db.scalar(
            select(Job)
            .where(Job.status == "queued", Job.run_after <= now())
            .order_by(Job.created_at)
            .limit(1)
        )
        if not job:
            return False
        job.status, job.locked_at = "running", now()
        job.attempts += 1
        identity, kind, attempt, payload = job.id, job.type, job.attempts, dict(job.payload)
    try:
        perform(kind, payload)
        with SessionLocal.begin() as db:
            job = db.get(Job, identity)
            if job:
                job.status, job.error = "done", ""
    except Exception as exc:
        logging.exception("Job %s (%s, attempt %s) failed: %s", identity, kind, attempt, exc)
        with SessionLocal.begin() as db:
            job = db.get(Job, identity)
            if job:
                job.status = "failed" if job.attempts >= 3 else "queued"
                job.error = failure_message(exc)
                job.run_after = now() + timedelta(seconds=10 * job.attempts)
                if job.type == "process_photo":
                    photo = db.get(Photo, job.payload["photo_id"])
                    if photo and photo.status != "deleted":
                        photo.status = "failed"
                        photo.error = failure_message(exc) + " Retry it on the Activity logs page."
    return True


def failure_message(exc):
    """What the photographer reads on a failed photo or job. ValueErrors are raised here with a
    sentence written for them; anything else is named so the cause is not hidden behind a generic
    line, and the traceback is in the worker log."""
    if isinstance(exc, ValueError):
        return str(exc)[:400]
    detail = str(exc)[:300].strip()
    return f"Unable to process this JPEG ({type(exc).__name__}{': ' + detail if detail else ''})."


def main():
    if "--health" in sys.argv:
        heartbeat = Path("/tmp/people-worker-heartbeat")
        sys.exit(0 if heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 600 else 1)
    if "--once" in sys.argv:
        run_once()
        return
    create_schema()
    while True:
        Path("/tmp/people-worker-heartbeat").touch()
        if not run_once():
            time.sleep(1)


if __name__ == "__main__":
    main()
