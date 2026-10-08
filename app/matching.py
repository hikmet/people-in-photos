import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import HTTPException
from PIL import Image

from app.i18n import fail
from app.security import normalize


def card_code_from_payload(payload: str):
    try:
        parsed = urlparse(payload.strip())
        parts = [unquote(part) for part in parsed.path.rstrip("/").split("/") if part]
        if len(parts) < 2 or parts[-2] != "c":
            return None
        return normalize(parts[-1])
    except (HTTPException, ValueError):
        return None


REDUCED_LONG_SIDE = 600


# Only QR codes are card markers; the linear barcodes zbar reads by default are switched off.
ZBAR_SYMBOLOGIES = [
    "-Sean13.disable", "-Sean8.disable", "-Supca.disable", "-Supce.disable",
    "-Sisbn13.disable", "-Sisbn10.disable", "-Si25.disable", "-Scodabar.disable",
    "-Scode39.disable", "-Scode93.disable", "-Scode128.disable", "-Sqrcode.enable",
]  # fmt: skip


def read_codes(path: Path):
    try:
        result = subprocess.run(
            ["zbarimg", "--quiet", "--raw", *ZBAR_SYMBOLOGIES, str(path)],
            capture_output=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        raise fail(400, "err_qr_timeout") from exc
    payloads = result.stdout.decode("utf-8", "replace").splitlines()
    return {code for payload in payloads if (code := card_code_from_payload(payload))}


def reduce_for_scan(source: Path, target: Path):
    """Write a grayscale copy about REDUCED_LONG_SIDE pixels wide; False if the original is already that small."""
    try:
        with Image.open(source) as image:
            long_side = max(image.size)
            if long_side <= REDUCED_LONG_SIDE:
                return False
            image.draft("L", (image.width * REDUCED_LONG_SIDE // long_side, image.height * REDUCED_LONG_SIDE // long_side))
            image.load()
            image.thumbnail((REDUCED_LONG_SIDE, REDUCED_LONG_SIDE))
            image.convert("L").save(target)
            return True
    except (OSError, ValueError):
        return False


def scan_card_codes(data: bytes):
    """Scan a reduced copy first, then the original.

    zbar is unreliable on full-resolution phone photographs: sensor noise, JPEG artefacts and, for
    photographs of a screen, the pixel grid break the QR modules apart. Shrinking averages that
    away, and libjpeg decodes at 1/4 or 1/8 scale almost for free, so the small pass is both the
    cheap one and the reliable one. The original is scanned only when the small pass finds
    nothing, for a card that is tiny in the frame.
    """
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "upload.jpg"
        source.write_bytes(data)
        reduced = Path(folder) / "reduced.png"
        if reduce_for_scan(source, reduced) and (codes := read_codes(reduced)):
            return codes
        return read_codes(source)


def natural_filename(value: str):
    return [(0, int(part)) if part.isdigit() else (1, part.casefold()) for part in re.split(r"(\d+)", value)]


def group_entries(entries, mode):
    """Split a camera sequence into one photo group per QR card.

    ``entries`` are ``("marker", card)`` and ``("photo", photo)`` pairs in camera order. With
    ``qr_after`` a marker closes the group of photos before it; with ``qr_before`` it opens the
    group after it. Returns ``{card_id: [photos]}`` (every marker card present, even when empty)
    and the photos outside any group. A card photographed twice collects both runs.
    """
    groups = {}
    unassigned = []
    if mode == "qr_after":
        pending = []
        for kind, item in entries:
            if kind == "photo":
                pending.append(item)
            else:
                groups.setdefault(item.id, []).extend(pending)
                pending = []
        unassigned = pending
    else:
        active = None
        for kind, item in entries:
            if kind == "marker":
                active = item
                groups.setdefault(item.id, [])
            elif active:
                groups[active.id].append(item)
            else:
                unassigned.append(item)
    return groups, unassigned
