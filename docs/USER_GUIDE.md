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

For Discord or Slack messages to *start* a Recipe, Hermes's own gateway has to be connected to that platform (`hermes gateway setup`). For Discord, also set these and restart the gateway:
- `DISCORD_FREE_RESPONSE_CHANNELS`: your trigger channel, so messages arrive without an @mention.
- `DISCORD_ALLOWED_USERS`: the people who may report.

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

If you want to choose between options, say "compare first". For example, Capalis will explain that a Slack incoming webhook is simpler but its posts can't be read back, while the Slack bot's can.

## 5. Approve

```
/capalis approve bug-reports
```

You get a one-paragraph summary and two buttons:

> When @ada posts in #bugs, it will open an issue in my-app (if needed), then post in #engineering, then post in #bugs, then wait until the issue is closed, then post in #bugs. Every write is read back independently before it counts as done.
> **[Approve]  [Not now]**

The approval covers exactly the version you were shown, for 30 days. If the Recipe changes, it needs a new approval. Use `/capalis show bug-reports` for every step, target and limit.

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

## What the words mean

| Word | Meaning |
| --- | --- |
| **verified** | Every write was read back from the service and matched. |
| **unverified** | It happened, or may have, but couldn't be independently confirmed. Capalis says why. |
| **waiting** | Parked until something happens (for example, the issue being closed), or until you resume a pause. |
| **needs attention** | A write's outcome is unknown and can't be checked automatically. Capalis won't guess; answer with `/capalis resolve <run> <step> applied` or `not-applied`. |
| **quarantined** | Something the Recipe depends on changed (a token, a permission, a service's shape). Nothing more is written. Ask Hermes to rebuild the Recipe, then approve the new version. |
| **shadow** | A practice run: real reads, simulated writes, nothing sent. |

## Team approvals

By default, only someone at the computer running Hermes (CLI, TUI or Desktop) can approve. To let a teammate approve from chat, add them in **Settings → Plugins → Capalis → Approvers** as `telegram:<id>` or `discord:<id>`. The model itself can never approve, resume, revoke or confirm anything.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| Nothing happens when someone posts in #bugs | Run `/capalis setup`. Check that the gateway is connected to Discord, that #bugs is in `DISCORD_FREE_RESPONSE_CHANNELS`, that the person is in `DISCORD_ALLOWED_USERS` and in the Recipe's reporter list, and that the Recipe is **live** (`/capalis`). |
| "could not find @name" | They haven't posted in that channel yet. Ask them to post once, or give their user id. |
| A Recipe is quarantined | `/capalis` shows the reason. Ask Hermes to rebuild the Recipe, then approve the new version. |
| A run is unverified | `/capalis evidence <run>` shows which check failed and why. The write itself may still have happened. |
