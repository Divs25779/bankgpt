# Computer-Use Automation System

A backend integration layer that lets an AI agent operate legacy back-office applications with
no API: an LLM discovers a flow once (computer use), the run is compiled into a typed, reusable
capability artifact, and that artifact replays deterministically -- no model in the loop -- as the
path an AI agent invokes in production. See `REPORT.md` for the full design write-up.

## Status

End-to-end vertical slice complete: discovery loop, artifact schema + compiler, deterministic
replay executor, escalation manager, safety/allowlist, redaction, evidence logging, and the mock
target app are all implemented and have been run for real (see `/evidence/`, which holds both the
saved capability artifact and the discovery/replay run logs). See `REPORT.md` section 7 for what was deliberately cut or left thin.

## Setup

macOS/Linux:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Windows (PowerShell/cmd):
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Once your venv is active, always invoke `python` (not `python3`) so you don't accidentally
fall through to a global interpreter outside the venv.

Create `.env` file:
- For Anthropic, the following keys need to be set:
`LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY={YOUR_API_KEY}`
- For OpenAI, the following keys need to be set:
`LLM_PROVIDER=openai` and `OPENAI_API_KEY={YOUR_API_KEY}`

Run the mock target app (in a separate terminal, **from the repo root** -- it's a package,
so `python mock_bank_app/main.py` directly will fail with a `ModuleNotFoundError`):

```bash
python -m mock_bank_app.main
# or: uvicorn mock_bank_app.main:app --reload --port 5000
```

This serves http://localhost:5000. Try it manually first:
- Search for `12345` or `67890` -> normal member detail -> "Open New Sub-Account" -> fill in a
  deposit amount -> Confirm (triggers a real browser `confirm()` dialog) -> success page.
- Search for `00000` -> not-found (declared business outcome).
- Search for `00001` -> permission denied (declared business outcome).
- Search for `00002` -> session expired (declared escalation trigger -- the HITL demo path).
- Search for `00003` -> valid member, but the detail page deliberately takes ~6s to render
  (the slow-load / WAIT_FOR checkpoint scenario).
- On the deposit form, entering `999999` produces an unmapped system error (the alternate
  "unhandled validation error" hard-failure scenario, kept available alongside session expiry).


## Demo path
Note: If using PowerShell, replace \ with the backtick ` character for multi-line commands

```bash
# 1. Discovery run: LLM drives the mock app to accomplish a goal, produces an artifact.
# This goal deliberately combines both of the assignment's own example goals into one
# run, so the single required discovery run exercises interactive elements (search,
# form fill, click), label-based data reading, AND the native confirm() dialog.
python -m src.agent.cli \
  --goal "Search for member 12345, read their current savings balance, then open a new sub-account for them with an initial deposit of 500 and reach the confirmation screen" \
  --target http://localhost:5000 \
  --param member_id=12345 \
  --param deposit_amount=500 \
  --output savings_balance="Savings Balance:" \
  --save-as evidence/artifacts/lookup_and_open_subaccount.json

# 2. Deterministic replay: no LLM, same artifact, new input
python -m src.replay.cli \
  --artifact evidence/artifacts/lookup_and_open_subaccount.json \
  --input member_id=67890 --input deposit_amount=250

# 3. Replay against a deliberately-injected error condition (evidence requirement) --
# member 00000 triggers the declared "member_not_found" business outcome
python -m src.replay.cli \
  --artifact evidence/artifacts/lookup_and_open_subaccount.json \
  --input member_id=00000 --input deposit_amount=250

# Add --headless to run without a visible window, or --no-human to disable the
# EscalationManager for unattended runs (any escalation becomes an immediate hard_failure).

# 4. Stretch goal: multi-run stability -- replays N times, one shared evidence folder,
# reports a success/business_outcome/escalated/hard_failure breakdown across attempts.
python -m src.replay.cli \
  --artifact evidence/artifacts/lookup_and_open_subaccount.json \
  --input member_id=67890 --input deposit_amount=250 --repeat 5

# 5. Stretch goal: confidence & approval -- promote a draft artifact to approved
# (required before any risky_irreversible step in it may run unattended; see REPORT.md section 6)
python -m src.artifact.approve --artifact evidence/artifacts/lookup_and_open_subaccount.json
```

The browser runs headed by default (`--headless` to disable) -- watch it drive the app live during
discovery. Evidence for both runs is written to `evidence/<run_id>/events.jsonl` plus
screenshots/accessibility snapshots in `evidence/<run_id>/captures/`; saved capability artifacts
live in `evidence/artifacts/`.

## Running tests

```bash
pytest
```

No API key and no running `mock_bank_app` needed -- `tests/test_compiler.py` is pure unit tests
against Pydantic objects, and `tests/test_replay_executor.py` uses Playwright's `page.set_content(...)`
against tiny inline HTML fixtures rather than a live server (the `chromium` browser installed
earlier via `playwright install chromium` is reused automatically). These are regression tests for
the three real bugs found and fixed during development -- see `REPORT.md` section 3 for the
original writeups and section 7 for what test coverage does *not* yet include.

## Running without live services

The safety and schema layers (`src/artifact`, `src/safety`, `src/evidence`) have no external
dependency and can be exercised directly, e.g.:

```bash
python -c "from src.artifact.schema import CapabilityArtifact; print('schema OK')"
```

## Repo layout

```
src/
  agent/        discovery loop (LLM in the loop) + perception (accessibility snapshot)
  llm/          provider-agnostic interface + Anthropic/OpenAI adapters
  artifact/     schema + compiler (transcript -> capability)
  replay/       deterministic replay executor (no LLM)
  safety/       allowlist / risk gate + redaction
  escalation/   stuck detection + human handoff
  evidence/     structured logging
mock_bank_app/  local legacy-style target surface (table layout, no test IDs)
evidence/
  artifacts/    saved capability artifacts (the reusable, agent-invocable capabilities)
  <run_id>/     per-run discovery/replay logs (events.jsonl) and failure captures
```
