import hashlib
import io
from pathlib import Path
from xml.sax.saxutils import escape

import segno
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus import Image as PdfImage

from app import storage
from app.i18n import CATALOGS, decision_label
from app.security import format_code

# DejaVu Sans ships with the app (app/fonts, Bitstream Vera licence) so PDFs render the same
# everywhere and cover Turkish glyphs.
FONTS = Path(__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("People", str(FONTS / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("PeopleBold", str(FONTS / "DejaVuSans-Bold.ttf")))


def consent_pdf(record, lang):
    """The decision record as a PDF labelled in one language: the photographer's dashboard language
    for their copy, the recipient's page language for theirs. What was entered and shown (names,
    note, the release text) is reproduced as recorded."""
    text = CATALOGS[lang]
    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=19 * mm,
        rightMargin=19 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="People in Photographs - " + text["pdf_title"],
    )
    style = ParagraphStyle("body", fontName="People", fontSize=9, leading=12, spaceAfter=6)
    title = ParagraphStyle(
        "title", parent=style, fontName="PeopleBold", fontSize=21, leading=27, spaceAfter=18
    )
    section = ParagraphStyle(
        "section", parent=title, fontSize=15, leading=20, spaceAfter=10, keepWithNext=True
    )
    elements: list[Flowable] = [
        Paragraph("People in Photographs", title),
        Paragraph(escape(text["pdf_title"]), style),
    ]
    s = record.snapshot
    connection = s["connection"]
    credit = text.get("attr_" + record.attribution, record.attribution)
    fields = [
        (text["r_id"], record.id),
        (text["r_decision"], decision_label(record.kind, lang)),
        (text["pdf_date"], str(record.created_at)),
        (text["b_role"], s["photographer"]),
        (text["r_accounts"], s["accounts"] or text["r_none"]),
        (text["r_name"], record.signer_name or text["r_name_missing"]),
        (text["pv_email"], record.signer_email or "-"),
        (text["r_ip"], record.ip or "-"),
        (text["r_ua"], record.user_agent or "-"),
        (text["r_languages"], connection["accept_language"] or "-"),
        (text["r_brands"], connection["browser_brands"] or "-"),
        (text["r_platform"], connection["platform"] or "-"),
        (text["r_mobile"], connection["mobile"] or "-"),
        (text["r_attribution"], credit + (f": {record.credit_name}" if record.credit_name else "")),
        (text["r_language"], record.locale.upper()),
        (text["r_release_version"], str(s["release_version"])),
        (text["r_release_sha"], s["release_sha256"]),
        (text["r_previous"], record.supersedes_id or "-"),
        (text["c_note"], record.note or text["r_none"]),
    ]
    for key, value in fields:
        elements.append(Paragraph(f"<b>{escape(key)}</b><br/>{escape(value).replace(chr(10), '<br/>')}", style))
    elements.append(Spacer(1, 8))
    elements.append(Paragraph(escape(text["r_photos"]), section))
    for photo in s["photos"]:
        state = text["r_allowed"] if photo["allowed"] else text["r_kept"]
        body = Paragraph(
            escape(f"{photo['filename']} - {state}")
            + "<br/>"
            + escape(f"{text['r_version']}: {photo['id']}")
            + "<br/>"
            + escape(f"{text['r_display_sha']}: {photo['display_sha256']}")
            + "<br/>"
            + escape(f"{text['r_original_sha']}: {photo['original_sha256']}"),
            style,
        )
        row = Table(
            [[thumbnail(photo), body]],
            colWidths=[THUMB + 4 * mm, doc.width - THUMB - 4 * mm],
            style=TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (0, 0), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ]
            ),
            hAlign="LEFT",
        )
        elements.append(row)
    elements.append(Paragraph(escape(text["r_text"]), section))
    elements.append(Paragraph(escape(s["release_text"]).replace("\n", "<br/>"), style))
    elements.append(Paragraph(escape(text["pdf_identity"]), style))

    def footer(c, d):
        c.setFont("People", 8)
        c.setFillColor(colors.HexColor("#666666"))
        c.drawString(19 * mm, 10 * mm, "People in Photographs · " + record.id[:8])
        c.drawRightString(191 * mm, 10 * mm, str(d.page))

    doc.build(elements, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


THUMB = 28 * mm


def thumbnail(photo):
    """A small copy of the photograph the record names, so it can be recognised at a glance. It is
    drawn only while the stored display image still matches the recorded hash; once the photo is
    deleted or replaced the cell stays empty and the hashes alone identify it."""
    try:
        data = storage.get(storage.photo_key(photo["id"], "display"))
    except (KeyError, ValueError):
        return ""
    if hashlib.sha256(data).hexdigest() != photo["display_sha256"]:
        return ""
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        image.thumbnail((240, 240), Image.Resampling.LANCZOS)
        ratio = image.width / image.height
        small = io.BytesIO()
        image.save(small, "JPEG", quality=80)
    small.seek(0)
    width, height = (THUMB, THUMB / ratio) if ratio >= 1 else (THUMB * ratio, THUMB)
    return PdfImage(small, width, height)


def wrap(text, font, size, width):
    """Words packed into lines no wider than `width`; a single word too long for a line is cut."""
    lines, current = [], ""
    for word in text.split():
        while pdfmetrics.stringWidth(word, font, size) > width:
            word = word[:-1]
        candidate = (current + " " + word).strip()
        if current and pdfmetrics.stringWidth(candidate, font, size) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def batch_pdf(batch, cards, photographer, logo=None, preview=False):
    """A4 sheets of 85x55 mm cards. The QR sits on the left; the right holds the photographer's
    logo (or their name when there is none) above the sentence the batch was generated with."""
    output = io.BytesIO()
    if logo:
        with Image.open(io.BytesIO(logo)) as image:
            logo_ratio = image.width / image.height
        logo = ImageReader(io.BytesIO(logo))
    c = canvas.Canvas(output, pagesize=A4)
    c.setTitle("People in Photographs - card preview" if preview else "People in Photographs - photo cards")
    for i, card in enumerate(cards):
        if i and i % 10 == 0:
            c.showPage()
        x = 20 * mm + (i % 2) * 85 * mm
        y = 286 * mm - ((i % 10) // 2 + 1) * 55 * mm
        c.setStrokeColor(colors.HexColor("#aaaaaa"))
        for dx, dy in ((0, 0), (85 * mm, 0), (0, 55 * mm), (85 * mm, 55 * mm)):
            c.line(x + dx - 2 * mm, y + dy, x + dx + 2 * mm, y + dy)
            c.line(x + dx, y + dy - 2 * mm, x + dx, y + dy + 2 * mm)
        c.setFillColor(colors.HexColor("#1f302b"))
        c.setFont("PeopleBold", 12)
        c.drawString(x + 5 * mm, y + 46 * mm, "People in Photographs")
        qr = segno.make(batch.base_url.rstrip("/") + "/c/" + card.code, micro=False)
        matrix = list(qr.matrix_iter(scale=1, border=4))
        size = 29 * mm / len(matrix)
        for row, cells in enumerate(matrix):
            for col, dark in enumerate(cells):
                if dark:
                    c.rect(
                        x + 5 * mm + col * size,
                        y + 11 * mm + (len(matrix) - row - 1) * size,
                        size,
                        size,
                        fill=1,
                        stroke=0,
                    )
        # The right-hand block: 43 mm wide from x+37, between the heading and the code line.
        left, width = x + 37 * mm, 43 * mm
        if logo:
            box_w, box_h = width, 16 * mm
            w = min(box_w, box_h * logo_ratio)
            h = w / logo_ratio
            c.drawImage(logo, left + (box_w - w) / 2, y + 40 * mm - h, w, h, mask="auto")
        else:
            c.setFont("PeopleBold", 9)
            c.drawString(left, y + 36 * mm, (wrap(photographer.public_name, "PeopleBold", 9, width) or [""])[0])
        c.setFont("People", 7)
        # Four lines of 7 pt fit the 120-character cap (CARD_TEXT_MAX in app/services.py) and stop just
        # above the code line.
        for line, text in enumerate(wrap(batch.card_text, "People", 7, width)[:4]):
            c.drawString(left, y + (20 - line * 3.5) * mm, text)
        c.setFont("PeopleBold", 8)
        c.drawString(x + 5 * mm, y + 6 * mm, format_code(card.code))
        c.setFont("People", 6)
        c.drawRightString(x + 80 * mm, y + 6 * mm, str(card.sequence or i + 1))
        if preview:
            c.saveState()
            c.setFillColor(colors.HexColor("#ad482f"))
            c.setFont("PeopleBold", 7)
            c.drawString(x + 37 * mm, y + 11 * mm, "LOCAL PREVIEW - NOT FOR USE")
            c.restoreState()
    c.save()
    return output.getvalue()
