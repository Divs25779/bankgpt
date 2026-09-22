# Evidence index

Folder names are the real, unedited `run_id` values generated at the time each run happened
(also recorded inside every line of that folder's own `events.jsonl`) -- nothing here has been
renamed or reordered after the fact. This file is just a map from those names to what each run
demonstrates, for quick navigation.

## Artifact

- `artifacts/lookup_and_open_subaccount.json` -- the saved capability artifact, currently
  `review_status: approved` (see the approval run below). Compiled from `discovery_1790055161`.

## Discovery (the required real, LLM-driven run)

- `discovery_1790055161` -- goal: search member 12345, read savings balance, open a new
  sub-account with a $500 deposit, reach confirmation. 7 steps, no errors. Produced the artifact
  above.

## Replay -- core scenarios (against the current, approved artifact)

- `replay_1790074572` -- member 67890 (different member than discovery used), deposit 250.
  `status: success`, `outputs: {"savings_balance": "$918.42"}` -- proves parameterization works.
- `replay_1790074599` -- member 00000. `status: business_outcome`, `outcome: member_not_found`
  -- the required "replay that hits an error/exceptional state" evidence; a declared, typed
  answer, not a crash.
- `replay_1790074619` -- member 00002 (simulated session expiry). Escalates correctly at step 1,
  and correctly reports `hard_failure` (not a false success) when resumed without the underlying
  condition actually being fixed -- see REPORT.md section 3.

## Replay -- confidence & approval stretch goal (`--repeat 5`)

- `replay_1790055204` -- **before** approval. All 5 attempts: `hard_failure` at step 6
  (`policy_violation`: "Irreversible step requires an approved artifact... to run unattended").
  Step 6 (confirming the sub-account) was classified `risky_irreversible` by the compiler, and the
  artifact was still `review_status: draft` -- `PolicyGate.check_risk` correctly refused to run it
  unattended. Screenshots in `captures/` show the browser stopped before the Confirm click, every
  attempt.
- `replay_1790055872` -- **after** running `python -m src.artifact.approve` on the same artifact
  (see the approval prompt output, not logged here since it's a separate CLI). All 5 attempts:
  `success`. Same artifact, same inputs, only the approval state changed.

This before/after pair is the concrete demonstration of the approval mechanism actually mattering
-- not just code that compiles, but a real block followed by a real, deliberate unblock.
