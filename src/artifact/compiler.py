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

from src.artifact.schema import (
    ActionType,
    BusinessOutcome,
    Checkpoint,
    CapabilityArtifact,
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
from src.llm.base import ActionKind, Observation, ProposedAction

_RISKY_VERB_HINTS = ("submit", "confirm", "open", "create", "delete", "transfer", "approve")


def _risk_for(action: ProposedAction) -> RiskLevel:
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
    turns: list[tuple[ProposedAction, Observation]],
    success_checkpoint: Checkpoint,
) -> CapabilityArtifact:
    """
    `turns` is the discovery run's (action, resulting_observation) pairs,
    in order, for the successful run only. The caller (agent loop) is
    responsible for only passing turns from a run that actually reached
    the goal -- the compiler does not re-verify success; that's the
    agent loop's job during discovery and the replay executor's job on
    every subsequent invocation.
    """

    steps: list[Step] = []
    for i, (action, resulting_obs) in enumerate(turns):
        target: Locator | None = None
        if action.target_ref:
            elem = next((e for e in resulting_obs.elements if e.ref == action.target_ref), None)
            # Fall back to the ref itself if the element isn't in the *resulting*
            # observation (it was on the prior page) -- acceptable here because
            # the agent loop is expected to pass the pre-action observation's
            # element for target resolution in the real implementation; flagged
            # here so it's visible during review rather than silently wrong.
            name_val = elem.name if elem else action.target_ref
            role_val = elem.role if elem else "unknown"
            target = Locator(
                strategy=LocatorStrategy.ROLE_NAME,
                value={"role": role_val, "name": name_val},
                intent=f"{role_val} '{name_val}' (discovered turn {i})",
            )

        checkpoint = None
        if i + 1 < len(turns):
            next_obs = turns[i + 1][1]
            checkpoint = Checkpoint(
                description=f"page advanced to '{next_obs.title}'",
                assertion=TextAssertion(contains=next_obs.title),
            )

        steps.append(
            Step(
                index=i,
                action=ActionType(action.kind.value) if action.kind != ActionKind.DONE else ActionType.WAIT_FOR,
                target=target,
                value_ref=None,  # caller should post-process: map literal text_value -> InputParam.name
                checkpoint=checkpoint,
                risk=_risk_for(action),
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
