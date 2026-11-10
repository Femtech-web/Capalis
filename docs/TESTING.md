# Testing

```bash
uv sync --group dev
make check     # ruff, mypy --strict, pytest (276 cases including parametrized ones; the live test is skipped unless you opt in)
make judge     # regenerate JUDGES.md and docs/results/<date>.json from a fresh run
make e2e       # live run on your own sandbox accounts (opt-in, see below)
```

## What each layer proves

| Layer | Files | What it covers |
| --- | --- | --- |
| Pure core | `tests/unit/test_compiler*.py`, `test_expressions_and_schema.py`, `test_policy_and_lifecycle.py`, `test_verification_and_drift.py`, `test_matching.py`, `test_wire_and_redact.py` | 38 rejection mutations of the golden Recipe, typed references and templates, fail-closed authorization, transition tables, outcome contracts, drift, deterministic capability matching, wire round-trips, redaction |
| Properties | `tests/property/test_core_properties.py` | Hypothesis: hostile payloads never change operations or targets; live authorization implies full grant coverage; transitions follow the tables; the compiler never crashes on arbitrary drafts |
| Storage | `tests/integration/test_storage.py` | Restarts, immutability, tamper detection, concurrent activation and duplicate deliveries across 6–8 connections, the canary secret absent from database and WAL bytes |
| Runner | `tests/integration/test_runner.py` | Against in-memory providers with injected faults (`tests/fakes.py`): shadow, duplicates, crashes before and after dispatch, ambiguous writes, false success, revocation, pause, drift, budgets, confirmations, waits, restart, concurrency |
| Service and surfaces | `tests/integration/test_service.py` | Compile → shadow-qualify → approve (typed and button) → gateway intake → close-the-loop replies; names instead of ids; readable views |
| Adapters | `tests/unit/test_adapters.py`, `test_directory.py` | Request shapes and failure classification for GitHub, Slack and Discord against scripted HTTP replies; name lookups; the setup report |
| Plugin | `tests/integration/test_plugin_registration.py` | Registrations match `plugin.yaml`; no model tool can approve; the pure core imports nothing from Hermes, the network or files; `private-notes/` is never tracked |
| Live | `tests/e2e/test_sandbox.py` | Opt-in; see below |

## Fakes, scripted replies and real services

- **Fakes** (`tests/fakes.py`) are in-memory GitHub, Slack and Discord used **only** to drive the runner through faults that real services can't produce on demand (timeouts after a write, lying success replies, crashes). They never stand in for an adapter.
- **Scripted HTTP replies** test the real adapters' requests and their handling of documented provider responses.
- **Real services** run in the opt-in live test and in the owner's live runs recorded in [JUDGES.md](../JUDGES.md).

## Live test on your own accounts

It creates one labeled issue and one Slack message per run:

```bash
export CAPALIS_GITHUB_TOKEN=...      # Issues read/write on your sandbox repository
export CAPALIS_SLACK_BOT_TOKEN=...   # bot in your test channel
export CAPALIS_E2E_REPO=you/sandbox CAPALIS_E2E_SLACK_CHANNEL=C0123456789
CAPALIS_E2E=1 .venv/bin/pytest tests/e2e
```

It shadow-runs the Recipe, then runs it live, and checks:
- the run is `verified`,
- a duplicate delivery is deduplicated,
- both effects can be found again by their idempotency keys.

It writes a sanitized record to `.dev/evidence/`, which `scripts/judge_report.py` publishes into `docs/results/` after a scan for credential-shaped strings.

## Checks that the tests themselves work

Two mutations were run by hand:
- **Redaction removed:** the canary test fails.
- **Crash guard removed** (the `dispatched → unknown_effect` rule): the crash test sees two issues and fails.

The first live run also exposed a real problem that tests with fakes couldn't. GitHub's issue list lags a second or two behind a new issue, so an immediate "not found" isn't proof of absence. That's why Capalis waits out a settle window before believing "not found" (D-025).

## Hermes checks

```bash
hermes plugins validate plugin --install-deps   # catalog admission gate: manifest, security scan, declared tools and hooks
hermes plugins doctor plugin --ci
```

Both pass on Hermes v0.23.0 (`2bd378ed`).
