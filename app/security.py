import hashlib
import hmac
import secrets
import time
from collections import OrderedDict

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings
from app.i18n import fail

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def code():
    return "".join(secrets.choice(ALPHABET) for _ in range(16))


def normalize(value):
    value = value.upper().translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
    value = "".join(value.split()).replace("-", "")
    if len(value) != 16 or any(c not in ALPHABET for c in value):
        raise fail(404, "err_not_found")
    return value


def format_code(value):
    """A card code as printed: four groups of four, 7K3M-9X2B-04HD-ZP8R."""
    return "-".join(value[i : i + 4] for i in range(0, 16, 4))


def serializer(salt):
    return URLSafeTimedSerializer(settings().secret_key, salt=salt)


def sign(data, salt):
    return serializer(salt).dumps(data)


def unsign(token, salt, age=7200):
    try:
        return serializer(salt).loads(token, max_age=age)
    except (BadSignature, SignatureExpired):
        raise fail(403, "err_expired") from None


def csrf(request: Request):
    value = request.cookies.get("csrf") or secrets.token_urlsafe(32)
    request.state.csrf = value
    return value


def check_csrf(request, form):
    cookie = request.cookies.get("csrf", "")
    if not cookie or not hmac.compare_digest(cookie, str(form.get("csrf", ""))):
        raise fail(403, "err_reload")
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site == "cross-site":
        raise fail(403, "err_origin")
    if fetch_site in {"same-origin", "same-site", "none"}:
        return
    origin = request.headers.get("origin")
    request_origin = f"{request.url.scheme}://{request.url.netloc}".rstrip("/")
    if origin and origin.rstrip("/") != request_origin:
        raise fail(403, "err_origin")


def ip(request):
    """The visitor's address, for rate limits and decision records.

    uvicorn runs with --proxy-headers and trusts X-Forwarded-For from any hop, which is safe because
    the app's port is only reachable through Caddy, and Caddy replaces that header with the address
    it was connected from. Behind a further proxy (a CDN, a load balancer) that address is the
    proxy's, so every visitor would share one rate-limit bucket: name the proxy in Caddy's
    `trusted_proxies` (docker/Caddyfile) so Caddy passes the real address on."""
    return request.client.host if request.client else "unknown"


class RateLimiter:
    def __init__(self):
        self.buckets = OrderedDict()

    def check(self, request, group, limit=120, window=60):
        key = (hashlib.sha256(ip(request).encode()).hexdigest(), group)
        current = time.monotonic()
        hits, start = self.buckets.pop(key, (0, current))
        if current - start > window:
            hits, start = 0, current
        self.buckets[key] = (hits + 1, start)
        while len(self.buckets) > 10000:
            self.buckets.popitem(last=False)
        if hits >= limit:
            raise fail(429, "err_rate", headers={"Retry-After": str(window)})


limiter = RateLimiter()
