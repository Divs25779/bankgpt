"""
Allowlist enforcement -- applies identically during discovery (LLM in the
loop) and replay (LLM out of the loop), because a permitted-action model
that only holds during one of the two phases isn't a safety model, it's
a formality. Both PolicyGate call sites must exist: one before the agent
loop acts (src/agent/loop.py), one before the replay executor acts
(src/replay/executor.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

from src.artifact.schema import ActionType, RiskLevel


class PolicyViolation(Exception):
    """Raised when an action would step outside the configured allowlist. Always a hard stop."""


@dataclass
class AllowlistConfig:
    allowed_domains: list[str]                  # e.g. ["localhost:5000", "*.mockbank.internal"]
    allowed_action_types: list[ActionType]
    # Risky action types require this to be true (or an approved artifact, see below)
    # before they may run unattended during replay.
    require_approval_for_irreversible: bool = True


class PolicyGate:
    """
    Call `check_navigation` before any navigate, and `check_action` before
    any click/type/select, on both the discovery and replay paths.
    """

    def __init__(self, config: AllowlistConfig) -> None:
        self._config = config

    def check_navigation(self, url: str) -> None:
        host = url.split("://", 1)[-1].split("/", 1)[0]
        if not any(fnmatch(host, pattern) for pattern in self._config.allowed_domains):
            raise PolicyViolation(
                f"Navigation to '{host}' blocked -- not in allowlist {self._config.allowed_domains}"
            )

    def check_action(self, action: ActionType) -> None:
        if action not in self._config.allowed_action_types:
            raise PolicyViolation(f"Action type '{action.value}' is not in the allowed action types")

    def check_risk(self, risk: RiskLevel, artifact_is_approved: bool) -> None:
        """
        Called by the replay executor before executing a step with this
        risk level. SAFE and RISKY_REVERSIBLE always proceed.
        RISKY_IRREVERSIBLE proceeds unattended only if the artifact has
        been through human review (review_status == approved); otherwise
        it must be escalated rather than executed blind. This is the
        conservative default the assignment asks us to justify: an
        artifact that opens a new account or moves money should not run
        unattended the first N times just because discovery succeeded once.
        """

        if risk == RiskLevel.RISKY_IRREVERSIBLE and self._config.require_approval_for_irreversible:
            if not artifact_is_approved:
                raise PolicyViolation(
                    "Irreversible step requires an approved artifact (review_status=approved) "
                    "to run unattended. Escalate to a human instead of guessing."
                )
