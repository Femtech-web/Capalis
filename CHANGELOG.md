# Changelog

All notable changes to Capalis. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-10

Your phone, your team's other tools, and more than bug reports.

### Added
- **Telegram as the owner's control panel.** Approval requests and alerts arrive on your phone; reply `/capalis approve` and tap **Approve**. Telegram can't read a message back, so it is used for messages to you, never as proof.
- **Owner alerts** to Telegram, Slack or Discord when a Recipe needs approval, quarantines itself, can't confirm a write, waits for you, or fails. Each alert is sent once, survives restarts, and includes the exact command to type.
- **Slack intake:** reports can come from a Slack channel, with every reply in the report's thread.
- **Schedules:** recurring Recipes (cron, in UTC) fire once per slot and never flood after downtime. New ready-made **site watch**: "check my page every 10 minutes and tell #ops if it's down."
- **Other Hermes tools in Recipes.** Built-in tools or tools from other plugins join as read steps or independent checks under a contract the owner tests and approves; calls go through `ctx.dispatch_tool`.
- **No silent upgrades:** a new version lists what changed, in `show` and in the approval summary.
- **Sharing and audit:**
  - Recipe export and import (tamper-checked, rebuilt locally, never carrying an approval);
  - `/capalis export <run>` writes a Markdown and JSON evidence bundle.
- **Smaller additions:**
  - `/capalis approve` without a name, when only one Recipe is waiting;
  - `/capalis cancel` and `/capalis prefer`;
  - comparisons count your own verified runs.

### Changed
- README and docs rewritten for people who aren't engineers: a 20-second problem, a before/after table, a five-step quick start, use cases, and guides for owners and reporters.
- Status runs the change check first, so a Recipe whose dependencies changed is never shown as live.
- `/capalis setup` prints the exact `hermes config set` commands that let Hermes pass a Slack or Discord report channel, and its reporters, to Capalis.
- Built for phones: `/capalis`, `/capalis runs`, alerts and the approval summary are short bullet lists in plain words, showing only the latest version of each Recipe. A quarantine says why in words ("Capalis was upgraded, so the tools this Recipe uses changed"), and the technical detail stays in the evidence.

### Fixed
- A reporter's "thanks!" reply to Capalis's acknowledgement could have opened a second issue. Replies and thread messages never start a run now.
- The secret scan flagged a public Discord channel id in a test fixture. That rule is disabled; the Discord token rules stay on.

### Upgrading from 0.1.0
Recipes approved under 0.1.0 are **quarantined** the first time 0.2.0 starts, because the capabilities they pinned changed version. That's the drift guard doing its job. Ask Hermes to rebuild the Recipe, then approve the new version.

## [0.1.0] - 2026-11-10 (pre-release)

The first public build, for the Nous Research Hermes Agent plugin hackathon.

### Added
- **Verified Recipes:** a typed compiler turns a model's draft into a scoped, immutable, digest-pinned workflow: triggers, steps, fixed targets, budgets and outcome checks.
- **Controlled runner:**
  - approval, pause, budget and pin checks before every write;
  - a write-intent journal with reconciliation instead of retries, plus a settle window for lagging provider lists;
  - independent read-back verification;
  - durable waits;
  - restart recovery.
- **Shadow runs:** real reads, simulated writes, nothing sent.
- **Capability comparison** with "Use recommended" and "Compare first".
- **Drift guard:** any change to a pinned capability quarantines the Recipe before its next write.
- **Reviewed adapters:** GitHub Issues, Slack (bot and webhook), Discord, and HTTPS GET.
- **Hermes integration:**
  - model tools and the `/capalis` owner command;
  - Discord intake through `pre_gateway_dispatch`;
  - `plugin_db` storage and `get_secret` credentials.
- **For non-technical users:**
  - names instead of ids;
  - approval with a button, bound to the version shown;
  - `/capalis setup` with exact fixes;
  - plain-text status and evidence;
  - the reporter is thanked at once and told how the issue was closed.
- **Evidence:** a hash-chained, redacted event log, and `JUDGES.md` generated from a real test run.
