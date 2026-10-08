"""The two reference pages of the workspace: how to use the tool, and the questions it raises."""

from fastapi import APIRouter, Depends, Request

from app.web import render, require_photographer

router = APIRouter()


@router.get("/dashboard/guide")
def guide(request: Request, photographer=Depends(require_photographer)):
    return render(request, "guide.html", photographer)


@router.get("/dashboard/faq")
def faq(request: Request, photographer=Depends(require_photographer)):
    return render(request, "faq.html", photographer)
