"""
Unit tests for src/artifact/compiler.py.

These need no browser and no live app -- compile_artifact operates
purely on Pydantic objects (DiscoveryTurn, ProposedAction, Observation).
That's exactly why this class of bug is cheap to catch here: the
pre/post-observation mixup (see test below) was a pure object-graph
mistake that a two-line test would have caught before it ever needed a
live discovery run to surface. See REPORT.md section 3 for the original
bug writeup and section 7 for why this suite exists at all.
"""

from __future__ import annotations

from src.artifact.compiler import DiscoveryTurn, compile_artifact
from src.artifact.schema import (
    Checkpoint,
    DialogPolicy,
    LocatorStrategy,
    OutputField,
    ParamType,
    RiskLevel,
    TargetAppRef,
    TextAssertion,
    BusinessOutcome,
    EscalationTrigger,
)
from src.llm.base import ActionKind, DialogEvent, ObservedElement, Observation, ProposedAction

_TARGET_APP = TargetAppRef(app_id="test-app", base_url="http://x")


def _obs(url: str, title: str, elements: list[tuple[str, str, str]]) -> Observation:
    return Observation(
        url=url,
        title=title,
        elements=[ObservedElement(ref=ref, role=role, name=name) for ref, role, name in elements],
    )


def _compile(turns, **overrides):
    defaults = dict(
        artifact_id="t", name="t", description="t", run_id="run",
        target_app=_TARGET_APP, inputs=[], outputs=[], business_outcomes=[],
        success_checkpoint=Checkpoint(description="done", assertion=TextAssertion(contains="x")),
    )
    defaults.update(overrides)
    return compile_artifact(turns=turns, **defaults)


def test_target_resolves_from_pre_action_observation_not_resulting_observation():
    """
    Regression test for the original compiler bug: a step's target used
    to be resolved by looking up action.target_ref in the turn's
    RESULTING (post-action) observation -- but that ref was assigned by
    a completely separate capture_observation() call, usually against a
    different page, with its own fresh ref numbering starting at "e0"
    again. Reusing the same ref string ("e1") for two unrelated elements
    across pre/post observations reproduces exactly the scenario that
    silently broke: the compiled Locator must reflect the PRE-action
    element (what the model actually saw and picked), never the
    post-action one.
    """
    pre_obs = _obs("http://x/search", "Search Page", [
        ("e0", "textbox", "Member ID:"),
        ("e1", "button", "Search"),
    ])
    post_obs = _obs("http://x/detail", "Member Detail", [
        ("e1", "link", "Totally Unrelated Element"),  # same ref, different page, different element
    ])
    action = ProposedAction(kind=ActionKind.CLICK, target_ref="e1", reasoning="click search")
    turn = DiscoveryTurn(observation=pre_obs, action=action, resulting_observation=post_obs)

    artifact = _compile([turn], success_checkpoint=Checkpoint(
        description="done", assertion=TextAssertion(contains="Member Detail")))

    step = artifact.steps[0]
    assert step.target.strategy == LocatorStrategy.ROLE_NAME
    assert step.target.value == {"role": "button", "name": "Search"}, (
        "target should come from the PRE-action observation; getting "
        "'Totally Unrelated Element' here means the old bug is back"
    )


def test_checkpoint_uses_this_turns_own_resulting_observation():
    """
    Regression test for the paired off-by-one bug: a step's checkpoint
    used to be built from the NEXT turn's resulting_observation instead
    of its own, meaning every checkpoint checked the wrong page state
    and the last step got no checkpoint at all (no "next" turn to look
    ahead to).
    """
    obs0 = _obs("http://x/a", "Page A", [("e0", "button", "Next")])
    obs1_result = _obs("http://x/b", "Page B", [])
    obs2_result = _obs("http://x/c", "Page C", [])

    turn0 = DiscoveryTurn(
        observation=obs0,
        action=ProposedAction(kind=ActionKind.CLICK, target_ref="e0", reasoning="go"),
        resulting_observation=obs1_result,
    )
    turn1 = DiscoveryTurn(
        observation=obs1_result,
        action=ProposedAction(kind=ActionKind.CLICK, target_ref=None, reasoning="go again"),
        resulting_observation=obs2_result,
    )

    artifact = _compile([turn0, turn1], success_checkpoint=Checkpoint(
        description="done", assertion=TextAssertion(contains="Page C")))

    assert artifact.steps[0].checkpoint.assertion.contains == "Page B"
    assert artifact.steps[1].checkpoint.assertion.contains == "Page C"  # last step now gets its own checkpoint


