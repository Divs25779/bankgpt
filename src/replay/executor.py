"""
The production execution path: given a CapabilityArtifact and input
values, run its steps with NO model in the loop, and return a
ReplayResult (see src/artifact/schema.py's ReplayStatus block for the
four-way taxonomy this implements).

CONTRACT (see also schema.py's ReplayStatus comment block, the
authoritative version of the priority order below):
  - PolicyGate.check_action / check_risk before every mutating step --
    same gate discovery uses. READ_TEXT/WAIT_FOR are exempt, matching
    the same rationale as src/agent/loop.py.
  - Dialog handling is PER-STEP: a one-shot handler is registered
    immediately before executing a step with expects_dialog set, and
    removed right after. An UNEXPECTED dialog at any other step is
    caught by a one-shot "unexpected" handler so Playwright never hangs
    waiting for a response that never comes, and is logged as anomalous
    rather than silently accepted.
  - Locator resolution mirrors discovery's matching semantics exactly:
    role_name via page.get_by_role(role, name=name, exact=True);
    label_sibling via page.get_by_text(label, exact=True) then the
    immediate next sibling. Fallbacks are tried in order if the primary
    strategy fails to resolve to at least one element.
  - Checkpoint text assertions are checked against a COMBINED surface
    (title + url + visible body text), not title alone or body alone.
    This matters concretely: my own compiler builds per-step checkpoints
    from the resulting page's TITLE ("page advanced to 'X'"), while
    business_outcomes/escalation_triggers are typically authored against
    BODY text (e.g. "No member found" appears in the mock app's body,
    not its title, which reads "No Member Found" with different
    capitalization). The schema doesn't record which surface an
    assertion was written against, so checking the union of both is the
    only choice that makes both kinds of assertion actually resolvable
    without adding a field nobody has populated. See REPORT.md section 3.
  - At every step, checked in this order: (1) the step's own checkpoint
    -> keep going; (2) any business_outcome -> BUSINESS_OUTCOME, replay
    ends; (3) any escalation_trigger -> escalate and, once resumed,
    re-check from scratch (the human may have fixed the state); (4)
    nothing within timeout_ms -> HARD_FAILURE, itself routed through
    escalation first if artifact.escalate_on_unclassified_hard_failure.
  - On HARD_FAILURE: capture screenshot + accessibility snapshot into
    the run's evidence dir before returning; FailureDetail names the
    step, what was expected, what was observed.
  - After all steps: the artifact's own success_checkpoint is checked
    once more, independent of the last step's own checkpoint -- they are
    logically separate in the schema even though they're often the same
    assertion in practice.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from playwright.sync_api import Error as PlaywrightError

from src.artifact.schema import (
    ActionType,
    CapabilityArtifact,
    DialogPolicy,
    FailureDetail,
    Locator,
    LocatorStrategy,
    ReplayResult,
    ReplayStatus,
    ReviewStatus,
    Step,
    TextAssertion,
)
from src.escalation.manager import EscalationManager, InterventionRequest
from src.evidence.logger import EvidenceLogger
from src.safety.allowlist import PolicyGate, PolicyViolation

_DEFAULT_TIMEOUT_MS = 5000
_POLL_INTERVAL_MS = 250

_POLICY_CHECKED_ACTIONS = (
    ActionType.CLICK, ActionType.TYPE, ActionType.SELECT,
    ActionType.NAVIGATE, ActionType.DISMISS_IF_PRESENT,
)


class LocatorResolutionError(Exception):
    pass


# ---------------------------------------------------------------------------
# Locator resolution -- must mirror src/agent/perception.py's matching
# semantics exactly (exact=True etc.), see module docstring.
# ---------------------------------------------------------------------------

def _resolve_single(page, locator: Locator):
    if locator.strategy == LocatorStrategy.ROLE_NAME:
        role = locator.value.get("role")
        name = locator.value.get("name")
        nth = locator.value.get("nth", 0)
        candidate = page.get_by_role(role, name=name, exact=True).nth(nth)
        return candidate if candidate.count() >= 1 else None

    if locator.strategy == LocatorStrategy.LABEL_SIBLING:
        label = locator.value.get("label")
        label_loc = page.get_by_text(label, exact=True)
        if label_loc.count() == 0:
            return None
        # Climb to the nearest enclosing <td> before taking the sibling.
        # get_by_text(exact=True) resolves to the INNERMOST element with
        # that exact text -- for markup like <td><b>Label:</b></td>,
        # that's the <b>, which has no siblings of its own (it's the
        # only child of its cell). ancestor-or-self::td[1] finds the
        # nearest <td> at or above the match; only THAT element's
        # sibling is the actual value cell. Scoped to <td> deliberately
        # -- see REPORT.md section 7 for why this convention is
        # table-adjacency-specific, not a general "nearby text" solver.
        sibling = label_loc.first.locator("xpath=ancestor-or-self::td[1]/following-sibling::*[1]")
        # Verify NOW, not lazily -- an unresolvable lazy Locator returned
        # here would look like a success until something later calls
        # .inner_text() and hangs for Playwright's full default timeout.
        return sibling if sibling.count() >= 1 else None

    if locator.strategy == LocatorStrategy.TEXT:
        text = locator.value.get("contains") or locator.value.get("text")
        candidate = page.get_by_text(text, exact=False)
        return candidate.first if candidate.count() >= 1 else None

    if locator.strategy == LocatorStrategy.CSS:
        candidate = page.locator(locator.value.get("selector", ""))
        return candidate.first if candidate.count() >= 1 else None

    if locator.strategy == LocatorStrategy.XPATH:
        candidate = page.locator(f"xpath={locator.value.get('expression', '')}")
        return candidate.first if candidate.count() >= 1 else None

    return None


def _resolve_locator(page, locator: Locator):
    """Try the primary strategy, then each fallback in order. Returns
    (resolved_playwright_locator, which_locator_resolved) so the caller
    can log which one actually worked -- a fallback saving a replay is
    exactly the drift signal described in REPORT.md section 4."""
    for candidate in [locator, *locator.fallbacks]:
        try:
            resolved = _resolve_single(page, candidate)
        except PlaywrightError:
            resolved = None
        if resolved is not None:
            return resolved, candidate
    raise LocatorResolutionError(f"No strategy resolved for: {locator.intent}")


# ---------------------------------------------------------------------------
# Assertion evaluation
# ---------------------------------------------------------------------------

def _page_text_surface(page) -> str:
    try:
        body_text = page.inner_text("body")
    except PlaywrightError:
        body_text = ""
    try:
        title = page.title()
    except PlaywrightError:
        title = ""
    return f"{title}\n{page.url}\n{body_text}"


def _text_assertion_holds(page, assertion: TextAssertion) -> bool:
    surface = _page_text_surface(page)
    if assertion.contains and assertion.contains not in surface:
        return False
    if assertion.not_contains and assertion.not_contains in surface:
        return False
    if assertion.matches_regex and not re.search(assertion.matches_regex, surface):
        return False
    return True


def _assertion_holds(page, assertion) -> bool:
    if isinstance(assertion, TextAssertion):
        return _text_assertion_holds(page, assertion)
    if isinstance(assertion, Locator):
        try:
            resolved, _ = _resolve_locator(page, assertion)
            return resolved.count() >= 1
        except LocatorResolutionError:
            return False
    return False


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------

def _execute_action(page, step: Step, resolved, value: Optional[str]) -> None:
    if step.action == ActionType.CLICK:
        resolved.click()
    elif step.action == ActionType.TYPE:
        resolved.fill(value or "")
    elif step.action == ActionType.SELECT:
        resolved.select_option(value or "")
    elif step.action == ActionType.NAVIGATE:
        if value:
            page.goto(value)
    elif step.action == ActionType.DISMISS_IF_PRESENT:
        # "If present" -- absence is a non-event, not a failure.
        if resolved is not None:
            try:
                resolved.click()
            except PlaywrightError:
                pass
    elif step.action in (ActionType.WAIT_FOR, ActionType.READ_TEXT):
        pass  # WAIT_FOR is satisfied entirely by checkpoint polling; READ_TEXT's
              # value is extracted separately after its checkpoint passes.


# ---------------------------------------------------------------------------
# Evidence capture on failure
# ---------------------------------------------------------------------------

def _capture_failure_evidence(page, logger: EvidenceLogger, tag: str, attempt: Optional[int] = None) -> list[str]:
    # attempt is prefixed onto the filename only -- it exists so
    # --repeat N (multi-run stability, see src/replay/cli.py) doesn't
    # have each attempt's failure captures overwrite the previous one.
    # A single run (attempt=None, unchanged default) produces byte-for-
    # byte the same paths as before this was added.
    if attempt is not None:
        tag = f"attempt{attempt}_{tag}"
    paths: list[str] = []
    capture_dir = logger.capture_dir()
    try:
        screenshot_path = f"{capture_dir}/{tag}.png"
        page.screenshot(path=screenshot_path)
        paths.append(screenshot_path)
    except PlaywrightError:
        pass
    try:
        a11y_path = f"{capture_dir}/{tag}.yaml"
        snapshot = page.locator("body").aria_snapshot()
        with open(a11y_path, "w", encoding="utf-8") as f:
            f.write(snapshot)
        paths.append(a11y_path)
    except PlaywrightError:
        pass
    return paths


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def replay_artifact(
    artifact: CapabilityArtifact,
    inputs: dict,
    policy: PolicyGate,
    logger: EvidenceLogger,
    page,  # playwright.sync_api.Page
    escalation: Optional[EscalationManager] = None,
    attempt: Optional[int] = None,  # set only by --repeat N (src/replay/cli.py); see _capture_failure_evidence
) -> ReplayResult:
    start_time = time.time()
    outputs: dict = {}
    artifact_is_approved = artifact.review_status == ReviewStatus.APPROVED
    sensitive_values = [
        str(inputs[p.name]) for p in artifact.inputs if p.pii and p.name in inputs
    ]

    # Registered ONCE for the whole replay run -- see module docstring's
    # dialog-handling note and the bug writeup in REPORT.md section 3.
    # A fresh page.once(...) per step accumulates: .once() only removes
    # itself after firing, so steps that never trigger a dialog leave
    # their handler registered indefinitely, and by the time a real
    # dialog appears several stale handlers race to handle the same
    # dialog object -- exactly what caused "Cannot dismiss dialog which
    # is already handled!" in testing. One persistent handler driven by
    # mutable state (which step is "current") avoids this categorically.
    _current = {"policy": None, "step_index": None}

    def _dialog_handler(dialog):
        if _current["policy"] is not None:
            (dialog.accept() if _current["policy"] == DialogPolicy.ACCEPT else dialog.dismiss())
        else:
            logger.log("unexpected_dialog", {
                "step": _current["step_index"], "message": dialog.message, "dialog_type": dialog.type,
            })
            dialog.dismiss()

    page.on("dialog", _dialog_handler)

    steps_completed = 0
    for step in artifact.steps:
        _current["policy"] = step.expects_dialog
        _current["step_index"] = step.index
        # --- policy gate (mirrors discovery; see module docstring) ---
        if step.action in _POLICY_CHECKED_ACTIONS:
            try:
                policy.check_action(step.action)
                policy.check_risk(step.risk, artifact_is_approved)
            except PolicyViolation as e:
                logger.log("error", {"reason": "policy_violation", "step": step.index, "error": str(e)},
                           sensitive_values=sensitive_values)
                evidence = _capture_failure_evidence(page, logger, f"step{step.index}_policy_violation", attempt=attempt)
                return ReplayResult(
                    status=ReplayStatus.HARD_FAILURE,
                    artifact_id=artifact.id, artifact_version=artifact.version,
                    outputs=outputs, steps_completed=steps_completed,
                    failure=FailureDetail(step_index=step.index, expected="policy allows this step",
                                           observed=str(e), evidence_paths=evidence),
                    duration_ms=int((time.time() - start_time) * 1000),
                )

        # --- resolve target and act (dialog handling is the persistent
        # handler registered above, driven by _current) ---
        resolved = None
        used_locator = None
        try:
            if step.target is not None:
                resolved, used_locator = _resolve_locator(page, step.target)
            value = inputs.get(step.value_ref) if step.value_ref else None
            _execute_action(page, step, resolved, value)
        except (PlaywrightError, LocatorResolutionError) as e:
            logger.log("error", {"reason": "action_failed", "step": step.index, "error": str(e)},
                       sensitive_values=sensitive_values)
            evidence = _capture_failure_evidence(page, logger, f"step{step.index}_action_failed", attempt=attempt)
            return ReplayResult(
                status=ReplayStatus.HARD_FAILURE,
                artifact_id=artifact.id, artifact_version=artifact.version,
                outputs=outputs, steps_completed=steps_completed,
                failure=FailureDetail(step_index=step.index,
                                       expected=step.target.intent if step.target else "action to succeed",
                                       observed=str(e), evidence_paths=evidence),
                duration_ms=int((time.time() - start_time) * 1000),
            )

        logger.log("act", {
            "step": step.index, "action": step.action.value,
            "locator_used": used_locator.strategy.value if used_locator else None,
            "locator_was_fallback": bool(used_locator and step.target and used_locator is not step.target),
        }, sensitive_values=sensitive_values)

        # --- evaluate outcome: checkpoint -> business_outcome -> escalation -> hard_failure ---
        status, matched = _evaluate_step(page, step, artifact)

        def _try_escalate_once(reason_text: str):
            """One escalation attempt + re-check. Returns the (possibly
            updated) (status, matched) -- never assumes the human fixed
            anything; the caller always re-derives status from a fresh
            _evaluate_step call, never from the fact that resume happened."""
            evidence_paths = _capture_failure_evidence(page, logger, f"step{step.index}_{status}", attempt=attempt)
            escalation.request_intervention(InterventionRequest(
                run_id=logger.run_id, capability_id=artifact.id, step_index=step.index,
                reason=reason_text, current_url=page.url,
                screenshot_path=evidence_paths[0] if evidence_paths else None,
            ))
            escalation.wait_for_resume()
            return _evaluate_step(page, step, artifact)

        # Escalate AT MOST ONCE per step -- if the human's resume doesn't
        # actually fix the condition, that must surface as a clear
        # failure, not loop forever or (worse) be silently treated as
        # success. escalated_this_step tracks whether we already tried,
        # purely so the final failure message can say which happened.
        escalated_this_step = False
        if status == "escalated" and escalation is not None:
            status, matched = _try_escalate_once(f"{matched.name}: {matched.reason}")
            escalated_this_step = True
        elif status == "hard_failure" and escalation is not None and artifact.escalate_on_unclassified_hard_failure:
            checkpoint_desc = step.checkpoint.description if step.checkpoint else "n/a"
            status, matched = _try_escalate_once(
                f"Unclassified hard failure: checkpoint '{checkpoint_desc}' not met and no "
                f"declared business_outcome/escalation_trigger matched."
            )
            escalated_this_step = True

        # From here, status is EXHAUSTIVELY one of: "ok", "business_outcome",
        # "escalated", "hard_failure" -- every branch is handled explicitly;
        # nothing falls through to the success path without status == "ok".

        if status == "business_outcome":
            logger.log("outcome", {"step": step.index, "outcome": matched.name}, sensitive_values=sensitive_values)
            return ReplayResult(
                status=ReplayStatus.BUSINESS_OUTCOME,
                artifact_id=artifact.id, artifact_version=artifact.version,
                outputs=outputs, outcome_name=matched.name,
                steps_completed=steps_completed, duration_ms=int((time.time() - start_time) * 1000),
            )

        if status != "ok":
            # Still "escalated" (human resumed but didn't fix it, or no
            # EscalationManager was available to begin with) or still
            # "hard_failure" after one escalation attempt -- all become a
            # hard failure here, never a silent pass-through.
            evidence = _capture_failure_evidence(page, logger, f"step{step.index}_hard_failure_final", attempt=attempt)
            if status == "escalated" and escalation is None:
                observed = f"escalation_trigger '{matched.name}' matched but no EscalationManager was provided"
            elif escalated_this_step:
                observed = f"condition not resolved after human intervention (still: {status})"
            else:
                observed = _page_text_surface(page)[:300]
            return ReplayResult(
                status=ReplayStatus.HARD_FAILURE,
                artifact_id=artifact.id, artifact_version=artifact.version,
                outputs=outputs, steps_completed=steps_completed,
                failure=FailureDetail(
                    step_index=step.index,
                    expected=step.checkpoint.description if step.checkpoint else "step to complete",
                    observed=observed, evidence_paths=evidence,
                ),
                duration_ms=int((time.time() - start_time) * 1000),
            )

        # status == "ok" -- genuinely verified, not assumed. Extract output if this step produces one.
        if step.action == ActionType.READ_TEXT and step.output_ref and resolved is not None:
            try:
                outputs[step.output_ref] = resolved.inner_text().strip()
            except PlaywrightError as e:
                logger.log("error", {"reason": "output_extraction_failed", "step": step.index, "error": str(e)})

        steps_completed += 1
        logger.log("checkpoint", {"step": step.index, "result": "ok"}, sensitive_values=sensitive_values)

    # --- all steps completed: verify the artifact's own success_checkpoint ---
    if _poll_assertion(page, artifact.success_checkpoint.assertion, artifact.success_checkpoint.timeout_ms):
        logger.log("success", {"outputs": list(outputs.keys())}, sensitive_values=sensitive_values)
        return ReplayResult(
            status=ReplayStatus.SUCCESS,
            artifact_id=artifact.id, artifact_version=artifact.version,
            outputs=outputs, steps_completed=steps_completed,
            duration_ms=int((time.time() - start_time) * 1000),
        )

    evidence = _capture_failure_evidence(page, logger, "success_checkpoint_failed", attempt=attempt)
    return ReplayResult(
        status=ReplayStatus.HARD_FAILURE,
        artifact_id=artifact.id, artifact_version=artifact.version,
        outputs=outputs, steps_completed=steps_completed,
        failure=FailureDetail(
            step_index=len(artifact.steps) - 1,
            expected=artifact.success_checkpoint.description,
            observed=_page_text_surface(page)[:300],
            evidence_paths=evidence,
        ),
        duration_ms=int((time.time() - start_time) * 1000),
    )


def _poll_assertion(page, assertion, timeout_ms: int) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while True:
        if _assertion_holds(page, assertion):
            return True
        if time.time() >= deadline:
            return _assertion_holds(page, assertion)
        page.wait_for_timeout(_POLL_INTERVAL_MS)


def _evaluate_step(page, step: Step, artifact: CapabilityArtifact):
    """Poll up to step.checkpoint.timeout_ms, checking in priority order:
    step checkpoint -> business_outcomes -> escalation_triggers -> hard_failure.
    Returns (status, matched_outcome_or_trigger_or_None)."""
    timeout_ms = step.checkpoint.timeout_ms if step.checkpoint else _DEFAULT_TIMEOUT_MS
    deadline = time.time() + timeout_ms / 1000

    while True:
        if step.checkpoint and _assertion_holds(page, step.checkpoint.assertion):
            return "ok", None
        for outcome in artifact.business_outcomes:
            if _assertion_holds(page, outcome.detection):
                return "business_outcome", outcome
        for trigger in artifact.escalation_triggers:
            if _assertion_holds(page, trigger.detection):
                return "escalated", trigger
        if time.time() >= deadline:
            # one final check pass at/after the deadline before giving up
            if step.checkpoint and _assertion_holds(page, step.checkpoint.assertion):
                return "ok", None
            for outcome in artifact.business_outcomes:
                if _assertion_holds(page, outcome.detection):
                    return "business_outcome", outcome
            for trigger in artifact.escalation_triggers:
                if _assertion_holds(page, trigger.detection):
                    return "escalated", trigger
            return "hard_failure", None
        page.wait_for_timeout(_POLL_INTERVAL_MS)
