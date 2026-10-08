"""Where people can find the photographer's work: a website and a handle per social network.

The profile keeps each one in its plain form (the website as a full URL, a network as the bare
handle); this module reads what the photographer typed, a handle, an @handle or a pasted profile
URL, and turns the stored value back into the link and text the recipient page shows.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.i18n import fail


@dataclass(frozen=True)
class Network:
    key: str
    label: str
    domains: tuple
    pattern: str
    url: str  # with {handle}


NETWORKS = (
    Network("instagram", "Instagram", ("instagram.com",), r"[A-Za-z0-9._]{1,30}", "https://www.instagram.com/{handle}/"),
    Network("threads", "Threads", ("threads.com", "threads.net"), r"[A-Za-z0-9._]{1,30}", "https://www.threads.com/@{handle}"),
    Network("x", "X", ("x.com", "twitter.com"), r"[A-Za-z0-9_]{1,15}", "https://x.com/{handle}"),
    Network(
        "bluesky",
        "Bluesky",
        ("bsky.app",),
        r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}",
        "https://bsky.app/profile/{handle}",
    ),
    Network("facebook", "Facebook", ("facebook.com", "fb.com"), r"[A-Za-z0-9.]{1,50}", "https://www.facebook.com/{handle}"),
    Network("tiktok", "TikTok", ("tiktok.com",), r"[A-Za-z0-9._]{1,24}", "https://www.tiktok.com/@{handle}"),
)
WEBSITE = "website"
FIELDS = (WEBSITE, *(network.key for network in NETWORKS))


def clean(form):
    """The links a profile form submits, each checked and in its stored form; empty ones left out."""
    links = {}
    for key in FIELDS:
        value = str(form.get("link_" + key, "")).strip()
        if value:
            links[key] = website(value) if key == WEBSITE else handle(network(key), value)
    return links


def network(key):
    return next(n for n in NETWORKS if n.key == key)


def website(value):
    url = value if re.match(r"(?i)https?://", value) else "https://" + value
    parts = urlsplit(url)
    if len(url) > 300 or not re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d+)?", parts.netloc) or " " in url:
        raise fail(400, "err_link", field="Website")
    return url


def handle(net, value):
    if re.match(r"(?i)(https?://)?([a-z0-9-]+\.)*(%s)(/|$)" % "|".join(map(re.escape, net.domains)), value):
        parts = urlsplit(value if "://" in value else "https://" + value)
        segments = [s for s in parts.path.split("/") if s]
        # bsky.app/profile/<handle>; every other network keeps the handle first in the path.
        if net.key == "bluesky" and segments[:1] == ["profile"]:
            segments = segments[1:]
        value = segments[0] if segments else ""
    value = value.removeprefix("@")
    if net.key == "bluesky" and value and "." not in value:
        value += ".bsky.social"
    if not re.fullmatch(net.pattern, value):
        raise fail(400, "err_link", field=net.label)
    return value.lower() if net.key == "bluesky" else value


def shown(links):
    """The links as the recipient page and the decision record show them: (label, text, url)."""
    rows = []
    if links.get(WEBSITE):
        url = links[WEBSITE]
        rows.append(("Website", re.sub(r"(?i)^https?://(www\.)?", "", url).rstrip("/"), url))
    for net in NETWORKS:
        if links.get(net.key):
            rows.append((net.label, "@" + links[net.key], net.url.format(handle=links[net.key])))
    return rows


def as_text(links):
    """One line per link, kept in decision records so they show exactly what was on the page."""
    return "\n".join(f"{label}: {text} ({url})" for label, text, url in shown(links))
