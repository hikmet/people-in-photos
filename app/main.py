import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.db import SessionLocal, create_schema
from app.i18n import LanguageMiddleware, fail
from app.models import Profile
from app.routes import activity, cards, media, profile, recipient, reference, sets, site
from app.services import DEFAULT_RELEASE_TEXT, latest_release, new_release
from app.web import go, render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app):
    create_schema()
    # The one photographer and their first release exist before anyone signs in.
    with SessionLocal.begin() as db:
        if not db.scalar(select(Profile)):
            db.add(Profile())
        if not latest_release(db):
            new_release(db, DEFAULT_RELEASE_TEXT)
    yield


app = FastAPI(title="People in Photographs", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.add_middleware(LanguageMiddleware)
for module in (site, sets, cards, profile, activity, reference, recipient, media):
    app.include_router(module.router)


CSP = "default-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; style-src 'self'; script-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


@app.middleware("http")
async def headers(request, call_next):
    response = await call_next(request)
    response.headers.update(
        {
            "X-Robots-Tag": "noindex, nofollow",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": CSP,
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }
    )
    if hasattr(request.state, "csrf"):
        response.set_cookie(
            "csrf",
            request.state.csrf,
            httponly=True,
            samesite="strict",
            secure=settings().app_env != "development",
        )
    if getattr(request.state, "lang_explicit", False):
        response.set_cookie("lang", request.state.lang, samesite="lax", max_age=31536000)
    return response


def describe(request):
    client = request.client.host if request.client else "-"
    return f"{request.method} {request.url.path} from {client}"


@app.exception_handler(StarletteHTTPException)
async def error(request, exc):
    # Every refused request leaves a line in the log with the same message the user sees, so a
    # report ("it just sent me to an error page") can be matched to what actually went wrong.
    if exc.status_code == 401:
        log.info("%s -> 401, redirected to sign-in", describe(request))
        return go("/login")
    log.log(
        logging.ERROR if exc.status_code >= 500 else logging.WARNING,
        "%s -> %s: %s",
        describe(request),
        exc.status_code,
        exc.detail,
    )
    return render(
        request, "error.html", status=exc.status_code, message=exc.detail, missing=exc.status_code == 404
    )


@app.exception_handler(RequestValidationError)
async def invalid(request, exc):
    problems = "; ".join(
        f"{'.'.join(str(part) for part in item.get('loc', ()) if part != 'body')}: {item.get('msg', '')}"
        for item in exc.errors()
    )
    log.warning("%s -> 422: %s", describe(request), problems)
    message = fail(422, "err_invalid_request", problems=problems).detail
    return render(request, "error.html", status=422, message=message)


@app.exception_handler(Exception)
async def crashed(request, exc):
    # The server keeps the traceback (uvicorn logs it right after this handler returns); the user
    # gets a plain page rather than the internals, which is the one case where detail is withheld.
    log.error("%s -> 500: %s: %s", describe(request), type(exc).__name__, exc)
    message = fail(500, "err_server", kind=type(exc).__name__).detail
    return render(request, "error.html", status=500, message=message)
