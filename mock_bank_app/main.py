"""
Mock target application: a small, deliberately legacy-styled back-office
banking surface for the discovery agent and replay engine to drive.

This app is NOT part of the graded design -- it's a stand-in for a real
core-banking screen (see the assignment's "target application" section).
Its only job is to exercise the interesting problems: a multi-step flow
(search -> detail -> action -> confirmation), runtime error/exceptional
states that must be distinguished from crashes, a native browser confirm()
dialog as an "unexpected interstitial", and a slow-rendering page that
needs a real wait/checkpoint, not a fixed sleep, on the automation side.

Markup choices, documented (see REPORT.md section 4 for the fuller
version): table-based layout throughout, no `data-testid` or other
automation-specific hooks, inline styles only. Inputs DO keep a
`<label for=...>` association -- a deliberate exception, not an
oversight: even genuinely old enterprise apps usually have *some*
minimal accessible name (a label an earlier developer added for basic
usability), and stripping that to zero would turn this into a
screenshot-coordinates exercise, which is a different and larger problem
than the one this assignment is testing.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mock_bank_app.data import (
    NOT_FOUND_ID,
    PERMISSION_DENIED_ID,
    SESSION_EXPIRED_ID,
    SLOW_LOAD_DELAY_SECONDS,
    SLOW_LOAD_ID,
    lookup_member,
    next_account_number,
    validate_deposit_amount,
)

app = FastAPI(title="Mock Core Banking (legacy back-office stand-in)")
templates = Jinja2Templates(directory="mock_bank_app/templates")


@app.get("/", response_class=HTMLResponse)
def search_page(request: Request):
    return templates.TemplateResponse(request, "search.html", {})


@app.get("/search")
def search(member_id: str):
    # Realistic legacy pattern: a search form posts/gets to a search
    # endpoint, which redirects to the resulting record's own URL rather
    # than rendering the result inline. Exercises NAVIGATE-after-search
    # as a distinct step from the initial page load.
    return RedirectResponse(url=f"/member/{member_id}", status_code=302)


@app.get("/member/{member_id}", response_class=HTMLResponse)
def member_detail(request: Request, member_id: str):
    if member_id == NOT_FOUND_ID:
        return templates.TemplateResponse(request, "not_found.html", {"member_id": member_id})

    if member_id == PERMISSION_DENIED_ID:
        return templates.TemplateResponse(request, "permission_denied.html", {"member_id": member_id})

    if member_id == SESSION_EXPIRED_ID:
        return templates.TemplateResponse(request, "session_expired.html", {"member_id": member_id})

    member = lookup_member(member_id)
    if member is None:
        return templates.TemplateResponse(request, "not_found.html", {"member_id": member_id})

    if member_id == SLOW_LOAD_ID:
        # Deliberate artificial delay -- simulates a slow downstream call
        # (e.g. a legacy mainframe lookup) that the real automation must
        # wait out via a dedicated checkpoint, not assume completes
        # instantly. See src/replay/executor.py's WAIT_FOR contract note.
        time.sleep(SLOW_LOAD_DELAY_SECONDS)

    return templates.TemplateResponse(request, "detail.html", {"member": member})


@app.get("/member/{member_id}/new-subaccount", response_class=HTMLResponse)
def new_subaccount_form(request: Request, member_id: str):
    member = lookup_member(member_id)
    if member is None:
        return templates.TemplateResponse(request, "not_found.html", {"member_id": member_id})
    return templates.TemplateResponse(request, "new_subaccount.html", {"member": member, "error": None})


@app.post("/member/{member_id}/new-subaccount", response_class=HTMLResponse)
def submit_subaccount(request: Request, member_id: str, deposit_amount: str = Form(...)):
    member = lookup_member(member_id)
    if member is None:
        return templates.TemplateResponse(request, "not_found.html", {"member_id": member_id})

    error = validate_deposit_amount(deposit_amount)
    if error == "__UNMAPPED__":
        # The deliberately-unmapped error path: text that matches no
        # declared BusinessOutcome or EscalationTrigger in the artifact
        # I compile. This is the alternate "unhandled validation error"
        # HITL scenario -- see mock_bank_app/data.py docstring.
        return templates.TemplateResponse(request, "unmapped_error.html", {"member": member}, status_code=500)
    if error:
        return templates.TemplateResponse(
            request, "new_subaccount.html", {"member": member, "error": error, "deposit_amount": deposit_amount}
        )

    return templates.TemplateResponse(
        request, "confirm.html", {"member": member, "deposit_amount": deposit_amount}
    )


@app.post("/member/{member_id}/subaccount/confirm", response_class=HTMLResponse)
def confirm_subaccount(request: Request, member_id: str, deposit_amount: str = Form(...)):
    member = lookup_member(member_id)
    if member is None:
        return templates.TemplateResponse(request, "not_found.html", {"member_id": member_id})
    account_number = next_account_number()
    return templates.TemplateResponse(
        request,
        "success.html",
        {"member": member, "deposit_amount": deposit_amount, "account_number": account_number},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=5000)
