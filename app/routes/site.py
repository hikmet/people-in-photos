"""Public pages that belong to no card: home, code entry, sign-in, privacy."""

import random
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.i18n import locale, translate
from app.security import limiter, normalize, sign
from app.web import current_photographer, form_data, go, render, session_value

router = APIRouter()

# Drop photographs into this folder and the landing page shows one of them at random per visit.
HOME_PHOTOS = Path("app/static/home")


def home_photo():
    photos = sorted(p.name for p in HOME_PHOTOS.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    return f"/static/home/{random.choice(photos)}" if photos else None


@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "application": "People in Photographs"}


@router.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


@router.get("/")
def home(request: Request, photographer=Depends(current_photographer)):
    if photographer:
        return go("/dashboard")
    return render(request, "home.html", home_photo=home_photo())


@router.post("/resolve")
async def resolve(request: Request):
    limiter.check(request, "manual", 12)
    form = await form_data(request)
    return go("/c/" + normalize(str(form.get("code", ""))))


@router.get("/login")
def login(request: Request, photographer=Depends(current_photographer)):
    if photographer:
        return go("/dashboard")
    return render(request, "login.html")


@router.post("/login")
async def login_submit(request: Request):
    limiter.check(request, "login", 8)
    form = await form_data(request)
    # Compared as bytes: compare_digest refuses str with non-ASCII characters.
    typed, expected = str(form.get("password", "")).encode(), settings().photographer_password.encode()
    if not secrets.compare_digest(typed, expected):
        return render(request, "login.html", status=400, error=translate(locale(request), "err_password"))
    response = go("/dashboard")
    response.set_cookie(
        "session",
        sign(session_value(), "session"),
        httponly=True,
        samesite="strict",
        max_age=43200,
        secure=settings().app_env != "development",
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    await form_data(request)
    response = go("/login")
    response.delete_cookie("session")
    return response


@router.get("/privacy")
def privacy(request: Request):
    return render(request, "privacy.html")
