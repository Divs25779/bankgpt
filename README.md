# Computer-Use Automation System

A backend integration layer that lets an AI agent operate legacy back-office applications with
no API: an LLM discovers a flow once (computer use), the run is compiled into a typed, reusable
capability artifact, and that artifact replays deterministically -- no model in the loop -- as the
path an AI agent invokes in production. See `REPORT.md` for the full design write-up.

## Status

Scaffolding in place: artifact schema, provider-agnostic LLM interface (Anthropic + OpenAI),
safety/allowlist, redaction, and evidence logging are implemented. The agent loop, replay
executor, escalation manager, and mock target app are in progress -- see inline `TODO` /
`NotImplementedError` markers in `src/agent/`, `src/replay/`, `src/escalation/`, and
`mock_bank_app/`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

export LLM_PROVIDER=anthropic          # or: openai
export ANTHROPIC_API_KEY=sk-...        # or: export OPENAI_API_KEY=sk-...
```

Run the mock target app (in a separate terminal):

```bash
python3 mock_bank_app/app.py           # serves http://localhost:5000
```

## Demo path (once implemented)

```bash
# 1. Discovery run: LLM drives the mock app to accomplish a goal, produces an artifact
python3 -m src.agent.cli \
  --goal "look up member 12345 and read their current savings balance" \
  --target http://localhost:5000 \
  --save-as artifacts/lookup_member_balance.json

# 2. Deterministic replay: no LLM, same artifact, new input
python3 -m src.replay.cli \
  --artifact artifacts/lookup_member_balance.json \
  --input member_id=67890

# 3. Replay against a deliberately-injected error condition (evidence requirement)
python3 -m src.replay.cli \
  --artifact artifacts/lookup_member_balance.json \
  --input member_id=00000    # mock app returns "no such member" for this id
```

Evidence for both runs is written to `evidence/<run_id>/events.jsonl` plus screenshots/accessibility
snapshots in `evidence/<run_id>/captures/`.

## Running without live services

The safety and schema layers (`src/artifact`, `src/safety`, `src/evidence`) have no external
dependency and can be exercised directly, e.g.:

```bash
python3 -c "from src.artifact.schema import CapabilityArtifact; print('schema OK')"
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
artifacts/      saved capability artifacts
evidence/       per-run logs and captures
```
