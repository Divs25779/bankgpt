"""
Compiles a successful discovery run into a CapabilityArtifact.

This is the seam between "the model discovered a flow" and "the flow is
now a reusable capability" -- see the through-line in the assignment
brief. It deliberately does very little inference: the model's
per-turn ProposedAction already names a target_ref that resolves to a
concrete ObservedElement (role + accessible name), so compiling a Step
is close to a direct copy, not a re-derivation. Where the compiler *does*
add value:

  - it turns the single successful run into a Step list with checkpoints
    (a raw transcript has no checkpoints; the compiler inserts one after
    every state-changing action using the *next* observation's
    distinguishing text, since "the URL/title changed to X" is exactly
    what confirms the step worked),
  - it assigns default risk levels conservatively (any action after which
    a WRITE-shaped verb like "confirm", "submit", "open" appeared in the
    model's own reasoning is flagged risky_reversible pending human
    classification -- see REPORT.md section 6 for why this is a starting
    point, not a substitute for review),
  - it strips the raw transcript's reasoning strings from the artifact
    entirely; they stay in the discovery evidence log only.

The output's review_status is always "draft". Nothing produced here is
approved for unattended irreversible execution until a human promotes it
-- see src/safety/allowlist.py PolicyGate.check_risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.artifact.schema import (
    ActionType,
    BusinessOutcome,
    Checkpoint,
    CapabilityArtifact,
    DialogPolicy,
    InputParam,
    Locator,
    LocatorStrategy,
    OutputField,
    ReviewStatus,
    RiskLevel,
    Step,
    TargetAppRef,
    TextAssertion,
)
from src.llm.base import ActionKind, DialogEvent, Observation, ProposedAction

_RISKY_VERB_HINTS = ("submit", "confirm", "open", "create", "delete", "transfer", "approve")


@dataclass
class DiscoveryTurn:
    """
    One turn of the discovery loop, and everything the compiler needs to
    turn it into a Step.

    `observation` is what the model was SHOWN before deciding -- the
    action's `target_ref` was assigned against THIS observation's
    elements, so target resolution below must look here, not in
    `resulting_observation`. Those are two independent
    capture_observation() calls, usually against two different pages
    (before the action / after it), and refs restart from "e0" on every
    call -- a ref from one has no relationship to a same-named ref in
    the other. This was caught during review by tracing a concrete
    "click Search" step end to end; see REPORT.md section 2.

    `resulting_observation` is the state AFTER `action` executed -- the
    source for THIS step's own checkpoint (not the next turn's).

    `dialog` MUST be populated whenever the loop's dialog handler fired
    while executing `action`, even though discovery auto-handles the
    dialog and the run just continues as if nothing happened. If this
    field is silently left None for a turn that actually triggered a
    dialog, the compiled artifact will be missing Step.expects_dialog,
    and replay -- which does not use a blanket auto-handler -- will hang
    or fail the first time it hits that same dialog for real. See
    DialogEvent's docstring in src/llm/base.py.
    """

    observation: Observation
    action: ProposedAction
    resulting_observation: Observation
    dialog: Optional[DialogEvent] = None


def _risk_for(action: ProposedAction, dialog: Optional[DialogEvent]) -> RiskLevel:
    if dialog is not None:
        # A step that triggers a confirmation dialog is, definitionally,
        # not a pure read -- trust this signal over the keyword heuristic.
        return RiskLevel.RISKY_REVERSIBLE
    if action.kind in (ActionKind.CLICK,) and any(
        hint in action.reasoning.lower() for hint in _RISKY_VERB_HINTS
    ):
        return RiskLevel.RISKY_REVERSIBLE
    return RiskLevel.SAFE


def compile_artifact(
    *,
    artifact_id: str,
    name: str,
    description: str,
    run_id: str,
    target_app: TargetAppRef,
    inputs: list[InputParam],
    outputs: list[OutputField],
    business_outcomes: list[BusinessOutcome],
    turns: list[DiscoveryTurn],
    success_checkpoint: Checkpoint,
) -> CapabilityArtifact:
    """
    `turns` is the discovery run's turns, in order, for the successful
    run only. The caller (agent loop) is responsible for only passing
    turns from a run that actually reached the goal -- the compiler does
    not re-verify success; that's the agent loop's job during discovery
    and the replay executor's job on every subsequent invocation.
    """

    steps: list[Step] = []
    for i, turn in enumerate(turns):
        action = turn.action

        target: Locator | None = None
        if action.target_ref:
            # Resolve against the PRE-action observation -- the one the
            # model actually saw when it picked this target_ref. See
            # DiscoveryTurn's docstring for why resulting_observation
            # would be the wrong (and usually unrelated) element list.
            elem = next((e for e in turn.observation.elements if e.ref == action.target_ref), None)
            name_val = elem.name if elem else action.target_ref
            role_val = elem.role if elem else "unknown"
            target = Locator(
                strategy=LocatorStrategy.ROLE_NAME,
                value={"role": role_val, "name": name_val},
                intent=f"{role_val} '{name_val}' (discovered turn {i})",
            )
        elif action.kind == ActionKind.READ_TEXT and action.text_value:
            # The model chose a label (from Observation.labels) instead
            # of a target_ref -- there was no interactive element to
            # point at, only a labeled data field. LABEL_SIBLING encodes
            # "find this label, then read what's next to it" explicitly
            # -- see LocatorStrategy.LABEL_SIBLING docstring for why
            # this isn't just TEXT with a different value shape.
            target = Locator(
                strategy=LocatorStrategy.LABEL_SIBLING,
                value={"label": action.text_value},
                intent=f"value adjacent to label '{action.text_value}' (discovered turn {i})",
            )

        # This step's own checkpoint comes from THIS turn's own result,
        # not a lookahead to the next turn -- every turn has a
        # resulting_observation, so this always applies, including
        # (especially) the last step, which is usually the actual
        # success page and previously got no checkpoint at all.
        checkpoint = Checkpoint(
            description=f"page advanced to '{turn.resulting_observation.title}'",
            assertion=TextAssertion(contains=turn.resulting_observation.title),
        )

        expects_dialog = None
        if turn.dialog is not None:
            expects_dialog = DialogPolicy(turn.dialog.policy)

        steps.append(
            Step(
                index=i,
                action=ActionType(action.kind.value) if action.kind != ActionKind.DONE else ActionType.WAIT_FOR,
                target=target,
                value_ref=None,  # caller should post-process: map literal text_value -> InputParam.name
                checkpoint=checkpoint,
                risk=_risk_for(action, turn.dialog),
                expects_dialog=expects_dialog,
                notes=None,  # reasoning intentionally dropped -- see module docstring
            )
        )

    return CapabilityArtifact(
        id=artifact_id,
        name=name,
        description=description,
        created_from_run_id=run_id,
        target_app=target_app,
        inputs=inputs,
        outputs=outputs,
        steps=steps,
        business_outcomes=business_outcomes,
        success_checkpoint=success_checkpoint,
        review_status=ReviewStatus.DRAFT,
    )
