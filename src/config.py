"""
Central config -- deliberately a single small file for a project this
size. In production this would be per-tenant config (see REPORT.md
section 4); here it's env-driven for simplicity, and that's a
documented cut, not an oversight.
"""

from __future__ import annotations

import os

from src.artifact.schema import ActionType
from src.safety.allowlist import AllowlistConfig

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")  # or "openai"

MOCK_APP_BASE_URL = os.environ.get("MOCK_APP_BASE_URL", "http://localhost:5000")

DEFAULT_ALLOWLIST = AllowlistConfig(
    allowed_domains=["localhost:5000", "127.0.0.1:5000"],
    allowed_action_types=list(ActionType),
    require_approval_for_irreversible=True,
)
