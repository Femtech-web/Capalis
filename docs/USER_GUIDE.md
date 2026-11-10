# Owner's guide

For the person who sets Capalis up for a team. No programming is needed. You chat with Hermes, and you approve by pressing a button or typing one command. Teammates who only report bugs need [REPORTERS.md](REPORTERS.md).

## What you will end up with

A **Recipe**: a small, approved workflow that runs on its own. For example:

> When someone posts a bug in Discord **#bugs**, open a GitHub issue in **my-app** (unless one already mentions it), tell engineering in Slack **#engineering**, thank the reporter, and tell them again when the issue is closed.

Capalis never does anything you haven't approved. It checks every write by reading it back, and it tells you plainly when something couldn't be checked.

## 1. Install (once)

```bash
hermes plugins install <owner>/<repo>#plugin
hermes plugins enable capalis
```

## 2. Connect the services you use (once)

Add only the credentials you need. The easiest place is Hermes Desktop, under **Settings → Plugins → Capalis**. In a terminal, `hermes config set NAME value` works too. Never paste tokens into a chat.

| Service | What to create | Store it as |
| --- | --- | --- |
| GitHub | A [fine-grained token](https://github.com/settings/personal-access-tokens/new) limited to your repositories, with **Issues: Read and write** | `CAPALIS_GITHUB_TOKEN` |
| Slack | A Slack app bot token with `chat:write`, `channels:read` and `channels:history` (plus `groups:*` for private channels). Invite the bot to the channel. | `CAPALIS_SLACK_BOT_TOKEN` |
| Discord | A Discord bot with **View Channel**, **Send Messages** and **Read Message History** in your channel, and the **Message Content** intent on | `CAPALIS_DISCORD_BOT_TOKEN` |
| Telegram (optional, your phone) | A bot from [@BotFather](https://t.me/BotFather); send it any message once so it can write to you | `CAPALIS_TELEGRAM_BOT_TOKEN` |

For Discord or Slack messages to *start* a Recipe, Hermes's own gateway has to be connected to that platform (`hermes gateway setup`). Then set these and restart the gateway:

| Reports arrive in | Set |
| --- | --- |
| Discord | `DISCORD_FREE_RESPONSE_CHANNELS` (your trigger channel, so messages arrive without an @mention) and `DISCORD_ALLOWED_USERS` (the people who may report) |
| Slack | `SLACK_FREE_RESPONSE_CHANNELS` and `SLACK_ALLOWED_USERS`, in the same way |

Optional: set **Alerts channel**. `telegram:<your chat id>` is best: it's your phone. `slack:#ops` and `discord:#ops` work too. Capalis posts there when something needs a person: a Recipe quarantined, a write it couldn't check, a step waiting for confirmation, a failed run. Each message comes with the exact command to type.

## 3. Check everything is ready

In any Hermes chat, type:

```
/capalis setup
```

Each line says **OK**, **FIX** (with the exact fix) or **NOTE**:

```
Capalis setup
  OK   GitHub: connected as your-name
  OK   Slack: connected as hermes in Your Team
  FIX  Slack: #engineering: the Capalis bot is not in this channel yet
       -> in Slack, open #engineering and type /invite @ followed by the bot's name
  OK   Discord: can see #bugs
1 thing(s) to fix.
```

## 4. Describe what you want

Ask in plain words, using names, not ids:

> Set up a Capalis Recipe called bug-reports: when @ada or @sam post in Discord #bugs, open an issue in my-app, notify #engineering in Slack, and tell the reporter when the issue is closed. Use the recommended options.

Hermes shows what each name resolved to (for example "#bugs is channel 1569… in Your Team"). It then builds the Recipe and runs it once in **shadow mode**: reads are real, writes are only simulated, and nothing is sent anywhere.

Reports in Slack instead? Say "reports arrive in Slack #support", and replies go into each report's thread.

Not about bugs? There's a ready-made **site watch** too:

> Check https://myshop.com/checkout every 10 minutes and tell #ops in Slack if it's down.

If you want to choose between options, say "compare first". To make that your default, type `/capalis prefer compare` (or `recommend`). For example, Capalis will explain that a Slack incoming webhook is simpler but its posts can't be read back, while the Slack bot's can.

## 5. Approve

```
/capalis approve bug-reports
```

You get a short summary and two buttons:

> **Approve bug-reports v1?**
> When @ada posts in #bugs, it will:
> • Open an issue in my-app (only if needed)
> • Post in #engineering
> • Post in #bugs: ack
> • Wait until the issue is closed
> • Post in #bugs: reply fixed (only if needed)
> • Post in #bugs: reply closed (only if needed)
>
> Every write is read back before it counts as done.
> **[Approve]  [Not now]**

The approval covers exactly the version you were shown, for 30 days. If the Recipe changes, it needs a new approval, and the summary starts with **what changed since the last version**. Use `/capalis show bug-reports` for every step, target and limit.

### Approve from your phone

With Telegram connected, add yourself under **Settings → Plugins → Capalis → Approvers** as `telegram:<your user id>`. Then, when a Recipe is ready, Capalis messages you:

> 🔔 Ready for your approval: bug-reports v2
> When @ada posts in #bugs, it will: …
> To approve it from here: 1. Send /capalis approve 2. Tap 1 (Approve)

Send `/capalis approve` to the bot. Telegram numbers the buttons, and the list above them says which is which, so tap **1** (Approve) or **2** (Not now). The reply confirms what it can now do and where:

> ✅ Approved bug-reports v2 for 30 days.
> When @ada posts in #bugs, it now runs on its own.
> It can only act in: #bugs, #engineering, my-app. Your Telegram user id is the number Hermes shows for you; it's also in your Hermes `TELEGRAM_ALLOWED_USERS` setting.

## 6. Use it

Ask a teammate to post a bug in #bugs. Within seconds:
- the issue appears,
- engineering is notified,
- the reporter gets "Thanks, tracked as …",
- and when someone closes the issue, the reporter is told how it was closed.

| You want to | Type |
| --- | --- |
| See what is happening | `/capalis` |
| See recent runs | `/capalis runs` |
| See the proof for one run | `/capalis evidence <first 8 characters of the run id>` |
| Stop everything right now | `/capalis pause` (and `/capalis resume` later) |
| Stop one Recipe | `/capalis pause bug-reports` |
| Withdraw approval | `/capalis revoke bug-reports` |
| Stop one run | `/capalis cancel <run>` |
| Keep a record for a reviewer | `/capalis export <run>` (writes Markdown and JSON) |
| Share a Recipe with another team | Ask Hermes to export it. Importing it elsewhere checks it hasn't been edited, rebuilds it against their tools, and needs their own approval; no credentials or approvals travel with it. |

## What the words mean

| Word | Meaning |
| --- | --- |
| **verified** | Every write was read back from the service and matched. |
| **unverified** | It happened, or may have, but couldn't be independently confirmed. Capalis says why. |
| **waiting** | Parked until something happens (for example, the issue being closed), or until you resume a pause. |
| **needs attention** | A write's outcome is unknown and can't be checked automatically. Capalis won't guess; answer with `/capalis resolve <run> <step> applied` or `not-applied`. |
| **quarantined** | Something the Recipe depends on changed (a token, a permission, a service's shape). Nothing more is written. Ask Hermes to rebuild the Recipe, then approve the new version. |
| **shadow** | A practice run: real reads, simulated writes, nothing sent. |

## Using other Hermes tools

Capalis's own connections cover GitHub, Slack, Discord and web pages. Another Hermes tool (built-in, or from another plugin you've installed) can take part as a read step or an extra check:

1. Ask Hermes: "Propose a Capalis contract for the read_file tool."
2. `/capalis contract test hermes:read_file` calls it once and shows you what it returned.
3. `/capalis contract approve hermes:read_file` lets Recipes use it. `/capalis contract revoke …` stops it, and quarantines the Recipes that use it.

Only tools that read can be approved this way.

## Team approvals

By default, only someone at the computer running Hermes (CLI, TUI or Desktop) can approve. To let a teammate approve from chat, add them in **Settings → Plugins → Capalis → Approvers** as `telegram:<id>` or `discord:<id>`. The model itself can never approve, resume, revoke or confirm anything.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| Nothing happens when someone posts in #bugs | Run `/capalis setup`. Check that the gateway is connected to Discord, that #bugs is in `DISCORD_FREE_RESPONSE_CHANNELS`, that the person is in `DISCORD_ALLOWED_USERS` and in the Recipe's reporter list, and that the Recipe is **live** (`/capalis`). |
| "could not find @name" | They haven't posted in that channel yet. Ask them to post once, or give their user id. |
| A Recipe is quarantined | `/capalis` shows the reason. Ask Hermes to rebuild the Recipe, then approve the new version. |
| A run is unverified | `/capalis evidence <run>` shows which check failed and why. The write itself may still have happened. |
