"""
CLI entrypoint: runs one discovery session against a live target and, on
success, compiles and saves a CapabilityArtifact.

WHY THIS OWNS THE INITIAL page.goto(): run_discovery (src/agent/loop.py)
never navigates anywhere except in response to a NAVIGATE action the
model explicitly proposes -- and the model has no way to know
target_url on its own; its prompt only ever shows the CURRENT page's
url/title, never the intended starting point. If this CLI didn't
navigate first, the model would observe a blank tab with nothing to act
on. "Take a goal + a target as input" (the assignment's own wording)
means turning "target" into "the browser is actually there" is this
layer's job, done once, before the loop takes over.

WHY BUSINESS OUTCOMES AND ESCALATION TRIGGERS ARE HARDCODED HERE, NOT
DERIVED: a successful discovery run has nothing to say about "member not
found" or "session expired" -- those are alternate paths a happy-path
run never takes. compile_artifact's docstring is explicit that these are
DECLARED knowledge of the target app, not inferred from turns. The ones
below are specific, documented knowledge of mock_bank_app's
error-injection member IDs (see mock_bank_app/data.py). A real system
would load these per target_app from a small config/registry rather than
hardcoding them in a CLI scoped to one app; noted as a cut in REPORT.md
section 7 rather than built out for one mock target.

WHY value_ref/output wiring happens AFTER compile_artifact, not inside
it: compile_artifact's docstring already flags this as the caller's job
-- the compiler has no way to know which literal a model typed
corresponds to a "parameter" versus an incidental fixed value, and no
way to know which labeled read the caller cares enough about to name as
an output. That judgment belongs to whoever is defining the capability
(here: --param and --output CLI flags), not to generic compilation logic.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from src.agent.loop import DiscoveryConfig, run_discovery
from src.artifact.compiler import compile_artifact
from src.artifact.schema import (
    ActionType,
    BusinessOutcome,
    Checkpoint,
    EscalationTrigger,
    InputParam,
    OutputField,
    ParamType,
    TargetAppRef,
    TextAssertion,
)
from src.evidence.logger import EvidenceLogger
from src.llm.base import ActionKind, get_provider
from src.safety.allowlist import AllowlistConfig, PolicyGate

MOCK_APP_BUSINESS_OUTCOMES = [
    BusinessOutcome(
        name="member_not_found",
        description="No member exists with the given ID.",
        detection=TextAssertion(contains="No member found"),
        is_success=False,
    ),
    BusinessOutcome(
        name="permission_denied",
        description="Operator role lacks permission to view this member.",
        detection=TextAssertion(contains="Access Denied"),
        is_success=False,
    ),
]

MOCK_APP_ESCALATION_TRIGGERS = [
    EscalationTrigger(
        name="session_expired",
        description="Session timed out mid-flow; replay has no re-authentication logic by design.",
        detection=TextAssertion(contains="Session Expired"),
        reason="Session expired -- a human must re-authenticate to continue.",
    ),
]


def _parse_kv(pairs: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"Expected NAME=VALUE, got: {pair!r}")
        name, value = pair.split("=", 1)
        result[name] = value
    return result


def main() -> int:
    load_dotenv()  # reads .env in the current working directory, if present -- see README

    parser = argparse.ArgumentParser(
        description="Run a discovery session and compile the result into a capability artifact."
    )
    parser.add_argument("--goal", required=True, help="Natural-language goal for the discovery agent.")
    parser.add_argument("--target", required=True, help="Starting URL for the target application.")
    parser.add_argument("--save-as", required=True, help="Path to write the compiled artifact JSON to.")
    parser.add_argument("--name", default=None, help="Capability name (defaults to the goal text).")
    parser.add_argument("--description", default=None, help="Capability description (defaults to the goal text).")
    parser.add_argument(
        "--param", action="append", default=[], metavar="NAME=LITERAL",
        help="Declare that a literal value typed during discovery (e.g. member_id=12345) "
        "should become a named, reusable input parameter. Repeatable.",
    )
    parser.add_argument(
        "--pii-param", action="append", default=[], metavar="NAME",
        help="Mark a --param NAME as carrying PII, for redaction. Repeatable.",
    )
    parser.add_argument(
        "--output", action="append", default=[], metavar="NAME=LABEL",
        help="Declare that reading a labeled field (e.g. savings_balance='Savings Balance:') "
        "should become a named output. LABEL must exactly match a label the model actually "
        "chose to read via a read_text action. Repeatable.",
    )
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument(
        "--headless", action="store_true",
        help="Run the browser headless. Default is headed, so a human can watch (and, later, intervene).",
    )
    parser.add_argument(
        "--app-id", default=None,
        help="Logical vendor-product id for TargetAppRef (defaults to the target's hostname).",
    )
    args = parser.parse_args()

    try:
        param_map = _parse_kv(args.param)
        output_map = _parse_kv(args.output)
    except ValueError as e:
        print(f"Argument error: {e}", file=sys.stderr)
        return 2
    pii_names = set(args.pii_param)

    run_id = f"discovery_{int(time.time())}"
    logger = EvidenceLogger(run_id=run_id, run_type="discovery")

    target_host = urlparse(args.target).netloc
    policy = PolicyGate(AllowlistConfig(
        allowed_domains=[target_host],
        allowed_action_types=list(ActionType),
    ))

    provider = get_provider(os.environ.get("LLM_PROVIDER", "anthropic"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page()
        page.goto(args.target)  # see module docstring: the loop never does this itself

        discovery_config = DiscoveryConfig(
            goal=args.goal,
            target_url=args.target,
            max_steps=args.max_steps,
            timeout_seconds=args.timeout_seconds,
        )

        print(f"[{run_id}] starting discovery: {args.goal!r} @ {args.target}")
        result = run_discovery(discovery_config, provider, policy, logger, page)

        if not result.succeeded:
            print(f"[{run_id}] discovery did NOT succeed: {result.stuck_reason}")
            print(f"Evidence: {logger.dir}/events.jsonl")
            browser.close()
            return 1

        print(f"[{run_id}] discovery succeeded in {len(result.turns)} steps")

        inputs = [
            InputParam(
                name=name,
                type=ParamType.STRING,
                required=True,
                description=f"Value for {name}",
                pii=(name in pii_names),
            )
            for name in param_map
        ]

        outputs: list[OutputField] = []
        for out_name, label in output_map.items():
            source_step = next(
                (i for i, t in enumerate(result.turns)
                 if t.action.kind == ActionKind.READ_TEXT and t.action.text_value == label),
                None,
            )
            if source_step is None:
                print(f"WARNING: --output {out_name}={label!r} matched no read_text action "
                      f"in this run (the model never read that label) -- skipping this output.")
                continue
            outputs.append(OutputField(
                name=out_name,
                type=ParamType.STRING,
                description=f"Value read from the '{label}' field.",
                source_step=source_step,
            ))

        target_app = TargetAppRef(app_id=args.app_id or target_host, base_url=args.target)

        last_title = result.turns[-1].resulting_observation.title
        success_checkpoint = Checkpoint(
            description=f"goal complete: reached '{last_title}'",
            assertion=TextAssertion(contains=last_title),
        )

        artifact_id = (args.name or args.goal).lower().replace(" ", "_")[:64]
        artifact = compile_artifact(
            artifact_id=artifact_id,
            name=args.name or args.goal,
            description=args.description or args.goal,
            run_id=run_id,
            target_app=target_app,
            inputs=inputs,
            outputs=outputs,
            business_outcomes=MOCK_APP_BUSINESS_OUTCOMES,
            escalation_triggers=MOCK_APP_ESCALATION_TRIGGERS,
            turns=result.turns,
            success_checkpoint=success_checkpoint,
        )

        # Wire value_ref for any step whose literal TYPE value matches a
        # declared --param -- compile_artifact's docstring names this as
        # the caller's responsibility (see module docstring above).
        value_to_param = {v: k for k, v in param_map.items()}
        for i, turn in enumerate(result.turns):
            if turn.action.kind == ActionKind.TYPE and turn.action.text_value in value_to_param:
                artifact.steps[i].value_ref = value_to_param[turn.action.text_value]

        # Mirror-image of the above: wire output_ref on the step that
        # PRODUCES each declared output, so the step is self-describing
        # on its own (matches Step.output_ref's documented intent) rather
        # than only discoverable by cross-referencing OutputField.source_step.
        for out in outputs:
            artifact.steps[out.source_step].output_ref = out.name

        save_dir = os.path.dirname(args.save_as)
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)
        with open(args.save_as, "w") as f:
            f.write(artifact.model_dump_json(indent=2))

        print(f"[{run_id}] artifact saved to {args.save_as}")
        print(f"[{run_id}] evidence at {logger.dir}/")
        browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
