"""
CLI: promote a draft CapabilityArtifact to review_status=approved.

WHY THIS MATTERS: PolicyGate.check_risk (src/safety/allowlist.py) already
enforces that a risky_irreversible step cannot run unattended unless the
artifact backing it is approved -- that enforcement existed from the
first version of the safety module. What was missing was any way to
actually promote an artifact other than hand-editing its saved JSON,
which is exactly the kind of manual, error-prone step a real review
workflow should not depend on. This is deliberately minimal: it prints
the artifact's risk profile for a human to actually look at, requires an
explicit confirmation, and writes review_status=approved back to the
same file. No new enforcement logic here -- that already existed and is
unchanged; this is only the missing promotion mechanism (stretch goal
"Confidence & approval" -- see REPORT.md).
"""

from __future__ import annotations

import argparse
import sys

from src.artifact.schema import CapabilityArtifact, ReviewStatus


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote a capability artifact to approved.")
    parser.add_argument("--artifact", required=True, help="Path to the artifact JSON to approve.")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    args = parser.parse_args()

    with open(args.artifact, "r", encoding="utf-8") as f:
        artifact = CapabilityArtifact.model_validate_json(f.read())

    if artifact.review_status == ReviewStatus.APPROVED:
        print(f"'{artifact.name}' is already approved.")
        return 0

    print(f"Capability : {artifact.name}")
    print(f"Description: {artifact.description}")
    print(f"Max risk   : {artifact.max_risk_level.value}")
    print("Steps:")
    for step in artifact.steps:
        flag = "  <-- risky/irreversible" if step.risk.value == "risky_irreversible" else ""
        print(f"  [{step.index}] {step.action.value} (risk={step.risk.value}){flag}")
    print()
    print("Approving allows any risky_irreversible step above to run UNATTENDED during replay")
    print("(see PolicyGate.check_risk) -- review the steps above before confirming.")

    if not args.yes:
        answer = input("Approve this artifact? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Not approved.")
            return 1

    artifact.review_status = ReviewStatus.APPROVED
    with open(args.artifact, "w", encoding="utf-8") as f:
        f.write(artifact.model_dump_json(indent=2))

    print(f"'{artifact.name}' approved and saved to {args.artifact}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
