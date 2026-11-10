# Design decisions

The public digest of the project's decision log. Each line gives the decision and the reason that drove it. Where a decision changed behavior, the tests that hold it are named in [JUDGES.md](../JUDGES.md) or [TESTING.md](TESTING.md).

## Foundations

| | Decision | Why |
| --- | --- | --- |
| D-001 | A Hermes plugin, not a fork | Users install it like any plugin, and Hermes upgrades don't break a fork. |
| D-002 | Verified Recipes are the core product | Agents can already call tools; what's missing is repeatable work whose outcome is proven. |
| D-003 | "Use recommended" and "Compare first" | Fast by default, transparent on request, and the same checks either way. |
| D-004 | The model proposes, the compiler decides | A typed, deterministic compiler is the only way a draft becomes runnable. |
| D-005 | Capalis's runner is the safety boundary | Hermes hooks don't cover plugin-dispatched calls (D-016), so Capalis enforces policy itself before every write. |
| D-006 | Truthful evidence levels | V0 (the tool's own reply) never counts. V1 is an independent read from the same system, V2 a different system. |
| D-007 | Local-first durable evidence | Everything lives in one SQLite file under Hermes's plugin data directory. |
| D-012 | No Python dependencies | Standard library only, so installation can't conflict with Hermes's own pins. |

## Safety

| | Decision | Why |
| --- | --- | --- |
| D-013 | Targets are compile-time constants | Report text can never choose where a write lands. |
| D-014 | A capability's own description can't lower its risk | Effects count only when Capalis has tested or reviewed the capability. |
| D-015 | Every change to a pinned capability is drift | Until a finer policy is proven, any change quarantines the Recipe before its next write. |
| D-016 | Don't rely on Hermes guards for plugin dispatch | In v0.23.0, `ctx.dispatch_tool` calls handlers directly. |
| D-020 | Runs read only their stored, redacted payload | Restarts reach the same decisions, and credential-shaped text is never forwarded. |
| D-021 | An attempt that may have reached the provider is never re-sent | The intent is journaled, dispatch is compare-and-set, and recovery reconciles instead of retrying. |
| D-022 | Lookups return the full resource | A write found after a timeout continues exactly as a successful one would. |
| D-023 | Verification gates dependents; revoked approval also stops read-back | Nobody is told about an outcome the independent check contradicts. |
| D-024 | Drift blocks shadow runs too | Practice against changed dependencies would prove nothing. |
| D-025 | "Not found" counts only after a settle window | Found in the first live run: GitHub's issue list lags behind new issues. |
| D-026 | Approve, resume, revoke, confirm and resolve are owner commands only | An injected instruction can't approve its own Recipe. |

## Integration

| | Decision | Why |
| --- | --- | --- |
| D-010 | Pin Hermes v0.23.0 | Every API used was read in that release's source. |
| D-018 | Executable capabilities are Capalis's reviewed adapters | v0.23.0 has no documented way to list other tools' schemas; other tools appear in comparisons only. |
| D-019 | Outbound messages use Capalis adapters | Hermes's `send_message` isn't agent-callable, and provider APIs allow read-back. |
| D-027 | Chat intake through `pre_gateway_dispatch`, admission first | A run row is durable before the hook returns, and the agent stays quiet in Recipe channels. |
| D-028 | Close the loop | Wait for the issue to close, then reply to the reporter. Discord replies are reconciled by this bot, the reply target and the text. |

## Usability

| | Decision | Why |
| --- | --- | --- |
| D-029 | People get text, the model gets JSON | Owners read and approve from these views. |
| D-030 | People use names; Recipes keep ids | "#bugs" and "@ada" are resolved by read-only lookups. Ids are pinned because names can change. |
| D-031 | Approve with a button, bound to the version shown | Nobody copies a digest, and a click can't approve a version you weren't shown. |
| D-032 | `/capalis setup` | Each gap comes with the exact fix in plain words. |
| D-033 | The reporter hears back twice, honestly | An immediate "tracked as #N", then a closing reply that says whether it was fixed. |
