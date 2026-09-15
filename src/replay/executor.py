"""
The production execution path: given a CapabilityArtifact and input
values, run its steps with NO model in the loop, and return a
ReplayResult (see src/artifact/schema.py for the three-way status
taxonomy this must respect: success / business_outcome / hard_failure --
plus escalated, which this module routes to
src/escalation/manager.py rather than handling itself).

CONTRACT:
  - Before every step: PolicyGate.check_action / check_risk. Same gate
    the discovery loop uses -- see src/safety/allowlist.py docstring for
    why this must not be a discovery-only concern.
  - For each step's target Locator: try `strategy` first, then each
    `fallbacks` entry in order. Record in the evidence log which one
    resolved -- that's the signal you'd use in a real system to flag an
    artifact as "drifting" (primary locator failing, fallback saving it)
    well before it fully breaks. This is the hook src/artifact schema's
    `version_fingerprint` field is for for -- see REPORT.md section 4.
  - After every state-changing step: evaluate `step.checkpoint`. Timeout
    on a checkpoint is NOT automatically a hard_failure -- check
    `artifact.business_outcomes` first (e.g. "no such member" produces a
    page that never reaches the expected checkpoint on purpose). Only if
    nothing matches a declared outcome do we escalate to hard_failure.
  - On hard_failure: capture screenshot + accessibility snapshot into
    the run's evidence dir before returning -- the FailureDetail's
    evidence_paths must point at real files, not be empty.

TODO (build together): implement `replay_artifact`.
"""

from __future__ import annotations

from src.artifact.schema import CapabilityArtifact, ReplayResult
from src.evidence.logger import EvidenceLogger
from src.safety.allowlist import PolicyGate


def replay_artifact(
    artifact: CapabilityArtifact,
    inputs: dict,
    policy: PolicyGate,
    logger: EvidenceLogger,
    page,  # playwright.sync_api.Page
) -> ReplayResult:
    raise NotImplementedError("Build together: locator resolution + checkpoint FSM + outcome taxonomy")
