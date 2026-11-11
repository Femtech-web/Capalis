# Changelog

All notable changes to Capalis. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.2.1] - 2026-11-11

Fixes from the Hermes plugin catalog review (D-044).

### Security
- **Web checks only reach public addresses.** A web check's address is written by the model, and practice runs read for real, so an internal page could have been read. Localhost, private networks, link-local addresses (including cloud metadata at 169.254.169.254), carrier-grade NAT, reserved and multicast addresses are now refused three times:
  - when the Recipe is compiled;
  - before each request;
  - at connect time, where the request connects only to the address that was checked and ignores proxy settings, so a DNS answer can't change in between.
- **Only known read tools can be contracted.** A contract's "read-only" label was the model's own word. Now only Hermes's `web_search` and `web_extract` are accepted: Hermes's own list of tools that are safe with untrusted input, minus `clarify` and `vision_analyze`. Tools that run commands, write, send or schedule are refused, and so are tools from other plugins.
- **The owner sees a contract test's inputs before it runs.** `/capalis contract test` shows the exact tool and inputs, and calls it only when the owner confirms, bound to the contract version shown.
- **Practice runs never call a contracted tool.** Those steps are simulated like writes, so an unapproved Recipe can't run a tool with inputs the model picked.

### Upgrading from 0.2.0
Recipes approved under 0.2.0 are quarantined the first time 0.2.1 starts, because the capabilities they pin changed version. Ask Hermes to rebuild the Recipe, then approve the new version. Contracts for any tool other than `web_search` and `web_extract` stop working.

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
