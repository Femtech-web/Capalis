# Capalis

**Teach your AI agent a job once. It does it every time, and proves every step.** Capability intelligence and Verified Recipes for Hermes Agent.

Describe a job in plain words: "when @ada posts a bug in #bugs, open an issue in my-app, tell #engineering, and let her know when it's fixed." Capalis turns it into a **Recipe**, a fixed, versioned plan that can only write where you approve. It practices once in shadow mode (real reads, nothing sent), then waits for you to press **Approve**. From then on, every write is read back from GitHub, Slack or Discord before it counts. Nothing is done twice, and if something it depends on changes, it stops and alerts you.

Ready-made Recipes:
- bug reports from Discord or Slack, with replies to the reporter;
- a site watch that checks a page on a schedule and posts to Slack when it's down.

Anything else you can describe, built from the same pieces, including other Hermes tools you approve.

## Setup

1. `hermes plugins enable capalis`
2. Add the credentials you need in **Settings → Plugins → Capalis**, or with `hermes config set`: `CAPALIS_GITHUB_TOKEN`, `CAPALIS_SLACK_BOT_TOKEN`, `CAPALIS_DISCORD_BOT_TOKEN`, and optionally `CAPALIS_TELEGRAM_BOT_TOKEN`. Then set an **Alerts channel** (for example `telegram:<your chat id>`) and add yourself as an approver to approve from your phone.
3. Type `/capalis setup`. It says what is ready and gives the exact fix for anything that isn't.
4. Describe the Recipe using names, then approve it with `/capalis approve <recipe>`.

Guides: [owners](../docs/USER_GUIDE.md) · [reporters](../docs/REPORTERS.md).

## Commands

`/capalis` status · `setup` · `show` · `approve` (button) · `runs` · `evidence` · `export` · `pause` / `resume` · `revoke` · `cancel` · `contract test|approve|revoke` · `prefer`.

The model can propose, practice and pause, but it can never approve, resume, revoke, confirm or resolve.

## Disclosure

- **Network:** `api.github.com`, `slack.com`, `discord.com` and `api.telegram.org` with your tokens (plus a Slack webhook if you set one), and credential-free HTTPS `GET`s to origins an approved Recipe names. No telemetry.
- **Other Hermes tools:** only those whose read-only contract you tested and approved, called through `ctx.dispatch_tool`.
- **Storage:** one SQLite file in `plugin-data/capalis/` with Recipes, approvals, runs and redacted payloads, plus evidence exports in `plugin-data/capalis/exports/`. Credentials are read only through Hermes.
- **Messages:** Capalis consumes the messages in channels a Recipe listens to, and sends alerts and approval requests to your Alerts channel if you set one.
- **Background work:** a daemon thread in the gateway advances runs, fires schedules, checks for drift and sends alerts every 30 s. No shell commands, subprocesses or self-updating.

MIT licensed. Architecture, security model, tests and the claims judges can check: see the repository root.
