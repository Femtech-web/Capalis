# Agent Instructions — Capalis

You are implementing **Capalis**, a production-minded Nous Research Hermes Agent plugin. Read `private-notes/product-brief.md`, `private-notes/prd.md`, `private-notes/architecture.md`, `private-notes/implementation-plan.md`, `private-notes/security-and-invariants.md`, `private-notes/testing-and-evidence.md`, and `private-notes/decisions.md` before altering design. These are local working notes; they are excluded from Git.

## First actions in a new checkout

1. Ensure a root `private-notes/` directory exists, with only the needed Markdown files documented above. Keep all working docs accurate; do not scatter duplicate planning notes.
2. Ensure `.gitignore` contains the anchored line `/private-notes/`. Check `git check-ignore -v private-notes/prd.md` and `git ls-files private-notes`. If any working notes are already tracked, untrack them without deleting local files (`git rm -r --cached private-notes`) and verify they are absent before pushing.
3. Pin the exact Hermes revision under test; read the official public plugin API/docs and the actual source before using fast-changing methods. Never invent an API or rely on private Hermes internals.
4. Implement in narrow vertical slices with real Hermes integration, deterministic tests, negative/fault tests, and honest evidence. Record design decisions and verified progress.

## Product principles

- **Simple by default, transparent by choice, verified in both cases.** Users choose “Use recommended” or “Compare first.” Neither path bypasses testing or essential approval.
- LLMs may propose a draft; a typed deterministic compiler and controlled runner decide what actually executes.
- Runtime untrusted input must never extend a Recipe's authorized control flow or targets.
- Only claim enforcement over **Capalis-managed** steps. Hermes hooks/middleware are not assumed to provide a global security boundary.
- Tool return values are not independent proof of intended results. Show `unverified` when a suitable observation is absent.
- Durable intent + idempotency/reconciliation before retries. Do not claim exactly-once for arbitrary external providers.
- Hermes credentials and installation follow normal permissions and approval flows; never scrape other plugins' credential stores.
- Keep private notes/private results out of Git, but publish sanitized code/tests/limitations and reproducible judge evidence.

## Code style and comments

Write readable, typed, idiomatic code with small single-purpose modules, descriptive names, explicit error handling, stable interfaces, and tests. **Do not add a comment above every line, every assignment, or every obvious function.** Comments should explain *why* a surprising decision exists, a security invariant, an external API constraint, concurrency reasoning, or a nonobvious fallback. Prefer clean code and precise tests over commentary. Add API docstrings when they materially help callers; do not generate decorative documentation or stale commented-out code.

## Test and verification discipline

No feature is “done” just because code was written. Run relevant unit tests, property tests, Hermes plugin validation/doctor, an actual Hermes tool call, and real sandbox integrations for supported external effects. Label fixture, sandbox and live results clearly. No fake telemetry, success rates, available tools, prices, plugin support, or screenshots. Maintain an honest limitations list.

## Public submission hygiene

The public README/JUDGES and docs/results are distinct from private working notes. Generate judge claims from real sanitized execution artifacts, verify counts in CI, run dependency and secret scanning, and never rely on local-only files for reproducibility. Respect Hermes plugin catalog SHA-pinning, consent, public API and disclosure rules.
