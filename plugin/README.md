# Capalis

**Describe a job once. Capalis sets it up, runs it, and checks it really happened.**

Say what you want in plain words, using the names your team already uses:

> When someone posts a bug in Discord #bugs, open an issue in my-app, tell #engineering in Slack, and keep the reporter updated until it's closed.

Capalis finds #bugs, my-app and #engineering by name and picks the right tool for each step. It turns your sentence into a **Recipe**, does a practice run that sends nothing, and waits for you.

You approve that exact version with a button, from your phone on Telegram if you like. From then on it runs by itself:
- A report becomes one issue.
- The team is told.
- The reporter gets a reply, and hears back again when the issue is closed.

Every write is read back from where it landed before it counts as done. Nothing is done twice. If a token, a permission or a tool changes, the Recipe stops itself and waits for you.

Ready-made jobs:
- bug reports from Discord or Slack, with replies to the reporter;
- a site watch that checks a page on a schedule and tells Slack when it's down.

You can describe others from the same pieces, including read-only tools from your other Hermes plugins, once you've tested and approved them.

## Setup

1. Connect Hermes to your chat apps: `hermes gateway setup`, then `hermes gateway install`.
2. In **Settings → Plugins → Capalis**, add a GitHub token and the same bot tokens Hermes uses. Set **Approvers** and **Alerts channel** to `telegram:<your Telegram user id>`.
3. Describe a job in any Hermes chat, then type `/capalis setup`. It shows what's ready, with the exact fix for anything that isn't, including the one Hermes command each report channel needs.
4. Type `/capalis approve`, and tap **Approve**. On Telegram, that's **1**.

Step by step, including how to get each token: [owner's guide](https://github.com/Femtech-web/Capalis/blob/main/docs/USER_GUIDE.md). Teammates who only report bugs need [one page](https://github.com/Femtech-web/Capalis/blob/main/docs/REPORTERS.md).

## Commands

| Command | What it does |
| --- | --- |
| `/capalis` | Status |
| `/capalis setup` | Readiness check, with fixes |
| `/capalis show <recipe>` | What a Recipe does, step by step |
| `/capalis approve [recipe]` | Approve with a button |
| `/capalis runs` | Recent runs |
| `/capalis evidence <run>` | Every step of a run, with its checks |
| `/capalis export <run>` | A Markdown and JSON record for a reviewer |
| `/capalis pause [recipe]` · `resume` · `revoke <recipe>` · `cancel <run>` | Stop things |
| `/capalis contract test\|approve\|revoke <tool>` | Bring another Hermes tool in, read-only |
| `/capalis prefer recommend\|compare\|ask` | How options are presented to you |

The model can propose, practise and pause. Only you can approve, resume, revoke, confirm or resolve.

## Disclosure

- **Network:**
  - `api.github.com`, `slack.com`, `discord.com` and `api.telegram.org`, with your tokens;
  - a Slack webhook, if you set one;
  - credential-free HTTPS `GET`s to the pages an approved Recipe names.

  There is no telemetry.
- **Other Hermes tools:** only those whose read-only contract you tested and approved, called through `ctx.dispatch_tool`.
- **Storage:** one SQLite file in `plugin-data/capalis/`, holding Recipes, approvals, runs and redacted payloads, plus evidence exports in `plugin-data/capalis/exports/`. Credentials are read only through Hermes (`get_secret`). Capalis never reads `.env` or other tools' files.
- **Messages:** Capalis consumes the messages in channels a Recipe listens to, so the agent doesn't answer them. It sends alerts and approval requests to the Alerts channel you set.
- **Background work:** a thread in the Hermes gateway advances waiting runs, fires schedules, checks for changes and sends alerts every 30 seconds. No shell commands, subprocesses or self-updating.

MIT licensed. [Architecture, security model, tests and live proof](https://github.com/Femtech-web/Capalis#readme).
