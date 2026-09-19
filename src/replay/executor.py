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
  - DIALOG HANDLING IS PER-STEP, NOT GLOBAL: if step.expects_dialog is
    set (src/artifact/schema.py DialogPolicy), register a ONE-SHOT
    `page.on("dialog", ...)` handler that resolves it per that policy
    BEFORE executing this step, then remove the handler immediately
    after. Do NOT install a blanket handler for the whole replay run the
    way discovery does (src/agent/loop.py) -- discovery can safely
    auto-accept anything because it's exploring toward a goal it wants
    to reach; replay must only ever resolve the EXACT dialogs the
    artifact declares expecting, at the EXACT step that triggers them.
    An unexpected dialog appearing at any other step is a hard_failure
    (or escalation trigger, if declared), not something to blanket-accept.
  - For each step's target Locator: try `strategy` first, then each
    `fallbacks` entry in order. Record in the evidence log which one
    resolved -- that's the signal you'd use in a real system to flag an
    artifact as "drifting" (primary locator failing, fallback saving it)
    well before it fully breaks. This is the hook src/artifact schema's
    `version_fingerprint` field is for -- see REPORT.md section 4.
  - LOCATOR RESOLUTION MUST MATCH DISCOVERY'S MATCHING SEMANTICS:
    src/agent/perception.py resolves role_name locators via
    `page.get_by_role(role, name=name, exact=True)`. Reconstruct the
    SAME call here (exact=True) for the `role_name` strategy -- Playwright's
    default name matching is substring/case-insensitive, so dropping
    exact=True here could resolve a saved artifact to a *different*
    element than the one discovery actually recorded, silently.
  - LABEL_SIBLING RESOLUTION: a Locator with strategy=label_sibling and
    value={"label": "Savings Balance:"} means: find the element whose
    text exactly matches that label, then read the text of its
    immediate next sibling in DOM order. Concretely, with Playwright:
    `page.get_by_text(label, exact=True).locator("xpath=following-sibling::*[1]").inner_text()`
    (or the CSS/XPath equivalent for the surface). This only works when
    label and value are DOM-adjacent siblings -- see LocatorStrategy.LABEL_SIBLING
    docstring and REPORT.md section 7 for the scope of this convention.
  - Some steps carry an ActionType.WAIT_FOR that is distinct from the
    preceding step's own checkpoint: a navigation can succeed near-
    instantly while the specific content the goal cares about (e.g. a
    balance populated by a slow async call) still needs patient polling.
    Keep these two checks separately evaluable in the result/evidence --
    if replay fails, the caller should be able to tell "did we even
    land on the right page" from "did the slow content ever appear",
    not just "something timed out".
  - Checkpoint evaluation priority at EVERY checkpoint/timeout, in order
    (see schema.py's ReplayStatus block for the authoritative version):
      1. success_checkpoint matched          -> SUCCESS
      2. a declared BusinessOutcome matched    -> BUSINESS_OUTCOME
      3. a declared EscalationTrigger matched  -> call
         EscalationManager.request_intervention, block on
         wait_for_resume, then re-evaluate from the resulting state
         (do NOT assume the human fixed it -- re-check like any other turn)
      4. nothing matched within timeout        -> HARD_FAILURE; if
         artifact.escalate_on_unclassified_hard_failure is true (the
         default), route to EscalationManager the same as step 3 before
         returning HARD_FAILURE to the caller -- give a human a chance
         at genuinely unanticipated states too, not just declared ones.
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
