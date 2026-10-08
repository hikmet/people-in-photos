"""Activity logs: the event timeline, background jobs, and the decision records behind them."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.i18n import fail
from app.models import Card, Consent, Event, Job, now
from app.services import found
from app.timeline import describe
from app.web import form_data, go, pdf_response, render, require_photographer

router = APIRouter()


@router.get("/dashboard/inbox")
def inbox(request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    events = list(db.scalars(select(Event).order_by(Event.created_at.desc()).limit(100)))
    jobs = list(
        db.scalars(
            select(Job).where(Job.status.in_(["queued", "running", "failed"])).order_by(Job.created_at.desc())
        )
    )
    days = describe(db, events, request.state.lang)
    return render(request, "inbox.html", photographer, days=days, jobs=jobs)


@router.post("/dashboard/jobs/{identity}/retry")
async def retry(identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    await form_data(request)
    job = found(db, Job, identity)
    if job.status != "failed":
        raise fail(409, "err_retry")
    job.status, job.attempts, job.run_after = "queued", 0, now()
    db.commit()
    return go("/dashboard/inbox")


@router.get("/dashboard/consents/{identity}")
def consent_detail(identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    record = found(db, Consent, identity)
    card = found(db, Card, record.card_id)
    previous = found(db, Consent, record.supersedes_id) if record.supersedes_id else None
    replacement = db.scalar(select(Consent).where(Consent.supersedes_id == record.id))
    return render(
        request,
        "consent.html",
        photographer,
        record=record,
        card=card,
        previous=previous,
        replacement=replacement,
        connection=record.snapshot["connection"],
    )


@router.get("/dashboard/consents/{identity}/pdf")
def photographer_pdf(identity: str, request: Request, photographer=Depends(require_photographer), db: Session = Depends(get_db)):
    record = found(db, Consent, identity)
    return pdf_response(record, request.state.lang)
