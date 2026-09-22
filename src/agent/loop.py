"""
The discovery agent loop -- the one piece of this system where an LLM
makes decisions. Everything it produces gets compiled into a
CapabilityArtifact (src/artifact/compiler.py) and never runs live again
after that: see the through-line in the assignment brief.

CONTRACT (do not violate while filling this in):
  - Every navigation and every action must pass through PolicyGate
    (src/safety/allowlist.py) BEFORE it happens, not after. A model
    proposing something outside the allowlist is not itself a failure --
    it's expected occasionally -- but the action must never reach the
    browser.
  - Every turn (observation, decision, and outcome) is written to
    EvidenceLogger, redacted, before moving to the next turn. If the
    process crashes mid-run, the evidence up to that point must already
    be on disk.
  - Stopping conditions: max_steps, wall-clock timeout, and
    ActionKind.STUCK from the model all end the run. STUCK is not an
    error path here -- it's the discovery-time trigger for escalation
    (src/escalation/manager.py), just like a replay hitting a hard
    failure is the replay-time trigger.
  - DIALOG HANDLING: register `page.on("dialog", ...)` ONCE, before the
    loop starts, with a handler that (a) accepts the dialog -- discovery
    is trying to succeed, so auto-accept is correct here -- and (b)
    appends a DialogEvent (src/llm/base.py) to a per-turn holding list.
    After executing each action, check that list: if it's non-empty, the
    action you just ran triggered a dialog, and the resulting
    DiscoveryTurn (src/artifact/compiler.py) MUST carry it. If you skip
    this, the compiled artifact will be silently missing
    Step.expects_dialog, and replay will hang or fail on the same
    dialog later -- this was caught during design review specifically
    because it's easy to miss. Clear the holding list after each turn.

  1. Loop: capture_observation -> provider.decide_next_action -> policy
     check -> execute against Playwright -> log -> repeat.
  2. Resolve target_ref back to a Playwright locator using the side
     table from capture_observation.
  3. On ActionKind.DONE, hand the full DiscoveryTurn history to
     src/artifact/compiler.compile_artifact.
  4. On ActionKind.STUCK or on an unhandled Playwright exception, call
     into src/escalation/manager.py instead of just raising.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from playwright.sync_api import Error as PlaywrightError

from src.agent.perception import capture_observation
from src.artifact.schema import ActionType
from src.artifact.compiler import DiscoveryTurn
from src.evidence.logger import EvidenceLogger
from src.llm.base import ActionKind, DialogEvent, LLMProvider, ProposedAction
from src.safety.allowlist import PolicyGate, PolicyViolation


@dataclass
class DiscoveryConfig:
    goal: str
    target_url: str
    max_steps: int = 25
    timeout_seconds: int = 300


@dataclass
class DiscoveryResult:
    succeeded: bool
    turns: list[DiscoveryTurn]
    run_id: str
    stuck_reason: str | None = None


def run_discovery(
    config: DiscoveryConfig,
    provider: LLMProvider,
    policy: PolicyGate,
    logger: EvidenceLogger,
    page,  # playwright.sync_api.Page
) -> DiscoveryResult:

    turns: list[DiscoveryTurn] = []
    history: list[ProposedAction] = []

    # DIALOG HANDLING (Blanket accept for discovery)
    dialogs_fired = []

    def handle_dialog(dialog):
        event = DialogEvent(
            message=dialog.message,
            dialog_type=dialog.type,
            policy="accept"
        )
        dialogs_fired.append(event)
        # Log at the moment it fires, not just when it later gets attached
        # to a DiscoveryTurn -- otherwise the raw evidence trail has no
        # record a dialog happened at all, only the *compiled artifact*
        # would show it (via expects_dialog), which defeats the point of
        # "dialogs must be real, not silently swallowed" if the log
        # itself stays silent about it.
        logger.log("dialog", {
            "message": event.message,
            "dialog_type": event.dialog_type,
            "policy": event.policy,
        })
        dialog.accept()

    page.on("dialog", handle_dialog)

    start_time = time.time()

    for step_index in range(config.max_steps):
        if time.time() - start_time > config.timeout_seconds:
            logger.log("error", {"reason": "timeout"})
            return DiscoveryResult(succeeded=False, turns=turns, run_id=logger.run_id, stuck_reason="timeout")

        # 1. OBSERVE
        try:
            obs, ref_to_locator = capture_observation(page)
        except Exception as e:
            logger.log("error", {"reason": "observation_failed", "error": str(e)})
            return DiscoveryResult(succeeded=False, turns=turns, run_id=logger.run_id, stuck_reason=f"observation_failed: {e}")

        # 2. DECIDE
        try:
            action = provider.decide_next_action(config.goal, obs, history)
        except Exception as e:
            logger.log("error", {"reason": "provider_error", "error": str(e)})
            return DiscoveryResult(succeeded=False, turns=turns, run_id=logger.run_id, stuck_reason=f"provider_error: {e}")

        # Log before acting
        logger.log("decide", {
          "action_kind": action.kind.value,
          "target_ref": action.target_ref,
          "text_value": action.text_value,
          "reasoning": action.reasoning,
          "observation_url": obs.url,
          "observation_title": obs.title,
      })
        history.append(action)

        # Check terminal actions
        if action.kind == ActionKind.DONE:
            return DiscoveryResult(succeeded=True, turns=turns, run_id=logger.run_id)

        if action.kind == ActionKind.STUCK:
            logger.log("stuck", {"stuck_reason": action.stuck_reason})
            return DiscoveryResult(succeeded=False, turns=turns, run_id=logger.run_id, stuck_reason=action.stuck_reason)

        # 3. CHECK (PolicyGate)
        try:
            # READ_TEXT and WAIT never touch the page's state, so they are exempt from action-type allowlisting by design.
            if action.kind in (ActionKind.CLICK, ActionKind.TYPE, ActionKind.SELECT, ActionKind.NAVIGATE):
                if action.kind == ActionKind.NAVIGATE and action.text_value:
                    policy.check_navigation(action.text_value)
                else:
                    policy.check_action(ActionType(action.kind.value))
        except PolicyViolation as e:
            logger.log("error", {"reason": "policy_violation", "error": str(e)})
            return DiscoveryResult(succeeded=False, turns=turns, run_id=logger.run_id, stuck_reason=f"policy_violation: {e}")

        # 4. ACT
        dialogs_fired.clear() # MUST clear before executing the action!

        try:
            if action.kind == ActionKind.NAVIGATE:
                if action.text_value:
                    page.goto(action.text_value)
            elif action.kind in (ActionKind.CLICK, ActionKind.TYPE, ActionKind.SELECT):
                if not action.target_ref or action.target_ref not in ref_to_locator:
                    raise ValueError(f"Invalid target_ref: {action.target_ref}")

                loc = ref_to_locator[action.target_ref]

                if action.kind == ActionKind.CLICK:
                    loc.click()
                elif action.kind == ActionKind.TYPE:
                    loc.fill(action.text_value or "")
                elif action.kind == ActionKind.SELECT:
                    loc.select_option(action.text_value or "")
            elif action.kind == ActionKind.WAIT:
                page.wait_for_timeout(2000)
            elif action.kind == ActionKind.READ_TEXT:
                pass
        except PlaywrightError as e:
            logger.log("error", {"reason": "playwright_error", "error": str(e)})
            return DiscoveryResult(succeeded=False, turns=turns, run_id=logger.run_id, stuck_reason=f"playwright_error: {e}")
        except Exception as e:
            logger.log("error", {"reason": "unexpected_error", "error": str(e)})
            return DiscoveryResult(succeeded=False, turns=turns, run_id=logger.run_id, stuck_reason=f"unexpected_error: {e}")

        # Capture resulting dialog and next observation to complete the DiscoveryTurn
        fired_dialog = dialogs_fired[0] if dialogs_fired else None

        try:
            resulting_obs, _ = capture_observation(page)
        except Exception as e:
            logger.log("error", {"reason": "post_action_observation_failed", "error": str(e)})
            return DiscoveryResult(succeeded=False, turns=turns, run_id=logger.run_id, stuck_reason=f"post_action_observation_failed: {e}")

        turns.append(DiscoveryTurn(
            observation=obs,        # <-- the pre-action observation
            action=action,
            resulting_observation=resulting_obs,
            dialog=fired_dialog
        ))

    return DiscoveryResult(succeeded=False, turns=turns, run_id=logger.run_id, stuck_reason="max_steps_exceeded")
