"""
CLI entrypoint: replays a saved CapabilityArtifact deterministically --
no LLM in the loop -- against live input values, and prints the
resulting ReplayResult. This is the path an AI agent would trigger in
production; see src/replay/executor.py for the actual FSM.
"""

from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from src.artifact.schema import ActionType, CapabilityArtifact, ReplayStatus
from src.escalation.manager import EscalationManager
from src.evidence.logger import EvidenceLogger
from src.replay.executor import replay_artifact
from src.safety.allowlist import AllowlistConfig, PolicyGate


def _parse_kv(pairs: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"Expected NAME=VALUE, got: {pair!r}")
        name, value = pair.split("=", 1)
        result[name] = value
    return result


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Deterministically replay a saved capability artifact.")
    parser.add_argument("--artifact", required=True, help="Path to the saved artifact JSON.")
    parser.add_argument(
        "--input", action="append", default=[], metavar="NAME=VALUE",
        help="Input parameter value, e.g. member_id=67890. Repeatable.",
    )
    parser.add_argument("--headless", action="store_true", help="Run headless. Default is headed.")
    parser.add_argument(
        "--no-human", action="store_true",
        help="Disable the EscalationManager -- any escalation trigger or unclassified hard "
        "failure becomes an immediate hard_failure. Useful for unattended runs with nobody "
        "present to respond to an intervention prompt.",
    )
    args = parser.parse_args()

    try:
        input_values = _parse_kv(args.input)
    except ValueError as e:
        print(f"Argument error: {e}", file=sys.stderr)
        return 2

    with open(args.artifact, "r", encoding="utf-8") as f:
        artifact = CapabilityArtifact.model_validate_json(f.read())

    missing = [p.name for p in artifact.inputs if p.required and p.name not in input_values]
    if missing:
        print(f"Missing required input(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    run_id = f"replay_{int(time.time())}"
    logger = EvidenceLogger(run_id=run_id, run_type="replay")

    target_host = urlparse(artifact.target_app.base_url).netloc
    policy = PolicyGate(AllowlistConfig(
        allowed_domains=[target_host],
        allowed_action_types=list(ActionType),
    ))

    escalation = None if args.no_human else EscalationManager(run_id=run_id, logger=logger)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page()
        page.goto(artifact.target_app.base_url)

        print(f"[{run_id}] replaying '{artifact.name}' (v{artifact.version})")
        result = replay_artifact(artifact, input_values, policy, logger, page, escalation=escalation)
        browser.close()

    print(f"[{run_id}] status: {result.status.value}")
    if result.status == ReplayStatus.SUCCESS:
        print(f"[{run_id}] outputs: {result.outputs}")
    elif result.status == ReplayStatus.BUSINESS_OUTCOME:
        print(f"[{run_id}] outcome: {result.outcome_name}")
    elif result.status == ReplayStatus.HARD_FAILURE and result.failure:
        print(f"[{run_id}] failed at step {result.failure.step_index}")
        print(f"[{run_id}] expected: {result.failure.expected}")
        print(f"[{run_id}] observed: {result.failure.observed}")
        print(f"[{run_id}] evidence: {result.failure.evidence_paths}")
    print(f"[{run_id}] {result.steps_completed}/{len(artifact.steps)} steps completed in {result.duration_ms}ms")
    print(f"[{run_id}] evidence at {logger.dir}/")

    return 0 if result.status in (ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME) else 1


if __name__ == "__main__":
    sys.exit(main())