def test_dialog_event_wires_expects_dialog_and_forces_risky_reversible():
    obs = _obs("http://x/confirm", "Confirm", [("e0", "button", "Confirm")])
    result_obs = _obs("http://x/done", "Done", [])
    action = ProposedAction(kind=ActionKind.CLICK, target_ref="e0", reasoning="confirm it")
    dialog = DialogEvent(message="Are you sure?", dialog_type="confirm", policy="accept")
    turn = DiscoveryTurn(observation=obs, action=action, resulting_observation=result_obs, dialog=dialog)

    artifact = _compile([turn], success_checkpoint=Checkpoint(
        description="done", assertion=TextAssertion(contains="Done")))

    step = artifact.steps[0]
    assert step.expects_dialog == DialogPolicy.ACCEPT
    assert step.risk == RiskLevel.RISKY_REVERSIBLE, "a dialog-triggering step must never be classified safe"
    assert artifact.max_risk_level == RiskLevel.RISKY_REVERSIBLE


def test_no_dialog_means_expects_dialog_stays_none():
    obs = _obs("http://x/a", "A", [("e0", "button", "Just Click")])
    result_obs = _obs("http://x/b", "B", [])
    action = ProposedAction(kind=ActionKind.CLICK, target_ref="e0", reasoning="just clicking")
    turn = DiscoveryTurn(observation=obs, action=action, resulting_observation=result_obs)

    artifact = _compile([turn], success_checkpoint=Checkpoint(
        description="done", assertion=TextAssertion(contains="B")))

    assert artifact.steps[0].expects_dialog is None


def test_label_based_read_compiles_to_label_sibling_locator():
    """A read_text action with no target_ref (the model picked a label
    instead, per the perception.py design) must compile to
    LABEL_SIBLING, not TEXT -- see LocatorStrategy's docstring for why
    these are deliberately distinct strategies."""
    obs = _obs("http://x/detail", "Member Detail", [])
    action = ProposedAction(kind=ActionKind.READ_TEXT, target_ref=None,
                             text_value="Savings Balance:", reasoning="read the balance")
    turn = DiscoveryTurn(observation=obs, action=action, resulting_observation=obs)

    artifact = _compile(
        [turn],
        outputs=[OutputField(name="balance", type=ParamType.STRING, description="d", source_step=0)],
        success_checkpoint=Checkpoint(description="done", assertion=TextAssertion(contains="Member Detail")),
    )

    step = artifact.steps[0]
    assert step.target.strategy == LocatorStrategy.LABEL_SIBLING
    assert step.target.value == {"label": "Savings Balance:"}


def test_business_outcomes_and_escalation_triggers_pass_through():
    """Regression test for a real gap: compile_artifact originally had
    no escalation_triggers parameter at all, so CapabilityArtifact.escalation_triggers
    silently stayed [] forever regardless of what the caller intended."""
    obs = _obs("http://x/a", "A", [])
    action = ProposedAction(kind=ActionKind.CLICK, target_ref=None, reasoning="noop")
    turn = DiscoveryTurn(observation=obs, action=action, resulting_observation=obs)

    outcome = BusinessOutcome(name="not_found", description="d",
                               detection=TextAssertion(contains="x"), is_success=False)
    trigger = EscalationTrigger(name="expired", description="d",
                                 detection=TextAssertion(contains="y"), reason="r")

    artifact = _compile(
        [turn],
        business_outcomes=[outcome],
        escalation_triggers=[trigger],
        success_checkpoint=Checkpoint(description="done", assertion=TextAssertion(contains="A")),
    )

    assert [o.name for o in artifact.business_outcomes] == ["not_found"]
    assert [t.name for t in artifact.escalation_triggers] == ["expired"]
