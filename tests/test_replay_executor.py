"""
Tests for src/replay/executor.py that need a REAL Playwright page (the
`page` fixture comes from the pytest-playwright plugin -- no extra
conftest needed once it's installed).

These are deliberately NOT mocked. The two bugs they guard against were
both genuine surprises about how a real browser behaves, not logic
errors -- a test built on my own assumptions about get_by_text() would
have encoded the same wrong assumption and passed regardless. Using
page.set_content(...) with tiny controlled HTML fixtures keeps these
fast and independent of mock_bank_app / a live server, while still
exercising real Playwright/DOM behavior. See REPORT.md section 3 for
the original bug writeups.
"""

from __future__ import annotations

import pytest

from src.artifact.schema import (
    ActionType,
    CapabilityArtifact,
    Checkpoint,
    EscalationTrigger,
    Locator,
    LocatorStrategy,
    ReviewStatus,
    RiskLevel,
    Step,
    TargetAppRef,
    TextAssertion,
)
from src.evidence.logger import EvidenceLogger
from src.replay.executor import (
    LocatorResolutionError,
    _resolve_locator,
    _text_assertion_holds,
    replay_artifact,
)
from src.safety.allowlist import AllowlistConfig, PolicyGate

_LEGACY_DETAIL_HTML = """
<html><head><title>Member Detail</title></head>
<body>
<table>
  <tr><td><b>Member ID:</b></td><td>12345</td></tr>
  <tr><td><b>Savings Balance:</b></td><td>$4,213.09</td></tr>
</table>
</body></html>
"""


def test_label_sibling_resolves_to_the_value_cell_not_the_label_wrapper(page):
    """
    Regression test for the exact bug found in testing: get_by_text(exact=True)
    resolves to the INNERMOST element with that exact text -- for
    <td><b>Label:</b></td>, that's the <b>, which has no siblings of its
    own (it's the only child of its <td>). Without climbing to the
    enclosing <td> first, "the next sibling" resolves to nothing and
    Locator.inner_text() hangs for Playwright's full default timeout.
    """
    page.set_content(_LEGACY_DETAIL_HTML)
    locator = Locator(strategy=LocatorStrategy.LABEL_SIBLING, value={"label": "Savings Balance:"}, intent="test")

    resolved, used = _resolve_locator(page, locator)

    assert resolved.inner_text().strip() == "$4,213.09"


def test_label_sibling_raises_cleanly_when_label_is_absent(page):
    """Must fail FAST with a clear exception, not hang -- see the same
    bug: the old code returned a lazy, unverified Locator that only
    failed much later when something tried to read it."""
    page.set_content(_LEGACY_DETAIL_HTML)
    locator = Locator(strategy=LocatorStrategy.LABEL_SIBLING, value={"label": "Nonexistent Field:"}, intent="test")

    with pytest.raises(LocatorResolutionError):
        _resolve_locator(page, locator)


def test_text_assertion_checks_title_and_body_together(page):
    """Checkpoints are authored against page TITLE (see compiler.py);
    business outcomes/escalation triggers are typically authored against
    BODY text. Both must resolve through the same assertion check."""
    page.set_content("<html><head><title>My Title</title></head><body>Body Text Here</body></html>")

    assert _text_assertion_holds(page, TextAssertion(contains="My Title"))
    assert _text_assertion_holds(page, TextAssertion(contains="Body Text Here"))
    assert not _text_assertion_holds(page, TextAssertion(contains="Not On This Page"))


class _FakeEscalationManager:
    """
    Test double: no terminal interaction, just records calls. Critically,
    wait_for_resume() does NOT change the page -- it simulates a human
    who resumes WITHOUT actually fixing the underlying condition, which
    is exactly the scenario that exposed the real bug below.
    """

    def __init__(self):
        self.requested = []
        self.resume_count = 0

    def request_intervention(self, request):
        self.requested.append(request)

    def wait_for_resume(self, poll_interval_seconds: float = 2.0):
        self.resume_count += 1


def test_unresolved_escalation_becomes_hard_failure_not_silent_success(page, tmp_path):
    """
    Regression test for the most serious of the three bugs found in
    testing: after a human resumes from an escalation, the executor used
    to only check for `business_outcome` and `hard_failure` explicitly --
    if the re-check came back `escalated` again (nothing was actually
    fixed), neither branch matched and execution silently fell through
    to the success path. See REPORT.md section 3, bug #3.
    """
    page.set_content("<html><head><title>Session Expired</title></head><body>Session Expired</body></html>")

    trigger = EscalationTrigger(
        name="session_expired", description="d",
        detection=TextAssertion(contains="Session Expired"), reason="re-auth needed",
    )
    step = Step(
        index=0, action=ActionType.WAIT_FOR, target=None,
        checkpoint=Checkpoint(description="reached detail", assertion=TextAssertion(contains="Member Detail"),
                               timeout_ms=200),
        risk=RiskLevel.SAFE,
    )
    artifact = CapabilityArtifact(
        id="t", name="t", description="t", created_from_run_id="r",
        target_app=TargetAppRef(app_id="x", base_url="http://x"),
        inputs=[], outputs=[], steps=[step], business_outcomes=[],
        escalation_triggers=[trigger],
        success_checkpoint=Checkpoint(description="done", assertion=TextAssertion(contains="Member Detail"),
                                       timeout_ms=200),
        review_status=ReviewStatus.DRAFT,
    )

    policy = PolicyGate(AllowlistConfig(allowed_domains=["*"], allowed_action_types=list(ActionType)))
    logger = EvidenceLogger(run_id="test_escalation_run", run_type="replay", base_dir=str(tmp_path))
    escalation = _FakeEscalationManager()

    result = replay_artifact(artifact, {}, policy, logger, page, escalation=escalation)

    assert result.status.value == "hard_failure", "an unresolved escalation must never report success"
    assert escalation.resume_count == 1, "must escalate exactly once, not loop forever"
    assert "not resolved" in result.failure.observed
