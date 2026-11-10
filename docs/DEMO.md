# 90-second demo

Everything shown is real: a Hermes chat, the owner's Discord, a GitHub sandbox repository and a Slack workspace. Nothing is mocked or sped up except cuts between shots.

**Before recording:**
- Have a Hermes chat (CLI or Desktop), Discord `#bugs`, the sandbox repository's Issues tab and Slack `#engineering` open.
- Use a fresh Recipe id such as `bug-reports-demo`.
- Run `/capalis setup` once off camera, so every line says OK.

| Time | Shot | What to say or type |
| --- | --- | --- |
| 0–8 s | Title | "Agents say 'done'. Capalis makes them prove it." |
| 8–25 s | Hermes chat | "Set up a Capalis Recipe called bug-reports-demo: when I post a bug in Discord #bugs, open an issue in capalis-sandbox, tell #engineering in Slack, and reply to me when it's closed. Use recommended." The model shows what each name resolved to, compiles, and shadow-runs: **writes simulated, nothing written**. |
| 25–35 s | Hermes chat | `/capalis approve bug-reports-demo` and press **Approve** on the summary. "Only I can approve, and only the version I was shown." |
| 35–50 s | Discord → GitHub → Slack → Discord | Post "Checkout button does nothing on Safari" in `#bugs`. Cut to the new issue, then the Slack notice, then the reply under the Discord message: "tracked as #N". |
| 50–62 s | GitHub → Discord | Close the issue as completed. Within ~30 s a second reply appears: "has been fixed and closed". |
| 62–75 s | Hermes chat | `/capalis evidence <run>`: every write `observed verified`, each with a `V1 met` check. "Verified means GitHub, Slack and Discord read back what was written, not that a tool said OK." |
| 75–85 s | Hermes chat + Discord | `/capalis pause`, post again: nothing is written. `/capalis resume`. "Pause, revoked approval or a changed dependency stop the next write." |
| 85–90 s | Terminal | `make judge`: every claim holds. "No blind retries, no duplicates, no unverified 'done'." |

Tips:
- If the model asks whether to use the recommended option or compare, answer "Use recommended".
- Runs take a few seconds, because each write is read back before the next step.
- The ✅ Hermes adds under a Discord message means "received"; Capalis's replies are the confirmation.
- `/capalis` shows anything that needs your attention, including quarantined Recipes and their reasons.
