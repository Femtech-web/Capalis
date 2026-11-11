# Roadmap

What's next, in order. Nothing here is claimed as working.

1. **Tools from other plugins.** Today only Hermes's `web_search` and `web_extract` can be contracted, because a model-written contract can't prove a tool only reads. Accepting other tools needs a signal the model can't write, such as a read-only declaration from the plugin itself or Hermes. Writes come after that.
2. **Contracts published by plugin authors.** A plugin would ship its own Capalis contract, which owners still test and approve, so its tools are ready to use.
3. **More reviewed adapters:** Linear and Jira issues, GitHub pull-request checks, email, and Telegram as a target.
4. **A read-only dashboard page** (Hermes dashboard plugin) listing Recipes, runs and evidence. Approvals would still be made by the owner.
5. **Finer drift policy:** a proven-compatible change, such as a new optional output field, would no longer need re-approval.
6. **Team sharing:** opt-in, access-controlled sharing of Recipes between Hermes installs, building on today's export and import.
