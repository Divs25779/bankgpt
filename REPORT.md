# Design Write-up: Computer-Use Automation System

## 1. Architecture

[TODO: final diagram + prose once agent loop / replay / escalation are wired end to end]

Key decisions made up front:

- **Provider-agnostic LLM interface** (`src/llm/base.py`): the discovery loop's model-selection
  is isolated behind a single `LLMProvider.decide_next_action` method. Anthropic and OpenAI
  adapters implement it via tool-calling / function-calling respectively. This matters because
  the *replay* path never calls an LLM at all -- provider choice, cost, and availability are
  entirely a discovery-time concern, never a production-reliability concern.
- **Accessibility-tree observation, not screenshots** (`src/agent/perception.py`): the model
  reasons over role+name element lists, not pixels. This is the direct answer to "bias toward an
  approach that still works when the surface has no clean DOM" -- the accessibility tree is
  populated by the browser from semantics, independent of markup quality, and the same
  abstraction exists for desktop apps (see section 4).
- **Single-process, synchronous, no queue.** Given the scope note against building scaling
  infrastructure prematurely, discovery and replay both run as direct Playwright calls in one
  process. The architecture that *would* scale this (a queue between "capability requested" and
  "replay executed", a worker pool per tenant) is describable without being built -- see section 4.

## 2. Artifact schema

See `src/artifact/schema.py`. Design rationale:

- `business_outcomes` is a first-class list, separate from `steps`, because the most common
  design mistake in this domain is conflating "no such member" with a crash. Anything the caller
  needs to know as a legitimate answer must appear here, not be inferred from an exception.
- Locators are strategy + ordered fallback chain, never a single selector -- `role_name` primary
  (accessibility tree), `text` and `css`/`xpath` as fallbacks for surfaces where roles aren't
  exposed. Which locator resolved is itself evidence (see section 3), giving a future drift-detection
  signal without building drift detection now.
- `value_ref` / `output_ref` indirection means no literal PII or account data is ever baked into a
  saved artifact -- an artifact is a *template*, callable with different inputs, which is what
  makes it a capability rather than a recorded macro.
- `created_from_run_id` links to discovery evidence instead of embedding the transcript --
  the transcript may contain incidental on-screen PII and free-text model reasoning that has no
  place in a document meant to be reviewed as a contract.
- `risk` per step + `review_status` on the artifact is the hook for conservative handling of
  irreversible actions (section 6).

## 3. Determinism & error handling

[TODO: finalize once replay executor is implemented]

Result taxonomy (`ReplayResult` / `ReplayStatus`):

- **success** -- checkpoint reached, declared outputs extracted.
- **business_outcome** -- a declared `BusinessOutcome` matched (e.g. member not found). Not a
  failure; a typed answer the caller needs.
- **hard_failure** -- neither the success checkpoint nor any declared outcome matched within
  timeout. Evidence (screenshot + accessibility snapshot) captured before returning; `FailureDetail`
  states which step, what was expected, what was observed.
- **escalated** -- routed to a human mid-run rather than resolved automatically.

Determinism is achieved by: no model in the replay decision loop at all; locator fallback chains
tried in a fixed order; explicit checkpoints (not "the click didn't throw") gating every
state-changing step.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam between "how we perceive/act on a surface" and "the recorded
flow" is exactly the `Locator` abstraction: `ObservedElement`/`Locator` express role+name today
(web accessibility tree), but the same shape maps onto a legacy web app's visible text (via the
`text` strategy already in the schema) or a native desktop app's UI Automation / MSAA tree, which
exposes the same role/name concept OS-level. The `CapabilityArtifact` schema doesn't know or care
which perception backend produced a Locator -- only `src/agent/perception.py` (discovery) and the
equivalent resolver in the replay executor would need a new implementation per surface type; the
artifact schema, compiler, and replay FSM are all surface-agnostic already.

**Multi-tenant reuse.** `TargetAppRef.app_id` is the vendor-product identity, shared across every
tenant running that product; `TargetAppRef.tenant_id` is null on a base artifact and set only on a
tenant-specific override. The proposed reuse model: replay resolves `tenant_id`-specific overrides
first (if one exists for this `app_id` + tenant), falling back to the base artifact otherwise --
same pattern as config inheritance. Per-tenant/version drift is detected via
`version_fingerprint`: a fingerprint mismatch between what an artifact was recorded against and
what replay currently observes is a signal to flag the artifact for re-review, not to silently
keep running or silently fail.

## 5. Escalation & handoff

[TODO: finalize once implemented]

The browser runs headed, locally, so "the human takes control of the live session" is literally
true -- there is one browser window, and control is a logical flag (`Controller.AUTOMATION` /
`Controller.HUMAN`) the automation checks before every action, not a separate session that needs
transferring. On `STUCK` (discovery) or `hard_failure` (replay), an `InterventionRequest` is
written to evidence with full context (goal/capability, step, URL, screenshot, reason) and control
flips to human. A minimal control surface lets the human signal resume once done; the loop
re-observes state before continuing rather than trusting stale state. Full action-by-action
capture of what the human does inside the browser is out of scope (see section 7) -- the fact and
duration of the handoff is logged; the human's individual clicks are not.

## 6. Safety

`PolicyGate` (`src/safety/allowlist.py`) is invoked identically on the discovery and replay paths
-- domain allowlisting and action-type allowlisting are not a discovery-only nicety. Risk is
classified per step (`safe` / `risky_reversible` / `risky_irreversible`); irreversible steps may
not run unattended unless the artifact has been promoted to `review_status=approved` by a human,
enforced in `PolicyGate.check_risk`. Redaction (`src/safety/redaction.py`) runs on every evidence
write via schema-tagged `pii=True` input values *and* independent pattern-based sweeping (SSN/card/secret-shaped
strings), so a mistagged field is still caught.

Known limits: the risk classifier used during artifact compilation is heuristic (keyword-based on
model reasoning text) and always defaults to a human-reviewable `draft` state rather than trusting
its own classification -- see section 7.

## 7. Cuts

[TODO: fill in honestly once the build is done -- e.g. multi-tenant override resolution designed
but not implemented; desktop surface not implemented; operator UI is a bare CLI, not a real
console; LLM-assisted single-step recovery on replay failure not built; multi-run stability
scoring not built.]
