"""Conversation surfaces: the tools the model sees and the /capalis command the owner types.

The model can plan, compare, compile, shadow-qualify, start manual runs, read evidence and pause. Only
the owner, through /capalis, can approve, resume, revoke, confirm or resolve (I-02): those change what
Capalis is allowed to do, so they never sit behind a tool the model can call.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from typing import Any

from .. import __version__
from ..core.lifecycle import IllegalTransition
from ..service import Actor, Capalis, ServiceError
from ..storage.db import StoreError
from ..templates import DRAFT_FORMAT
from . import render

TOOLSET = "capalis"
OWNER = "owner"
ServiceFactory = Callable[[], Capalis]

_OBJ: dict[str, Any] = {"type": "object"}

STATUS_SCHEMA: dict[str, Any] = {
    "name": "capalis_status",
    "description": ("Show Capalis's state in this Hermes install: Recipes and their lifecycle state, recent runs, "
                    "anything needing the owner's attention, and which capabilities are configured. Call it before "
                    "promising the user any Capalis automation."),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

CAPABILITIES_SCHEMA: dict[str, Any] = {
    "name": "capalis_capabilities",
    "description": (
        "List the capabilities Capalis can run (with input/output schemas, effects, read-back and setup), or, "
        "with 'needs', compare candidates for each step of a goal. Comparison is deterministic: it recommends a "
        "capability only when the evidence clearly favors it, and lists exclusions. Offer the user 'Use "
        "recommended' or 'Compare first'; when no capability is recommended, show the comparison and let the "
        "user choose. Never claim a capability can be verified when the comparison says it cannot."),
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "needs": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "required": ["need_id", "operation", "max_effect"],
                                             "properties": {
            "need_id": {"type": "string"},
            "operation": {"type": "string", "description": "e.g. issue.create, issue.search, message.post"},
            "description": {"type": "string"},
            "max_effect": {"type": "string", "enum": ["read", "local_write", "external_write", "external_send"]},
            "min_evidence": {"type": "string", "enum": ["V1", "V2", "none"],
                             "description": "independent evidence the step must support (default V1)"}}}},
    }},
}

RECIPE_SCHEMA: dict[str, Any] = {
    "name": "capalis_recipe",
    "description": (
        "Build Verified Recipes. 'template' returns a starting draft for a Discord bug report -> GitHub issue -> "
        "Slack notice -> (when the issue is closed) reply to the reporter workflow; pass names the way the user "
        "says them (#bugs, #engineering, my-repo, @ada) and show the user what each resolved to. 'compile' checks"
        " a draft against the reviewed capabilities and stores it as a new immutable version (or returns problems"
        " to fix). 'qualify' shadow-runs the latest version on a sample trigger (use a realistic payload: Discord"
        " message ids are 17-20 digit numbers): reads are real, writes are simulated and nothing is sent; on "
        "success the version awaits the owner's approval, which only the owner can give by typing /capalis "
        "approve. 'show' and 'list' describe Recipes. " + DRAFT_FORMAT),
    "parameters": {"type": "object", "additionalProperties": False, "required": ["action"], "properties": {
        "action": {"type": "string", "enum": ["template", "compile", "qualify", "show", "list", "export", "import"]},
        "bundle": {**_OBJ, "description": "import: a capalis-recipe/1 export; it is compiled here and still needs "
                                          "shadow qualification and the owner's approval"},
        "recipe_id": {"type": "string", "description": "3-40 lowercase letters, digits or dashes"},
        "draft": {**_OBJ, "description": "the Recipe draft (compile)"},
        "sample_payload": {**_OBJ, "description": "a realistic trigger payload for the shadow run (qualify)"},
        "repo": {"type": "string",
                 "description": "template: repository name or owner/repo, as the user said it"},
        "slack_channel": {"type": "string", "description": "template: Slack channel name (#engineering) or id"},
        "template": {"type": "string", "enum": ["bug_report", "site_watch"],
                     "description": "template: bug_report (default) or site_watch (check a page on a schedule and "
                                    "tell Slack when it is not up)"},
        "url": {"type": "string", "description": "site_watch: the https:// page to check"},
        "every_minutes": {"type": "integer", "description": "site_watch: how often (divides an hour; default 10)"},
        "intake": {"type": "string", "enum": ["discord", "slack"],
                   "description": "template: where reports are posted (default discord)"},
        "discord_channel": {"type": "string",
                            "description": "template: the channel reports are posted in (#bugs), on the intake "
                                           "platform"},
        "reporters": {"type": "array", "items": {"type": "string"},
                      "description": "template: who may report: usernames on the intake platform (@ada) or ids"},
        "reply_when_closed": {"type": "boolean",
                              "description": "template: wait for the issue to close, then reply to the reporter "
                                             "in Discord (default true)"},
    }},
}

RUN_SCHEMA: dict[str, Any] = {
    "name": "capalis_run",
    "description": ("Start a run of a manual-trigger Recipe, or report on runs. 'evidence' returns each step's "
                    "state and the independent checks behind it; report outcomes exactly as it states them "
                    "(verified only when it says verified)."),
    "parameters": {"type": "object", "additionalProperties": False, "required": ["action"], "properties": {
        "action": {"type": "string", "enum": ["start", "evidence", "export", "list"]},
        "recipe_id": {"type": "string"}, "run_id": {"type": "string"}, "payload": _OBJ,
    }},
}

SETUP_SCHEMA: dict[str, Any] = {
    "name": "capalis_setup",
    "description": ("Check, read-only, whether Capalis is ready: which services are connected, whether the bots can "
                    "reach every channel and repository the Recipes use, and the exact fix for each gap. Relay the "
                    "fixes in plain words; never ask the user to paste a token into chat."),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

CONTRACT_SCHEMA: dict[str, Any] = {
    "name": "capalis_contract",
    "description": (
        "Let Capalis use one of Hermes's built-in read tools (web_search or web_extract) inside Recipes, as a read "
        "step or an independent check. No other tool, and no tool from another plugin, is accepted. 'propose' "
        "records a capability contract: the Hermes tool name, a summary, a dotted operation name, input_schema, "
        "output_schema (or output_format text), and sample_inputs for a test call. A proposal is untrusted: the "
        "owner runs /capalis contract test <id>, sees the exact inputs before anything is called, then "
        "/capalis contract approve <id>. Practice runs never call it. 'list' shows contracts and their state."),
    "parameters": {"type": "object", "additionalProperties": False, "required": ["action"], "properties": {
        "action": {"type": "string", "enum": ["propose", "list"]},
        "contract": {**_OBJ, "description": "propose: the contract"},
    }},
}

PAUSE_SCHEMA: dict[str, Any] = {
    "name": "capalis_pause",
    "description": ("Pause one live Recipe, or every Recipe when recipe_id is omitted. Runs in progress hold before "
                    "their next write. Only the owner can resume (/capalis resume)."),
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "recipe_id": {"type": "string"}, "reason": {"type": "string"}}},
}

TOOLS = (STATUS_SCHEMA, SETUP_SCHEMA, CAPABILITIES_SCHEMA, RECIPE_SCHEMA, RUN_SCHEMA, CONTRACT_SCHEMA, PAUSE_SCHEMA)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _guard(fn: Callable[[], Any]) -> str:
    try:
        return _json(fn())
    except (ServiceError, StoreError, IllegalTransition, KeyError, TypeError, ValueError) as exc:
        return _json({"error": str(exc)})


def handlers(service: ServiceFactory) -> dict[str, Callable[..., str]]:
    def status(args: dict[str, Any], **_: Any) -> str:
        return _guard(lambda: {"plugin": "capalis", "version": __version__, **service().status()})

    def capabilities(args: dict[str, Any], **_: Any) -> str:
        needs = args.get("needs")
        return _guard(lambda: service().compare(needs) if needs else service().capabilities())

    def recipe(args: dict[str, Any], **_: Any) -> str:
        action = args.get("action")

        def go() -> Any:
            svc = service()
            if action == "template" and args.get("template") == "site_watch":
                return svc.site_watch_template(url=args["url"], slack_channel=args["slack_channel"],
                                               every_minutes=int(args.get("every_minutes") or 10))
            if action == "template":
                return svc.bug_report_template(repo=args["repo"], slack_channel=args["slack_channel"],
                                               discord_channel=args["discord_channel"],
                                               reporters=list(args.get("reporters") or []),
                                               reply_when_closed=bool(args.get("reply_when_closed", True)),
                                               intake=str(args.get("intake") or "discord"))
            if action == "compile":
                return svc.compile(args["recipe_id"], args["draft"], owner=OWNER)
            if action == "qualify":
                return svc.qualify(args["recipe_id"], args.get("sample_payload") or {})
            if action == "show":
                latest = svc.store.latest(args["recipe_id"])
                if latest is None:
                    raise ServiceError(f"no Recipe named {args['recipe_id']}")
                return svc.describe(latest[0])
            if action == "list":
                return svc.recipes()
            if action == "export":
                return svc.export_recipe(args["recipe_id"])
            if action == "import":
                return svc.import_recipe(args["bundle"], args["recipe_id"], owner=OWNER)
            raise ServiceError(f"unknown action {action!r}")

        return _guard(go)

    def run(args: dict[str, Any], **_: Any) -> str:
        action = args.get("action")

        def go() -> Any:
            svc = service()
            if action == "start":
                return svc.run_manual(args["recipe_id"], args.get("payload") or {}, sender=OWNER)
            if action == "evidence":
                return svc.evidence(args["run_id"])
            if action == "export":
                return svc.export_evidence(args["run_id"])
            if action == "list":
                return [svc.run_line(r) for r in svc.store.runs(args.get("recipe_id"), limit=20)]
            raise ServiceError(f"unknown action {action!r}")

        return _guard(go)

    def pause(args: dict[str, Any], **_: Any) -> str:
        return _guard(lambda: {"result": service().pause(args.get("recipe_id"), actor="agent",
                                                         reason=str(args.get("reason") or ""))})

    def setup(args: dict[str, Any], **_: Any) -> str:
        return _guard(lambda: service().setup_report())

    def contract(args: dict[str, Any], **_: Any) -> str:
        if args.get("action") == "propose":
            return _guard(lambda: service().propose_contract(args.get("contract") or {}))
        return _guard(lambda: service().contracts())

    return {"capalis_status": status, "capalis_setup": setup, "capalis_capabilities": capabilities,
            "capalis_contract": contract, "capalis_recipe": recipe,
            "capalis_run": run, "capalis_pause": pause}


COMMAND_HELP = """/capalis                         status
/capalis setup                      what is connected, and what to fix
/capalis approve [recipe]           review and approve with a button (the Recipe can be left out if only one is waiting)
/capalis approve <recipe> <digest> approve the reviewed version by its digest (from 'show')
/capalis show <recipe>              what a Recipe does, its targets and checks
/capalis runs [recipe]              recent runs
/capalis evidence <run>             steps and independent checks for a run
/capalis pause [recipe]             pause one Recipe or all
/capalis resume [recipe]            resume after a pause
/capalis revoke <recipe>            withdraw approval
/capalis export <run>               write a reviewer-friendly evidence bundle (Markdown + JSON)
/capalis contracts                  other Hermes tools proposed for use in Recipes
/capalis contract test|approve|revoke <id>   see and run a proposed tool's test, then approve or revoke it
/capalis cancel <run>               stop a run (an effect already sent stays recorded)
/capalis prefer recommend|compare|ask   how options are presented to you
/capalis confirm <run> <step>       confirm a step that needs per-run confirmation
/capalis resolve <run> <step> applied|not-applied   answer for an effect Capalis could not check
Run ids can be shortened to their first 8 characters. Add --json for the raw record."""


def _format(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, default=str, ensure_ascii=False)


Ask = Callable[[str, list[Any]], Any]


def command(service: ServiceFactory, ask: Ask | None = None) -> Callable[..., str]:
    def handle(raw_args: str = "", *, platform: str = "", source: Any = None, **_: Any) -> str:
        actor = Actor(platform or "cli", None if source is None else getattr(source, "user_id", None))
        try:
            words = shlex.split(raw_args or "")
        except ValueError as exc:
            return f"Could not read that: {exc}"
        raw = "--json" in words
        words = [w for w in words if w != "--json"]
        verb, rest = (words[0], words[1:]) if words else ("status", [])
        svc = service()

        def view(value: Any, text: Callable[[Any], str]) -> str:
            return _format(value) if raw else text(value)

        try:
            if verb == "status":
                return view({"version": __version__, **svc.status()}, render.status)
            if verb == "help":
                return COMMAND_HELP
            if verb == "setup":
                return view(svc.setup_report(), render.setup)
            if verb == "approve" and not rest:
                waiting = [r["recipe_id"] for r in svc.recipes() if r["state"] == "awaiting_approval"]
                if len(waiting) != 1:
                    return ("Nothing is waiting for your approval." if not waiting else
                            "Several Recipes are waiting: " + ", ".join(waiting)
                            + ". Say which: /capalis approve <recipe>")
                return _approve_with_choice(svc, waiting[0], actor, ask)
            if verb == "approve" and len(rest) == 1:
                return _approve_with_choice(svc, rest[0], actor, ask)
            if verb == "approve" and len(rest) == 2:
                return svc.approve(rest[0], rest[1], actor)
            if verb == "show" and len(rest) == 1:
                latest = svc.store.latest(rest[0])
                return view(svc.describe(latest[0]), render.recipe) if latest else f"No Recipe named {rest[0]}."
            if verb == "runs":
                return view([svc.run_line(r) for r in svc.store.runs(rest[0] if rest else None, limit=10)],
                            render.runs)
            if verb == "evidence" and len(rest) == 1:
                return view(svc.evidence(rest[0]), render.evidence)
            if verb == "pause":
                return svc.pause(rest[0] if rest else None, actor=actor.id)
            if verb == "resume":
                return svc.resume(rest[0] if rest else None, actor)
            if verb == "revoke" and len(rest) == 1:
                return svc.revoke(rest[0], actor)
            if verb == "contracts":
                return _format(svc.contracts())
            if verb == "contract" and len(rest) == 2 and rest[0] == "test":
                return _test_with_choice(svc, rest[1], actor, ask)
            if verb == "contract" and len(rest) == 3 and rest[0] == "test":
                return _format(svc.test_contract(rest[1], actor, digest=rest[2]))
            if verb == "contract" and len(rest) == 2 and rest[0] == "approve":
                return svc.approve_contract(rest[1], actor)
            if verb == "contract" and len(rest) == 2 and rest[0] == "revoke":
                return svc.revoke_contract(rest[1], actor)
            if verb == "export" and len(rest) == 1:
                bundle = svc.export_evidence(rest[0])
                return "Evidence written to:\n" + "\n".join(f"  {f}" for f in bundle.get("files", [])) \
                    if bundle.get("files") else _format(bundle)
            if verb == "cancel" and len(rest) == 1:
                return view(svc.cancel(rest[0], actor), render.evidence)
            if verb == "prefer" and len(rest) == 1 and rest[0] in ("recommend", "compare", "ask"):
                return svc.prefer(actor, rest[0])
            if verb == "confirm" and len(rest) == 2:
                return view(svc.confirm(rest[0], rest[1], actor), render.evidence)
            if verb == "resolve" and len(rest) == 3 and rest[2] in ("applied", "not-applied"):
                return view(svc.resolve(rest[0], rest[1], rest[2] == "applied", actor), render.evidence)
        except (ServiceError, StoreError, IllegalTransition) as exc:
            return f"Not done: {exc}"
        return COMMAND_HELP

    return handle


def _test_with_choice(svc: Capalis, capability_id: str, actor: Actor, ask: Ask | None) -> str:
    """A contract test makes one real call, so the owner sees the exact tool and inputs first. The choice is
    bound to the contract version shown: if it changes meanwhile, nothing runs."""
    preview = svc.contract_preview(capability_id, actor)
    shown = json.dumps(preview["inputs"], indent=2, ensure_ascii=False)
    question = f"Run a test of {capability_id}? It calls {preview['tool']} once with:\n{shown}"
    answer = ask(question, [{"value": "run", "label": "Run the test"}, {"value": "cancel", "label": "Not now"}]) \
        if ask is not None else None
    if answer is None:
        return (f"{question}\n\nTo run exactly this, send: /capalis contract test {capability_id} "
                f"{preview['digest']}")
    if answer != "run":
        return "Not run. Nothing was called."
    return _format(svc.test_contract(capability_id, actor, digest=preview["digest"]))


def _approve_with_choice(svc: Capalis, recipe_id: str, actor: Actor, ask: Ask | None) -> str:
    """Approve with a button. The choice is bound to the version shown: if a newer version appears meanwhile,
    the digest no longer matches and nothing is approved."""
    svc.authorize_actor(actor)
    latest = svc.store.latest(recipe_id)
    if latest is None:
        return f"No Recipe named {recipe_id}."
    recipe, state = latest
    desc = svc.describe(recipe)
    if state.value != "awaiting_approval":
        return f"{recipe_id} v{recipe.version} is {state.value.replace('_', ' ')}; there is nothing to approve."
    question = f"Approve {recipe_id} v{recipe.version}?\n\n{render.plain(desc)}"
    answer = ask(question, [{"value": "approve", "label": "Approve"}, {"value": "cancel", "label": "Not now"}]) \
        if ask is not None else None
    if answer is None:
        return render.recipe(desc) + "\n(No buttons here: type the approve command above.)"
    if answer != "approve":
        return "Not approved. Nothing changed."
    return svc.approve(recipe_id, recipe.digest.removeprefix("sha256:"), actor)
