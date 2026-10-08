"""Capture the screenshots the "How to use" page shows, in both dashboard languages.

Run in the test profile:

    docker compose --profile test run --rm --build tests python scripts/guide_screenshots.py

Unlike the smoke script, this one does not touch the running stack: it starts its own app and
worker inside the container, on the throwaway test database and with the local password login,
so the pages show nothing but the one demo encounter it creates (a set, a virtual card, an upload
with a QR marker frame and one stray photo, a delivery, a recipient decision). The images are
written straight into app/static/guide/<lang>/, which the test profile mounts from the host, so a
run replaces the page's pictures; rebuild the app image to serve them.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from browser_common import marker, run, sample
from playwright.sync_api import expect, sync_playwright

out = Path("app/static/guide")
base = "http://localhost:8010"
password = os.environ.get("PHOTOGRAPHER_PASSWORD", "people-local")
LANGUAGES = ("en", "tr")


def serve():
    """The app and its worker, in this container, on the port the public URL names."""
    env = {**os.environ, "PUBLIC_BASE_URL": base}
    children = [
        subprocess.Popen(["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8010"], env=env),
        subprocess.Popen([sys.executable, "-m", "app.worker"], env=env),
    ]
    for _ in range(60):
        try:
            urlopen(base + "/health", timeout=2)
            return children
        except OSError:
            time.sleep(1)
    raise SystemExit("the app did not come up")


def path_of(url):
    """The page path without its language prefix, so the same page opens in either language."""
    return re.sub(r"^/(en|tr)(?=/|$)", "", urlparse(url).path)


children = serve()
with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    context = browser.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
    page = context.new_page()

    def shoot(name, path, selector, prepare=None, max_height=None):
        """Photograph one element of a page in every language."""
        for lang in LANGUAGES:
            page.goto(f"{base}/{lang}{path}")
            if prepare:
                prepare()
            target = page.locator(selector).first
            expect(target).to_be_visible()
            target.scroll_into_view_if_needed()
            box = target.bounding_box()
            assert box, f"{selector} has no box on {path}"
            file = out / lang / f"{name}.png"
            file.parent.mkdir(parents=True, exist_ok=True)
            if max_height and box["height"] > max_height:
                page.screenshot(path=str(file), clip={**box, "height": max_height})
            else:
                target.screenshot(path=str(file))

    page.goto(base + "/login")
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("heading", name="Photo sets", exact=True)).to_be_visible()

    # Before the walk: the profile and the cards.
    shoot("profile", "/dashboard/profile", ".profile-form .panel")
    shoot("cards", "/dashboard/cards", ".two-columns > aside")

    # A set and a virtual card for one demo encounter.
    page.goto(base + "/en/dashboard")
    page.get_by_role("button", name="Create a new photo set").click()
    page.get_by_label("Name", exact=True).fill(f"Guide walk · {run}")
    page.get_by_label("Location (optional)").fill("Karaköy, Istanbul")
    page.get_by_label("Ask recipients for permission to publish").check()
    page.get_by_role("button", name="Create", exact=True).click()
    expect(page.get_by_role("heading", name=f"Guide walk · {run}")).to_be_visible()
    set_path = path_of(page.url)

    page.goto(base + "/en/dashboard/cards")
    page.get_by_label("Optional note").fill("Blue coat, outside the bookshop (demo, nobody real)")
    page.get_by_role("button", name="Generate QR card", exact=True).click()
    expect(page.get_by_role("heading", name="Waiting for its QR marker")).to_be_visible()
    card_path = path_of(page.url)
    formatted_code = page.locator(".card-code-title").inner_text().strip()
    card_code = formatted_code.replace("-", "")
    recipient_url = f"{base}/c/{card_code}"
    shoot("virtual-card", card_path, ".recipient-link")

    # At home: the upload panel, then a sequence of two portraits, the card frame, and one stray
    # frame that no marker claims.
    shoot("upload", set_path, ".upload-box")
    page.goto(f"{base}/en{set_path}")
    page.locator("input[type=file]").set_input_files(
        [sample(0, 1), sample(1, 2), marker(recipient_url, 3), sample(2, 4)]
    )
    page.wait_for_url(re.compile(r"markers=1&grouped=2"), timeout=90000)

    def group_ready():
        group = page.locator(".detected-group", has_text=formatted_code)
        expect(group.locator("input[name=photo_ids]:not([disabled])")).to_have_count(2, timeout=120000)
        return group

    group_ready()
    shoot("group", set_path, f".detected-group:has-text('{formatted_code}')", prepare=group_ready)

    def open_stray_preview():
        group_ready()
        page.locator(".photo-tile", has=page.locator(".tile-unassigned")).first.locator("[data-preview-open]").click()
        expect(page.locator("#photo-preview")).to_be_visible()

    shoot("preview", set_path, "#photo-preview", prepare=open_stray_preview)

    # Assign the two portraits, leaving the stray frame unassigned so the cleanup offers itself.
    page.goto(f"{base}/en{set_path}")
    group = group_ready()
    for checkbox in group.locator("input[name=photo_ids]:not([disabled])").all():
        checkbox.check()
    with page.expect_navigation():
        group.get_by_role("button", name="Assign selected", exact=True).click()
    shoot("cleanup", set_path, ".set-cleanup")

    # The recipient opens the link, then allows the photos.
    subject = browser.new_context(viewport={"width": 1100, "height": 860}).new_page()
    for lang in LANGUAGES:
        subject.goto(f"{base}/{lang}/c/{card_code}")
        expect(subject.locator(".tile")).to_have_count(2)
        (out / lang).mkdir(parents=True, exist_ok=True)
        subject.screenshot(path=str(out / lang / "recipient.png"))
    subject.goto(f"{base}/en/c/{card_code}")
    subject.get_by_label("Your full name", exact=True).fill("Demo Person")
    subject.get_by_label("I am the person pictured and I am 18 or older.", exact=True).check()
    subject.get_by_role("button", name="Allow me to publish all photos", exact=True).click()
    expect(subject.get_by_text("Your decision has been saved.", exact=True)).to_be_visible()

    # Afterwards: the photo states on the set page and the activity log.
    shoot("states", set_path, ".photo-grid")
    shoot("activity", "/dashboard/inbox", ".panel.log", max_height=560)
    print(f"Screenshots written to {out} for card {formatted_code}")
    browser.close()
for child in children:
    child.terminate()
