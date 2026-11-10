# Capalis

**Agents that prove they're done.** Capability intelligence and Verified Recipes for Hermes Agent.

Describe a workflow in chat. Capalis compiles it into a **Recipe**, a fixed and versioned plan that can only write where you approve. It shadow-runs the Recipe (real reads, nothing sent) and waits for you to press **Approve**. From then on, every write is read back from the service by a separate check before it counts. It never creates a duplicate, never blindly retries, and stops itself if something it depends on changes.

> Example: when @ada posts a bug in Discord #bugs, open an issue in my-app, tell #engineering in Slack, thank her, and tell her again when the issue is closed.

## Setup

1. `hermes plugins enable capalis`
2. Add the credentials you need in **Settings → Plugins → Capalis**, or with `hermes config set`: `CAPALIS_GITHUB_TOKEN`, `CAPALIS_SLACK_BOT_TOKEN`, `CAPALIS_DISCORD_BOT_TOKEN`.
3. Type `/capalis setup`. It says what is ready and gives the exact fix for anything that isn't.
4. Ask Hermes for the Recipe using names (`#bugs`, `#engineering`, `my-app`, `@ada`), then approve with `/capalis approve <recipe>`.

Full guide: [docs/USER_GUIDE.md](../docs/USER_GUIDE.md). For teammates who only report bugs: [docs/REPORTERS.md](../docs/REPORTERS.md).

## Commands

`/capalis` status · `setup` · `show <recipe>` · `approve <recipe>` (button) · `runs` · `evidence <run>` · `pause` / `resume` · `revoke <recipe>` · `confirm` · `resolve`. The model can pause, but it can never approve, resume, revoke, confirm or resolve.

## Disclosure

- **Network:** `api.github.com`, `slack.com` and `discord.com` with your tokens (plus a Slack webhook if you set one), and credential-free HTTPS `GET`s to origins an approved Recipe names. No telemetry.
- **Storage:** one SQLite file in `plugin-data/capalis/` with Recipes, approvals, runs and redacted payloads. Credentials are read only through Hermes.
- **Messages:** Capalis consumes the messages in channels a Recipe listens to, so the agent doesn't reply there.
- **Background work:** a daemon thread in the gateway advances parked runs every 30 s. No shell commands, subprocesses or self-updating.

MIT licensed. Security model, tests and the claims judges can check: see the repository root.
