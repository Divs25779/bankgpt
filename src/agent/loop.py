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

TODO (build together):
  1. Loop: capture_observation -> provider.decide_next_action -> policy
     check -> execute against Playwright -> log -> repeat.
  2. Resolve target_ref back to a Playwright locator using the side
     table from capture_observation.
  3. On ActionKind.DONE, hand the full turn history to
     src/artifact/compiler.compile_artifact.
  4. On ActionKind.STUCK or on an unhandled Playwright exception, call
     into src/escalation/manager.py instead of just raising.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.evidence.logger import EvidenceLogger
from src.llm.base import LLMProvider, ProposedAction, Observation
from src.safety.allowlist import PolicyGate


@dataclass
class DiscoveryConfig:
    goal: str
    target_url: str
    max_steps: int = 25
    timeout_seconds: int = 300


@dataclass
class DiscoveryResult:
    succeeded: bool
    turns: list[tuple[ProposedAction, Observation]]
    run_id: str
    stuck_reason: str | None = None


def run_discovery(
    config: DiscoveryConfig,
    provider: LLMProvider,
    policy: PolicyGate,
    logger: EvidenceLogger,
    page,  # playwright.sync_api.Page
) -> DiscoveryResult:
    raise NotImplementedError("Build together: the observe/decide/act loop")
