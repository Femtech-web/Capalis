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
- **Evidence:** a hash-chained, redacted event log, plus `JUDGES.md` generated from a real test run and the latest live run.
