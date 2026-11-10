# Changelog

All notable changes to Capalis. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-11

First release, for the Nous Research Hermes Agent plugin hackathon.

### Added
- **Verified Recipes:** a typed compiler turns a model's draft into a scoped, immutable, digest-pinned workflow (triggers, steps, targets, budgets, outcome contracts).
- **Controlled runner:**
  - approval, pause, budget and pin checks before every write;
  - a write-intent journal with reconciliation instead of retries;
  - independent read-back verification (V1, V2);
  - durable waits;
  - restart recovery.
- **Shadow runs:** real reads, simulated writes, nothing sent.
- **CapabilityMatch:** a deterministic comparison of capabilities for each step, with "Use recommended" and "Compare first".
- **Drift guard:** any change to a pinned capability quarantines the Recipe before its next write, through a pre-write check and a background sweep.
- **Reviewed adapters:** GitHub Issues (search, create, read back, find by key), Slack (post, read back, find by metadata, incoming webhook) and Discord (post or reply, read back, find by reply), plus credential-free HTTPS GET.
- **Hermes integration:**
  - six model tools and the `/capalis` owner command, with button approvals;
  - Discord intake through `pre_gateway_dispatch`;
  - storage in `plugin_db` and credentials through `get_secret`.
- **Usability:**
  - names instead of ids (`#bugs`, `@ada`, `my-app`);
  - `/capalis setup` with exact fixes;
  - plain-text status, runs and evidence;
  - the reporter is thanked at once and told how the issue was closed.
- **More than bug reports:**
  - Slack-channel intake with replies in the report's thread;
  - schedule triggers (UTC cron, once per slot, never backfilled);
  - a ready-made site-watch Recipe.
- **Owner alerts** to a channel you choose, when something needs a person.
- **Other Hermes tools** in Recipes, as read steps or independent checks, under owner-tested and owner-approved read-only contracts (`ctx.dispatch_tool`).
- **No silent upgrades:** a new version shows what changed, in `show` and in the approval summary.
- **Sharing:** Recipe export and import (digest-checked, recompiled locally, never carrying approvals), plus reviewer evidence bundles (`/capalis export`).
- **Smaller commands:** reliability counts from your own live runs in comparisons, `/capalis prefer`, and `/capalis cancel`.
- **Evidence:** a hash-chained, redacted event log, plus `JUDGES.md` generated from a real test run and the latest live run.

### Fixed
- A reply to Capalis's own acknowledgement could have been treated as a new report; replies never start a run.
- GitHub's issue list lags behind new issues, so "not found" now counts only after a settle window.
- The secret scan flagged a public Discord channel id in a test fixture; that rule is disabled, and token rules stay on.
