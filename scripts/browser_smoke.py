"""Exercise the live Compose stack through Chromium. Leaves clearly labelled sample data.

Run inside the Compose network:

    docker compose --profile test run --rm tests python scripts/browser_smoke.py

The browser must see the stack at its public origin (http://localhost:8010): Chromium sends
Fetch Metadata and a real Origin header only to trustworthy origins, so a bare service name
such as http://caddy fails the same-origin check on every form post. The script therefore
forwards 127.0.0.1:8010 to caddy:80 in-process. Screenshots and result.json land in
output/browser on the host.
"""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from browser_common import forward, marker, run, sample
from playwright.sync_api import expect, sync_playwright

out = Path("output/browser")
out.mkdir(parents=True, exist_ok=True)
base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8010").rstrip("/")
upstream = os.environ.get("SMOKE_UPSTREAM", "caddy:80")  # empty when the stack already answers on `base`
password = os.environ.get("PHOTOGRAPHER_PASSWORD", "people-local")

if upstream:
    host, _, port = upstream.partition(":")
    forward(urlparse(base).port or 80, host, int(port or 80))


with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    context = browser.new_context(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
    errors = []
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))

    # Photographer: sign in, open a set, generate the QR card that will close the group.
    page.goto(base + "/login")
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("heading", name="Photo sets", exact=True)).to_be_visible()
    page.get_by_role("button", name="Create a new photo set").click()
    page.get_by_label("Name", exact=True).fill(f"Demo walk · {run}")
    page.get_by_label("Location (optional)").fill("A place to try the workflow")
    page.get_by_label("Ask recipients for permission to publish").check()
    page.get_by_role("button", name="Create", exact=True).click()
    expect(page.get_by_role("heading", name=f"Demo walk · {run}")).to_be_visible()
    set_url = page.url

    page.goto(base + "/dashboard/cards")
    page.get_by_label("Optional note").fill("Demo recipient · no real person pictured")
    page.get_by_role("button", name="Generate QR card", exact=True).click()
    expect(page.get_by_role("heading", name="Waiting for its QR marker")).to_be_visible()
    card_url = page.url
    formatted_code = page.locator(".card-code-title").inner_text().strip()
    card_code = formatted_code.replace("-", "")
    recipient_url = f"{base}/c/{card_code}"

    # Upload two portraits followed by the QR frame; the marker closes the group automatically.
    page.goto(set_url)
    page.locator("input[type=file]").set_input_files([sample(0), sample(1), marker(recipient_url)])
    page.wait_for_url(re.compile(r"markers=1&grouped=2"), timeout=90000)
    group = page.locator(".detected-group", has_text=formatted_code)  # earlier runs may leave other groups
    ready = group.locator("input[name=photo_ids]:not([disabled])")
    expect(ready).to_have_count(2, timeout=120000)  # the page reloads itself while the worker runs
    for checkbox in ready.all():
        checkbox.check()
    with page.expect_navigation():
        group.get_by_role("button", name="Assign selected", exact=True).click()
    page.goto(card_url)
    expect(page.locator(".photo-tile")).to_have_count(2)
    expect(page.get_by_role("heading", name="Assigned photos")).to_be_visible()
    page.screenshot(path=str(out / "card-desktop.png"), full_page=True)
    page.goto(base + "/dashboard")
    page.screenshot(path=str(out / "dashboard-desktop.png"), full_page=True)

    # Recipient: download, allow all photos in Turkish, fetch the record, withdraw.
    subject = browser.new_context(viewport={"width": 1280, "height": 1000}).new_page()
    subject.on("pageerror", lambda error: errors.append(str(error)))
    subject.goto(recipient_url)
    expect(subject.locator(".tile")).to_have_count(2)
    expect(subject.get_by_text("Download any photo.", exact=True)).to_be_visible()
    with subject.expect_download() as download:
        subject.get_by_role("link", name="Download photo", exact=True).first.click()
    assert download.value.suggested_filename.endswith(".jpg")
    subject.locator(".lang-menu summary").click()
    subject.get_by_role("link", name="Türkçe", exact=True).click()
    expect(subject.get_by_role("heading", name="Bu fotoğrafları yayımlayabilir miyim?")).to_be_in_viewport()
    subject.get_by_label("Adınız ve soyadınız", exact=True).fill("Demo Çağrı Şahin")
    subject.get_by_label("Fotoğraftaki kişi benim ve 18 yaşında veya daha büyüğüm.", exact=True).check()
    subject.get_by_role("button", name="Tüm fotoğrafları yayımlamama izin ver", exact=True).click()
    expect(subject.get_by_text("Kararınız kaydedildi.", exact=True)).to_be_visible()
    with subject.expect_download() as record_download:
        subject.get_by_role("link", name="Karar kaydını indir (PDF)", exact=True).click()
    record_download.value.save_as(str(out / "browser-decision-tr.pdf"))
    subject.set_viewport_size({"width": 390, "height": 844})
    subject.screenshot(path=str(out / "gallery-mobile-tr.png"), full_page=True)
    assert subject.evaluate("document.documentElement.scrollWidth <= innerWidth")
    subject.get_by_role("button", name="İznimi geri çek", exact=True).click()
    expect(subject.get_by_text("İzin geri çekildi", exact=True)).to_be_visible()
    subject.locator(".lang-menu summary").click()
    subject.get_by_role("link", name="English", exact=True).click()
    subject.set_viewport_size({"width": 1280, "height": 1000})
    subject.screenshot(path=str(out / "gallery-desktop.png"), full_page=True)

    # Photographer: the withdrawal reaches the inbox; the workspace holds up on a phone.
    page.goto(base + "/dashboard/inbox")
    expect(page.get_by_text("Permission withdrawn", exact=True).first).to_be_visible()
    page.screenshot(path=str(out / "inbox-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base + "/dashboard")
    page.screenshot(path=str(out / "dashboard-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors, errors
    (out / "result.json").write_text(
        json.dumps(
            {
                "result": "passed",
                "set_url": set_url,
                "card_url": card_url,
                "recipient_url": recipient_url,
                "javascript_errors": errors,
            },
            indent=2,
        )
    )
    print((out / "result.json").read_text())
    browser.close()
