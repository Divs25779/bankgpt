"""
Human escalation & handoff -- the part of the brief explicitly called
out as needing to be REAL, not a TODO, even though the operator UI
itself may be mocked.

DESIGN (agreed, build together):
  - The browser runs headed (visible), locally, for both discovery and
    replay. This is what makes "the human operates the same live
    session" true without building any session-sharing infrastructure:
    there is only one browser window, and control of it is a *logical*
    flag this module owns, not a physical resource that needs handing
    off over a network.
  - `IntervenionRequest` is written to evidence (context: goal/artifact
    id, current step, current URL, screenshot, reason) the moment a
    STUCK action or a hard_failure occurs, and a `control` file/flag
    flips to "human". The automation loop must check this flag before
    every action and block if it is not "automation".
  - A minimal CLI (or one-route Flask page) lets the human view the
    request context and, once done acting manually in the visible
    browser, flip control back to "automation" with a `resume` command.
    The manager re-captures an observation at that point so the
    resuming loop isn't acting on stale state.
  - Everything the human does is NOT auto-logged at the DOM level (out
    of scope per the brief's "not a full co-browsing console") but the
    fact of the handoff -- who had control, for how long, and the
    state before/after -- IS logged. That's the real, well-reasoned
    part; a full action-by-action replay of the human's own clicks is
    the acknowledged cut (see REPORT.md section 7).

TODO (build together): implement request_intervention / wait_for_resume.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Controller(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


@dataclass
class InterventionRequest:
    run_id: str
    capability_id: str | None
    step_index: int | None
    reason: str
    current_url: str
    screenshot_path: str | None


class EscalationManager:
    def __init__(self, run_id: str, base_dir: str = "evidence") -> None:
        self.run_id = run_id
        self.base_dir = base_dir
        self._controller = Controller.AUTOMATION

    def request_intervention(self, request: InterventionRequest) -> None:
        raise NotImplementedError("Build together: write request to evidence, flip control flag to HUMAN")

    def current_controller(self) -> Controller:
        return self._controller

    def wait_for_resume(self, poll_interval_seconds: float = 2.0) -> None:
        raise NotImplementedError("Build together: block until a human signals resume")

    def resume(self) -> None:
        raise NotImplementedError("Build together: flip control flag back to AUTOMATION, log handoff duration")
