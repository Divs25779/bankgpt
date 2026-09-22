"""
Artifact schema: the typed, versioned contract that turns a one-off LLM
discovery run into a reusable, agent-invocable capability.

Design intent (see /REPORT.md section 2 for full rationale):

- A capability is data, not code. An AI agent (or a human reviewer) should
  be able to understand what it does, what it needs, and what it returns
  by reading this schema alone -- never by re-reading the discovery
  transcript.

- `business_outcomes` are first-class and separate from `steps`. This is
  the schema-level enforcement of the single most important distinction
  in this whole project: "no such member" is a legitimate typed result,
  not an exception. If you can't express an outcome as an entry in this
  list, it doesn't belong in the artifact.

- Every step targets an element via a `Locator`, which is a *strategy +
  fallback chain*, never a single brittle selector. Replay tries locators
  in order; which one succeeded is itself evidence (see EvidenceEvent).

- Nothing in this schema ever holds a secret, a credential, or raw PII.
  `value_ref` points at a named input parameter; the actual value is
  supplied per-invocation and is redacted before it is ever logged or
  persisted (see src/safety/redaction.py).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Locators
# ---------------------------------------------------------------------------

class LocatorStrategy(str, Enum):
    ROLE_NAME = "role_name"     # accessibility tree: role + accessible name (preferred)
    TEXT = "text"                # visible text PRESENCE match (checkpoints, business outcomes,
                                  # escalation triggers) -- value={"contains": "..."}. Asserts
                                  # this text exists; does not itself locate a readable VALUE.
    LABEL_SIBLING = "label_sibling"
    # Locate the element whose text exactly matches value["label"], then
    # target its immediate next sibling in DOM order as the thing to
    # read. Exists specifically for legacy "label cell, value cell"
    # table layouts (see mock_bank_app's member-detail table) where the
    # value itself has no role or accessible name of its own -- only
    # its neighboring label does. Deliberately a DISTINCT strategy from
    # TEXT rather than an overload of it: a Step should be
    # understandable from its own fields, not require knowing "TEXT
    # means something different here because this happens to be a
    # READ_TEXT action." Scope: only works when label and value are
    # DOM-adjacent siblings (the common case); a value nested deeper or
    # separated from its label needs a different strategy -- see
    # REPORT.md section 7.
    CSS = "css"                  # last resort -- brittle, flagged in review
    XPATH = "xpath"               # last resort -- brittle, flagged in review


class Locator(BaseModel):
    """
    How to find one element/control on the target surface.

    `strategy`/`value` is the primary attempt. `fallbacks` are tried in
    order if the primary fails to resolve to exactly one element. This
    fallback chain -- not any single selector -- is what "stable
    targeting" means in this system; see REPORT.md section 3.
    """

    strategy: LocatorStrategy
    value: dict = Field(
        description=(
            "Strategy-specific locator payload, e.g. "
            '{"role": "button", "name": "Search"} for role_name, '
            '{"contains": "Account Balance"} for text.'
        )
    )
    fallbacks: list["Locator"] = Field(default_factory=list)
    intent: str = Field(
        description="Human-readable description of what this targets, "
        "e.g. 'the member search submit button'. Required for review "
        "and for LLM-assisted recovery (stretch goal) to reason about "
        "the element without re-deriving it from scratch."
    )


Locator.model_rebuild()


class TextAssertion(BaseModel):
    """A checkpoint/outcome condition based on page text rather than a specific element."""

    contains: Optional[str] = None
    not_contains: Optional[str] = None
    matches_regex: Optional[str] = None


class Checkpoint(BaseModel):
    """
    A condition that must hold for a step (or the whole capability) to be
    considered successfully completed. Checkpoints exist because "the
    click didn't error" is not evidence the click worked.
    """

    description: str
    assertion: Union[Locator, TextAssertion]
    timeout_ms: int = 5000


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    WAIT_FOR = "wait_for"
    READ_TEXT = "read_text"
    DISMISS_IF_PRESENT = "dismiss_if_present"
    # ^ For a recoverable interstitial that is itself a page ELEMENT (e.g. a
    # dismissible banner/toast) -- a distinct step with its own target Locator.
    # NOT for native browser dialogs (confirm/alert/prompt) -- those are
    # handled via Step.expects_dialog on the step that triggers them,
    # because they fire synchronously inside that call and have no
    # separate DOM element of their own to target. See DialogPolicy.


class RiskLevel(str, Enum):
    SAFE = "safe"                              # read-only, fully reversible
    RISKY_REVERSIBLE = "risky_reversible"       # mutates state but can be undone
    RISKY_IRREVERSIBLE = "risky_irreversible"    # cannot be undone (e.g. submit transfer)


class DialogPolicy(str, Enum):
    """
    How to handle a native browser dialog (confirm/alert/prompt/beforeunload)
    that fires as a synchronous side effect of executing a step.

    This is deliberately attached to the STEP that triggers the dialog,
    not modeled as its own separate step in `steps`. A dialog fires
    *inside* the Playwright call that causes it (e.g. click()), which
    blocks until something handles the dialog -- there is no way to
    execute "click" and then, as a later, separate step, "accept the
    dialog"; the handler must already be registered before the
    triggering call runs. Attaching the policy as data on the triggering
    step keeps this a purely local rule for the replay executor ("before
    running this step, register the matching one-shot handler if
    expects_dialog is set") instead of needing to look ahead one step
    and couple the two together mechanically.
    """

    ACCEPT = "accept"
    DISMISS = "dismiss"


class Step(BaseModel):
    index: int
    action: ActionType
    target: Optional[Locator] = None
    value_ref: Optional[str] = Field(
        default=None,
        description="Name of an InputParam supplying this step's value. "
        "Never a literal value -- see module docstring.",
    )
    output_ref: Optional[str] = Field(
        default=None,
        description="If action is READ_TEXT, the name of the OutputField "
        "this step's extracted text populates.",
    )
    checkpoint: Optional[Checkpoint] = None
    risk: RiskLevel = RiskLevel.SAFE
    expects_dialog: Optional[DialogPolicy] = Field(
        default=None,
        description="Set if executing this step is known (from discovery) to trigger a "
        "native browser dialog. The replay executor must register the matching one-shot "
        "dialog handler BEFORE executing this step -- see DialogPolicy docstring.",
    )
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Inputs / outputs / outcomes
# ---------------------------------------------------------------------------

class ParamType(str, Enum):
    STRING = "string"
    INT = "int"
    ENUM = "enum"
    BOOL = "bool"


class InputParam(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str
    enum_values: list[str] | None = None
    pii: bool = Field(
        default=False,
        description="If true, this parameter's *value* is redacted in "
        "logs/evidence at every log site -- see src/safety/redaction.py. "
        "The parameter *name* and presence are never sensitive.",
    )


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str
    source_step: int = Field(description="Index of the Step (action=READ_TEXT) that produces this field.")


class BusinessOutcome(BaseModel):
    """
    A named, anticipated non-happy-path result that is nonetheless a
    *valid answer* to the goal -- e.g. "member not found", "insufficient
    permissions". Detected the same way a checkpoint is detected.

    `is_success` distinguishes "the capability executed correctly and
    this is the true answer" (is_success=True) from "the capability
    could not complete because of an expected business condition"
    (is_success=False, but still NOT a hard_failure -- the caller gets a
    clean typed reason, not a stack trace).
    """

    name: str
    description: str
    detection: Union[Locator, TextAssertion]
    is_success: bool


class EscalationTrigger(BaseModel):
    """
    A named, anticipated condition that the artifact knows it CANNOT
    resolve deterministically -- distinct from a BusinessOutcome, which
    the artifact answers on its own. Detecting one of these means "stop
    and get a human", not "here is the answer".

    The canonical example in this system is session expiry: a
    BusinessOutcome like "member not found" is something the replay
    engine can answer with confidence. "Your session has expired" is
    not -- the deterministic replay has no re-authentication logic, and
    deliberately should not: teaching replay to silently re-auth is
    exactly the kind of scope creep that turns a reviewable, bounded
    capability into an opaque one. A human is the correct owner of that
    decision, every time.

    Detected with the same Locator/TextAssertion mechanism as a
    checkpoint or outcome -- escalation triggers are just as much a
    declared, reviewable part of the contract as anything else here.
    """

    name: str
    description: str
    detection: Union[Locator, TextAssertion]
    reason: str = Field(description="Shown to the human operator in the intervention request, "
                         "e.g. 'session expired mid-flow, re-authentication required'.")


# ---------------------------------------------------------------------------
# Target app reference (multi-tenant / heterogeneity hook)
# ---------------------------------------------------------------------------

class TargetAppRef(BaseModel):
    app_id: str = Field(description="Logical vendor-product id, stable across tenants, "
                         "e.g. 'acme-core-banking'. Shared by every tenant running this vendor product.")
    base_url: str
    version_fingerprint: Optional[str] = Field(
        default=None,
        description="Best-effort fingerprint of the surface this artifact was recorded "
        "against (e.g. a hash of key DOM landmarks or a vendor-reported version string). "
        "Used to detect drift on replay -- see REPORT.md section 4.",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description="Set only on a tenant-specific override artifact. Base artifacts "
        "recorded against the vendor product leave this null -- see REPORT.md 3.7 for "
        "the base/override reuse model.",
    )


# ---------------------------------------------------------------------------
# The capability artifact itself
# ---------------------------------------------------------------------------

class ReviewStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class CapabilityArtifact(BaseModel):
    id: str
    name: str
    description: str
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_from_run_id: str = Field(
        description="Reference to the discovery run's evidence record. The raw model "
        "transcript is NOT embedded here by design -- it may contain incidental PII "
        "the model observed on-screen and is not itself reviewable as a capability contract."
    )
    target_app: TargetAppRef

    inputs: list[InputParam]
    outputs: list[OutputField]
    steps: list[Step]
    business_outcomes: list[BusinessOutcome] = Field(default_factory=list)
    escalation_triggers: list[EscalationTrigger] = Field(
        default_factory=list,
        description="Anticipated conditions this capability knows it cannot resolve on its "
        "own (e.g. session expiry) -- checked before falling through to hard_failure. "
        "See EscalationTrigger docstring.",
    )
    success_checkpoint: Checkpoint

    escalate_on_unclassified_hard_failure: bool = Field(
        default=True,
        description="If true (default), even a hard_failure that matches no declared "
        "business_outcome or escalation_trigger is routed to a human before being "
        "returned to the caller -- 'silently fail' is worse than 'ask a person' for "
        "regulated financial workflows. Set false per-artifact for low-stakes, "
        "read-only capabilities where fail-fast to the caller is preferable.",
    )

    review_status: ReviewStatus = ReviewStatus.DRAFT
    max_risk_level: RiskLevel = RiskLevel.SAFE  # derived; see validator below

    @model_validator(mode="after")
    def _derive_max_risk(self) -> "CapabilityArtifact":
        order = [RiskLevel.SAFE, RiskLevel.RISKY_REVERSIBLE, RiskLevel.RISKY_IRREVERSIBLE]
        highest = max((order.index(s.risk) for s in self.steps), default=0)
        object.__setattr__(self, "max_risk_level", order[highest])
        return self

    @model_validator(mode="after")
    def _validate_refs(self) -> "CapabilityArtifact":
        input_names = {p.name for p in self.inputs}
        for s in self.steps:
            if s.value_ref and s.value_ref not in input_names:
                raise ValueError(f"Step {s.index} references unknown input '{s.value_ref}'")
        output_names = {o.name for o in self.outputs}
        for o in self.outputs:
            step_indices = {s.index for s in self.steps}
            if o.source_step not in step_indices:
                raise ValueError(f"Output '{o.name}' references unknown step {o.source_step}")
        return self


# ---------------------------------------------------------------------------
# Replay result contract (see REPORT.md section 3 for the taxonomy rationale)
#
# Evaluation priority at every checkpoint during replay (src/replay/executor.py):
#   1. success_checkpoint matched            -> SUCCESS
#   2. a declared BusinessOutcome matched      -> BUSINESS_OUTCOME (a typed answer, not an error)
#   3. a declared EscalationTrigger matched    -> ESCALATED (artifact knows it can't resolve this)
#   4. nothing matched within timeout          -> HARD_FAILURE, and if
#      escalate_on_unclassified_hard_failure is true (default), routed to a
#      human before being returned to the caller -- see EscalationManager.
# ---------------------------------------------------------------------------

class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    HARD_FAILURE = "hard_failure"
    ESCALATED = "escalated"  # handed to a human mid-run; see src/escalation


class FailureDetail(BaseModel):
    step_index: int
    expected: str
    observed: str
    evidence_paths: list[str] = Field(default_factory=list)  # screenshot / a11y snapshot on disk


class ReplayResult(BaseModel):
    status: ReplayStatus
    artifact_id: str
    artifact_version: int
    outputs: dict = Field(default_factory=dict)
    outcome_name: Optional[str] = None       # set iff status == BUSINESS_OUTCOME
    failure: Optional[FailureDetail] = None   # set iff status == HARD_FAILURE
    steps_completed: int = 0
    duration_ms: int = 0
