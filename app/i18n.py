import contextvars
import json
import re
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from app.config import settings

CATALOGS = {p.stem: json.loads(p.read_text()) for p in Path("app/locales").glob("*.json")}

LANGUAGES = ("en", "tr")
current_lang = contextvars.ContextVar("lang", default="en")


def negotiate(cookie, accept_language, fallback="en"):
    """Pick a language for a URL that carries none: the remembered choice, then the browser's."""
    if cookie in CATALOGS:
        return cookie
    options = []
    for part in (accept_language or "").split(","):
        tag, *params = part.strip().split(";")
        try:
            weight = float(params[0].strip().removeprefix("q=")) if params else 1.0
        except ValueError:
            continue
        if weight > 0:
            options.append((weight, tag.split("-")[0].lower()))
    for _, tag in sorted(options, key=lambda x: x[0], reverse=True):
        if tag in CATALOGS:
            return tag
    return fallback if fallback in CATALOGS else "en"


def locale(request):
    return request.state.lang


def translate(lang, key):
    return CATALOGS[lang][key]


def decision_label(kind, lang):
    """How a decision record is named ("Consent granted", ...), in the viewer's language."""
    return CATALOGS[lang].get("decision_" + kind, kind.replace("_", " ").capitalize())


def fail(status, key, headers=None, **values):
    """An HTTPException whose message is shown in the language of the current request."""
    message = translate(current_lang.get(), key)
    if values:
        message = message.format(**values)
    return HTTPException(status, message, headers=headers)


LANGUAGE_PREFIX = re.compile(r"^/(" + "|".join(LANGUAGES) + r")(?=/|$)")
LANGUAGE_NEUTRAL = re.compile(r"^/(static/|media/|health$|robots\.txt$)")


class LanguageMiddleware:
    """Every page lives under /<lang>/. The prefix is stripped before routing; a language-neutral
    page URL (the QR code's /c/<code>, a typed /login) redirects to the visitor's language."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        cookies = dict(
            pair.strip().split("=", 1) for pair in headers.get("cookie", "").split(";") if "=" in pair
        )
        path = scope["path"]
        match = LANGUAGE_PREFIX.match(path)
        state = scope.setdefault("state", {})
        if match:
            lang = match.group(1)
            scope["path"] = path[len(lang) + 1 :] or "/"
            scope["raw_path"] = scope["path"].encode()
            state["lang_explicit"] = True
        else:
            lang = negotiate(cookies.get("lang"), headers.get("accept-language"), settings().default_locale)
            state["lang_explicit"] = False
            if scope["method"] in ("GET", "HEAD") and not LANGUAGE_NEUTRAL.match(path):
                query = scope.get("query_string", b"").decode()
                target = f"/{lang}{path}" + (f"?{query}" if query else "")
                return await RedirectResponse(target, status_code=302)(scope, receive, send)
        state["lang"] = lang
        token = current_lang.set(lang)
        try:
            await self.app(scope, receive, send)
        finally:
            current_lang.reset(token)
