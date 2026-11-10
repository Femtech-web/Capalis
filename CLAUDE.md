# Claude Instructions — Capalis

Read and follow `AGENTS.md` first. The detailed living requirements are under the local-only, gitignored `private-notes/` directory. Do not treat a missing note as proof of implementation.

**Important:** Do not over-comment the code. Add comments only for nonobvious logic, state machine invariants, authorization/security boundaries, tricky concurrency or third-party compatibility issues. Avoid explaining ordinary assignments, imports, and self-explanatory functions in comments.

Always consult the pinned Hermes source before asserting an API exists. Use documented public surfaces; never fabricate test success. Record blockers honestly and keep `private-notes/implementation-plan.md` and `private-notes/decisions.md` current.
