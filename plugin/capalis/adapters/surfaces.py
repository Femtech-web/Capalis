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
        "action": {"type": "string", "enum": ["template", "compile", "qualify", "show", "list"]},
        "recipe_id": {"type": "string", "description": "3-40 lowercase letters, digits or dashes"},
        "draft": {**_OBJ, "description": "the Recipe draft (compile)"},
        "sample_payload": {**_OBJ, "description": "a realistic trigger payload for the shadow run (qualify)"},
        "repo": {"type": "string",
                 "description": "template: repository name or owner/repo, as the user said it"},
        "slack_channel": {"type": "string", "description": "template: Slack channel name (#engineering) or id"},
        "discord_channel": {"type": "string", "description": "template: Discord channel name (#bugs) or id"},
        "reporters": {"type": "array", "items": {"type": "string"},
                      "description": "template: who may report: Discord usernames (@ada) or user ids"},
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
        "action": {"type": "string", "enum": ["start", "evidence", "list"]},
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

PAUSE_SCHEMA: dict[str, Any] = {
    "name": "capalis_pause",
    "description": ("Pause one live Recipe, or every Recipe when recipe_id is omitted. Runs in progress hold before "
                    "their next write. Only the owner can resume (/capalis resume)."),
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "recipe_id": {"type": "string"}, "reason": {"type": "string"}}},
}

TOOLS = (STATUS_SCHEMA, SETUP_SCHEMA, CAPABILITIES_SCHEMA, RECIPE_SCHEMA, RUN_SCHEMA, PAUSE_SCHEMA)


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
            if action == "template":
                return svc.bug_report_template(repo=args["repo"], slack_channel=args["slack_channel"],
                                               discord_channel=args["discord_channel"],
                                               reporters=list(args.get("reporters") or []),
                                               reply_when_closed=bool(args.get("reply_when_closed", True)))
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
            if action == "list":
                return [svc.run_line(r) for r in svc.store.runs(args.get("recipe_id"), limit=20)]
            raise ServiceError(f"unknown action {action!r}")

        return _guard(go)

    def pause(args: dict[str, Any], **_: Any) -> str:
        return _guard(lambda: {"result": service().pause(args.get("recipe_id"), actor="agent",
                                                         reason=str(args.get("reason") or ""))})

    def setup(args: dict[str, Any], **_: Any) -> str:
        return _guard(lambda: service().setup_report())

    return {"capalis_status": status, "capalis_setup": setup, "capalis_capabilities": capabilities,
            "capalis_recipe": recipe,
            "capalis_run": run, "capalis_pause": pause}


COMMAND_HELP = """/capalis                         status
/capalis setup                      what is connected, and what to fix
/capalis approve <recipe>           review and approve with a button
/capalis approve <recipe> <digest> approve the reviewed version by its digest (from 'show')
/capalis show <recipe>              what a Recipe does, its targets and checks
/capalis runs [recipe]              recent runs
/capalis evidence <run>             steps and independent checks for a run
/capalis pause [recipe]             pause one Recipe or all
/capalis resume [recipe]            resume after a pause
/capalis revoke <recipe>            withdraw approval
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
            if verb == "confirm" and len(rest) == 2:
                return view(svc.confirm(rest[0], rest[1], actor), render.evidence)
            if verb == "resolve" and len(rest) == 3 and rest[2] in ("applied", "not-applied"):
                return view(svc.resolve(rest[0], rest[1], rest[2] == "applied", actor), render.evidence)
        except (ServiceError, StoreError, IllegalTransition) as exc:
            return f"Not done: {exc}"
        return COMMAND_HELP

    return handle


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
    question = f"{render.plain(desc)}\nApprove {recipe_id} v{recipe.version} ({desc['short_digest']})?"
    answer = ask(question, [{"value": "approve", "label": "Approve"}, {"value": "cancel", "label": "Not now"}]) \
        if ask is not None else None
    if answer is None:
        return render.recipe(desc) + "\n(No buttons here: type the approve command above.)"
    if answer != "approve":
        return "Not approved. Nothing changed."
    return svc.approve(recipe_id, recipe.digest.removeprefix("sha256:"), actor)
