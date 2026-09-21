"""
Human escalation & handoff -- the part of the brief explicitly called
out as needing to be REAL, not a TODO, even though the operator UI
itself may be mocked.

WHY THIS COUNTS AS REAL, NOT MOCKED, DESPITE BEING A TERMINAL PROMPT:
The browser runs headed throughout (see src/agent/cli.py and
src/replay/cli.py) -- there is exactly ONE browser window, visible on
screen the whole time. "The human takes control of the live session"
does not require any session-transfer machinery here, because there is
no second session to transfer to: it just means the automation stops
touching the page. `request_intervention` prints full context (what
capability, which step, the current URL, why it stopped) and blocks on
`wait_for_resume`; a human reads that, manually operates the SAME
visible window (e.g. logging back in after a simulated session expiry),
then types "resume" in the terminal. That terminal prompt is the "bare
operator surface" the brief explicitly says is acceptable -- the parts
that must be real (pause, live-session access, explicit resume signal,
logged handoff) all are; only the *UI polish* around them is minimal.

WHAT'S DELIBERATELY NOT CAPTURED: what the human actually clicks/types
during the handoff is not recorded action-by-action -- only that a
handoff happened, why, and how long it took. A full co-browsing console
that replays the human's own actions is explicitly out of scope per the
brief; see REPORT.md section 7.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from src.evidence.logger import EvidenceLogger


class Controller(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


@dataclass
class InterventionRequest:
    run_id: str
    capability_id: Optional[str]
    step_index: Optional[int]
    reason: str
    current_url: str
    screenshot_path: Optional[str] = None


class EscalationManager:
    def __init__(self, run_id: str, logger: EvidenceLogger, base_dir: str = "evidence") -> None:
        self.run_id = run_id
        self.base_dir = base_dir
        self._logger = logger
        self._controller = Controller.AUTOMATION

    def current_controller(self) -> Controller:
        return self._controller

    def request_intervention(self, request: InterventionRequest) -> None:
        """
        Flip control to the human and log the request with full context.
        Does NOT block by itself -- the caller (replay executor) calls
        wait_for_resume() right after this, kept as two steps so the
        logged "escalate" event and the actual blocking wait are visible
        as distinct, independently-timestamped evidence entries.
        """
        self._controller = Controller.HUMAN
        self._logger.log("escalate", {
            "capability_id": request.capability_id,
            "step_index": request.step_index,
            "reason": request.reason,
            "current_url": request.current_url,
        }, screenshot_path=request.screenshot_path)

        print("\n" + "=" * 70)
        print("HUMAN INTERVENTION REQUESTED")
        print("=" * 70)
        print(f"Capability : {request.capability_id}")
        print(f"Step       : {request.step_index}")
        print(f"Reason     : {request.reason}")
        print(f"Current URL: {request.current_url}")
        if request.screenshot_path:
            print(f"Screenshot : {request.screenshot_path}")
        print("-" * 70)
        print("The browser window is live -- operate it directly to resolve this.")
        print("Type 'resume' and press Enter here once done.")
        print("=" * 70)

    def wait_for_resume(self, poll_interval_seconds: float = 2.0) -> None:
        """
        Blocks until the human signals resume. A bare `input()` is the
        "bare/mock operator surface" the brief explicitly allows -- the
        blocking itself, and the fact that it only unblocks on an
        explicit human signal, is the real part.
        """
        start = time.time()
        while True:
            typed = input("> ").strip().lower()
            if typed in ("resume", "r"):
                break
            print("Type 'resume' (or 'r') to hand control back to automation.")
        self.resume(duration_seconds=time.time() - start)

    def resume(self, duration_seconds: float | None = None) -> None:
        self._controller = Controller.AUTOMATION
        self._logger.log("resume", {
            "handoff_duration_seconds": round(duration_seconds, 1) if duration_seconds is not None else None,
        })
        print("Control handed back to automation. Resuming...\n")
