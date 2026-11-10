# Roadmap

What's next, in order. Nothing here is claimed as working.

1. **Alerts to the owner** when a Recipe is quarantined, a write's outcome is unknown, or a step waits for confirmation, posted to a channel the owner chooses.
2. **Slack-channel triggers** with threaded replies, for teams that don't use Discord.
3. **Capability contracts for plugin authors.** Today only Capalis's own adapters can run in a Recipe. A plugin author would declare a tool's effect, its target parameter, a read-back probe and a reconciliation lookup, and a contract test would prove them. That would make their tool executable inside Recipes, with the same guarantees.
4. **More reviewed adapters:** Linear and Jira issues, GitHub pull-request checks, email.
5. **A read-only dashboard page** (Hermes dashboard plugin) listing Recipes, runs and evidence, with approvals still made by the owner.
6. **Finer drift policy:** a proven-compatible change, such as a new optional output field, would no longer need re-approval.
7. **Reliability reporting:** per-capability counts of verified, failed and unverified runs, feeding "Use recommended".
