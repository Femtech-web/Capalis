# Roadmap

What's next, in order. Nothing here is claimed as working.

1. **Write contracts for other plugins' tools.** Read-only contracts already work. Allowing writes needs a contract test in a sandbox target the owner names, a read-back probe the owner approves, and a reconciliation lookup, so the same "never twice, always proven" guarantees hold.
2. **Contracts published by plugin authors.** A plugin would ship its own Capalis contract, which owners still test and approve, so its tools are ready to use.
3. **More reviewed adapters:** Linear and Jira issues, GitHub pull-request checks, email, and Telegram as a target.
4. **A read-only dashboard page** (Hermes dashboard plugin) listing Recipes, runs and evidence. Approvals would still be made by the owner.
5. **Finer drift policy:** a proven-compatible change, such as a new optional output field, would no longer need re-approval.
6. **Team sharing:** opt-in, access-controlled sharing of Recipes between Hermes installs, building on today's export and import.
