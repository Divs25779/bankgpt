"""
In-memory "core banking" data for the mock target app.

Error/exceptional-state injection is keyed off member_id and is entirely
deterministic ON PURPOSE -- a real session timeout is time-based and
non-reproducible, which would make it useless for a graded evidence
folder. Triggering it via a specific, documented member_id means the
exact same replay can be re-run to reproduce the exact same exceptional
state every time. This determinism-for-testability trade-off is called
out in REPORT.md section 7.

Reserved member IDs (all other unregistered IDs fall through to "not found"):
    00000  -> member not found (declared BusinessOutcome, is_success=False)
    00001  -> permission denied (declared BusinessOutcome, is_success=False)
    00002  -> session expired mid-flow (declared EscalationTrigger)
    00003  -> valid member, but detail page takes ~6s to render (slow-load /
              WAIT_FOR checkpoint scenario)
    12345, 67890 -> valid members, normal-speed detail page
"""

from __future__ import annotations

from dataclasses import dataclass

NOT_FOUND_ID = "00000"
PERMISSION_DENIED_ID = "00001"
SESSION_EXPIRED_ID = "00002"
SLOW_LOAD_ID = "00003"

SLOW_LOAD_DELAY_SECONDS = 6

# A deposit amount that deliberately produces an UNMAPPED error message --
# i.e. text that does not match any declared BusinessOutcome or
# EscalationTrigger. This is the alternate "unhandled validation error"
# hard-failure scenario mentioned as an option alongside session expiry;
# kept available so both suggested HITL triggers can be demonstrated,
# even though the artifact I build treats session expiry as the primary,
# declared escalation path.
UNMAPPED_ERROR_AMOUNT = "999999"


@dataclass
class Member:
    member_id: str
    name: str
    savings_balance: str


_MEMBERS: dict[str, Member] = {
    "12345": Member(member_id="12345", name="J. Rao", savings_balance="$4,213.09"),
    "67890": Member(member_id="67890", name="A. Fernandes", savings_balance="$918.42"),
    SLOW_LOAD_ID: Member(member_id=SLOW_LOAD_ID, name="S. Iyer", savings_balance="$2,004.50"),
}

_next_account_number = 100000


def lookup_member(member_id: str) -> Member | None:
    return _MEMBERS.get(member_id)


def next_account_number() -> str:
    global _next_account_number
    _next_account_number += 1
    return str(_next_account_number)


def validate_deposit_amount(raw: str) -> str | None:
    """Returns an error message string if invalid, else None."""
    if raw == UNMAPPED_ERROR_AMOUNT:
        return "__UNMAPPED__"  # sentinel: caller renders the unmapped-error page, not a normal validation message
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        return "Deposit amount must be a number."
    if amount <= 0:
        return "Deposit amount must be greater than zero."
    if amount > 50000:
        return "Deposit amount exceeds the maximum allowed for online sub-account opening ($50,000)."
    return None
